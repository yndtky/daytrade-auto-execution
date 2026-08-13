"""指標を組み合わせて、ファンダメンタルをスクレイピングする対象(ショートリスト)を決める。

流動性フィルターを必須条件とし、OBVダイバージェンス・ゴールデンクロス接近・RSI過熱/
売られすぎのうち2つ以上が同時に成立する銘柄をショートリストとする
(いずれか1つだけだと対象が広すぎるため、複数シグナルが重なる銘柄に絞る)。
"""

import pandas as pd

MIN_SIGNALS = 2
ATTENTION_FLAGS = ["obv_divergence_flag", "golden_cross_flag", "rsi_flag"]


def select_shortlist(df: pd.DataFrame) -> list[str]:
    """daily_metrics形式のDataFrameからショートリスト対象のticker一覧を返す。"""
    liquid = df[df["liquidity_ok"].astype(bool)].copy()
    signal_count = liquid[ATTENTION_FLAGS].astype(bool).sum(axis=1)
    attention = liquid[signal_count >= MIN_SIGNALS]
    return attention["ticker"].tolist()


def momentum_pick_flag(liquidity_ok: bool, uptrend_turning_flag: bool, buy_pressure_score: float) -> bool:
    """「本日の注目銘柄」とは別軸: 買い優勢(スコア>0)かつ上昇トレンドへの転換(期・済み)。"""
    return bool(liquidity_ok and uptrend_turning_flag and buy_pressure_score > 0)


def select_momentum_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """買い優勢スコアが高く、かつ上昇トレンドへの転換(期・済み)の銘柄を、スコア降順で返す。"""
    liquid = df[df["liquidity_ok"].astype(bool)].copy()
    cond = liquid["uptrend_turning_flag"].astype(bool) & (liquid["buy_pressure_score"] > 0)
    return liquid[cond].sort_values("buy_pressure_score", ascending=False)


BETA_MIN = 0.5
BETA_MAX = 1.5
RANGE_52W_RECOVERY_THRESHOLD = 30  # 52週レンジの下位30%以内を「歴史的な安値圏に近い」とみなす


def recovery_candidate_flag(
    liquidity_ok: bool,
    sold_more_than_nikkei_flag: bool,
    range_position_52w: float,
    is_nikkei225: bool,
    beta: float | None,
) -> bool:
    """下落からの回復余地候補: 日経平均以上に売られ、52週レンジの下位圏にあり、
    日経225採用銘柄でβが1前後(指数と連動しやすく、地合い改善で戻りやすいと期待できる)。
    """
    if beta is None:
        return False
    return bool(
        liquidity_ok
        and sold_more_than_nikkei_flag
        and range_position_52w <= RANGE_52W_RECOVERY_THRESHOLD
        and is_nikkei225
        and BETA_MIN <= beta <= BETA_MAX
    )


def select_recovery_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """下落からの回復余地候補を、52週レンジ位置の低い順(安値圏に近い順)で返す。"""
    return df[df["is_recovery_candidate"].astype(bool)].sort_values("range_position_52w", ascending=True)
