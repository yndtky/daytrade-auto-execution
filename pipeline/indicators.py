"""テクニカル指標の計算: RSI(14)、OBVダイバージェンス/買い優勢スコア、ゴールデンクロス接近/転換、流動性フィルター。"""

import numpy as np
import pandas as pd

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

GOLDEN_CROSS_SHORT_MA = 5
GOLDEN_CROSS_LONG_MA = 25
GOLDEN_CROSS_LOOKBACK = 3  # ギャップ縮小・傾き判定に使う直近営業日数

OBV_LOOKBACK = 10

LIQUIDITY_LOOKBACK = 20
LIQUIDITY_MIN_AVG_TURNOVER_JPY = 50_000_000  # 直近20営業日の平均売買金額の下限(初期値、調整可)

BETA_WINDOW = 252  # 日次リターンでβ・相関を計算する期間(約1年)
RANGE_52W_WINDOW = 252  # 52週レンジ位置の計算期間

ATR_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_NUM_STD = 2.0
AVG_VOLUME_WINDOW = 20


def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilderの平滑化によるRSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss  # avg_loss=0はrs=inf(妥当。RSI=100に収束)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # avg_gain=avg_loss=0(値動きなし)の場合のみ中立値50


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def _zscore_slope(series: pd.Series, window: int) -> float:
    """直近windowぶんを標準化し、その回帰直線の傾きを返す。"""
    tail = series.tail(window)
    if tail.std(ddof=0) == 0 or len(tail) < window:
        return 0.0
    z = (tail - tail.mean()) / tail.std(ddof=0)
    x = np.arange(len(z))
    slope, _ = np.polyfit(x, z, 1)
    return float(slope)


def buy_pressure_score(close: pd.Series, volume: pd.Series, window: int = OBV_LOOKBACK) -> float:
    """OBVの傾きが株価の傾きよりどれだけ強いか(標準化した回帰直線の傾きの差)。

    値段の上昇・下降に関わらず常に計算する。正で大きいほど、値段の動きに対して
    出来高の買い方向の勢いが強い(買い優勢)ことを示す。obv_divergence_flagは
    この値を「値段が下落中」という条件つきで使っているのに対し、こちらは
    条件なしの生スコアで、ランキングや別軸のスクリーニングに使う。
    """
    if len(close) < window + 1:
        return 0.0
    obv = compute_obv(close, volume)
    price_slope = _zscore_slope(close, window)
    obv_slope = _zscore_slope(obv, window)
    return float(obv_slope - price_slope)


def obv_divergence_flag(close: pd.Series, volume: pd.Series, window: int = OBV_LOOKBACK) -> bool:
    """直近window営業日で株価が下落しているが、OBVの傾きが株価の傾きより緩やか(または正)。"""
    if len(close) < window + 1:
        return False
    obv = compute_obv(close, volume)
    price_slope = _zscore_slope(close, window)
    obv_slope = _zscore_slope(obv, window)
    return bool(price_slope < 0 and obv_slope > price_slope)


def golden_cross_approaching_flag(
    close: pd.Series,
    short: int = GOLDEN_CROSS_SHORT_MA,
    long: int = GOLDEN_CROSS_LONG_MA,
    lookback: int = GOLDEN_CROSS_LOOKBACK,
) -> bool:
    """短期MAが長期MAをまだ下回るが、直近でギャップが縮小し短期MAが上向きに転じている。"""
    if len(close) < long + lookback:
        return False
    ma_short = close.rolling(short).mean()
    ma_long = close.rolling(long).mean()
    gap = ma_short - ma_long

    gap_now, gap_before = gap.iloc[-1], gap.iloc[-1 - lookback]
    ma_short_now, ma_short_before = ma_short.iloc[-1], ma_short.iloc[-1 - lookback]

    not_yet_crossed = gap_now < 0
    gap_narrowing = gap_now > gap_before
    short_ma_turning_up = ma_short_now > ma_short_before
    return bool(not_yet_crossed and gap_narrowing and short_ma_turning_up)


def golden_cross_recent_flag(
    close: pd.Series,
    short: int = GOLDEN_CROSS_SHORT_MA,
    long: int = GOLDEN_CROSS_LONG_MA,
    lookback: int = GOLDEN_CROSS_LOOKBACK,
) -> bool:
    """直近lookback営業日以内に短期MAが長期MAを上に抜けた(=すでにゴールデンクロス済み)。"""
    if len(close) < long + lookback:
        return False
    ma_short = close.rolling(short).mean()
    ma_long = close.rolling(long).mean()
    gap = ma_short - ma_long

    return bool(gap.iloc[-1] > 0 and gap.iloc[-1 - lookback] < 0)


def uptrend_turning_flag(close: pd.Series) -> bool:
    """ゴールデンクロス接近中、またはすでに直近でクロス済み(=上昇トレンドへの転換期)。"""
    return golden_cross_approaching_flag(close) or golden_cross_recent_flag(close)


def ytd_high_decline_pct(close: pd.Series) -> float:
    """今年に入ってからの最高値からの下落率(%)。close.index は日付(DatetimeIndex)である必要がある。"""
    current_year = close.index[-1].year
    ytd = close[close.index.year == current_year]
    if ytd.empty:
        return 0.0
    ytd_high = ytd.max()
    if ytd_high == 0:
        return 0.0
    return float((close.iloc[-1] - ytd_high) / ytd_high * 100)


