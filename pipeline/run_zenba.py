"""前場引け後(昼休み中)の更新エントリーポイント(タスクスケジューラから12:00頃に呼ぶ)。

朝と同じ計算を、前場引け時点で入手できる最新の株価を使って再実行し、
4つの軸シートを上書きする。Perplexityプロンプト・ウォッチリストのグラフは作り直さない。
"""

from .run_daily import run

if __name__ == "__main__":
    run(session_label="前場", run_extras=False)
