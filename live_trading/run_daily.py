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
  - 監理・整理銘柄の除外: JPX公式ページ(pipeline.universe.get_supervised_tickers())から
    現在の監理・整理銘柄一覧を取得し、新規エントリー対象から除外する。無料データ(yfinance)では
    上場廃止銘柄を遡ってバックテストに組み込む生存者バイアス対策ができなかった(2026-08-14、
    データソースの構造的な限界)代わりの、前向きなリスク回避策

【決済(損切り・利確)の管理について】
kabuステーションAPIはOCO注文(利確・損切りのセット注文)に未対応(公式Issue #1119、
2026-08-14時点で「内部で検討中」)。そのため、このモジュールは毎回の実行で3段階の
「疑似OCO」ライフサイクル管理を行う(live_trading/storage.pyのopen_positionsテーブルで追跡):
  1. entry_pending → 新規買い注文の約定確認。約定していれば損切り(逆指値)・利確(指値)の
     両方を新たに発注してholdingへ。未約定のまま終了していればclosedへ(何もしない)
  2. holding → 損切り・利確の両注文の状態を確認。どちらかが約定していれば、もう一方を
     キャンセルしてclosedへ
  3. closed → 完了。それ以上何もしない
毎回の実行の冒頭でこの2段階の確認(reconcile_entry_fills/reconcile_exit_fills)を行ってから、
サーキットブレーカー判定・新規エントリー走査に進む。

【重要: 2026-08-14時点でまだ実機未検証】
kabuステーションAPIのProfessionalプランを未取得のため、このモジュール全体が一度も実際の
APIに接続してテストされていない。client.pyのdocstringに記載の通り、フィールド名・enum値は
公式OpenAPI仕様で裏取り済みだが、実際のレスポンス(特に/ordersのDetails配下)は未確認。
検証用環境(ポート18081)は常に固定値を返すため、通信・パース・状態遷移のコードが正しく
動くかは検証できるが、「戦略として正しく動くか」の検証にはならない。

実行方法(検証用環境がデフォルト):
    python -m live_trading.run_daily
