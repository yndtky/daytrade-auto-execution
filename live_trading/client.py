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
  - 以下のフィールド名・enum値は公式のOpenAPI仕様(kabucom/kabusapi リポジトリの
    reference/kabu_STATION_API.yaml)を直接取得して裏取り済み(2026-08-14): AccountType
    (2=一般/4=特定/12=法人)、FrontOrderType(10=成行/20=指値/30=逆指値)、DelivType/FundType、
    ReverseLimitOrderの構造(TriggerSec/TriggerPrice/UnderOver/AfterHitOrderType/
    AfterHitPrice)、/wallet/cashのStockAccountWallet、/positionsのValuation(評価金額)、
    /ordersのState/OrderState/CumQty。ただし実際のレスポンスで一度も確認していないため、
    型や値の細部(特にDetails配下の約定明細)は初回接続時に生のレスポンスで再確認すること
  - トークンの有効期限(1日単位か、kabuステーションアプリの再起動まで持つか等)も未確認。
    安全側に倒し、クライアントの呼び出しごとに新しいトークンを取得する設計にしている
  - kabuステーションAPIはOCO注文(利確・損切りのセット注文)に未対応(公式Issue #1119で
    「内部で検討中」と回答があるのみ、2026-08-14時点)。そのため利確・損切りは別々の注文として
    出し、どちらかが約定したらもう一方をキャンセルする「疑似OCO」をrun_daily.py側で実装している
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

# 公式OpenAPI仕様(kabu_STATION_API.yaml)で裏取り済み(2026-08-14)。
DELIV_TYPE_BUY = 2  # 買い注文時: お預り金
DELIV_TYPE_SELL = 0  # 売り注文時: 指定なし
FUND_TYPE_BUY = "AA"  # 買い注文時の資産区分(信用代用)。公式サンプルの値をそのまま採用
FUND_TYPE_SELL = "  "  # 売り注文時は半角スペース2文字(未指定)必須

ACCOUNT_TYPE_GENERAL = 2  # 一般口座
ACCOUNT_TYPE_SPECIFIC = 4  # 特定口座
ACCOUNT_TYPE_CORPORATE = 12  # 法人口座

FRONT_ORDER_TYPE_MARKET = 10  # 成行
FRONT_ORDER_TYPE_LIMIT = 20  # 指値
FRONT_ORDER_TYPE_STOP = 30  # 逆指値(ReverseLimitOrderの指定が別途必須)
EXPIRE_DAY_TODAY_ONLY = 0  # 当日限り

# 逆指値(ReverseLimitOrder)関連のenum値
TRIGGER_SEC_SYMBOL = 1  # トリガ銘柄: 発注銘柄自身の値段
UNDER_OVER_BELOW = 1  # トリガ価格「以下」になったら発動(損切りに使う: 下に抜けたら発動)
UNDER_OVER_ABOVE = 2  # トリガ価格「以上」になったら発動
AFTER_HIT_ORDER_TYPE_MARKET = 1  # 発動後: 成行
AFTER_HIT_ORDER_TYPE_LIMIT = 2  # 発動後: 指値

# 注文状態(/ordersのState・OrderState共通)。5(終了)はCumQty(約定数量)で
# 「約定済み」か「取消/失効/エラーで終わった」かを判別する(CumQty>0なら約定分あり)。
ORDER_STATE_FINISHED = 5


class KabuStationError(Exception):
    """APIパスワード未設定・HTTPエラー・通信エラーをまとめて表す。"""


def _get_api_password() -> str:
    password = os.environ.get("KABUSTATION_API_PASSWORD", "")
    if not password:
        raise KabuStationError("KABUSTATION_API_PASSWORD が未設定です(.envを確認してください)")
    return password


def _get_account_type() -> int:
    """口座種別(一般/特定/法人)。人によって異なるため.envで明示させる(誤ると全注文が拒否される)。"""
    raw = os.environ.get("KABUSTATION_ACCOUNT_TYPE", "")
    mapping = {"general": ACCOUNT_TYPE_GENERAL, "specific": ACCOUNT_TYPE_SPECIFIC, "corporate": ACCOUNT_TYPE_CORPORATE}
    if raw not in mapping:
        raise KabuStationError(
            "KABUSTATION_ACCOUNT_TYPE が未設定、または不正な値です(.envで "
            "'general'(一般口座)/'specific'(特定口座)/'corporate'(法人口座) のいずれかを指定してください)"
        )
    return mapping[raw]


