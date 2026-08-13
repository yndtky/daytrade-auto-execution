"""友人が触れるデータ(Shortlist / PerplexityNotes / TradeLog / TradedPicks)のGoogleスプレッドシート読み書き。

事前準備:
1. Google Cloudでサービスアカウントを作成し、Sheets APIを有効化
2. 認証キーJSONを data/credentials.json に保存
3. 対象スプレッドシートをサービスアカウントのメールアドレス(JSON内のclient_email)と共有(編集者権限)
4. .env に GOOGLE_SHEET_ID を設定(スプレッドシートURLのID部分)
"""

from pipeline import _net  # noqa: F401

import datetime as dt
import json
import os
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "data" / "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ダッシュボードの4つの軸(本日の注目銘柄/買い優勢(方向問わず)/上昇転換+買い優勢/下落からの回復期待)と
# 1対1で対応する専用シート。列構成はどのシートも同じ(最も詳しい列セット)に統一している
# (以前は軸ごとに列数が違い、シートによって情報が少なく見づらいという指摘を受けたため)。
UNIFIED_AXIS_HEADERS = [
    "date", "ticker", "name", "industry", "is_manufacturing", "close", "per", "pbr", "rsi14",
    "rsi_flag", "obv_divergence_flag", "buy_pressure_score",
    "golden_cross_flag", "golden_cross_recent_flag",
    "ytd_decline_pct", "sold_more_than_nikkei_flag", "range_position_52w",
    "is_nikkei225", "beta", "correlation",
    "atr14", "atr_pct", "bollinger_band_width_pct", "avg_volume_20d",
    "earnings_info",
]

SHORTLIST_SHEET_TITLE = "本日の注目銘柄"
SHORTLIST_SHEET_HEADERS = UNIFIED_AXIS_HEADERS

BUY_PRESSURE_SHEET_TITLE = "買い優勢(方向問わず)"
BUY_PRESSURE_SHEET_HEADERS = UNIFIED_AXIS_HEADERS

MOMENTUM_SHEET_TITLE = "上昇転換+買い優勢の候補"
MOMENTUM_SHEET_HEADERS = UNIFIED_AXIS_HEADERS

RECOVERY_SHEET_TITLE = "下落からの回復期待候補"
RECOVERY_SHEET_HEADERS = UNIFIED_AXIS_HEADERS

PERPLEXITY_PROMPTS_HEADERS = ["date", "batch_no", "tickers", "prompt"]
# new_content(前回との差分)は列を安全に追加する作業を保留中。現状の実シートは4列のまま。
PERPLEXITY_NOTES_HEADERS = ["ticker", "date", "note_text", "created_at"]
TRADE_LOG_HEADERS = ["trade_date", "ticker", "return_pct", "referenced_tool", "note", "created_at"]
TRADED_PICKS_HEADERS = ["flagged_date", "ticker", "name", "close_at_flag", "created_at"]

WATCHLIST_HEADERS = ["ticker"]
MAX_WATCHLIST_SIZE = 10
CHART_BLOCK_WIDTH = 6  # Date, Close, MA5, MA25 の4列 + 2列の空きでブロック間を区切る

