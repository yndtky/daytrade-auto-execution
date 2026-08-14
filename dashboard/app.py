"""日本株デイトレ支援ダッシュボード。

- 本日の注目銘柄(指標)表示、価格帯フィルター
- 銘柄別Perplexityノート欄(履歴保存)、売買済みチェック
- 経過観察(売買済み銘柄の現在値との差分を自動追跡)
- トレード記録タグ付けフォーム + タグ別集計

Perplexityノート・トレード記録・売買済み記録はGoogleスプレッドシートに保存する(storage/sheets.py)。
未セットアップの場合はその旨を表示し、指標表示自体は問題なく使えるようにする。
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fetch_prices import fetch_ohlcv  # noqa: E402
from pipeline.indicators import GOLDEN_CROSS_LONG_MA, GOLDEN_CROSS_SHORT_MA, compute_atr, compute_obv  # noqa: E402
from pipeline.parse_perplexity import split_by_ticker  # noqa: E402
from pipeline.perplexity_templates import BATCH_SIZE, build_batch_prompt, build_batches  # noqa: E402
from pipeline.risk_management import (  # noqa: E402
    ATR_STOP_MULTIPLIER,
    DEFAULT_RISK_PCT_PER_TRADE,
    RISK_REWARD_RATIO,
    compute_risk_plan,
)
from pipeline.screen import ATTENTION_FLAGS, BETA_MAX, BETA_MIN, RANGE_52W_RECOVERY_THRESHOLD  # noqa: E402
from storage.local_cache import latest_date, read_daily_metrics  # noqa: E402

st.set_page_config(page_title="日本株デイトレ支援ダッシュボード", layout="wide")
st.title("日本株デイトレ支援ダッシュボード")

# Streamlit Cloud上ではSecretsはst.secrets経由でしか読めないため、
# 環境変数ベースの既存コード(storage/sheets.py等)がそのまま動くようos.environへ橋渡しする。
# ローカルPCではsecrets.tomlが無いため何も起きず、.envの値がそのまま使われる。
try:
    import os as _os

    for _k, _v in st.secrets.items():
        _os.environ.setdefault(_k, str(_v))
except Exception:  # noqa: BLE001
    pass


def _sheets():
    """Googleシート連携モジュールを遅延import。認証情報が未セットアップならNoneを返す。"""
    import os

    from storage.sheets import CREDENTIALS_PATH

    has_creds = CREDENTIALS_PATH.exists() or bool(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    if not has_creds or not os.environ.get("GOOGLE_SHEET_ID"):
        return None
    try:
        from storage import sheets

        return sheets
    except Exception:  # noqa: BLE001
        return None


date = latest_date()
if date is None:
    st.warning("まだデータがありません。`python -m pipeline.run_daily` を実行してください。")
    st.stop()

df = read_daily_metrics(date)
st.caption(f"データ日付: {date} ／ 対象: プライム市場 {len(df)}銘柄")

flag_cols = ["rsi_flag", "obv_divergence_flag", "golden_cross_flag", "liquidity_ok"]
for c in flag_cols + ["golden_cross_recent_flag", "uptrend_turning_flag"]:
    df[c] = df[c].astype(bool)
df["is_shortlisted"] = df["is_shortlisted"].astype(bool)
df["is_momentum_pick"] = df["is_momentum_pick"].astype(bool)
df["is_recovery_candidate"] = df["is_recovery_candidate"].astype(bool)
df["is_nikkei225"] = df["is_nikkei225"].astype(bool)
df["sold_more_than_nikkei_flag"] = df["sold_more_than_nikkei_flag"].astype(bool)

df["buy_pressure_flag"] = df["buy_pressure_score"] > 0

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric(
    "RSI過熱/売られすぎ",
    int(df["rsi_flag"].sum()),
    help="RSI(14日)が70超(買われすぎ)または30未満(売られすぎ)。行き過ぎている状態で、反転(反落・反発)のリスクが高まっている可能性を示す警戒サイン。",
)
col2.metric(
    "OBVダイバージェンス",
    int(df["obv_divergence_flag"].sum()),
    help="値段は下がっているのに、出来高の勢い(OBV)はそこまで下がっていない状態。売り圧力が弱まってきている=そろそろ反発するかもしれない、という注目ポイント。「値段が下落中」の銘柄に限定した指標。",
)
col3.metric(
    "買い優勢(方向問わず)",
    int(df["buy_pressure_flag"].sum()),
    help="買い優勢スコアが正の銘柄数。OBVダイバージェンスと違い、値段が上昇中でも下落中でも、値段の動きに対して出来高の買い方向の勢いが強ければ該当する(方向を問わない広い指標)。",
)
col4.metric(
    "ゴールデンクロス接近",
    int(df["golden_cross_flag"].sum()),
    help="短期(5日)の値動きの平均線が、中期(25日)の平均線をまだ下回っているが、その差が縮まってきている状態。上昇トレンドに転じる可能性がある予兆。",
)
col5.metric(
    "本日の注目銘柄",
    int(df["is_shortlisted"].sum()),
    help="RSI・OBVダイバージェンス・ゴールデンクロス接近のうち2つ以上が同時に当てはまった銘柄の数(「買い」「売り」の推奨ではなく、チェックする価値がある候補という意味)。",
)

with st.expander("各指標の見方(はじめての方はこちら)", expanded=False):
    st.markdown(
        """
