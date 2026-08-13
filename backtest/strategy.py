"""既存の「本日の注目銘柄」ロジック(pipeline/signals_walkforward.py)をエントリー条件とし、
ATRベースの損切り/利確(pipeline/risk_management.py)で決済するBacktrader戦略。

複数銘柄(複数data)を同一のブローカー(共通の資金枠)でまとめて回せる設計。
ポジションサイズはエントリー時点の口座評価額(self.broker.getvalue())を基準に、
risk_managementの「1トレードあたりの許容損失額」ルールで都度計算する
(固定額ではなく、含み損益で資金が増減すればサイズも追随する)。
"""

import backtrader as bt

from pipeline import risk_management as rm


class ShortlistStrategy(bt.Strategy):
    params = (
        ("risk_pct", rm.DEFAULT_RISK_PCT_PER_TRADE),
        ("atr_multiplier", rm.ATR_STOP_MULTIPLIER),
        ("risk_reward_ratio", rm.RISK_REWARD_RATIO),
        ("lot_size", rm.LOT_SIZE),
    )

    def __init__(self):
        self.pending = set()  # entry注文が約定待ちのdata名(重複エントリー防止)
        self.entry_sizes = {}  # data名 -> 約定株数(trade close時にはtrade.sizeが0に戻るため別管理)
        self.trade_log = []

    def next(self):
        for d in self.datas:
            name = d._name
            if self.getposition(d).size:
                continue
            if name in self.pending:
                continue
            if d.entry_signal[0] <= 0:
                continue

            entry_price = float(d.close[0])
            atr = float(d.atr14[0])
            if atr <= 0 or entry_price <= 0:
                continue

            stop = rm.stop_loss_price(entry_price, atr, self.p.atr_multiplier)
            target = rm.take_profit_price(entry_price, stop, self.p.risk_reward_ratio)
            capital = self.broker.getvalue()
            shares = rm.position_size_shares(capital, self.p.risk_pct, entry_price, stop, self.p.lot_size)
            if shares <= 0:
                continue

            self.buy_bracket(
                data=d,
                size=shares,
                price=entry_price,
                exectype=bt.Order.Market,
                stopprice=stop,
                limitprice=target,
            )
            self.pending.add(name)

    def notify_order(self, order):
        if order.parent is not None:
            return  # bracket子注文(損切り/利確)自体は通知だけ受けて何もしない
        if order.status == order.Completed and order.isbuy():
            self.entry_sizes[order.data._name] = order.executed.size
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected, order.Expired):
            self.pending.discard(order.data._name)

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        name = trade.data._name
        size = self.entry_sizes.pop(name, None)
        self.trade_log.append(
            {
                "ticker": name,
                "entry_date": bt.num2date(trade.dtopen).date().isoformat(),
                "exit_date": bt.num2date(trade.dtclose).date().isoformat(),
                "size": size,
                "pnl_jpy": round(trade.pnl, 0),
                "pnl_net_jpy": round(trade.pnlcomm, 0),
                "pnl_pct": round(trade.pnlcomm / (trade.price * size) * 100, 2) if size else None,
            }
        )