USAGE_GUIDE_TITLE = "使い方ガイド"
USAGE_GUIDE_CONTENT = """■ このツールについて
このダッシュボードは、友人の裁量デイトレード判断を補強するための情報基盤です。自動売買シグナルではなく、「今日チェックする価値がある候補」を提示するだけで、最終判断は常に友人の裁量です。「本日の注目銘柄」という言葉も、空売り(ショート)とは無関係で、「買った方がいい/売った方がいい」という意味ではありません。

■ 毎日の流れ
7:00 朝の実行(前営業日までのデータで指標計算。本日の終値はまだ存在しないので使いません)。PerplexityPrompts・ウォッチリストのグラフもここで作成
7:10頃 Discordに通知、このスプレッドシートも更新完了
7:10〜9:00 ダッシュボードやこのシートを確認し、気になる銘柄をPerplexityでリサーチ
9:00 東証取引開始。友人が裁量で売買判断
12:00頃 前場引け後の再計算(前場の値段で4つの軸シートを上書き)
12:30 後場開始
15:40頃 後場引け後の再計算(本日の確定に近い値段で4つの軸シートを再度上書き)
16:00頃 PCが自動シャットダウン
取引後 実際に売買した銘柄があれば、ダッシュボードやTradeLogシートに記録
※PCの電源が入っていない時刻は実行がスキップされ、次に電源を入れた時点で自動的に追いついて実行されます

■ 各シートの見方
本日の注目銘柄: RSI過熱/売られすぎ・OBVダイバージェンス・ゴールデンクロス接近のうち2つ以上該当した銘柄
買い優勢(方向問わず): 値段が上昇中でも下落中でも、出来高の買い勢いが強い銘柄
上昇転換+買い優勢の候補: 買いが強く、かつ上昇トレンドへの転換期(ゴールデンクロス接近・直近クロス済み)にある銘柄
下落からの回復期待候補: 日経平均以上に売られ、自身の52週レンジで安値圏にあり、日経225採用銘柄でβが指数と連動しやすい範囲の銘柄
PerplexityPrompts: Perplexity Proに貼り付ける質問プロンプト(銘柄25件ずつのバッチ)
PerplexityNotes: Perplexityの回答を貼り付けて保存した履歴(銘柄別、上書きせず積み上げ)
TradeLog: 実際のトレード記録(日付・銘柄・リターン・ツールを参考にしたか)
TradedPicks: ダッシュボードで「売買済み」として記録した銘柄と、記録時点からの値動き
Watchlist: 個別にグラフを見たい銘柄コードを手入力(最大10件)
PriceCharts: Watchlistに登録した銘柄の値動き+移動平均線グラフ(自動生成)
設定: Discord通知の「業種別」セクションで注目する業種を指定(「値」列を編集)。「製造業」「製造業以外」、またはJPX33業種区分の個別名(例: 情報・通信業)を入力可能。変更は次回の自動実行(7:00/12:00/15:40頃)のDiscord通知から反映されます

■ 各カラム(列)の意味
以下は一般的な技術分析での「解釈の目安」であり、「この数値なら買い/売り」という指示ではありません。あくまで数値の意味を理解するための参考で、最終判断は友人の裁量です。相場状況によって当てはまらないことも多くあります。

close: 終値(円)。前営業日の終値(本日はまだ取引前のため)

per: PER(株価収益率)。株価が1株あたり利益の何倍かを示す。一般的に数値が低いほど「割安」、高いほど「割高」とされるが、業種により適正水準は大きく異なり、絶対的な基準はない

pbr: PBR(株価純資産倍率)。株価が1株あたり純資産の何倍かを示す。「1倍」が理論上の解散価値の目安としてよく引用されるが、成長性の高い企業は1倍を大きく超えるのが普通

rsi14: RSI(14日、0〜100の数値)。値段の行き過ぎ度を示す。一般的な目安は「70超=買われすぎ」「30未満=売られすぎ」だが、強いトレンド相場では70超のまま長く推移することもあり、この数値だけで売買判断すべきではない

rsi_flag: 上記RSIが70超または30未満に該当するかどうかのフラグ(True/False)

obv_divergence_flag: 値段が下落中でも出来高の勢い(OBV)がそこまで下がっていない状態かどうか。売り圧力が弱まっている=反発の可能性、という見方の目安

buy_pressure_score: 買い優勢スコア(数値)。値段の傾きに対してOBVの傾きがどれだけ強いかを表す。正の値が大きいほど「値段の割に買いが強い」、負の値が大きいほど「値段の割に売りが強い」ことを示す。ゼロ付近は方向感が弱いことを意味する。明確な閾値は設定していない(相対的な強弱の目安として使う)

golden_cross_flag: 短期(5日)移動平均線が中期(25日)移動平均線にまだ届いていないが、差が縮まってきている状態(接近中)

golden_cross_recent_flag: 直近数営業日以内に短期移動平均線が中期移動平均線を上に抜けた(ゴールデンクロス済み)状態

ytd_decline_pct: 今年の高値からの下落率(%)。マイナスが大きいほど、年初来の高値から大きく下がっていることを示す

range_position_52w: 直近52週の値幅の中で、現在値がどのあたりか(0%=52週安値、100%=52週高値)。低いほど「歴史的な安値圏に近い」ことを示す目安

beta(β値): 日経平均の値動きに対する感応度。1.0が日経平均と同程度、1.0超は日経平均より大きく動く傾向、1.0未満は動きが小さい傾向を示す

correlation(相関係数): 日経平均との連動の強さ(-1〜1)。1に近いほど日経平均と同じ方向に動きやすく、0に近いほど連動性が低い(β値が高くても相関が低い場合は「連動しているように見えて実は個別要因で動いている」ケースもあるので注意)

referenced_tool(TradeLog): このツールの情報を参考にして売買したかどうか(Y/N)。後からY/N別の勝率・平均リターンを比較し、ツールの有効性を検証するために使う

■ Perplexityリサーチの使い方
1. PerplexityPromptsタブ、またはダッシュボードの「銘柄別リサーチ」から、バッチのプロンプトをコピー
2. Perplexity Proに貼り付けて質問する
3. 返ってきた回答をそのまま、ダッシュボードの該当バッチの貼り付け欄にコピペする
4. 「自動仕分けして保存」を押すと、銘柄コードを目印に自動で仕分けされ、PerplexityNotesに保存される
(内容の重要度判定などはAIによる自動分析はしていません。人が読んで判断する材料です)

■ ダッシュボードの開き方
プロジェクトフォルダで下記を実行し、ブラウザで http://localhost:8501 を開く
.venv\\Scripts\\streamlit.exe run dashboard\\app.py
※ダッシュボードを見るだけなら日中いつでも安全。データ収集(pipeline.run_daily)の手動実行は取引時間中(9:00〜15:00)は避け、朝の自動実行に任せるのがおすすめ。"""


