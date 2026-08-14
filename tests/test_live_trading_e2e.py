"""live_trading/run_daily.pyの疑似OCOライフサイクル全体を、モックAPIサーバー(HTTP経由)で
end-to-endに検証するテスト。

kabuステーションAPIの検証用環境(ポート18081)は常に固定値しか返さないため試せない
「発注→約定→決済注文→約定→キャンセル」の一連の流れを、tests/mock_kabu_server.pyに
対して実際にHTTPリクエストを送ることで確認する。client.py側のリクエスト組み立て・
レスポンス解析コードも(FakeClientによる単体テストとは違い)実際に通る。

実行方法:
    python -m tests.test_live_trading_e2e

対象の日付・銘柄コードはどちらも架空の値(date="9999-01-01", ticker="TESTX9")を使い、
実行後にstorage/local_cache.py・live_trading/storage.pyの両方から確実に削除するため、
本番データへの影響はない。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("KABUSTATION_API_PASSWORD", "dummy-for-mock-server")
os.environ.setdefault("KABUSTATION_ACCOUNT_TYPE", "specific")

import pandas as pd

from live_trading import run_daily, storage as live_storage
from storage import local_cache
from tests.mock_kabu_server import MockKabuServer

TEST_DATE = "9999-01-01"
TEST_TICKER = "TESTX9"


def seed_shortlist() -> None:
    df = pd.DataFrame([{
        "date": TEST_DATE,
        "ticker": TEST_TICKER,
        "name": "テスト銘柄",
        "industry": None,
        "close": 1000.0,
        "atr14": 50.0,
        "beta": 1.0,
        "is_shortlisted": 1,
    }])
    local_cache.upsert_daily_metrics(df)


def cleanup() -> None:
    with local_cache._connect() as conn:
        conn.execute("DELETE FROM daily_metrics WHERE ticker = ?", (TEST_TICKER,))
    with live_storage._connect() as conn:
        conn.execute("DELETE FROM open_positions WHERE ticker = ?", (TEST_TICKER,))
        conn.execute("DELETE FROM live_orders WHERE ticker = ?", (TEST_TICKER,))
        conn.execute("DELETE FROM daily_runs WHERE date = ?", (TEST_DATE,))


def _fetch_all_orders(server: MockKabuServer) -> list[dict]:
    import requests

    resp = requests.get(f"{server.base_url}/orders", timeout=5)
    resp.raise_for_status()
    return resp.json()


def get_position_row(position_id: int | None = None):
    """position_id未指定時は「最初に作られた行」を返す(1回目のエントリー直後専用)。
    2回目以降は必ずposition_idを指定すること(同日の再実行では、決済済みの後に
    再度ショートリスト対象として新規エントリーが走るのが正しい挙動のため)。
    """
    positions = live_storage.read_all_positions()
    rows = positions[positions["ticker"] == TEST_TICKER]
    if position_id is not None:
        rows = rows[rows["id"] == position_id]
        assert len(rows) == 1, f"id={position_id}の行が見つかりません: {rows}"
        return rows.iloc[0]
    assert len(rows) >= 1, f"ポジション行が見つかりません: {rows}"
    return rows.sort_values("id").iloc[0]


def main() -> None:
    cleanup()  # 前回の失敗テストの残骸があれば先に片付ける
    seed_shortlist()

    server = MockKabuServer(cash=1_000_000.0)
    server.start()
    print(f"モックサーバー起動: {server.base_url}")

    try:
        # --- 1回目の実行: 新規エントリー(買い注文)が出るはず ---
        run_daily.run(today=TEST_DATE, base_url=server.base_url)
        row = get_position_row()
        position_id = int(row["id"])
        assert row["status"] == "entry_pending", f"期待: entry_pending, 実際: {row['status']}"
        entry_order_id = row["entry_order_id"]
        print(f"[OK] 1回目実行: entry_pending、注文ID={entry_order_id}")

        # --- エントリー注文を約定させる ---
        server.fill_order(entry_order_id)

        # --- 2回目の実行: 約定を検知し、損切り・利確の両方を発注してholdingになるはず ---
        run_daily.run(today=TEST_DATE, base_url=server.base_url)
        row = get_position_row(position_id)
        assert row["status"] == "holding", f"期待: holding, 実際: {row['status']}"
        assert row["stop_order_id"] and row["target_order_id"], "損切り/利確注文IDが空"
        stop_order_id = row["stop_order_id"]
        target_order_id = row["target_order_id"]
        print(f"[OK] 2回目実行: holding、損切り注文={stop_order_id}、利確注文={target_order_id}")

        # --- 損切り注文を約定させる(利確注文はまだ生きている) ---
        server.fill_order(stop_order_id)

        # --- 3回目の実行: 損切り約定を検知し、利確注文をキャンセルしてclosedになるはず ---
        # (同日の再実行なので、決済済みの後にもう一度新規エントリーが走るのは正常な挙動。
        #  ここではposition_idで最初のポジションだけを追跡して確認する)
        run_daily.run(today=TEST_DATE, base_url=server.base_url)
        row = get_position_row(position_id)
        assert row["status"] == "closed", f"期待: closed, 実際: {row['status']}"
        assert row["close_reason"] == "stop_hit", f"期待: stop_hit, 実際: {row['close_reason']}"
        print(f"[OK] 3回目実行: closed(理由: {row['close_reason']})")

        # --- 利確注文が本当にキャンセルされたか(疑似OCO)も確認 ---
        orders = {o["ID"]: o for o in _fetch_all_orders(server)}
        target_order = orders.get(target_order_id)
        assert target_order is not None, f"利確注文が見つかりません: {target_order_id}"
        assert target_order["State"] == 5, f"利確注文はキャンセル(State=5)のはずが: {target_order['State']}"
        print("[OK] 利確注文が正しくキャンセルされている(疑似OCO)")

        print("\n=== end-to-endテストすべて成功 ===")
    finally:
        server.stop()
        cleanup()
        print("テストデータを片付けました")


if __name__ == "__main__":
    main()
