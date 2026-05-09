"""
计算模块：
- 持仓 / 净值时间序列
- 再平衡触发检测
- 金字塔加仓信号
- 最大回撤 / 年化收益
"""

import json
import sqlite3
from datetime import datetime

import config


# ──────────────────────────────────────────────
# 基础查询
# ──────────────────────────────────────────────

def get_latest_prices(conn: sqlite3.Connection) -> dict:
    """返回 {code: {"price": float, "date": str}} 各资产最新后复权收盘价。"""
    result = {}
    for code in config.ASSETS:
        row = conn.execute(
            "SELECT date, close FROM price_history WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        if row:
            result[code] = {"price": row["close"], "date": row["date"]}
    return result


def get_current_holdings(conn: sqlite3.Connection) -> dict:
    """返回 {code: shares} 当前各资产持有份额。"""
    rows = conn.execute(
        "SELECT code, SUM(shares_delta) AS shares FROM transactions GROUP BY code"
    ).fetchall()
    return {r["code"]: r["shares"] for r in rows if r["shares"] and r["shares"] > 1e-6}


def get_daily_price_change(conn: sqlite3.Connection, code: str) -> float:
    """返回最近一个交易日涨跌幅（小数形式）。"""
    rows = conn.execute(
        "SELECT close FROM price_history WHERE code = ? ORDER BY date DESC LIMIT 2",
        (code,),
    ).fetchall()
    if len(rows) < 2:
        return 0.0
    latest, prev = rows[0]["close"], rows[1]["close"]
    return (latest - prev) / prev if prev else 0.0


# ──────────────────────────────────────────────
# 净值时间序列
# ──────────────────────────────────────────────

def build_portfolio_timeseries(conn: sqlite3.Connection) -> list:
    """
    构建每日组合净值序列。

    算法：
    1. 将所有价格加载到内存（按日期升序），并做前向填充。
    2. 将所有交易按日期顺序应用，得到每日持仓。
    3. 计算每日总市值及各资产市值。
    返回 [{"date": str, "total": float, "assets": {code: float}}, ...]
    """
    # 1. 加载所有价格
    all_price_rows = conn.execute(
        "SELECT date, code, close FROM price_history ORDER BY date"
    ).fetchall()
    if not all_price_rows:
        return []

    # 按日期聚合
    prices_by_date: dict = {}
    for r in all_price_rows:
        d = r["date"]
        if d not in prices_by_date:
            prices_by_date[d] = {}
        prices_by_date[d][r["code"]] = r["close"]

    # 2. 加载所有交易
    txs = conn.execute(
        "SELECT date, code, shares_delta FROM transactions ORDER BY date, id"
    ).fetchall()

    # 现金分红累计：按日期汇总（shares_delta=0 的 dividend 交易）
    div_rows = conn.execute(
        "SELECT date, SUM(amount) AS income FROM transactions WHERE reason='dividend' GROUP BY date ORDER BY date"
    ).fetchall()
    div_income_by_date: dict = {r["date"]: float(r["income"]) for r in div_rows}

    trading_dates = sorted(prices_by_date.keys())
    filled_prices: dict = {}   # 前向填充的最新价格
    holdings: dict = {}        # 当前持仓
    tx_idx = 0
    cumul_div = 0.0            # 累计现金分红
    result = []

    for d in trading_dates:
        # 应用当天及之前所有交易
        while tx_idx < len(txs) and txs[tx_idx]["date"] <= d:
            tx = txs[tx_idx]
            holdings[tx["code"]] = holdings.get(tx["code"], 0.0) + tx["shares_delta"]
            tx_idx += 1

        # 累计当日分红现金
        cumul_div += div_income_by_date.get(d, 0.0)

        # 更新前向填充价格
        for code, price in prices_by_date[d].items():
            filled_prices[code] = price

        if not holdings:
            continue

        market_value = 0.0
        asset_values: dict = {}
        for code, shares in holdings.items():
            price = filled_prices.get(code)
            if price and shares > 0:
                val = shares * price
                market_value += val
                asset_values[code] = round(val, 2)

        total = market_value + cumul_div
        if total > 0:
            result.append({
                "date": d,
                "total": round(total, 2),
                "assets": asset_values,
            })

    return result


# ──────────────────────────────────────────────
# 收益 / 回撤
# ──────────────────────────────────────────────

def calculate_max_drawdown(values: list) -> float:
    """计算历史最大回撤（负数）。"""
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def calculate_current_drawdown(values: list) -> float:
    """计算当前回撤（相对历史最高点）。"""
    if not values:
        return 0.0
    peak = max(values)
    return (values[-1] - peak) / peak if peak else 0.0


def calculate_annualized_return(
    start_value: float, end_value: float,
    start_date_str: str, end_date_str: str,
) -> float:
    try:
        sd = datetime.strptime(start_date_str, "%Y-%m-%d")
        ed = datetime.strptime(end_date_str, "%Y-%m-%d")
        days = (ed - sd).days
        if days <= 0 or start_value <= 0:
            return 0.0
        years = days / 365.25
        return (end_value / start_value) ** (1 / years) - 1
    except Exception:
        return 0.0


# ──────────────────────────────────────────────
# 再平衡
# ──────────────────────────────────────────────

def check_rebalance_needed(
    conn: sqlite3.Connection,
    latest_prices: dict,
    holdings: dict,
) -> dict | None:
    """
    检测是否满足再平衡触发条件。
    满足则返回包含建议的字典，否则返回 None。
    注意：已有未执行的再平衡记录时，由调用方决定是否跳过本函数。
    """
    # 计算当前总市值
    total_value = sum(
        holdings.get(code, 0) * latest_prices[code]["price"]
        for code in config.ASSETS
        if code in latest_prices
    )
    if total_value <= 0:
        return None

    # ── 偏离检测 ──
    deviations: dict = {}
    for code in config.ASSETS:
        if code not in latest_prices:
            continue
        value = holdings.get(code, 0) * latest_prices[code]["price"]
        current_ratio = value / total_value
        deviations[code] = round(current_ratio - config.ASSETS[code]["target"], 4)

    max_dev = max(abs(d) for d in deviations.values()) if deviations else 0
    deviation_triggered = max_dev >= config.REBALANCE_THRESHOLD

    # ── 季度检测 ──
    # 季度首日：1.1 / 4.1 / 7.1 / 10.1，从 REBALANCE_START_DATE 开始计算
    today = datetime.now()
    quarterly_triggered = False
    quarter_month = ((today.month - 1) // 3) * 3 + 1  # 当前季度起始月份
    quarter_start = datetime(today.year, quarter_month, 1)
    quarter_start_str = quarter_start.strftime("%Y-%m-%d")
    if quarter_start_str >= config.REBALANCE_START_DATE:
        existing = conn.execute(
            "SELECT id FROM rebalance_records "
            "WHERE trigger_type LIKE '%季度%' AND trigger_date >= ? LIMIT 1",
            (quarter_start_str,),
        ).fetchone()
        if not existing:
            quarterly_triggered = True

    if not deviation_triggered and not quarterly_triggered:
        return None

    trigger_parts = []
    if quarterly_triggered:
        trigger_parts.append("季度")
    if deviation_triggered:
        trigger_parts.append("偏离")

    # ── 调仓建议 ──
    suggestions = []
    for code, asset_cfg in config.ASSETS.items():
        if code not in latest_prices:
            continue
        price = latest_prices[code]["price"]
        current_value = holdings.get(code, 0) * price
        target_value = total_value * asset_cfg["target"]
        diff = target_value - current_value
        shares_diff = diff / price if price else 0

        suggestions.append({
            "code": code,
            "name": asset_cfg["name"],
            "current_value": round(current_value, 2),
            "target_value": round(target_value, 2),
            "diff": round(diff, 2),
            "shares_diff": round(shares_diff, 4),
            "price": round(price, 4),
            "action": "买入" if diff > 0 else ("卖出" if diff < 0 else "不动"),
        })

    # 季度触发时用季度首日作为 trigger_date，偏离触发时用今天
    trigger_date = quarter_start_str if quarterly_triggered else today.strftime("%Y-%m-%d")

    return {
        "trigger_type": " & ".join(trigger_parts),
        "trigger_date": trigger_date,
        "total_value": round(total_value, 2),
        "deviations": deviations,
        "suggestions": suggestions,
    }


# ──────────────────────────────────────────────
# 金字塔加仓信号
# ──────────────────────────────────────────────

def get_pyramid_signals(conn: sqlite3.Connection, latest_prices: dict) -> list:
    """
    返回各金字塔资产当前触发状态。
    每个资产包含各档位的触发状态和上次执行时间。
    """
    signals = []

    for code, rules in config.PYRAMID_RULES.items():
        if code not in latest_prices:
            continue

        current_price = latest_prices[code]["price"]

        # 历史最高价（从 START_DATE 至今）
        row = conn.execute(
            "SELECT MAX(close) AS mx FROM price_history WHERE code = ? AND date >= ?",
            (code, config.START_DATE),
        ).fetchone()
        if not row or not row["mx"]:
            continue

        hist_max = row["mx"]
        current_drop = (hist_max - current_price) / hist_max  # 正数 = 下跌

        tiers = []
        for i, rule in enumerate(rules):
            is_triggered = current_drop >= rule["drop"]

            # 上次执行记录
            last_exec = conn.execute(
                "SELECT execution_date FROM pyramid_executions "
                "WHERE code = ? AND tier_drop = ? ORDER BY execution_date DESC LIMIT 1",
                (code, rule["drop"]),
            ).fetchone()

            # 距下一档位还需下跌多少
            next_drop_needed = None
            if i + 1 < len(rules):
                gap = rules[i + 1]["drop"] - current_drop
                if gap > 0:
                    next_drop_needed = round(gap * 100, 2)

            tiers.append({
                "tier": i + 1,
                "drop_threshold": rule["drop"],
                "drop_threshold_pct": f"{rule['drop']*100:.0f}%",
                "amount": rule["amount"],
                "is_triggered": is_triggered,
                "last_executed": last_exec["execution_date"] if last_exec else None,
                "next_drop_needed_pct": next_drop_needed,
            })

        signals.append({
            "code": code,
            "name": config.ASSETS[code]["name"],
            "current_price": round(current_price, 4),
            "hist_max": round(hist_max, 4),
            "current_drop": round(current_drop, 4),
            "current_drop_pct": round(current_drop * 100, 2),
            "tiers": tiers,
        })

    return signals


# ──────────────────────────────────────────────
# 金字塔自动执行
# ──────────────────────────────────────────────

def auto_execute_pyramid_signals(conn: sqlite3.Connection, latest_prices: dict) -> list:
    """
    检查所有金字塔规则，对已触发且本下跌周期尚未执行的档位自动买入。
    每个档位在同一下跌周期内只执行一次：若上次执行后价格未曾回升至触发线以上，
    则视为同一下跌周期，跳过；否则允许再次执行。
    返回本次实际执行的记录列表。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    executed = []

    for code, rules in config.PYRAMID_RULES.items():
        if code not in latest_prices:
            continue

        current_price = latest_prices[code]["price"]

        row = conn.execute(
            "SELECT MAX(close) AS mx FROM price_history WHERE code = ? AND date >= ?",
            (code, config.START_DATE),
        ).fetchone()
        if not row or not row["mx"]:
            continue

        hist_max = row["mx"]
        current_drop = (hist_max - current_price) / hist_max

        for rule in rules:
            if current_drop < rule["drop"]:
                continue  # 未达触发线

            last_exec = conn.execute(
                "SELECT execution_date FROM pyramid_executions "
                "WHERE code = ? AND tier_drop = ? ORDER BY execution_date DESC LIMIT 1",
                (code, rule["drop"]),
            ).fetchone()

            if last_exec:
                # 若执行后价格曾回升到触发线以上，则允许本次再执行（新周期）
                threshold_price = hist_max * (1 - rule["drop"])
                recovery = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM price_history "
                    "WHERE code = ? AND date > ? AND close > ?",
                    (code, last_exec["execution_date"], threshold_price),
                ).fetchone()
                if not recovery or recovery["cnt"] == 0:
                    continue  # 同一下跌周期内已执行，跳过

            amount = rule["amount"]
            shares_to_buy = amount / current_price

            conn.execute(
                """INSERT INTO transactions
                   (date, code, shares_delta, price, amount, reason, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (today, code, shares_to_buy, current_price, amount,
                 "pyramid", f"金字塔自动加仓 {rule['drop']:.0%} 档"),
            )
            conn.execute(
                """INSERT INTO pyramid_executions
                   (execution_date, code, tier_drop, amount,
                    shares_bought, cash_shares_sold, price, cash_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, code, rule["drop"], amount,
                 shares_to_buy, 0, current_price, 0),
            )

            executed.append({
                "code":     code,
                "name":     config.ASSETS[code]["name"],
                "tier_drop": rule["drop"],
                "amount":   amount,
                "price":    current_price,
            })
            print(f"[INFO] 金字塔自动加仓 {code}({config.ASSETS[code]['name']}) "
                  f"{rule['drop']:.0%}档 ¥{amount:,} @ {current_price}")

    if executed:
        conn.commit()

    return executed
