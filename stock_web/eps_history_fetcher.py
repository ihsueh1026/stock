"""Fetch a multi-year quarterly EPS history for one stock from MOPS.

The 'simplified profit/loss' endpoint t146sb05 used by fundamentals_fetcher
only returns the last 3-4 periods, which is not enough to plot a trend.
For full quarterly EPS we hit ajax_t164sb04 (合併綜合損益表 - 完整版),
which returns one fiscal (year, season) at a time.

IMPORTANT Taiwanese-disclosure quirk: t164sb04 returns *standalone
single-quarter* EPS for season ∈ {1, 2, 3}, but for season = 4 it
returns the FULL-YEAR cumulative (because the Q4 filing is the annual
report, not a separate Q4 report). So:

    raw[year, 1]  = Q1 standalone EPS
    raw[year, 2]  = Q2 standalone EPS
    raw[year, 3]  = Q3 standalone EPS
    raw[year, 4]  = annual EPS (= Q1+Q2+Q3+Q4 standalone)
    → Q4_standalone = raw[year, 4] - (raw[y,1] + raw[y,2] + raw[y,3])

Each call also gives the same period of the *prior* year for free.

`firstin` MUST be '1' (not 'true'). With 'true', MOPS treats it as the
landing page and silently ignores the `season` filter — every call
returns the latest annual.

Cache: `eps_q_{code}_{Y}Q{N}.json` (no date suffix) — fiscal-period data
is immutable once published, so these sit outside the dated-cache purge
window. Unpublished periods are NOT cached so subsequent calls retry.

Public entry: `get_history(code, market, years=3)`.
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
MOPS_WARMUP = f"{MOPS_BASE}/mops/web/t164sb04"
MOPS_AJAX = f"{MOPS_BASE}/mops/web/ajax_t164sb04"

REQUEST_TIMEOUT_SEC = 30
REQUEST_INTERVAL_SEC = 2
MAX_RETRIES = 1
RETRY_BACKOFF_SEC = 2

MARKET_TWSE = "twse"
MARKET_OTC = "otc"

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

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(MOPS_WARMUP, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException:
            pass
        _session = s
    return _session


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("\xa0", " ").strip()


def _to_float(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cache_path(code: str, year_ad: int, season: int) -> Path:
    return CACHE_DIR / f"eps_q_{code}_{year_ad:04d}Q{season}.json"


def _parse_eps_pair(html: str) -> dict[str, float | None]:
    """Find the 基本每股盈餘 row; pick out (current, prior) EPS.

    The row looks like:
        <tr><td>基本每股盈餘</td>
            <td>66.26</td><td>...</td><td>45.25</td><td>...</td></tr>

    There may be multiple rows starting with '基本每股盈餘' (an empty
    header row, then the data row). Take the first row whose numeric
    cells parse as floats.
    """
    for tr in _TR_RE.finditer(html):
        cells = [_strip(c) for c in _TD_RE.findall(tr.group(1))]
        if not cells or not cells[0].startswith("基本每股盈餘"):
            continue
        nums = [_to_float(c) for c in cells[1:]]
        # Expected layout: [current_amt, current_pct, prior_amt, prior_pct]
        if len(nums) >= 3 and (nums[0] is not None or nums[2] is not None):
            return {"current": nums[0],
                    "prior": nums[2] if len(nums) >= 3 else None}
    return {"current": None, "prior": None}


def _fetch_one(code: str, year_ad: int, season: int,
               market: str = MARKET_TWSE,
               *, force: bool = False) -> dict[str, Any] | None:
    """Fetch one (year, season). Returns None if not yet published.

    Cached file shape: {"year": Y, "season": Q, "eps": v|null,
                        "prior_eps": v|null, "prior_year": Y-1}
    """
    global _session
    p = _cache_path(code, year_ad, season)
    if not force and p.exists():
        try:
            with p.open() as f:
                d = json.load(f)
            # If cached as "not yet published" (eps null) and the period is
            # now plausibly out, allow a refetch on the *next* call by
            # NOT caching None — see below. Reaching here means we have data.
            return d
        except (OSError, json.JSONDecodeError):
            pass

    typek = "sii" if market == MARKET_TWSE else "otc"
    # NB: firstin must be '1', not 'true' — the latter puts the endpoint
    # into "splash/index" mode and ignores the season parameter, returning
    # the latest annual data regardless of what season was asked.
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "TYPEK": typek,
        "co_id": code,
        "year": str(year_ad - 1911),
        "season": str(season),
    }

    body: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        s = _get_session()
        try:
            r = s.post(MOPS_AJAX, data=payload,
                       headers={"Referer": MOPS_WARMUP},
                       timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC)
                _session = None
                continue
            print(f"  [warn] eps fetch {code} {year_ad}Q{season}: {e}",
                  file=sys.stderr)
            return None
        if r.status_code == 200 and "頁面無法執行" not in r.text:
            body = r.text
            break
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC)
    if body is None:
        return None

    if "基本每股盈餘" not in body:
        return None  # period not published yet
    pair = _parse_eps_pair(body)
    if pair["current"] is None and pair["prior"] is None:
        return None

    out = {
        "code": code,
        "year": year_ad,
        "season": season,
        "eps": pair["current"],
        "prior_year": year_ad - 1,
        "prior_eps": pair["prior"],
    }
    # Only cache if we actually got the current-period number. A response
    # that has only the prior year (because the current quarter hasn't
    # been published yet) is a transient state — we'd rather retry it
    # later than freeze in a partial answer.
    if out["eps"] is not None:
        try:
            with p.open("w") as f:
                json.dump(out, f, ensure_ascii=False)
        except OSError as e:
            print(f"  [warn] eps cache write failed {p}: {e}",
                  file=sys.stderr)
    return out


def get_history(code: str, market: str = MARKET_TWSE,
                years: int = 3,
                today: date | None = None) -> dict[str, Any]:
    """Return chronologically-sorted quarterly EPS history.

    Walks back `years` ROC years × 4 seasons. Each call gives us the
    asked (year, season) AND the same season of the prior year, so we
    bag two data points per network round trip.

    Raw values are interpreted as:
      - season 1,2,3 → that quarter's *standalone* EPS
      - season 4    → the full-year cumulative EPS
    Q4 standalone is derived as annual - (Q1+Q2+Q3).

    Returns periods with both standalone (per-quarter) and annual
    (full-year, attached to Q4) values plus YoY.
    """
    today = today or date.today()
    current_ad = today.year

    # raw[(year, season)] = the raw number MOPS gives us. Standalone
    # for season 1..3, annual for season 4.
    raw: dict[tuple[int, int], float] = {}

    # Walk oldest → newest so cache hits are deterministic.
    for y in range(current_ad - years, current_ad + 1):
        for season in (1, 2, 3, 4):
            row = _fetch_one(code, y, season, market=market)
            if row is None:
                continue
            if row.get("eps") is not None:
                raw[(row["year"], row["season"])] = row["eps"]
            if row.get("prior_eps") is not None:
                raw.setdefault(
                    (row["prior_year"], row["season"]),
                    row["prior_eps"],
                )

    if not raw:
        return {"code": code, "market": market, "available": False,
                "periods": []}

    # Build per-quarter standalone, deriving Q4 from annual.
    standalone: dict[tuple[int, int], float | None] = {}
    annual: dict[int, float | None] = {}
    years_seen = sorted({y for (y, _q) in raw.keys()})
    for y in years_seen:
        q1 = raw.get((y, 1))
        q2 = raw.get((y, 2))
        q3 = raw.get((y, 3))
        ann = raw.get((y, 4))
        if q1 is not None:
            standalone[(y, 1)] = q1
        if q2 is not None:
            standalone[(y, 2)] = q2
        if q3 is not None:
            standalone[(y, 3)] = q3
        if ann is not None:
            annual[y] = ann
            # Derive Q4 standalone if we have all 3 prior quarters.
            if None not in (q1, q2, q3):
                standalone[(y, 4)] = round(ann - q1 - q2 - q3, 2)

    keys = sorted(set(standalone.keys()) | {(y, 4) for y in annual})
    periods: list[dict[str, Any]] = []
    for (y, q) in keys:
        val = standalone.get((y, q))
        yoy_prev = standalone.get((y - 1, q))
        yoy_pct = None
        if val is not None and yoy_prev is not None and yoy_prev != 0:
            yoy_pct = round((val - yoy_prev) / abs(yoy_prev) * 100, 1)
        periods.append({
            "label": f"{y}Q{q}",
            "year": y,
            "quarter": q,
            "eps": val,
            "eps_yoy_pct": yoy_pct,
            "annual_eps": annual.get(y) if q == 4 else None,
        })

    return {
        "code": code,
        "market": market,
        "available": True,
        "periods": periods,
        "annual": [
            {"year": y, "eps": annual[y]} for y in sorted(annual.keys())
        ],
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--market", default="twse", choices=["twse", "otc"])
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()
    for code in args.codes:
        d = get_history(code, market=args.market, years=args.years)
        if not d.get("available"):
            print(f"{code}: no data")
            continue
        print(f"\n=== {code} ({args.market}) ===")
        for p in d["periods"]:
            eps = p["eps"]
            yoy = p["eps_yoy_pct"]
            ann = p.get("annual_eps")
            eps_s = f"{eps:>6.2f}" if eps is not None else "  n/a"
            yoy_s = f"{yoy:+.1f}%" if yoy is not None else "  n/a"
            ann_s = f"  [annual={ann:.2f}]" if ann is not None else ""
            print(f"  {p['label']:>8}  EPS={eps_s}  YoY={yoy_s}{ann_s}")


if __name__ == "__main__":
    main()
