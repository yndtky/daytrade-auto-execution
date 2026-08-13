"""損切り/利確価格・ポジションサイズの計算(リスク管理パラメータ)。

いずれも「計算上の目安」であり、発注を行うものではない。証券口座の資金額・
リスク許容度は個人の状況により大きく異なるため、実際に使う値は必ず本人が
確認・調整すること。ここではATRベースの一般的な計算式を提供するのみ。
"""

ATR_STOP_MULTIPLIER = 2.0  # 損切り幅 = entry - multiplier × ATR(一般的な目安。銘柄・戦略により調整)
RISK_REWARD_RATIO = 2.0  # 利確幅 = risk_reward_ratio × 損切り幅(リスクに対して2倍のリターンを狙う目安)
DEFAULT_RISK_PCT_PER_TRADE = 0.02  # 1トレードで許容する損失額 = 口座資金の2%(一般的によく使われる目安)
LOT_SIZE = 100  # 日本株の単元株数(2018年の単元株統一により、ほぼすべての銘柄が100株単位)


def stop_loss_price(
    entry_price: float, atr: float, multiplier: float = ATR_STOP_MULTIPLIER, direction: str = "long"
) -> float:
    """ATRベースの損切り価格。ボラティリティが大きい銘柄ほど、損切り幅も広く取る。"""
    if direction == "long":
        return entry_price - multiplier * atr
    return entry_price + multiplier * atr


def take_profit_price(
    entry_price: float, stop_loss: float, risk_reward_ratio: float = RISK_REWARD_RATIO, direction: str = "long"
) -> float:
    """損切り幅に対してrisk_reward_ratio倍のリターンを狙う利確価格。"""
    risk = abs(entry_price - stop_loss)
    if direction == "long":
        return entry_price + risk_reward_ratio * risk
    return entry_price - risk_reward_ratio * risk


def position_size_shares(
    account_capital: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    lot_size: int = LOT_SIZE,
) -> int:
    """「1トレードでの許容損失額(=口座資金×risk_pct)」を超えない株数を、単元株数の倍数で返す。

    ATRが小さい(値動きが穏やかな)銘柄では、リスク基準の計算だけだと口座資金を超える
    株数になり得るため、現物取引を前提に「口座資金で買える株数」も上限として課す。
    """
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0 or account_capital <= 0 or entry_price <= 0:
        return 0
    max_loss = account_capital * risk_pct
    risk_based_shares = max_loss / risk_per_share
    affordable_shares = account_capital / entry_price
    raw_shares = min(risk_based_shares, affordable_shares)
    lots = int(raw_shares // lot_size)
    return lots * lot_size


def compute_risk_plan(
    account_capital: float,
    entry_price: float,
    atr: float,
    risk_pct: float = DEFAULT_RISK_PCT_PER_TRADE,
    atr_multiplier: float = ATR_STOP_MULTIPLIER,
    risk_reward_ratio: float = RISK_REWARD_RATIO,
    lot_size: int = LOT_SIZE,
    direction: str = "long",
) -> dict:
    """損切り/利確価格・株数・想定損益をまとめて計算する。あくまで計算結果の提示であり、発注は行わない。"""
    stop = stop_loss_price(entry_price, atr, atr_multiplier, direction)
    take_profit = take_profit_price(entry_price, stop, risk_reward_ratio, direction)
    shares = position_size_shares(account_capital, risk_pct, entry_price, stop, lot_size)
    position_value = shares * entry_price
    max_loss = shares * abs(entry_price - stop)
    max_gain = shares * abs(take_profit - entry_price)
    return {
        "stop_loss_price": stop,
        "take_profit_price": take_profit,
        "shares": shares,
        "position_value_jpy": position_value,
        "position_pct_of_capital": (position_value / account_capital * 100) if account_capital else None,
        "max_loss_jpy": max_loss,
        "max_gain_jpy": max_gain,
    }
