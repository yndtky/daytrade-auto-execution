"""バックテスト用のOHLCV取得。

pipeline.fetch_prices は日次パイプライン用に直近1年分だけを取る設計になっている
(β/相関・年初来高値の計算に必要な分のみ)。バックテストは複数年の履歴が要るため、
別途 yfinance を直接呼ぶ薄いラッパーをここに置く(pipeline側の本番挙動には触れない)。
"""

from pipeline import _net  # noqa: F401

import time

import backtrader as bt
import pandas as pd
import yfinance as yf

CHUNK_SIZE = 150  # pipeline.fetch_prices と同じ(yfinanceの一括取得が安定する程度の件数)
CHUNK_PAUSE_SEC = 2


def _to_yf_ticker(code: str) -> str:
    return code if code.endswith(".T") or "." in code else f"{code}.T"


def fetch_history(ticker: str, years: int) -> pd.DataFrame:
    """1銘柄ぶんの複数年OHLCVを取得する。列は Open/High/Low/Close/Volume。"""
    data = yf.download(
        _to_yf_ticker(ticker),
        period=f"{years}y",
        interval="1d",
        progress=False,
        auto_adjust=True,
        actions=True,
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data.dropna(how="all")


NIKKEI225_INDEX_SYMBOL = "^N225"


def fetch_nikkei_history(years: int) -> pd.DataFrame:
    """日経平均指数の複数年OHLCVを取得する(ポートフォリオ全体のβ計算・急落フィルター用)。"""
    data = yf.download(
        NIKKEI225_INDEX_SYMBOL, period=f"{years}y", interval="1d", progress=False, auto_adjust=True
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data.dropna(how="all")


def fetch_history_bulk(tickers: list[str], years: int) -> dict[str, pd.DataFrame]:
    """複数銘柄ぶんの複数年OHLCVをチャンク分割して取得する(pipeline.fetch_prices.fetch_ohlcvの
    複数年版)。プライム市場全体のような大量銘柄をバックテストにかける際に使う。
    """
    result: dict[str, pd.DataFrame] = {}

    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i : i + CHUNK_SIZE]
        yf_symbols = [_to_yf_ticker(c) for c in chunk]

        data = yf.download(
            yf_symbols,
            period=f"{years}y",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
            actions=True,
        )

        for code, yf_symbol in zip(chunk, yf_symbols):
            try:
                df = data[yf_symbol].dropna(how="all")
            except KeyError:
                continue
            if df.empty or df["Close"].dropna().empty:
                continue
            result[code] = df

        print(f"取得進捗: {min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)}")
        if i + CHUNK_SIZE < len(tickers):
            time.sleep(CHUNK_PAUSE_SEC)

    return result


class SignalPandasData(bt.feeds.PandasData):
    """entry_signal/atr14など、事前計算済みの列をbacktraderのラインとして追加で読ませるフィード。"""

    lines = ("entry_signal", "atr14", "buy_pressure_score", "beta")
    params = (
        ("entry_signal", -1),
        ("atr14", -1),
        ("buy_pressure_score", -1),
        ("beta", -1),
    )