class KabuStationClient:
    """kabuステーションAPIのクライアント。使う前に必ずkabuステーションアプリを起動しログインしておくこと。

    使い方:
        client = KabuStationClient(production=False)  # まずは検証用環境で
        client.authenticate()
        balance = client.get_cash_balance()
    """

    def __init__(self, production: bool = False, base_url: str | None = None):
        """base_urlはテスト用(mock_server.py)にAPIサーバーの向き先を差し替えるためのもの。
        通常は指定しない(production引数でlocalhostの本番/検証用ポートを自動選択する)。
        """
        port = PORT_PRODUCTION if production else PORT_VALIDATION
        self.base_url = base_url or f"http://localhost:{port}/kabusapi"
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
        """現物買付可能額(円)。公式OpenAPI仕様で確認済みのフィールド名(StockAccountWallet)。"""
        data = self._request("GET", "/wallet/cash")
        if "StockAccountWallet" not in data:
            raise KabuStationError(f"想定していたフィールド(StockAccountWallet)がレスポンスにありません: {data}")
        return float(data["StockAccountWallet"])

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        """保有中の建玉一覧。product=1で現物のみに絞る。addinfo=trueで各建玉に
        CurrentPrice(現在値)・Valuation(評価金額)・ProfitLoss(評価損益額)・
        ProfitLossRate(評価損益率)が付与される(公式OpenAPI仕様で確認済み)。
        """
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
            "AccountType": _get_account_type(),
            "Qty": quantity,
            "FrontOrderType": FRONT_ORDER_TYPE_LIMIT,
            "Price": price,
            "ExpireDay": EXPIRE_DAY_TODAY_ONLY,
        }
        return self._request("POST", "/sendorder", json=body)

    def send_cash_sell_order(self, symbol: str, quantity: int, price: float) -> dict:
        """現物の売り注文(指値・利確用)。保有株数を超える数量は取引所側で拒否される想定。"""
        body = {
            "Symbol": symbol,
            "Exchange": EXCHANGE_TOKYO,
            "SecurityType": SECURITY_TYPE_STOCK,
            "Side": SIDE_SELL,
            "CashMargin": CASH_MARGIN_CASH,
            "DelivType": DELIV_TYPE_SELL,
            "FundType": FUND_TYPE_SELL,
            "AccountType": _get_account_type(),
            "Qty": quantity,
            "FrontOrderType": FRONT_ORDER_TYPE_LIMIT,
            "Price": price,
            "ExpireDay": EXPIRE_DAY_TODAY_ONLY,
        }
        return self._request("POST", "/sendorder", json=body)

    def send_cash_sell_stop_order(self, symbol: str, quantity: int, trigger_price: float) -> dict:
        """現物の売り注文(逆指値・損切り用)。trigger_price以下になったら成行で即座に売る。

        kabuステーションAPIはOCO注文に対応していないため、利確(send_cash_sell_order)と
        この損切り注文は別々の注文として管理し、どちらかが約定したらもう一方をキャンセルする
        「疑似OCO」をrun_daily.py側で行う。AfterHitOrderTypeを成行にしているのは、暴落時に
        指値では約定しないリスクを避けるため(スリッページより約定確実性を優先)。
        """
        body = {
            "Symbol": symbol,
            "Exchange": EXCHANGE_TOKYO,
            "SecurityType": SECURITY_TYPE_STOCK,
            "Side": SIDE_SELL,
            "CashMargin": CASH_MARGIN_CASH,
            "DelivType": DELIV_TYPE_SELL,
            "FundType": FUND_TYPE_SELL,
            "AccountType": _get_account_type(),
            "Qty": quantity,
            "FrontOrderType": FRONT_ORDER_TYPE_STOP,
            "Price": 0,
            "ExpireDay": EXPIRE_DAY_TODAY_ONLY,
            "ReverseLimitOrder": {
                "TriggerSec": TRIGGER_SEC_SYMBOL,
                "TriggerPrice": trigger_price,
                "UnderOver": UNDER_OVER_BELOW,
                "AfterHitOrderType": AFTER_HIT_ORDER_TYPE_MARKET,
                "AfterHitPrice": 0,
            },
        }
        return self._request("POST", "/sendorder", json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("PUT", "/cancelorder", json={"OrderId": order_id})

    def get_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"product": 1}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/orders", params=params)
