"""ペーパートレード用のシンプルな移動平均クロス判定。

このフェーズの目的は「発注・状態管理・エラー処理の仕組みが正しく動くか」の検証であり、
この戦略自体に投資判断としての意味を持たせていない。JP株のRSI/OBVロジックを流用しない
理由も同じ——検証対象は仕組み側であって、シグナルの質ではない。頻繁にクロスが起きる
短い移動平均を使い、実行サイクルを多く発生させて仕組みを繰り返しテストできるようにしている。
"""

MA_SHORT_PERIOD = 5
MA_LONG_PERIOD = 20


def compute_moving_averages(closes: list[float]) -> tuple[float | None, float | None]:
    """直近のcloses(古い順)から短期・長期移動平均を計算する。データ不足ならNoneを返す。"""
    if len(closes) < MA_LONG_PERIOD:
        return None, None
    ma_short = sum(closes[-MA_SHORT_PERIOD:]) / MA_SHORT_PERIOD
    ma_long = sum(closes[-MA_LONG_PERIOD:]) / MA_LONG_PERIOD
    return ma_short, ma_long


def decide_action(
    ma_short: float | None,
    ma_long: float | None,
    prev_ma_short: float | None,
    prev_ma_long: float | None,
    in_position: bool,
) -> str:
    """直近2時点の移動平均からゴールデンクロス/デッドクロスを判定し、'BUY'/'SELL'/'HOLD'を返す。"""
    if None in (ma_short, ma_long, prev_ma_short, prev_ma_long):
        return "HOLD"

    crossed_up = prev_ma_short <= prev_ma_long and ma_short > ma_long
    crossed_down = prev_ma_short >= prev_ma_long and ma_short < ma_long

    if not in_position and crossed_up:
        return "BUY"
    if in_position and crossed_down:
        return "SELL"
    return "HOLD"
