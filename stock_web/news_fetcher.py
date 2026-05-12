"""Fetch MOPS material announcements (重大訊息) for Taiwan stocks.

Step 1 of the news/sentiment feature: pull raw 重訊 from
公開資訊觀測站, cache by trading day. No LLM summarization yet — that
comes in step 2.

The MOPS ajax endpoint returns a whole month of announcements per stock
in a single query, so we issue one request per (code, calendar-month)
pair and filter to the recent N days client-side.

Notes:
  - Use `mopsov.twse.com.tw`, not `mops.twse.com.tw` — the latter
    rejects programmatic POSTs with a "頁面無法執行" security page.
  - Year is ROC (民國) format: 2026 → 115. Month is 1-12, no zero pad.
  - 主旨 cells contain <pre>/<font> wrappers and &nbsp; padding that
    must be stripped.

Cache file: stock_web/cache/news_{code}_{YYYYMMDD}.json
Auto-purged by `_purge_old_caches` in app.py after CACHE_RETENTION_DAYS.

Usage:
    python3 -m stock_web.news_fetcher 2330
    python3 -m stock_web.news_fetcher 2330 2317 5388 --days 14
    python3 -m stock_web.news_fetcher 2330 --no-cache       # force refresh
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

MOPS_BASE = "https://mopsov.twse.com.tw"
MOPS_WARMUP = f"{MOPS_BASE}/mops/web/t05st01"
MOPS_AJAX = f"{MOPS_BASE}/mops/web/ajax_t05st01"

REQUEST_TIMEOUT_SEC = 20
REQUEST_INTERVAL_SEC = 2  # gap between MOPS calls — they tolerate
                          # faster than TWSE but be polite

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

_session: requests.Session | None = None
_last_req_ts: float = 0.0


def _get_session() -> requests.Session:
    """Lazy-init a shared session. First call also warms up MOPS so the
    server-side anti-bot check is satisfied before we POST."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(MOPS_WARMUP, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException:
            pass  # warmup failures aren't fatal — POSTs may still work
        _session = s
    return _session


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


def _roc_year(d: date) -> int:
    return d.year - 1911


def _strip_html(s: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")  # NBSP from &nbsp;
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_TR_RE = re.compile(
    r"<tr\s+class=['\"](?:even|odd)['\"][^>]*>(.*?)</tr>",
    re.DOTALL | re.IGNORECASE,
)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_SPOKE_DATE_RE = re.compile(r"spoke_date\.value\s*=\s*['\"](\d{8})['\"]")
_SPOKE_TIME_RE = re.compile(r"spoke_time\.value\s*=\s*['\"](\d{6})['\"]")


def _parse_html(body: str) -> list[dict[str, Any]]:
    """Extract one row per material announcement from the MOPS HTML.

    Each <tr.even|odd> has 6 <td> cells:
      [0] 公司代號  [1] 公司名稱  [2] 發言日期(ROC)  [3] 發言時間
      [4] 主旨    [5] 詳細資料按鈕(onclick has spoke_date/time in AD)
    """
    out: list[dict[str, Any]] = []
    for tr in _TR_RE.finditer(body):
        block = tr.group(1)
        cells = _TD_RE.findall(block)
        if len(cells) < 5:
            continue
        code = _strip_html(cells[0])
        name = _strip_html(cells[1])
        roc_date = _strip_html(cells[2])      # e.g. "115/05/05"
        time_str = _strip_html(cells[3])      # e.g. "18:19:27"
        title = _strip_html(cells[4])

        # AD date pulled from the 詳細資料 button so we don't have to
        # re-do ROC→AD math at every consumer.
        ad_date = ""
        if len(cells) >= 6:
            m = _SPOKE_DATE_RE.search(cells[5])
            if m:
                d8 = m.group(1)
                ad_date = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"

        if not (code and title and roc_date):
            continue
        out.append({
            "code": code,
            "name": name,
            "date": ad_date,      # ISO, empty if button parse failed
            "roc_date": roc_date,
            "time": time_str,
            "title": title,
        })
    return out


def fetch_month(code: str, year: int, month: int) -> list[dict[str, Any]]:
    """Fetch all 重訊 for one stock in one ROC year-month.

    `year` is the AD year (e.g. 2026); we convert to ROC internally.
    Returns rows sorted newest-first.
    """
    _throttle()
    s = _get_session()
    payload = {
        "step": "1",
        "firstin": "1",
        "off": "1",
        "queryName": "co_id",
        "inpuType": "co_id",
        "TYPEK": "all",
        "co_id": code,
        "year": str(year - 1911),
        "month": str(month),
    }
    try:
        resp = s.post(
            MOPS_AJAX,
            data=payload,
            headers={"Referer": MOPS_WARMUP},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        print(f"  [warn] MOPS request failed for {code} "
              f"{year}/{month}: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        print(f"  [warn] MOPS HTTP {resp.status_code} "
              f"for {code} {year}/{month}", file=sys.stderr)
        return []
    if "頁面無法執行" in resp.text:
        print(f"  [warn] MOPS blocked request for {code} "
              f"{year}/{month}", file=sys.stderr)
        return []
    rows = _parse_html(resp.text)
    rows.sort(key=lambda r: (r["date"] or r["roc_date"], r["time"]),
              reverse=True)
    return rows


def fetch_recent(code: str, days: int = 7,
                 today: date | None = None) -> list[dict[str, Any]]:
    """Fetch material announcements for the last `days` calendar days.

    Spans at most two MOPS month-queries (current + previous month),
    even when `days` crosses a month boundary.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=days - 1)

    months_to_query: list[tuple[int, int]] = [(today.year, today.month)]
    if (cutoff.year, cutoff.month) != (today.year, today.month):
        months_to_query.append((cutoff.year, cutoff.month))

    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for y, m in months_to_query:
        for row in fetch_month(code, y, m):
            iso = row["date"]
            if iso:
                try:
                    d = date.fromisoformat(iso)
                except ValueError:
                    continue
                if d < cutoff or d > today:
                    continue
            key = (row["date"], row["time"], row["title"])
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    out.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
    return out


def _cache_path(code: str, today: date | None = None) -> Path:
    today = today or date.today()
    return CACHE_DIR / f"news_{code}_{today.strftime('%Y%m%d')}.json"


def load_or_fetch(code: str, days: int = 7,
                  *, force: bool = False) -> dict[str, Any]:
    """Return {"code", "fetched_at", "items"}. Cached per trading day.

    Pass `force=True` to bypass cache. On fetch failure with no cache,
    returns an empty items list rather than raising.
    """
    p = _cache_path(code)
    if not force and p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass  # corrupt cache — refetch
    items = fetch_recent(code, days=days)
    payload = {
        "code": code,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "items": items,
    }
    try:
        with p.open("w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [warn] could not write cache {p}: {e}", file=sys.stderr)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("codes", nargs="+", help="stock codes, e.g. 2330 2317")
    ap.add_argument("--days", type=int, default=7,
                    help="lookback window in calendar days (default 7)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore on-disk cache and fetch fresh")
    args = ap.parse_args()

    for code in args.codes:
        print(f"\n=== {code}  (last {args.days} days) ===")
        data = load_or_fetch(code, days=args.days, force=args.no_cache)
        items = data.get("items", [])
        if not items:
            print("  (no material announcements)")
            continue
        for r in items:
            d = r["date"] or r["roc_date"]
            print(f"  {d}  {r['time']:>8}  {r['title']}")


if __name__ == "__main__":
    main()
