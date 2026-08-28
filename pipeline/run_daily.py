"""日中の指標計算・Googleシート更新のエントリーポイント(タスクスケジューラから呼ぶ)。

同じ処理を1日に複数回(朝7:00・前場引け後・後場引け後)呼び出す想定。
呼び出すたびに、その時点で入手できる最新の株価を「本日の値」として指標を再計算し、
4つの軸シート(本日の注目銘柄/買い優勢/上昇転換+買い優勢/下落からの回復期待)を上書きする。
Perplexity質問プロンプトの生成・ウォッチリストのグラフ更新は、朝の実行時のみ行う
(値段に依存しない、または1日に何度も作り直す必要がないため)。
"""

import datetime as dt

import pandas as pd

from . import _net  # noqa: F401
from .fetch_prices import fetch_nikkei225_index, fetch_ohlcv
from .indicators import (
    GOLDEN_CROSS_LONG_MA,
    GOLDEN_CROSS_SHORT_MA,
    compute_latest_metrics,
    compute_market_relative_metrics,
    compute_volatility_metrics,
    ytd_high_decline_pct,
)
from .nikkei225 import get_constituents as get_nikkei225_constituents
from .screen import (
    momentum_pick_flag,
    recovery_candidate_flag,
    select_momentum_candidates,
    select_recovery_candidates,
    select_shortlist,
)
from .scrape_fundamentals import fetch_fundamentals_bulk
from .universe import get_prime_universe
from storage.local_cache import (
    mark_not_shortlisted,
    read_daily_metrics,
    update_fundamentals,
    upsert_daily_metrics,
)


