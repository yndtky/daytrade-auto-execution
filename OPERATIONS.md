# 運用ガイド(daytrade-auto-execution)

最終更新: 2026-08-14

このプロジェクトの「日頃の運用」「バックテストの回し方」「ペーパートレードの確認方法」をまとめた
リファレンスです。詳しい経緯や検証結果は、Claudeとの会話ログ(メモリ)にすべて記録されています。
ここでは「実際に何をすればいいか」だけに絞っています。

---

## 1. 全体の構成

| 部分 | 役割 | 自動/手動 |
|---|---|---|
| 日次シグナル計算(JP株) | プライム市場の指標を毎朝自動計算 | 完全自動(GitHub Actions) |
| ダッシュボード | 指標・バックテスト結果・ペーパートレード状況を閲覧 | 常時公開(Streamlit Cloud) |
| バックテスト | 過去データでの検証(手元PCで実行) | 手動 |
| ペーパートレード(Binance Testnet) | 発注・エラー処理の仕組み検証 | 自動(15分おき、要PC起動) |

**ダッシュボードURL**: https://daytrade-auto-execution-pnht2rzzpbxegtu9sfgary.streamlit.app/
**GitHubリポジトリ**: https://github.com/yndtky/daytrade-auto-execution (Public)

---

## 2. 日次シグナル計算(JP株)

**何もする必要はありません。** GitHub Actionsが毎朝7:00(JST)に自動実行し、結果はダッシュボードに
自動反映されます。

手動で今すぐ実行したい場合:
1. `github.com/yndtky/daytrade-auto-execution/actions/workflows/daily_pipeline.yml` を開く
2. 「Run workflow」をクリック

---

## 3. バックテストの実行方法

すべて手元のPCで、プロジェクトフォルダから実行します。

```powershell
cd "<プロジェクトのルート>\daytrade-auto-execution"
& "<プロジェクトのルート>\.venv\Scripts\python.exe" -m backtest.<スクリプト名> <オプション>
```

### 3-1. `backtest.run_backtest` — 通常のバックテスト(1回だけ)

一番よく使う、基本のバックテスト。

```bash
python -m backtest.run_backtest --tickers prime --max_tickers 300 --years 5 --capital 100000 --lot_size 1 --risk_pct 0.005
```

| オプション | 意味 | デフォルト |
|---|---|---|
| `--tickers` | 銘柄コード(カンマ区切り)、または `prime`(プライム市場全体)/`standard`(スタンダード市場)/`full`(プライム+スタンダード+グロース) | 例示用5銘柄 |
| `--max_tickers` | 対象銘柄数の上限(超える場合は固定シードでランダム抽出、比較検証がしやすいよう再現性あり) | 制限なし |
| `--years` | 遡る年数 | 5 |
| `--capital` | 初期資金(円) | 1,000,000 |
| `--lot_size` | 売買単位(株)。単元未満株(プチ株など)を想定するなら `1` | 100 |
| `--risk_pct` | 1トレードで許容するリスク(口座資金に対する割合)。小口座では `0.005`(0.5%)が今のところの検証結果が良い | 0.02(2%) |
| `--min_signals` | エントリーに必要なシグナル数(3条件中いくつ以上) | 2 |
| `--commission_pct` | 片道手数料率 | 0(ゼロ革命プラン想定) |
| `--slippage_pct` | 想定スリッページ率 | 0.001(0.1%)。プチ株を使う場合は `0.005`(0.5%)程度を推奨 |
| `--cash_injection` | 途中入金を試す。`'YYYY-MM-DD:金額,YYYY-MM-DD:金額'` の形式 | なし |
| `--max_positions_per_industry` | 同じ業種を同時に何ポジションまで持つか(業種分散) | 制限なし |

結果は `data/backtest_results/` にCSV・PNGとして保存され、**ダッシュボードの「バックテスト結果」
セクション**からブラウザで確認できます(直近の実行結果一覧から選択)。

