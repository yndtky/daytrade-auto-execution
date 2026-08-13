"""日経平均プロフィル(日本経済新聞)から日経225採用銘柄一覧を取得・キャッシュする。"""

from . import _net  # noqa: F401

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import requests

NIKKEI225_URL = "https://indexes.nikkei.co.jp/nkave/index/component?idx=nk225"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "nikkei225_cache.csv"
CACHE_MAX_AGE_DAYS = 30


def _download_constituents() -> pd.DataFrame:
    resp = requests.get(NIKKEI225_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    all_rows = pd.concat(tables, ignore_index=True)
    all_rows.columns = ["ticker", "name", "name_full"]
    all_rows["ticker"] = all_rows["ticker"].astype(str)
    return all_rows[["ticker", "name"]]


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    age = dt.datetime.now() - dt.datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
    return age.days < CACHE_MAX_AGE_DAYS


def get_constituents(force_refresh: bool = False) -> pd.DataFrame:
    """日経225採用銘柄一覧(ticker, name)を返す。キャッシュが30日以内なら再取得しない。"""
    if not force_refresh and _cache_is_fresh():
        return pd.read_csv(CACHE_PATH, dtype={"ticker": str})

    constituents = _download_constituents()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    constituents.to_csv(CACHE_PATH, index=False)
    return constituents


if __name__ == "__main__":
    df = get_constituents(force_refresh=True)
    print(f"日経225採用銘柄数: {len(df)}")
    print(df.head())
