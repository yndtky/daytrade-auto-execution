"""シグナル自体の期待値を検証する(単一口座の資金制約から切り離した集計テスト)。

run_backtest.py(--tickers prime)は全銘柄を1つの共有口座(共通の現金)でシミュレーションする。
これは「実際に自分の口座で運用したらどうなるか」には近いが、口座資金が小さいと同時に持てる
ポジション数がすぐ頭打ちになり、シグナルの大半が資金不足で見送られてしまう
(実際、1557銘柄5年でわずか18トレードしか発生しなかった — シグナルの質ではなく、
資金の奪い合いに結果が支配されてしまっている)。

「このエントリー/決済ロジックそのものに期待値があるか」を検証するには、銘柄ごとに
独立した口座(同じ初期資金)で個別にバックテストし、出てきた全トレードをプールして
集計する方が適切(=各シグナルが本来持つはずの発生機会を、資金制約で潰さずに評価できる)。
一方でこれは「実際にこの資金で運用した場合の資産推移」ではないことに注意。

使い方の例:
    python -m backtest.signal_quality --tickers prime --max_tickers 300 --years 5
"""

import argparse
import sys
import time

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data import fetch_history_bulk
from .engine import run_backtest_on_signals
from .run_backtest import OUTPUT_DIR, resolve_tickers
from pipeline.signals_walkforward import compute_all_signals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="銘柄ごとに独立した口座でバックテストし、全トレードをプールして集計する")
    parser.add_argument("--tickers", type=str, default="prime")
    parser.add_argument("--max_tickers", type=int, default=300)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--capital", type=float, default=1_000_000, help="銘柄ごとに与える独立した初期資金(円)")
    parser.add_argument("--commission_pct", type=float, default=0.0)
    parser.add_argument("--slippage_pct", type=float, default=0.001)
    return parser.parse_args()


PLAUSIBLE_PNL_PCT_LIMIT = 100  # ATRベース1トレードの損益率がこれを超えるのはほぼ確実にデータ異常


def _drop_outlier_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """異常なpnl_pct(データ不備・銘柄コードの誤解決などが原因)を検知して除外する。

    ATRベースの損切り/利確幅(2×ATR損切り、その2倍=4×ATRの利確)を考えると、1トレードの
    損益率が±100%を超えることは通常あり得ない。実際、universe.py側のticker形式(4文字)
    フィルター漏れの優先株コード(5桁)がyfinanceで誤った銘柄に解決し、+90457%という
    明らかに異常なpnl_pctを生んで集計全体を壊した事例があった(2026-08-13)。
    """
    if trades.empty or "pnl_pct" not in trades.columns:
        return trades
    is_outlier = trades["pnl_pct"].abs() > PLAUSIBLE_PNL_PCT_LIMIT
    if is_outlier.any():
        outliers = trades.loc[is_outlier, ["ticker", "entry_date", "exit_date", "pnl_pct"]]
        print(f"⚠ 異常なpnl_pct(|値|>{PLAUSIBLE_PNL_PCT_LIMIT}%)のトレードを{is_outlier.sum()}件除外(データ不備の疑い):")
        print(outliers.to_string(index=False))
    return trades.loc[~is_outlier].reset_index(drop=True)


def run_pooled(signals_by_ticker: dict, capital: float, commission_pct: float, slippage_pct: float) -> pd.DataFrame:
    """銘柄ごとに個別のCerebroでバックテストし、全トレードを1つのDataFrameにまとめる。"""
    all_trades = []
    for i, (ticker, signals) in enumerate(signals_by_ticker.items(), start=1):
        result = run_backtest_on_signals({ticker: signals}, capital, commission_pct, slippage_pct)
        if result is not None:
            all_trades.extend(result["strategy"].trade_log)
        if i % 200 == 0:
            print(f"  {i}/{len(signals_by_ticker)}銘柄処理済み...")
    return _drop_outlier_trades(pd.DataFrame(all_trades))


def print_report(trades: pd.DataFrame, n_tickers: int) -> None:
    print(f"\n=== シグナル期待値の集計(独立口座プール方式、対象{n_tickers}銘柄) ===")
    if trades.empty:
        print("トレードが1件も発生しませんでした。")
        return

    total = len(trades)
    won = (trades["pnl_net_jpy"] > 0).sum()
    win_rate = won / total * 100
    avg_pnl_pct = trades["pnl_pct"].mean()
    avg_win_pct = trades.loc[trades["pnl_net_jpy"] > 0, "pnl_pct"].mean()
    avg_loss_pct = trades.loc[trades["pnl_net_jpy"] <= 0, "pnl_pct"].mean()
    expectancy_pct = win_rate / 100 * avg_win_pct + (1 - win_rate / 100) * avg_loss_pct

    print(f"総トレード数: {total}(プールされた銘柄数の実効サンプル)")
    print(f"勝率: {win_rate:.1f}%")
    print(f"平均リターン/トレード: {avg_pnl_pct:+.2f}%")
    print(f"平均利益(勝ち): {avg_win_pct:+.2f}% ／ 平均損失(負け): {avg_loss_pct:+.2f}%")
    print(f"期待値(1トレードあたり): {expectancy_pct:+.2f}%")
    print()
    if total < 100:
        print("⚠ サンプル数がまだ少なく(100件未満)、この期待値の信頼性は低い。")
    elif expectancy_pct > 0:
        print("✓ プールしたトレードの期待値はプラス。ただし1トレードあたりの値であり、")
        print("  実際の口座では同時に何ポジション持てるか(資金制約)で年間の実現リターンは変わる。")
    else:
        print("⚠ プールしたトレードの期待値がマイナス。エントリー/決済ロジック自体を見直す必要がある。")
    print("※ 過去データ上のシミュレーションであり、将来の利益を保証するものではない。")


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

    print(f"銘柄ごとに独立した口座({args.capital:,.0f}円)でバックテスト中...")
    t0 = time.time()
    trades = run_pooled(signals_by_ticker, args.capital, args.commission_pct, args.slippage_pct)
    print(f"完了 ({time.time() - t0:.0f}秒)")

    print_report(trades, len(signals_by_ticker))

    if not trades.empty:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        import datetime as dt

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"{stamp}_signal_quality_{len(signals_by_ticker)}tickers_{args.years}y_trades.csv"
        trades.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n全トレードを保存: {path}")


if __name__ == "__main__":
    main()
