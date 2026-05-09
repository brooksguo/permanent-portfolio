"""
Flask 后端入口。

启动流程：
1. 初始化/迁移数据库
2. 从 akshare 增量拉取最新价格
3. 若无持仓则按 START_DATE 价格初始化
4. 启动 HTTP 服务，监听 localhost:5000
"""

import json
import sqlite3
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import config
from calculator import (
    build_portfolio_timeseries,
    calculate_annualized_return,
    calculate_current_drawdown,
    calculate_max_drawdown,
    check_rebalance_needed,
    get_current_holdings,
    get_daily_price_change,
    get_latest_prices,
    get_pyramid_signals,
    auto_execute_pyramid_signals,
)
from data_fetcher import backfill_dividends, ensure_holdings_initialized, update_benchmark_prices, update_prices
from database import get_db, init_db, load_settings_into_config, save_setting

app = Flask(__name__)

# 模块加载时从数据库读取持久化设置（兼容 python app.py 和 flask run）
_boot_conn = init_db()
load_settings_into_config(_boot_conn)
_boot_conn.close()


# ──────────────────────────────────────────────
# 页面
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ──────────────────────────────────────────────
# 数据接口
# ──────────────────────────────────────────────

@app.route("/api/data")
def get_data():
    """页面加载时调用，返回当前组合全量数据（使用已缓存价格）。"""
    conn = get_db()
    ensure_holdings_initialized(conn)
    return jsonify(_build_portfolio_data(conn))


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """手动刷新：增量拉取最新数据后返回更新后的组合数据。"""
    conn = get_db()
    update_results = update_prices(conn)
    update_benchmark_prices(conn)
    ensure_holdings_initialized(conn)
    latest_prices = get_latest_prices(conn)
    auto_executed = auto_execute_pyramid_signals(conn, latest_prices)
    data = _build_portfolio_data(conn)
    data["refresh_results"] = update_results
    data["auto_executed_pyramid"] = auto_executed
    return jsonify(data)


@app.route("/api/history")
def get_history():
    """返回每日历史数据：组合总值、日涨跌、各资产收盘价。"""
    conn = get_db()
    timeseries = build_portfolio_timeseries(conn)

    # 各资产每日收盘价，按日期升序
    price_rows = conn.execute(
        "SELECT date, code, close FROM price_history WHERE date >= ? ORDER BY date",
        (config.START_DATE,),
    ).fetchall()
    prices_by_date: dict = {}
    for r in price_rows:
        prices_by_date.setdefault(r["date"], {})[r["code"]] = round(r["close"], 4)

    # 组合总值按日期索引
    ts_by_date = {t["date"]: t["total"] for t in timeseries}

    # 合并日期列表（升序），计算日涨跌
    all_dates = sorted(set(list(prices_by_date) + list(ts_by_date)))
    rows_asc = []
    prev_total = None
    for d in all_dates:
        total = ts_by_date.get(d)
        daily_chg = round((total - prev_total) / prev_total, 4) if (total and prev_total) else None
        if total:
            prev_total = total
        rows_asc.append({
            "date": d,
            "total": round(total, 2) if total else None,
            "daily_change": daily_chg,
            "prices": prices_by_date.get(d, {}),
        })

    return jsonify({
        "codes": list(config.ASSETS.keys()),
        "names": {code: cfg["name"] for code, cfg in config.ASSETS.items()},
        "start_date": config.START_DATE,
        "rows": list(reversed(rows_asc)),   # 最新在前
    })


# ──────────────────────────────────────────────


# ──────────────────────────────────────────────
# 金字塔加仓接口
# ──────────────────────────────────────────────

@app.route("/api/pyramid/rules", methods=["POST"])
def save_pyramid_rules():
    """保存金字塔加仓规则（不影响持仓数据）。"""
    data = request.get_json() or {}
    new_pyramid = data.get("pyramid_rules", {})
    config.PYRAMID_RULES = {
        code: [t for t in tiers if t.get("amount", 0) > 0 and t.get("drop", 0) > 0]
        for code, tiers in new_pyramid.items()
        if any(t.get("amount", 0) > 0 and t.get("drop", 0) > 0 for t in tiers)
    }
    conn = get_db()
    save_setting(conn, "pyramid_rules", json.dumps(config.PYRAMID_RULES, ensure_ascii=False))
    return jsonify({"success": True, "pyramid_config": config.PYRAMID_RULES})


