"""in-sample/out-of-sample分割によるバックテストの過学習チェック。

「IKEDAさん」の指摘(実運用でSR2.0を超えるような明確なエッジが常に存在するのは怪しい、
たまたま/過学習の可能性を疑うべき)を踏まえ、単に1回バックテストして良い数値が出て終わり、
にしないための仕組み。全期間のうち末尾の一定期間(デフォルト直近1年)を「見ていない期間」
として完全に切り離し、
  1) in-sample期間(古い方)だけを見てパラメータの当たりを付ける(--sweepで感度分析)
  2) その結果をout-of-sample期間(新しい方、一度も参照しない)で検証する
という順序を強制する。in-sampleで良く見えた設定がout-of-sampleで崩れるなら、それは
偶然/過学習の可能性が高いと判断する。

使い方の例:
    # 分割検証のみ(デフォルトパラメータで in-sample vs out-of-sample を比較)
    python -m backtest.walkforward --tickers prime --max_tickers 300 --years 6 --oos_years 1

    # パラメータ感度分析つき(in-sampleでATR倍率・リスクリワード比の組み合わせを試し、
    # 最良の組み合わせをout-of-sampleで再検証する)
    python -m backtest.walkforward --tickers prime --max_tickers 300 --years 6 --oos_years 1 --sweep
"""

import argparse
import itertools
import sys
import time

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data import fetch_history_bulk
from .engine import run_backtest_on_signals, summarize
from .run_backtest import resolve_tickers
from .signal_quality import run_pooled
from pipeline import risk_management as rm
from pipeline.signals_walkforward import compute_all_signals

# 感度分析用のグリッド(値そのものを最適化して埋め込むのではなく、「近い値でも結果が
# 大きく崩れないか」を見るためのもの。極端に細かく振らない)
ATR_MULTIPLIER_GRID = [1.5, 2.0, 2.5]
RISK_REWARD_GRID = [1.5, 2.0, 3.0]
MIN_TRADES_FOR_RANKING = 10  # トレード数がこれ未満の組み合わせは、たまたまの結果として順位付けから除外


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="in-sample/out-of-sample分割によるバックテスト検証")
    parser.add_argument("--tickers", type=str, default="prime")
    parser.add_argument("--max_tickers", type=int, default=300)
    parser.add_argument("--years", type=int, default=6, help="全期間の年数(in-sample+out-of-sample合計)")
    parser.add_argument("--oos_years", type=float, default=1.0, help="末尾を切り離すout-of-sample期間の長さ(年)")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--commission_pct", type=float, default=0.0)
    parser.add_argument("--slippage_pct", type=float, default=0.001)
    parser.add_argument("--sweep", action="store_true", help="ATR倍率・リスクリワード比の感度分析を行う")
    return parser.parse_args()


def split_signals(signals_by_ticker: dict, cutoff: pd.Timestamp) -> tuple[dict, dict]:
    in_sample, out_of_sample = {}, {}
    for ticker, df in signals_by_ticker.items():
        in_sample[ticker] = df[df.index < cutoff]
        out_of_sample[ticker] = df[df.index >= cutoff]
    return in_sample, out_of_sample


def print_row(label: str, s: dict) -> None:
    trades = s["trades"]
    win = f"{s['win_rate_pct']:.1f}%" if s["win_rate_pct"] is not None else "-"
    ret = f"{s['total_return_pct']:+.1f}%" if s["total_return_pct"] is not None else "-"
    cagr = f"{s['cagr_pct']:+.1f}%" if s["cagr_pct"] is not None else "-"
    dd = f"{s['max_dd_pct']:.1f}%" if s["max_dd_pct"] is not None else "-"
    sqn = f"{s['sqn']:.2f}" if s["sqn"] is not None else "-"
    print(f"{label:<18} トレード数:{trades:>4}  勝率:{win:>7}  リターン:{ret:>8}  年率:{cagr:>7}  最大DD:{dd:>7}  SQN:{sqn:>6}")


