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

from .data import fetch_history_bulk, fetch_nikkei_history
from .engine import run_backtest_on_signals, summarize
from .experiment_log import log_experiment
from .run_backtest import nikkei_daily_returns, resolve_tickers
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
    parser.add_argument("--risk_pct", type=float, default=None, help="1トレードのリスク%%(未指定ならrisk_managementの既定値)")
    parser.add_argument("--lot_size", type=int, default=None, help="売買単位(株)。単元未満株を想定する場合は1を指定")
    parser.add_argument("--min_signals", type=int, default=None, help="エントリーに必要なシグナル数(未指定なら本番と同じ2)")
    parser.add_argument(
        "--max_positions_per_industry", type=int, default=None,
        help="同じ業種を同時に保有できる上限ポジション数。未指定なら制限しない",
    )
    parser.add_argument("--max_drawdown_pct", type=float, default=None, help="口座ドローダウンによる新規エントリー停止しきい値")
    parser.add_argument("--nikkei_crash_pct", type=float, default=None, help="日経急落による新規エントリー停止しきい値")
    parser.add_argument("--beta_weighted_halt_pct", type=float, default=None, help="β加重想定インパクトによる新規エントリー停止しきい値")
    parser.add_argument(
        "--trailing_stop", action="store_true",
        help="損切りラインを買値固定ではなく、値段が上がるほど切り上がるトレーリングストップにする",
    )
    parser.add_argument("--correlation_window", type=int, default=None, help="相関ベース分散の計算に使う日数")
    parser.add_argument(
        "--max_avg_correlation", type=float, default=None,
        help="新規候補と保有中銘柄群との平均相関がこれを超えたら見送る(0〜1)",
    )
    parser.add_argument(
        "--target_portfolio_beta", type=float, default=None,
        help="保有中銘柄の加重平均βがこれを超えたら、新規ポジションのサイズを比例的に縮小する",
    )
    parser.add_argument("--sweep", action="store_true", help="ATR倍率・リスクリワード比の感度分析を行う")
    parser.add_argument(
        "--rolling_folds", type=int, default=None,
        help="全期間をこの数の独立した期間に区切り、期間ごとの成績のばらつき(相場局面依存度)を見る。指定時はin-sample/out-of-sample分割の代わりにこちらを実行",
    )
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


def run_split_check(
    signals_by_ticker: dict,
    cutoff: pd.Timestamp,
    capital: float,
    commission_pct: float,
    slippage_pct: float,
    strategy_kwargs: dict | None = None,
) -> None:
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    is_result = run_backtest_on_signals(in_sample, capital, commission_pct, slippage_pct, strategy_kwargs)
    oos_result = run_backtest_on_signals(out_of_sample, capital, commission_pct, slippage_pct, strategy_kwargs)

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
    signals_by_ticker: dict,
    cutoff: pd.Timestamp,
    capital: float,
    commission_pct: float,
    slippage_pct: float,
    strategy_kwargs: dict | None = None,
) -> None:
    """銘柄ごとに独立した口座でプールする方式(signal_quality.pyと同じ)でのin-sample/out-of-sample比較。

    run_split_check()は複数銘柄を1つの共有口座で回すため、銘柄数が多いと資金の奪い合いで
    トレード数が極端に少なくなり、信頼できる比較にならない(300銘柄超で実際に確認済み)。
    こちらはシグナル自体の期待値を、資金制約から切り離して比較する。
    """
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    print(f"\n=== [プール方式] in-sample vs out-of-sample (分割日: {cutoff.date()}) ===")
    is_trades = run_pooled(in_sample, capital, commission_pct, slippage_pct, strategy_kwargs)
    oos_trades = run_pooled(out_of_sample, capital, commission_pct, slippage_pct, strategy_kwargs)

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