def run(today: str | None = None, session_label: str = "朝", run_extras: bool = True) -> pd.DataFrame:
    """session_label: 通知やログに使うラベル(「朝」「前場」「後場」など)。
    run_extras: Trueなら、Perplexityプロンプト生成・ウォッチリストのグラフ更新も行う(朝のみ想定)。
    """
    today = today or dt.date.today().isoformat()
    print(f"=== {session_label}の実行を開始 (date={today}) ===")

    universe = get_prime_universe()
    name_by_ticker = dict(zip(universe["ticker"], universe["name"]))
    industry_by_ticker = dict(zip(universe["ticker"], universe["industry"]))
    is_manufacturing_by_ticker = dict(zip(universe["ticker"], universe["is_manufacturing"]))

    nikkei225 = get_nikkei225_constituents()
    nikkei225_tickers = set(nikkei225["ticker"])

    nikkei_index = fetch_nikkei225_index()
    nikkei_ytd_decline = ytd_high_decline_pct(nikkei_index["Close"])
    print(f"日経平均 年初来高値からの下落率: {nikkei_ytd_decline:.1f}%")

    prices = fetch_ohlcv(universe["ticker"].tolist())
    print(f"価格取得: {len(prices)}/{len(universe)} 銘柄")

    rows = []
    for ticker, df in prices.items():
        try:
            metrics = compute_latest_metrics(df)
            market_metrics = compute_market_relative_metrics(df["Close"], nikkei_index["Close"], nikkei_ytd_decline)
            volatility_metrics = compute_volatility_metrics(df)
        except Exception as e:  # noqa: BLE001
            print(f"指標計算失敗 {ticker}: {e}")
            continue

        is_nikkei225 = ticker in nikkei225_tickers
        rows.append(
            {
                "date": today,
                "ticker": ticker,
                "name": name_by_ticker.get(ticker),
                "industry": industry_by_ticker.get(ticker),
                "is_manufacturing": bool(is_manufacturing_by_ticker.get(ticker, False)),
                "close": metrics["close"],
                "volume": metrics["volume"],
                "dividend_per_share": (
                    float(df["Dividends"].iloc[-1])
                    if "Dividends" in df.columns and pd.notna(df["Dividends"].iloc[-1])
                    else 0.0
                ),
                "rsi14": metrics["rsi14"],
                "rsi_flag": metrics["rsi_flag"],
                "obv_divergence_flag": metrics["obv_divergence_flag"],
                "bearish_obv_divergence_flag": metrics["bearish_obv_divergence_flag"],
                "buy_pressure_score": metrics["buy_pressure_score"],
                "volume_poc_price": metrics["volume_poc_price"],
                "volume_poc_distance_pct": metrics["volume_poc_distance_pct"],
                "golden_cross_flag": metrics["golden_cross_flag"],
                "golden_cross_recent_flag": metrics["golden_cross_recent_flag"],
                "uptrend_turning_flag": metrics["uptrend_turning_flag"],
                "liquidity_ok": metrics["liquidity_ok"],
                "ytd_decline_pct": market_metrics["ytd_decline_pct"],
                "sold_more_than_nikkei_flag": market_metrics["sold_more_than_nikkei_flag"],
                "range_position_52w": market_metrics["range_position_52w"],
                "is_nikkei225": is_nikkei225,
                "beta": market_metrics["beta"],
                "correlation": market_metrics["correlation"],
                "per": None,
                "pbr": None,
                "earnings_info": None,
                "atr14": volatility_metrics["atr14"],
                "atr_pct": volatility_metrics["atr_pct"],
                "bollinger_band_width_pct": volatility_metrics["bollinger_band_width_pct"],
                "avg_volume_20d": volatility_metrics["avg_volume_20d"],
                "is_shortlisted": None,
                "is_momentum_pick": momentum_pick_flag(
                    metrics["liquidity_ok"], metrics["uptrend_turning_flag"], metrics["buy_pressure_score"]
                ),
                "is_recovery_candidate": recovery_candidate_flag(
                    metrics["liquidity_ok"],
                    market_metrics["sold_more_than_nikkei_flag"],
                    market_metrics["range_position_52w"],
                    is_nikkei225,
                    market_metrics["beta"],
                ),
            }
        )

    result = pd.DataFrame(rows)
    upsert_daily_metrics(result)
    print(f"保存完了: {len(result)}件 (date={today})")

    shortlist = select_shortlist(result)
    print(f"本日の注目銘柄: {len(shortlist)}銘柄")

    fundamentals = fetch_fundamentals_bulk(shortlist)
    for ticker, f in fundamentals.items():
        update_fundamentals(today, ticker, f.get("per"), f.get("pbr"), f.get("earnings_info"))

    not_shortlisted = [t for t in result["ticker"] if t not in shortlist]
    mark_not_shortlisted(today, not_shortlisted)

    print(f"ファンダメンタル取得完了: {len(fundamentals)}/{len(shortlist)}銘柄")

    final = read_daily_metrics(today)
    shortlist_df = final[final["is_shortlisted"] == 1].sort_values("rsi14", ascending=False)
    buy_pressure_df = final[final["buy_pressure_score"] > 0].sort_values("buy_pressure_score", ascending=False)
    momentum_df = select_momentum_candidates(final)
    recovery_df = select_recovery_candidates(final)
    print(f"買い優勢(方向問わず): {len(buy_pressure_df)}銘柄")
    print(f"モメンタム候補: {len(momentum_df)}銘柄")
    print(f"回復期待候補: {len(recovery_df)}銘柄")

    try:
        from storage.sheets import (
            write_buy_pressure_sheet,
            write_momentum_sheet,
            write_recovery_sheet,
            write_shortlist_sheet,
        )

        write_shortlist_sheet(today, shortlist_df)
        write_buy_pressure_sheet(today, buy_pressure_df)
        write_momentum_sheet(today, momentum_df)
        write_recovery_sheet(today, recovery_df)
        print("Googleシート(4軸)への書き込み完了")
    except Exception as e:  # noqa: BLE001
        print(f"Googleシートへの書き込みをスキップ(未設定または失敗): {e}")

    if run_extras:
        try:
            from storage.sheets import write_perplexity_prompts

            write_perplexity_prompts(today, shortlist_df)
            print("PerplexityPrompts書き込み完了")
        except Exception as e:  # noqa: BLE001
            print(f"PerplexityPromptsの書き込みをスキップ(未設定または失敗): {e}")

        try:
            from storage.sheets import read_watchlist, write_watchlist_charts

            watchlist_tickers = read_watchlist()
            chart_data = {}
            for ticker in watchlist_tickers:
                ohlcv = prices.get(ticker)
                if ohlcv is None:
                    fetched = fetch_ohlcv([ticker])
                    ohlcv = fetched.get(ticker)
                if ohlcv is None:
                    print(f"ウォッチリスト銘柄の価格取得失敗: {ticker}")
                    continue

                chart_df = ohlcv.tail(120).copy()
                chart_df["MA5"] = chart_df["Close"].rolling(GOLDEN_CROSS_SHORT_MA).mean()
                chart_df["MA25"] = chart_df["Close"].rolling(GOLDEN_CROSS_LONG_MA).mean()
                chart_df = chart_df.dropna(subset=["MA25"]).reset_index()
                chart_df["Date"] = chart_df["Date"].dt.strftime("%Y-%m-%d")
                chart_data[ticker] = (
                    name_by_ticker.get(ticker, ticker),
                    chart_df[["Date", "Close", "MA5", "MA25"]],
                )

            write_watchlist_charts(chart_data)
            print(f"ウォッチリストのグラフ更新完了: {len(chart_data)}/{len(watchlist_tickers)}銘柄")
        except Exception as e:  # noqa: BLE001
            print(f"ウォッチリストのグラフ更新をスキップ(未設定または失敗): {e}")

    try:
        from .notify_discord import send_momentum_notification
        from storage.sheets import read_industry_focus

        industry_focus = read_industry_focus()
        send_momentum_notification(f"{today} ({session_label})", momentum_df, industry_focus)
        print(f"Discord通知完了(業種フォーカス: {industry_focus})")
    except Exception as e:  # noqa: BLE001
        print(f"Discord通知をスキップ(未設定または失敗): {e}")

    print(f"=== {session_label}の実行が完了 ===")
    return final


if __name__ == "__main__":
    run(session_label="朝", run_extras=True)
