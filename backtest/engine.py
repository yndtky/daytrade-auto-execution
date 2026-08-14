"""事前計算済みのシグナルDataFrame群から、Backtraderでのシミュレーションを実行する共通部分。

run_backtest.py(通常の1回きりバックテスト)とwalkforward.py(in-sample/out-of-sample分割・
パラメータ感度分析)の両方から呼ばれる。データ取得・指標計算(重い処理)とシミュレーション実行を
分離しておくことで、walkforward.py側は同じ指標データを使い回しながら期間だけ切り替えて
何度もシミュレーションを回せる(取得・計算をパラメータの組み合わせごとに繰り返さない)。
"""

import backtrader as bt
import pandas as pd

from .data import SignalPandasData
from .strategy import ShortlistStrategy

# backtraderは複数データフィードを1つのCerebroに混ぜると、一番開始日が遅い(=データ期間が
# 短い)フィードに全体の実行期間を合わせてしまう(next()がそのフィードの開始日以降しか
# 呼ばれない)。新規上場銘柄が1つ混ざるだけで「5年分のつもりが実質数ヶ月」になり得るため
# (2026-08-14に実際にこれで結果が壊れたことがある)、最も早く始まる銘柄の開始日から
# この日数以上遅れて始まる銘柄は、共有口座シミュレーションの対象から外す。
MAX_START_DATE_LAG_DAYS = 30


def run_backtest_on_signals(
    signals_by_ticker: dict,
    capital: float,
    commission_pct: float = 0.0,
    slippage_pct: float = 0.001,
    strategy_kwargs: dict | None = None,
) -> dict | None:
    """{ticker: シグナルDataFrame} を受け取ってバックテストを実行する(複数銘柄は1つの共有口座)。

    有効な銘柄が1つもない(全銘柄が最小データ長に満たない)場合はNoneを返す。
    """
    candidates = {t: s for t, s in signals_by_ticker.items() if s is not None and len(s) >= 60}
    if not candidates:
        return None

    earliest_start = min(s.index.min() for s in candidates.values())
    cutoff = earliest_start + pd.Timedelta(days=MAX_START_DATE_LAG_DAYS)
    skipped = [t for t, s in candidates.items() if s.index.min() > cutoff]
    if skipped:
        print(
            f"⚠ 開始日が{earliest_start.date()}から{MAX_START_DATE_LAG_DAYS}日以上遅い{len(skipped)}銘柄を"
            f"共有口座シミュレーションから除外(混在させると全体の実行期間がそこに引きずられるため): "
            f"{', '.join(skipped[:10])}{' ...' if len(skipped) > 10 else ''}"
        )

    cerebro = bt.Cerebro()
    loaded = []
    for ticker, signals in candidates.items():
        if ticker in skipped:
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
        "injections": strat.injection_log,
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