def run_sweep(signals_by_ticker: dict, cutoff: pd.Timestamp, capital: float, commission_pct: float, slippage_pct: float) -> dict | None:
    in_sample, out_of_sample = split_signals(signals_by_ticker, cutoff)

    print(f"\n=== パラメータ感度分析(in-sampleのみで評価、分割日: {cutoff.date()}) ===")
    print(f"デフォルト: ATR倍率={rm.ATR_STOP_MULTIPLIER}, リスクリワード比={rm.RISK_REWARD_RATIO}")

    rows = []
    time_returns_by_label = {}
    for atr_mult, rr in itertools.product(ATR_MULTIPLIER_GRID, RISK_REWARD_GRID):
        kwargs = {"atr_multiplier": atr_mult, "risk_reward_ratio": rr}
        result = run_backtest_on_signals(in_sample, capital, commission_pct, slippage_pct, kwargs)
        s = summarize(result)
        s["atr_multiplier"] = atr_mult
        s["risk_reward_ratio"] = rr
        rows.append(s)
        print_row(f"ATR{atr_mult}/RR{rr}", s)
        if result is not None:
            time_returns_by_label[f"ATR{atr_mult}_RR{rr}"] = result["time_return"]

    ranked = [r for r in rows if r["trades"] >= MIN_TRADES_FOR_RANKING and r["sqn"] is not None]
    ranked.sort(key=lambda r: r["sqn"], reverse=True)

    if not ranked:
        print("\n有効なトレード数を確保できた組み合わせがなく、感度分析の結論は出せない。")
        return None

    best = ranked[0]
    print(f"\nin-sampleでのSQN最良: ATR倍率={best['atr_multiplier']}, リスクリワード比={best['risk_reward_ratio']} (SQN={best['sqn']})")

    # PBO(Probability of Backtest Overfitting): 9通りの候補のうちin-sampleで一番良かった
    # ものが、実は偶然の勝者に過ぎない確率をCSCV(組み合わせ対称交差検証)で定量化する
    # (2026-08-14、IKEDAさんの記事を受けて追加)。in-sample期間のみを使う
    # (out-of-sampleは最終確認用に封印したままにする、という既存の方針を崩さないため)。
    pbo_result = None
    try:
        from .pbo import build_returns_matrix, compute_pbo

        returns_matrix = build_returns_matrix(time_returns_by_label)
        pbo_result = compute_pbo(returns_matrix, n_blocks=10)
        print(
            f"\nPBO(選んだ設定が偶然の勝者である確率、CSCV): {pbo_result['pbo']:.2f} "
            f"({pbo_result['n_splits']}通りの分割で推定)"
        )
        if pbo_result["pbo"] >= 0.5:
            print("⚠ PBOが0.5以上。in-sampleで一番良く見えた設定は、選んでも選ばなくても大差ない可能性が高い。")
        elif pbo_result["pbo"] >= 0.3:
            print("△ PBOがやや高め。選んだ設定への過度な期待は禁物。")
        else:
            print("✓ PBOは低め。in-sampleの勝者は偶然ではなく、比較的安定して良い可能性が高い。")
    except Exception as e:  # noqa: BLE001
        print(f"\n(PBO計算をスキップ: {e})")

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

    return pbo_result


