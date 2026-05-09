"""
数据拉取模块：
- 唯一数据源：天天基金 NAV API（东方财富），拉取单位净值（DWJZ）
- 增量更新：从 DB 最后一条记录日期开始重拉，确保最新净值准确
- 首次运行则从 START_DATE 全量拉取
- 持仓初始化：若 transactions 表为空，基于 START_DATE 附近首笔净值按目标比例建仓
"""

import math
import sqlite3
import time
from datetime import datetime

import pandas as pd
import requests

import config

_EM_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_PAGE_SIZE = 49   # 东方财富 API 单页最大支持 49 条

_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EM_KLINE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.eastmoney.com/",
}

# 基准指数：(DB存储code, 东方财富secid)
_BENCHMARK_INDEXES = [
    ("IDX_000300", "1.000300"),   # 沪深300
]
# 基准基金（直接复用 _fetch_fund_nav）
_BENCHMARK_FUNDS = [
    "017641",   # 摩根标普500指数（SP500代理）
]


def _fetch_em_index_kline(secid: str, start_date: str, end_date: str) -> pd.DataFrame:
    """东方财富行情 API 拉取指数日 K，返回 date/close 升序 DataFrame。"""
    params = {
        "secid":   secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt":     101,
        "fqt":     1,
        "beg":     start_date.replace("-", ""),
        "end":     end_date.replace("-", ""),
        "_":       round(time.time() * 1000),
    }
    try:
        r = requests.get(_EM_KLINE_URL, params=params, headers=_EM_KLINE_HEADERS, timeout=15)
        r.raise_for_status()
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception as e:
        print(f"[WARN] 东方财富指数 kline API 拉取 {secid} 失败: {e}")
        return pd.DataFrame()
    rows = []
    for kline in klines:
        parts = kline.split(",")
        if len(parts) >= 3:
            rows.append({"date": parts[0], "close": float(parts[2])})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _upsert_prices(conn: sqlite3.Connection, code: str, df: pd.DataFrame) -> tuple[int, int]:
    """将 DataFrame 写入 price_history，返回 (new_count, update_count)。"""
    new_count = update_count = 0
    for _, r in df.iterrows():
        date_str = str(r["date"])[:10]
        close_price = float(r["close"])
        existing = conn.execute(
            "SELECT close FROM price_history WHERE date = ? AND code = ?",
            (date_str, code),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO price_history (date, code, close) VALUES (?, ?, ?)",
            (date_str, code, close_price),
        )
        if existing is None:
            new_count += 1
        elif existing["close"] != close_price:
            update_count += 1
    conn.commit()
    return new_count, update_count


