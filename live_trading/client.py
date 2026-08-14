"""kabuステーションAPI(三菱UFJ eスマート証券、旧auカブコム証券)への薄いREST APIクライアント。

paper_trading/client.py(Binance Testnet向け)と同じ考え方: SDKを使わず、認証・リクエスト・
エラー処理を自前で書く。実際に発注コードを書いて検証するのがこのフェーズの目的なので、
ラッパーの中に隠さない。

【重要な前提】kabuステーションAPIを使うには、まずWindows専用の「kabuステーション」アプリを
起動してログインしておく必要がある(このAPIはそのアプリのAPIサーバー機能に対して、同じPC上の
localhostから接続する設計。Binance Testnetのようなクラウド上のAPIサーバーではない)。
そのため、このクライアントは常にlocalhost(127.0.0.1)を叩く。

【本番用/検証用の切り替え】
  - 本番用: ポート18080。実際の残高・発注が行われる
  - 検証用: ポート18081。常に固定のダミー値を返す(実際の発注は行われない)。
    価格が固定値のため、戦略ロジックの妥当性は検証できないが、認証・リクエスト形式・
    レスポンス処理・エラー処理のコードが正しく動くかは検証できる(paper_tradingと同じ範囲)

【この時点(2026-08-14)でまだ実機確認できていないこと】
  - Professionalプラン未取得のため、実際にAPIへ接続してのテストは未実施
  - DelivType/FundType/AccountType等の注文パラメータは、公式サンプルコード
    (kabucom/kabusapi リポジトリ)の値をそのまま踏襲しているが、全区分の一覧は
    未確認。値の意味を変える前に、公式リファレンスで再確認すること
  - トークンの有効期限(1日単位か、kabuステーションアプリの再起動まで持つか等)も未確認。
    安全側に倒し、クライアントの呼び出しごとに新しいトークンを取得する設計にしている
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PORT_PRODUCTION = 18080
PORT_VALIDATION = 18081
REQUEST_TIMEOUT_SEC = 10

# 東証(現物取引で使う唯一の市場)。他の市場コードは今回のスコープ外。
EXCHANGE_TOKYO = 1
SECURITY_TYPE_STOCK = 1  # 株式

SIDE_SELL = "1"
SIDE_BUY = "2"

CASH_MARGIN_CASH = 1  # 現物取引(信用取引は対象外)

# 公式サンプル(kabucom/kabusapi: sample/Python/kabusapi_sendorder_cash_buy.py,
# kabusapi_sendorder_cash_sell.py)にある値をそのまま採用。値の意味の完全な一覧は
# 未確認のため、変更する場合は公式リファレンスで裏取りすること。
DELIV_TYPE_BUY = 2  # 買い注文時(お預り金からの支払いを指すと見られる)
DELIV_TYPE_SELL = 0  # 売り注文時
FUND_TYPE_BUY = "AA"  # 買い注文時の資産区分
FUND_TYPE_SELL = "  "  # 売り注文時は半角スペース2文字(未指定)
ACCOUNT_TYPE_SPECIFIC = 2  # 特定口座
FRONT_ORDER_TYPE_LIMIT = 30  # 指値
EXPIRE_DAY_TODAY_ONLY = 0  # 当日限り


class KabuStationError(Exception):
    """APIパスワード未設定・HTTPエラー・通信エラーをまとめて表す。"""


def _get_api_password() -> str:
    password = os.environ.get("KABUSTATION_API_PASSWORD", "")
    if not password:
        raise KabuStationError("KABUSTATION_API_PASSWORD が未設定です(.envを確認してください)")
    return password


class KabuStationClient:
    """kabuステーションAPIのクライアント。使う前に必ずkabuステーションアプリを起動しログインしておくこと。

    使い方:
        client = KabuStationClient(production=False)  # まずは検証用環境で
        client.authenticate()
        balance = client.get_cash_balance()
    """

    def __init__(self, production: bool = False):
        port = PORT_PRODUCTION if production else PORT_VALIDATION
        self.base_url = f"http://localhost:{port}/kabusapi"
        self.production = production
        self._token: str | None = None

    def authenticate(self) -> None:
        """APIパスワードでトークンを取得する。他のメソッドを呼ぶ前に必ず一度呼ぶこと。"""
        password = _get_api_password()
        try:
            resp = requests.post(
                f"{self.base_url}/token",
                json={"APIPassword": password},
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT_SEC,
            )
        except requests.exceptions.RequestException as e:
            raise KabuStationError(
                f"認証リクエストで通信エラー: {e}(kabuステーションアプリが起動・ログイン済みか確認してください)"
            ) from e

        if resp.status_code != 200:
            raise KabuStationError(f"認証に失敗しました({resp.status_code}): {resp.text}")

        data = resp.json()
        token = data.get("Token")
        if not token:
            raise KabuStationError(f"認証レスポンスにTokenが含まれていません: {data}")
        self._token = token

    def _headers(self) -> dict:
        if self._token is None:
            raise KabuStationError("authenticate() を先に呼んでください")
        return {"Content-Type": "application/json", "X-API-KEY": self._token}

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            resp = requests.request(
                method, f"{self.base_url}{path}", headers=self._headers(), timeout=REQUEST_TIMEOUT_SEC, **kwargs
            )
        except requests.exceptions.RequestException as e:
            raise KabuStationError(f"{method} {path} で通信エラー: {e}") from e

        if resp.status_code != 200:
            raise KabuStationError(f"{method} {path} が{resp.status_code}で失敗: {resp.text}")
        return resp.json()

    def get_cash_balance(self) -> float:
        """現物取引に使える余力(円)。

        レスポンスのフィールド名(CashAccountWallet)は未検証(公式サンプルはリクエストのみ確認済み、
        実際のレスポンス内容は未確認)。初回接続時に生のレスポンスを一度printして確認すること。
        """
        data = self._request("GET", "/wallet/cash")
        if "CashAccountWallet" not in data:
            raise KabuStationError(f"想定していたフィールド(CashAccountWallet)がレスポンスにありません: {data}")
        return float(data["CashAccountWallet"])

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        """保有中の建玉一覧。product=1で現物のみに絞る。"""
        params = {"product": 1, "addinfo": "true"}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/positions", params=params)

    def get_board(self, symbol: str) -> dict:
        """銘柄の気配・現在値情報。symbolは証券コードのみ(例: '7203')、内部で東証を付与する。"""
        return self._request("GET", f"/board/{symbol}@{EXCHANGE_TOKYO}")

    def send_cash_buy_order(self, symbol: str, quantity: int, price: float) -> dict:
        """現物の買い注文(指値)。数量は単元株数の倍数であること(呼び出し側で担保)。"""
        body = {
            "Symbol": symbol,
            "Exchange": EXCHANGE_TOKYO,
            "SecurityType": SECURITY_TYPE_STOCK,
            "Side": SIDE_BUY,
            "CashMargin": CASH_MARGIN_CASH,
            "DelivType": DELIV_TYPE_BUY,
            "FundType": FUND_TYPE_BUY,
            "AccountType": ACCOUNT_TYPE_SPECIFIC,
            "Qty": quantity,
            "FrontOrderType": FRONT_ORDER_TYPE_LIMIT,
            "Price": price,
            "ExpireDay": EXPIRE_DAY_TODAY_ONLY,
        }
        return self._request("POST", "/sendorder", json=body)

    def send_cash_sell_order(self, symbol: str, quantity: int, price: float) -> dict:
        """現物の売り注文(指値)。保有株数を超える数量は取引所側で拒否される想定。"""
        body = {
            "Symbol": symbol,
            "Exchange": EXCHANGE_TOKYO,
            "SecurityType": SECURITY_TYPE_STOCK,
            "Side": SIDE_SELL,
            "CashMargin": CASH_MARGIN_CASH,
            "DelivType": DELIV_TYPE_SELL,
            "FundType": FUND_TYPE_SELL,
            "AccountType": ACCOUNT_TYPE_SPECIFIC,
            "Qty": quantity,
            "FrontOrderType": FRONT_ORDER_TYPE_LIMIT,
            "Price": price,
            "ExpireDay": EXPIRE_DAY_TODAY_ONLY,
        }
        return self._request("POST", "/sendorder", json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("PUT", "/cancelorder", json={"OrderID": order_id})

    def get_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"product": 1}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/orders", params=params)
