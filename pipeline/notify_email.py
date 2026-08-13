"""Gmail SMTP経由で本日のショートリストを通知する。

事前準備:
1. 送信元Googleアカウントで2段階認証を有効化
2. https://myaccount.google.com/apppasswords でアプリパスワードを発行
3. プロジェクトルートに .env を作成し(.env.exampleを参照)、
   GMAIL_SENDER_ADDRESS / GMAIL_APP_PASSWORD / GMAIL_RECIPIENT_ADDRESS を設定
"""

from . import _net  # noqa: F401

import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _build_body(date: str, shortlist: pd.DataFrame) -> str:
    lines = [f"{date} 本日の注目銘柄: {len(shortlist)}銘柄", ""]
    for _, row in shortlist.iterrows():
        reasons = []
        if row.get("obv_divergence_flag"):
            reasons.append("OBVダイバージェンス")
        if row.get("golden_cross_flag"):
            reasons.append("ゴールデンクロス接近")
        if row.get("rsi_flag"):
            reasons.append("RSI過熱/売られすぎ")
        per = row.get("per")
        pbr = row.get("pbr")
        per_pbr = f"PER {per:.1f} / PBR {pbr:.2f}" if pd.notna(per) and pd.notna(pbr) else "PER/PBR取得不可"
        lines.append(f"- {row['ticker']} {row['name']} ({per_pbr}) [{', '.join(reasons)}]")
    lines.append("")
    lines.append("詳細はダッシュボードを確認してください。")
    return "\n".join(lines)


def send_shortlist_email(date: str, shortlist: pd.DataFrame) -> None:
    sender = os.environ["GMAIL_SENDER_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    friend = os.environ["GMAIL_RECIPIENT_ADDRESS"]

    # 送信者(あなた)にも同じ内容を送る。友人と同じアドレスなら重複させない
    recipients = [sender] if sender == friend else [sender, friend]

    msg = MIMEText(_build_body(date, shortlist), _charset="utf-8")
    msg["Subject"] = f"[デイトレ支援] {date} 本日の注目銘柄({len(shortlist)}銘柄)"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(sender, app_password)
        server.sendmail(sender, recipients, msg.as_string())


if __name__ == "__main__":
    import datetime as dt

    from storage.local_cache import read_daily_metrics

    today = dt.date.today().isoformat()
    df = read_daily_metrics(today)
    shortlist = df[df["is_shortlisted"] == 1]
    send_shortlist_email(today, shortlist)
    print(f"送信完了: {len(shortlist)}銘柄")
