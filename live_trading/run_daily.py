"""kabuステーションAPIを使った日次実行ループ(現物取引、JP株)。

backtest/strategy.py(ShortlistStrategy)で検証済みのロジックを、backtraderのバー単位実行
ではなく「1日1回の判断」に移植したもの。使うルールはすべて同じ:
  - エントリー条件: storage/local_cache.pyのdaily_metrics(pipeline.run_dailyが毎朝自動更新)の
    is_shortlisted(本日の注目銘柄)をそのまま使う。シグナル計算をここで重複させない
  - ATRベースの損切り/利確: pipeline/risk_management.py
  - 業種分散: 同じ業種を同時に何ポジションまで持つか
  - ポートフォリオ全体のサーキットブレーカー: 口座ドローダウン・日経急落・β加重インパクト
    (2026-08-14のバックテスト検証で「保険として有効、ただし過度に厳しいと逆効果」と確認済みの
    しきい値をデフォルトにしている)

【重要: 2026-08-14時点でまだ実機未検証】
kabuステーションAPIのProfessionalプランを未取得のため、このモジュール全体が一度も実際の
APIに接続してテストされていない。特に以下は公式サンプルコードで確認できておらず、
初めて検証用環境(PRODUCTION=False)に接続した時点で、生のレスポンスを見て確認・修正すること:
  - get_positions()のレスポンスに含まれる、保有銘柄の評価額のフィールド名
    (下記ではEvalPriceという仮の名前を使っているが未確認)
  - get_board()のレスポンスに含まれる、現在値のフィールド名(下記ではCurrentPriceという
    仮の名前を使っているが未確認)
検証用環境は常に固定値を返すため、これらの検証自体はできるが、「戦略として正しく動くか」の
検証にはならない(paper_trading/Binance Testnetと同じ制約。詳細はlive_trading/client.pyの
docstring参照)。

実行方法(検証用環境がデフォルト):
    python -m live_trading.run_daily
"""

import datetime as dt
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import storage
from .client import KabuStationClient, KabuStationError
from pipeline import risk_management as rm
from pipeline.fetch_prices import fetch_nikkei225_index
from pipeline.universe import get_industry_map
from storage.local_cache import read_daily_metrics

# 2026-08-14のバックテスト検証(walkforward.py --rolling_folds 3)で確認済みの設定。
# 変更する場合は、必ずバックテストで再検証してから反映すること。
RISK_PCT = rm.DEFAULT_RISK_PCT_PER_TRADE * 0.25  # 0.5%相当(検証済みの小口座向け設定)
LOT_SIZE = 1  # プチ株(単元未満株)を想定
MAX_POSITIONS_PER_INDUSTRY = 1
MAX_DRAWDOWN_PCT = 30.0
NIKKEI_CRASH_PCT = 5.0
BETA_WEIGHTED_HALT_PCT = 5.0


def get_nikkei_today_return() -> float:
    nikkei = fetch_nikkei225_index()
    closes = nikkei["Close"].dropna().tail(2)
    if len(closes) < 2:
        return 0.0
    return float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)


def check_halt(account_value: float, nikkei_return: float, portfolio_beta: float) -> tuple[bool, str | None]:
    """新規エントリーを停止すべきか判定する(既存ポジションの決済判断はここでは行わない)。"""
    peak_value = max(storage.peak_value_so_far(), account_value)

    drawdown_pct = (account_value - peak_value) / peak_value * 100 if peak_value else 0.0
    if drawdown_pct <= -MAX_DRAWDOWN_PCT:
        return True, f"口座ドローダウン{drawdown_pct:.1f}%"

    if nikkei_return <= -NIKKEI_CRASH_PCT:
        return True, f"日経急落{nikkei_return:.1f}%"

    estimated_impact = portfolio_beta * nikkei_return
    if estimated_impact <= -BETA_WEIGHTED_HALT_PCT:
        return True, f"β加重想定インパクト{estimated_impact:.1f}%"

    return False, None