def run_split_check(signals_by_ticker: dict, cutoff: pd.Timestamp, capital: float, commission_pct: float, slippage_pct: float) -> None:
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    is_result = run_backtest_on_signals(in_sample, capital, commission_pct, slippage_pct)
    oos_result = run_backtest_on_signals(out_of_sample, capital, commission_pct, slippage_pct)

    is_summary = summarize(is_result)
    oos_summary = summarize(oos_result)

    print(f"\n=== in-sample vs out-of-sample (分割日: {cutoff.date()}) ===")
    print_row("in-sample(既知)", is_summary)
    print_row("out-of-sample(未知)", oos_summary)

    _print_verdict(is_summary, oos_summary)


def _print_verdict(is_summary: dict, oos_summary: dict) -> None:
    is_cagr, oos_cagr = is_summary["cagr_pct"], oos_summary["cagr_pct"]
    oos_trades = oos_summary["trades"]

    print()
    if oos_trades < MIN_TRADES_FOR_RANKING:
        print(f"⚠ out-of-sampleのトレード数が{oos_trades}件と少なく、結果の信頼性は低い(判断保留)。")
    elif is_cagr is not None and oos_cagr is not None:
        if oos_cagr <= 0 and is_cagr > 0:
            print("⚠ in-sampleではプラスだが、out-of-sampleではマイナス。in-sampleの結果は偶然/過学習の疑いが強い。")
        elif is_cagr > 0 and oos_cagr < is_cagr * 0.4:
            print("⚠ out-of-sampleの年率リターンがin-sampleの4割未満に低下。エッジが弱まっている可能性がある。")
        else:
            print("✓ out-of-sampleでもin-sampleと同程度以上の傾向が続いている(過学習の兆候は薄い)。")
    print("※ いずれも過去データ上のシミュレーションであり、将来の利益を保証するものではない。")


def _pooled_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "win_rate_pct": None, "expectancy_pct": None}
    win_rate = (trades["pnl_net_jpy"] > 0).mean() * 100
    expectancy = trades["pnl_pct"].mean()
    return {"trades": len(trades), "win_rate_pct": round(win_rate, 1), "expectancy_pct": round(expectancy, 2)}


def _print_pooled_row(label: str, s: dict) -> None:
    win = f"{s['win_rate_pct']:.1f}%" if s["win_rate_pct"] is not None else "-"
    exp = f"{s['expectancy_pct']:+.2f}%" if s["expectancy_pct"] is not None else "-"
    print(f"{label:<20} トレード数:{s['trades']:>6}  勝率:{win:>7}  期待値/トレード:{exp:>8}")


def run_pooled_split_check(
    signals_by_ticker: dict, cutoff: pd.Timestamp, capital: float, commission_pct: float, slippage_pct: float
) -> None:
    """銘柄ごとに独立した口座でプールする方式(signal_quality.pyと同じ)でのin-sample/out-of-sample比較。

    run_split_check()は複数銘柄を1つの共有口座で回すため、銘柄数が多いと資金の奪い合いで
    トレード数が極端に少なくなり、信頼できる比較にならない(300銘柄超で実際に確認済み)。
    こちらはシグナル自体の期待値を、資金制約から切り離して比較する。
    """
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    print(f"\n=== [プール方式] in-sample vs out-of-sample (分割日: {cutoff.date()}) ===")
    is_trades = run_pooled(in_sample, capital, commission_pct, slippage_pct)
    oos_trades = run_pooled(out_of_sample, capital, commission_pct, slippage_pct)

    is_stats = _pooled_stats(is_trades)
    oos_stats = _pooled_stats(oos_trades)
    _print_pooled_row("in-sample(既知)", is_stats)
    _print_pooled_row("out-of-sample(未知)", oos_stats)

    print()
    if oos_stats["trades"] < 100:
        print(f"⚠ out-of-sampleのトレード数が{oos_stats['trades']}件と少なく、結果の信頼性は低い(判断保留)。")
    elif is_stats["expectancy_pct"] is not None and oos_stats["expectancy_pct"] is not None:
        is_exp, oos_exp = is_stats["expectancy_pct"], oos_stats["expectancy_pct"]
        if oos_exp <= 0 and is_exp > 0:
            print("⚠ in-sampleでは期待値プラスだが、out-of-sampleではマイナス。過学習/偶然の疑いが強い。")
        elif is_exp > 0 and oos_exp < is_exp * 0.4:
            print("⚠ out-of-sampleの期待値がin-sampleの4割未満に低下。エッジが弱まっている可能性がある。")
        else:
            print("✓ out-of-sampleでもin-sampleと同程度以上の期待値が続いている(過学習の兆候は薄い)。")
    print("※ いずれも過去データ上のシミュレーションであり、将来の利益を保証するものではない(銘柄ごとに独立した口座を想定した集計)。")


