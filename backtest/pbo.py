"""CSCV(Combinatorially Symmetric Cross-Validation)によるPBO(Probability of Backtest
Overfitting)の推定。

IKEDAさん(Qiita: tikeda123)が2026年5月に紹介した、Bailey/Borwein/López de Prado/Zhu(2014)の
手法。「複数のパラメータ候補を試して一番良かったものを選ぶ」という行為自体が選択バイアスに
なる、という問題意識に基づく。全期間を複数の等しいブロックに分割し、そのブロックを訓練用・
検証用に対称に(半分ずつ)分けるあらゆる組み合わせについて、

    1. 訓練期間で一番良かった候補(in-sampleの勝者)を選ぶ
    2. その候補が検証期間では全候補中どの順位に落ち着くかを見る

を繰り返し、「訓練期間の勝者が検証期間では中央値未満に沈む」割合をPBOとする。PBOが高いほど、
「一番良く見えた候補」が実際には偶然の産物である可能性が高い。0.5に近いほど「選んでも選ばな
くても同じ」、0に近いほど「選んだ候補は本当に安定して良い」ことを示す。
"""

import itertools

import numpy as np
import pandas as pd


def compute_pbo(returns: pd.DataFrame, n_blocks: int = 10) -> dict:
    """returns: 日付を行、パラメータ候補を列とする日次リターン行列。

    n_blocksは偶数にすること(訓練/検証を対称に半分ずつ選ぶため。既定10ブロックなら
    C(10,5)=252通りの分割を試す)。

    戻り値: {"pbo": PBO値(0〜1、高いほど過学習が疑わしい), "n_splits": 実際に使った分割数,
             "logits": 各分割で得られたλ値のリスト}
    """
    if n_blocks % 2 != 0:
        raise ValueError("n_blocksは偶数にしてください(訓練/検証を対称に分けるため)")
    if returns.shape[1] < 2:
        raise ValueError("PBOの計算には2つ以上の候補が必要です")

    returns = returns.dropna(how="any")
    n_days = len(returns)
    if n_days < n_blocks:
        raise ValueError(f"データ日数({n_days})がブロック数({n_blocks})より少ない")

    block_bounds = np.linspace(0, n_days, n_blocks + 1, dtype=int)
    blocks = [returns.iloc[block_bounds[i]:block_bounds[i + 1]] for i in range(n_blocks)]

    n_candidates = returns.shape[1]
    logits = []

    for train_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        test_idx = [i for i in range(n_blocks) if i not in train_idx]
        train = pd.concat([blocks[i] for i in train_idx])
        test = pd.concat([blocks[i] for i in test_idx])

        # 評価指標には単純な平均日次リターンを使う(標準偏差が極端に小さい候補で
        # Sharpe比が不安定・発散しがちなのを避けるため)。
        is_perf = train.mean()
        oos_perf = test.mean()

        best_candidate = is_perf.idxmax()

        # oos_perfを順位付け(1=最下位、n_candidates=最上位)し、in-sampleの勝者の
        # 相対順位ω(0〜1)からロジットλを計算する。λ<=0は「検証期間では中央値未満」を意味する。
        oos_rank = oos_perf.rank(method="average")
        omega = oos_rank[best_candidate] / (n_candidates + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)  # 0/1ちょうどだとlogitが発散するため丸める
        logits.append(float(np.log(omega / (1 - omega))))

    pbo = float(np.mean([1 if lam <= 0 else 0 for lam in logits]))
    return {"pbo": pbo, "n_splits": len(logits), "logits": logits}


def build_returns_matrix(time_returns_by_label: dict[str, dict]) -> pd.DataFrame:
    """{候補ラベル: backtraderのTimeReturnアナライザーの生データ(dict)} を、
    compute_pbo()に渡せる日付×候補の行列に変換する。候補ごとに存在する日付が
    異なる場合は、全候補に共通する日付だけを残す(欠損があるとブロック分割がずれるため)。
    """
    series = {label: pd.Series(tr) for label, tr in time_returns_by_label.items()}
    df = pd.DataFrame(series)
    df.index = pd.to_datetime(df.index)
    return df.sort_index().dropna(how="any")