@app.route("/api/pyramid/execute", methods=["POST"])
def execute_pyramid():
    data = request.get_json()
    code = data.get("code")
    tier_drop = data.get("tier_drop")
    amount = data.get("amount")

    if not code or tier_drop is None or not amount:
        return jsonify({"error": "缺少参数"}), 400

    conn = get_db()
    latest_prices = get_latest_prices(conn)

    if code not in latest_prices:
        return jsonify({"error": f"{code} 暂无价格数据"}), 400
    if "511880" not in latest_prices:
        return jsonify({"error": "现金 ETF 暂无价格数据"}), 400

    asset_price = latest_prices[code]["price"]
    cash_price = latest_prices["511880"]["price"]
    today = datetime.now().strftime("%Y-%m-%d")

    # 检查现金是否充足
    cash_shares = conn.execute(
        "SELECT COALESCE(SUM(shares_delta), 0) FROM transactions WHERE code = '511880'"
    ).fetchone()[0]
    cash_value = cash_shares * cash_price

    if cash_value < amount:
        return jsonify({
            "error": f"现金不足：当前现金市值 {cash_value:,.2f} 元，需要 {amount:,.2f} 元"
        }), 400

    shares_to_buy = amount / asset_price
    cash_shares_to_sell = amount / cash_price

    conn.execute(
        """INSERT INTO transactions
           (date, code, shares_delta, price, amount, reason, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (today, code, shares_to_buy, asset_price, amount,
         "pyramid", f"金字塔加仓 {tier_drop:.0%} 档位"),
    )
    conn.execute(
        """INSERT INTO transactions
           (date, code, shares_delta, price, amount, reason, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (today, "511880", -cash_shares_to_sell, cash_price, -amount,
         "pyramid", f"减现金 → {code}"),
    )
    conn.execute(
        """INSERT INTO pyramid_executions
           (execution_date, code, tier_drop, amount,
            shares_bought, cash_shares_sold, price, cash_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, code, tier_drop, amount,
         shares_to_buy, cash_shares_to_sell, asset_price, cash_price),
    )
    conn.commit()
    return jsonify({"success": True, "shares_bought": round(shares_to_buy, 4)})


# ──────────────────────────────────────────────
# 资金投入 / 取出接口
# ──────────────────────────────────────────────

@app.route("/api/invest", methods=["POST"])
def invest():
    """按目标比例追加投入资金。"""
    data = request.get_json()
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "金额必须大于 0"}), 400

    conn = get_db()
    latest_prices = get_latest_prices(conn)
    today = datetime.now().strftime("%Y-%m-%d")
    details = []

    for code, asset_cfg in config.ASSETS.items():
        if code not in latest_prices:
            continue
        price = latest_prices[code]["price"]
        buy_amount = round(amount * asset_cfg["target"], 2)
        shares = buy_amount / price
        conn.execute(
            """INSERT INTO transactions (date, code, shares_delta, price, amount, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, code, shares, price, buy_amount, "invest", f"追加投入 {buy_amount:.2f} 元"),
        )
        details.append({
            "code": code, "name": asset_cfg["name"],
            "amount": buy_amount, "shares": round(shares, 4),
            "price": round(price, 4), "action": "买入",
        })

    conn.execute(
        """INSERT INTO rebalance_records (trigger_date, trigger_type, details_json, is_executed, executed_at)
           VALUES (?, ?, ?, 1, ?)""",
        (today, "资金投入", json.dumps(details, ensure_ascii=False), today),
    )
    conn.commit()
    return jsonify({"success": True, "total": amount, "details": details})


@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    """按目标比例从各资产等比赎回，使取出后持仓仍符合目标配比。"""
    data = request.get_json()
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "金额必须大于 0"}), 400

    conn = get_db()
    latest_prices = get_latest_prices(conn)
    holdings = get_current_holdings(conn)

    # 校验总持仓充足
    total_value = sum(
        holdings.get(code, 0) * latest_prices[code]["price"]
        for code in config.ASSETS if code in latest_prices
    )
    if total_value < amount - 0.01:
        return jsonify({"error": f"持仓不足：当前总资产 ¥{total_value:,.2f}，不足 ¥{amount:,.2f}"}), 400

    # 校验各资产单独充足
    for code, asset_cfg in config.ASSETS.items():
        if code not in latest_prices:
            continue
        price = latest_prices[code]["price"]
        sell_amount = round(amount * asset_cfg["target"], 2)
        available_amount = holdings.get(code, 0) * price
        if available_amount < sell_amount - 0.01:
            return jsonify({
                "error": f"{asset_cfg['name']} 持仓不足：当前市值 ¥{available_amount:,.2f}，按比例需赎回 ¥{sell_amount:,.2f}"
            }), 400

    today = datetime.now().strftime("%Y-%m-%d")
    details = []
    for code, asset_cfg in config.ASSETS.items():
        if code not in latest_prices:
            continue
        price = latest_prices[code]["price"]
        sell_amount = round(amount * asset_cfg["target"], 2)
        if sell_amount < 0.01:
            continue
        shares_to_sell = sell_amount / price
        conn.execute(
            """INSERT INTO transactions (date, code, shares_delta, price, amount, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, code, -shares_to_sell, price, -sell_amount,
             "withdraw", f"按比例取出 {sell_amount:.2f} 元"),
        )
        details.append({
            "code": code, "name": asset_cfg["name"],
            "amount": sell_amount, "shares": round(shares_to_sell, 4),
            "price": round(price, 4), "action": "卖出",
        })

    conn.execute(
        """INSERT INTO rebalance_records (trigger_date, trigger_type, details_json, is_executed, executed_at)
           VALUES (?, ?, ?, 1, ?)""",
        (today, "资金取出", json.dumps(details, ensure_ascii=False), today),
    )
    conn.commit()
    return jsonify({"success": True, "amount": amount, "details": details})


# ──────────────────────────────────────────────
# 设置接口
# ──────────────────────────────────────────────

@app.route("/api/reinitialize", methods=["POST"])
def reinitialize():
    """清空交易记录，用新设置重新建仓。"""
    data = request.get_json() or {}
    new_total = data.get("total_assets", config.TOTAL_ASSETS)
    new_start = data.get("start_date", "")
    new_assets = data.get("assets", None)  # [{code, name, target}, ...]

    # 统一 start_date 为 YYYY-MM-DD
    if new_start:
        s = new_start.replace("-", "")
        new_start = f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else config.START_DATE
    else:
        new_start = config.START_DATE

    # 校验并应用资产配置
    if new_assets:
        total_pct = sum(a.get("target", 0) for a in new_assets)
        if abs(total_pct - 1.0) > 0.001:
            return jsonify({"error": f"各资产比例之和须为100%，当前为 {total_pct*100:.1f}%"}), 400
        for a in new_assets:
            if not a.get("code") or not a.get("name"):
                return jsonify({"error": "每个资产必须填写代码和名称"}), 400
        config.ASSETS = {a["code"]: {"name": a["name"], "target": a["target"]} for a in new_assets}

    config.TOTAL_ASSETS = int(new_total)
    config.START_DATE = new_start

    conn = get_db()
    save_setting(conn, "start_date",   config.START_DATE)
    save_setting(conn, "total_assets", str(config.TOTAL_ASSETS))
    if new_assets:
        save_setting(conn, "assets", json.dumps(
            [{"code": c, "name": v["name"], "target": v["target"]} for c, v in config.ASSETS.items()],
            ensure_ascii=False,
        ))
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM rebalance_records")
    conn.execute("DELETE FROM pyramid_executions")
    conn.execute("DELETE FROM price_history")
    conn.commit()

    update_prices(conn)
    ensure_holdings_initialized(conn)
    backfill_dividends(conn)
    return jsonify({"success": True, "total_assets": config.TOTAL_ASSETS, "start_date": config.START_DATE})


# ──────────────────────────────────────────────
# 核心数据构建
# ──────────────────────────────────────────────

def _build_portfolio_data(conn: sqlite3.Connection) -> dict:
    latest_prices = get_latest_prices(conn)
    holdings = get_current_holdings(conn)

    # ── 基金市值（不含分红现金，用于再平衡比例计算）──
    market_value = sum(
        holdings.get(code, 0) * latest_prices[code]["price"]
        for code in config.ASSETS
        if code in latest_prices
    )

    # 累计现金分红
    cumul_div_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE reason='dividend'"
    ).fetchone()
    cumulative_dividends = float(cumul_div_row[0]) if cumul_div_row and cumul_div_row[0] else 0.0

    # 总资产 = 基金市值 + 累计现金分红
    total_value = market_value + cumulative_dividends

    # ── 各资产明细 ──
    assets_data = []
    for code, asset_cfg in config.ASSETS.items():
        price_info = latest_prices.get(code, {})
        price = price_info.get("price", 0.0)
        shares = holdings.get(code, 0.0)
        value = shares * price
        current_ratio = value / market_value if market_value > 0 else 0
        deviation = current_ratio - asset_cfg["target"]

        # 距历史高点：(当前价 - 历史最高价) / 历史最高价，负=低于高点，正=创新高
        dist_from_high = None
        if price:
            row = conn.execute(
                "SELECT MAX(close) AS mx FROM price_history WHERE code = ? AND date >= ?",
                (code, config.START_DATE),
            ).fetchone()
            if row and row["mx"]:
                dist_from_high = (price - row["mx"]) / row["mx"]

        # 起始日期以来收益率：价格涨跌 + 累计现金分红（每份）
        return_since_2026 = None
        if price:
            row_2026 = conn.execute(
                "SELECT close FROM price_history WHERE code = ? AND date >= ? ORDER BY date ASC LIMIT 1",
                (code, config.START_DATE),
            ).fetchone()
            if row_2026 and row_2026["close"]:
                initial_price = row_2026["close"]
                # 累计每份分红：各笔分红金额 ÷ 除息日前持仓份额
                div_txs = conn.execute(
                    "SELECT date, amount FROM transactions "
                    "WHERE code=? AND reason='dividend' AND date>=? ORDER BY date",
                    (code, config.START_DATE),
                ).fetchall()
                cumul_div_per_share = 0.0
                for dtx in div_txs:
                    shares_then = conn.execute(
                        "SELECT COALESCE(SUM(shares_delta), 0) FROM transactions "
                        "WHERE code=? AND date<?",
                        (code, dtx["date"]),
                    ).fetchone()[0] or 0.0
                    if shares_then > 0:
                        cumul_div_per_share += dtx["amount"] / shares_then
                return_since_2026 = (price - initial_price + cumul_div_per_share) / initial_price

        assets_data.append({
            "code": code,
            "name": asset_cfg["name"],
            "price": round(price, 4),
            "price_date": price_info.get("date", ""),
            "price_change_pct": round(get_daily_price_change(conn, code), 4),
            "shares": round(shares, 4),
            "current_value": round(value, 2),
            "current_ratio": round(current_ratio, 4),
            "target_ratio": asset_cfg["target"],
            "deviation": round(deviation, 4),
            "dist_from_high": round(dist_from_high, 4) if dist_from_high is not None else None,
            "return_since_2026": round(return_since_2026, 4) if return_since_2026 is not None else None,
        })

    # ── 净值时间序列 ──
    timeseries = build_portfolio_timeseries(conn)
    values_list = [t["total"] for t in timeseries]

    # 净投入成本（用于展示）：init + invest 买入之和 − withdraw 取出之和
    net_invested_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE reason IN ('init', 'invest', 'withdraw')"
    ).fetchone()
    net_invested = float(net_invested_row[0]) if net_invested_row and net_invested_row[0] else float(config.TOTAL_ASSETS)

    # 简单加权（Modified Dietz）分母：每笔外部现金流按"剩余持有时间"加权
    # 权重 = (总天数 − 该笔现金流距起始天数) / 总天数
    # 期末取出权重趋近 0，不会压低分母；期初投入权重为 1
    cf_rows = conn.execute(
        """SELECT date, SUM(amount) AS cf
           FROM transactions
           WHERE reason IN ('init', 'invest', 'withdraw')
           GROUP BY date
           ORDER BY date"""
    ).fetchall()

    # 现金流日期映射（供回撤调整和图表共用）
    cf_by_date_chart = {r["date"]: float(r["cf"]) for r in cf_rows}

    # 回撤用现金流调整序列：total - 累计净投入 + 初始投入
    # 追加/取出资金时 total 和累计净投入同步变动，调整后值不受扰动
    _cumul_adj = 0.0
    _initial_adj: float | None = None
    adjusted_values: list = []
    for t in timeseries:
        _cumul_adj += cf_by_date_chart.get(t["date"], 0.0)
        if _initial_adj is None:
            _initial_adj = _cumul_adj
        adjusted_values.append(t["total"] - _cumul_adj + (_initial_adj or 0.0))
    end_date_str = timeseries[-1]["date"] if timeseries else datetime.now().strftime("%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    first_cf_dt = datetime.strptime(cf_rows[0]["date"], "%Y-%m-%d") if cf_rows else end_dt
    total_days = (end_dt - first_cf_dt).days
    weighted_denominator = 0.0
    for row in cf_rows:
        cf_dt = datetime.strptime(row["date"], "%Y-%m-%d")
        days_elapsed = (cf_dt - first_cf_dt).days
        weight = (total_days - days_elapsed) / total_days if total_days > 0 else 1.0
        weight = max(0.0, min(1.0, weight))
        weighted_denominator += weight * float(row["cf"])
    if weighted_denominator <= 0:
        weighted_denominator = net_invested or float(config.TOTAL_ASSETS)

    total_return = total_value - net_invested
    total_return_rate = total_return / weighted_denominator if weighted_denominator else 0
    max_dd = calculate_max_drawdown(adjusted_values)
    cur_dd = calculate_current_drawdown(adjusted_values)
    # 年化收益率：基于 Modified Dietz 总收益率直接复利折算，避免取出现金影响终值
    annualized = 0.0
    if timeseries and total_return_rate > -1:
        try:
            first_dt = datetime.strptime(timeseries[0]["date"], "%Y-%m-%d")
            last_dt = datetime.strptime(timeseries[-1]["date"], "%Y-%m-%d")
            days = (last_dt - first_dt).days
            if days > 0:
                annualized = (1 + total_return_rate) ** (365.25 / days) - 1
        except Exception:
            pass

    # ── 再平衡（自动执行） ──
    today_str = datetime.now().strftime("%Y-%m-%d")

    def _auto_execute_rebalance(rb_id, rb_total, rb_prices, rb_holdings):
        """根据当时市值和持仓，写入调仓 transactions 并标记已执行。"""
        for code, asset_cfg in config.ASSETS.items():
            if code not in rb_prices:
                continue
            price = rb_prices[code]["price"]
            current_shares = rb_holdings.get(code, 0)
            target_shares = (rb_total * asset_cfg["target"]) / price
            shares_diff = target_shares - current_shares
            amount = shares_diff * price
            if abs(amount) < 1:
                continue
            conn.execute(
                """INSERT INTO transactions
                   (date, code, shares_delta, price, amount, reason, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (today_str, code, shares_diff, price, amount,
                 "rebalance", f"自动再平衡 #{rb_id}"),
            )
        conn.execute(
            "UPDATE rebalance_records SET is_executed=1, executed_at=? WHERE id=?",
            (today_str, rb_id),
        )
        conn.commit()

    # 处理历史遗留的未执行记录（再平衡基于基金市值，不含分红现金）
    pending_old = conn.execute(
        "SELECT * FROM rebalance_records WHERE is_executed=0"
    ).fetchall()
    for p in pending_old:
        old_market_value = sum(
            holdings.get(c, 0) * latest_prices[c]["price"]
            for c in config.ASSETS if c in latest_prices
        )
        _auto_execute_rebalance(p["id"], old_market_value, latest_prices, holdings)
        holdings = get_current_holdings(conn)

    # 检测是否需要再平衡，需要则立即自动执行
    new_rb = check_rebalance_needed(conn, latest_prices, holdings)
    if new_rb:
        cur = conn.execute(
            """INSERT INTO rebalance_records
               (trigger_date, trigger_type, details_json)
               VALUES (?, ?, ?)""",
            (new_rb["trigger_date"], new_rb["trigger_type"], json.dumps(new_rb["suggestions"])),
        )
        rb_id = cur.lastrowid
        conn.commit()
        _auto_execute_rebalance(rb_id, new_rb["total_value"], latest_prices, holdings)
        holdings = get_current_holdings(conn)

    # 取最近一次已执行的再平衡记录（只取偏离/季度调仓，排除资金投入/取出）
    last_rb = conn.execute(
        """SELECT * FROM rebalance_records
           WHERE is_executed=1
             AND trigger_type NOT IN ('资金投入', '资金取出')
           ORDER BY executed_at DESC LIMIT 1"""
    ).fetchone()

    # 下次季度再平衡日期
    today_dt = datetime.now()
    next_dates = []
    for year in [today_dt.year, today_dt.year + 1]:
        for month in [1, 4, 7, 10]:
            d = datetime(year, month, 1)
            if d.date() > today_dt.date():
                next_dates.append(d)
    next_rebalance_date = min(next_dates).strftime("%Y-%m-%d") if next_dates else "—"

    rebalance_data = {
        "next_date": next_rebalance_date,
        "last": {
            "trigger_date": last_rb["trigger_date"],
            "executed_at": last_rb["executed_at"],
            "trigger_type": last_rb["trigger_type"],
            "suggestions": json.loads(last_rb["details_json"]),
        } if last_rb else None,
    }

    # ── 金字塔信号 ──
    pyramid_signals = get_pyramid_signals(conn, latest_prices)

    # ── 再平衡历史 ──
    rebalance_rows = conn.execute(
        """SELECT id, trigger_date, trigger_type, is_executed, executed_at, details_json
           FROM rebalance_records ORDER BY trigger_date DESC LIMIT 30"""
    ).fetchall()
    rebalance_history = [
        {
            "record_type": "rebalance",
            "id": h["id"],
            "date": h["trigger_date"],
            "trigger_type": h["trigger_type"],
            "is_executed": bool(h["is_executed"]),
            "executed_at": h["executed_at"],
            "details": json.loads(h["details_json"]),
        }
        for h in rebalance_rows
    ]

    # ── 金字塔加仓历史 ──
    pyramid_rows = conn.execute(
        """SELECT p.id, p.execution_date, p.code, p.tier_drop, p.amount,
                  p.shares_bought, p.price
           FROM pyramid_executions p
           ORDER BY p.execution_date DESC LIMIT 30"""
    ).fetchall()
    pyramid_history = [
        {
            "record_type": "pyramid",
            "id": p["id"],
            "date": p["execution_date"],
            "code": p["code"],
            "name": config.ASSETS.get(p["code"], {}).get("name", p["code"]),
            "tier_drop": p["tier_drop"],
            "amount": p["amount"],
            "shares_bought": p["shares_bought"],
            "price": p["price"],
        }
        for p in pyramid_rows
    ]

    # 合并两类历史，按日期降序
    combined_history = sorted(
        rebalance_history + pyramid_history,
        key=lambda x: x["date"],
        reverse=True,
    )[:50]

    # ── 图表数据 ──
    chart_dates = [t["date"] for t in timeseries]

    # 组合净值：与汇总卡统一 —— (当日总值 - 累计净投入) / 初始总资产
    # 净投入 = init + invest + withdraw 之和（不含 pyramid，与卡片计算口径一致）
    # cf_by_date_chart 已在上方计算，此处直接复用
    chart_portfolio = []
    cumulative_invested = 0.0
    for t in timeseries:
        cumulative_invested += cf_by_date_chart.get(t["date"], 0.0)
        gain = t["total"] - cumulative_invested
        chart_portfolio.append(round(gain / config.TOTAL_ASSETS, 4) if config.TOTAL_ASSETS else 0)

    # 各资产净值（以各自初始市值归一化）
    init_asset_values: dict = {}
    for t in timeseries:
        for code, val in t["assets"].items():
            if code not in init_asset_values:
                init_asset_values[code] = val

    chart_assets: dict = {code: [] for code in config.ASSETS}
    for t in timeseries:
        for code in config.ASSETS:
            init_val = init_asset_values.get(code, 0)
            cur_val = t["assets"].get(code, 0)
            chart_assets[code].append(
                round(cur_val / init_val, 4) if init_val else 0
            )

    # ── 基准指数对比（沪深300 / 标普500） ──
    def _align_and_normalize(price_map: dict, dates: list) -> list:
        """按组合日期前向填充，归一化为累计收益率序列。"""
        aligned, last = [], None
        for d in dates:
            if d in price_map:
                last = price_map[d]
            aligned.append(last)
        base = next((v for v in aligned if v is not None), None)
        if not base:
            return [None] * len(dates)
        return [round(v / base - 1, 4) if v is not None else None for v in aligned]

    def _load_benchmark_from_db(db_code: str, start_date: str, end_date: str) -> dict:
        """从 price_history 读取基准数据，返回 {date_str: close_price} 字典。"""
        rows = conn.execute(
            "SELECT date, close FROM price_history WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            (db_code, start_date, end_date),
        ).fetchall()
        return {r["date"]: r["close"] for r in rows}

    if chart_dates:
        csi300_prices = _load_benchmark_from_db("IDX_000300", chart_dates[0], chart_dates[-1])
        chart_csi300 = _align_and_normalize(csi300_prices, chart_dates)

        sp500_prices = _load_benchmark_from_db("017641", chart_dates[0], chart_dates[-1])
        chart_sp500 = _align_and_normalize(sp500_prices, chart_dates)
    else:
        chart_csi300 = []
        chart_sp500  = []

    # ── 各资产贡献 ──
    # 用各资产自身的价格涨幅（return_since_2026）衡量贡献，避免再平衡后持仓变化的干扰
    contributions = []
    assets_by_code = {a["code"]: a for a in assets_data}
    for code, asset_cfg in config.ASSETS.items():
        init_amount = config.TOTAL_ASSETS * asset_cfg["target"]
        ret = assets_by_code.get(code, {}).get("return_since_2026")
        gain_rate = ret if ret is not None else 0
        gain = init_amount * gain_rate
        contributions.append({
            "code": code,
            "name": asset_cfg["name"],
            "initial_amount": round(init_amount, 2),
            "gain": round(gain, 2),
            "gain_rate": round(gain_rate, 4),
        })

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_assets_initial": round(net_invested, 2),
        "start_date": config.START_DATE,
        "total_value": round(total_value, 2),
        "total_return": round(total_return, 2),
        "total_return_rate": round(total_return_rate, 4),
        "annualized_return": round(annualized, 4),
        "max_drawdown": round(max_dd, 4),
        "current_drawdown": round(cur_dd, 4),
        "assets": assets_data,
        "assets_config": [
            {"code": c, "name": v["name"], "target": v["target"]}
            for c, v in config.ASSETS.items()
        ],
        "pyramid_config": config.PYRAMID_RULES,
        "rebalance": rebalance_data,
        "pyramid": pyramid_signals,
        "rebalance_history": combined_history,
        "contributions": contributions,
        "chart": {
            "dates": chart_dates,
            "portfolio": chart_portfolio,
            "assets": chart_assets,
            "csi300": chart_csi300,
            "sp500": chart_sp500,
        },
    }


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("永久投资组合管理系统")
    print("=" * 50)
    conn = get_db()
    print(f"[设置] 起始日期：{config.START_DATE}，初始资产：{config.TOTAL_ASSETS:,} 元")
    print("\n[步骤 1/3] 拉取最新数据...")
    update_prices(conn)
    print("\n[步骤 1b] 拉取基准指数数据...")
    update_benchmark_prices(conn)
    print("\n[步骤 2/3] 检查持仓初始化...")
    ensure_holdings_initialized(conn)
    print("\n[步骤 3/3] 检查金字塔加仓信号...")
    latest_prices = get_latest_prices(conn)
    auto_executed = auto_execute_pyramid_signals(conn, latest_prices)
    if not auto_executed:
        print("[INFO] 无需自动加仓")
    print("\n启动服务：http://localhost:5000  /  http://192.168.31.228:5000（局域网）")
    print("按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