- **RSI過熱/売られすぎ**: 値段の行き過ぎ度を示す指標。買われすぎ・売られすぎのどちらも「反転しやすい」という意味で警戒サインとして扱う
- **OBVダイバージェンス**: 値段が下がっているのに出来高の勢いはそこまで下がっていない銘柄(下落中限定)。売りが弱まってきている=反発の兆候かもしれない
- **買い優勢(方向問わず)**: OBVダイバージェンスと同じ考え方だが、値段が下落中かどうかを問わない広い指標。上昇中の銘柄でも買いの勢いが強ければ該当する
- **ゴールデンクロス接近**: 短期的な値動きが、これから中期トレンドを上に抜けそうな気配がある銘柄
- **本日の注目銘柄**: RSI・OBVダイバージェンス・ゴールデンクロス接近のうち2つ以上が重なった銘柄。あくまで「今日チェックする価値がある」候補であり、買い・売りの指示ではありません
        """
    )

price_min_all = int(df["close"].min())
price_max_all = int(df["close"].max())
price_range = st.slider(
    "価格帯で絞り込み(円)",
    min_value=price_min_all,
    max_value=price_max_all,
    value=(price_min_all, price_max_all),
)
df_in_range = df[df["close"].between(price_range[0], price_range[1])]

INDUSTRY_OPTIONS = ["製造業", "製造業以外"] + sorted(df["industry"].dropna().unique().tolist())
industry_choice = st.multiselect(
    "業種で絞り込み(複数選択可・未選択なら全業種を表示)",
    INDUSTRY_OPTIONS,
    help="友人が業種を気にしていたため追加。「製造業」「製造業以外」はJPXの33業種区分(食料品〜その他製品)に基づく判定のショートカット。"
    "個別の業種名も選べます。複数選んだ場合はいずれかに該当する銘柄を表示",
)
if industry_choice:
    industry_mask = pd.Series(False, index=df_in_range.index)
    for choice in industry_choice:
        if choice == "製造業":
            industry_mask |= df_in_range["is_manufacturing"].astype(bool)
        elif choice == "製造業以外":
            industry_mask |= ~df_in_range["is_manufacturing"].astype(bool)
        else:
            industry_mask |= df_in_range["industry"] == choice
    df_in_range = df_in_range[industry_mask]

shortlist_threshold = st.select_slider(
    "本日の注目銘柄: いくつ以上ヒットで表示するか",
    options=[1, 2, 3],
    value=2,
    help="RSI過熱/売られすぎ・OBVダイバージェンス・ゴールデンクロス接近のうち、いくつ以上が同時に該当したら"
    "「注目銘柄」とするかをここで調整できます。これはダッシュボード表示のみの調整で、Googleシート・Discord通知・"
    "Perplexity用プロンプトの対象銘柄はこれまで通り2つ以上のままです。"
    "1にすると、これまでPER/PBR等のファンダメンタル情報を取得していない銘柄も含まれるため、その欄は空欄になることがあります。",
)
signal_count = df_in_range[ATTENTION_FLAGS].astype(bool).sum(axis=1)
shortlist = df_in_range[
    df_in_range["liquidity_ok"].astype(bool) & (signal_count >= shortlist_threshold)
].copy()
st.subheader(f"本日の注目銘柄({len(shortlist)}社)")
st.caption(f"流動性OKの銘柄のうち、OBVダイバージェンス・ゴールデンクロス接近・RSI過熱/売られすぎのうち{shortlist_threshold}つ以上が同時に成立した銘柄")

FLAG_COLUMN_CONFIG = {
    "rsi_flag": st.column_config.CheckboxColumn(
        "RSI過熱/売られすぎ", help="RSI(14日)が70超または30未満。行き過ぎによる反転リスクの警戒サイン"
    ),
    "obv_divergence_flag": st.column_config.CheckboxColumn(
        "OBVダイバージェンス", help="値段は下落中だが出来高の勢いはそこまで落ちていない。反発の兆候かもしれない"
    ),
    "golden_cross_flag": st.column_config.CheckboxColumn(
        "ゴールデンクロス接近", help="短期の値動きが中期トレンドを上に抜けそうな気配"
    ),
    "liquidity_ok": st.column_config.CheckboxColumn("流動性OK", help="直近20営業日の平均売買金額が一定額以上"),
}

SCORE_COLUMN_CONFIG = {
    "buy_pressure_score": st.column_config.NumberColumn(
        "買い優勢スコア",
        help="値段の傾きに対してOBV(出来高の勢い)の傾きがどれだけ強いかを表す数値。"
        "正で大きいほど、値段の割に買いが強い(値段の動きにOBVが追随・先行している)ことを示す。",
        format="%.2f",
    ),
}

# β・年初来下落率などは特定の軸(下落からの回復期待候補)専用ではなく、
# 一般的な参考情報として全ての表に共通で表示する
MARKET_COLS = ["ytd_decline_pct", "range_position_52w", "beta", "correlation"]
MARKET_COLUMN_CONFIG = {
    "ytd_decline_pct": st.column_config.NumberColumn(
        "年初来高値からの下落率(%)", help="今年に入ってからの高値からの下落率"
    ),
    "range_position_52w": st.column_config.NumberColumn(
        "52週レンジ位置(%)", help="0=52週安値、100=52週高値。低いほど歴史的な安値圏に近い(PBR/PER長期レンジの代替指標)"
    ),
    "beta": st.column_config.NumberColumn("β値", help="日経平均に対する感応度。1に近いほど指数と同程度動く"),
    "correlation": st.column_config.NumberColumn("相関係数", help="日経平均との相関の強さ(-1〜1)"),
}
MARKET_ROUND = {"ytd_decline_pct": 1, "range_position_52w": 1, "beta": 2, "correlation": 2}

if shortlist.empty:
    st.info("本日は注目銘柄がありません(価格帯フィルターの影響である場合もあります)。")
else:
    shortlist = shortlist.sort_values("rsi14", ascending=False)
    st.dataframe(
        shortlist[
            ["ticker", "name", "industry", "close", "per", "pbr"]
            + flag_cols[:-1]
            + ["buy_pressure_score", "rsi14"]
            + MARKET_COLS
            + ["earnings_info"]
        ].round(MARKET_ROUND),
        use_container_width=True,
        hide_index=True,
        column_config={**FLAG_COLUMN_CONFIG, **SCORE_COLUMN_CONFIG, **MARKET_COLUMN_CONFIG},
    )

momentum = df_in_range[df_in_range["is_momentum_pick"]].copy()
st.subheader(f"上昇転換+買い優勢の候補({len(momentum)}社)")
st.caption(
    "「本日の注目銘柄」とは別の軸: 買い優勢スコアが正(値段の割に買いが強い)、かつ"
    "ゴールデンクロスに接近中またはすでに直近でクロス済み(=上昇トレンドへの転換期)の銘柄。買い優勢スコアの高い順"
)
if momentum.empty:
    st.info("本日は該当する銘柄がありません(価格帯フィルターの影響である場合もあります)。")
else:
    momentum = momentum.sort_values("buy_pressure_score", ascending=False)
    st.dataframe(
        momentum[
            ["ticker", "name", "industry", "close", "buy_pressure_score", "golden_cross_flag", "golden_cross_recent_flag"]
            + MARKET_COLS
        ].round(MARKET_ROUND),
        use_container_width=True,
        hide_index=True,
        column_config={
            **SCORE_COLUMN_CONFIG,
            **MARKET_COLUMN_CONFIG,
            "golden_cross_flag": st.column_config.CheckboxColumn("接近中", help="ゴールデンクロスに接近中(まだ未クロス)"),
            "golden_cross_recent_flag": st.column_config.CheckboxColumn("クロス済み", help="直近数営業日以内にゴールデンクロス済み"),
        },
    )

buy_pressure = df_in_range[df_in_range["buy_pressure_flag"]].copy()
st.subheader(f"買い優勢銘柄(値段の方向を問わない)({len(buy_pressure)}社)")
st.caption(
    "OBVダイバージェンス(値段下落中限定)を、値段の方向を問わない形に広げたもの。"
    "買い優勢スコアが正の銘柄をすべて表示(ゴールデンクロス等の条件は問わない)。買い優勢スコアの高い順"
)
if buy_pressure.empty:
    st.info("本日は該当する銘柄がありません(価格帯フィルターの影響である場合もあります)。")
else:
    buy_pressure = buy_pressure.sort_values("buy_pressure_score", ascending=False)
    st.dataframe(
        buy_pressure[["ticker", "name", "industry", "close", "buy_pressure_score", "obv_divergence_flag"]].round(
            {"buy_pressure_score": 2}
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            **SCORE_COLUMN_CONFIG,
            "obv_divergence_flag": st.column_config.CheckboxColumn(
                "うち値段下落中(OBVダイバージェンス)", help="この中でも値段が下落中の銘柄はここにチェックが付く"
            ),
        },
    )

recovery = df_in_range[df_in_range["is_recovery_candidate"]].copy()
st.subheader(f"下落からの回復期待候補({len(recovery)}社)")
st.caption(
    f"日経平均以上に売られ、自身の52週レンジで下位{RANGE_52W_RECOVERY_THRESHOLD}%以内(安値に近い)にあり、"
    f"日経225採用銘柄でβが{BETA_MIN}〜{BETA_MAX}(指数と連動しやすい)の銘柄。"
    f"指数要因での売られすぎなら地合い改善で戻りやすいという考え方。52週レンジ下位の順"
)
if recovery.empty:
    st.info("本日は該当する銘柄がありません(価格帯フィルターの影響である場合もあります)。")
else:
    recovery = recovery.sort_values("range_position_52w", ascending=True)
    st.dataframe(
        recovery[["ticker", "name", "industry", "close"] + MARKET_COLS].round(MARKET_ROUND),
        use_container_width=True,
        hide_index=True,
        column_config=MARKET_COLUMN_CONFIG,
    )

st.subheader("銘柄チャート")
chart_candidates = sorted(
    set(shortlist["ticker"]) | set(momentum["ticker"]) | set(recovery["ticker"]) | set(buy_pressure["ticker"])
)
chart_ticker = st.selectbox(
    "チャートを見る銘柄",
    ["(選択してください)"] + chart_candidates,
    help="本日の注目銘柄・上昇転換候補から選べます。それ以外の銘柄は下の入力欄にコードを入力してください。",
)
manual_ticker = st.text_input("または銘柄コードを直接入力(任意)")
target_ticker = manual_ticker.strip() if manual_ticker.strip() else (
    chart_ticker if chart_ticker != "(選択してください)" else None
)

if target_ticker:
    with st.spinner(f"{target_ticker} の価格データを取得中..."):
        chart_prices = fetch_ohlcv([target_ticker])
    if target_ticker not in chart_prices:
        st.error("価格データを取得できませんでした。銘柄コードを確認してください。")
    else:
        chart_df = chart_prices[target_ticker].copy()
        chart_df["MA5"] = chart_df["Close"].rolling(GOLDEN_CROSS_SHORT_MA).mean()
        chart_df["MA25"] = chart_df["Close"].rolling(GOLDEN_CROSS_LONG_MA).mean()
        chart_df["OBV"] = compute_obv(chart_df["Close"], chart_df["Volume"])

        name_lookup = df.set_index("ticker")["name"]
        display_name = name_lookup.get(target_ticker, target_ticker)
        st.caption(f"{display_name}({target_ticker}) 直近6ヶ月の値動き")
        st.line_chart(chart_df[["Close", "MA5", "MA25"]])
        st.caption("出来高の勢い(OBV) — 値段が下がってもOBVが下がっていなければ買いが強いサイン")
        st.line_chart(chart_df[["OBV"]])

        st.subheader("リスク管理計算機")
        st.caption(
            "ATRベースで損切り/利確価格・株数を計算します。あくまで計算上の目安であり、発注は行いません。"
            "口座資金・リスク許容度はご自身の状況に合わせて入力してください。"
        )
        atr_series = compute_atr(chart_df["High"], chart_df["Low"], chart_df["Close"])
        atr_latest = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None
        entry_price_default = float(chart_df["Close"].iloc[-1])

        if atr_latest is None:
            st.info("ATRを計算するにはデータが不足しています(直近のデータが少なすぎる可能性があります)。")
        else:
            rc1, rc2, rc3 = st.columns(3)
            account_capital = rc1.number_input("口座資金(円)", min_value=0, value=500_000, step=10_000)
            risk_pct_input = rc2.number_input(
                "1トレードで許容する損失(%)", min_value=0.1, max_value=10.0, value=DEFAULT_RISK_PCT_PER_TRADE * 100, step=0.1
            )
            entry_price = rc3.number_input("想定エントリー価格(円)", min_value=0.0, value=entry_price_default, step=1.0)

            plan = compute_risk_plan(
                account_capital=account_capital,
                entry_price=entry_price,
                atr=atr_latest,
                risk_pct=risk_pct_input / 100,
            )
            rc4, rc5, rc6, rc7 = st.columns(4)
            rc4.metric("損切り価格(目安)", f"{plan['stop_loss_price']:,.0f}円")
            rc5.metric("利確価格(目安)", f"{plan['take_profit_price']:,.0f}円")
            rc6.metric("株数(目安)", f"{plan['shares']:,}株")
            rc7.metric("想定最大損失", f"{plan['max_loss_jpy']:,.0f}円")
            st.caption(
                f"ATR(14日): {atr_latest:.1f}円 ／ 損切り幅はATRの{ATR_STOP_MULTIPLIER}倍、"
                f"利確幅は損切り幅の{RISK_REWARD_RATIO}倍という一般的な計算式に基づく目安です。"
                f"株数は単元株(100株単位)に丸めています。"
                + (
                    f" 建玉金額は口座資金の{plan['position_pct_of_capital']:.1f}%です。"
                    if plan["position_pct_of_capital"] is not None
                    else ""
                )
            )

with st.expander("全銘柄(プライム市場)を見る"):
    show_only_flagged = st.checkbox("何かフラグが立っている銘柄だけ表示", value=False)
    view = df_in_range.copy()
    if show_only_flagged:
        view = view[view[flag_cols[:-1]].any(axis=1)]
    view = view.sort_values("rsi14", ascending=False)
    st.dataframe(
        view[["ticker", "name", "close", "volume", "rsi14"] + flag_cols],
        use_container_width=True,
        hide_index=True,
        column_config=FLAG_COLUMN_CONFIG,
    )

sheets = _sheets()
sheets_ready = sheets is not None
if not sheets_ready:
    st.warning(
        "Googleシート連携が未設定です(`data/credentials.json` と `.env` の `GOOGLE_SHEET_ID` を用意してください)。"
        " Perplexityノート欄・トレード記録は設定後に使えます。"
    )

st.header("銘柄別リサーチ(Perplexity Pro)")
if shortlist.empty:
    st.caption("本日は注目銘柄がないため対象がありません。")
else:
    if sheets_ready:
        # 銘柄ごとに個別取得するとAPIレート制限にすぐ達するため、1回の呼び出しで
        # 全件取得してからメモリ上で銘柄別に絞り込む(Streamlitは操作ごとに
        # スクリプト全体を再実行するため、キャッシュも必須)
        @st.cache_data(ttl=60)
        def _all_perplexity_notes():
            return sheets.read_perplexity_notes()

        all_notes = _all_perplexity_notes()
    else:
        all_notes = pd.DataFrame()

    st.caption(f"{BATCH_SIZE}銘柄ずつまとめたプロンプトをPerplexity Proに貼り付け、返ってきた回答をそのまま下に貼り付けてください。銘柄コードを目印に自動で仕分けます。")
    entries = list(zip(shortlist["ticker"], shortlist["name"]))
    batches = build_batches(entries)

    for batch_no, batch in enumerate(batches, start=1):
        batch_tickers = [t for t, _ in batch]
        label = "、".join(batch_tickers)
        with st.expander(f"バッチ{batch_no}: {label}"):
            st.caption("① このプロンプトをPerplexity Proにコピペ")
            st.code(build_batch_prompt(batch), language=None)

            if sheets_ready:
                st.caption("② Perplexityの回答をそのまま貼り付け")
                batch_answer = st.text_area("回答を貼り付け", key=f"batch_answer_{batch_no}")
                if st.button("自動仕分けして保存", key=f"batch_save_{batch_no}"):
                    if batch_answer.strip():
                        parsed = split_by_ticker(batch_answer.strip(), batch_tickers)
                        try:
                            sheets.append_perplexity_notes_bulk(
                                [(ticker, date, segment) for ticker, segment in parsed.items()]
                            )
                            st.cache_data.clear()
                            missing = [t for t in batch_tickers if t not in parsed]
                            if missing:
                                st.warning(
                                    f"{len(parsed)}銘柄を仕分けました。次の銘柄は回答内に見つかりませんでした: {'、'.join(missing)}"
                                )
                            else:
                                st.success(f"{len(parsed)}銘柄すべてを仕分けて保存しました。")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"保存に失敗しました: {e}")
                    else:
                        st.warning("回答を貼り付けてから保存してください。")

    for _, row in shortlist.iterrows():
        ticker, name = row["ticker"], row["name"]
        with st.expander(f"{ticker} {name} のリサーチ履歴・売買記録"):
            if sheets_ready:
                notes = all_notes[all_notes["ticker"] == str(ticker)] if not all_notes.empty else all_notes

                if not notes.empty:
                    st.caption("これまでのリサーチ履歴(🆕は前回のノートに無かった新しい内容)")
                    for _, n in notes.sort_values("date", ascending=False).iterrows():
                        has_new = str(n.get("new_content", "")).strip() != ""
                        label = f"🆕 **{n['date']}**" if has_new else f"**{n['date']}**"
                        st.markdown(f"{label}: {n['note_text']}")
                        if has_new:
                            st.caption(f"新規: {n['new_content']}")
                else:
                    st.caption("まだリサーチ履歴がありません。")

                st.divider()
                traded_checked = st.checkbox("この銘柄を実際に売買した", key=f"traded_{ticker}")
                if st.button("売買済みとして記録", key=f"traded_btn_{ticker}"):
                    if traded_checked:
                        try:
                            sheets.append_traded_pick(date, ticker, name, float(row["close"]))
                            st.cache_data.clear()
                            st.success("記録しました。価格の変化は下の「経過観察」に表示されます。")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"保存に失敗しました: {e}")
                    else:
                        st.warning("チェックを入れてから記録してください。")

st.header("経過観察(売買した銘柄のその後)")
if sheets_ready:
    @st.cache_data(ttl=60)
    def _traded_picks():
        return sheets.read_traded_picks()

    try:
        picks = _traded_picks()
    except Exception as e:  # noqa: BLE001
        picks = pd.DataFrame()
        st.error(f"記録の取得に失敗しました: {e}")

    if picks.empty:
        st.caption("まだ「売買済みとして記録」した銘柄がありません。")
    else:
        picks = picks.copy()
        picks["close_at_flag"] = pd.to_numeric(picks["close_at_flag"], errors="coerce")
        latest_close_by_ticker = df.set_index("ticker")["close"]
        picks["current_close"] = picks["ticker"].map(latest_close_by_ticker)
        picks["change_pct"] = (
            (picks["current_close"] - picks["close_at_flag"]) / picks["close_at_flag"] * 100
        )
        st.dataframe(
            picks[["flagged_date", "ticker", "name", "close_at_flag", "current_close", "change_pct"]]
            .sort_values("flagged_date", ascending=False)
            .round({"close_at_flag": 1, "current_close": 1, "change_pct": 1}),
            use_container_width=True,
            hide_index=True,
        )
else:
    st.caption("Googleシート連携の設定後に経過観察が使えます。")

st.header("トレード記録")
if sheets_ready:
    ticker_options = shortlist["ticker"].tolist() if not shortlist.empty else []
    with st.form("trade_log_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        trade_date = c1.date_input("トレード日")
        ticker_input = c2.selectbox("銘柄コード", ticker_options + ["(その他/手入力)"])
        if ticker_input == "(その他/手入力)":
            ticker_input = c2.text_input("銘柄コードを入力")
        return_pct = c3.number_input("リターン(%)", step=0.1, format="%.1f")
        referenced = st.radio("このツールの情報を参考にしましたか?", ["Y", "N"], horizontal=True)
        note = st.text_input("メモ(任意)")
        submitted = st.form_submit_button("記録する")

    if submitted:
        try:
            sheets.append_trade_log(trade_date.isoformat(), ticker_input, return_pct, referenced, note)
            st.cache_data.clear()
            st.success("記録しました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存に失敗しました: {e}")

    st.subheader("タグ別集計")

    @st.cache_data(ttl=60)
    def _trade_log():
        return sheets.read_trade_log()

    try:
        log = _trade_log()
    except Exception as e:  # noqa: BLE001
        log = pd.DataFrame()
        st.error(f"トレード記録の取得に失敗しました: {e}")

    if log.empty:
        st.caption("まだトレード記録がありません。")
    else:
        log["return_pct"] = pd.to_numeric(log["return_pct"], errors="coerce")
        summary = log.groupby("referenced_tool")["return_pct"].agg(
            件数="count", 勝率=lambda s: (s > 0).mean() * 100, 平均リターン="mean"
        )
        st.dataframe(summary.round(1), use_container_width=True)
else:
    st.caption("Googleシート連携の設定後にトレード記録フォームが使えます。")

st.header("バックテスト結果")
st.caption(
    "`python -m backtest.run_backtest` や `python -m backtest.walkforward` の実行結果を一覧表示します。"
    "処理に時間がかかるため、バックテスト自体はコマンドラインで実行してください(このダッシュボードからは実行できません)。"
    "あくまで過去データ上のシミュレーションであり、将来の利益を保証するものではありません。"
)

BACKTEST_RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_results"
trade_files = sorted(BACKTEST_RESULTS_DIR.glob("*_trades.csv"), reverse=True) if BACKTEST_RESULTS_DIR.exists() else []

if not trade_files:
    st.caption("まだバックテスト結果がありません。")
else:
    run_labels = [f.name.removesuffix("_trades.csv") for f in trade_files]
    selected_label = st.selectbox("結果を選択(新しい順)", run_labels)
    base = BACKTEST_RESULTS_DIR / selected_label

    trades_df = pd.read_csv(f"{base}_trades.csv")
    equity_path = Path(f"{base}_equity.csv")

    total_trades = len(trades_df)
    if total_trades:
        win_rate = (trades_df["pnl_net_jpy"] > 0).mean() * 100
        total_pnl = trades_df["pnl_net_jpy"].sum()
        avg_pnl_pct = trades_df["pnl_pct"].mean()
    else:
        win_rate = total_pnl = avg_pnl_pct = 0.0

    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("トレード数", total_trades)
    bc2.metric("勝率", f"{win_rate:.1f}%")
    bc3.metric("損益合計", f"{total_pnl:,.0f}円")
    bc4.metric("平均リターン/トレード", f"{avg_pnl_pct:.1f}%")

    if equity_path.exists():
        equity_df = pd.read_csv(equity_path, index_col=0, parse_dates=True)
        st.caption("資産推移")
        st.line_chart(equity_df)

    if total_trades:
        st.caption("トレード履歴(新しい順)")
        st.dataframe(
            trades_df.sort_values("entry_date", ascending=False),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("この実行ではトレードが発生しませんでした。")

st.header("ペーパートレード(Binance Testnet)")
st.caption(
    "ロードマップのステップ3(発注・エラー処理・再接続まわりの仕組みの検証)。"
    "実際のお金は一切動いていません(Binance公式のテスト専用環境)。JP株のシグナルロジックとは無関係の、"
    "仕組み検証専用のシンプルな移動平均クロス戦略を使っています。GitHub Actionsが15分おきに自動実行します。"
)

try:
    from paper_trading import storage as paper_storage

    cycles = paper_storage.read_cycles(limit=200)
    orders = paper_storage.read_orders(limit=200)
    snapshots = paper_storage.read_account_snapshots(limit=200)
except Exception as e:  # noqa: BLE001
    cycles, orders, snapshots = None, None, None
    st.caption(f"ペーパートレードのデータをまだ読み込めません(未実行、または読み込みエラー: {e})。")

if cycles is not None:
    if cycles.empty:
        st.caption("まだ実行履歴がありません。GitHub Actionsの初回実行を待つか、手動実行してください。")
    else:
        pc1, pc2, pc3, pc4, pc5 = st.columns(5)
        pc1.metric("実行サイクル数", len(cycles))
        pc2.metric("発注件数", len(orders) if orders is not None else 0)
        error_count = cycles["error"].notna().sum()
        pc3.metric("エラー件数", int(error_count))
        latest = cycles.iloc[0]
        pc4.metric("直近ポジション", latest.get("position_state") or "-")
        if snapshots is not None and not snapshots.empty:
            latest_snapshot = snapshots.iloc[0]
            halted = bool(latest_snapshot.get("halted"))
            pc5.metric("サーキットブレーカー", "停止中" if halted else "平常")
            if halted:
                st.warning(f"新規BUYを停止中(理由: {latest_snapshot.get('halt_reason')})")
        else:
            pc5.metric("サーキットブレーカー", "-")

        st.caption("実行履歴(新しい順)")
        st.dataframe(
            cycles[["timestamp", "symbol", "action", "price", "ma_short", "ma_long", "position_state", "note", "error"]],
            use_container_width=True,
            hide_index=True,
        )

        if orders is not None and not orders.empty:
            st.caption("発注履歴(新しい順)")
            st.dataframe(
                orders[["timestamp", "symbol", "side", "quantity", "order_id", "status"]],
                use_container_width=True,
                hide_index=True,
            )
