"""事前計算済みのシグナルDataFrame群から、Backtraderでのシミュレーションを実行する共通部分。

run_backtest.py(通常の1回きりバックテスト)とwalkforward.py(in-sample/out-of-sample分割・
パラメータ感度分析)の両方から呼ばれる。データ取得・指標計算(重い処理)とシミュレーション実行を
分離しておくことで、walkforward.py側は同じ指標データを使い回しながら期間だけ切り替えて
何度もシミュレーションを回せる(取得・計算をパラメータの組み合わせごとに繰り返さない)。
"""

import backtrader as bt

from .data import SignalPandasData
from .strategy import ShortlistStrategy


def run_backtest_on_signals(
    signals_by_ticker: dict,
    capital: float,
    commission_pct: float = 0.0,
    slippage_pct: float = 0.001,
    strategy_kwargs: dict | None = None,
) -> dict | None:
    """{ticker: シグナルDataFrame} を受け取ってバックテストを実行する。

    有効な銘柄が1つもない(全銘柄が最小データ長に満たない)場合はNoneを返す。
    """
    cerebro = bt.Cerebro()

    loaded = []
    for ticker, signals in signals_by_ticker.items():
        if signals is None or len(signals) < 60:
            continue
        feed = SignalPandasData(dataname=signals)
        cerebro.adddata(feed, name=ticker)
        loaded.append(ticker)

    if not loaded:
        return None

    cerebro.addstrategy(ShortlistStrategy, **(strategy_kwargs or {}))

    cerebro.broker.setcash(capital)
    cerebro.broker.setcommission(commission=commission_pct)
    cerebro.broker.set_slippage_perc(slippage_pct)

    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="time_return", timeframe=bt.TimeFrame.Days)

    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    strat = results[0]
    end_value = cerebro.broker.getvalue()

    return {
        "tickers": loaded,
        "start_value": start_value,
        "end_value": end_value,
        "strategy": strat,
        "trades_analysis": strat.analyzers.trades.get_analysis(),
        "drawdown": strat.analyzers.dd.get_analysis(),
        "returns": strat.analyzers.returns.get_analysis(),
        "sqn": strat.analyzers.sqn.get_analysis(),
        "time_return": strat.analyzers.time_return.get_analysis(),
    }


def summarize(result: dict) -> dict:
    """resultから主要指標だけを抜き出した軽量な辞書を作る(比較表示・CSV出力向け)。"""
    if result is None:
        return {
            "trades": 0, "win_rate_pct": None, "total_return_pct": None,
            "cagr_pct": None, "max_dd_pct": None, "sqn": None,
        }
    start, end = result["start_value"], result["end_value"]
    trades = result["trades_analysis"]
    # total.totalは建玉中(まだ決済していない)トレードも含むため、勝率の分母には使わない。
    # won.total/lost.totalは決済済みトレードのみを指すので、closed = won+lostを使う。
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    closed_trades = won + lost
    return {
        "trades": closed_trades,
        "win_rate_pct": round(won / closed_trades * 100, 1) if closed_trades else None,
        "total_return_pct": round((end / start - 1) * 100, 1),
        "cagr_pct": round(result["returns"].get("rnorm100", 0.0), 1),
        "max_dd_pct": round(result["drawdown"].get("max", {}).get("drawdown", 0.0), 1),
        "sqn": round(result["sqn"].get("sqn", 0.0), 2) if result["sqn"].get("sqn") is not None else None,
    }
