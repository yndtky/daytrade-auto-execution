"""ペーパートレードの実行履歴・注文履歴を保存するSQLite。storage/local_cache.pyと同じ設計パターン。"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_trading.db"
# GitHub Actions実行後にDB_PATHをこのファイル名でコミットし、ダッシュボード(Streamlit Cloud)から
# 読めるようにする(storage/local_cache.pyのCLOUD_SNAPSHOT_PATHと同じ考え方)。
CLOUD_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_trading_cloud.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL,
    ma_short REAL,
    ma_long REAL,
    position_state TEXT,
    note TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    order_id TEXT,
    status TEXT,
    raw_response TEXT
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    account_value REAL,
    peak_value REAL,
    halted INTEGER,
    halt_reason TEXT
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


def log_cycle(
    symbol: str,
    action: str,
    price: float | None = None,
    ma_short: float | None = None,
    ma_long: float | None = None,
    position_state: str | None = None,
    note: str | None = None,
    error: str | None = None,
) -> None:
    import datetime as dt

    with _connect() as conn:
        conn.execute(
            "INSERT INTO cycles (timestamp, symbol, action, price, ma_short, ma_long, position_state, note, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dt.datetime.now(dt.timezone.utc).isoformat(),
                symbol,
                action,
                price,
                ma_short,
                ma_long,
                position_state,
                note,
                error,
            ),
        )


def log_order(symbol: str, side: str, quantity: float, order_id: str | None, status: str, raw_response: str) -> None:
    import datetime as dt

    with _connect() as conn:
        conn.execute(
            "INSERT INTO orders (timestamp, symbol, side, quantity, order_id, status, raw_response) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), symbol, side, quantity, order_id, status, raw_response),
        )


def read_cycles(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql(
            "SELECT * FROM cycles ORDER BY timestamp DESC LIMIT ?", conn, params=(limit,)
        )


def read_orders(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql(
            "SELECT * FROM orders ORDER BY timestamp DESC LIMIT ?", conn, params=(limit,)
        )


def peak_value_so_far() -> float:
    """これまでに記録された口座評価額の最大値。記録がなければ0(=初回実行時)。
    live_trading/storage.pyの同名関数と同じ考え方(サーキットブレーカーのドローダウン判定用)。
    """
    with _read_connect() as conn:
        row = conn.execute("SELECT MAX(peak_value) FROM account_snapshots").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


def log_account_snapshot(account_value: float, peak_value: float, halted: bool, halt_reason: str | None) -> None:
    import datetime as dt

    with _connect() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (timestamp, account_value, peak_value, halted, halt_reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), account_value, peak_value, int(halted), halt_reason),
        )


def read_account_snapshots(limit: int = 200) -> pd.DataFrame:
    with _read_connect() as conn:
        return pd.read_sql(
            "SELECT * FROM account_snapshots ORDER BY timestamp DESC LIMIT ?", conn, params=(limit,)
        )


def own_net_position(symbol: str) -> float:
    """自分(ボット)がこれまで発注した履歴から、買い-売りの差引き数量を計算する。

    取引所の生の残高(口座に元から入っていた分を含む)ではなく、こちらを「今ポジションを
    持っているか」の正とする(2026-08-14、Binance Testnetの初期付与残高を誤ってポジションと
    見なすバグを実機で発見して以来の方針)。FILLED/PARTIALLY_FILLED以外(失敗・却下等)は含めない。
    """
    with _read_connect() as conn:
        rows = conn.execute(
            "SELECT side, quantity FROM orders WHERE symbol = ? AND status IN ('FILLED', 'PARTIALLY_FILLED')",
            (symbol,),
        ).fetchall()
    net = 0.0
    for side, quantity in rows:
        net += quantity if side == "BUY" else -quantity
    return net