def update_benchmark_prices(conn: sqlite3.Connection) -> dict:
    """
    增量拉取基准指数（沪深300）和基准基金（摩根标普500）净值并写入 price_history。
    返回 {code: 新增行数} 的字典。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    results = {}

    for db_code, secid in _BENCHMARK_INDEXES:
        row = conn.execute(
            "SELECT MAX(date) AS last_date FROM price_history WHERE code = ?", (db_code,)
        ).fetchone()
        fetch_from = row["last_date"] if row["last_date"] else config.START_DATE
        print(f"[INFO] 拉取基准 {db_code} ({secid})  {fetch_from} ~ {today}")
        df = _fetch_em_index_kline(secid, fetch_from, today)
        if df.empty:
            print(f"[WARN]   → 无数据")
            results[db_code] = 0
            continue
        new_count, update_count = _upsert_prices(conn, db_code, df)
        parts = [f"新增 {new_count} 条"]
        if update_count:
            parts.append(f"更新 {update_count} 条")
        print(f"[INFO]   → {', '.join(parts)}")
        results[db_code] = new_count

    for fund_code in _BENCHMARK_FUNDS:
        row = conn.execute(
            "SELECT MAX(date) AS last_date FROM price_history WHERE code = ?", (fund_code,)
        ).fetchone()
        fetch_from = row["last_date"] if row["last_date"] else config.START_DATE
        print(f"[INFO] 拉取基准基金 {fund_code}  {fetch_from} ~ {today}")
        price_df, _ = _fetch_fund_nav(fund_code, fetch_from, today)
        if price_df.empty:
            print(f"[WARN]   → 无数据")
            results[fund_code] = 0
            continue
        new_count, update_count = _upsert_prices(conn, fund_code, price_df)
        parts = [f"新增 {new_count} 条"]
        if update_count:
            parts.append(f"更新 {update_count} 条")
        print(f"[INFO]   → {', '.join(parts)}")
        results[fund_code] = new_count

    return results


def _fetch_fund_nav(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    调用天天基金 NAV API，拉取基金单位净值（DWJZ）及分红数据（FHFCZ）。
    返回 (price_df, dividend_df)：
      price_df   含 date、close 两列，升序
      dividend_df 含 date、dividend_per_share、close 三列（仅分红日），升序
    任一为空时返回空 DataFrame。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html",
    }

    # ── 第 1 页：获取总条数 ──────────────────────────────────────
    params = {
        "fundCode":  code,
        "pageIndex": 1,
        "pageSize":  _PAGE_SIZE,
        "startDate": start_date,
        "endDate":   end_date,
        "_":         round(time.time() * 1000),
    }
    try:
        r = requests.get(_EM_URL, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"[WARN] 天天基金 API 拉取 {code} 第1页失败: {e}")
        return pd.DataFrame(), pd.DataFrame()

    total_count = payload.get("TotalCount", 0)
    if total_count == 0:
        return pd.DataFrame(), pd.DataFrame()

    def _parse_items(items: list) -> list:
        result = []
        for item in items:
            result.append({
                "date":  item["FSRQ"],
                "close": item["DWJZ"],
                "fhfcz": item.get("FHFCZ", ""),
            })
        return result

    rows = _parse_items(payload.get("Data", {}).get("LSJZList", []))

    # ── 后续页 ───────────────────────────────────────────────────
    actual_page_size = len(rows) if rows else _PAGE_SIZE
    total_pages = math.ceil(total_count / actual_page_size)
    for page in range(2, total_pages + 1):
        params["pageIndex"] = page
        params["_"] = round(time.time() * 1000)
        try:
            r = requests.get(_EM_URL, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            rows.extend(_parse_items(r.json().get("Data", {}).get("LSJZList", [])))
        except Exception as e:
            print(f"[WARN] 天天基金 API {code} 第{page}页失败: {e}")
            break

    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(rows)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df.iloc[::-1].reset_index(drop=True)  # 升序

    # 分红记录：FHFCZ 非空且 > 0
    div_df = df[df["fhfcz"].astype(str).str.strip() != ""].copy()
    div_df["dividend_per_share"] = pd.to_numeric(div_df["fhfcz"], errors="coerce")
    div_df = div_df[div_df["dividend_per_share"] > 0][["date", "dividend_per_share", "close"]].reset_index(drop=True)

    price_df = df[["date", "close"]].copy()
    return price_df, div_df


def _process_dividends(conn: sqlite3.Connection, code: str, div_df: pd.DataFrame) -> int:
    """
    现金分红：除息日持仓份额不变，将分红金额记为现金收入（shares_delta=0）。
    跳过 START_DATE 之前及已处理的分红。返回本次处理的分红笔数。
    """
    if div_df.empty:
        return 0
    processed = 0
    for _, row in div_df.iterrows():
        date_str = str(row["date"])[:10]
        if date_str < config.START_DATE:
            continue
        # 已处理则跳过
        exists = conn.execute(
            "SELECT id FROM transactions WHERE code=? AND date=? AND reason='dividend'",
            (code, date_str),
        ).fetchone()
        if exists:
            continue
        # 除息日当日之前的累计持仓份额
        shares = conn.execute(
            "SELECT COALESCE(SUM(shares_delta), 0) FROM transactions WHERE code=? AND date<?",
            (code, date_str),
        ).fetchone()[0] or 0.0
        if shares <= 0:
            continue
        div = float(row["dividend_per_share"])
        income = shares * div
        if income <= 0:
            continue
        # shares_delta=0：不增加持仓，仅记录现金收入
        conn.execute(
            """INSERT INTO transactions (date, code, shares_delta, price, amount, reason, note)
               VALUES (?, ?, 0, ?, ?, 'dividend', ?)""",
            (date_str, code, float(row["close"]), income,
             f"现金分红 {div:.4f}元/份 × {shares:.4f}份"),
        )
        print(f"[INFO] {code} 现金分红 {date_str}: {shares:.4f}份 × {div} = {income:.2f}元")
        processed += 1
    if processed:
        conn.commit()
    return processed


def update_prices(conn: sqlite3.Connection) -> dict:
    """
    增量更新所有资产净值。
    从 DB 中最后一条记录日期重新拉，确保最新净值准确。
    返回 {code: 新增行数} 的字典。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    results = {}

    for code, asset in config.ASSETS.items():
        row = conn.execute(
            "SELECT MAX(date) AS last_date FROM price_history WHERE code = ?", (code,)
        ).fetchone()
        fetch_from = row["last_date"] if row["last_date"] else config.START_DATE

        print(f"[INFO] 拉取 {code} ({asset['name']})  {fetch_from} ~ {today}")
        price_df, _ = _fetch_fund_nav(code, fetch_from, today)

        if price_df.empty:
            print(f"[WARN]   → 无数据")
            results[code] = 0
            continue

        new_count, update_count = _upsert_prices(conn, code, price_df)
        results[code] = new_count
        parts = [f"新增 {new_count} 条"]
        if update_count:
            parts.append(f"更新 {update_count} 条")

        # 分红检测从 START_DATE 全量扫描：增量窗口可能错过历史分红日
        _, div_df = _fetch_fund_nav(code, config.START_DATE, today)
        div_count = _process_dividends(conn, code, div_df)
        if div_count:
            parts.append(f"现金分红 {div_count} 笔")
        print(f"[INFO]   → {', '.join(parts)}")

    return results


