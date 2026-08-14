"""バックテストのエントリーポイント。

使い方の例:
    python -m backtest.run_backtest --tickers 7203,6758,8306 --years 5 --capital 1000000
    python -m backtest.run_backtest --tickers prime --max_tickers 300 --years 5   # プライム市場からランダムに300銘柄
    # 10万円で開始し、半年後・1年後にそれぞれ5万円ずつ入金した場合を試す
    python -m backtest.run_backtest --tickers prime --max_tickers 300 --years 2 --capital 100000 --lot_size 1 \
        --cash_injection 2025-02-14:50000,2025-08-14:50000

指標・エントリー条件は pipeline/signals_walkforward.py (= 本番の「本日の注目銘柄」ロジックと同一)、
決済は pipeline/risk_management.py のATRベース損切り/利確をそのまま使う。手数料はデフォルト0円
(SBI/楽天など主要ネット証券の現物取引ゼロ革命プランを想定)としているが、使う証券会社の実際の
手数料体系に合わせて --commission_pct で調整すること。スリッページは寄り成行の想定で0.1%を
デフォルトとしている(手数料無料でも約定価格のズレは残るため、期待値を過大評価しないよう入れている)。
"""

import argparse
import datetime as dt
import random
import sys
import time
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windowsのコンソール既定コードページ(cp932)対策

from .data import fetch_history_bulk
from .engine import run_backtest_on_signals
from pipeline.signals_walkforward import compute_all_signals
from pipeline.universe import get_full_tse_universe, get_industry_map, get_prime_universe, get_standard_universe

DEFAULT_TICKERS = ["7203", "6758", "9984", "8306", "6501"]  # 例示用(トヨタ/ソニーG/SBG/三菱UFJ/日立)
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_results"
SAMPLE_SEED = 42  # --max_tickers でのランダム抽出を再現可能にする(パラメータ比較のため固定)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本日の注目銘柄ロジックのバックテスト")
    parser.add_argument(
        "--tickers",
        type=str,
        default=",".join(DEFAULT_TICKERS),
        help="証券コードをカンマ区切りで指定、'prime'でプライム市場全銘柄、'full'でプライム+スタンダード+グロース",
    )
    parser.add_argument("--max_tickers", type=int, default=None, help="対象銘柄数の上限(指定時は固定シードでランダム抽出)")
    parser.add_argument("--years", type=int, default=5, help="遡る年数(デフォルト5年)")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初期資金(円、デフォルト100万円)")
    parser.add_argument("--commission_pct", type=float, default=0.0, help="片道手数料率(例: 0.0005=0.05%)")
    parser.add_argument("--slippage_pct", type=float, default=0.001, help="想定スリッページ率(デフォルト0.1%)")
    parser.add_argument("--risk_pct", type=float, default=None, help="1トレードのリスク%%(未指定ならrisk_managementの既定値)")
    parser.add_argument(
        "--lot_size", type=int, default=None,
        help="売買単位(株)。未指定なら100株単位。単元未満株(プチ株など、1株単位)を想定する場合は1を指定",
    )
    parser.add_argument(
        "--min_signals", type=int, default=None,
        help="エントリーに必要なシグナル数(3つ中いくつ以上)。未指定なら本番と同じ2",
    )
    parser.add_argument(
        "--cash_injection", type=str, default=None,
        help="途中入金の予定を'YYYY-MM-DD:金額'をカンマ区切りで指定(例: 2026-03-01:50000,2026-06-01:50000)",
    )
    parser.add_argument(
        "--max_positions_per_industry", type=int, default=None,
        help="同じ業種(33業種区分)を同時に保有できる上限ポジション数。未指定なら制限しない",
    )
    return parser.parse_args()


def parse_cash_injections(spec: str | None) -> dict:
    """'YYYY-MM-DD:金額,YYYY-MM-DD:金額' 形式の文字列を {日付文字列: 金額} の辞書にする。"""
    if not spec:
        return {}
    injections = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        date_str, amount_str = part.split(":")
        injections[date_str.strip()] = float(amount_str.strip())
    return injections


