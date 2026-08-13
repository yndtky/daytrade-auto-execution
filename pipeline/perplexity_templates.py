"""Perplexity Proへ毎朝手動で質問するテンプレート一覧(注目銘柄向け)。

1銘柄ずつではなく、複数銘柄をまとめた1つのプロンプトとして生成する
(コピペする回数を減らすため)。回答を銘柄ごとに自動仕分けできるよう、
本文中の数字(日付・金額等)と衝突しにくい記号付きの区切り行
(">>>証券コード<<<")での回答を明示的に指示する。この区切りが
守られない場合に備え、parse_perplexity.split_by_tickerは
「証券コード: 内容」形式やコード単純出現へのフォールバックも持つ。
"""

CATEGORIES = [
    "決算内容・市場評価",
    "適時開示・重要ニュース",
    "出来高急増の背景",
    "アナリストのレーティング・目標株価変更",
    "【αの源泉】意外な人事(専門外の人材の重要ポスト起用など)",
    "【αの源泉】設備投資計画の縮小・延期・中止",
    "【αの源泉】経営陣・幹部の異動",
    "【αの源泉】特許出願・研究開発動向",
    "【αの源泉】大量保有報告書の変更・役員のインサイダー株式売買",
]

BATCH_SIZE = 25


def build_batch_prompt(entries: list[tuple[str, str]]) -> str:
    """entries: [(ticker, name), ...] のリストから、まとめて質問する1つのプロンプトを作る。"""
    ticker_list = "、".join(f"{name}({ticker})" for ticker, name in entries)
    categories = "\n".join(f"・{c}" for c in CATEGORIES)
    return (
        f"次の銘柄について、それぞれ以下の項目を教えてください。\n\n"
        f"【対象銘柄】{ticker_list}\n\n"
        f"【確認したい項目】\n{categories}\n\n"
        f"回答形式の指定: 各銘柄の回答の直前に、他の文字を一切含めず正確に\n"
        f">>>証券コード<<<\n"
        f"という行だけを書いてから内容を続けてください(例: >>>1234<<<)。"
        f"見出しや太字などの装飾は使わず、指定した記号だけで区切ってください。"
    )


def build_batches(shortlist_entries: list[tuple[str, str]], batch_size: int = BATCH_SIZE) -> list[list[tuple[str, str]]]:
    return [
        shortlist_entries[i : i + batch_size]
        for i in range(0, len(shortlist_entries), batch_size)
    ]
