"""Fetch per-stock 融資/融券/借券 daily snapshots from TWSE.

Two TWSE endpoints, both whole-market dumps cached per trading day:

  1. MI_MARGN (信用交易) — exchangeReport endpoint that returns two
     tables: market-aggregate (ignored) and per-stock 融資+融券 in
     LOTS (張). We parse table[1] only.
       URL: /exchangeReport/MI_MARGN?date=YYYYMMDD&selectType=ALL&response=json
     Fields used per row:
       [0] 代號  [1] 名稱
       [2..7]   融資: 買進, 賣出, 現金償還, 前日餘額, 今日餘額, 次一營業日限額
       [8..13]  融券: 買進, 賣出, 現券償還, 前日餘額, 今日餘額, 次一營業日限額
       [14]     資券互抵   [15] 註記

  2. TWT93U (信用交易+借券明細) — newer rwd endpoint with 融券
     再次列出 (cols 2-7, in SHARES not lots) AND 借券 (cols 8-13).
     We only consume the 借券 half here since 融券 comes cleaner
     out of MI_MARGN in lots.
       URL: /rwd/zh/marginTrading/TWT93U?date=YYYYMMDD&response=json
     Fields used per row:
       [0] 代號  [1] 名稱
       [2..7]   融券 in shares (ignored — duplicate of MI_MARGN /1000)
       [8..13]  借券: 前日餘額, 當日賣出, 當日還券, 當日調整,
                       當日餘額, 次一營業日可限額  (all in 股 shares)
       [14]     備註

Caches (whole-market, immutable once a trading day completes):
  cache/margin_{YYYYMMDD}.json   — {code: {f_today, f_yday, f_limit,
                                           s_today, s_yday, s_limit}}
                                   all lot counts
  cache/sbl_{YYYYMMDD}.json      — {code: {bal_today, bal_yday}}
                                   in lots (shares/1000, rounded)

Read-only loaders try cache only — no network fan-out at request time;
the morning launchd refresh (`tools/refresh_watchlist.py`) is what
prepopulates these. If a code isn't in today's cache, the API returns
`available: false` and the UI hides the panel.

OTC is NOT yet supported — TWSE-only for v1.

Usage from app.py:
    from stock_web import margin_sbl_fetcher as msbl
    snapshot = msbl.get_for_code("2330", days=10)
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (compatible; stock-web/1.0)"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

MARGIN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
SBL_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"
REQUEST_TIMEOUT_SEC = 15
REQUEST_INTERVAL_SEC = 2

_last_req = 0.0


def _throttle() -> None:
    global _last_req
    elapsed = time.time() - _last_req
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req = time.time()


def _to_int_lots(s: str) -> int | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _margin_cache(date_compact: str) -> Path:
    return CACHE_DIR / f"margin_{date_compact}.json"


def _sbl_cache(date_compact: str) -> Path:
    return CACHE_DIR / f"sbl_{date_compact}.json"


def fetch_margin(date_iso: str) -> dict[str, dict]:
    """Whole-market 融資+融券 snapshot for date_iso (lots).

    Returns {code: {f_today, f_yday, f_limit, s_today, s_yday, s_limit}}.
    Empty dict on non-trading day / fetch failure / unexpected shape.
    """
    date_compact = date_iso.replace("-", "")
    cache = _margin_cache(date_compact)
    if cache.exists():
        try:
            with cache.open() as f:
                d = json.load(f)
            if d:
                return d
        except (OSError, json.JSONDecodeError):
            pass
    _throttle()
    try:
        r = requests.get(
            MARGIN_URL,
            params={"date": date_compact, "selectType": "ALL",
                    "response": "json"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC,
        )
        r.raise_for_status()
        j = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if j.get("stat") != "OK":
        return {}
    out: dict[str, dict] = {}
    tables = j.get("tables") or []
    detail = None
    for t in tables:
        title = (t.get("title") or "")
        if "彙總" in title:  # the per-stock 融資融券彙總 table
            detail = t
            break
    if detail is None and len(tables) >= 2:
        detail = tables[1]  # known position fallback
    if detail is None:
        return {}
    for row in detail.get("data") or []:
        if not row or len(row) < 14:
            continue
        code = (row[0] or "").strip()
        if not code or not re.match(r"^\d{4,6}[A-Z]?$", code):
            continue
        out[code] = {
            "f_today": _to_int_lots(row[6]),
            "f_yday":  _to_int_lots(row[5]),
            "f_limit": _to_int_lots(row[7]),
            "s_today": _to_int_lots(row[12]),
            "s_yday":  _to_int_lots(row[11]),
            "s_limit": _to_int_lots(row[13]),
        }
    if out:
        try:
            with cache.open("w") as f:
                json.dump(out, f)
        except OSError:
            pass
    return out


def fetch_sbl(date_iso: str) -> dict[str, dict]:
    """Whole-market 借券 snapshot for date_iso.

    TWT93U reports values in shares (股); we convert to lots (張 =
    1000 shares) to align with everything else in this codebase.
    Fractional balances (e.g. 119,500 shares = 119.5 lots) get
    rounded to nearest int — these are SBL aggregates so a rounded
    lot count is plenty for visual scale.

    Returns {code: {bal_today, bal_yday}} in lots.
    """
    date_compact = date_iso.replace("-", "")
    cache = _sbl_cache(date_compact)
    if cache.exists():
        try:
            with cache.open() as f:
                d = json.load(f)
            if d:
                return d
        except (OSError, json.JSONDecodeError):
            pass
    _throttle()
    try:
        r = requests.get(
            SBL_URL,
            params={"date": date_compact, "response": "json"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC,
        )
        r.raise_for_status()
        j = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if j.get("stat") != "OK":
        return {}
    out: dict[str, dict] = {}
    for row in j.get("data") or []:
        if not row or len(row) < 13:
            continue
        code = (row[0] or "").strip()
        if not code or not re.match(r"^\d{4,6}[A-Z]?$", code):
            continue
        bal_today_sh = _to_int_lots(row[12])  # 當日餘額 in shares
        bal_yday_sh = _to_int_lots(row[8])    # 前日餘額 in shares
        out[code] = {
            "bal_today": (round(bal_today_sh / 1000)
                          if bal_today_sh is not None else None),
            "bal_yday":  (round(bal_yday_sh / 1000)
                          if bal_yday_sh is not None else None),
        }
    if out:
        try:
            with cache.open("w") as f:
                json.dump(out, f)
        except OSError:
            pass
    return out


def _load_cached_margin(date_iso: str) -> dict | None:
    cache = _margin_cache(date_iso.replace("-", ""))
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_cached_sbl(date_iso: str) -> dict | None:
    cache = _sbl_cache(date_iso.replace("-", ""))
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_for_code(code: str, days: int = 10,
                 today: date | None = None,
                 fetch_missing: bool = True) -> dict[str, Any]:
    """Return a snapshot for `code` with up to `days` of trailing
    margin + SBL history.

    Shape:
      {
        "available": bool,
        "code": str,
        "latest_date": "YYYY-MM-DD" | None,
        "snapshot": {
          "f": {balance_today, balance_yday, change_5d, usage_pct}
                margin financing — 融資
          "s": {balance_today, balance_yday, change_5d}
                margin short — 融券
          "sbl": {balance_today, balance_yday, change_5d}
                securities lending — 借券
          "long_short_ratio": float | None    資券比 = 融資/融券
        },
        "history": [
          {"date": ..., "f": int, "s": int, "sbl": int|None}, ...
        ]   chronological asc
      }

    `days` clamps to [3, 30] for the returned `history` list. The
    function also walks back ~60 trading days separately to compute
    `range_60d` stats per metric (max / min / current percentile),
    so the UI can tell whether today sits in the high or low end of
    recent history. `fetch_missing=True` triggers a network fetch
    when the LATEST trading day isn't cached; historical-day fetches
    are NEVER fanned out per request — they come from existing
    caches only, so the function is cheap when warm.
    """
    days = max(3, min(int(days or 10), 30))
    today = today or date.today()
    # Walk back enough weekdays to populate range stats (60 trading
    # days ≈ 3 calendar months), capped so we don't iterate forever.
    RANGE_LOOKBACK_DAYS = 60

    # Build candidate dates: walk back from `today` skipping weekends.
    # We don't have a TWSE calendar handy, so weekends + missing-cache
    # behaves like a holiday.
    candidates: list[str] = []
    d = today
    while (len(candidates) < RANGE_LOOKBACK_DAYS * 2
           and (today - d).days < RANGE_LOOKBACK_DAYS * 2):
        if d.weekday() < 5:  # Mon-Fri
            candidates.append(d.isoformat())
        d -= timedelta(days=1)

    history: list[dict] = []
    latest_date: str | None = None
    for i, iso in enumerate(candidates):
        m = _load_cached_margin(iso)
        s = _load_cached_sbl(iso)
        # Opportunistic fetch for the most recent candidate only, and
        # only if the caller asked for it. Avoids fanning out 10 fetches
        # per request — historical days come from cache only.
        if (m is None or s is None) and i == 0 and fetch_missing:
            if m is None:
                m = fetch_margin(iso)
            if s is None:
                s = fetch_sbl(iso)
        if not m and not s:
            continue
        m_row = (m or {}).get(code)
        s_row = (s or {}).get(code)
        if not m_row and not s_row:
            continue
        history.append({
            "date": iso,
            "f": (m_row or {}).get("f_today"),
            "s": (m_row or {}).get("s_today"),
            "sbl": (s_row or {}).get("bal_today"),
            "f_limit": (m_row or {}).get("f_limit"),
        })
        latest_date = latest_date or iso

    if not history:
        return {"available": False, "code": code,
                "latest_date": None, "snapshot": None, "history": []}

    history.sort(key=lambda x: x["date"])  # chronological asc
    latest = history[-1]
    yday = history[-2] if len(history) >= 2 else None
    # 5-day change: compare latest to history[-6] if available, else
    # earliest.
    bench = history[-6] if len(history) >= 6 else history[0]

    def _delta(now, prev):
        if now is None or prev is None:
            return None
        return now - prev

    def _ratio(now, prev):
        if now is None or prev is None or prev == 0:
            return None
        return (now - prev) / prev

    f_now, s_now, sbl_now = latest["f"], latest["s"], latest["sbl"]
    usage_pct = None
    if f_now is not None and latest["f_limit"]:
        usage_pct = round(f_now / latest["f_limit"] * 100, 2)
    long_short = None
    if f_now is not None and s_now and s_now > 0:
        long_short = round(f_now / s_now, 2)

    # 60-day range stats per metric — lets the UI tell the user
    # whether today sits at the high / low end of recent history.
    # Percentile = (current - min) / (max - min); 0..1, where 1 = at
    # 60-day high. None if <5 days of data or flat series.
    def _range_stats(key: str):
        vals = [h.get(key) for h in history if h.get(key) is not None]
        if len(vals) < 5:
            return None
        mx, mn = max(vals), min(vals)
        cur = history[-1].get(key)
        if cur is None or mx == mn:
            return {"max": mx, "min": mn, "n_days": len(vals),
                    "percentile": None}
        pct = (cur - mn) / (mx - mn)
        return {"max": mx, "min": mn, "n_days": len(vals),
                "percentile": round(pct, 3)}

    snapshot = {
        "f": {
            "balance_today": f_now,
            "balance_yday": (yday or {}).get("f"),
            "change_5d_abs": _delta(f_now, bench["f"]),
            "change_5d_pct": _ratio(f_now, bench["f"]),
            "usage_pct": usage_pct,
            "range_60d": _range_stats("f"),
        },
        "s": {
            "balance_today": s_now,
            "balance_yday": (yday or {}).get("s"),
            "change_5d_abs": _delta(s_now, bench["s"]),
            "change_5d_pct": _ratio(s_now, bench["s"]),
            "range_60d": _range_stats("s"),
        },
        "sbl": {
            "balance_today": sbl_now,
            "balance_yday": (yday or {}).get("sbl"),
            "change_5d_abs": _delta(sbl_now, bench["sbl"]),
            "change_5d_pct": _ratio(sbl_now, bench["sbl"]),
            "range_60d": _range_stats("sbl"),
        },
        "long_short_ratio": long_short,
    }
    # Truncate the returned history to the requested `days` window —
    # we walked back 60 days to compute range_60d but the response
    # only needs the most recent N for display.
    history_return = history[-days:]
    return {
        "available": True,
        "code": code,
        "latest_date": latest["date"],
        "snapshot": snapshot,
        "history": history_return,
    }