def run_rolling_folds(
    signals_by_ticker: dict,
    n_folds: int,
    capital: float,
    commission_pct: float,
    slippage_pct: float,
    strategy_kwargs: dict | None = None,
    shared_kwargs: dict | None = None,
) -> tuple[list[float], list[float]]:
    """戻り値: (プール方式の期間ごとの期待値%リスト, 共有口座方式の期間ごとのSQNリスト)。
    experiment_log.pyへの自動記録用(呼び出し側で使わなくてもよい)。
    """
    """全期間をn_folds個の独立した(重複しない)期間に区切り、期間ごとの成績を並べる。

    1回のin-sample/out-of-sample分割だと、たまたま「未知期間」に選んだ1つの相場局面
    (例: 2023〜2026年の強い上げ相場)が良かった/悪かっただけなのか、相場局面によらず
    エッジが安定しているのかを区別できない(2026-08-14、oos_years=1→3で実際にこの問題に
    遭遇し、in-sample SQN 0.19 → out-of-sample SQN 2.57という大きなばらつきが出た)。
    複数の独立した期間を並べて見ることで、「毎回だいたい同じくらいの成績」なのか
    「期間によって全く違う」のかを区別する。
    """
    all_dates = pd.concat([df.index.to_series() for df in signals_by_ticker.values() if len(df)])
    overall_start, overall_end = all_dates.min(), all_dates.max()
    total_days = (overall_end - overall_start).days
    fold_days = total_days // n_folds

    boundaries = [overall_start + pd.Timedelta(days=fold_days * i) for i in range(n_folds + 1)]
    boundaries[-1] = overall_end + pd.Timedelta(days=1)  # 最後の境界だけ確実に全データを含むよう補正

    print(f"\n=== ローリング検証(全期間を独立した{n_folds}期間に分割) ===")

    pooled_rows, shared_rows = [], []
    for i in range(n_folds):
        fold_start, fold_end = boundaries[i], boundaries[i + 1]
        fold_signals = {
            t: df[(df.index >= fold_start) & (df.index < fold_end)] for t, df in signals_by_ticker.items()
        }
        label = f"期間{i+1}({fold_start.date()}〜{(fold_end - pd.Timedelta(days=1)).date()})"

        pooled_trades = run_pooled(fold_signals, capital, commission_pct, slippage_pct, strategy_kwargs)
        pooled_stats = _pooled_stats(pooled_trades)
        pooled_stats["label"] = label
        pooled_rows.append(pooled_stats)

        shared_result = run_backtest_on_signals(fold_signals, capital, commission_pct, slippage_pct, shared_kwargs)
        shared_stats = summarize(shared_result)
        shared_stats["label"] = label
        shared_rows.append(shared_stats)

    print("\n--- プール方式(銘柄ごとに独立した口座) ---")
    for r in pooled_rows:
        _print_pooled_row(r["label"], r)

    print("\n--- 共有口座方式(1つの口座を想定) ---")
    for r in shared_rows:
        print_row(r["label"], r)

    print()
    pooled_exps = [r["expectancy_pct"] for r in pooled_rows if r["expectancy_pct"] is not None]
    shared_sqns = [r["sqn"] for r in shared_rows if r["sqn"] is not None]
    if pooled_exps:
        print(f"プール方式の期待値/トレード: 期間ごとに {[f'{e:+.2f}%' for e in pooled_exps]}")
        if all(e > 0 for e in pooled_exps):
            print("✓ 全期間でプラス。相場局面によらず比較的安定している。")
        elif any(e <= 0 for e in pooled_exps):
            print("⚠ マイナスの期間がある。相場局面への依存度が高い可能性がある。")
    if shared_sqns:
        print(f"共有口座方式のSQN: 期間ごとに {[f'{s:.2f}' for s in shared_sqns]}")
        if max(shared_sqns) - min(shared_sqns) > 2.0:
            print("⚠ 期間によるSQNのばらつきが大きい(2.0超)。共有口座方式はトレード数が少なく、")
            print("  1回の実現順序に結果が左右されやすいため、この振れ幅そのものは想定の範囲内。")
    print("※ いずれも過去データ上のシミュレーションであり、将来の利益を保証するものではない。")

    return pooled_exps, shared_sqns


