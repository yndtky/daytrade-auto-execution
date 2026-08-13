"""ショートリスト銘柄のみ、finance.yahoo.co.jpからPER・PBR・決算発表情報をスクレイピングする。"""

from . import _net  # noqa: F401

import json
import re
import time

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REQUEST_PAUSE_SEC = 1.5


def _fetch_quote_page(ticker: str) -> str:
    url = f"https://finance.yahoo.co.jp/quote/{ticker}.T"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    return resp.text


def _extract_ratio(soup: BeautifulSoup, label: str, sub_label: str) -> float | None:
    """label(例:'PER')かつsub_label(例:'（会社予想）')が一致する dl の dd から数値を取り出す。"""
    for span in soup.find_all("span", class_=lambda c: c and "DataListItem__name" in c):
        if span.get_text(strip=True) != label:
            continue
        dt = span.find_parent("dt")
        if dt is None:
            continue
        sub = dt.find("span", class_=lambda c: c and "DataListItem__sub" in c)
        if sub is None or sub_label not in sub.get_text(strip=True):
            continue
        dl = dt.find_parent("dl")
        dd = dl.find("dd") if dl else None
        if dd is None:
            continue
        m = re.search(r"[\d,]+\.?\d*", dd.get_text(strip=True))
        if m:
            return float(m.group(0).replace(",", ""))
    return None


def _extract_press_release_schedule(html: str) -> dict:
    """Next.jsのRSCストリームに埋め込まれたpressReleaseScheduleオブジェクトを取り出す。"""
    idx = html.find("pressReleaseSchedule")
    if idx == -1:
        return {}
    brace_start = html.find("{", idx)
    if brace_start == -1:
        return {}
    depth, j = 0, brace_start
    while j < len(html):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    raw = html[brace_start : j + 1].replace('\\"', '"')
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def fetch_fundamentals(ticker: str) -> dict:
    """1銘柄のPER・PBR・決算発表情報を返す。取得失敗した項目はNone。"""
    html = _fetch_quote_page(ticker)
    soup = BeautifulSoup(html, "lxml")

    per = _extract_ratio(soup, "PER", "会社予想")
    pbr = _extract_ratio(soup, "PBR", "実績")
    schedule = _extract_press_release_schedule(html)

    return {
        "ticker": ticker,
        "per": per,
        "pbr": pbr,
        "earnings_info": schedule.get("pressReleaseScheduleMessage"),
    }


def fetch_fundamentals_bulk(tickers: list[str]) -> dict[str, dict]:
    result = {}
    for i, ticker in enumerate(tickers):
        try:
            result[ticker] = fetch_fundamentals(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"ファンダメンタル取得失敗 {ticker}: {e}")
        if i < len(tickers) - 1:
            time.sleep(REQUEST_PAUSE_SEC)
    return result


if __name__ == "__main__":
    for code in ["7203", "6758", "9984"]:
        print(fetch_fundamentals(code))
