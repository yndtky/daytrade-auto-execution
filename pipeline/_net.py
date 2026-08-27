"""HTTPS通信を行う各モジュールの先頭でimportする。

このPC環境ではPython標準のCA証明書束(certifi)だとTLS検証が失敗する
(ローカルのセキュリティソフト等がTLSを検査し、Windowsの証明書ストアにのみ
信頼済みルート証明書を追加しているため)。
- truststoreでOSの証明書ストアをssl標準ライブラリ/requests経由の検証に使う
- yfinance(curl_cffi)はcertifiを見ずCURL_CA_BUNDLE/SSL_CERT_FILE環境変数の
  バンドルファイルを見るため、Windows証明書ストアをPEMに書き出したものを
  そこに指定する
どちらも証明書検証自体は維持したまま(検証を無効化するのではなく)解決する。
"""

import datetime as dt
import os

import truststore

from . import setup_ca_bundle

truststore.inject_into_ssl()

_BUNDLE = setup_ca_bundle.BUNDLE_PATH
# 2026-08-28: 14日経過した時点でセキュリティソフトの検査用証明書が更新されており、
# 古いバンドルではTLS検証に失敗する(yfinance/curl_cffi経由の通信が原因不明のまま
# 全滅し、「possibly delisted」という紛らわしいエラーで長時間気づけなかった)ことが
# 判明したため、30日から短縮。
_MAX_AGE_DAYS = 7

if not _BUNDLE.exists() or (
    dt.datetime.now() - dt.datetime.fromtimestamp(_BUNDLE.stat().st_mtime)
).days > _MAX_AGE_DAYS:
    setup_ca_bundle.build_bundle()

if _BUNDLE.exists():
    os.environ.setdefault("CURL_CA_BUNDLE", str(_BUNDLE))
    os.environ.setdefault("SSL_CERT_FILE", str(_BUNDLE))