def run_sweep(signals_by_ticker: dict, cutoff: pd.Timestamp, capital: float, commission_pct: float, slippage_pct: float) -> None:
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    print(f"\n=== パラメータ感度分析(in-sampleのみで評価、分割日: {cutoff.date()}) ===")
    print(f"デフォルト: ATR倍率={rm.ATR_STOP_MULTIPLIER}, リスクリワード比={rm.RISK_REWARD_RATIO}")

    rows = []
    for atr_mult, rr in itertools.product(ATR_MULTIPLIER_GRID, RISK_REWARD_GRID):
        kwargs = {"atr_multiplier": atr_mult, "risk_reward_ratio": rr}
        result = run_backtest_on_signals(in_sample, capital, commission_pct, slippage_pct, kwargs)
        s = summarize(result)
        s["atr_multiplier"] = atr_mult
        s["risk_reward_ratio"] = rr
        rows.append(s)
        print_row(f"ATR{atr_mult}/RR{rr}", s)

    ranked = [r for r in rows if r["trades"] >= MIN_TRADES_FOR_RANKING and r["sqn"] is not None]
    ranked.sort(key=lambda r: r["sqn"], reverse=True)

    if not ranked:
        print("\n有効なトレード数を確保できた組み合わせがなく、感度分析の結論は出せない。")
        return

    best = ranked[0]
    print(f"\nin-sampleでのSQN最良: ATR倍率={best['atr_multiplier']}, リスクリワード比={best['risk_reward_ratio']} (SQN={best['sqn']})")

    print("\n--- この組み合わせをout-of-sampleで再検証 ---")
    best_kwargs = {"atr_multiplier": best["atr_multiplier"], "risk_reward_ratio": best["risk_reward_ratio"]}
    default_oos = summarize(run_backtest_on_signals(out_of_sample, capital, commission_pct, slippage_pct))
    best_oos = summarize(run_backtest_on_signals(out_of_sample, capital, commission_pct, slippage_pct, best_kwargs))
    print_row("デフォルト設定(oos)", default_oos)
    print_row("in-sample最良(oos)", best_oos)

    if best_oos["sqn"] is not None and default_oos["sqn"] is not None and best_oos["sqn"] < default_oos["sqn"]:
        print("\n⚠ in-sampleで最良だった組み合わせが、out-of-sampleではデフォルト設定より悪化している。")
        print("  これはまさにパラメータ調整がin-sampleへの過学習になっている典型例。デフォルト値を変えない方が無難。")
    else:
        print("\n✓ in-sampleで良かった組み合わせは、out-of-sampleでも(少なくともデフォルトを下回らない程度に)機能している。")


def main() -> None:
    args = parse_args()
    tickers = resolve_tickers(args.tickers, args.max_tickers)

    print(f"価格データ取得中: {len(tickers)}銘柄 x {args.years}年...")
    t0 = time.time()
    prices = fetch_history_bulk(tickers, args.years)
    print(f"取得完了: {len(prices)}/{len(tickers)}銘柄 ({time.time() - t0:.0f}秒)")

    t0 = time.time()
    signals_by_ticker = {
        ticker: compute_all_signals(raw) for ticker, raw in prices.items() if not raw.empty and len(raw) >= 60
    }
    print(f"指標計算完了: {len(signals_by_ticker)}銘柄 ({time.time() - t0:.0f}秒)")

    if not signals_by_ticker:
        raise SystemExit("有効なデータが1銘柄も取得できませんでした")

    latest_dates = [df.index.max() for df in signals_by_ticker.values() if len(df)]
    cutoff = max(latest_dates) - pd.Timedelta(days=int(args.oos_years * 365))

    t0 = time.time()
    run_pooled_split_check(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct)
    run_split_check(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct)
    if args.sweep:
        run_sweep(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct)
    print(f"\n(検証処理時間: {time.time() - t0:.0f}秒)")


if __name__ == "__main__":
    main()