def resolve_tickers(tickers_arg: str, max_tickers: int | None) -> list[str]:
    arg = tickers_arg.strip().lower()
    if arg == "prime":
        universe = get_prime_universe()
        tickers = universe["ticker"].tolist()
    elif arg == "full":
        universe = get_full_tse_universe()
        tickers = universe["ticker"].tolist()
    elif arg == "standard":
        universe = get_standard_universe()
        tickers = universe["ticker"].tolist()
    else:
        tickers = [t.strip() for t in tickers_arg.split(",") if t.strip()]

    if max_tickers is not None and len(tickers) > max_tickers:
        rng = random.Random(SAMPLE_SEED)
        tickers = rng.sample(tickers, max_tickers)

    return tickers


def run(
    tickers: list[str],
    years: int,
    capital: float,
    commission_pct: float,
    slippage_pct: float,
    risk_pct: float | None,
    lot_size: int | None = None,
    min_signals: int | None = None,
    cash_injections: dict | None = None,
    max_positions_per_industry: int | None = None,
) -> dict:
    print(f"価格データ取得中: {len(tickers)}銘柄 x {years}年...")
    t0 = time.time()
    prices = fetch_history_bulk(tickers, years)
    print(f"取得完了: {len(prices)}/{len(tickers)}銘柄 ({time.time() - t0:.0f}秒)")

    t0 = time.time()
    signals_by_ticker = {
        ticker: compute_all_signals(raw, min_signals) for ticker, raw in prices.items() if not raw.empty and len(raw) >= 60
    }
    print(f"指標計算完了: {len(signals_by_ticker)}銘柄 ({time.time() - t0:.0f}秒)")

    strategy_kwargs = {}
    if risk_pct is not None:
        strategy_kwargs["risk_pct"] = risk_pct
    if lot_size is not None:
        strategy_kwargs["lot_size"] = lot_size
    if cash_injections:
        strategy_kwargs["cash_injections"] = cash_injections
    if max_positions_per_industry is not None:
        strategy_kwargs["industry_by_ticker"] = get_industry_map()
        strategy_kwargs["max_positions_per_industry"] = max_positions_per_industry

    t0 = time.time()
    result = run_backtest_on_signals(signals_by_ticker, capital, commission_pct, slippage_pct, strategy_kwargs)
    print(f"シミュレーション実行完了 ({time.time() - t0:.0f}秒)")

    if result is None:
        raise SystemExit("有効なデータが1銘柄も取得できませんでした")
    return result


