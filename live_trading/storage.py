"""live_tradingの実行履歴(口座評価額の推移・発注)を保存するSQLite。

paper_trading/storage.py・storage/local_cache.pyと同じ設計パターン(ローカルDB+クラウド用
スナップショットのフォールバック)。peak_value(口座評価額の過去最高値)を日をまたいで
永続化する必要があるのが、backtestのShortlistStrategy(1回のCerebro実行内で完結)との違い
——実運用では毎日プロセスが新しく起動するため、ここに記録しておかないとドローダウン判定の
基準(ピーク)を見失う。
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "live_trading.db"
CLOUD_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "live_trading_cloud.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_runs (
    date TEXT PRIMARY KEY,
    account_value REAL,
    peak_value REAL,
    halted INTEGER,
    halt_reason TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER,
    price REAL,
    order_id TEXT,
    status TEXT,
    note TEXT
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def _read_connect() -> sqlite3.Connection:
    path = DB_PATH if DB_PATH.exists() else CLOUD_SNAPSHOT_PATH
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def peak_value_so_far() -> float:
    """これまでに記録された口座評価額の最大値。記録がなければ0(=初回実行時)。"""
    with _read_connect() as conn:
        row = conn.execute("SELECT MAX(peak_value) FROM daily_runs").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


def log_daily_run(
    date: str, account_value: float, peak_value: float, halted: bool, halt_reason: str | None, note: str | None = None
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_runs (date, account_value, peak_value, halted, halt_reason, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, account_value, peak_value, int(halted), halt_reason, note),
        )


def log_order(date: str, ticker: str, side: str, quantity: int, price: float, order_id: str | None, status: str, note: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO live_orders (date, ticker, side, quantity, price, order_id, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date, ticker, side, quantity, price, order_id, status, note),
        )


def read_daily_runs(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql("SELECT * FROM daily_runs ORDER BY date DESC LIMIT ?", conn, params=(limit,))


def read_orders(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql("SELECT * FROM live_orders ORDER BY date DESC, id DESC LIMIT ?", conn, params=(limit,))
