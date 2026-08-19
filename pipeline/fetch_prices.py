"""yfinanceでプライム市場全銘柄のOHLCVを一括取得する。"""

from . import _net  # noqa: F401

import time

import pandas as pd
import yfinance as yf

CHUNK_SIZE = 150
CHUNK_PAUSE_SEC = 2
PERIOD = "1y"  # 年初来高値・日経平均とのβ/相関計算に1年分の日次データが必要

NIKKEI225_INDEX_SYMBOL = "^N225"


def _to_yf_ticker(code: str) -> str:
    return f"{code}.T"


def fetch_nikkei225_index() -> pd.DataFrame:
    """日経平均指数(^N225)のOHLCVを取得する(β・相関・年初来高値の基準として使う)。"""
    data = yf.download(
        NIKKEI225_INDEX_SYMBOL,
        period=PERIOD,
        interval="1d",
        progress=False,
        auto_adjust=True,
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data.dropna(how="all")


def fetch_ohlcv(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """銘柄コードのリストを受け取り、{コード: OHLCV DataFrame} を返す。

    取得に失敗した銘柄は結果に含めない(呼び出し側でログ確認可能)。
    OHLCVに加えて配当実績(Dividends列、1株あたりの円額。配当が無い日は0)も含む
    (2026-08-20、pick_performance.pyでの選出後リターン集計に配当落ち分を反映するため追加)。
    """
    result: dict[str, pd.DataFrame] = {}

    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i : i + CHUNK_SIZE]
        yf_symbols = [_to_yf_ticker(c) for c in chunk]

        data = yf.download(
            yf_symbols,
            period=PERIOD,
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

        if i + CHUNK_SIZE < len(tickers):
            time.sleep(CHUNK_PAUSE_SEC)

    return result


def fetch_intraday_latest(tickers: list[str]) -> dict[str, float]:
    """指定銘柄の直近の株価(5分足の最新値)を取得する。前場更新など軽量な値動きチェック用。

    全銘柄の日足1年分を取り直すfetch_ohlcvと違い、指定した少数銘柄の「今の値段」だけを
    素早く取るためのもの(前場引け後の昼休みに使う想定)。
    """
    result: dict[str, float] = {}

    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i : i + CHUNK_SIZE]
        yf_symbols = [_to_yf_ticker(c) for c in chunk]

        data = yf.download(
            yf_symbols,
            period="1d",
            interval="5m",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )

        for code, yf_symbol in zip(chunk, yf_symbols):
            try:
                close = data[yf_symbol]["Close"].dropna()
            except KeyError:
                continue
            if not close.empty:
                result[code] = float(close.iloc[-1])

        if i + CHUNK_SIZE < len(tickers):
            time.sleep(CHUNK_PAUSE_SEC)

    return result


if __name__ == "__main__":
    from .universe import get_prime_universe

    universe = get_prime_universe()
    sample = universe["ticker"].tolist()[:20]
    prices = fetch_ohlcv(sample)
    print(f"取得成功: {len(prices)}/{len(sample)}")
    first_code = next(iter(prices))
    print(prices[first_code].tail(3))