def high_low_range_position(close: pd.Series, window: int = RANGE_52W_WINDOW) -> float:
    """直近window営業日の値幅の中で、現在値がどのあたりか(0=52週安値、100=52週高値)。

    過去5〜10年のPBR/PERレンジという理想的な指標は無料データでは取得できないため、
    「現在の値段が自身の直近1年の値幅の中でどの位置にあるか」を歴史的な位置づけの
    代替指標として使う(低いほど「歴史的な安値圏に近い」ことを示す)。
    """
    tail = close.tail(window)
    high, low = tail.max(), tail.min()
    if high == low:
        return 50.0
    return float((close.iloc[-1] - low) / (high - low) * 100)


def beta_and_correlation(
    close: pd.Series, index_close: pd.Series, window: int = BETA_WINDOW
) -> tuple[float | None, float | None]:
    """日経平均に対するβ値と相関係数。データが少なすぎる場合は(None, None)。"""
    stock_ret = close.pct_change()
    index_ret = index_close.pct_change()
    aligned = pd.concat([stock_ret, index_ret], axis=1, join="inner").dropna()
    aligned = aligned.tail(window)
    if len(aligned) < 30:
        return None, None

    stock_r, index_r = aligned.iloc[:, 0], aligned.iloc[:, 1]
    variance = index_r.var()
    if variance == 0:
        return None, None
    beta = float(stock_r.cov(index_r) / variance)
    correlation = float(stock_r.corr(index_r))
    return beta, correlation


def liquidity_ok_flag(
    close: pd.Series,
    volume: pd.Series,
    window: int = LIQUIDITY_LOOKBACK,
    min_turnover: float = LIQUIDITY_MIN_AVG_TURNOVER_JPY,
) -> bool:
    if len(close) < window:
        return False
    turnover = (close * volume).tail(window).mean()
    return bool(turnover >= min_turnover)


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Wilderの平滑化によるATR(Average True Range)。1日あたりの値動きの大きさ(ボラティリティ)を示す。"""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def bollinger_bands(
    close: pd.Series, period: int = BOLLINGER_PERIOD, num_std: float = BOLLINGER_NUM_STD
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """20日移動平均±2標準偏差のボリンジャーバンド。(上限, 中心線, 下限)を返す。"""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def bollinger_band_width_pct(close: pd.Series, period: int = BOLLINGER_PERIOD, num_std: float = BOLLINGER_NUM_STD) -> float:
    """バンド幅を中心線に対する%で表したもの。数値が大きいほど値動きが荒い(ボラティリティが高い)。"""
    upper, mid, lower = bollinger_bands(close, period, num_std)
    if mid.iloc[-1] == 0 or pd.isna(mid.iloc[-1]):
        return 0.0
    return float((upper.iloc[-1] - lower.iloc[-1]) / mid.iloc[-1] * 100)


def average_volume(volume: pd.Series, window: int = AVG_VOLUME_WINDOW) -> float:
    """直近window営業日の平均出来高(株数)。少なすぎる銘柄は自分の注文で価格が飛びやすい(スリッページリスク)。"""
    if len(volume) < window:
        return float(volume.mean())
    return float(volume.tail(window).mean())


def compute_volatility_metrics(df: pd.DataFrame) -> dict:
    """ATR・ボリンジャーバンド幅・平均出来高をまとめて返す(リスク管理パラメータの計算に使う)。"""
    high, low, close, volume = df["High"], df["Low"], df["Close"], df["Volume"]
    atr = compute_atr(high, low, close)
    atr_latest = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None
    close_latest = float(close.iloc[-1])
    return {
        "atr14": atr_latest,
        "atr_pct": (atr_latest / close_latest * 100) if atr_latest and close_latest else None,
        "bollinger_band_width_pct": bollinger_band_width_pct(close),
        "avg_volume_20d": average_volume(volume),
    }


def compute_market_relative_metrics(close: pd.Series, nikkei_close: pd.Series, nikkei_ytd_decline: float) -> dict:
    """日経平均を基準にした指標(β・相関・年初来下落率の比較)。OHLCVの取得元は個別銘柄と共通。"""
    beta, correlation = beta_and_correlation(close, nikkei_close)
    ytd_decline = ytd_high_decline_pct(close)
    return {
        "ytd_decline_pct": ytd_decline,
        "sold_more_than_nikkei_flag": bool(ytd_decline < nikkei_ytd_decline),
        "range_position_52w": high_low_range_position(close),
        "beta": beta,
        "correlation": correlation,
    }


def compute_latest_metrics(df: pd.DataFrame) -> dict:
    """OHLCV DataFrame(日付昇順)から最新日の指標をまとめて返す。"""
    close, volume = df["Close"], df["Volume"]

    rsi = compute_rsi(close)
    rsi_latest = float(rsi.iloc[-1])

    return {
        "close": float(close.iloc[-1]),
        "volume": float(volume.iloc[-1]),
        "rsi14": rsi_latest,
        "rsi_flag": bool(rsi_latest > RSI_OVERBOUGHT or rsi_latest < RSI_OVERSOLD),
        "obv_divergence_flag": obv_divergence_flag(close, volume),
        "buy_pressure_score": buy_pressure_score(close, volume),
        "golden_cross_flag": golden_cross_approaching_flag(close),
        "golden_cross_recent_flag": golden_cross_recent_flag(close),
        "uptrend_turning_flag": uptrend_turning_flag(close),
        "liquidity_ok": liquidity_ok_flag(close, volume),
    }
