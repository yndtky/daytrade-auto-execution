"""JPX上場銘柄一覧からプライム市場銘柄を取得・キャッシュする。"""

from . import _net  # noqa: F401  (TLS検証をOS証明書ストア経由にするための副作用import)

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import requests

JPX_LIST_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "universe_cache.csv"
CACHE_MAX_AGE_DAYS = 30

# JPXの33業種コードのうち、食料品(3050)からその他製品(3800)までが製造業の区分
# (JPXの33業種コードは分類のまとまりごとに連番が振られており、この範囲がまとまって製造業にあたる)
MANUFACTURING_INDUSTRY_CODES = {
    "3050", "3100", "3150", "3200", "3250", "3300", "3350", "3400",
    "3450", "3500", "3550", "3600", "3650", "3700", "3750", "3800",
}


def _download_jpx_list() -> pd.DataFrame:
    resp = requests.get(JPX_LIST_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    return pd.read_excel(io.BytesIO(resp.content))


def _extract_prime(df: pd.DataFrame) -> pd.DataFrame:
    is_prime = df["市場・商品区分"].astype(str).str.contains("プライム")
    prime = df.loc[
        is_prime, ["コード", "銘柄名", "市場・商品区分", "33業種区分", "33業種コード"]
    ].copy()
    prime.columns = ["ticker", "name", "market_segment", "industry", "industry_code"]
    prime["ticker"] = prime["ticker"].astype(str)
    prime["industry_code"] = prime["industry_code"].astype(str)
    prime["is_manufacturing"] = prime["industry_code"].isin(MANUFACTURING_INDUSTRY_CODES)

    # 通常の証券コードは4文字(数字4桁、または2024年以降の英数字混在4桁)。
    # 優先株式・社債型種類株式などは「25935」のように5桁の別コード体系で、普通株とは
    # 値動きの性質が全く異なる上、単純に".T"を付けてyfinanceに問い合わせても正しい銘柄に
    # 解決するとは限らない(実際、バックテストで銘柄名不明の異常なリターンを出す原因になった)。
    # 対象を普通株のプライム市場銘柄に絞るため、4文字以外のコードは除外する。
    prime = prime[prime["ticker"].str.len() == 4]

    return prime.reset_index(drop=True)


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    age = dt.datetime.now() - dt.datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
    return age.days < CACHE_MAX_AGE_DAYS


def get_prime_universe(force_refresh: bool = False) -> pd.DataFrame:
    """プライム市場銘柄一覧を返す。キャッシュが30日以内なら再取得しない。"""
    if not force_refresh and _cache_is_fresh():
        return pd.read_csv(CACHE_PATH, dtype={"ticker": str})

    df = _download_jpx_list()
    prime = _extract_prime(df)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prime.to_csv(CACHE_PATH, index=False)
    return prime


if __name__ == "__main__":
    universe = get_prime_universe(force_refresh=True)
    print(f"プライム市場銘柄数: {len(universe)}")
    print(universe.head())