def run(today: str | None = None, production: bool = False) -> None:
    today = today or dt.date.today().isoformat()
    print(f"=== live_trading 実行開始 (date={today}, 環境={'本番' if production else '検証用'}) ===")

    client = KabuStationClient(production=production)
    client.authenticate()

    cash = client.get_cash_balance()
    positions = client.get_positions()
    # NOTE: 評価額のフィールド名は未確認(モジュールdocstring参照)。実機接続時に要修正。
    holdings_value = sum(float(p.get("EvalPrice", 0)) for p in positions)
    account_value = cash + holdings_value

    held_tickers = {str(p["Symbol"]) for p in positions}
    industry_map = get_industry_map()

    daily_metrics = read_daily_metrics(today)
    if daily_metrics.empty:
        print(f"⚠ {today}のdaily_metricsがありません。pipeline.run_dailyが先に実行されている必要があります。")
        return
    beta_by_ticker = dict(zip(daily_metrics["ticker"].astype(str), daily_metrics["beta"]))

    portfolio_beta = 0.0
    if held_tickers:
        total_value, weighted = 0.0, 0.0
        for p in positions:
            ticker = str(p["Symbol"])
            value = float(p.get("EvalPrice", 0))
            beta = beta_by_ticker.get(ticker) or 1.0
            total_value += value
            weighted += value * beta
        portfolio_beta = weighted / total_value if total_value else 0.0

    nikkei_return = get_nikkei_today_return()
    halted, halt_reason = check_halt(account_value, nikkei_return, portfolio_beta)
    peak_value = max(storage.peak_value_so_far(), account_value)
    storage.log_daily_run(today, account_value, peak_value, halted, halt_reason)

    print(f"口座評価額: {account_value:,.0f}円(現金{cash:,.0f}円 + 保有株評価額{holdings_value:,.0f}円)")
    print(f"日経平均当日リターン: {nikkei_return:+.2f}% ／ 保有銘柄の加重平均β: {portfolio_beta:.2f}")

    if halted:
        print(f"⚠ 新規エントリーを停止(理由: {halt_reason})。既存ポジションの管理は別途手動または今後の実装で対応。")
        return

    industry_position_count: dict[str, int] = {}
    for ticker in held_tickers:
        industry = industry_map.get(ticker)
        if industry:
            industry_position_count[industry] = industry_position_count.get(industry, 0) + 1

    shortlist = daily_metrics[daily_metrics["is_shortlisted"] == 1]
    print(f"本日の注目銘柄: {len(shortlist)}銘柄")

    for _, row in shortlist.iterrows():
        ticker = str(row["ticker"])
        if ticker in held_tickers:
            continue

        industry = industry_map.get(ticker)
        if industry and industry_position_count.get(industry, 0) >= MAX_POSITIONS_PER_INDUSTRY:
            continue

        atr = row.get("atr14")
        entry_price = row.get("close")
        if atr is None or entry_price is None or atr <= 0 or entry_price <= 0:
            continue

        stop = rm.stop_loss_price(entry_price, atr)
        shares = rm.position_size_shares(account_value, RISK_PCT, entry_price, stop, LOT_SIZE)
        if shares <= 0:
            continue

        try:
            order = client.send_cash_buy_order(ticker, shares, entry_price)
            order_id = order.get("OrderId") or order.get("OrderID")
            storage.log_order(today, ticker, "BUY", shares, entry_price, order_id, "SENT")
            print(f"発注: 買い {ticker} {shares}株 @ {entry_price}円(注文ID: {order_id})")
            if industry:
                industry_position_count[industry] = industry_position_count.get(industry, 0) + 1
        except KabuStationError as e:
            storage.log_order(today, ticker, "BUY", shares, entry_price, None, "ERROR", note=str(e))
            print(f"⚠ 発注失敗 {ticker}: {e}")

    print("=== live_trading 実行完了 ===")


if __name__ == "__main__":
    run(production=False)
