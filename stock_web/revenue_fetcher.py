"""Fetch monthly revenue (月營收) for Taiwan stocks from MOPS.

Monthly revenue is published around the 10th of each month for the
previous calendar month. The whole-market file at

    https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{ROC_Y}_{M}_0.html

lists every TWSE (sii) or TPEX (otc) stock's revenue for that month in
one Big5-encoded HTML table.

We cache per (market, AD-year, AD-month) under filenames like
`revenue_sii_202604.json`. These files are immutable once published, so
they sit *outside* the dated-cache auto-purge window — `_parse_cache_date`
in app.py returns None for the non-YYYYMMDD suffix, so the purger
ignores them.

For a given stock we typically need just the most recent published
month (MoM% and YoY% are pre-computed in the source file itself). The
high-level entry `get_for_code(code)` falls back one month if the
expected file isn't published yet.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

MOPS_BASE = "https://mopsov.twse.com.tw"
REQUEST_TIMEOUT_SEC = 30
REQUEST_INTERVAL_SEC = 2

MARKET_TWSE = "twse"
MARKET_OTC = "otc"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36"
    ),
}

_last_req_ts: float = 0.0


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


def _cache_path(market: str, year: int, month: int) -> Path:
    seg = "sii" if market == MARKET_TWSE else "otc"
    return CACHE_DIR / f"revenue_{seg}_{year:04d}{month:02d}.json"


# Match any <tr>...</tr>. We filter by cell structure (11 cells, first
# is a 4-digit code) which is a more reliable discriminator than the
# `align=right` attribute alone.
_RE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_RE_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("\xa0", " ").strip()


def _to_int(s: str) -> int | None:
    s = s.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_month_file(text: str) -> dict[str, dict[str, Any]]:
    """Return {code: {name, revenue, prev_month_rev, yoy_rev,
                      mom_pct, yoy_pct, cum, cum_prev, cum_yoy_pct}}.

    Column layout in the source file:
      0=code  1=name  2=當月營收  3=上月營收  4=去年當月營收
      5=上月%  6=去年同月%  7=當月累計  8=去年累計  9=累計%  10=備註
    """
    out: dict[str, dict[str, Any]] = {}
    for tr in _RE_ROW.finditer(text):
        cells = [_strip(c) for c in _RE_TD.findall(tr.group(1))]
        if len(cells) < 10:
            continue
        code = cells[0]
        if not (code.isdigit() and 4 <= len(code) <= 6):
            continue
        out[code] = {
            "name": cells[1],
            "revenue": _to_int(cells[2]),
            "prev_month_rev": _to_int(cells[3]),
            "yoy_rev": _to_int(cells[4]),
            "mom_pct": _to_float(cells[5]),
            "yoy_pct": _to_float(cells[6]),
            "cum": _to_int(cells[7]),
            "cum_prev": _to_int(cells[8]),
            "cum_yoy_pct": _to_float(cells[9]),
        }
    return out


def fetch_month(market: str, ad_year: int, month: int,
                *, force: bool = False) -> dict[str, dict[str, Any]]:
    """Return parsed month-of-revenue for the given market.

    Cached per (market, year, month) — these files are immutable once
    published. Empty dict on failure or if the file hasn't been
    published yet (e.g. asking for the current month).
    """
    p = _cache_path(market, ad_year, month)
    if not force and p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass  # corrupt cache → refetch
    seg = "sii" if market == MARKET_TWSE else "otc"
    roc = ad_year - 1911
    url = f"{MOPS_BASE}/nas/t21/{seg}/t21sc03_{roc}_{month}_0.html"
    _throttle()
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    except requests.RequestException as e:
        print(f"  [warn] revenue fetch failed {market} "
              f"{ad_year}/{month}: {e}", file=sys.stderr)
        return {}
    if r.status_code != 200:
        return {}
    r.encoding = "big5"
    if "公司代號" not in r.text:
        return {}  # file exists but wasn't a data file
    parsed = _parse_month_file(r.text)
    if parsed:
        try:
            with p.open("w") as f:
                json.dump(parsed, f, ensure_ascii=False)
        except OSError as e:
            print(f"  [warn] revenue cache write failed {p}: {e}",
                  file=sys.stderr)
    return parsed


def _prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _latest_published(today: date | None = None) -> tuple[int, int]:
    """The most recent month for which revenue is expected to be
    published. Revenue for month M is required to be published by
    M+1's 10th; we use day 11 as the safe cutoff."""
    today = today or date.today()
    py, pm = _prev_month(today.year, today.month)
    if today.day < 11:
        py, pm = _prev_month(py, pm)
    return py, pm


def get_for_code(code: str, market: str = MARKET_TWSE,
                 today: date | None = None) -> dict[str, Any] | None:
    """Return the most-recent monthly-revenue snapshot for one stock.

    Returns None if revenue isn't available — e.g. new listing not yet
    in the file, or month file not yet published and even fallback
    month doesn't have it.
    """
    today = today or date.today()
    py, pm = _latest_published(today)
    # Try the latest expected month, then walk back one more if missing.
    for _ in range(2):
        month_data = fetch_month(market, py, pm)
        if code in month_data:
            row = month_data[code]
            return {
                "code": code,
                "name": row.get("name") or "",
                "market": market,
                "year": py,
                "month": pm,
                "revenue": row["revenue"],
                "prev_month_rev": row["prev_month_rev"],
                "yoy_rev": row["yoy_rev"],
                "mom_pct": row["mom_pct"],
                "yoy_pct": row["yoy_pct"],
                "cum": row["cum"],
                "cum_prev": row["cum_prev"],
                "cum_yoy_pct": row["cum_yoy_pct"],
            }
        py, pm = _prev_month(py, pm)
    return None


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--market", default="twse", choices=["twse", "otc"])
    args = ap.parse_args()
    for code in args.codes:
        rev = get_for_code(code, market=args.market)
        if not rev:
            print(f"{code}: no revenue data")
            continue
        # Source values are already in 千元 (kilo-NTD); convert to 億
        # (hundred-million NTD) for display readability.
        rev_yi = (rev["revenue"] or 0) / 1e5
        mom = rev["mom_pct"] if rev["mom_pct"] is not None else float("nan")
        yoy = rev["yoy_pct"] if rev["yoy_pct"] is not None else float("nan")
        ytd = (rev["cum_yoy_pct"]
               if rev["cum_yoy_pct"] is not None else float("nan"))
        print(
            f"{code} {rev['name']}  "
            f"{rev['year']}/{rev['month']:02d}  "
            f"{rev_yi:>10,.1f} 億元  "
            f"MoM={mom:+.2f}%  YoY={yoy:+.2f}%  YTD={ytd:+.2f}%"
        )


if __name__ == "__main__":
    main()
