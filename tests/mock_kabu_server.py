"""kabuステーションAPIを模した、テスト専用のスタブHTTPサーバー。

本物のkabuステーションAPI(検証用環境ポート18081)は常に固定のダミー値しか返さず、
「発注→約定→決済注文→約定→キャンセル」という一連の状態遷移を試すことができない。
このモックサーバーは実際のHTTP経由でclient.pyのリクエスト組み立て・レスポンス解析
コードを検証しつつ、テストスクリプト側が任意のタイミングで注文を「約定させる」ことが
できる(POST /_test/fill)ため、live_trading/run_daily.pyの疑似OCOライフサイクル
(entry_pending -> holding -> closed)をend-to-endで検証できる。

本番のkabuステーションAPIとの通信は一切行わない、完全にローカルなテスト用実装。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from live_trading.client import ORDER_STATE_FINISHED

ORDER_STATE_WAITING = 1


class _State:
    def __init__(self, cash: float):
        self.cash = cash
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}  # symbol -> {"Qty": int, "Valuation": float, "CurrentPrice": float}
        self._next_id = 1
        self.lock = threading.Lock()

    def new_order_id(self) -> str:
        oid = f"MOCK{self._next_id:06d}"
        self._next_id += 1
        return oid


class _Handler(BaseHTTPRequestHandler):
    state: _State  # クラス変数としてMockKabuServer.start()内でセットする

    def log_message(self, format, *args):
        pass  # テスト出力を静かにする(必要ならコメントアウトして有効化)

    def _send_json(self, status: int, body: dict | list) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()
        state = self.state

        if path == "/kabusapi/token":
            self._send_json(200, {"ResultCode": 0, "Token": "MOCK_TOKEN"})
            return

        if path == "/kabusapi/sendorder":
            with state.lock:
                oid = state.new_order_id()
                state.orders[oid] = {
                    "ID": oid,
                    "State": ORDER_STATE_WAITING,
                    "OrderState": ORDER_STATE_WAITING,
                    "CumQty": 0,
                    "OrderQty": body.get("Qty", 0),
                    "Symbol": body.get("Symbol"),
                    "Side": body.get("Side"),
                    "Price": body.get("Price"),
                    "ReverseLimitOrder": body.get("ReverseLimitOrder"),
                }
            self._send_json(200, {"Result": 0, "OrderId": oid})
            return

        if path == "/kabusapi/_test/fill":
            # テスト専用: 指定した注文を約定させ、保有ポジションを更新する(本物のAPIには存在しない)
            with state.lock:
                oid = body["OrderId"]
                order = state.orders.get(oid)
                if order is None:
                    self._send_json(404, {"error": f"unknown order {oid}"})
                    return
                qty = body.get("Qty", order["OrderQty"])
                order["State"] = ORDER_STATE_FINISHED
                order["OrderState"] = ORDER_STATE_FINISHED
                order["CumQty"] = qty

                symbol = order["Symbol"]
                price = order.get("Price") or (order.get("ReverseLimitOrder") or {}).get("AfterHitPrice") or 0
                pos = state.positions.setdefault(symbol, {"Qty": 0, "Valuation": 0.0, "CurrentPrice": price})
                if order["Side"] == "2":  # 買い
                    pos["Qty"] += qty
                else:  # 売り
                    pos["Qty"] -= qty
                pos["CurrentPrice"] = price or pos["CurrentPrice"]
                pos["Valuation"] = pos["Qty"] * pos["CurrentPrice"]
                if pos["Qty"] <= 0:
                    state.positions.pop(symbol, None)
            self._send_json(200, {"ok": True})
            return

        if path == "/kabusapi/_test/reset":
            with state.lock:
                state.orders.clear()
                state.positions.clear()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": f"unhandled path {path}"})

    def do_PUT(self):
        path = self.path.split("?")[0]
        body = self._read_json()
        state = self.state

        if path == "/kabusapi/cancelorder":
            with state.lock:
                oid = body["OrderId"]
                order = state.orders.get(oid)
                if order is None:
                    self._send_json(404, {"error": f"unknown order {oid}"})
                    return
                if order["State"] != ORDER_STATE_FINISHED:
                    order["State"] = ORDER_STATE_FINISHED
                    order["OrderState"] = ORDER_STATE_FINISHED
            self._send_json(200, {"Result": 0, "OrderId": oid})
            return

        self._send_json(404, {"error": f"unhandled path {path}"})

    def do_GET(self):
        path = self.path.split("?")[0]
        state = self.state

        if path == "/kabusapi/wallet/cash":
            self._send_json(200, {"StockAccountWallet": state.cash})
            return

        if path == "/kabusapi/positions":
            with state.lock:
                result = [
                    {"Symbol": symbol, **pos}
                    for symbol, pos in state.positions.items()
                ]
            self._send_json(200, result)
            return

        if path == "/kabusapi/orders":
            with state.lock:
                result = list(state.orders.values())
            self._send_json(200, result)
            return

        self._send_json(404, {"error": f"unhandled path {path}"})


class MockKabuServer:
    """使い方:
        server = MockKabuServer(cash=100000)
        server.start()
        client = KabuStationClient(base_url=server.base_url)
        ...
        server.stop()
    """

    def __init__(self, cash: float = 100_000.0, port: int = 0):
        self._state = _State(cash)
        handler = type("_BoundHandler", (_Handler,), {"state": self._state})
        self._httpd = HTTPServer(("127.0.0.1", port), handler)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/kabusapi"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def fill_order(self, order_id: str, qty: int | None = None) -> None:
        """テストスクリプトから注文を約定させる(通常のkabuステーションAPIには無い操作)。"""
        import requests

        body = {"OrderId": order_id}
        if qty is not None:
            body["Qty"] = qty
        resp = requests.post(f"{self.base_url}/_test/fill", json=body, timeout=5)
        if resp.status_code != 200:
            raise RuntimeError(f"fill_order({order_id})が失敗: {resp.status_code} {resp.text}")
