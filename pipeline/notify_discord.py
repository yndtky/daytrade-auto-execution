"""Discord Webhook経由で本日のショートリストを通知する。

事前準備: 通知したいDiscordチャンネルでWebhookを発行し、.env の
DISCORD_WEBHOOK_URL に設定する(.env.exampleを参照)。
"""

from . import _net  # noqa: F401

import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MAX_LISTED = 20  # Discordの文字数制限(content最大2000字)に収めるための表示上限


def _build_message(date: str, shortlist: pd.DataFrame) -> str:
    lines = [f"**{date} 本日の注目銘柄: {len(shortlist)}銘柄**", ""]
    for _, row in shortlist.head(MAX_LISTED).iterrows():
        reasons = []
        if row.get("obv_divergence_flag"):
            reasons.append("OBV")
        if row.get("golden_cross_flag"):
            reasons.append("GC接近")
        if row.get("rsi_flag"):
            reasons.append("RSI")
        per, pbr = row.get("per"), row.get("pbr")
        per_pbr = f"PER{per:.1f}/PBR{pbr:.2f}" if pd.notna(per) and pd.notna(pbr) else "PER/PBR不可"
        lines.append(f"- {row['ticker']} {row['name']} ({per_pbr}) [{','.join(reasons)}]")

    remaining = len(shortlist) - MAX_LISTED
    if remaining > 0:
        lines.append(f"…他{remaining}銘柄。詳細はダッシュボードを確認してください。")
    return "\n".join(lines)


def send_shortlist_notification(date: str, shortlist: pd.DataFrame) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    content = _build_message(date, shortlist)
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    resp.raise_for_status()


MOMENTUM_MAX_LISTED = 10  # Discord通知は「狙い目」の速報用途のため、シートより件数を絞る


def _rank(momentum: pd.DataFrame) -> pd.DataFrame:
    """確度の高そうな順に並べる。優先順位: ゴールデンクロス成立済み > 買い優勢スコアの高さ。"""
    return momentum.sort_values(
        ["golden_cross_recent_flag", "buy_pressure_score"], ascending=[False, False]
    )


def _format_line(row: pd.Series) -> str:
    if row.get("golden_cross_recent_flag"):
        status = "クロス済み"
    elif row.get("golden_cross_flag"):
        status = "もうすぐクロス"
    else:
        status = "上昇転換"
    close = row.get("close")
    price = f"{close:,.0f}円" if pd.notna(close) else "株価不明"
    return f"- {row['ticker']} {row['name']} {price} 買い優勢{row['buy_pressure_score']:+.2f} [{status}]"


def _build_section(label: str, candidates: pd.DataFrame) -> list[str]:
    top = _rank(candidates).head(MOMENTUM_MAX_LISTED)
    lines = [f"【{label} 上位{len(top)}件】(候補{len(candidates)}銘柄中)"]
    if top.empty:
        lines.append("該当なし")
    else:
        lines.extend(_format_line(row) for _, row in top.iterrows())
    return lines


def _filter_by_industry_focus(momentum: pd.DataFrame, industry_focus: str) -> pd.DataFrame:
    """業種フォーカス設定に応じて絞り込む。「製造業」「製造業以外」は専用フラグで、
    それ以外はJPX33業種区分の個別名として完全一致で絞り込む(例: 情報・通信業)。
    """
    if industry_focus == "製造業":
        return momentum[momentum["is_manufacturing"].astype(bool)]
    if industry_focus == "製造業以外":
        return momentum[~momentum["is_manufacturing"].astype(bool)]
    return momentum[momentum["industry"] == industry_focus]


def _build_momentum_message(date: str, momentum: pd.DataFrame, industry_focus: str = "製造業") -> str:
    """「上昇転換+買い優勢の候補」を、全体の上位と業種フォーカスの上位に分けて通知文を作る。

    業種フォーカスは友人がGoogleスプレッドシートの「設定」タブで変更でき、
    変更後は次回の自動実行から通知内容に反映される(このスクリプトは実行のたびに設定を読み直すため)。
    株価は個人投資家である友人が「買える値段か」を一目で判断できるように併記している。
    ベータはスプレッドシート側の参考情報にとどめ、Discordでは省略(速報用途で情報過多を避けるため)。
    """
    lines = [f"**{date} 上昇転換+買い優勢の候補: 全{len(momentum)}銘柄**", ""]
    lines.extend(_build_section("全体", momentum))
    lines.append("")
    focused = _filter_by_industry_focus(momentum, industry_focus)
    lines.extend(_build_section(industry_focus, focused))
    lines.append("")
    lines.append("詳細は「上昇転換+買い優勢の候補」シートを確認してください。業種の絞り込みは「設定」シートで変更できます。")
    return "\n".join(lines)


def send_momentum_notification(date: str, momentum: pd.DataFrame, industry_focus: str = "製造業") -> None:
    if momentum.empty:
        return
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    content = _build_momentum_message(date, momentum, industry_focus)
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    resp.raise_for_status()


if __name__ == "__main__":
    import datetime as dt

    from storage.local_cache import read_daily_metrics

    today = dt.date.today().isoformat()
    df = read_daily_metrics(today)
    shortlist = df[df["is_shortlisted"] == 1]
    send_shortlist_notification(today, shortlist)
    print(f"送信完了: {len(shortlist)}銘柄")
