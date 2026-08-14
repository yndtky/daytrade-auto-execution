"""バックテスト実験の試行履歴を記録する。

IKEDAさん(Qiita: tikeda123)の「AI時代のバックテスト過学習入門」が指摘する通り、
「一番良く見えた結果を選ぶ」こと自体が選択バイアスになる。何パターン試して、それぞれ
どうだったかを後から追える記録を残しておかないと、PBO(Probability of Backtest
Overfitting)のような補正評価もできないし、無意識に「都合の良い記憶」だけが残ってしまう
(2026-08-14、外部資料の指摘を受けて追加)。

python -m backtest.walkforward を実行するたびに、設定と結果の要約を
data/experiment_log.csv に自動で追記する。過去(このファイル導入前)に行った主要な実験は、
セッションの記録から遡って手動で登録済み(backfilled=Trueの行)。
"""

import csv
import datetime as dt
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "experiment_log.csv"

FIELDNAMES = [
    "timestamp", "script", "config_summary", "method",
    "headline_metric", "verdict", "notes", "backfilled",
]


def log_experiment(
    script: str,
    config_summary: str,
    method: str,
    headline_metric: str,
    verdict: str,
    notes: str = "",
    backfilled: bool = False,
) -> None:
    """1回の実験結果を追記する。

    verdict: 'adopted'(採用) / 'rejected'(不採用) / 'inconclusive'(未決着) /
             'info'(自動記録、判断は別途) のいずれかを推奨するが強制はしない。
    """
    is_new = not LOG_PATH.exists()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "script": script,
            "config_summary": config_summary,
            "method": method,
            "headline_metric": headline_metric,
            "verdict": verdict,
            "notes": notes,
            "backfilled": backfilled,
        })
