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

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "universe_cache.csv"
FULL_CACHE_PATH = DATA_DIR / "full_universe_cache.csv"
STANDARD_CACHE_PATH = DATA_DIR / "standard_universe_cache.csv"
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


def _extract_by_market(df: pd.DataFrame, is_target_market: pd.Series) -> pd.DataFrame:
    result = df.loc[
        is_target_market, ["コード", "銘柄名", "市場・商品区分", "33業種区分", "33業種コード"]
    ].copy()
    result.columns = ["ticker", "name", "market_segment", "industry", "industry_code"]
    result["ticker"] = result["ticker"].astype(str)
    result["industry_code"] = result["industry_code"].astype(str)
    result["is_manufacturing"] = result["industry_code"].isin(MANUFACTURING_INDUSTRY_CODES)

    # 通常の証券コードは4文字(数字4桁、または2024年以降の英数字混在4桁)。
    # 優先株式・社債型種類株式などは「25935」のように5桁の別コード体系で、普通株とは
    # 値動きの性質が全く異なる上、単純に".T"を付けてyfinanceに問い合わせても正しい銘柄に
    # 解決するとは限らない(実際、バックテストで銘柄名不明の異常なリターンを出す原因になった)。
    # 対象を普通株に絞るため、4文字以外のコードは除外する。
    result = result[result["ticker"].str.len() == 4]

    return result.reset_index(drop=True)


def _extract_by_keywords(df: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    """市場・商品区分がkeywordsのいずれかを含む行を抽出する(内国株式の普通株のみ)。"""
    segment = df["市場・商品区分"].astype(str)
    is_target = segment.str.contains("|".join(keywords))
    return _extract_by_market(df, is_target)


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
    return age.days < CACHE_MAX_AGE_DAYS


def _get_universe_cached(cache_path: Path, keywords: list[str], force_refresh: bool) -> pd.DataFrame:
    if not force_refresh and _cache_is_fresh(cache_path):
        return pd.read_csv(cache_path, dtype={"ticker": str})

    df = _download_jpx_list()
    universe = _extract_by_keywords(df, keywords)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(cache_path, index=False)
    return universe


def get_prime_universe(force_refresh: bool = False) -> pd.DataFrame:
    """プライム市場銘柄一覧を返す。キャッシュが30日以内なら再取得しない。"""
    return _get_universe_cached(CACHE_PATH, ["プライム"], force_refresh)


def get_standard_universe(force_refresh: bool = False) -> pd.DataFrame:
    """スタンダード市場銘柄一覧を返す(バックテスト検証用)。"""
    return _get_universe_cached(STANDARD_CACHE_PATH, ["スタンダード"], force_refresh)


def get_full_tse_universe(force_refresh: bool = False) -> pd.DataFrame:
    """プライム・スタンダード・グロースを合わせた銘柄一覧を返す(バックテスト検証用)。

    小口座(数万円〜十数万円)ではプライム市場の値がさ株ばかりでは選択肢が
    限られるため、日次パイプライン本番用の get_prime_universe() とは別に、
    バックテストで銘柄範囲を広げて検証するために用意した。
    """
    return _get_universe_cached(FULL_CACHE_PATH, ["プライム", "スタンダード", "グロース"], force_refresh)


def get_industry_map() -> dict[str, str]:
    """銘柄コード→33業種名のマッピングを返す(プライム+スタンダード+グロース全体)。

    バックテストの業種分散ロジック(同じ業種を同時に何ポジションまで持つか)向け。
    """
    universe = get_full_tse_universe()
    return dict(zip(universe["ticker"], universe["industry"]))


if __name__ == "__main__":
    universe = get_prime_universe(force_refresh=True)
    print(f"プライム市場銘柄数: {len(universe)}")
    print(universe.head())

    standard_universe = get_standard_universe(force_refresh=True)
    print(f"スタンダード市場銘柄数: {len(standard_universe)}")

    full_universe = get_full_tse_universe(force_refresh=True)
    print(f"プライム+スタンダード+グロース銘柄数: {len(full_universe)}")
    print(full_universe["market_segment"].value_counts())