### 3-2. `backtest.walkforward` — 過学習チェック(in-sample/out-of-sample検証)

「良い結果が出たが、たまたまではないか」を確認するための検証。**新しい設定を試すときは、必ずこちらで確認してから採用すること。**

```bash
python -m backtest.walkforward --tickers prime --max_tickers 300 --years 6 --oos_years 1 --capital 100000 --lot_size 1 --risk_pct 0.005
```

`run_backtest`と共通のオプションに加えて:

| オプション | 意味 |
|---|---|
| `--oos_years` | 末尾を「見ていない期間」として切り離す年数 |
| `--sweep` | ATR倍率・リスクリワード比の組み合わせをin-sampleだけで試し、最良の組み合わせをout-of-sampleで再検証 |
| `--rolling_folds N` | 全期間をN個の独立した期間に分割し、期間ごとの成績のばらつきを見る(**in-sample/out-of-sample分割より信頼性が高い、推奨**) |

**重要**: 1回のin-sample/out-of-sample分割だけでは、たまたま良い/悪い期間を見ただけの可能性がある
(2026-08-14に実際に誤った結論を出しかけた)。判断に迷ったら `--rolling_folds 3` 等で複数期間を
比較すること。

### 3-3. `backtest.signal_quality` — シグナル自体の期待値検証(大サンプル)

「資金制約を無視して、シグナルそのものに統計的な優位性があるか」を、銘柄ごとに独立した口座で
プールして検証する。トレード数が非常に多くなるため、統計的な信頼性が高い。

```bash
python -m backtest.signal_quality --tickers prime --max_tickers 300 --years 5
```

### 3-4. 現時点(2026-08-14)で一番信頼できる設定

```bash
python -m backtest.walkforward --tickers prime --max_tickers 300 --years 6 --rolling_folds 3 --capital 100000 --lot_size 1 --risk_pct 0.005 --slippage_pct 0.005
```

結果: プール方式(シグナル自体)は3期間すべてプラス(+0.53%〜+1.13%/トレード)で安定。ただし
実際の1口座(共有口座方式)は期間によって損失もあり得る(2020-2022期間はSQN -0.53)。
**「シグナルは本物だが、小口座では年単位で損失も普通にあり得る」という前提で運用を考えること。**

### 3-5. バックテストの既知の限界(生存者バイアス)

対象銘柄(`pipeline/universe.py`)は**現在のJPX上場銘柄一覧**から作っている。つまり、検証期間
(2020〜2026年)の途中で上場廃止・倒産・買収された銘柄は一切含まれておらず、**生き残った
銘柄だけで見た、実態よりやや楽観的な成績である可能性が高い**(2026-08-14、外部資料の指摘を
受けて調査)。

無料データ(yfinance)では、上場廃止銘柄の過去の株価データがほぼ入手できないため
(直近半年以内に廃止された銘柄でも取得成功率3割弱、2年以上前の廃止銘柄はほぼ全滅)、
過去に遡ってこの偏りを修正することは断念した。有料データベンダーを使わない限り解決できない、
無料データソース側の構造的な限界として認識しておくこと。

その代わりの前向きな対策として、`live_trading/run_daily.py`は**現在**JPXの監理銘柄・整理銘柄
(上場廃止が濃厚、または決定済みの銘柄)に指定されている銘柄への新規エントリーを自動的に
避ける(`pipeline.universe.get_supervised_tickers()`)。これは過去の偏りを直すものではなく、
今後同じ問題が実運用で起きるのを防ぐためのものである。

---

## 4. ペーパートレード(Binance Testnet)

発注・エラー処理・状態管理の「仕組み」を検証するフェーズ。実際のお金は一切動かない。

### 4-1. 仕組み

- 15分おきに、Windowsタスクスケジューラ(タスク名: `JPDayTrade_PaperTrading`)が
  `paper_trading/run_cycle_task.ps1` を実行
