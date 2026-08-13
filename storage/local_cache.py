"""内部作業用SQLite(プライム全銘柄の日次指標)の初期化・読み書き。"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline_cache.db"
# Streamlit Cloud等、バッチ処理(GitHub Actions)とダッシュボードが別環境で動く場合の読み取り用スナップショット。
# GitHub Actionsの実行後にpipeline_cache.dbをこのパスへコピーしてリポジトリにコミットする運用を想定。
# ローカルPCではpipeline_cache.dbが常に存在するため、通常はこちらは使われない。
CLOUD_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "cloud_metrics.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    industry TEXT,
    is_manufacturing INTEGER,
    close REAL,
    volume REAL,
    rsi14 REAL,
    rsi_flag INTEGER,
    obv_divergence_flag INTEGER,
    buy_pressure_score REAL,
    golden_cross_flag INTEGER,
    golden_cross_recent_flag INTEGER,
    uptrend_turning_flag INTEGER,
    liquidity_ok INTEGER,
    ytd_decline_pct REAL,
    sold_more_than_nikkei_flag INTEGER,
    range_position_52w REAL,
    is_nikkei225 INTEGER,
    beta REAL,
    correlation REAL,
    per REAL,
    pbr REAL,
    earnings_info TEXT,
    atr14 REAL,
    atr_pct REAL,
    bollinger_band_width_pct REAL,
    avg_volume_20d REAL,
    is_shortlisted INTEGER,
    is_momentum_pick INTEGER,
    is_recovery_candidate INTEGER,
    PRIMARY KEY (date, ticker)
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def _read_connect() -> sqlite3.Connection:
    """読み取り専用。pipeline_cache.dbが無ければクラウド用スナップショットにフォールバックする。"""
    path = DB_PATH if DB_PATH.exists() else CLOUD_SNAPSHOT_PATH
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    return conn


def upsert_daily_metrics(df: pd.DataFrame) -> None:
    """dfは daily_metrics のカラムを含む必要がある。同じ(date, ticker)は上書き。"""
    cols = [
        "date", "ticker", "name", "industry", "is_manufacturing", "close", "volume", "rsi14", "rsi_flag",
        "obv_divergence_flag", "buy_pressure_score",
        "golden_cross_flag", "golden_cross_recent_flag", "uptrend_turning_flag",
        "liquidity_ok", "ytd_decline_pct", "sold_more_than_nikkei_flag",
        "range_position_52w", "is_nikkei225", "beta", "correlation",
        "per", "pbr", "earnings_info",
        "atr14", "atr_pct", "bollinger_band_width_pct", "avg_volume_20d",
        "is_shortlisted", "is_momentum_pick", "is_recovery_candidate",
    ]
    df = df.reindex(columns=cols)

    with _connect() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO daily_metrics ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            df[cols].itertuples(index=False, name=None),
        )


def read_daily_metrics(date: str | None = None) -> pd.DataFrame:
    with _read_connect() as conn:
        if date is None:
            return pd.read_sql("SELECT * FROM daily_metrics", conn)
        return pd.read_sql(
            "SELECT * FROM daily_metrics WHERE date = ?", conn, params=(date,)
        )


def update_fundamentals(date: str, ticker: str, per: float | None, pbr: float | None, earnings_info: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE daily_metrics SET per = ?, pbr = ?, earnings_info = ?, is_shortlisted = 1 "
            "WHERE date = ? AND ticker = ?",
            (per, pbr, earnings_info, date, ticker),
        )


def mark_not_shortlisted(date: str, tickers: list[str]) -> None:
    """ショートリスト対象外の銘柄はis_shortlisted=0に揃える(NULLのまま残さない)。"""
    if not tickers:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE daily_metrics SET is_shortlisted = 0 WHERE date = ? AND ticker = ?",
            [(date, t) for t in tickers],
        )


def latest_date() -> str | None:
    with _read_connect() as conn:
        row = conn.execute("SELECT MAX(date) FROM daily_metrics").fetchone()
        return row[0] if row else None
