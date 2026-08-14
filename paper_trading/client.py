"""Binance Spot Testnet(https://testnet.binance.vision)への薄いREST APIクライアント。

python-binanceのようなSDKを使わず、署名・リクエスト・エラー処理を自前で書いている。
これはこのフェーズの目的そのもの——「発注・エラー処理・再接続まわりの自前ロジックを
実際に動かして検証する」——のためで、SDKの内部に隠すと検証する意味が薄れるため。
実際のお金は一切動かないテスト専用環境(本番のBinance.comとは別のサーバー・別残高)。
"""

import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

from pipeline import _net  # noqa: F401  (TLS証明書の解決をpipeline側と同じ方法で行う)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://testnet.binance.vision/api"
REQUEST_TIMEOUT_SEC = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2


class BinanceTestnetError(Exception):
    """APIキー未設定・HTTPエラー・再試行しても解決しなかった通信エラーをまとめて表す。"""


def _get_credentials() -> tuple[str, str]:
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    if not api_key or not api_secret:
        raise BinanceTestnetError(
            "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET が未設定です(.envを確認してください)"
        )
    return api_key, api_secret


def _sign(params: dict, api_secret: str) -> dict:
    query = urlencode(params)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return {**params, "signature": signature}


def _request(method: str, path: str, params: dict | None = None, signed: bool = False) -> dict:
    """再接続テストを兼ねて、タイムアウト・一時的なHTTPエラーは指数バックオフで自前リトライする。

    リトライしても失敗した場合は例外を投げる(呼び出し側で「今回は見送り」として扱う想定。
    黙って諦めて不整合な状態のまま次に進むと、まさに検証したい「エラー処理バグ」になる)。
    """
    params = dict(params or {})
    headers = {}

    if signed:
        api_key, api_secret = _get_credentials()
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        params = _sign(params, api_secret)
        headers["X-MBX-APIKEY"] = api_key

    url = f"{BASE_URL}{path}"
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
            if resp.status_code == 200:
                return resp.json()
            # 4xx(注文内容の誤りなど)はリトライしても直らないので即座に失敗させる
            if 400 <= resp.status_code < 500:
                raise BinanceTestnetError(f"{method} {path} が{resp.status_code}で失敗: {resp.text}")
            last_error = BinanceTestnetError(f"{method} {path} が{resp.status_code}で失敗: {resp.text}")
        except requests.exceptions.RequestException as e:
            last_error = BinanceTestnetError(f"{method} {path} で通信エラー: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)

    raise last_error


def get_klines(symbol: str, interval: str = "5m", limit: int = 50) -> list[list]:
    """直近のローソク足を取得(移動平均計算用)。署名不要の公開エンドポイント。"""
    return _request("GET", "/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def get_price(symbol: str) -> float:
    data = _request("GET", "/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])


def get_open_orders(symbol: str) -> list[dict]:
    """現在の未約定注文一覧(署名必要)。新規発注前に必ず確認し、二重発注を防ぐ。"""
    return _request("GET", "/v3/openOrders", {"symbol": symbol}, signed=True)


def get_account_balance(asset: str) -> float:
    """指定資産の残高(利用可能分、署名必要)。"""
    data = _request("GET", "/v3/account", signed=True)
    for balance in data.get("balances", []):
        if balance["asset"] == asset:
            return float(balance["free"])
    return 0.0


def place_market_order(symbol: str, side: str, quantity: float) -> dict:
    """成行注文(署名必要)。side は 'BUY' または 'SELL'。"""
    return _request(
        "POST", "/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity}, signed=True
    )
