"""Discord Webhook経由で、その日のlive_trading運用成果を通知する。

友人向けダッシュボード(daytrade-dashboard)と同じDiscordチャンネルに投稿する運用を想定
(2026-08-24、ユーザーの希望)。同じWebhook URLを .env の DISCORD_WEBHOOK_URL に設定する。
1日に複数回(前場引け後・後場引け後など)呼ばれる想定で、呼ばれるたびに現在の状態を報告する
(pipeline.run_dailyのsession_labelパターンと同じ)。
"""

from pipeline import _net  # noqa: F401

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CLOSE_REASON_LABELS = {
    "entry_unfilled": "エントリー未約定",
    "stop_hit": "損切り",
    "target_hit": "利確",
    "manual_intervention_detected": "手動介入検知",
    "both_filled_anomaly": "異常(両方約定)",
}


def build_message(
    today: str,
    session_label: str,
    account_value: float,
    cash: float,
    holdings_value: float,
    previous_value: float | None,
    open_count: int,
    opened_today: list[str],
    closed_today: list[dict],
    halted: bool,
    halt_reason: str | None,
) -> str:
    change_line = ""
    if previous_value and previous_value > 0:
        change_pct = (account_value - previous_value) / previous_value * 100
        change_line = f"(前回記録比 {change_pct:+.2f}%)"

    lines = [
        f"**【自動売買】{today} {session_label}の運用成果**",
        f"口座評価額: {account_value:,.0f}円{change_line}",
        f"　現金: {cash:,.0f}円 / 保有株評価額: {holdings_value:,.0f}円",
        f"保有中ポジション: {open_count}件",
    ]
    if opened_today:
        lines.append(f"本日エントリー: {', '.join(opened_today)}")
    if closed_today:
        closed_strs = [
            f"{c['ticker']}({CLOSE_REASON_LABELS.get(c['close_reason'], c['close_reason'])})"
            for c in closed_today
        ]
        lines.append(f"本日決済: {', '.join(closed_strs)}")
    if halted:
        lines.append(f"⚠ 新規エントリー停止中(理由: {halt_reason})")
    return "\n".join(lines)


def send_performance_report(
    today: str,
    session_label: str,
    account_value: float,
    cash: float,
    holdings_value: float,
    previous_value: float | None,
    open_count: int,
    opened_today: list[str],
    closed_today: list[dict],
    halted: bool,
    halt_reason: str | None,
) -> None:
    """DISCORD_WEBHOOK_URL未設定の場合は何もせず終了する(取引ロジックはこれに依存しない)。"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("(DISCORD_WEBHOOK_URL未設定のため、運用成果の通知はスキップ)")
        return
    content = build_message(
        today, session_label, account_value, cash, holdings_value, previous_value,
        open_count, opened_today, closed_today, halted, halt_reason,
    )
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠ Discordへの運用成果通知に失敗(取引結果自体には影響なし): {e}")
