"""Windows証明書ストアの信頼済みルート/中間CA証明書をPEMバンドルとして書き出す。

このPCではセキュリティソフト等がTLS通信を検査しており、Windowsの証明書
ストアには信頼済みだがPython(certifi)のCA一覧には含まれないルート証明書が
追加されている。yfinance(curl_cffi)はcertifiではなくCURL_CA_BUNDLE/
SSL_CERT_FILE環境変数のバンドルを見るため、Windows証明書ストアの内容を
書き出してcertifiと合成したバンドルを作り、それを指す。
"""

import ssl
from pathlib import Path

import certifi

BUNDLE_PATH = Path(__file__).resolve().parent.parent / "data" / "windows_ca_bundle.pem"


def build_bundle() -> Path | None:
    if not hasattr(ssl, "enum_certificates"):
        return None  # Windows専用API。他OSでは不要(この問題自体が発生しない)

    der_certs = []
    for store in ("ROOT", "CA"):
        der_certs.extend(cert for cert, _, _ in ssl.enum_certificates(store))

    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BUNDLE_PATH, "w", encoding="ascii") as out:
        for der in der_certs:
            out.write(ssl.DER_cert_to_PEM_cert(der))
        with open(certifi.where(), "r", encoding="ascii") as f:
            out.write(f.read())
    return BUNDLE_PATH


if __name__ == "__main__":
    path = build_bundle()
    print(f"CA bundle written: {path} ({path.stat().st_size} bytes)")