def backfill_dividends(conn: sqlite3.Connection) -> int:
    """
    初始化建仓后调用：对所有持仓资产从 START_DATE 扫描并补录现金分红。
    需在 ensure_holdings_initialized 之后执行，否则持仓份额为 0 会跳过所有分红。
    返回本次补录的分红总笔数。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    total = 0
    for code, asset in config.ASSETS.items():
        _, div_df = _fetch_fund_nav(code, config.START_DATE, today)
        count = _process_dividends(conn, code, div_df)
        if count:
            print(f"[INFO] {code} ({asset['name']}) 补录分红 {count} 笔")
        total += count
    return total


# ──────────────────────────────────────────────
# 持仓初始化
# ──────────────────────────────────────────────

def ensure_holdings_initialized(conn: sqlite3.Connection) -> bool:
    """
    若 transactions 表为空，则按目标比例、START_DATE 价格完成初始建仓。
    返回 True 表示本次完成了初始化，False 表示已有数据无需处理。
    """
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if count > 0:
        return False

    # 找各资产在 START_DATE 之后最早的可用净值
    init_prices: dict = {}
    for code in config.ASSETS:
        row = conn.execute(
            "SELECT date, close FROM price_history WHERE code = ? AND date >= ? ORDER BY date LIMIT 1",
            (code, config.START_DATE),
        ).fetchone()
        if row:
            init_prices[code] = {"date": row["date"], "price": row["close"]}

    if not init_prices:
        print("[WARN] 数据库中暂无净值数据，无法初始化持仓，请先拉取数据。")
        return False

    # 统一初始化日期 = 所有资产首日中最晚的那天
    init_date = max(v["date"] for v in init_prices.values())

    # 用统一日期重新取各资产净值
    for code in config.ASSETS:
        row = conn.execute(
            "SELECT close FROM price_history WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (code, init_date),
        ).fetchone()
        if row:
            init_prices[code] = {"date": init_date, "price": row["close"]}

    for code, asset_cfg in config.ASSETS.items():
        if code not in init_prices:
            print(f"[WARN] {code} 无净值，跳过初始化")
            continue
        price  = init_prices[code]["price"]
        amount = config.TOTAL_ASSETS * asset_cfg["target"]
        shares = amount / price
        conn.execute(
            """INSERT INTO transactions
               (date, code, shares_delta, price, amount, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (init_date, code, shares, price, amount, "init", "初始建仓"),
        )

    conn.commit()
    print(f"[INFO] 持仓已按 {init_date} 净值初始化，初始总资产 {config.TOTAL_ASSETS:,.0f} 元")
    return True
