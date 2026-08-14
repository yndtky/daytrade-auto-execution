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
import numpy as np

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
        # ポートフォリオ全体のリスク管理(3層構成、すべて未指定なら従来通り無効):
        #  1) 口座評価額がピークからmax_drawdown_pct%以上下がったら新規エントリーを停止
        #  2) 日経平均の当日リターンがnikkei_crash_pct%以下なら新規エントリーを停止
        #     (βの前提が崩れる暴落時のための、単純な安全網。βに関係なく機械的に発動する)
        #  3) 保有銘柄のβで加重した「日経急落の想定インパクト」がbeta_weighted_halt_pct%以下
        #     なら新規エントリーを停止(高β銘柄ばかり持っている時ほど早く反応する)
        # いずれも「既存ポジションのATR損切り/利確」は止めない。新規エントリーだけを止める。
        ("nikkei_returns", {}),  # {"YYYY-MM-DD": 当日リターン(%)}
        ("max_drawdown_pct", None),
        ("nikkei_crash_pct", None),
        ("beta_weighted_halt_pct", None),
        # トレーリングストップ(2026-08-14追加、デフォルト無効=従来通り):
        # Falseの場合、損切りラインは買値から固定(entry - atr_multiplier×ATR)のまま保有期間中
        # 変わらない。Trueの場合、値段が上がるほど損切りラインも切り上がる(値段が下がっても
        # 切り下がらない)ため、一度乗った含み益を守りやすい。ただし通常のブレでも早期決済され
        # やすくなり、本来の利確ライン(risk_reward_ratio倍)まで伸びる勝ちトレードを途中で
        # 切ってしまう可能性もある。バックテストで比較してから採用するかどうかを判断すること。
        ("use_trailing_stop", False),
        # 相関ベースの分散(2026-08-14追加、デフォルト無効): 業種分散(カテゴリ的な近さ)を
        # 補完する、値動き(連続的な近さ)による判断。新規候補と「現在保有中の各銘柄」の
        # トレーリングcorrelation_window日リターンのピアソン相関を計算し、平均相関が
        # max_avg_correlationを超える候補は見送る(同じ方向に一斉に動きやすい銘柄ばかり
        # 集めることを避ける狙い)。IKEDAさんの「分散の質・共分散構造を重視すべき」という
        # 指摘を受けた、業種分散に続く2つ目の分散手法。業種分散と独立に併用できる。
        ("correlation_window", None),
        ("max_avg_correlation", None),
        # β連動ポジションサイジング(2026-08-14追加、デフォルト無効): 相関ベース分散(入るか
        # 入らないかの二択)が小口座では効きにくかったため、代わりに「量」を連続的に調整する
        # 案。現在保有中銘柄の時価加重平均β(_portfolio_beta())がtarget_portfolio_betaを
        # 超えている時だけ、新規ポジションのrisk_pctを比例的に縮小する
        # (scale = target_portfolio_beta / current_portfolio_beta、上限1.0)。
        # 保有が無い、またはまだ目標β以下の時は縮小しない(=risk_pctそのまま)。
        ("target_portfolio_beta", None),
    )

    def __init__(self):
        self.pending = set()  # entry注文が約定待ちのdata名(重複エントリー防止)
        self.entry_sizes = {}  # data名 -> 約定株数(trade close時にはtrade.sizeが0に戻るため別管理)
        self.trade_log = []
        self._pending_injections = dict(self.p.cash_injections)  # 消化済みは都度popする
        self.injection_log = []  # 実際に入金した(日付, 金額)の記録
        self.industry_position_count = {}  # 業種名 -> 現在保有中(建玉待ち含む)のポジション数
        self.peak_value = None  # ポートフォリオ評価額の過去最高値(ドローダウン判定用)
        self.halt_log = []  # 新規エントリーを停止した日の記録(理由付き)

    def _portfolio_beta(self) -> float:
        """現在保有中のポジションを、時価で加重平均したβ。ポジションが無ければ0を返す。"""
        total_value, weighted_beta = 0.0, 0.0
        for d in self.datas:
            size = self.getposition(d).size
            if size:
                value = abs(size * float(d.close[0]))
                total_value += value
                weighted_beta += value * float(d.beta[0])
        return weighted_beta / total_value if total_value else 0.0

    def _trailing_returns(self, d) -> np.ndarray | None:
        window = self.p.correlation_window
        if len(d) < window + 1:
            return None
        closes = np.array(d.close.get(size=window + 1))
        if len(closes) < window + 1 or np.any(closes <= 0):
            return None
        return np.diff(closes) / closes[:-1]

    def _avg_correlation_with_holdings(self, d) -> float | None:
        """dの直近correlation_window日リターンと、現在保有中の各銘柄のリターンとの平均相関。
        保有銘柄が無い、またはデータ不足で計算できなければNone(判定を素通りさせる)。
        """
        held = [h for h in self.datas if h._name != d._name and self.getposition(h).size]
        if not held:
            return None

        d_returns = self._trailing_returns(d)
        if d_returns is None or np.std(d_returns) == 0:
            return None

        correlations = []
        for h in held:
            h_returns = self._trailing_returns(h)
            if h_returns is None or np.std(h_returns) == 0:
                continue
            corr = np.corrcoef(d_returns, h_returns)[0, 1]
            if not np.isnan(corr):
                correlations.append(corr)

        return float(np.mean(correlations)) if correlations else None

    def _risk_pct_scale(self) -> float:
        """現在の保有β合計が目標を超えている分だけ、新規ポジションのrisk_pctを縮小する倍率。
        target_portfolio_beta未指定、保有無し、または目標未満ならそのまま1.0(縮小なし)。
        """
        if self.p.target_portfolio_beta is None:
            return 1.0
        current_beta = self._portfolio_beta()
        if current_beta <= self.p.target_portfolio_beta:
            return 1.0
        return self.p.target_portfolio_beta / current_beta

    def _check_halt_new_entries(self, today: str) -> bool:
        """新規エントリーを停止すべきか判定する(既存ポジションの決済には影響しない)。"""
        value = self.broker.getvalue()
        self.peak_value = value if self.peak_value is None else max(self.peak_value, value)

        if self.p.max_drawdown_pct is not None:
            drawdown_pct = (value - self.peak_value) / self.peak_value * 100 if self.peak_value else 0.0
            if drawdown_pct <= -self.p.max_drawdown_pct:
                self.halt_log.append((today, f"口座ドローダウン{drawdown_pct:.1f}%"))
                return True

        nikkei_return = self.p.nikkei_returns.get(today)
        if nikkei_return is not None:
            if self.p.nikkei_crash_pct is not None and nikkei_return <= -self.p.nikkei_crash_pct:
                self.halt_log.append((today, f"日経急落{nikkei_return:.1f}%"))
                return True

            if self.p.beta_weighted_halt_pct is not None:
                estimated_impact = self._portfolio_beta() * nikkei_return
                if estimated_impact <= -self.p.beta_weighted_halt_pct:
                    self.halt_log.append((today, f"β加重想定インパクト{estimated_impact:.1f}%"))
                    return True

        return False

    def next(self):
        today = self.datas[0].datetime.date(0).isoformat()
        amount = self._pending_injections.pop(today, None)
        if amount:
            self.broker.add_cash(amount)
            self.injection_log.append((today, amount))

        if self._check_halt_new_entries(today):
            return  # 新規エントリーのみ停止。既存ポジションのブラケット注文(損切り/利確)は生きたまま

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

            if self.p.correlation_window is not None and self.p.max_avg_correlation is not None:
                avg_corr = self._avg_correlation_with_holdings(d)
                if avg_corr is not None and avg_corr > self.p.max_avg_correlation:
                    continue  # 保有中の銘柄群と値動きが似すぎているので見送る

            entry_price = float(d.close[0])
            atr = float(d.atr14[0])
            if atr <= 0 or entry_price <= 0:
                continue

            stop = rm.stop_loss_price(entry_price, atr, self.p.atr_multiplier)
            target = rm.take_profit_price(entry_price, stop, self.p.risk_reward_ratio)
            capital = self.broker.getvalue()
            effective_risk_pct = self.p.risk_pct * self._risk_pct_scale()
            shares = rm.position_size_shares(capital, effective_risk_pct, entry_price, stop, self.p.lot_size)
            if shares <= 0:
                continue

            if self.p.use_trailing_stop:
                # 固定ラインと同じ幅(atr_multiplier×ATR)をトレール幅として使う(比較を公平にするため)。
                # 値段が上がるほど損切りラインも切り上がり、下がっても切り下がらない。
                self.buy_bracket(
                    data=d,
                    size=shares,
                    price=entry_price,
                    exectype=bt.Order.Market,
                    stopexec=bt.Order.StopTrail,
                    trailamount=entry_price - stop,
                    limitprice=target,
                )
            else:
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
