"""ペーパートレードの1サイクル実行(GitHub Actionsから数分〜十数分おきに呼ばれる想定)。

このフェーズの狙いは「戦略の質」ではなく「仕組みの堅牢性」の検証:
  - プロセスが毎回まっさらな状態で起動する(GitHub Actionsは実行のたびに新しいコンテナ)前提で、
    「自分(ボット)がこれまで発注した履歴」を正として現在ポジションを判定する
  - 通信エラー・注文エラーで異常終了しても、次回実行時に自然に復旧できる設計にする
  - 二重発注(すでに買っているのにまた買う、等)を確実に防ぐ

実際に動かして分かった注意点(2026-08-14): 取引所の生の残高をそのまま「ポジションの有無」に
使うと、口座に最初から入っている残高(Binance Testnetは登録時に自動で1 BTC等が付与される)を
「すでに買っている」と誤認してしまう。このバグを実機で踏んだため、判定は自分の発注履歴
(storage.orders)の買い-売りの差引きで行うようにしている。取引所の残高は「本当にその数量を
持っているか」の突き合わせ(食い違いがあれば警告)にのみ使う。
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import storage
from .client import BinanceTestnetError, get_account_balance, get_klines, get_price, place_market_order
from .strategy import MA_LONG_PERIOD, MA_SHORT_PERIOD, compute_moving_averages, decide_action

SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
INTERVAL = "5m"
KLINES_LIMIT = MA_LONG_PERIOD + 5  # 長期MA + クロス判定用の前時点1本 + 予備
ORDER_QUANTITY = 0.001  # 固定の練習用数量(テストネットなので実際の金額的な意味はない)
POSITION_DUST_THRESHOLD = ORDER_QUANTITY * 0.1  # これ未満の残高は「ポジションなし」とみなす

# サーキットブレーカー(2026-08-14追加): このフェーズの目的は「戦略の質」ではなく
# 「発注・エラー処理・状態管理の仕組みの堅牢性」の検証だが、live_trading/backtestと同じ
# 「口座評価額がピークから大きく下がったら新規エントリーを停止する」仕組み自体もテスト対象に
# 含める(3つのシステムで一貫した設計にしておく)。テストネットの実資金は動かないため、
# しきい値はJP株側ほど厳密に調整していない(BTCは短時間でも値動きが大きいため、あくまで
# 「仕組みが正しく発動・記録されるか」を確認できる程度の値を置いている)。
MAX_DRAWDOWN_PCT = 20.0


def reconcile_position_state() -> tuple[bool, float]:
    """自分の発注履歴から「持っているはずの数量」を計算し、取引所の実残高と突き合わせる。

    ポジションの有無は自分の発注履歴(own_net_position)を正とする。取引所の残高は
    「本当にその数量が存在するか」の確認にのみ使い、大きく食い違えば警告する
    (元から入っていた残高を誤ってポジションと見なさないため。詳細はモジュールdocstring参照)。
    """
    own_position = storage.own_net_position(SYMBOL)
    exchange_balance = get_account_balance(BASE_ASSET)
    in_position = own_position > POSITION_DUST_THRESHOLD

    if in_position and exchange_balance < own_position * 0.9:
        print(
            f"⚠ 自分の発注履歴では{own_position}保有しているはずですが、取引所の残高は{exchange_balance}しかありません"
            "(手動操作や別プロセスからの発注の可能性)。"
        )

    return in_position, own_position


def check_halt(account_value: float) -> tuple[bool, str | None]:
    """新規BUYを停止すべきか判定する(SELLによるポジション解消は妨げない、live_trading/
    backtestと同じ考え方)。"""
    peak_value = max(storage.peak_value_so_far(), account_value)
    drawdown_pct = (account_value - peak_value) / peak_value * 100 if peak_value else 0.0
    if drawdown_pct <= -MAX_DRAWDOWN_PCT:
        return True, f"口座評価額ドローダウン{drawdown_pct:.1f}%"
    return False, None


def run() -> None:
    try:
        klines = get_klines(SYMBOL, INTERVAL, limit=KLINES_LIMIT)
        closes = [float(k[4]) for k in klines]  # klines[i][4] = 終値
        current_price = get_price(SYMBOL)

        ma_short, ma_long = compute_moving_averages(closes)
        prev_ma_short, prev_ma_long = compute_moving_averages(closes[:-1])

        in_position, own_position_qty = reconcile_position_state()
        remote_state = "IN_POSITION" if in_position else "FLAT"

        account_value = get_account_balance(QUOTE_ASSET) + own_position_qty * current_price
        halted, halt_reason = check_halt(account_value)
        peak_value = max(storage.peak_value_so_far(), account_value)
        storage.log_account_snapshot(account_value, peak_value, halted, halt_reason)

        action = decide_action(ma_short, ma_long, prev_ma_short, prev_ma_long, in_position)
        print(
            f"{SYMBOL}: 価格={current_price} MA{MA_SHORT_PERIOD}={ma_short} MA{MA_LONG_PERIOD}={ma_long} "
            f"ポジション={remote_state} 判定={action} 口座評価額={account_value:.2f}{QUOTE_ASSET}"
        )

        if action == "HOLD":
            storage.log_cycle(SYMBOL, "HOLD", current_price, ma_short, ma_long, remote_state)
            return

        if action == "BUY" and halted:
            storage.log_cycle(
                SYMBOL, "BUY_HALTED", current_price, ma_short, ma_long, remote_state, note=halt_reason
            )
            print(f"⚠ 新規BUYを停止(理由: {halt_reason})")
            return

        side = "BUY" if action == "BUY" else "SELL"
        quantity = ORDER_QUANTITY if side == "BUY" else own_position_qty
        if side == "SELL" and quantity <= 0:
            storage.log_cycle(
                SYMBOL, "SELL_SKIPPED", current_price, ma_short, ma_long, remote_state, note="残高0のため見送り"
            )
            return

        try:
            order = place_market_order(SYMBOL, side, round(quantity, 6))
            storage.log_order(SYMBOL, side, quantity, str(order.get("orderId")), order.get("status", "UNKNOWN"), str(order))
            storage.log_cycle(
                SYMBOL, action, current_price, ma_short, ma_long,
                "IN_POSITION" if side == "BUY" else "FLAT",
                note=f"注文成功: {order.get('status')}",
            )
            print(f"発注成功: {side} {quantity} {BASE_ASSET} (注文ID: {order.get('orderId')})")
        except BinanceTestnetError as e:
            storage.log_cycle(SYMBOL, action, current_price, ma_short, ma_long, remote_state, error=str(e))
            print(f"⚠ 発注失敗(次回実行時に取引所の実状態から再判定されます): {e}")

    except BinanceTestnetError as e:
        try:
            storage.log_cycle(SYMBOL, "ERROR", error=str(e))
        except Exception:  # noqa: BLE001
            pass
        print(f"⚠ サイクル実行中にエラー: {e}")
        raise


if __name__ == "__main__":
    run()
