"""バッチでまとめて聞いたPerplexityの回答を、銘柄コードを目印に自動で仕分ける。"""

import re


def _find_delimiter_position(text: str, ticker: str) -> int | None:
    """">>>証券コード<<<" 形式の区切り行を探す(最も優先)。"""
    pattern = r">>>\s*" + re.escape(ticker) + r"\s*<<<"
    m = re.search(pattern, text)
    return m.start() if m else None


def _find_colon_position(text: str, ticker: str) -> int | None:
    """「証券コード: 内容」形式(コード直後20文字以内に:/：)へのフォールバック。"""
    pattern = re.escape(ticker) + r".{0,20}?[:：]"
    m = re.search(pattern, text)
    return m.start() if m else None


def split_by_ticker(text: str, tickers: list[str]) -> dict[str, str]:
    """text中に現れる各tickerの位置を探し、次のtickerが現れるまでを その銘柄の回答とする。

    優先順: ">>>コード<<<" の区切り行 → 「コード: 内容」形式 → 単純な文字列出現位置。
    """
    positions: list[tuple[int, str]] = []
    for ticker in tickers:
        pos = _find_delimiter_position(text, ticker)
        if pos is None:
            pos = _find_colon_position(text, ticker)
        if pos is None:
            idx = text.find(ticker)
            pos = idx if idx != -1 else None
        if pos is not None:
            positions.append((pos, ticker))

    positions.sort(key=lambda p: p[0])

    result: dict[str, str] = {}
    for i, (pos, ticker) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        segment = text[pos:end].strip()
        if segment:
            result[ticker] = segment
    return result
