"""過去にピックアップされた銘柄(本日の注目銘柄・上昇転換+買い優勢の候補・下落からの回復期待候補)
が、選出後に実際どうなったかを集計する。

目的は「モデルの精度向上」の第一段階として、まず正確に測定できる仕組みを作ること。
選出条件やスコアの重み付けを結果から自動調整するフィードバックループはあえて作らない
(2026-08-15、多重検定・過学習を避ける一連の議論を踏まえた判断。集計結果を見てどう
改善するかの判断は、常に人間が行う)。

storage/local_cache.pyのdaily_metricsは、ショートリストされなかった銘柄も含めて毎日
全銘柄の終値を記録しているため、同じ銘柄の後日の終値をそのまま引くだけで選出後の
リターンを計算できる(別途株価を再取得する必要はない)。
"""

import pandas as pd

from storage.local_cache import read_daily_metrics

# 3つの軸(daily_metricsのフラグ列名との対応)
AXES = {
    "本日の注目銘柄": "is_shortlisted",
    "上昇転換+買い優勢の候補": "is_momentum_pick",
    "下落からの回復期待候補": "is_recovery_candidate",
}
DEFAULT_HORIZONS = (1, 3, 5, 10)  # 選出から何営業日後を見るか


def compute_pick_returns(horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> pd.DataFrame:
    """各ピックアップイベント(日付×銘柄×軸)について、horizons営業日後のリターンを計算する。

    まだその日数分の後日データが蓄積されていない直近の選出は、対象の列がNaNのままになる
    (日々の自動実行でdaily_metricsが積み上がるにつれ、自然に埋まっていく)。
    """
    all_data = read_daily_metrics()
    if all_data.empty:
        return pd.DataFrame()

    all_data = all_data.sort_values("date")
    trading_dates = sorted(all_data["date"].unique())
    date_index = {d: i for i, d in enumerate(trading_dates)}

    # 銘柄別に「日付→終値」の対応表を作っておく(繰り返しlookupを避けて高速化)
    price_by_ticker: dict[str, dict[str, float]] = {
        ticker: dict(zip(g["date"], g["close"])) for ticker, g in all_data.groupby("ticker")
    }

    rows = []
    for axis_name, flag_col in AXES.items():
        picks = all_data[all_data[flag_col] == 1]
        for _, row in picks.iterrows():
            ticker, date, entry_price = row["ticker"], row["date"], row["close"]
            if pd.isna(entry_price) or entry_price <= 0:
                continue

            record = {
                "date": date, "ticker": ticker, "name": row.get("name"),
                "axis": axis_name, "entry_price": entry_price,
            }
            date_pos = date_index[date]
            for h in horizons:
                future_pos = date_pos + h
                future_price = None
                if future_pos < len(trading_dates):
                    future_price = price_by_ticker.get(ticker, {}).get(trading_dates[future_pos])
                record[f"return_{h}d_pct"] = (
                    round((future_price / entry_price - 1) * 100, 2)
                    if future_price and future_price > 0 else None
                )
            rows.append(record)

    return pd.DataFrame(rows)


def summarize_pick_performance(returns_df: pd.DataFrame, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> pd.DataFrame:
    """軸×期間ごとに、件数・的中率(選出後にプラスだった割合)・平均/中央値リターンを集計する。"""
    if returns_df.empty:
        return pd.DataFrame()

    rows = []
    for axis_name in AXES:
        axis_df = returns_df[returns_df["axis"] == axis_name]
        for h in horizons:
            valid = axis_df[f"return_{h}d_pct"].dropna()
            if valid.empty:
                continue
            rows.append({
                "axis": axis_name,
                "horizon_days": h,
                "n": len(valid),
                "hit_rate_pct": round((valid > 0).mean() * 100, 1),
                "mean_return_pct": round(valid.mean(), 2),
                "median_return_pct": round(valid.median(), 2),
            })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    returns = compute_pick_returns()
    print(f"ピックアップイベント数: {len(returns)}")
    summary = summarize_pick_performance(returns)
    if summary.empty:
        print("まだ集計できるだけのデータが蓄積されていません(日々の自動実行を待ってください)。")
    else:
        print(summary.to_string(index=False))
