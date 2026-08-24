"""33業種(JPX業種分類)ごとの当日モメンタム・出来高急増を、既存データから計算する。

外部サイトのスクレイピング(日経電子版・Yahoo!ファイナンス・株探等)やkabuステーションAPIの
TOPIX業種別指数を新たに使わず、pipeline.fetch_pricesが既に全銘柄ぶん毎日取得しているOHLCVと
pipeline.universe.get_industry_map()の業種分類から計算する(2026-08-25、ユーザー提案のPDFを
検討した結果、外部データ源はサイト仕様変更・アクセス制限のリスクがあり、この用途には既存データの
集計で十分と判断)。

モメンタム: 業種内の各銘柄の当日リターン(終値/前日終値-1)の単純平均。
出来高急増: 業種内の売買代金(終値×出来高)合計が、直近N営業日平均の何倍か。
"""

import pandas as pd

VOLUME_SURGE_LOOKBACK_DAYS = 5
VOLUME_SURGE_THRESHOLD = 1.5  # この倍率以上を「資金流入」とみなす目安


def compute_industry_momentum(daily_metrics_today: pd.DataFrame, daily_metrics_prev: pd.DataFrame) -> pd.DataFrame:
    """当日と前営業日のdaily_metrics(read_daily_metricsの返り値)から、業種別モメンタムを計算する。

    戻り値の列: industry, momentum_pct(業種内平均リターン%、降順ランキング可能), n_tickers。
    """
    prev_close = dict(zip(daily_metrics_prev["ticker"], daily_metrics_prev["close"]))
    today = daily_metrics_today.copy()
    today["prev_close"] = today["ticker"].map(prev_close)
    today = today.dropna(subset=["close", "prev_close", "industry"])
    today = today[today["prev_close"] > 0]
    today["return_pct"] = (today["close"] / today["prev_close"] - 1) * 100

    grouped = today.groupby("industry")["return_pct"].agg(["mean", "count"]).reset_index()
    grouped.columns = ["industry", "momentum_pct", "n_tickers"]
    return grouped.sort_values("momentum_pct", ascending=False).reset_index(drop=True)


def compute_industry_volume_surge(
    daily_metrics_today: pd.DataFrame, recent_daily_metrics: pd.DataFrame, lookback_days: int = VOLUME_SURGE_LOOKBACK_DAYS
) -> pd.DataFrame:
    """recent_daily_metrics(直近lookback_days+1営業日ぶんのdaily_metrics、当日含む)から
    業種別の売買代金急増倍率を計算する。

    戻り値の列: industry, turnover_today_jpy, turnover_avg_jpy, surge_ratio(降順ランキング可能)。
    """
    df = recent_daily_metrics.copy()
    df = df.dropna(subset=["close", "volume", "industry"])
    df["turnover"] = df["close"] * df["volume"]

    dates = sorted(df["date"].unique())
    if not dates:
        return pd.DataFrame(columns=["industry", "turnover_today_jpy", "turnover_avg_jpy", "surge_ratio"])
    today_date = dates[-1]
    past_dates = dates[:-1][-lookback_days:]

    today_turnover = df[df["date"] == today_date].groupby("industry")["turnover"].sum()
    past_turnover_avg = (
        df[df["date"].isin(past_dates)].groupby(["industry", "date"])["turnover"].sum()
        .groupby("industry").mean()
    )

    result = pd.DataFrame({"turnover_today_jpy": today_turnover, "turnover_avg_jpy": past_turnover_avg}).dropna()
    result = result[result["turnover_avg_jpy"] > 0]
    result["surge_ratio"] = result["turnover_today_jpy"] / result["turnover_avg_jpy"]
    return result.reset_index().sort_values("surge_ratio", ascending=False).reset_index(drop=True)
