"""live_tradingの実行履歴(口座評価額の推移・発注)を保存するSQLite。

paper_trading/storage.py・storage/local_cache.pyと同じ設計パターン(ローカルDB+クラウド用
スナップショットのフォールバック)。peak_value(口座評価額の過去最高値)を日をまたいで
永続化する必要があるのが、backtestのShortlistStrategy(1回のCerebro実行内で完結)との違い
——実運用では毎日プロセスが新しく起動するため、ここに記録しておかないとドローダウン判定の
基準(ピーク)を見失う。
"""

import datetime as dt
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
CREATE TABLE IF NOT EXISTS open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    opened_date TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_order_id TEXT,
    entry_qty INTEGER,
    entry_price REAL,
    filled_qty INTEGER,
    stop_price REAL,
    target_price REAL,
    stop_order_id TEXT,
    target_order_id TEXT,
    closed_date TEXT,
    close_reason TEXT
);
"""

# 2026-08-14追加: date(日付のみ)に加えて、実際の発注時刻(created_at)を記録する。
# バックテストと実運用の時間軸のズレ(シグナル確定時刻と発注時刻の差)を後から検証できるように
# するため(外部資料の指摘を受けて追加)。CREATE TABLEだけでは既存DBに新しい列は増えないため、
# ALTER TABLEで移行する(列が既にあればsqlite3.OperationalErrorを無視するだけでよい)。
_MIGRATIONS = (
    "ALTER TABLE live_orders ADD COLUMN created_at TEXT",
    "ALTER TABLE open_positions ADD COLUMN created_at TEXT",
)


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列が既に存在する場合はこれでよい


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _read_connect() -> sqlite3.Connection:
    path = DB_PATH if DB_PATH.exists() else CLOUD_SNAPSHOT_PATH
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
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
            "INSERT INTO live_orders (date, ticker, side, quantity, price, order_id, status, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, ticker, side, quantity, price, order_id, status, note, dt.datetime.now().isoformat(timespec="seconds")),
        )


def open_position(ticker: str, opened_date: str, entry_order_id: str, entry_qty: int, entry_price: float, stop_price: float, target_price: float) -> int:
    """新規エントリー注文を送った直後に呼ぶ。約定確認前なのでstatus='entry_pending'。"""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO open_positions (ticker, opened_date, status, entry_order_id, entry_qty, entry_price, "
            "stop_price, target_price, created_at) VALUES (?, ?, 'entry_pending', ?, ?, ?, ?, ?, ?)",
            (ticker, opened_date, entry_order_id, entry_qty, entry_price, stop_price, target_price,
             dt.datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def mark_position_holding(position_id: int, filled_qty: int, stop_order_id: str, target_order_id: str) -> None:
    """エントリー注文の約定を確認し、損切り(逆指値)・利確(指値)の両方を出した後に呼ぶ。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE open_positions SET status = 'holding', filled_qty = ?, stop_order_id = ?, target_order_id = ? "
            "WHERE id = ?",
            (filled_qty, stop_order_id, target_order_id, position_id),
        )


def close_position(position_id: int, closed_date: str, close_reason: str) -> None:
    """損切り/利確のどちらかが約定し、もう一方をキャンセルした後に呼ぶ。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE open_positions SET status = 'closed', closed_date = ?, close_reason = ? WHERE id = ?",
            (closed_date, close_reason, position_id),
        )


def read_open_positions() -> pd.DataFrame:
    """まだ完了していないポジション(entry_pending・holding)一覧。"""
    with _read_connect() as conn:
        return pd.read_sql(
            "SELECT * FROM open_positions WHERE status IN ('entry_pending', 'holding') ORDER BY opened_date", conn
        )


def read_all_positions(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql("SELECT * FROM open_positions ORDER BY opened_date DESC LIMIT ?", conn, params=(limit,))


def read_daily_runs(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql("SELECT * FROM daily_runs ORDER BY date DESC LIMIT ?", conn, params=(limit,))


def read_orders(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql("SELECT * FROM live_orders ORDER BY date DESC, id DESC LIMIT ?", conn, params=(limit,))