def main() -> None:
    args = parse_args()
    tickers = resolve_tickers(args.tickers, args.max_tickers)

    print(f"価格データ取得中: {len(tickers)}銘柄 x {args.years}年...")
    t0 = time.time()
    prices = fetch_history_bulk(tickers, args.years)
    nikkei = fetch_nikkei_history(args.years)
    print(f"取得完了: {len(prices)}/{len(tickers)}銘柄 + 日経平均 ({time.time() - t0:.0f}秒)")

    t0 = time.time()
    signals_by_ticker = {
        ticker: compute_all_signals(raw, args.min_signals, index_close=nikkei["Close"])
        for ticker, raw in prices.items()
        if not raw.empty and len(raw) >= 60
    }
    print(f"指標計算完了: {len(signals_by_ticker)}銘柄 ({time.time() - t0:.0f}秒)")

    if not signals_by_ticker:
        raise SystemExit("有効なデータが1銘柄も取得できませんでした")

    strategy_kwargs = {}
    if args.risk_pct is not None:
        strategy_kwargs["risk_pct"] = args.risk_pct
    if args.lot_size is not None:
        strategy_kwargs["lot_size"] = args.lot_size
    if args.trailing_stop:
        strategy_kwargs["use_trailing_stop"] = True

    # 業種分散は「複数銘柄を同時に保有する」という概念があって初めて意味を持つため、
    # 銘柄ごとに独立した口座で回すプール方式(run_pooled_split_check)には渡さない。
    shared_kwargs = dict(strategy_kwargs)
    if args.max_positions_per_industry is not None:
        from pipeline.universe import get_industry_map

        shared_kwargs["industry_by_ticker"] = get_industry_map()
        shared_kwargs["max_positions_per_industry"] = args.max_positions_per_industry
    if args.correlation_window is not None and args.max_avg_correlation is not None:
        shared_kwargs["correlation_window"] = args.correlation_window
        shared_kwargs["max_avg_correlation"] = args.max_avg_correlation
    if args.target_portfolio_beta is not None:
        shared_kwargs["target_portfolio_beta"] = args.target_portfolio_beta
    if args.max_drawdown_pct is not None:
        shared_kwargs["max_drawdown_pct"] = args.max_drawdown_pct
    if args.nikkei_crash_pct is not None or args.beta_weighted_halt_pct is not None:
        shared_kwargs["nikkei_returns"] = nikkei_daily_returns(nikkei)
        if args.nikkei_crash_pct is not None:
            shared_kwargs["nikkei_crash_pct"] = args.nikkei_crash_pct
        if args.beta_weighted_halt_pct is not None:
            shared_kwargs["beta_weighted_halt_pct"] = args.beta_weighted_halt_pct

    # 試行履歴の記録用(IKEDAさんの「試行回数・選択プロセスを検証対象にする」という指摘を受けて
    # 2026-08-14追加)。tickers引数の解決済み全銘柄リストは長すぎるので含めず、指定値のみ残す。
    config_summary = (
        f"tickers={args.tickers}(max={args.max_tickers}) years={args.years} capital={args.capital:.0f} "
        f"risk_pct={args.risk_pct} lot_size={args.lot_size} min_signals={args.min_signals} "
        f"trailing_stop={args.trailing_stop} max_positions_per_industry={args.max_positions_per_industry} "
        f"correlation_window={args.correlation_window} max_avg_correlation={args.max_avg_correlation} "
        f"target_portfolio_beta={args.target_portfolio_beta} max_drawdown_pct={args.max_drawdown_pct} "
        f"nikkei_crash_pct={args.nikkei_crash_pct} beta_weighted_halt_pct={args.beta_weighted_halt_pct}"
    )

    t0 = time.time()
    if args.rolling_folds:
        pooled_exps, shared_sqns = run_rolling_folds(
            signals_by_ticker, args.rolling_folds, args.capital, args.commission_pct, args.slippage_pct,
            strategy_kwargs, shared_kwargs,
        )
        headline = f"pooled_expectancy_pct={[round(e, 2) for e in pooled_exps]} shared_sqn={[round(s, 2) for s in shared_sqns]}"
        log_experiment(
            script="walkforward.py --rolling_folds",
            config_summary=config_summary + f" rolling_folds={args.rolling_folds}",
            method="rolling_folds",
            headline_metric=headline,
            verdict="info",
            notes="自動記録。採用/不採用の判断は別途人間が行う。",
        )
    else:
        latest_dates = [df.index.max() for df in signals_by_ticker.values() if len(df)]
        cutoff = max(latest_dates) - pd.Timedelta(days=int(args.oos_years * 365))
        run_pooled_split_check(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct, strategy_kwargs)
        run_split_check(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct, shared_kwargs)
        pbo_result = None
        if args.sweep:
            pbo_result = run_sweep(signals_by_ticker, cutoff, args.capital, args.commission_pct, args.slippage_pct)
        headline = "(詳細はコンソール出力参照。in-sample/out-of-sample分割検証)"
        if pbo_result is not None:
            headline += f" PBO={pbo_result['pbo']:.2f}({pbo_result['n_splits']}分割)"
        log_experiment(
            script="walkforward.py",
            config_summary=config_summary + f" oos_years={args.oos_years} sweep={args.sweep}",
            method="split_check" + ("+sweep+PBO" if args.sweep else ""),
            headline_metric=headline,
            verdict="info",
            notes="自動記録。採用/不採用の判断は別途人間が行う。",
        )
    print(f"\n(検証処理時間: {time.time() - t0:.0f}秒)")


if __name__ == "__main__":
    main()