"""

import datetime as dt
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import storage
from .client import ORDER_STATE_FINISHED, KabuStationClient, KabuStationError
from pipeline import risk_management as rm
from pipeline.fetch_prices import fetch_nikkei225_index
from pipeline.universe import get_industry_map, get_supervised_tickers
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


def reconcile_entry_fills(client: KabuStationClient, today: str) -> None:
    """entry_pending中のポジションについて、買い注文が約定していれば損切り・利確の両注文を出す。"""
    pending = storage.read_open_positions()
    pending = pending[pending["status"] == "entry_pending"]
    if pending.empty:
        return

    orders_by_id = {o["ID"]: o for o in client.get_orders()}
    for _, pos in pending.iterrows():
        order = orders_by_id.get(pos["entry_order_id"])
        if order is None or order.get("State") != ORDER_STATE_FINISHED:
            continue  # まだ処理中、または注文情報が見つからない(次回の実行で再確認)

        filled_qty = int(order.get("CumQty", 0))
        if filled_qty <= 0:
            storage.close_position(pos["id"], today, "entry_unfilled")
            print(f"エントリー未約定のため終了: {pos['ticker']}")
            continue

        try:
            stop_order = client.send_cash_sell_stop_order(pos["ticker"], filled_qty, pos["stop_price"])
            target_order = client.send_cash_sell_order(pos["ticker"], filled_qty, pos["target_price"])
            stop_id = stop_order.get("OrderId")
            target_id = target_order.get("OrderId")
            storage.mark_position_holding(pos["id"], filled_qty, stop_id, target_id)
            storage.log_order(today, pos["ticker"], "SELL_STOP", filled_qty, pos["stop_price"], stop_id, "SENT")
            storage.log_order(today, pos["ticker"], "SELL_TARGET", filled_qty, pos["target_price"], target_id, "SENT")
            print(
                f"エントリー約定確認: {pos['ticker']} {filled_qty}株 → "
                f"損切り{pos['stop_price']:.0f}円/利確{pos['target_price']:.0f}円の決済注文を発注"
            )
        except KabuStationError as e:
            print(f"⚠ {pos['ticker']}: 決済注文の発注に失敗(次回の実行で再試行が必要): {e}")


def reconcile_exit_fills(client: KabuStationClient, today: str) -> None:
    """holding中のポジションについて、損切り・利確のどちらかが約定していればもう一方をキャンセルする(疑似OCO)。

    client.pyの損切り・利確注文はどちらもExpireDay=0(当日限り)で出しているため、その日のうちに
    約定しなければ翌日には自動的に失効し、ポジションが決済注文の無い無防備な状態になる
    (2026-08-15、外部資料の指摘で気づいた実在のバグ)。両方とも失効・未約定と判定した場合は、
    同じ価格で決済注文を出し直す。
    """
    holding = storage.read_open_positions()
    holding = holding[holding["status"] == "holding"]
    if holding.empty:
        return

    orders_by_id = {o["ID"]: o for o in client.get_orders()}

    def _is_filled(order_id: str) -> bool:
        order = orders_by_id.get(order_id)
        return order is not None and order.get("State") == ORDER_STATE_FINISHED and int(order.get("CumQty", 0)) > 0

    def _is_expired_unfilled(order_id: str) -> bool:
        order = orders_by_id.get(order_id)
        return order is not None and order.get("State") == ORDER_STATE_FINISHED and int(order.get("CumQty", 0)) == 0

    for _, pos in holding.iterrows():
        stop_filled = _is_filled(pos["stop_order_id"])
        target_filled = _is_filled(pos["target_order_id"])

        if stop_filled and target_filled:
            print(f"⚠ {pos['ticker']}: 損切り・利確が両方約定と判定(通常あり得ない異常系、要手動確認)")
            storage.close_position(pos["id"], today, "both_filled_anomaly")
            continue

        if stop_filled:
            try:
                client.cancel_order(pos["target_order_id"])
            except KabuStationError as e:
                print(f"⚠ {pos['ticker']}: 利確注文のキャンセル失敗(手動確認が必要): {e}")
            storage.close_position(pos["id"], today, "stop_hit")
            print(f"損切り約定: {pos['ticker']}(利確注文はキャンセル)")
        elif target_filled:
            try:
                client.cancel_order(pos["stop_order_id"])
            except KabuStationError as e:
                print(f"⚠ {pos['ticker']}: 損切り注文のキャンセル失敗(手動確認が必要): {e}")
            storage.close_position(pos["id"], today, "target_hit")
            print(f"利確約定: {pos['ticker']}(損切り注文はキャンセル)")
        elif _is_expired_unfilled(pos["stop_order_id"]) and _is_expired_unfilled(pos["target_order_id"]):
            try:
                new_stop = client.send_cash_sell_stop_order(pos["ticker"], int(pos["filled_qty"]), pos["stop_price"])
                new_target = client.send_cash_sell_order(pos["ticker"], int(pos["filled_qty"]), pos["target_price"])
                new_stop_id, new_target_id = new_stop.get("OrderId"), new_target.get("OrderId")
                storage.mark_position_holding(pos["id"], int(pos["filled_qty"]), new_stop_id, new_target_id)
                storage.log_order(today, pos["ticker"], "SELL_STOP", int(pos["filled_qty"]), pos["stop_price"], new_stop_id, "SENT", note="前日分の失効により再発注")
                storage.log_order(today, pos["ticker"], "SELL_TARGET", int(pos["filled_qty"]), pos["target_price"], new_target_id, "SENT", note="前日分の失効により再発注")
                print(f"⚠ {pos['ticker']}: 決済注文が期限切れだったため再発注(損切り{pos['stop_price']:.0f}円/利確{pos['target_price']:.0f}円)")
            except KabuStationError as e:
                print(f"⚠⚠ {pos['ticker']}: 決済注文の再発注に失敗。ポジションが無防備な状態です。手動確認が必要: {e}")


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


def run(today: str | None = None, production: bool = False, base_url: str | None = None) -> None:
    """base_urlはテスト用(tests/mock_kabu_server.py)にAPIサーバーの向き先を差し替えるためのもの。
    通常は指定しない。
    """
    today = today or dt.date.today().isoformat()
    print(f"=== live_trading 実行開始 (date={today}, 環境={'本番' if production else '検証用'}) ===")

    client = KabuStationClient(production=production, base_url=base_url)
    client.authenticate()

    reconcile_entry_fills(client, today)
    reconcile_exit_fills(client, today)

    cash = client.get_cash_balance()
    positions = client.get_positions()
    holdings_value = sum(float(p.get("Valuation", 0)) for p in positions)
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
            value = float(p.get("Valuation", 0))
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
        print(f"⚠ 新規エントリーを停止(理由: {halt_reason})。既存ポジションの損切り・利確注文はそのまま有効。")
        return

    industry_position_count: dict[str, int] = {}
    for ticker in held_tickers:
        industry = industry_map.get(ticker)
        if industry:
            industry_position_count[industry] = industry_position_count.get(industry, 0) + 1

    shortlist = daily_metrics[daily_metrics["is_shortlisted"] == 1]
    print(f"本日の注目銘柄: {len(shortlist)}銘柄")

    supervised_tickers = get_supervised_tickers()
    if supervised_tickers:
        print(f"監理・整理銘柄(新規エントリー対象外): {len(supervised_tickers)}銘柄")

    # 実口座の保有(held_tickers)だけでなく、まだ約定確認前(entry_pending)・決済待ち(holding)の
    # 自分の未決済ポジションも重複エントリー防止の対象にする。実口座残高だけを見ていると、
    # 同じ日にrun_daily.pyを2回実行した場合(手動リトライ・誤操作等)、1回目の買い注文が
    # まだ約定していない間に2回目が同じ銘柄へまた買い注文を出してしまう(2026-08-14、
    # 外部資料の指摘で気づいた実在のリスク)。
    pending_or_holding_tickers = set(storage.read_open_positions()["ticker"])

    # リスクベースのサイジング(rm.position_size_shares)は口座評価額(account_value、
    # 保有株の含み評価額も含む)を基準にするが、実際に新規注文へ使える予算は現金(cash)のみ。
    # この現金予算は1回の実行で複数銘柄にエントリーするたびに減っていくはずなのに、
    # 元のコードはaccount_valueを固定のまま使い回しており、2件目以降のサイジングが
    # 「まだ満額残っている」前提のままになっていた(2026-08-14、外部資料の指摘で発見した
    # 実在のバグ)。remaining_cashで実際に使える予算を追跡し、発注のたびに差し引く。
    remaining_cash = cash

    for _, row in shortlist.iterrows():
        ticker = str(row["ticker"])
        if ticker in held_tickers or ticker in pending_or_holding_tickers:
            continue

        if ticker in supervised_tickers:
            print(f"見送り: {ticker}(監理・整理銘柄に指定されているため)")
            continue

        industry = industry_map.get(ticker)
        if industry and industry_position_count.get(industry, 0) >= MAX_POSITIONS_PER_INDUSTRY:
            continue

        atr = row.get("atr14")
        entry_price = row.get("close")
        if atr is None or entry_price is None or atr <= 0 or entry_price <= 0:
            continue

        stop = rm.stop_loss_price(entry_price, atr)
        target = rm.take_profit_price(entry_price, stop)
        shares = rm.position_size_shares(account_value, RISK_PCT, entry_price, stop, LOT_SIZE)
        # リスクベースの株数を、その時点で実際に残っている現金予算でさらに制限する
        affordable_shares = int(remaining_cash // entry_price // LOT_SIZE) * LOT_SIZE
        shares = min(shares, affordable_shares)
        if shares <= 0:
            continue

        try:
            order = client.send_cash_buy_order(ticker, shares, entry_price)
            order_id = order.get("OrderId")
            storage.log_order(today, ticker, "BUY", shares, entry_price, order_id, "SENT")
            storage.open_position(ticker, today, order_id, shares, entry_price, stop, target)
            remaining_cash -= shares * entry_price
            print(
                f"発注: 買い {ticker} {shares}株 @ {entry_price}円(注文ID: {order_id}、"
                f"損切り予定{stop:.0f}円/利確予定{target:.0f}円、残り予算{remaining_cash:,.0f}円)"
            )
            if industry:
                industry_position_count[industry] = industry_position_count.get(industry, 0) + 1
        except KabuStationError as e:
            storage.log_order(today, ticker, "BUY", shares, entry_price, None, "ERROR", note=str(e))
            print(f"⚠ 発注失敗 {ticker}: {e}")

    print("=== live_trading 実行完了 ===")


def run_safely(today: str | None = None, production: bool = False, base_url: str | None = None) -> None:
    """run()を例外から保護するラッパー。予期しないエラーで異常終了しても、
    その日の失敗をdaily_runsに記録してから再送出する(2026-08-14、外部資料の指摘を受けて追加。
    元のrun()は例外発生時にstorage.log_daily_run()を一度も呼ばずに落ちるため、後から
    「その日は何が起きて止まったのか」が記録に残らなかった)。
    """
    run_date = today or dt.date.today().isoformat()
    try:
        run(today=run_date, production=production, base_url=base_url)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ live_trading実行中に予期しないエラーで停止: {e}")
        try:
            peak_value = storage.peak_value_so_far()
            storage.log_daily_run(run_date, peak_value, peak_value, True, f"異常終了: {e}")
        except Exception:  # noqa: BLE001
            pass  # 記録自体に失敗しても、元の例外を握りつぶさず必ず再送出する
        raise


if __name__ == "__main__":
    run_safely(production=False)
