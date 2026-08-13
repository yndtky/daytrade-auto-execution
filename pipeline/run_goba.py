"""後場引け後の更新エントリーポイント(タスクスケジューラから15:40頃に呼ぶ)。

朝・前場と同じ計算を、後場引け(本日の確定値に近い)時点の株価で再実行し、
4つの軸シートを上書きする。Perplexityプロンプト・ウォッチリストのグラフは作り直さない。
"""

from .run_daily import run

if __name__ == "__main__":
    run(session_label="後場", run_extras=False)