def print_summary(result: dict) -> None:
    start, end = result["start_value"], result["end_value"]
    injections = result.get("injections") or []
    total_injected = sum(amount for _, amount in injections)
    total_contributed = start + total_injected
    # 途中入金があると「終値/開始値」は入金分まで運用益として数えてしまい過大評価になるため、
    # 「総投入資金に対する損益」を主指標にする(入金がなければ従来通りの単純な比率と一致する)。
    profit_jpy = end - total_contributed
    total_return_pct = (profit_jpy / total_contributed * 100) if total_contributed else 0.0
    trades = result["trades_analysis"]
    # total.totalは建玉中(まだ決済していない)トレードも含む。勝率の分母は決済済み(won+lost)を使う。
    opened_trades = trades.get("total", {}).get("total", 0)
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    closed_trades = won + lost
    still_open = opened_trades - closed_trades
    win_rate = (won / closed_trades * 100) if closed_trades else 0.0
    avg_won = trades.get("won", {}).get("pnl", {}).get("average", 0.0)
    avg_lost = trades.get("lost", {}).get("pnl", {}).get("average", 0.0)
    max_dd = result["drawdown"].get("max", {}).get("drawdown", 0.0)
    cagr = result["returns"].get("rnorm100", None)
    sqn = result["sqn"].get("sqn", None)

    time_return = result.get("time_return") or {}
    if time_return:
        dates = sorted(time_return.keys())
        print(f"実際にシミュレーションした期間: {dates[0]} 〜 {dates[-1]} ({len(dates)}日分)")

    print("\n=== バックテスト結果 ===")
    print(f"対象銘柄: {', '.join(result['tickers'])}")
    if injections:
        for date_str, amount in injections:
            print(f"途中入金: {date_str} に {amount:,.0f}円")
        print(f"初期資金: {start:,.0f}円 + 途中入金合計 {total_injected:,.0f}円 = 総投入資金 {total_contributed:,.0f}円")
        print(f"最終評価額: {end:,.0f}円 (総投入資金に対する損益 {profit_jpy:+,.0f}円, {total_return_pct:+.1f}%)")
        if cagr is not None:
            print(f"年率換算リターン(rnorm100): {cagr:.1f}% ※途中入金のタイミングを厳密には織り込んでいない参考値")
    else:
        print(f"初期資金: {start:,.0f}円 → 最終評価額: {end:,.0f}円 (リターン {total_return_pct:+.1f}%)")
        if cagr is not None:
            print(f"年率換算リターン(rnorm100): {cagr:.1f}%")
    print(f"最大ドローダウン: {max_dd:.1f}%")
    print(f"トレード数: {closed_trades} (勝ち {won} / 負け {lost}, 勝率 {win_rate:.1f}%)" + (f" ／ 期間終了時点で未決済: {still_open}件" if still_open else ""))
    print(f"平均利益(勝ちトレード): {avg_won:,.0f}円 / 平均損失(負けトレード): {avg_lost:,.0f}円")
    if sqn is not None:
        print(f"SQN: {sqn:.2f}")
    print("※ 過去データ上のシミュレーション結果であり、将来の利益を保証するものではない。")


def save_outputs(result: dict, tickers: list[str], years: int) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "-".join(tickers[:5]) + (f"_and_{len(tickers) - 5}more" if len(tickers) > 5 else "")
    base = OUTPUT_DIR / f"{stamp}_{tag}_{years}y"

    trade_log = result["strategy"].trade_log
    if trade_log:
        pd.DataFrame(trade_log).to_csv(f"{base}_trades.csv", index=False, encoding="utf-8-sig")
        print(f"トレード履歴を保存: {base}_trades.csv")

    time_return = result["time_return"]
    if time_return:
        equity = pd.Series(time_return).sort_index()
        equity_curve = (1 + equity).cumprod() * result["start_value"]
        equity_curve.to_csv(f"{base}_equity.csv", header=["equity_jpy"], encoding="utf-8-sig")

        try:
            import matplotlib

            matplotlib.use("Agg")
            matplotlib.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "MS Gothic", "sans-serif"]
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(equity_curve.index, equity_curve.values)
            for date_str, amount in result.get("injections") or []:
                ax.axvline(pd.Timestamp(date_str), color="gray", linestyle="--", alpha=0.6)
                ax.annotate(f"入金 {amount:,.0f}円", (pd.Timestamp(date_str), ax.get_ylim()[1]), rotation=90, va="top", fontsize=8, color="gray")
            ax.set_title("資産推移(バックテスト)")
            ax.set_ylabel("評価額(円)")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(f"{base}_equity.png", dpi=120)
            plt.close(fig)
            print(f"資産推移グラフを保存: {base}_equity.png")
        except ImportError:
            pass


def main() -> None:
    args = parse_args()
    tickers = resolve_tickers(args.tickers, args.max_tickers)

    result = run(
        tickers=tickers,
        years=args.years,
        capital=args.capital,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct,
        risk_pct=args.risk_pct,
        lot_size=args.lot_size,
        min_signals=args.min_signals,
        cash_injections=parse_cash_injections(args.cash_injection),
        max_positions_per_industry=args.max_positions_per_industry,
    )
    print_summary(result)
    save_outputs(result, result["tickers"], args.years)


if __name__ == "__main__":
    main()