- シンプルな移動平均クロス戦略(BTCUSDT)で発注・決済を試す(戦略の質は問わない、仕組みの検証が目的)
- 結果は `data/paper_trading.db` に記録 → 自動でGitHubにコミット&プッシュ →
  ダッシュボードの「ペーパートレード」セクションで確認可能

**なぜGitHub Actionsではなく手元PCなのか**: GitHub Actionsの実行サーバー(主に米国)が、
Binanceの地域制限(HTTP 451)でブロックされるため。日本からのアクセスなら問題ない。
そのため、このフェーズだけはPCの起動が必要(本番運用とは別の、一時的な検証フェーズという位置づけ)。

**セットアップ(初回のみ)**: `paper_trading/run_cycle_task.ps1` は実際の絶対パスを含む
マシン固有ファイルのため、gitの管理対象から外しています。`run_cycle_task.ps1.example` を
コピーして `run_cycle_task.ps1` を作成し、自分の環境のパスを入力してください。

### 4-2. 状態の確認方法

```powershell
# タスクの状態確認
schtasks /query /tn "JPDayTrade_PaperTrading" /v /fo list

# 実行ログを見る
Get-Content "<プロジェクトのルート>\data\paper_trading_task.log" -Tail 30
```

またはダッシュボードの「ペーパートレード(Binance Testnet)」セクションで、実行サイクル数・
発注件数・直近ポジション・履歴を確認できます。

### 4-3. 停止・再開

```powershell
# 一時停止
schtasks /change /tn "JPDayTrade_PaperTrading" /disable

# 再開
schtasks /change /tn "JPDayTrade_PaperTrading" /enable

# 完全に削除
schtasks /delete /tn "JPDayTrade_PaperTrading" /f
```

### 4-4. 手動で1回だけ実行

```powershell
powershell -ExecutionPolicy Bypass -File "<プロジェクトのルート>\paper_trading\run_cycle_task.ps1"
```

---

## 5. トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| `SSL: CERTIFICATE_VERIFY_FAILED` | このPC特有の証明書問題。`pipeline/_net.py`をimportしているスクリプト経由なら自動で回避される。git自体のpushで出る場合は `git config --global http.sslBackend schannel` を実行 |
| Pythonが `python --version` で変な挙動をする | PATH上の`python`はWindowsAppsのダミーの可能性がある。実際に使うのは `.venv\Scripts\python.exe` |
| PowerShellスクリプトで日本語パス・文字列が文字化けする | .ps1ファイルがUTF-8 BOM無しで保存されている。BOM付きで保存し直す必要がある |
| `git push` が `rejected` になる | GitHub Actionsのボットが先にコミットしている可能性が高い。`git pull --rebase origin main` してから再度push |
| Binance Testnetで `401 Invalid API-key` | キーの再発行時に値がズレていないか確認。うまくいかなければキーを作り直す |
| Binance Testnetで `451 restricted location` | GitHub Actions(米国サーバー)からのアクセス。手元PCから実行する必要がある(セクション4参照) |

---

## 6. 用語集

- **プール方式(独立口座)**: 銘柄ごとに別々の口座があると仮定して集計する検証方法。シグナル自体の
  質を見るのに適するが、実際の1口座での成績とは異なる
- **共有口座方式**: 1つの口座ですべての銘柄を売買すると仮定する検証方法。実際の運用に近いが、
  資金が少ないとトレード数が減り、統計的信頼性が下がりやすい
- **SQN**: System Quality Number。トレードの安定性を示す指標。目安として2.0を超えるとむしろ
  「過学習/偶然では」と疑うべき、というのがIKEDAさんの指摘
- **in-sample / out-of-sample**: 「見た期間」と「見ていない期間」。out-of-sampleで崩れる結果は
  過去データへの当てはめ(過学習)の疑いが強い
- **ローリング検証(rolling_folds)**: 全期間を複数の独立した期間に分け、相場局面によって結果が
  大きく変わらないかを確認する方法。1回の分割検証より信頼性が高い
- **プチ株**: 単元未満株(1株単位)での売買。au株コム証券の「プチ株(S)」が想定サービス
