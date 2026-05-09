import json
import sqlite3
import config

# 默认设置
_DEFAULT_SETTINGS = {
    "start_date":   "2026-01-01",
    "total_assets": "1000000",
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> sqlite3.Connection:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            date    TEXT NOT NULL,
            code    TEXT NOT NULL,
            close   REAL NOT NULL,
            UNIQUE(date, code)
        );

        -- 所有买卖操作（初始建仓 / 再平衡 / 金字塔加仓）
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            code         TEXT NOT NULL,
            shares_delta REAL NOT NULL,   -- 正数买入，负数卖出
            price        REAL NOT NULL,
            amount       REAL NOT NULL,   -- 正数买入金额，负数卖出金额
            reason       TEXT NOT NULL,   -- init / rebalance / pyramid
            note         TEXT
        );

        -- 每次触发的再平衡记录
        CREATE TABLE IF NOT EXISTS rebalance_records (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_date TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            is_executed  INTEGER DEFAULT 0,
            executed_at  TEXT
        );

        -- 金字塔加仓执行记录（用于展示历史，判断上次执行时间）
        CREATE TABLE IF NOT EXISTS pyramid_executions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_date   TEXT NOT NULL,
            code             TEXT NOT NULL,
            tier_drop        REAL NOT NULL,
            amount           REAL NOT NULL,
            shares_bought    REAL NOT NULL,
            cash_shares_sold REAL NOT NULL,
            price            REAL NOT NULL,
            cash_price       REAL NOT NULL
        );

        -- 用户设置（持久化，跨重启保留）
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ph_code_date  ON price_history(code, date);
        CREATE INDEX IF NOT EXISTS idx_tx_date       ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_tx_code       ON transactions(code);
    """)

    # 写入缺失的默认值（不覆盖已有记录）
    for key, value in _DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else _DEFAULT_SETTINGS.get(key)


def save_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def load_settings_into_config(conn: sqlite3.Connection) -> None:
    """从数据库读取设置并更新 config 模块的运行时变量。"""
    start_date = get_setting(conn, "start_date")
    total_assets = get_setting(conn, "total_assets")
    assets_json = get_setting(conn, "assets")
    pyramid_json = get_setting(conn, "pyramid_rules")
    if start_date:
        config.START_DATE = start_date
    if total_assets:
        config.TOTAL_ASSETS = int(total_assets)
    if assets_json:
        try:
            assets_list = json.loads(assets_json)
            config.ASSETS = {
                a["code"]: {"name": a["name"], "target": a["target"]}
                for a in assets_list
            }
        except Exception:
            pass
    if pyramid_json:
        try:
            config.PYRAMID_RULES = json.loads(pyramid_json)
        except Exception:
            pass
