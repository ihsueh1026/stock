"""Compute per-industry P/E median across TWSE + TPEX.

Joins the per-stock PER from the same OpenAPI feeds used by
`dividend_fetcher`:

  - TWSE: openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL  (PEratio)
  - TPEX: tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis
                                                       (PriceEarningRatio)

against the daily company-info dump cached in app.py. The result is a
single dict keyed by industry name (e.g. "半導體業") with median /
count / quartile stats.

Negative or zero PERs (loss-making companies) are excluded — a P/E
median means little if half the basket has no earnings. We require at
least 5 valid samples per industry to publish a median (smaller buckets
get returned with `count` but no `median_pe`).

Cache: `industry_pe_{YYYYMMDD}.json`. The build is cheap (single CPU
pass over ~2000 rows once both raw feeds are warm) but daily caching
makes the per-detail-page lookup constant-time.

Public entry:
  - `build_stats(companies_twse, companies_otc)` — pure function, used
    when caller has company maps in hand (e.g. app.py).
  - `for_industry(industry, ...)` — daily-cached lookup.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
OTC_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

REQUEST_TIMEOUT_SEC = 20
REQUEST_INTERVAL_SEC = 1
MIN_SAMPLES = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}

_last_req_ts: float = 0.0


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


def _today_tag() -> str:
    return date.today().strftime("%Y%m%d")


def _cache_path() -> Path:
    return CACHE_DIR / f"industry_pe_{_today_tag()}.json"


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_per_twse() -> dict[str, float]:
    _throttle()
    try:
        r = requests.get(TWSE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] TWSE PER fetch: {e}", file=sys.stderr)
        return {}
    out: dict[str, float] = {}
    for row in rows:
        code = row.get("Code") or ""
        per = _to_float(row.get("PEratio"))
        if code and per is not None and per > 0:
            out[code] = per
    return out


def _fetch_per_otc() -> dict[str, float]:
    _throttle()
    try:
        r = requests.get(OTC_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] TPEX PER fetch: {e}", file=sys.stderr)
        return {}
    out: dict[str, float] = {}
    for row in rows:
        code = row.get("SecuritiesCompanyCode") or ""
        per = _to_float(row.get("PriceEarningRatio"))
        if code and per is not None and per > 0:
            out[code] = per
    return out


def _summarize(pers: list[float]) -> dict[str, Any]:
    pers_sorted = sorted(pers)
    n = len(pers_sorted)
    summary: dict[str, Any] = {"count": n}
    if n >= MIN_SAMPLES:
        summary["median_pe"] = round(statistics.median(pers_sorted), 2)
        # 25/75 quartile via index — good enough for n>=5
        summary["q25_pe"] = round(pers_sorted[max(0, n // 4)], 2)
        summary["q75_pe"] = round(pers_sorted[min(n - 1, n * 3 // 4)], 2)
    return summary


def build_stats(companies_twse: dict[str, dict[str, Any]],
                companies_otc: dict[str, dict[str, Any]]
                ) -> dict[str, Any]:
    """Return {industry: {count, median_pe, q25_pe, q75_pe}} from live data.

    companies_* maps are code → {"industry": "半導體業", ...} from app.py.
    """
    per_twse = _fetch_per_twse()
    per_otc = _fetch_per_otc()
    by_industry: dict[str, list[float]] = {}

    def _consume(companies: dict, pers: dict):
        for code, info in companies.items():
            per = pers.get(code)
            if per is None:
                continue
            ind = (info.get("industry") or "").strip()
            if not ind:
                continue
            by_industry.setdefault(ind, []).append(per)

    _consume(companies_twse, per_twse)
    _consume(companies_otc, per_otc)

    return {
        ind: _summarize(vals) for ind, vals in by_industry.items()
    }


def load_or_build(companies_twse: dict, companies_otc: dict,
                  *, force: bool = False) -> dict[str, Any]:
    """Daily-cached `build_stats`. Falls back to live build on cache miss."""
    p = _cache_path()
    if not force and p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    stats = build_stats(companies_twse, companies_otc)
    if stats:
        try:
            with p.open("w") as f:
                json.dump(stats, f, ensure_ascii=False)
        except OSError as e:
            print(f"  [warn] industry-pe cache write {p}: {e}",
                  file=sys.stderr)
    return stats


def for_industry(industry: str,
                 companies_twse: dict, companies_otc: dict
                 ) -> dict[str, Any] | None:
    """Lookup one industry's stats. Returns None if industry unknown."""
    if not industry:
        return None
    stats = load_or_build(companies_twse, companies_otc)
    s = stats.get(industry)
    if not s:
        return None
    return {"industry": industry, **s}
