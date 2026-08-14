"""バックテスト用: indicators.pyの各指標を「その日までのデータだけ」を使う時系列(walk-forward)で計算する。

indicators.py本体の関数はほとんどが最新1日分の値(bool/float)を返す設計で、日次パイプライン
(run_daily.py)がその日1回だけ呼ぶ用途には適しているが、バックテストでは「過去の各日に、
その日時点でどう判定されていたか」を未来のデータを混ぜずに全期間ぶん計算する必要がある。
本モジュールはそのための同じロジックのベクトル化版(pandas Series全体を返す)を提供する。

screen.pyのMIN_SIGNALS/ATTENTION_FLAGSをそのまま再利用することで、本番の「本日の注目銘柄」
判定とバックテストのエントリー条件が食い違わないようにしている。
"""

import numpy as np
import pandas as pd

from . import indicators as ind
from . import screen


def _rolling_slope(series: pd.Series, window: int) -> pd.Series:
    """indicators._zscore_slopeのローリング版。各時点で直近windowぶんを標準化した回帰直線の傾き。

    素直に書くと Series.rolling(window).apply(polyfit) になるが、1銘柄5年分×数百銘柄という
    バックテスト規模だと1行ずつのPython呼び出し+polyfitの最小二乗解法が積み重なって遅い
    (100銘柄5年で数十秒)。xが常に0..window-1の固定値であることを使い、xとの内積だけを
    sliding_window_view + 1回の行列積で一括計算する閉形式に置き換えている(数値的には同じ結果)。
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    if n < window:
        return pd.Series(np.nan, index=series.index)

    windows = np.lib.stride_tricks.sliding_window_view(values, window)
    weights = np.arange(window, dtype=float)
    dot_xy = np.concatenate([np.full(window - 1, np.nan), windows @ weights])

    rolling_mean = series.rolling(window).mean().to_numpy()
    rolling_std = series.rolling(window).std(ddof=0).to_numpy()
    xbar = (window - 1) / 2
    denom = window * (window**2 - 1) / 12  # sum((x-xbar)^2)、xが固定なのでwindowだけで決まる定数

    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (dot_xy - xbar * window * rolling_mean) / rolling_std / denom
    slope = np.where(rolling_std == 0, 0.0, slope)
    return pd.Series(slope, index=series.index)


def rsi_flag_series(close: pd.Series, period: int = ind.RSI_PERIOD) -> pd.Series:
    rsi = ind.compute_rsi(close, period)
    return (rsi > ind.RSI_OVERBOUGHT) | (rsi < ind.RSI_OVERSOLD)


def buy_pressure_score_series(close: pd.Series, volume: pd.Series, window: int = ind.OBV_LOOKBACK) -> pd.Series:
    obv = ind.compute_obv(close, volume)
    price_slope = _rolling_slope(close, window)
    obv_slope = _rolling_slope(obv, window)
    return obv_slope - price_slope


def obv_divergence_flag_series(close: pd.Series, volume: pd.Series, window: int = ind.OBV_LOOKBACK) -> pd.Series:
    obv = ind.compute_obv(close, volume)
    price_slope = _rolling_slope(close, window)
    obv_slope = _rolling_slope(obv, window)
    return (price_slope < 0) & (obv_slope > price_slope)


def golden_cross_approaching_series(
    close: pd.Series,
    short: int = ind.GOLDEN_CROSS_SHORT_MA,
    long: int = ind.GOLDEN_CROSS_LONG_MA,
    lookback: int = ind.GOLDEN_CROSS_LOOKBACK,
) -> pd.Series:
    ma_short = close.rolling(short).mean()
    ma_long = close.rolling(long).mean()
    gap = ma_short - ma_long
    gap_before = gap.shift(lookback)
    ma_short_before = ma_short.shift(lookback)

    not_yet_crossed = gap < 0
    gap_narrowing = gap > gap_before
    short_ma_turning_up = ma_short > ma_short_before
    return not_yet_crossed & gap_narrowing & short_ma_turning_up


def golden_cross_recent_series(
    close: pd.Series,
    short: int = ind.GOLDEN_CROSS_SHORT_MA,
    long: int = ind.GOLDEN_CROSS_LONG_MA,
    lookback: int = ind.GOLDEN_CROSS_LOOKBACK,
) -> pd.Series:
    ma_short = close.rolling(short).mean()
    ma_long = close.rolling(long).mean()
    gap = ma_short - ma_long
    gap_before = gap.shift(lookback)
    return (gap > 0) & (gap_before < 0)


def beta_series(close: pd.Series, index_close: pd.Series, window: int = ind.BETA_WINDOW) -> pd.Series:
    """日経平均に対するβのローリング版(indicators.beta_and_correlationの単一値版と同じ定義)。

    ポートフォリオ全体のリスク管理(保有銘柄のβで重み付けした、日経急落時の想定インパクト)に使う。
    pandasのrolling().cov()/rolling().var()はどちらもC実装で高速なため、_rolling_slopeのときのような
    独自の閉形式最適化は不要(素直な実装のままで十分速い)。
    """
    stock_ret = close.pct_change()
    index_ret = index_close.reindex(close.index).pct_change()
    covariance = stock_ret.rolling(window).cov(index_ret)
    variance = index_ret.rolling(window).var()
    with np.errstate(divide="ignore", invalid="ignore"):
        beta = covariance / variance
    return beta.replace([np.inf, -np.inf], np.nan)


def liquidity_ok_series(
    close: pd.Series,
    volume: pd.Series,
    window: int = ind.LIQUIDITY_LOOKBACK,
    min_turnover: float = ind.LIQUIDITY_MIN_AVG_TURNOVER_JPY,
) -> pd.Series:
    turnover = (close * volume).rolling(window).mean()
    return turnover >= min_turnover


def compute_all_signals(
    df: pd.DataFrame, min_signals: int | None = None, index_close: pd.Series | None = None
) -> pd.DataFrame:
    """OHLCV DataFrame(日付昇順、列: Open/High/Low/Close/Volume)から、全期間ぶんの指標・
    エントリーシグナルをDataFrameで返す(各行はその日までのデータのみで計算、未来参照なし)。

    entry_signalは screen.select_shortlist と同じ条件(流動性OK かつ ATTENTION_FLAGSの
    うちmin_signals個以上が成立)。min_signals未指定時は本番の「本日の注目銘柄」判定と
    同じ screen.MIN_SIGNALS(=2)を使うが、バックテストで条件の厳しさを変えて検証したい
    場合はここで上書きできる(小口座では候補を絞って質を優先したい、等の検証用)。

    index_close(日経平均の終値)を渡すと、beta列(日経に対するβのローリング値)も計算する。
    渡さない場合はbeta列は1.0で埋める(=市場平均並みという中立的な仮定、ポートフォリオ全体の
    リスク管理機能を使わないバックテストでは計算コストをかけない)。
    """
    min_signals = screen.MIN_SIGNALS if min_signals is None else min_signals
    close, volume = df["Close"], df["Volume"]

    out = pd.DataFrame(index=df.index)
    out["open"] = df["Open"]
    out["high"] = df["High"]
    out["low"] = df["Low"]
    out["close"] = close
    out["volume"] = volume

    out["rsi_flag"] = rsi_flag_series(close)
    out["obv_divergence_flag"] = obv_divergence_flag_series(close, volume)
    out["golden_cross_flag"] = golden_cross_approaching_series(close)
    out["golden_cross_recent_flag"] = golden_cross_recent_series(close)
    out["uptrend_turning_flag"] = out["golden_cross_flag"] | out["golden_cross_recent_flag"]
    out["buy_pressure_score"] = buy_pressure_score_series(close, volume)
    out["liquidity_ok"] = liquidity_ok_series(close, volume)
    out["atr14"] = ind.compute_atr(df["High"], df["Low"], close)

    if index_close is not None:
        out["beta"] = beta_series(close, index_close)
    else:
        out["beta"] = 1.0

    for flag_col in ["rsi_flag", "obv_divergence_flag", "golden_cross_flag", "uptrend_turning_flag", "liquidity_ok"]:
        out[flag_col] = out[flag_col].fillna(False)

    signal_count = out[screen.ATTENTION_FLAGS].astype(bool).sum(axis=1)
    out["entry_signal"] = (out["liquidity_ok"] & (signal_count >= min_signals)).astype(float)
    out["atr14"] = out["atr14"].ffill().fillna(0.0)
    out["buy_pressure_score"] = out["buy_pressure_score"].fillna(0.0)
    out["beta"] = out["beta"].ffill().fillna(1.0)

    return out
