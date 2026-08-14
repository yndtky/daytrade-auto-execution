"""既存の「本日の注目銘柄」ロジック(pipeline/signals_walkforward.py)をエントリー条件とし、
ATRベースの損切り/利確(pipeline/risk_management.py)で決済するBacktrader戦略。

複数銘柄(複数data)を同一のブローカー(共通の資金枠)でまとめて回せる設計。
ポジションサイズはエントリー時点の口座評価額(self.broker.getvalue())を基準に、
risk_managementの「1トレードあたりの許容損失額」ルールで都度計算する
(固定額ではなく、含み損益で資金が増減すればサイズも追随する)。

資金の小さい口座では、同じ日に複数銘柄でシグナルが出ても全部は買えない。買い優勢
スコア順に優先してエントリーする案を試したが、self.datasの並び順(ほぼ無作為)の
ままの方が結果が良かった(2026-08-14の検証)。買い優勢スコアは元々別軸のシグナル用の
指標で、このATR損切り/利確ロジックでのトレード結果を予測する根拠がなかったと考えられる。
安易な「賢い順序」を追加で試すより、まずこの設計をwalk-forward検証で確かめる方針とし、
優先順位ロジックは無地(self.datasの並び順)に戻している。
"""

import backtrader as bt

from pipeline import risk_management as rm


class ShortlistStrategy(bt.Strategy):
    params = (
        ("risk_pct", rm.DEFAULT_RISK_PCT_PER_TRADE),
        ("atr_multiplier", rm.ATR_STOP_MULTIPLIER),
        ("risk_reward_ratio", rm.RISK_REWARD_RATIO),
        ("lot_size", rm.LOT_SIZE),
        # 「途中から入金する」を試すための予定表。{"YYYY-MM-DD": 追加額(円)} の形。
        # 該当日のnext()内でself.broker.add_cash()する(backtraderは開始時のsetcash()しか
        # 素の状態では持たないため、途中入金はストラテジー側で明示的に行う必要がある)。
        ("cash_injections", {}),
        # 業種分散: 同じ業種を同時に何ポジションまで持つかの上限。industry_by_tickerが
        # 空、または該当銘柄の業種が不明な場合は制限しない(後方互換のためデフォルト無効)。
        ("industry_by_ticker", {}),
        ("max_positions_per_industry", None),
    )

    def __init__(self):
        self.pending = set()  # entry注文が約定待ちのdata名(重複エントリー防止)
        self.entry_sizes = {}  # data名 -> 約定株数(trade close時にはtrade.sizeが0に戻るため別管理)
        self.trade_log = []
        self._pending_injections = dict(self.p.cash_injections)  # 消化済みは都度popする
        self.injection_log = []  # 実際に入金した(日付, 金額)の記録
        self.industry_position_count = {}  # 業種名 -> 現在保有中(建玉待ち含む)のポジション数

    def next(self):
        today = self.datas[0].datetime.date(0).isoformat()
        amount = self._pending_injections.pop(today, None)
        if amount:
            self.broker.add_cash(amount)
            self.injection_log.append((today, amount))

        for d in self.datas:
            name = d._name
            if self.getposition(d).size:
                continue
            if name in self.pending:
                continue
            if d.entry_signal[0] <= 0:
                continue

            industry = self.p.industry_by_ticker.get(name)
            if (
                self.p.max_positions_per_industry is not None
                and industry is not None
                and self.industry_position_count.get(industry, 0) >= self.p.max_positions_per_industry
            ):
                continue  # 同じ業種を規定数以上すでに保有中(または注文中)なので見送る

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
            if industry is not None:
                self.industry_position_count[industry] = self.industry_position_count.get(industry, 0) + 1

    def notify_order(self, order):
        if order.parent is not None:
            return  # bracket子注文(損切り/利確)自体は通知だけ受けて何もしない
        if order.status == order.Completed and order.isbuy():
            self.entry_sizes[order.data._name] = order.executed.size
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected, order.Expired):
            self.pending.discard(order.data._name)
        if order.status in (order.Canceled, order.Margin, order.Rejected, order.Expired):
            # 発注時に予約した業種枠を、結局約定しなかった分だけ戻す
            industry = self.p.industry_by_ticker.get(order.data._name)
            if industry is not None and self.industry_position_count.get(industry, 0) > 0:
                self.industry_position_count[industry] -= 1

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        name = trade.data._name
        industry = self.p.industry_by_ticker.get(name)
        if industry is not None and self.industry_position_count.get(industry, 0) > 0:
            self.industry_position_count[industry] -= 1
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