def write_usage_guide() -> None:
    """使い方ガイドを1シートにまとめて書き出す(データではなく説明文なので毎日の自動実行では呼ばない)。"""
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, USAGE_GUIDE_TITLE, ["内容"])
    ws.clear()
    rows = [[line] for line in USAGE_GUIDE_CONTENT.split("\n")]
    ws.update("A1", rows)


def _client() -> gspread.Client:
    """GOOGLE_CREDENTIALS_JSON環境変数(GitHub Actions/Streamlit CloudのSecrets経由)が
    あればそちらを優先し、無ければローカルのdata/credentials.jsonファイルを使う。
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


# _spreadsheet()・_get_or_create_worksheet()はどちらもAPIの「メタデータ取得」を伴う
# 呼び出し(open_by_key/spreadsheet.worksheet)であり、以前はSheets関数を呼ぶたびに
# 毎回叩いていたため、1回のボタン操作で読み取りクォータ(分あたり)にすぐ達していた。
# プロセス内でキャッシュして再利用することで、この無駄な再取得をなくす。
_spreadsheet_cache: gspread.Spreadsheet | None = None
_worksheet_cache: dict[str, gspread.Worksheet] = {}


def _spreadsheet() -> gspread.Spreadsheet:
    global _spreadsheet_cache
    if _spreadsheet_cache is None:
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        _spreadsheet_cache = _client().open_by_key(sheet_id)
    return _spreadsheet_cache


def _get_or_create_worksheet(spreadsheet, title: str, headers: list[str]):
    if title in _worksheet_cache:
        return _worksheet_cache[title]
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
    _worksheet_cache[title] = ws
    return ws


def _invalidate_worksheet_cache(title: str) -> None:
    _worksheet_cache.pop(title, None)


def _write_axis_sheet(title: str, headers: list[str], date: str, df: pd.DataFrame) -> None:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, title, headers)
    ws.clear()
    ws.append_row(headers)
    if df.empty:
        return
    rows = df[[c for c in headers if c != "date"]].copy()
    rows.insert(0, "date", date)
    rows = rows.fillna("").astype(str)
    ws.append_rows(rows.values.tolist())


def write_shortlist_sheet(date: str, df: pd.DataFrame) -> None:
    """「本日の注目銘柄」用シート。ダッシュボードの同名の表と同じ列構成。"""
    _write_axis_sheet(SHORTLIST_SHEET_TITLE, SHORTLIST_SHEET_HEADERS, date, df)


def write_buy_pressure_sheet(date: str, df: pd.DataFrame) -> None:
    """「買い優勢(方向問わず)」用シート。"""
    _write_axis_sheet(BUY_PRESSURE_SHEET_TITLE, BUY_PRESSURE_SHEET_HEADERS, date, df)


def write_momentum_sheet(date: str, df: pd.DataFrame) -> None:
    """「上昇転換+買い優勢の候補」用シート。"""
    _write_axis_sheet(MOMENTUM_SHEET_TITLE, MOMENTUM_SHEET_HEADERS, date, df)


def write_recovery_sheet(date: str, df: pd.DataFrame) -> None:
    """「下落からの回復期待候補」用シート。"""
    _write_axis_sheet(RECOVERY_SHEET_TITLE, RECOVERY_SHEET_HEADERS, date, df)


def write_perplexity_prompts(date: str, shortlist: pd.DataFrame) -> None:
    """本日の注目銘柄を10銘柄ずつのバッチにまとめ、コピペしやすい形でPerplexityPromptsタブに書き出す。

    Shortlistタブと同様に本日分で上書きする(前日分を残す必要はない)。
    """
    from pipeline.perplexity_templates import build_batch_prompt, build_batches

    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "PerplexityPrompts", PERPLEXITY_PROMPTS_HEADERS)
    ws.clear()
    ws.append_row(PERPLEXITY_PROMPTS_HEADERS)
    if shortlist.empty:
        return

    entries = list(zip(shortlist["ticker"], shortlist["name"]))
    rows = []
    for batch_no, batch in enumerate(build_batches(entries), start=1):
        tickers_str = "、".join(t for t, _ in batch)
        rows.append([date, batch_no, tickers_str, build_batch_prompt(batch)])
    ws.append_rows(rows)


def append_perplexity_note(ticker: str, date: str, note_text: str) -> None:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "PerplexityNotes", PERPLEXITY_NOTES_HEADERS)
    ws.append_row([ticker, date, note_text, dt.datetime.now().isoformat(timespec="seconds")])


def append_perplexity_notes_bulk(entries: list[tuple[str, str, str]]) -> None:
    """複数銘柄分のノートを1回のAPI呼び出しでまとめて追加する(entries: [(ticker, date, note_text), ...])。

    銘柄ごとにappend_perplexity_noteをループ呼び出しすると、バッチサイズが大きい場合に
    1クリックで読み取り/書き込みリクエストが積み重なりクォータに達するため、まとめて送る。
    """
    if not entries:
        return
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "PerplexityNotes", PERPLEXITY_NOTES_HEADERS)
    now = dt.datetime.now().isoformat(timespec="seconds")
    rows = [[ticker, date, note_text, now] for ticker, date, note_text in entries]
    ws.append_rows(rows)


def read_perplexity_notes(ticker: str | None = None) -> pd.DataFrame:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "PerplexityNotes", PERPLEXITY_NOTES_HEADERS)
    # numericise_ignore=["all"]: gspreadは"1518"のような数字に見える文字列を自動でintに
    # 変換してしまうため、銘柄コード比較がずれないよう文字列のまま取得する
    df = pd.DataFrame(ws.get_all_records(numericise_ignore=["all"]))
    if df.empty or ticker is None:
        return df
    return df[df["ticker"] == str(ticker)]


def append_trade_log(trade_date: str, ticker: str, return_pct: float, referenced_tool: str, note: str) -> None:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "TradeLog", TRADE_LOG_HEADERS)
    ws.append_row(
        [trade_date, ticker, return_pct, referenced_tool, note, dt.datetime.now().isoformat(timespec="seconds")]
    )


def read_trade_log() -> pd.DataFrame:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "TradeLog", TRADE_LOG_HEADERS)
    return pd.DataFrame(ws.get_all_records(numericise_ignore=["all"]))


def append_traded_pick(flagged_date: str, ticker: str, name: str, close_at_flag: float) -> None:
    """「本日の注目銘柄」のうち実際に売買した銘柄を記録する(同一日+銘柄の重複登録は防ぐ)。"""
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "TradedPicks", TRADED_PICKS_HEADERS)
    existing = pd.DataFrame(ws.get_all_records(numericise_ignore=["all"]))
    if not existing.empty:
        dup = (existing["flagged_date"] == flagged_date) & (existing["ticker"] == str(ticker))
        if dup.any():
            return
    ws.append_row(
        [flagged_date, ticker, name, close_at_flag, dt.datetime.now().isoformat(timespec="seconds")]
    )


def read_traded_picks() -> pd.DataFrame:
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "TradedPicks", TRADED_PICKS_HEADERS)
    return pd.DataFrame(ws.get_all_records(numericise_ignore=["all"]))


def read_watchlist() -> list[str]:
    """Watchlistタブ(あなたが手入力する銘柄コード、最大10件)を読む。"""
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, "Watchlist", WATCHLIST_HEADERS)
    values = ws.col_values(1)[1:]  # ヘッダー行を除く
    tickers = [v.strip() for v in values if v.strip()]
    return tickers[:MAX_WATCHLIST_SIZE]


SETTINGS_SHEET_TITLE = "設定"
SETTINGS_HEADERS = ["設定項目", "値", "説明"]
INDUSTRY_FOCUS_KEY = "業種フォーカス"
DEFAULT_INDUSTRY_FOCUS = "製造業"
INDUSTRY_FOCUS_DESCRIPTION = (
    "Discord通知の「業種別」セクションで表示する業種。"
    "「製造業」「製造業以外」、またはJPX33業種区分の個別名(例: 情報・通信業)を入力できます。"
    "この値を変更すると、次回の自動実行(7:00/12:00/15:40頃)のDiscord通知から反映されます。"
)


def _ensure_settings_sheet():
    ss = _spreadsheet()
    ws = _get_or_create_worksheet(ss, SETTINGS_SHEET_TITLE, SETTINGS_HEADERS)
    values = ws.get_all_records(numericise_ignore=["all"])
    if not any(v.get("設定項目") == INDUSTRY_FOCUS_KEY for v in values):
        ws.append_row([INDUSTRY_FOCUS_KEY, DEFAULT_INDUSTRY_FOCUS, INDUSTRY_FOCUS_DESCRIPTION])
    return ws


def read_industry_focus() -> str:
    """Discord通知の業種別セクションで使う対象業種を設定シートから読む。未設定時は「製造業」。"""
    try:
        ws = _ensure_settings_sheet()
        values = ws.get_all_records(numericise_ignore=["all"])
        for v in values:
            if v.get("設定項目") == INDUSTRY_FOCUS_KEY:
                focus = str(v.get("値", "")).strip()
                return focus or DEFAULT_INDUSTRY_FOCUS
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_INDUSTRY_FOCUS


def write_watchlist_charts(chart_data: dict[str, tuple[str, pd.DataFrame]]) -> None:
    """ウォッチリスト銘柄(最大10件)の価格+移動平均データとグラフをPriceChartsタブに書き出す。

    chart_data: {ticker: (name, df)}。dfは列 Date/Close/MA5/MA25 を持つこと。
    毎日タブを作り直す(既存のグラフオブジェクトも含めてクリーンな状態にするため)。
    """
    ss = _spreadsheet()
    try:
        ss.del_worksheet(ss.worksheet("PriceCharts"))
    except gspread.WorksheetNotFound:
        pass
    _invalidate_worksheet_cache("PriceCharts")

    if not chart_data:
        return

    n = len(chart_data)
    max_rows = max(len(df) for _, df in chart_data.values())
    ws = ss.add_worksheet(title="PriceCharts", rows=max_rows + 20, cols=CHART_BLOCK_WIDTH * n)
    sheet_id = ws.id

    chart_requests = []
    for i, (ticker, (name, df)) in enumerate(chart_data.items()):
        start_col = i * CHART_BLOCK_WIDTH  # 0-indexed
        data = df[["Date", "Close", "MA5", "MA25"]].astype(str).values.tolist()

        block = [[f"{ticker} {name}", "", "", ""], ["Date", "Close", "MA5", "MA25"], *data]
        start_a1 = gspread.utils.rowcol_to_a1(1, start_col + 1)
        end_a1 = gspread.utils.rowcol_to_a1(2 + len(data), start_col + 4)
        ws.update(f"{start_a1}:{end_a1}", block)

        header_row_idx = 1  # 0-indexed (Date/Close/MA5/MA25の行)
        last_row_idx = 2 + len(data)  # 0-indexed exclusive end
        chart_requests.append(
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": f"{ticker} {name}",
                            "basicChart": {
                                "chartType": "LINE",
                                "legendPosition": "BOTTOM_LEGEND",
                                "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
                                "domains": [
                                    {
                                        "domain": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": sheet_id,
                                                        "startRowIndex": header_row_idx,
                                                        "endRowIndex": last_row_idx,
                                                        "startColumnIndex": start_col,
                                                        "endColumnIndex": start_col + 1,
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                ],
                                "series": [
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": sheet_id,
                                                        "startRowIndex": header_row_idx,
                                                        "endRowIndex": last_row_idx,
                                                        "startColumnIndex": start_col + col_offset,
                                                        "endColumnIndex": start_col + col_offset + 1,
                                                    }
                                                ]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                    }
                                    for col_offset in (1, 2, 3)
                                ],
                                "headerCount": 1,
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": sheet_id,
                                    "rowIndex": last_row_idx + 1,
                                    "columnIndex": start_col,
                                },
                                "widthPixels": 480,
                                "heightPixels": 300,
                            }
                        },
                    }
                }
            }
        )

    ss.batch_update({"requests": chart_requests})
