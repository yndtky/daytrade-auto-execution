"""同一銘柄のPerplexityノートを文章単位で比較し、前回になかった新しい内容を検出する。

AIによる意味理解は行わない(費用がかからない代わりに、単純な文字列一致でしか
「新しいかどうか」を判定できない)。皮肉・言い換え・要約の違いなどは検知できない。
"""

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？\n])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def find_new_sentences(new_text: str, previous_text: str | None) -> list[str]:
    """previous_textに無く、new_textにのみ含まれる文を返す。

    previous_textがNone(その銘柄で初めてのノート)の場合は、比較対象がないため
    空リストを返す(「初回」と「新規情報あり」を区別するため)。
    """
    if previous_text is None:
        return []
    previous_sentences = set(split_sentences(previous_text))
    return [s for s in split_sentences(new_text) if s not in previous_sentences]
