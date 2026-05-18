"""FastAPI backend for the TWSE stock viewer.

Wraps the existing fetch_twse_daily.py logic and exposes one JSON endpoint.
Run with:
    uvicorn stock_web.app:app --reload --port 8000
from the parent directory.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Body
from fastapi.staticfiles import StaticFiles

# Make the parent dir importable so we can reuse fetch_twse_daily.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch_twse_daily as twse  # noqa: E402

# News/revenue extensions. These modules are intentionally lazy-loaded
# at module level but their side effects (HTTP calls) only happen when
# their public functions are called from an endpoint.
from stock_web import (  # noqa: E402
    news_fetcher,
    news_llm,
    revenue_fetcher,
    fundamentals_fetcher,
    eps_history_fetcher,
    dividend_fetcher,
    industry_pe_fetcher,
    forward_log,
)

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Shorter warmup than the CLI script's 250: trades MACD-EMA precision for
# response time. 60 days fully covers MA20/RSI12/KD9; MACD-OSC will differ
# slightly from broker apps that load multi-year history.
WEB_WARMUP_DAYS = 60

# How many days of dated cache files to keep on disk. Anything older is
# purged at startup. Per-stock incremental updates also rely on a recent
# cache to skip the full 13-month refetch, so don't drop below ~7.
CACHE_RETENTION_DAYS = 7

# Filename prefixes that carry a _YYYYMMDD.json suffix and are safe to purge.
# 'taiex_manual.json' has no date suffix and is the only persistent override.
_DATED_CACHE_PREFIXES = (
    "taiex_", "t86_", "t86otc_", "companies_", "companies_otc_",
)

# Per-stock locks so concurrent requests for the same code don't double-fetch.
_fetch_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fetch_locks[key] = lock
        return lock


def _parse_cache_date(path: Path) -> date | None:
    """Extract the YYYYMMDD date from a cache filename, or None if it has no
    date suffix. Matches both per-stock files (`2330_20260508.json`) and
    prefixed files (`taiex_20260508.json`)."""
    stem = path.stem
    tag = stem.rsplit("_", 1)[-1]
    if not (tag.isdigit() and len(tag) == 8):
        return None
    try:
        return datetime.strptime(tag, "%Y%m%d").date()
    except ValueError:
        return None


def _purge_old_caches(retention_days: int = CACHE_RETENTION_DAYS) -> int:
    """Delete dated cache files older than `retention_days`. Returns the
    number of files removed. Skips `taiex_manual.json` (no date suffix)."""
    cutoff = _trading_day() - timedelta(days=retention_days)
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        d = _parse_cache_date(path)
        if d is None or d >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# 對應的交易日 = 今天日曆日,週末回推至週五。盤後資料若尚未產出,
# TAIEX 會缺值,前端會在 taiexBar 提示使用者手動填入。
def _trading_day(now: datetime | None = None) -> date:
    now = now or datetime.now()
    d = now.date()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _today_tag() -> str:
    return _trading_day().strftime("%Y%m%d")


def _today_iso() -> str:
    return _trading_day().isoformat()


def _stock_cache(code: str) -> Path:
    return CACHE_DIR / f"{code}_{_today_tag()}.json"


def _taiex_cache() -> Path:
    return CACHE_DIR / f"taiex_{_today_tag()}.json"


# Market identifiers — drives fetcher routing, T86 source, and frontend label.
MARKET_TWSE = "twse"   # 上市
MARKET_OTC = "otc"     # 上櫃


# Summary labels emitted by `_summary()`. Shared with backtest scripts
# (backtest/study.py SIGNAL_DEFS, backtest/exit_rules.py BAD_SUMMARIES)
# so a label-string change here propagates automatically instead of
# silently producing empty event samples. Keys are stable identifiers;
# values are the user-facing strings, free to evolve.
SUMMARY_LABELS = {
    "strong":     "🟢 多頭擴張",
    "sub-strong": "🟢 多頭發展",
    "reversal":   "🔵 反彈訊號",
    "exit":       "🔴 趨勢轉弱",
    "watch":      "🟠 訊號分歧",
    "wait":       "🟡 盤整中",
}


# TAIEX regime classification (mirrors backtest/bear_regime_test.py).
# Trailing-60-day drawdown >= TAIEX_BEAR_THRESH means today is bear;
# otherwise bull. Used to adjust chip emphasis live: LEAD's edge
# inverts in bear regime (backtest n=70, 40d alpha -0.76% vs +1.58%
# in bull), so we mute it; AVOID + reversal+綠 keep or strengthen.
TAIEX_BEAR_THRESH = 0.10
TAIEX_LOOKBACK = 60


def _taiex_regime_from_rows(rows: list[dict]) -> str | None:
    """Classify today's TAIEX regime from a stock's row series.

    Uses the per-bar `taiex` field that _compute_rows attaches. Returns
    None if too few TAIEX points are available (e.g. very new listings).
    """
    if not rows:
        return None
    tvals = [r.get("taiex") for r in rows[-TAIEX_LOOKBACK:]
             if r.get("taiex") is not None]
    if len(tvals) < 5:
        return None
    peak = max(tvals)
    cur = tvals[-1]
    if peak <= 0:
        return None
    dd = (cur - peak) / peak
    return "bear" if dd <= -TAIEX_BEAR_THRESH else "bull"


def _taiex_regime_today() -> str | None:
    """Classify today's TAIEX regime from the TAIEX cache (no specific
    stock series needed). Used by /api/taiex/today.
    """
    cache = _taiex_cache()
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    # Overlay any manual override (matches /api/taiex/today behaviour).
    manual = _load_taiex_manual()
    if manual:
        merged = dict(data)
        merged.update(manual)
        data = merged
    sorted_pairs = sorted(data.items())
    last_n = sorted_pairs[-TAIEX_LOOKBACK:]
    vals = [v for _, v in last_n if v is not None]
    if len(vals) < 5:
        return None
    peak = max(vals)
    if peak <= 0:
        return None
    cur = vals[-1]
    dd = (cur - peak) / peak
    return "bear" if dd <= -TAIEX_BEAR_THRESH else "bull"


T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
T86_OTC_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
T86_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}

# Minimum 4-digit-stock count we expect in a healthy T86 dump. TWSE
# occasionally serves a truncated response (~780-810 4-digit codes
# vs the usual ~1070) — these are missing rows for many real stocks,
# not just warrants. We reject those and let the caller retry.
# OTC normally has ~800-825 4-digit codes; below ~700 is suspect.
T86_TWSE_MIN_STOCKS = 900
T86_OTC_MIN_STOCKS = 700


def _t86_looks_complete(out: dict, market: str) -> bool:
    """Heuristic: does this T86 dump appear to be a full snapshot?

    Counts 4-digit stock codes (typical TWSE/OTC listings) and compares
    against a market-specific threshold. Truncated dumps tend to be
    missing 200-300 listings even though they still include warrants
    and ETFs, so total entry count alone isn't enough.
    """
    if not out:
        return False
    n_4digit = sum(1 for c in out if len(c) == 4 and c.isdigit())
    threshold = (T86_TWSE_MIN_STOCKS if market == MARKET_TWSE
                 else T86_OTC_MIN_STOCKS)
    return n_4digit >= threshold


def _t86_cache(date_compact: str, market: str = MARKET_TWSE) -> Path:
    prefix = "t86" if market == MARKET_TWSE else "t86otc"
    return CACHE_DIR / f"{prefix}_{date_compact}.json"


def _t86_cached_only(date_iso: str, market: str = MARKET_TWSE) -> dict | None:
    """Return cached T86 dict for the date, or None if not cached. No network.

    Also returns None if the cached dump looks truncated — a defensive
    catch in case an older partial-response cache slipped in before
    the write-time guard was added.
    """
    cache = _t86_cache(date_iso.replace("-", ""), market)
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not _t86_looks_complete(data, market):
        return None
    return data


def _fetch_t86(date_iso: str, market: str = MARKET_TWSE) -> dict:
    """Return {code: {f, t, d, tot}} of net shares per stock for date_iso.

    Empty dict on non-trading days or fetch errors. Only non-empty results
    are cached — empty caches are ignored on load so a later retry can
    succeed after a transient API failure.
    Net values are in shares (not lots).
    """
    date_compact = date_iso.replace("-", "")
    cache = _t86_cache(date_compact, market)
    if cache.exists():
        try:
            with cache.open() as f:
                data = json.load(f)
            if data:  # skip empty cache — allow re-fetch
                return data
        except (OSError, json.JSONDecodeError):
            pass
    with _lock_for(f"__t86_{market}_{date_compact}__"):
        if cache.exists():
            try:
                with cache.open() as f:
                    data = json.load(f)
                if data:
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        if market == MARKET_TWSE:
            out = _fetch_t86_twse(date_compact)
        else:
            out = _fetch_t86_otc(date_iso)
        # Reject truncated responses so the cache stays clean and the
        # next call retries. Returning {} here matches the "non-trading
        # day" path — callers already tolerate empty result.
        if out and not _t86_looks_complete(out, market):
            print(f"  [warn] T86 {market} {date_compact} looks truncated "
                  f"({sum(1 for c in out if len(c)==4 and c.isdigit())} "
                  f"4-digit stocks); not caching",
                  file=sys.stderr)
            return {}
        if out:  # only persist complete results
            try:
                with cache.open("w") as f:
                    json.dump(out, f)
            except OSError:
                pass
        return out


def _fetch_t86_twse(date_compact: str) -> dict:
    try:
        resp = requests.get(
            T86_URL,
            params={"date": date_compact, "selectType": "ALL", "response": "json"},
            headers=T86_HTTP_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    out: dict = {}
    if data.get("stat") == "OK":
        for row in data.get("data", []):
            try:
                code = (row[0] or "").strip()
                if not code:
                    continue
                foreign = int((row[4] or "0").replace(",", ""))
                trust = int((row[10] or "0").replace(",", ""))
                dealer = int((row[11] or "0").replace(",", ""))
                total = int((row[18] or "0").replace(",", ""))
                out[code] = {"f": foreign, "t": trust, "d": dealer, "tot": total}
            except (IndexError, ValueError):
                continue
    return out


def _fetch_t86_otc(date_iso: str) -> dict:
    """TPEx 三大法人買賣明細 (24-col schema).

    Columns verified against live API:
        [0]  代號
        [1]  名稱
        [10] 外資合計買賣超股數
        [13] 投信買賣超股數
        [22] 自營商合計買賣超股數
        [23] 三大法人合計買賣超股數
    """
    try:
        resp = requests.get(
            T86_OTC_URL,
            params={"type": "Daily", "sect": "EW", "date": date_iso.replace("-", "/")},
            headers=T86_HTTP_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    out: dict = {}
    tables = data.get("tables") or []
    rows = tables[0].get("data") if tables else []
    for row in rows:
        try:
            code = (row[0] or "").strip()
            if not code:
                continue
            foreign = int((row[10] or "0").replace(",", ""))
            trust = int((row[13] or "0").replace(",", ""))
            dealer = int((row[22] or "0").replace(",", ""))
            total = int((row[23] or "0").replace(",", ""))
            out[code] = {"f": foreign, "t": trust, "d": dealer, "tot": total}
        except (IndexError, ValueError):
            continue
    return out


TAIEX_MANUAL_FILE = CACHE_DIR / "taiex_manual.json"
_taiex_manual_lock = threading.Lock()


def _load_taiex_manual() -> dict:
    """Manual TAIEX overrides keyed by ISO date string (persists across days)."""
    if not TAIEX_MANUAL_FILE.exists():
        return {}
    try:
        with TAIEX_MANUAL_FILE.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_taiex_manual(data: dict) -> None:
    with TAIEX_MANUAL_FILE.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _overlay_manual(parsed: dict) -> dict:
    """Overlay manual overrides onto an auto-fetched {date: value} dict."""
    manual = _load_taiex_manual()
    for k, v in manual.items():
        try:
            parsed[datetime.fromisoformat(k).date()] = v
        except ValueError:
            continue
    return parsed


def _load_taiex(target_rows: int) -> dict:
    cache = _taiex_cache()
    if cache.exists():
        with cache.open() as f:
            data = json.load(f)
        parsed = {datetime.fromisoformat(k).date(): v for k, v in data.items()}
        return _overlay_manual(parsed)

    with _lock_for("__taiex__"):
        if cache.exists():
            with cache.open() as f:
                data = json.load(f)
            parsed = {datetime.fromisoformat(k).date(): v for k, v in data.items()}
            return _overlay_manual(parsed)
        rows = twse.fetch_recent_rows(twse.fetch_taiex_month, target_rows, "TAIEX")
        parsed = twse.parse_taiex_rows(rows)
        serial = {d.isoformat(): v for d, v in parsed.items()}
        with cache.open("w") as f:
            json.dump(serial, f)
        return _overlay_manual(parsed)


def _adx_wilder(highs: list, lows: list, closes: list, n: int = 14) -> list:
    """ADX(n) using Wilder smoothing. Aligned to closes; None where insufficient."""
    L = len(closes)
    out = [None] * L
    if L < 2 * n + 1:
        return out
    tr = [0.0] * L
    plus_dm = [0.0] * L
    minus_dm = [0.0] * L
    for i in range(1, L):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl = highs[i - 1], lows[i - 1]
        if None in (h, l, pc, ph, pl):
            # propagate gap by treating as zero movement; rare in practice
            continue
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    tr_n = sum(tr[1:n + 1])
    pdm_n = sum(plus_dm[1:n + 1])
    mdm_n = sum(minus_dm[1:n + 1])
    dx_list = []
    def _dx(tr_, pdm_, mdm_):
        if tr_ <= 0:
            return 0.0
        pdi = 100 * pdm_ / tr_
        mdi = 100 * mdm_ / tr_
        denom = pdi + mdi
        return 100 * abs(pdi - mdi) / denom if denom else 0.0
    dx_list.append(_dx(tr_n, pdm_n, mdm_n))
    for i in range(n + 1, L):
        tr_n = tr_n - tr_n / n + tr[i]
        pdm_n = pdm_n - pdm_n / n + plus_dm[i]
        mdm_n = mdm_n - mdm_n / n + minus_dm[i]
        dx_list.append(_dx(tr_n, pdm_n, mdm_n))
    if len(dx_list) < n:
        return out
    adx_v = sum(dx_list[:n]) / n
    out[2 * n - 1] = adx_v
    for j in range(n, len(dx_list)):
        adx_v = (adx_v * (n - 1) + dx_list[j]) / n
        out[n + j] = adx_v
    return out


def _compute_rows(series: list[dict], taiex_close: dict) -> list[dict]:
    closes = [pt["close"] for pt in series]
    highs = [pt["high"] for pt in series]
    lows = [pt["low"] for pt in series]

    ma5 = twse.sma(closes, 5)
    ma10 = twse.sma(closes, 10)
    ma20 = twse.sma(closes, 20)
    ma60 = twse.sma(closes, 60)
    rsi6 = twse.rsi_wilder(closes, 6)
    rsi12 = twse.rsi_wilder(closes, 12)
    k_vals, d_vals = twse.kd(highs, lows, closes, 9)
    _dif, _sig, osc = twse.macd(closes, 12, 26, 9)
    chg = twse.pct_change(closes)
    adx = _adx_wilder(highs, lows, closes, 14)

    out = []
    for i, pt in enumerate(series):
        out.append({
            "date": pt["date"].isoformat(),
            "taiex": taiex_close.get(pt["date"]),
            "high": pt["high"],
            "low": pt["low"],
            "close": pt["close"],
            "lots": pt["lots"],
            "change_pct": chg[i],
            "ma5": ma5[i],
            "ma10": ma10[i],
            "ma20": ma20[i],
            "ma60": ma60[i],
            "rsi6": rsi6[i],
            "rsi12": rsi12[i],
            "kd_k": k_vals[i],
            "kd_d": d_vals[i],
            "macd_osc": osc[i],
            "adx": adx[i],
        })
    return out


def _backfill_adx(rows: list[dict]) -> list[dict]:
    """Compute ADX in-place for rows from older cache files that lack it."""
    if not rows or "adx" in rows[-1]:
        return rows
    highs = [r.get("high") for r in rows]
    lows = [r.get("low") for r in rows]
    closes = [r.get("close") for r in rows]
    adx = _adx_wilder(highs, lows, closes, 14)
    for i, r in enumerate(rows):
        r["adx"] = adx[i]
    return rows


def _find_recent_stock_cache(code: str, max_age_days: int = CACHE_RETENTION_DAYS
                             ) -> tuple[list[dict], str | None] | None:
    """Locate the freshest non-empty per-stock cache for `code` in the last
    `max_age_days` trading-day tags (excluding today). Returns (rows, market)
    or None. Used by `_load_stock` to do incremental refresh instead of a
    full 13-month refetch."""
    today = _trading_day()
    candidates = []
    for path in CACHE_DIR.glob(f"{code}_*.json"):
        d = _parse_cache_date(path)
        if d is None or d >= today:
            continue
        if (today - d).days > max_age_days:
            continue
        candidates.append((d, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    for _, path in candidates:
        try:
            with path.open() as f:
                payload = json.load(f)
            rows = payload.get("rows") or []
            if rows:
                return _backfill_adx(rows), payload.get("market")
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _load_stock(code: str, output_rows: int) -> list[dict]:
    cache = _stock_cache(code)
    if cache.exists():
        with cache.open() as f:
            return _backfill_adx(json.load(f).get("rows") or [])

    with _lock_for(code):
        if cache.exists():
            with cache.open() as f:
                return _backfill_adx(json.load(f).get("rows") or [])

        # Always fetch enough to satisfy the largest reasonable request,
        # so a single day's cache covers any output window the user picks.
        target = max(output_rows, 200) + WEB_WARMUP_DAYS
        prior = _find_recent_stock_cache(code)
        market = (prior[1] if prior else None) or _market_for(code) or MARKET_TWSE
        if market == MARKET_OTC:
            month_fetcher = lambda y, m: twse.fetch_otc_month(y, m, code)
        else:
            month_fetcher = lambda y, m: twse.fetch_stock_month(y, m, code)

        if prior is not None:
            prior_rows = prior[0]
            last_iso = prior_rows[-1]["date"]
            last_date = datetime.fromisoformat(last_iso).date()
            now = datetime.now()
            # Pull the current month, plus the prior month if last_date crosses
            # a month boundary (covers month-flip refresh).
            raw = month_fetcher(now.year, now.month)
            if last_date.year != now.year or last_date.month != now.month:
                prev_y, prev_m = twse.previous_month(now.year, now.month)
                raw = month_fetcher(prev_y, prev_m) + raw
            new_series = twse.parse_stock_rows(raw)
            old_dates = {r["date"] for r in prior_rows}
            base_series = [{
                "date": datetime.fromisoformat(r["date"]).date(),
                "high": r["high"], "low": r["low"],
                "close": r["close"], "lots": r["lots"],
            } for r in prior_rows]
            additions = [s for s in new_series
                         if s["date"].isoformat() not in old_dates]
            if not additions:
                # Nothing new — write today's cache pointing at the same series
                # so subsequent calls skip the lookup.
                taiex = _load_taiex(len(base_series))
                rows = _compute_rows(base_series, taiex)
                with cache.open("w") as f:
                    json.dump({"code": code, "market": market, "rows": rows}, f)
                return rows
            combined = base_series + additions
            combined.sort(key=lambda s: s["date"])
            taiex = _load_taiex(len(combined))
            rows = _compute_rows(combined, taiex)
            with cache.open("w") as f:
                json.dump({"code": code, "market": market, "rows": rows}, f)
            return rows

        # No usable prior cache — full 13-month fetch.
        raw = twse.fetch_recent_rows(month_fetcher, target, code)
        if not raw:
            return []
        series = twse.parse_stock_rows(raw)
        if not series:
            return []
        taiex = _load_taiex(target)
        rows = _compute_rows(series, taiex)
        with cache.open("w") as f:
            json.dump({"code": code, "market": market, "rows": rows}, f)
        return rows


def _stock_cache_market(code: str) -> str | None:
    """Read the persisted market from today's cache, if any."""
    cache = _stock_cache(code)
    if not cache.exists():
        return None
    try:
        with cache.open() as f:
            return json.load(f).get("market")
    except (OSError, json.JSONDecodeError):
        return None


# ---- 7-step dashboard (mirrors stock.xlsx per-stock dashboard) -------------
#
# Lights are returned as tokens, not emojis, so the frontend can style them:
#   "green" | "yellow" | "red" | "gray" (insufficient data) | "none" (informational)
#
# All step calculations use a 20-day window ending at the most recent row,
# matching the cell ranges in stock.xlsx (B6:B25 etc.).


def _sigma(window):
    """Sample stddev of last 19 daily change %, divided by 100 (xlsx N32)."""
    changes = [r["change_pct"] for r in window[-19:] if r["change_pct"] is not None]
    if len(changes) < 2:
        return None
    return statistics.stdev(changes) / 100.0


def _divergence(window):
    """Detect price-vs-RSI6 divergence over the last 20 bars.

    Splits the window into a "recent" tail (last 5 bars) and a "prior"
    band (10 bars before that). Compares price highs/lows with the
    matching RSI6 highs/lows.

      - 頂背離 (bearish): price makes new high but RSI doesn't follow.
      - 底背離 (bullish): price makes new low but RSI holds higher.

    Returns {"kind": "bearish"|"bullish"|None, "detail": str|None}.
    The kind is used in step 3 to soft-downgrade green→yellow on
    bearish divergence (a real warning), and surfaced as an alert chip
    for either direction.
    """
    if len(window) < 15:
        return {"kind": None, "detail": None}
    recent = window[-5:]
    prior = window[-15:-5]
    rp = [(r.get("close"), r.get("rsi6")) for r in recent]
    pp = [(r.get("close"), r.get("rsi6")) for r in prior]
    rp = [(c, s) for c, s in rp if c is not None and s is not None]
    pp = [(c, s) for c, s in pp if c is not None and s is not None]
    if len(rp) < 3 or len(pp) < 5:
        return {"kind": None, "detail": None}

    r_high_close, r_high_rsi = max(rp, key=lambda x: x[0])
    p_high_close, p_high_rsi = max(pp, key=lambda x: x[0])
    r_low_close, r_low_rsi = min(rp, key=lambda x: x[0])
    p_low_close, p_low_rsi = min(pp, key=lambda x: x[0])

    # Require at least a clear price move (0.5%) and an RSI gap of 3pt
    # to avoid noise-level matches near consolidation.
    if (r_high_close > p_high_close * 1.005
            and r_high_rsi < p_high_rsi - 3):
        return {
            "kind": "bearish",
            "detail": (f"頂背離:價 {p_high_close:.2f}→{r_high_close:.2f} "
                       f"但 RSI6 {p_high_rsi:.0f}→{r_high_rsi:.0f}"),
        }
    if (r_low_close < p_low_close * 0.995
            and r_low_rsi > p_low_rsi + 3):
        return {
            "kind": "bullish",
            "detail": (f"底背離:價 {p_low_close:.2f}→{r_low_close:.2f} "
                       f"但 RSI6 {p_low_rsi:.0f}→{r_low_rsi:.0f}"),
        }
    return {"kind": None, "detail": None}


def _step_1_market(window, last):
    taiex_today = last.get("taiex")
    taiex_vals = [r["taiex"] for r in window if r.get("taiex") is not None]
    base = {"step": 1, "title": "大盤過濾", "condition": "大盤 > 自身 MA20"}
    if taiex_today is None or len(taiex_vals) < 5:
        return {**base, "light": "gray", "detail": "大盤資料不足"}
    avg = sum(taiex_vals) / len(taiex_vals)
    light = "green" if taiex_today > avg else "red"
    detail = f"大盤={taiex_today:,.0f}  MA={avg:,.0f}"
    return {**base, "light": light, "detail": detail}


def _step_2_trend(window, last):
    ma10 = last.get("ma10")
    ma20 = last.get("ma20")
    ma60 = last.get("ma60")
    adx = last.get("adx")
    # 5-day linear regression slope on MA20 — avoids 2-point jitter at flat tops
    ma20_vals = [row.get("ma20") for row in window[-5:]]
    base = {"step": 2, "title": "趨勢結構",
            "condition": "MA10>MA20>MA60 + MA20 五日回歸斜率↑ + ADX>20"}
    if any(v is None for v in (ma10, ma20, ma60)) or None in ma20_vals or len(ma20_vals) < 5:
        return {**base, "light": "gray", "detail": "資料不足"}
    n = len(ma20_vals)
    x_bar = (n - 1) / 2.0
    y_bar = sum(ma20_vals) / n
    cov = sum((i - x_bar) * (ma20_vals[i] - y_bar) for i in range(n))
    var = sum((i - x_bar) ** 2 for i in range(n))  # = 10.0 for n=5
    lr_slope = cov / var if var else 0.0
    c_slope = lr_slope > 0
    c_stack = (ma10 > ma20) and (ma20 > ma60)
    c_partial = ma10 > ma20
    # ADX > 20 = 趨勢成形；< 20 視為盤整，降一級為黃燈
    c_adx = (adx is not None) and (adx > 20)
    if c_stack and c_slope and c_adx:
        light = "green"
    elif c_stack and c_slope:
        light = "yellow"  # MA + slope OK but 盤整 (ADX 弱)
    elif c_partial and c_slope:
        light = "yellow"
    elif c_partial or c_slope:
        light = "yellow"
    else:
        light = "red"
    trend = "多頭排列" if c_stack else ("多頭" if c_partial else "空頭")
    adx_str = f"ADX={adx:.1f}" if adx is not None else "ADX=NA"
    detail = (f"MA10={ma10:.1f}  MA20={ma20:.1f}  MA60={ma60:.1f}  "
              f"{trend}  斜率:{'↑' if c_slope else '↓'}({lr_slope:+.2f})  "
              f"{adx_str}{'(趨勢成形)' if c_adx else '(盤整)'}")
    return {**base, "light": light, "detail": detail}


def _step_3_momentum(window, last, prev, divergence=None):
    needed = (last.get("ma5"), last.get("ma10"), last.get("rsi6"),
              last.get("kd_k"), last.get("kd_d"),
              prev.get("rsi6"), prev.get("kd_k"), prev.get("kd_d"))
    base = {"step": 3, "title": "動能三合",
            "condition": "MA5>MA10 + K>D + RSI6>50\nRSI6 自<30反彈首破50 / KD 低位金叉 / K首破50"}
    if any(v is None for v in needed):
        return {**base, "light": "gray", "detail": "資料不足"}
    ma5, ma10, rsi6, k, d = (last["ma5"], last["ma10"], last["rsi6"],
                              last["kd_k"], last["kd_d"])
    rsi6_p, k_p, d_p = prev["rsi6"], prev["kd_k"], prev["kd_d"]

    c_ma = ma5 > ma10
    c_kd = k > d
    c_rsi = rsi6 > 50

    rsi6_recent = [r["rsi6"] for r in window[-11:-1] if r.get("rsi6") is not None]
    rebounded_from_oversold = bool(rsi6_recent) and min(rsi6_recent) < 30

    kd_low_zone = (k_p < 20) or (40 <= k_p < 50)
    kd_golden_low = (k_p < d_p) and (k > d) and kd_low_zone
    rsi6_first_50 = (rsi6_p < 50) and (rsi6 >= 50) and rebounded_from_oversold
    k_first_50 = (k_p < 50) and (k >= 50)
    trigger = kd_golden_low or rsi6_first_50 or k_first_50

    if c_ma and c_kd and c_rsi and trigger:
        light = "green"
    elif c_ma and c_kd and c_rsi:
        light = "yellow"
    elif sum([c_ma, c_kd, c_rsi]) >= 2:
        light = "yellow"
    else:
        light = "red"

    # 頂背離 → soft-downgrade green (bullish flow with hidden weakness).
    # 底背離 doesn't upgrade — it's a heads-up, not a confirmation.
    div_tag = ""
    if divergence and divergence.get("kind") == "bearish":
        if light == "green":
            light = "yellow"
        div_tag = "  ⚠頂背離"
    elif divergence and divergence.get("kind") == "bullish":
        div_tag = "  💡底背離"

    kd_cross_today = "✓" if (k_p < d_p and k > d) else "✗"
    suffix = "(自<30反彈首破50)" if rsi6_first_50 else ""
    detail = (f"MA金叉:{'✓' if c_ma else '✗'}  "
              f"KD金叉:{kd_cross_today}(K={k:.0f})  "
              f"RSI6:{rsi6:.0f}{suffix}{div_tag}")
    return {**base, "light": light, "detail": detail}


def _step_4_volume(window, last, prev, s3_light, s6_light):
    base = {"step": 4, "title": "量能",
            "condition": "≥1.5 × 5日均量,或連2日價漲量增"}
    vol = last.get("lots")
    close = last.get("close")
    close_p = prev.get("close")
    vol_p = prev.get("lots")
    recent = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]
    if vol is None or close is None or close_p is None or len(recent) < 3:
        return {**base, "light": "gray", "detail": "資料不足"}
    avg = sum(recent) / len(recent)
    ratio = vol / avg if avg > 0 else 0
    price_up_vol_down = (close > close_p) and (vol_p is not None and vol < vol_p)

    prev2 = window[-3] if len(window) >= 3 else {}
    close_p2 = prev2.get("close")
    vol_p2 = prev2.get("lots")
    two_day_pv_up = (
        vol_p is not None and close_p2 is not None and vol_p2 is not None
        and close > close_p and vol > vol_p
        and close_p > close_p2 and vol_p > vol_p2
    )

    burst = vol >= 1.5 * avg
    healthy = vol >= avg

    if s3_light == "green":
        if burst or two_day_pv_up:
            light = "green"
        elif healthy:
            light = "yellow"
        else:
            light = "red"
    elif s6_light == "green":
        if price_up_vol_down:
            light = "red"
        elif healthy:
            light = "green"
        else:
            light = "yellow"
    else:
        if burst or two_day_pv_up:
            light = "green"
        elif healthy:
            light = "yellow"
        else:
            light = "red"

    warn = "  ⚠價漲量縮" if price_up_vol_down else ""
    pv_tag = "  連2日價漲量增" if two_day_pv_up else ""
    detail = f"今量={vol:,}  5日均={avg:,.0f}  比值={ratio:.2f}x{pv_tag}{warn}"
    return {**base, "light": light, "detail": detail}


def _stoploss_levels(last, sigma):
    """Return three stop-loss rows for the 距離預警 area (not a signal step).

    緊 = MA10 動態, 中 = 固定 -X%(依 σ), 鬆 = MA20 動態.
    """
    ma10 = last.get("ma10")
    ma20 = last.get("ma20")
    close = last.get("close")
    if any(v is None for v in (ma10, ma20, close)) or sigma is None:
        return []
    if sigma < 0.025:
        pct = 5
    elif sigma < 0.04:
        pct = 6
    else:
        pct = 8
    mid = close * (1 - pct / 100)
    rows = [
        ("🟢 緊停損", ma10, "MA10 動態停損"),
        ("🟡 中停損", mid, f"固定 -{pct}% (σ={sigma * 100:.1f}%)"),
        ("🔴 鬆停損", ma20, "MA20 動態停損"),
    ]
    out = []
    for label, target, note in rows:
        out.append({
            "label": label,
            "target": round(target, 1),
            "delta_pct": round((target - close) / close * 100, 2),
            "note": note,
        })
    return out


def _step_6_holding(window, last, prev):
    base = {"step": 6, "title": "持有訊號",
            "condition": "收盤 > MA5;跌破需連2日 或 帶量跌破"}
    close, ma5 = last.get("close"), last.get("ma5")
    close_p, ma5_p = prev.get("close"), prev.get("ma5")
    if any(v is None for v in (close, ma5, close_p, ma5_p)):
        return {**base, "light": "gray", "detail": "資料不足"}

    vol = last.get("lots")
    recent_vols = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]
    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else None
    vol_surge = (vol is not None and avg_vol is not None
                 and avg_vol > 0 and vol >= 1.5 * avg_vol)

    if close > ma5:
        light, note = "green", "守穩"
    elif close < ma5 and close_p < ma5_p:
        light, note = "red", "連2日跌破"
    elif close < ma5 and vol_surge:
        light, note = "red", "帶量跌破"
    else:
        light, note = "yellow", "單日跌破(警示)"
    detail = f"收盤={close:.1f}  MA5={ma5:.1f}  {note}"
    return {**base, "light": light, "detail": detail}


def _step_7_exit(window, last, prev):
    """4 conditions → ≥2 = red, =1 = yellow, =0 = green."""
    base = {"step": 7, "title": "出場警示", "condition": "4 取 2"}
    needed_keys = ("close", "rsi6", "lots", "ma5", "kd_k", "kd_d")
    if any(last.get(k) is None for k in needed_keys):
        return {**base, "light": "gray", "detail": "資料不足"}
    if any(prev.get(k) is None for k in ("close", "ma5", "kd_k", "kd_d")):
        return {**base, "light": "gray", "detail": "資料不足"}

    prev10 = window[-11:-1] if len(window) >= 11 else window[:-1]
    closes_p10 = [r["close"] for r in prev10 if r.get("close") is not None]
    rsi6_p10 = [r["rsi6"] for r in prev10 if r.get("rsi6") is not None]
    vol_p10 = [r["lots"] for r in prev10 if r.get("lots") is not None]
    if not (closes_p10 and rsi6_p10 and vol_p10):
        return {**base, "light": "gray", "detail": "歷史資料不足"}

    close, rsi6, vol = last["close"], last["rsi6"], last["lots"]
    ma5 = last["ma5"]
    close_p, ma5_p = prev["close"], prev["ma5"]
    k_p, d_p = prev["kd_k"], prev["kd_d"]
    k, d = last["kd_k"], last["kd_d"]
    osc, osc_p = last.get("macd_osc"), prev.get("macd_osc")

    macd_shrinking = (osc is not None and osc_p is not None
                      and osc > 0 and osc_p > 0 and osc < osc_p)
    rsi_div = ((close > max(closes_p10)) and (rsi6 < max(rsi6_p10))
               and macd_shrinking)
    vol_div = (close > max(closes_p10)) and (vol < max(vol_p10))
    ma5_down = (close < ma5) and (ma5 < ma5_p)
    kd_high_dead = ((k_p > d_p) and (k < d) and (k_p > 80)
                    and (close < close_p))

    triggered = sum([rsi_div, vol_div, ma5_down, kd_high_dead])
    if triggered >= 2:
        light = "red"
    elif triggered == 1:
        light = "yellow"
    else:
        light = "green"

    detail = (f"RSI背離+MACD縮:{'✓' if rsi_div else '✗'}  "
              f"量價背離:{'✓' if vol_div else '✗'}  "
              f"MA5翻下:{'✓' if ma5_down else '✗'}  "
              f"KD高死叉+價跌:{'✓' if kd_high_dead else '✗'}")
    return {**base, "light": light, "detail": detail}


def _step_8_institutional(window, code, market=MARKET_TWSE, cached_only=False):
    """5 日三大法人籌碼 → 法人認養度。

    依據文件「動態法人認養度」概念,但暫不依市值動態調權重,
    以「外資買超力道 + 投信連買 + 三大合計淨流向」三條件綜合判斷:
      綠 = 5 日三大合計淨買 且 (外資力道 ≥1% 或 投信連買 ≥3 日)
      紅 = 5 日三大合計淨賣 且 (外資或投信連賣 ≥3 日)
      黃 = 其他

    cached_only=True skips the network and reads only persisted T86 — used
    when computing historical lights so old dates don't trigger fresh fetches.
    """
    base = {"step": 8, "title": "法人認養", "condition": "5 日三大法人籌碼"}
    if len(window) < 5:
        return {**base, "light": "gray", "detail": "資料不足"}
    last5 = window[-5:]

    days = []
    for r in last5:
        date_iso = r.get("date")
        if not date_iso:
            continue
        if cached_only:
            t86 = _t86_cached_only(date_iso, market)
            if t86 is None:
                continue
            info = t86.get(code)
        else:
            info = _fetch_t86(date_iso, market).get(code)
        if info is None:
            continue
        lots = r.get("lots") or 0
        days.append({
            "date": date_iso,
            "f": info["f"], "t": info["t"], "tot": info["tot"],
            "vol_shares": lots * 1000,
        })
    if len(days) < 3:
        return {**base, "light": "gray", "detail": "法人資料不足"}

    sum_f = sum(d["f"] for d in days)
    sum_t = sum(d["t"] for d in days)
    sum_tot = sum(d["tot"] for d in days)
    sum_vol = sum(d["vol_shares"] for d in days) or 1
    foreign_strength = sum_f / sum_vol * 100  # %

    def _streak(values):
        """Return (buy_streak, sell_streak) counted from the latest day backward."""
        buy = sell = 0
        for v in reversed(values):
            if v > 0 and sell == 0:
                buy += 1
            elif v < 0 and buy == 0:
                sell += 1
            else:
                break
        return buy, sell

    f_buy, f_sell = _streak([d["f"] for d in days])
    t_buy, t_sell = _streak([d["t"] for d in days])

    bullish = (sum_tot > 0) and (foreign_strength >= 1.0 or t_buy >= 3)
    bearish = (sum_tot < 0) and (f_sell >= 3 or t_sell >= 3)
    if bullish:
        light = "green"
    elif bearish:
        light = "red"
    else:
        light = "yellow"

    def _streak_txt(buy, sell):
        if buy:
            return f"連買{buy}日"
        if sell:
            return f"連賣{sell}日"
        return "持平"

    detail = (
        f"5日合計:{sum_tot/1000:+,.0f}張  "
        f"外資力道:{foreign_strength:+.2f}%  "
        f"外資{_streak_txt(f_buy, f_sell)}  "
        f"投信{_streak_txt(t_buy, t_sell)}"
    )
    return {**base, "light": light, "detail": detail}


def _compute_alerts(window, code=None, market=MARKET_TWSE,
                    divergence=None, cached_only=False,
                    steps=None, history=None,
                    reversal_quality=None,
                    topping_quality=None,
                    taiex_regime=None):
    """Return a list of alert chips for the current view.

    Alerts are observations layered on top of the 7-step lights — they
    don't gate any signal, they just surface stuff a trader would
    glance at: 爆量 / 量縮 / 法人連 N 日同向 / 背離 / 法人未確認 /
    反轉品質+法人綠.

    Each entry is {kind, icon, text, tone} where tone ∈ {info,warn,danger}.
    `cached_only` skips T86 network fetches (passed True from the history
    strip if we ever extend alerts there; current call sites use False).
    `steps` is today's 7 lights; `history` is the last ~15 days of lights
    used to detect red-regime-exit / green-regime-entry context for the
    institutional-confirmation chips. `reversal_quality` is today's score
    used for the 4★/5★ + 法人綠 chip.
    """
    alerts: list[dict] = []

    # --- Volume burst / dry-up vs last 20 bars (excluding today) ---
    # volume_burst_active is also captured so the AVOID + reversal chips
    # below can amplify their tone/text when 爆量 co-fires — backtest/
    # cross_chip_results.md showed +/- 4-5pp deltas vs the chip alone.
    volume_burst_active = False
    vols = [r.get("lots") for r in window[:-1] if r.get("lots") is not None]
    today_vol = window[-1].get("lots") if window else None
    if today_vol and len(vols) >= 5:
        recent20 = vols[-20:]
        avg = sum(recent20) / len(recent20)
        if avg > 0:
            ratio = today_vol / avg
            if len(recent20) >= 5:
                sd = statistics.stdev(recent20)
            else:
                sd = 0
            if today_vol > avg + 2 * sd and ratio >= 1.5:
                volume_burst_active = True
                alerts.append({
                    "kind": "volume_burst",
                    "icon": "🔥",
                    "tone": "warn",
                    "text": f"爆量 ({ratio:.1f}x 20日均, {today_vol:,}張)",
                })
            elif ratio < 0.5:
                alerts.append({
                    "kind": "volume_dry",
                    "icon": "💤",
                    "tone": "info",
                    "text": f"量縮 ({ratio:.1f}x 20日均, {today_vol:,}張)",
                })

    # --- Institutional consecutive-direction streaks (last 10 dates) ---
    if code and len(window) >= 3:
        recent_dates = [r.get("date") for r in window[-10:] if r.get("date")]
        f_vals: list[int] = []
        t_vals: list[int] = []
        for date_iso in recent_dates:
            if cached_only:
                t86 = _t86_cached_only(date_iso, market)
                info = t86.get(code) if t86 else None
            else:
                info = _fetch_t86(date_iso, market).get(code)
            if not info:
                continue
            f_vals.append(info.get("f", 0))
            t_vals.append(info.get("t", 0))

        def _streak(values):
            buy = sell = 0
            for v in reversed(values):
                if v > 0 and sell == 0:
                    buy += 1
                elif v < 0 and buy == 0:
                    sell += 1
                else:
                    break
            return buy, sell

        if f_vals:
            fb, fs = _streak(f_vals)
            if fb >= 5:
                alerts.append({"kind": "foreign_buy_streak", "icon": "🏦",
                               "tone": "info",
                               "text": f"外資連買 {fb} 日"})
            elif fs >= 5:
                alerts.append({"kind": "foreign_sell_streak", "icon": "🏦",
                               "tone": "warn",
                               "text": f"外資連賣 {fs} 日"})
        if t_vals:
            tb, ts = _streak(t_vals)
            if tb >= 4:
                alerts.append({"kind": "trust_buy_streak", "icon": "📈",
                               "tone": "info",
                               "text": f"投信連買 {tb} 日"})
            elif ts >= 4:
                alerts.append({"kind": "trust_sell_streak", "icon": "📉",
                               "tone": "warn",
                               "text": f"投信連賣 {ts} 日"})

    # --- AVOID (institutional non-confirmation) ---
    # Backed by backtest/red_recovery.py + backtest/green_entry.py and
    # re-validated on the 50-stock universe (build_stats output):
    #   - Red-regime exit (>=5 days at >=3 reds, then drop) while 法人 still
    #     red, OR green-regime entry (>=3 greens after >=5 quiet days) while
    #     both 法人 and 量能 are non-green: pooled 40d alpha −1.89% / 44%,
    #     bear-regime sub-sample −3.19% (ROBUST). n=866 across 50 codes.
    # The LEAD branch (green entry with 法人 green but 量能 non-green) used
    # to fire on the original 30-stock sample at +1.2% / 54%, but on the
    # 50-stock universe collapsed to +0.06% / 50% (essentially noise).
    # Removed in production; build_stats still computes it for reference.
    # INST_IDX/VOL_IDX match the order in steps[] = [s1, s2, s3, s4, s6, s7, s8]
    # which renders as UI steps 1..7 = [大盤, 趨勢, 動能, 量能, 持有, 出場, 法人].
    INST_IDX, VOL_IDX = 6, 3
    RED_THRESH, RED_DAYS = 3, 5
    GREEN_THRESH, QUIET_DAYS = 3, 5
    if steps and history and len(history) >= max(RED_DAYS, QUIET_DAYS) + 1:
        hist_lights = [h.get("lights") or [] for h in history]
        if all(len(L) > max(INST_IDX, VOL_IDX) for L in hist_lights):
            def _count(L, color):
                return sum(1 for x in L if x == color)
            hist_red = [_count(L, "red") for L in hist_lights]
            hist_green = [_count(L, "green") for L in hist_lights]
            today_inst = steps[INST_IDX]["light"] if len(steps) > INST_IDX else None
            today_vol = steps[VOL_IDX]["light"] if len(steps) > VOL_IDX else None

            # Red-regime exit: today red_count dropped below threshold after
            # RED_DAYS consecutive >=threshold bars. history[-1] is today.
            red_exit = (
                hist_red[-1] < RED_THRESH
                and len(hist_red) >= RED_DAYS + 1
                and all(c >= RED_THRESH for c in hist_red[-RED_DAYS - 1:-1])
            )
            green_entry = (
                hist_green[-1] >= GREEN_THRESH
                and len(hist_green) >= QUIET_DAYS + 1
                and all(c < GREEN_THRESH for c in hist_green[-QUIET_DAYS - 1:-1])
            )

            # AVOID + 爆量 combo: cross-chip study shows 40d alpha
            # deepens to -6.4% / 42% (n=38) vs -1.84% for AVOID alone.
            # Upgrade tone to danger and tag the chip when both fire.
            def _avoid_chip(detail: str) -> dict:
                amp = volume_burst_active
                return {
                    "kind": "inst_not_confirmed",
                    "icon": "⚠",
                    "tone": "danger" if amp else "warn",
                    "text": (f"法人未確認+爆量 ({detail})"
                             if amp else f"法人未確認 ({detail})"),
                    "stat_key": "inst_not_confirmed",
                    "combo_amp": "volume_burst" if amp else None,
                }
            if red_exit and today_inst == "red":
                alerts.append(_avoid_chip("紅燈深陷期退出, 法人仍紅"))
            elif green_entry:
                inst_green = today_inst == "green"
                vol_green = today_vol == "green"
                if not inst_green and not vol_green:
                    alerts.append(_avoid_chip("綠燈進場, 法人量能皆非綠"))
                # LEAD (inst_green && !vol_green) intentionally not emitted:
                # 50-stock universe shows no pooled edge (+0.06% / 50%).

    # --- Reversal-quality + 法人綠 confirmation ---
    # backtest/reversal_quality_study.py on the 50-stock universe:
    #   - score==5 + 法人=綠  → 40d alpha +2.3% / 57%. Bull/bear split
    #     reveals the edge is bear-only: bull -0.1% (n=81) vs bear
    #     +4.16% / 67% (n=69). When today is bull regime the chip is
    #     near-noise; when bear regime it's the strongest reversal cell.
    #   - score==4 + 法人=綠  → 40d alpha +2.6% / 57%. Bull-leaning
    #     (bull +3.3% vs bear +1.7%). Step 5 (持有) yellow subset
    #     is null (-0.1% / 47% on n=53, ~15% of events); excluded
    #     below so the chip's pool stat stays sharp (~+3.1%).
    #     n=411 → ~358 after exclusion — the steadiest reversal tier.
    # score==3 was tried as a chip but collapsed to -0.03% / 50% on
    # the 50-stock universe (no edge), so we keep the threshold ≥4.
    HOLD_IDX = 4  # step 5 持有 in steps[]
    if (steps and reversal_quality
            and reversal_quality.get("score") is not None
            and len(steps) > INST_IDX):
        score = reversal_quality["score"]
        inst_light = steps[INST_IDX]["light"]
        hold_light = steps[HOLD_IDX]["light"] if len(steps) > HOLD_IDX else None
        # 4★: also exclude step 5 = yellow (the transitional / noisy
        # subset that drags the chip's median to zero). 5★ doesn't
        # show the same pattern — its 持有=yellow bucket is too thin
        # to matter (n=9, 6%) and reads positive.
        gate_ok = (
            inst_light == "green"
            and (score == 5 or (score == 4 and hold_light != "yellow"))
        )
        if gate_ok and score >= 4:
            stars = "★" * score
            # 反轉+法人到位 + 爆量 combo: cross-chip study shows
            # 4★+爆量 → +4.8% / 69% (n=29) vs +1.3% alone; 5★+爆量 →
            # +1.8% / 60% (n=30) vs +0.8%. Tag and prefix the chip.
            amp = volume_burst_active
            chip = {
                "kind": "reversal_inst_confirm",
                "icon": "🔥" if amp else "✨",
                "tone": "info",
                "text": (f"反轉 {stars}+法人到位+爆量"
                         if amp else f"反轉 {stars}+法人到位"),
                "stat_key": f"reversal_inst_confirm_{score}",
                "combo_amp": "volume_burst" if amp else None,
            }
            # 4★ sub-shape annotation: which of the 5 conditions is
            # missing materially shifts forward alpha.
            # backtest/reversal_4star_missing_study.py on 50-stock
            # universe (n=302 qualifying events):
            #   missing C1 (not at 20d low)  → 40d +7.74% / 71% (n=34)
            #   missing C2 (no prior drop)   → 40d -5.21% / 14% (n= 7)
            #   missing C3 (K not <20)       → 40d +2.04% / 55% (n=135)
            #   missing C4 (RSI6 not <35)    → 40d +18.6% / 88% (n=  8)
            #   missing C5 (vol not ≥1.0x)   → 40d +2.58% / 61% (n=116)
            # vs baseline +2.99% / 59%. Only C1-miss (起跑型) and
            # C2-miss (假反轉) have both meaningful sample (>20) and
            # large deviation; C4-miss looks huge but n=8 is too thin
            # to ship as a callout. C3 and C5 are near baseline.
            if score == 4 and reversal_quality.get("checks"):
                checks_ = reversal_quality["checks"]
                missing_idx = next(
                    (i for i, c in enumerate(checks_) if not c["passed"]),
                    None)
                if missing_idx == 0:
                    chip["shape_note"] = {
                        "tone": "good",
                        "text": "起跑型 (價已抬離20日低): "
                                "40d +7.7% / 71% (n=34)",
                    }
                elif missing_idx == 1:
                    chip["shape_note"] = {
                        "tone": "warn",
                        "text": "假反轉型 (前期未真跌): "
                                "40d -5.2% / 14% (n=7,稀有)",
                    }
            alerts.append(chip)

    # --- Topping-quality conditional chips ---
    # backtest/topping_quality_study.py on the 50-stock universe
    # (runup threshold raised to ≥15% on 2026-05; check #2 was too
    # permissive at ≥5% with 59% bar-level pass rate, which made the
    # 5★ score under-discriminating):
    #
    # 5★ + 法人=red → 5d alpha -2.72% / 33% (n=46 after K>80 raise
    #   from K>75; was -2.79% / 32% n=57). Short-horizon bearish,
    #   signal dies past 20d. Per-stock pool is thin (5 codes with
    #   n≥3) so most stocks fall back to pool stats.
    # 5★ + 法人=yellow → 20d alpha +3.76% / 61% (n=144 after K>80
    #   raise; was +3.66% / 61% n=184). Continuation configuration —
    #   overbought + institutions holding fire usually resolves up,
    #   not down. Per-stock n≥3 on 26 codes (reliable per-stock
    #   coverage). UI labelled "強勢延伸" not "topping".
    # 4★ + 法人=red → flat at ≥5%, untested at ≥15% but the per-stock
    #   asymmetry was the bigger issue. Skipped.
    # 3★ + 法人=red → diluted per-stock at ≥5%. Skipped.
    #
    # Mirror of reversal_inst_confirm: same exact-score gating logic.
    if (steps and topping_quality
            and topping_quality.get("score") is not None
            and len(steps) > INST_IDX):
        t_score = topping_quality["score"]
        inst_light = steps[INST_IDX]["light"]
        if t_score == 5 and inst_light == "red":
            alerts.append({
                "kind": "topping_inst_red",
                "icon": "⚠",
                "tone": "danger",
                "text": "高點 ★★★★★+法人未確認 (5日內短期警示)",
                "stat_key": "topping_inst_red_5",
            })
        elif t_score == 5 and inst_light == "yellow":
            alerts.append({
                "kind": "topping_inst_yellow",
                "icon": "📈",
                "tone": "info",
                "text": "強勢延伸 ★★★★★+法人觀望 (20日續攻訊號)",
                "stat_key": "topping_inst_yellow_5",
            })

    # --- Divergence (re-using already-computed result from step 3) ---
    if divergence and divergence.get("kind") == "bearish":
        # stat_key lets the backtest-stats card pull per-stock history
        # for this chip. Pool-level alpha is ~0, but per-stock readings
        # split cleanly into "true topper" vs "trend-continue" stocks
        # (range -16% to +13% at 40d in `backtest/bearish_div_study.py`).
        # Headline horizon is 10d — bull-market drift washes the signal
        # out by 20d.
        alerts.append({
            "kind": "bearish_divergence", "icon": "⚠",
            "tone": "danger",
            "text": divergence.get("detail") or "頂背離",
            "stat_key": "bearish_divergence",
        })
    elif divergence and divergence.get("kind") == "bullish":
        alerts.append({
            "kind": "bullish_divergence", "icon": "💡",
            "tone": "info",
            "text": divergence.get("detail") or "底背離",
        })

    # --- Enrichment: streak count + industry-conditional notes ---
    # backtest/ studies (see commit log) showed:
    #
    # 1. Multi-day chip streaks change forward alpha materially:
    #    - 反轉 4★+綠 streak 4+ days: +7.5% / 85% (n=13) — strong
    #      conviction signal vs the single-day base of +2.0% / 57%
    #    - 反轉 5★+綠 streak 2-3 days: -3.3% / 47% — inverts!
    #    - 高點 5★+紅 streak 2-3 days: -3.4% / 20% — deepens
    #    UI surfaces the streak count so the user reads the chip
    #    with the right intensity.
    #
    # 2. Per-industry chip performance varies dramatically:
    #    - 通信網路業 is a "chip-failure" sector for reversal +
    #      strong-extension chips (5★+綠 reads -6.7% / 19% win!)
    #    - 光電業 reversal_4 underperforms (-7.3pp); bearish_div
    #      OUTPERFORMS (-2.7% vs ~0% pool) — actual bearish signal
    #    - 半導體 + 電腦及週邊 are the chip-friendly sectors
    INDUSTRY_CHIP_NOTES: dict[str, dict[str, dict]] = {
        "通信網路業": {
            "reversal_inst_confirm_4": {"warn": True,
                "text": "通信網路業:此 chip 弱於 pool (-3.7% vs +3.0%)"},
            "reversal_inst_confirm_5": {"warn": True,
                "text": "通信網路業:此 chip 為反指標 (-6.7% / 19% win)"},
            "topping_inst_yellow_5": {"warn": True,
                "text": "通信網路業:此 chip 弱於 pool (-1.4% vs +3.8%)"},
        },
        "光電業": {
            "reversal_inst_confirm_4": {"warn": True,
                "text": "光電業:此 chip 弱於 pool (-4.4% vs +3.0%)"},
            "bearish_divergence": {"warn": False,
                "text": "光電業:頂背離為真空頭訊號 (-2.7% / 39% win,他業近零)"},
        },
    }
    HIGH_CONVICTION_STREAK = {
        # chip_key → (min_streak, badge text)
        "reversal_inst_confirm_4": (4, "高確信"),
    }
    if history and len(history) >= 2:
        # Look up industry once for this code (cached, cheap)
        try:
            industry = (_company_info(code) or {}).get("industry") if code else None
        except Exception:
            industry = None
        for a in alerts:
            sk = a.get("stat_key")
            if not sk:
                continue
            # Streak: count consecutive trailing days where this
            # chip also fired. history[-1] is today (the bar we're
            # alerting on); walk backwards from history[-2].
            streak = 1
            for i in range(len(history) - 2, -1, -1):
                h = history[i]
                if sk in (h.get("chip_keys") or []):
                    streak += 1
                else:
                    break
            if streak >= 2:
                a["streak"] = streak
                hc = HIGH_CONVICTION_STREAK.get(sk)
                if hc and streak >= hc[0]:
                    a["high_conviction"] = True
            # Industry-conditional note
            if industry:
                note = INDUSTRY_CHIP_NOTES.get(industry, {}).get(sk)
                if note:
                    a["industry_note"] = note

    return alerts


def _summary(steps):
    greens = sum(1 for s in steps if s["light"] == "green")
    yellows = sum(1 for s in steps if s["light"] == "yellow")
    reds = sum(1 for s in steps if s["light"] == "red")
    total = len(steps)
    s1_light = steps[0]["light"]
    s2_light = steps[1]["light"]
    s3_light = steps[2]["light"]
    s4_light = steps[3]["light"]
    s7_light = steps[5]["light"]  # 出場警示 (after dropping 停損 from steps)
    s8_light = steps[6]["light"] if len(steps) >= 7 else "gray"  # 法人認養
    first_4_green = sum(1 for s in steps[:4] if s["light"] == "green")

    # Labels describe the current market state, not an action.
    # Backtest (backtest/study.py) showed no signal predicts forward
    # alpha across stocks/horizons, so these are observation tags.
    if (s1_light == "green" and first_4_green == 4
            and s7_light != "red" and s8_light != "red"):
        light, label = "green", SUMMARY_LABELS["strong"]
    elif (s1_light == "green" and first_4_green >= 3
            and s7_light != "red" and s8_light != "red"):
        light, label = "green", SUMMARY_LABELS["sub-strong"]
    elif (s1_light == "green" and s3_light == "green"
            and s2_light != "green" and s4_light != "green"
            and s7_light != "red" and s8_light != "red"):
        light, label = "blue", SUMMARY_LABELS["reversal"]
    elif s7_light == "red":
        light, label = "red", SUMMARY_LABELS["exit"]
    elif reds >= 2:
        light, label = "orange", SUMMARY_LABELS["watch"]
    else:
        light, label = "yellow", SUMMARY_LABELS["wait"]

    return {
        "light": light, "label": label,
        "passed": greens, "warning": yellows, "danger": reds,
        "total": total,
        "score": f"通過 {greens} / {total}  警示 {yellows}  危險 {reds}",
    }


def _price_zones(summary, last, sigma, s6_light):
    close = last.get("close")
    ma5 = last.get("ma5")
    ma10 = last.get("ma10")
    if close is None or sigma is None:
        return {"mode": "wait", "zones": [], "note": "資料不足"}

    light = summary["light"]
    is_buy = light == "green"
    is_sell = (light == "red") or (light == "orange" and s6_light == "red")

    if is_buy:
        zones = [
            {"name": "🟢 第一支撐", "basis": "-1σ 統計下緣",
             "low": round(close * (1 - sigma * 1.2), 1),
             "high": round(close * (1 - sigma * 0.8), 1)},
            {"name": "🟡 第二支撐", "basis": "-1.5σ 跌深區",
             "low": round(close * (1 - sigma * 1.8), 1),
             "high": round(close * (1 - sigma * 1.4), 1)},
            {"name": "🔵 強支撐", "basis": "-2σ 統計下緣",
             "low": round(close * (1 - sigma * 2.4), 1),
             "high": round(close * (1 - sigma * 2.0), 1)},
        ]
        note = (f"燈號偏多 → 顯示下方支撐區(收盤 {close:.1f}、"
                f"σ={sigma*100:.2f}%);僅供觀察,非進場建議")
        return {"mode": "buy", "zones": zones, "note": note}

    if is_sell and ma5 is not None and ma10 is not None:
        zones = [
            {"name": "🟡 第一壓力", "basis": "MA5 壓力",
             "low": round(min(ma5, close * (1 + sigma * 0.8)), 1),
             "high": round(max(ma5, close * (1 + sigma * 1.2)), 1)},
            {"name": "🔴 第二壓力", "basis": "MA10 強壓",
             "low": round(min(ma10, close * (1 + sigma * 1.4)), 1),
             "high": round(max(ma10, close * (1 + sigma * 1.8)), 1)},
            {"name": "⚠️ 關鍵壓力", "basis": "站上此區燈號可能轉強",
             "low": round(max(ma10, close * (1 + sigma * 2.0)), 1),
             "high": round(close * (1 + sigma * 2.4), 1)},
        ]
        note = (f"燈號偏空 → 顯示上方壓力區(收盤 {close:.1f}、"
                f"σ={sigma*100:.2f}%);僅供觀察,非出場建議")
        return {"mode": "sell", "zones": zones, "note": note}

    return {
        "mode": "wait", "zones": [],
        "note": f"目前燈號為「{summary['label']}」,訊號分歧,儀表板僅作觀察",
    }


def _distance(last, sigma):
    close = last.get("close")
    ma5 = last.get("ma5")
    if close is None or sigma is None:
        return []
    buy_target = round(close * (1 - sigma), 1)
    sell_base = max(ma5, close * (1 + sigma)) if ma5 is not None else close * (1 + sigma)
    sell_target = round(sell_base, 1)
    buy_pct = (buy_target - close) / close * 100
    sell_pct = (sell_target - close) / close * 100
    return [
        {"label": "🟢 至支撐區", "target": buy_target,
         "delta_pct": round(buy_pct, 2),
         "note": f"再跌 {abs(buy_pct):.1f}% 至 {buy_target} 進入支撐區"},
        {"label": "🔴 至壓力區", "target": sell_target,
         "delta_pct": round(sell_pct, 2),
         "note": f"再漲 {abs(sell_pct):.1f}% 至 {sell_target} 進入壓力區"},
    ]


def _compute_steps(window, code, market=MARKET_TWSE, t86_cached_only=False,
                   divergence=None):
    """Run the 7 step checks on a 20-day window. Returns the steps list with
    UI step numbers (1..7) assigned. Window must have ≥2 rows.

    `divergence` can be passed in to share the result with the alert
    layer; if None, it's computed fresh."""
    last = window[-1]
    prev = window[-2]
    if divergence is None:
        divergence = _divergence(window)
    s1 = _step_1_market(window, last)
    s2 = _step_2_trend(window, last)
    s3 = _step_3_momentum(window, last, prev, divergence=divergence)
    s6 = _step_6_holding(window, last, prev)
    s4 = _step_4_volume(window, last, prev, s3["light"], s6["light"])
    s7 = _step_7_exit(window, last, prev)
    if code:
        s8 = _step_8_institutional(window, code, market=market,
                                   cached_only=t86_cached_only)
    else:
        s8 = {"step": 0, "title": "法人認養", "condition": "5 日三大法人籌碼",
              "light": "gray", "detail": "需股票代碼"}
    steps = [s1, s2, s3, s4, s6, s7, s8]
    for i, s in enumerate(steps, 1):
        s["step"] = i
    return steps


HISTORY_DAYS = 15


def _history_lights(full_rows, code, market=MARKET_TWSE, days=HISTORY_DAYS):
    """For each of the last `days` trading days, recompute the 7 lights and
    the overall summary light using a 20-day window ending at that day.
    Prefetches the T86 dates needed by step 8 so the institutional light
    is populated for every history row, not just where cache happened to
    exist. T86 is whole-market data shared across stocks, so the cost is
    paid once per day across the whole watchlist.

    Each row also includes `chip_keys`: list of stat_key-bearing chips
    that would have emitted on that bar. AVOID is excluded — its
    trigger requires a separate window-walk over prior days that we
    don't want to replicate inside the 15-day strip. The
    quality-based chips (REV-4/5, TOP-RED/YEL) and BEAR-DIV are
    pure window-state functions and cheap to recompute here.
    """
    if not full_rows:
        return []
    start = max(1, len(full_rows) - days)
    needed_t86_start = max(0, start - 4)
    for r in full_rows[needed_t86_start:]:
        date_iso = r.get("date")
        if date_iso:
            _fetch_t86(date_iso, market)
    out = []
    prev_rev_score = None
    prev_top_score = None
    prev_div_kind = None
    for end_idx in range(start, len(full_rows)):
        sub = full_rows[: end_idx + 1]
        window = sub[-20:]
        if len(window) < 2:
            continue
        steps = _compute_steps(window, code, market=market, t86_cached_only=True)
        summary = _summary(steps)
        chip_keys: list[str] = []
        INST_STEP = 6  # step 7 法人 index in steps[]
        HOLD_STEP = 4  # step 5 持有 index in steps[]
        if len(window) >= 20 and steps and len(steps) > INST_STEP:
            inst_light = steps[INST_STEP]["light"]
            hold_light = steps[HOLD_STEP]["light"] if len(steps) > HOLD_STEP else None
            rq = _reversal_quality(window)
            tq = _topping_quality(window)
            div = _divergence(window) or {}
            # Reversal chip — exact-score first-cross at 4 or 5 with 法人=綠.
            # 4★ additionally excludes 持有=yellow (matches the live
            # emission rule in _compute_alerts).
            if rq and rq.get("score") in (4, 5) and rq["score"] != prev_rev_score:
                rev_ok = inst_light == "green" and (
                    rq["score"] == 5 or hold_light != "yellow"
                )
                if rev_ok:
                    chip_keys.append(f"reversal_inst_confirm_{rq['score']}")
            prev_rev_score = rq.get("score") if rq else None
            # Topping chip — exact-score 5 first-cross with 法人=red/yellow
            if tq and tq.get("score") == 5 and prev_top_score != 5:
                if inst_light == "red":
                    chip_keys.append("topping_inst_red_5")
                elif inst_light == "yellow":
                    chip_keys.append("topping_inst_yellow_5")
            prev_top_score = tq.get("score") if tq else None
            # Bearish divergence first-cross
            kind = div.get("kind")
            if kind == "bearish" and prev_div_kind != "bearish":
                chip_keys.append("bearish_divergence")
            prev_div_kind = kind
        out.append({
            "date": window[-1]["date"],
            "lights": [s["light"] for s in steps],
            "overall": summary["light"],
            "overall_label": summary["label"],
            "chip_keys": chip_keys,
        })
    return out


def _reversal_quality(window: list[dict]) -> dict | None:
    """Score how 'reversal-shaped' the current bar is, 0..5.

    Reverse-engineered from backtest/find_winners.py on 2395/5388/2357 —
    these are the conditions that, on average, distinguish reversal lows
    that went on to rally ≥15-20% within 60 days from those that didn't.
    The score is intentionally an OBSERVATION AID, not an entry signal:
    no single combination has cross-stock alpha. Hide on UI when score
    is 0-1.
    """
    if len(window) < 20:
        return None
    last = window[-1]
    close = last.get("close")
    if close is None:
        return None
    closes20 = [r["close"] for r in window[-20:] if r.get("close") is not None]
    highs20 = [r["high"] for r in window[-20:] if r.get("high") is not None]
    if len(closes20) < 20 or not highs20:
        return None
    min_close_20 = min(closes20)
    peak_high_20 = max(highs20)
    drawdown_pct = (close - peak_high_20) / peak_high_20 * 100
    near_low_pct = (close - min_close_20) / min_close_20 * 100  # ≥ 0

    k = last.get("kd_k")
    rsi6 = last.get("rsi6")
    lots = last.get("lots")
    lots5 = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]

    checks = []
    # 1. close 在 20 日低點附近 (≤2%)
    checks.append({
        "name": "近 20 日低點 (≤2%)",
        "passed": near_low_pct <= 2.0,
        "detail": f"距 20 日低 +{near_low_pct:.1f}%",
    })
    # 2. 前期跌幅 ≥7.5%
    # Tightened from ≥5% on 2026-05. Pass rate at ≥5% was 51.6% —
    # essentially "any stock that pulled back at all". Threshold sweep
    # showed ≥7.5% is the sweet spot: 4★+綠 +0.7pp (+1.93→+2.61%),
    # 5★+綠 +0.7pp (+1.64→+2.30%) at 40d, both with minimal sample
    # loss. ≥10% degrades 4★ (over-filters), ≥15% degrades 4★ further
    # while 5★ keeps strengthening (asymmetric sweet spot — kept
    # symmetric here to avoid splitting the score into score-specific
    # rules). Note this isn't symmetric with topping's ≥15% runup
    # because reversal's check #1 (≤2% from low) is itself more
    # restrictive than topping's check #1 in a bull universe.
    checks.append({
        "name": "前期跌幅 ≥7.5%",
        "passed": drawdown_pct <= -7.5,
        "detail": f"DD20={drawdown_pct:+.1f}%",
    })
    # 3. K < 20 (KD 超賣)
    # Tightened from K<25 on 2026-05 after threshold sweep showed
    # K<20 strengthens the validated chips (反轉 5★ +0.6pp, 4★ +0.2pp
    # at 40d alpha) with modest sample loss. K<15 over-filters and
    # hurts 4★. K=20 sits at the K-value P10 across 50-stock pool.
    checks.append({
        "name": "K 超賣 (<20)",
        "passed": k is not None and k < 20,
        "detail": f"K={k:.1f}" if k is not None else "K 資料不足",
    })
    # 4. RSI6 < 35
    checks.append({
        "name": "RSI6 偏低 (<35)",
        "passed": rsi6 is not None and rsi6 < 35,
        "detail": f"RSI6={rsi6:.1f}" if rsi6 is not None else "RSI6 資料不足",
    })
    # 5. 量比 ≥ 1.0
    if lots is not None and lots5:
        avg = sum(lots5) / len(lots5)
        ratio = (lots / avg) if avg > 0 else 0
        c5_pass = ratio >= 1.0
        c5_detail = f"量比={ratio:.2f}x"
    else:
        c5_pass = False
        c5_detail = "量資料不足"
    checks.append({"name": "量比 ≥1.0", "passed": c5_pass, "detail": c5_detail})

    score = sum(1 for c in checks if c["passed"])
    STARS = ["—", "★", "★★", "★★★", "★★★★", "★★★★★"]
    DESCS = [
        "未呈反轉特徵",
        "條件不足",
        "部分條件成立",
        "中等反轉條件",
        "良好反轉條件",
        "高品質反轉位置",
    ]
    return {
        "score": score,
        "max": 5,
        "stars": STARS[score],
        "desc": DESCS[score],
        "checks": checks,
        "note": "觀察用,非進場依據",
    }


def _topping_quality(window: list[dict]) -> dict | None:
    """Score how 'topping-shaped' the current bar is, 0..5.

    180° mirror of `_reversal_quality()`: near 20d HIGH instead of low,
    recent rally instead of drawdown, K/RSI6 overbought instead of
    oversold, volume either burst (出貨) or dry (價漲量縮背離).

    Backtest (`backtest/topping_quality_study.py` on 50-stock pool)
    showed pool-level alpha is flat at 40d — but conditioned on
    step 7 法人 = red, score == 5 produces a clean short-horizon
    bearish signal: -1.01% / 39% win at 5d (n=120), with 3:1 per-stock
    negative asymmetry. That's the basis for the topping_inst_red_5
    chip emitted by `_compute_alerts`.

    Like `_reversal_quality`, this is OBSERVATION AID — the
    unconditional score has no edge. The chip is only fired with the
    +法人=red filter applied.
    """
    if len(window) < 20:
        return None
    last = window[-1]
    close = last.get("close")
    if close is None:
        return None
    closes20 = [r["close"] for r in window[-20:] if r.get("close") is not None]
    lows20 = [r["low"] for r in window[-20:] if r.get("low") is not None]
    highs20 = [r["high"] for r in window[-20:] if r.get("high") is not None]
    if len(closes20) < 20 or not lows20 or not highs20:
        return None
    peak_high_20 = max(highs20)
    min_low_20 = min(lows20)
    near_high_pct = (peak_high_20 - close) / peak_high_20 * 100  # ≥ 0
    runup_pct = (close - min_low_20) / min_low_20 * 100  # ≥ 0

    k = last.get("kd_k")
    rsi6 = last.get("rsi6")
    lots = last.get("lots")
    lots5 = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]

    checks = []
    # 1. close 在 20 日高點附近 (≤2%)
    checks.append({
        "name": "近 20 日高點 (≤2%)",
        "passed": near_high_pct <= 2.0,
        "detail": f"距 20 日高 -{near_high_pct:.1f}%",
    })
    # 2. 前期漲幅 ≥15%
    # Originally ≥5% (mirror of reversal's ≥5% drawdown), but empirical
    # pass rate was 58.9% — essentially "any stock that isn't completely
    # flat", which made the check non-discriminating. Threshold raise to
    # ≥15% (pass rate ~18% on 50-stock pool) sharpens the 5★ signal:
    # 5★+red @ 5d -1.01% → -1.88%, 5★+yellow @ 20d +1.96% → +3.13%.
    # See `backtest/topping_quality_study.py` threshold sweep.
    checks.append({
        "name": "前期漲幅 ≥15%",
        "passed": runup_pct >= 15.0,
        "detail": f"自 20 日低 +{runup_pct:.1f}%",
    })
    # 3. K > 80 (KD 超買)
    # Tightened from K>75 on 2026-05 (paired with reversal's K<20 cut).
    # 強勢延伸 5★+黃 @ 20d strengthens +0.5pp (+3.13 → +3.67),
    # 高點 5★+紅 @ 5d holds steady. K>85 thins samples to 31 for the
    # red chip without enough signal gain to justify. K=80 sits at
    # the K-value P90 across 50-stock pool.
    checks.append({
        "name": "K 超買 (>80)",
        "passed": k is not None and k > 80,
        "detail": f"K={k:.1f}" if k is not None else "K 資料不足",
    })
    # 4. RSI6 > 65
    checks.append({
        "name": "RSI6 偏高 (>65)",
        "passed": rsi6 is not None and rsi6 > 65,
        "detail": f"RSI6={rsi6:.1f}" if rsi6 is not None else "RSI6 資料不足",
    })
    # 5. 量比 ≥ 1.0 (爆量出貨) OR ≤0.7 (價漲量縮背離)
    if lots is not None and lots5:
        avg = sum(lots5) / len(lots5)
        ratio = (lots / avg) if avg > 0 else 0
        c5_pass = (ratio >= 1.0) or (ratio < 0.7)
        c5_detail = f"量比={ratio:.2f}x"
    else:
        c5_pass = False
        c5_detail = "量資料不足"
    checks.append({"name": "量比 出貨/背離", "passed": c5_pass, "detail": c5_detail})

    score = sum(1 for c in checks if c["passed"])
    STARS = ["—", "★", "★★", "★★★", "★★★★", "★★★★★"]
    DESCS = [
        "未呈續攻特徵",
        "條件不足",
        "部分條件成立",
        "中等續攻條件",
        "良好續攻條件",
        "高品質續攻位置",
    ]
    return {
        "score": score,
        "max": 5,
        "stars": STARS[score],
        "desc": DESCS[score],
        "checks": checks,
        "note": "觀察用,5★+法人狀態才有 chip 訊號",
    }


def compute_dashboard(full_rows: list[dict], code: str | None = None,
                      market: str = MARKET_TWSE, *,
                      compact: bool = False) -> dict:
    """Compute the dashboard payload from a stock's row series.

    `compact=True` skips the 15-day history strip, alerts, price zones,
    distance and stop-loss panel — used by the watchlist card, which
    only renders today's 7-light state + summary + reversal_quality.
    Drops per-stock cost from ~3,150 step computations (15 days × 7
    lights + alerts walk) down to a single 7-step compute, which is
    what dominates watchlist load time on a 20-30 code list.
    """
    if not full_rows:
        return {"as_of": None, "sigma": None, "steps": [],
                "summary": None, "price_zones": None, "distance": [],
                "history": [], "reversal_quality": None, "alerts": []}
    window = full_rows[-20:]
    last = window[-1]
    if len(window) < 2:
        return {"as_of": last["date"], "sigma": None, "steps": [],
                "summary": None, "price_zones": None, "distance": [],
                "history": [], "reversal_quality": None, "alerts": []}
    sigma = _sigma(window)
    divergence = _divergence(window)
    steps = _compute_steps(window, code, market=market,
                           t86_cached_only=False, divergence=divergence)
    summary = _summary(steps)
    reversal = _reversal_quality(window)
    topping = _topping_quality(window)

    if compact:
        return {
            "as_of": last["date"],
            "sigma": sigma,
            "steps": steps,
            "summary": summary,
            "reversal_quality": reversal,
            "topping_quality": topping,
            "price_zones": None,
            "distance": [],
            "history": [],
            "alerts": [],
        }

    s6 = steps[4]  # 持有
    zones = _price_zones(summary, last, sigma, s6["light"])
    distance = _distance(last, sigma) + _stoploss_levels(last, sigma)
    history = _history_lights(full_rows, code, market=market)
    taiex_regime = _taiex_regime_from_rows(full_rows)
    alerts = _compute_alerts(window, code=code, market=market,
                             divergence=divergence, cached_only=False,
                             steps=steps, history=history,
                             reversal_quality=reversal,
                             topping_quality=topping,
                             taiex_regime=taiex_regime)

    return {
        "as_of": last["date"],
        "sigma": sigma,
        "steps": steps,
        "summary": summary,
        "reversal_quality": reversal,
        "topping_quality": topping,
        "price_zones": zones,
        "distance": distance,
        "history": history,
        "alerts": alerts,
        "taiex_regime": taiex_regime,
    }


# ---- Company info (TWSE OpenAPI) -------------------------------------------
#
# Pulls 公司簡稱 + 產業類別 from the TWSE listed-company basic-info endpoint
# once per day. The full list is ~1MB and rarely changes, so a single daily
# fetch is plenty.

COMPANY_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
COMPANY_INFO_URL_OTC = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
COMPANY_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}

# TWSE 產業別代碼 → 中文名稱。OpenAPI 回傳的是代碼,自己對照。
INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險業",
    "18": "貿易百貨", "19": "綜合", "20": "其他業",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證",
}

_company_lock = threading.Lock()
_company_cache_mem = {"date": None, "twse": {}, "otc": {}}


def _company_cache_path(market: str = MARKET_TWSE) -> Path:
    suffix = "" if market == MARKET_TWSE else "_otc"
    return CACHE_DIR / f"companies{suffix}_{_today_tag()}.json"


def _fetch_companies_twse() -> dict:
    cache = _company_cache_path(MARKET_TWSE)
    if cache.exists():
        try:
            with cache.open() as f:
                rows = json.load(f)
            return {r["code"]: r for r in rows}
        except (OSError, json.JSONDecodeError):
            pass
    with _company_lock:
        if cache.exists():
            try:
                with cache.open() as f:
                    rows = json.load(f)
                return {r["code"]: r for r in rows}
            except (OSError, json.JSONDecodeError):
                pass
        try:
            resp = requests.get(COMPANY_INFO_URL,
                                headers=COMPANY_HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            raw = resp.json()
        except (requests.RequestException, ValueError):
            return {}
        out = {}
        for row in raw:
            code = (row.get("公司代號") or "").strip()
            if not code:
                continue
            ind_code = (row.get("產業別") or "").strip()
            ind_name = INDUSTRY_NAMES.get(ind_code, ind_code)
            out[code] = {
                "code": code,
                "short_name": (row.get("公司簡稱") or "").strip(),
                "industry": ind_name,
                "industry_code": ind_code,
                "market": MARKET_TWSE,
            }
        try:
            with cache.open("w") as f:
                json.dump(list(out.values()), f, ensure_ascii=False)
        except OSError:
            pass
        return out


def _fetch_companies_otc() -> dict:
    cache = _company_cache_path(MARKET_OTC)
    if cache.exists():
        try:
            with cache.open() as f:
                rows = json.load(f)
            return {r["code"]: r for r in rows}
        except (OSError, json.JSONDecodeError):
            pass
    with _company_lock:
        if cache.exists():
            try:
                with cache.open() as f:
                    rows = json.load(f)
                return {r["code"]: r for r in rows}
            except (OSError, json.JSONDecodeError):
                pass
        try:
            resp = requests.get(COMPANY_INFO_URL_OTC,
                                headers=COMPANY_HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            raw = resp.json()
        except (requests.RequestException, ValueError):
            return {}
        out = {}
        for row in raw:
            code = (row.get("SecuritiesCompanyCode") or "").strip()
            if not code:
                continue
            ind_code = (row.get("SecuritiesIndustryCode") or "").strip()
            # TPEx codes are 1-2 digits; pad to match TWSE 2-digit map.
            ind_key = ind_code.zfill(2) if ind_code.isdigit() else ind_code
            ind_name = INDUSTRY_NAMES.get(ind_key, ind_code)
            out[code] = {
                "code": code,
                "short_name": (row.get("CompanyAbbreviation") or "").strip(),
                "industry": ind_name,
                "industry_code": ind_code,
                "market": MARKET_OTC,
            }
        try:
            with cache.open("w") as f:
                json.dump(list(out.values()), f, ensure_ascii=False)
        except OSError:
            pass
        return out


def _refresh_company_cache() -> None:
    today = _today_tag()
    if _company_cache_mem["date"] != today:
        _company_cache_mem["twse"] = _fetch_companies_twse()
        _company_cache_mem["otc"] = _fetch_companies_otc()
        _company_cache_mem["date"] = today


def _company_info(code: str) -> dict:
    _refresh_company_cache()
    info = _company_cache_mem["twse"].get(code)
    if info:
        return info
    info = _company_cache_mem["otc"].get(code)
    if info:
        return info
    # Unknown code — fall back to whatever today's stock cache claims.
    market = _stock_cache_market(code) or ""
    return {"code": code, "short_name": "", "industry": "",
            "industry_code": "", "market": market}


def _market_for(code: str) -> str | None:
    """Look up a code's market via the daily company maps. Returns
    MARKET_TWSE / MARKET_OTC, or None if the code isn't in either."""
    _refresh_company_cache()
    if code in _company_cache_mem["twse"]:
        return MARKET_TWSE
    if code in _company_cache_mem["otc"]:
        return MARKET_OTC
    # If we already fetched data for this code today, trust the persisted market.
    return _stock_cache_market(code)


# ---- Watchlist persistence --------------------------------------------------

WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
_watchlist_lock = threading.Lock()


def _load_watchlist():
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with WATCHLIST_FILE.open() as f:
            return json.load(f).get("codes", [])
    except (OSError, json.JSONDecodeError):
        return []


def _save_watchlist(codes):
    with _watchlist_lock:
        with WATCHLIST_FILE.open("w") as f:
            json.dump({"codes": codes}, f, ensure_ascii=False, indent=2)


def _validate_code(code: str):
    if not code.isdigit() or not (4 <= len(code) <= 6):
        raise HTTPException(400, "code must be a 4-6 digit TWSE stock number")


def _watchlist_item(code: str) -> dict:
    """Read today's cache (if any) and return a compact summary for the watchlist."""
    info = _company_info(code)
    market = info.get("market") or _stock_cache_market(code) or MARKET_TWSE
    base = {
        "code": code, "cached": False,
        "short_name": info["short_name"],
        "industry": info["industry"],
        "market": market,
    }
    cache = _stock_cache(code)
    if not cache.exists():
        return base
    try:
        with cache.open() as f:
            payload = json.load(f)
        full = payload.get("rows") or []
        cached_market = payload.get("market") or market
        if not full:
            return base
        last = full[-1]
        dash = compute_dashboard(full, code, market=cached_market, compact=True)
        rq = dash.get("reversal_quality") or None
        tq = dash.get("topping_quality") or None
        # Compact form for the watchlist card — full check list lives on
        # the detail page.
        rq_compact = (
            {"score": rq["score"], "max": rq["max"],
             "stars": rq["stars"], "desc": rq["desc"]}
            if rq else None
        )
        tq_compact = (
            {"score": tq["score"], "max": tq["max"],
             "stars": tq["stars"], "desc": tq["desc"]}
            if tq else None
        )
        return {
            **base,
            "market": cached_market,
            "cached": True,
            "as_of": last.get("date"),
            "close": last.get("close"),
            "change_pct": last.get("change_pct"),
            "high": last.get("high"),
            "low": last.get("low"),
            "lots": last.get("lots"),
            "summary": dash["summary"],
            "step_lights": [s["light"] for s in dash["steps"]],
            "reversal_quality": rq_compact,
            "topping_quality": tq_compact,
        }
    except (OSError, json.JSONDecodeError, KeyError):
        return base


# ---- App --------------------------------------------------------------------

app = FastAPI(title="TWSE Stock Viewer")


@app.on_event("startup")
def _on_startup() -> None:
    """Run cheap chores in the background so server boot stays snappy:
    (1) purge stale dated caches, (2) warm the company-info cache so the
    first watchlist load doesn't pay the ~1MB OpenAPI fetch, (3) pre-warm
    the today_chips scan (15-45s for a 28-stock watchlist) so the first
    page open finds it instantly cached, (4) sweep the forward log to
    fill any newly-matured alpha records since the last server run."""
    def _warm():
        try:
            _purge_old_caches()
        except Exception:
            pass
        try:
            _refresh_company_cache()
        except Exception:
            pass
        try:
            watchlist_chips()
        except Exception:
            pass
        # Forward-log: fill any horizons that matured while the
        # server was offline. Idempotent — a no-op when nothing has
        # matured since last sweep.
        try:
            forward_log.fill_matured_records(_load_rows_from_cache)
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
    # Cron worker (Q2 = lazy + cron). Lazy fill handles the case
    # where `/api/forward_log/summary` is read; this background
    # thread handles the case where no one reads but the server
    # keeps running. 6 hours is a balance between latency and load.
    def _forward_log_cron():
        import time
        while True:
            time.sleep(6 * 3600)
            try:
                forward_log.fill_matured_records(_load_rows_from_cache)
            except Exception:
                pass
    threading.Thread(target=_forward_log_cron, daemon=True).start()


@app.get("/api/stock/{code}")
def get_stock(code: str, rows: int = 30):
    _validate_code(code)
    if rows < 1 or rows > 500:
        raise HTTPException(400, "rows must be between 1 and 500")

    full = _load_stock(code, rows)
    if not full:
        raise HTTPException(404, f"no data found for {code}")
    info = _company_info(code)
    market = _stock_cache_market(code) or info.get("market") or MARKET_TWSE
    if not info.get("market"):
        info = {**info, "market": market}
    return {
        "code": code,
        "info": info,
        "market": market,
        "rows": full[-rows:],
        "dashboard": compute_dashboard(full, code, market=market),
    }


@app.get("/api/news/{code}")
def get_news(code: str, days: int = 14):
    """Return MOPS material announcements (重大訊息) for one stock.

    Items: {code, name, date, roc_date, time, title} from MOPS plus
    optional LLM-derived {sentiment, summary} when ANTHROPIC_API_KEY is
    configured. Annotations are persisted back into the news cache so
    each item is summarized at most once per trading day.
    """
    _validate_code(code)
    if days < 1 or days > 60:
        raise HTTPException(400, "days must be between 1 and 60")
    with _lock_for(f"__news_{code}__"):
        data = news_fetcher.load_or_fetch(code, days=days)
        items = data.get("items") or []
        if items and news_llm.is_available():
            todo = [it for it in items
                    if not (it.get("sentiment") and it.get("summary"))]
            if todo:
                news_llm.annotate(items, code=code)
                try:
                    with news_fetcher._cache_path(code).open("w") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass
    return {
        "code": code,
        "days": days,
        "fetched_at": data.get("fetched_at"),
        "llm_available": news_llm.is_available(),
        "items": items,
    }


@app.get("/api/news_log/{code}")
def get_news_log(code: str, days: int = 14):
    """Return Yahoo Finance news for one stock from the manually-curated
    news_log.jsonl, filtered to the last `days`.

    Unlike `/api/news/{code}` (MOPS auto-fetch), news_log.jsonl is
    populated through Claude conversation when the user says
    "更新 watchlist 新聞" — see CLAUDE.md "Manual-via-Claude workflows"
    for the bootstrap + update protocol. The endpoint just slices the
    JSONL by code + recency; the panel auto-hides when there are no
    records for this code.

    Response also includes `last_updated` (most recent fetched_at across
    ALL records, not just this code) so the UI can flag staleness when
    the user hasn't refreshed for a while.
    """
    _validate_code(code)
    if days < 1 or days > 60:
        raise HTTPException(400, "days must be between 1 and 60")
    log_path = Path(__file__).resolve().parent / "news_log.jsonl"
    if not log_path.exists():
        return {"code": code, "days": days, "items": [], "last_updated": None,
                "total_in_log": 0}
    from datetime import date as _date, timedelta
    cutoff = _date.today() - timedelta(days=days)
    items: list[dict] = []
    last_updated: str | None = None
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fa = r.get("fetched_at")
                if fa and (last_updated is None or fa > last_updated):
                    last_updated = fa
                if r.get("code") != code:
                    continue
                nd = r.get("news_date")
                if not nd or nd == "no-date":
                    # Treat undated items as "today" so they at least show
                    # up in the most-recent window.
                    nd_date = _date.today()
                else:
                    try:
                        from datetime import datetime as _dt
                        nd_date = _dt.fromisoformat(nd).date()
                    except (ValueError, TypeError):
                        continue
                if nd_date < cutoff:
                    continue
                items.append(r)
    except OSError:
        pass
    # Sort newest first, then ties by source name for stability
    items.sort(key=lambda r: (r.get("news_date") or "", r.get("source") or ""),
               reverse=True)
    return {
        "code": code,
        "days": days,
        "items": items,
        "last_updated": last_updated,
        "total_in_log": sum(1 for _ in log_path.open()) if log_path.exists() else 0,
    }


@app.get("/api/fundamentals/{code}")
def get_fundamentals(code: str, close: Optional[float] = None):
    """Return the last 3 fiscal years (and most recent quarters if
    published) of EPS / revenue / margins / book value per share.

    When `close` is supplied, also returns a trailing P/E ratio
    computed against the most recent annual EPS (skipping quarterly,
    since broker apps quote PER on TTM/annual EPS — the comparable
    basis).
    """
    _validate_code(code)
    market = (_stock_cache_market(code)
              or _market_for(code)
              or MARKET_TWSE)
    data = fundamentals_fetcher.fetch(code, market=market)
    if not data.get("available"):
        return {"code": code, "available": False, "market": market}
    per = None
    if close is not None:
        per = fundamentals_fetcher.per_for(
            fundamentals_fetcher.latest_annual_eps(data.get("periods") or []),
            close,
        )
    return {**data, "per": per, "close_used": close}


_BACKTEST_STATS_PATH = (Path(__file__).resolve().parent.parent
                        / "backtest" / "data" / "_summary_stats.json")
_backtest_stats_cache: dict | None = None
_backtest_stats_mtime: float = 0.0


def _load_backtest_stats() -> dict | None:
    """Read the pre-built backtest aggregate. Re-reads on mtime change so
    `python3 -m backtest.build_stats` updates are picked up without restart."""
    global _backtest_stats_cache, _backtest_stats_mtime
    if not _BACKTEST_STATS_PATH.exists():
        return None
    try:
        m = _BACKTEST_STATS_PATH.stat().st_mtime
    except OSError:
        return None
    if _backtest_stats_cache is not None and m == _backtest_stats_mtime:
        return _backtest_stats_cache
    try:
        with _BACKTEST_STATS_PATH.open() as f:
            _backtest_stats_cache = json.load(f)
        _backtest_stats_mtime = m
    except (OSError, json.JSONDecodeError):
        return None
    return _backtest_stats_cache


@app.get("/api/backtest_stats")
def get_backtest_stats(label: Optional[str] = None,
                       chip_key: Optional[str] = None):
    """Return pooled historical forward-return stats.

    The data comes from `backtest/build_stats.py` which event-studies
    both the 7-step summary labels and the chip-trigger conditions
    across all stocks in `backtest/data/`. The summary-label numbers
    are mostly drift (no cross-stock alpha); the chip-keyed numbers
    are the validated signals the dashboard actually surfaces.

    Three modes:
      - no args: full payload
      - label=X: stats for summary label X (legacy)
      - chip_key=X: stats for chip trigger X (preferred — what the
        dashboard card uses now)
    """
    data = _load_backtest_stats()
    if not data:
        return {"available": False, "reason": "backtest stats not built"}
    if label is None and chip_key is None:
        return {"available": True, **data}
    if chip_key is not None:
        chip = (data.get("chips") or {}).get(chip_key)
        if not chip or not chip.get("events_total"):
            return {"available": False, "chip_key": chip_key,
                    "reason": "no stats for chip"}
        return {
            "available": True,
            "chip_key": chip_key,
            "stocks": data.get("stocks", []),
            "generated_at": data.get("generated_at"),
            "note": data.get("note", ""),
            **chip,
        }
    sig = (data.get("signals") or {}).get(label)
    if not sig:
        return {"available": False, "label": label, "reason": "no stats for label"}
    return {
        "available": True,
        "label": label,
        "stocks": data.get("stocks", []),
        "generated_at": data.get("generated_at"),
        "note": data.get("note", ""),
        **sig,
    }


def _load_rows_from_cache(code: str) -> Optional[list[dict]]:
    """Helper passed to `forward_log.fill_matured_records` — reads
    the most recent cached price series for a stock, or returns None
    when no cache exists. Used by the lazy + cron fill paths.
    """
    cache = _stock_cache(code)
    try:
        if cache.exists():
            with cache.open() as f:
                payload = json.load(f)
            return payload.get("rows") or None
        # Fall back: most recent cache file for this code within the
        # 7-day retention window (covers weekends / yesterday-only
        # cache when today's hasn't been generated yet).
        recent = sorted(CACHE_DIR.glob(f"{code}_*.json"))
        if not recent:
            return None
        with recent[-1].open() as f:
            payload = json.load(f)
        return payload.get("rows") or None
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/forward_log/summary")
def get_forward_log_summary():
    """Forward-looking validation summary: per-chip pool prediction
    vs actual OOS delivery, accumulated since the log started.

    Triggers a `fill_matured_records` sweep first (lazy fill) so the
    response always reflects the latest matured horizons. The cron
    worker (see `_forward_log_worker`) does the same on a 6-hour
    schedule for long-quiet servers."""
    try:
        forward_log.fill_matured_records(_load_rows_from_cache)
    except Exception:  # noqa: BLE001
        # Non-fatal — summary should still serve even if a single
        # stock's cache is corrupted.
        pass
    pool_stats = _load_backtest_stats()
    return forward_log.summarize(pool_stats)


@app.get("/api/industry_pe/{code}")
def get_industry_pe(code: str, per: Optional[float] = None):
    """Return industry-median P/E for the stock's industry bucket.

    Pass `per` (this stock's own P/E) so the response includes a
    side-by-side comparison without the frontend having to read it
    twice from the fundamentals endpoint.

    Stats are computed across BOTH TWSE + TPEX, grouped by 產業別.
    Loss-making (negative or zero PER) stocks are excluded. Buckets
    with fewer than 5 samples have no median.
    """
    _validate_code(code)
    _refresh_company_cache()
    info = _company_info(code)
    industry = (info.get("industry") or "").strip()
    if not industry:
        return {"code": code, "available": False, "reason": "unknown industry"}
    stats = industry_pe_fetcher.for_industry(
        industry,
        _company_cache_mem["twse"],
        _company_cache_mem["otc"],
    )
    if not stats:
        return {"code": code, "available": False, "industry": industry,
                "reason": "no stats"}
    out = {"code": code, "available": True, **stats, "my_pe": per}
    median = stats.get("median_pe")
    if per is not None and median is not None:
        out["delta_pct"] = round((per - median) / median * 100, 1)
    return out


@app.get("/api/dividend/{code}")
def get_dividend(code: str, close: Optional[float] = None):
    """Return the most recent annual cash dividend + yield.

    For TWSE, the source file only carries yield, so we derive
    cash-dividend ≈ yield × close / 100 when `close` is supplied.
    For TPEX, both fields come straight from the source.
    """
    _validate_code(code)
    market = (_stock_cache_market(code)
              or _market_for(code)
              or MARKET_TWSE)
    info = dividend_fetcher.get_for_code(code, market=market,
                                         last_close=close)
    if info is None:
        return {"code": code, "available": False, "market": market}
    return {"available": True, **info}


@app.get("/api/eps_history/{code}")
def get_eps_history(code: str, years: int = 3):
    """Return multi-year quarterly EPS for trend visualization.

    `years` clamps to [1, 5]. Each year requires 4 MOPS round-trips
    if not cached. Cache filenames are `eps_q_{code}_{Y}Q{N}.json`
    (no date suffix → permanent for published quarters).
    """
    _validate_code(code)
    years = max(1, min(int(years or 3), 5))
    market = (_stock_cache_market(code)
              or _market_for(code)
              or MARKET_TWSE)
    data = eps_history_fetcher.get_history(code, market=market, years=years)
    return data


@app.get("/api/revenue/{code}")
def get_revenue(code: str):
    """Return the most-recent monthly revenue (月營收) snapshot.

    Falls back one month if the latest expected month isn't yet
    published. Returns `{available: false}` if the code isn't in
    either month's market file (new listing, non-revenue entity, etc.).
    """
    _validate_code(code)
    market = (_stock_cache_market(code)
              or _market_for(code)
              or MARKET_TWSE)
    rev = revenue_fetcher.get_for_code(code, market=market)
    if rev is None:
        return {"code": code, "available": False, "market": market}
    return {"available": True, **rev}


@app.get("/api/watchlist")
def watchlist_get():
    codes = _load_watchlist()
    return {"codes": codes, "items": [_watchlist_item(c) for c in codes]}


@app.post("/api/watchlist/reorder")
def watchlist_reorder(codes: list[str] = Body(..., embed=True)):
    """Persist a new order for the watchlist. Unknown codes ignored;
    existing codes missing from the request are appended at the end."""
    current = _load_watchlist()
    current_set = set(current)
    seen = set()
    new_order = []
    for c in codes:
        if c in current_set and c not in seen:
            new_order.append(c)
            seen.add(c)
    for c in current:
        if c not in seen:
            new_order.append(c)
    _save_watchlist(new_order)
    return {"ok": True, "codes": new_order}


@app.put("/api/watchlist/{code}")
def watchlist_add(code: str):
    _validate_code(code)
    codes = _load_watchlist()
    if code not in codes:
        codes.append(code)
        _save_watchlist(codes)
    _invalidate_today_chips_cache()
    return {"ok": True, "codes": codes, "item": _watchlist_item(code)}


@app.delete("/api/watchlist/{code}")
def watchlist_remove(code: str):
    _validate_code(code)
    codes = _load_watchlist()
    if code in codes:
        codes.remove(code)
        _save_watchlist(codes)
    for f in CACHE_DIR.glob(f"{code}_*.json"):
        f.unlink(missing_ok=True)
    _invalidate_today_chips_cache()
    return {"ok": True, "codes": codes}


@app.post("/api/watchlist/{code}/refresh")
def watchlist_refresh(code: str):
    """Drop today's cache for this stock and re-fetch from TWSE.

    With incremental update (using yesterday's cache as a base), this is
    typically a few seconds. First-ever fetch with no prior cache still
    walks back ~13 months (~30-60s).
    """
    _validate_code(code)
    cache = _stock_cache(code)
    if cache.exists():
        cache.unlink()
    full = _load_stock(code, 30)
    if not full:
        raise HTTPException(404, f"no data for {code}")
    _invalidate_today_chips_cache()
    return {"ok": True, "item": _watchlist_item(code)}


def _scan_chip_alerts_for_code(code: str) -> dict | None:
    """Run full compute_dashboard on a cached stock and return:
      - `alerts`: list of stat_key-bearing chip alerts (for the
        existing today_chips API consumer)
      - `emit_records`: list of forward-log emission records ready
        to be appended (one per stat_key chip firing today)
      - `prev_trading_day`: this stock's prior trading day (for the
        first-cross dedup check inside forward_log.log_emissions)

    Returns None when the stock has no cache or no rows. The combined
    return shape lets the watchlist_chips endpoint do one batch
    forward-log capture after all scans complete, rather than each
    scan racing on the log file.
    """
    try:
        cache = _stock_cache(code)
        if not cache.exists():
            return None
        with cache.open() as f:
            payload = json.load(f)
        rows = payload.get("rows") or []
        if not rows:
            return None
        market = payload.get("market") or _market_for(code) or MARKET_TWSE
        info = _company_info(code)
        short_name = info.get("short_name") or code
        dash = compute_dashboard(rows, code, market=market)
        alerts_out: list[dict] = []
        emit_records: list[dict] = []
        last = rows[-1]
        as_of = last.get("date")
        close_at_emit = last.get("close")
        taiex_at_emit = last.get("taiex")
        steps = dash.get("steps") or []
        inst_light = steps[6]["light"] if len(steps) > 6 else None
        regime = dash.get("taiex_regime")
        for a in dash.get("alerts") or []:
            sk = a.get("stat_key")
            if not sk:
                continue
            alerts_out.append({
                "code": code,
                "short_name": short_name,
                "kind": a.get("kind"),
                "stat_key": sk,
                "icon": a.get("icon", ""),
                "text": a.get("text", ""),
                "tone": a.get("tone", "info"),
            })
            # Build the forward-log record while we have the data
            # in scope. The log layer applies first-cross dedup
            # against the previous trading day below.
            if as_of and close_at_emit is not None and taiex_at_emit is not None:
                emit_records.append({
                    "emitted_at": as_of,
                    "code": code,
                    "stat_key": sk,
                    "inst_light": inst_light,
                    "regime": regime,
                    "close_at_emit": close_at_emit,
                    "taiex_at_emit": taiex_at_emit,
                })
        prev_trading_day = rows[-2]["date"] if len(rows) >= 2 else None
        return {
            "alerts": alerts_out,
            "emit_records": emit_records,
            "prev_trading_day": prev_trading_day,
        }
    except Exception:  # noqa: BLE001
        return None


# Daily cache for the watchlist chip scan. Computing the full dashboard
# (history + alerts) for every watchlist code takes 15-45s the first
# time after a restart; the result is stable within a trading day so
# we cache it. Invalidates when the trading-day tag changes or when
# the watchlist is mutated (add/remove/refresh). The lock prevents two
# parallel callers from each kicking off a full scan when the cache is
# cold — second caller waits for the first to finish.
_today_chips_cache: dict = {"date": None, "data": None}
_today_chips_lock = threading.Lock()


def _invalidate_today_chips_cache() -> None:
    _today_chips_cache["date"] = None
    _today_chips_cache["data"] = None


@app.get("/api/watchlist/chips")
def watchlist_chips():
    """Scan every cached watchlist stock and group today's firing chip
    alerts (stat_key-bearing) by chip kind. Lets the frontend show a
    "今日觸發" summary without round-tripping per stock.

    Uses a small thread pool to parallelize the per-stock dashboard
    compute. Result is cached for the trading day so subsequent calls
    are O(1).
    """
    today = _today_tag()
    cached = _today_chips_cache.get("data")
    if _today_chips_cache.get("date") == today and cached is not None:
        return {**cached, "cached": True}
    with _today_chips_lock:
        # Re-check inside lock — the prior caller may have just populated
        # the cache while we were waiting.
        cached = _today_chips_cache.get("data")
        if _today_chips_cache.get("date") == today and cached is not None:
            return {**cached, "cached": True}
        from concurrent.futures import ThreadPoolExecutor
        codes = _load_watchlist()
        fires: dict[str, list[dict]] = {}
        if not codes:
            result = {"chips": fires, "total": 0, "scanned": 0}
            _today_chips_cache["date"] = today
            _today_chips_cache["data"] = result
            return {**result, "cached": False}
        # Collect alerts (for the today_chips response) + emission
        # records (for forward_log). The forward log call happens
        # AFTER the scan completes so a single batch handles dedup
        # against the previous trading day for all stocks at once.
        all_emit_records: list[dict] = []
        prev_day_per_code: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for code, result in zip(codes, ex.map(_scan_chip_alerts_for_code, codes)):
                if not result:
                    continue
                for a in result.get("alerts") or []:
                    fires.setdefault(a["stat_key"], []).append(a)
                for rec in result.get("emit_records") or []:
                    all_emit_records.append(rec)
                pd = result.get("prev_trading_day")
                if pd:
                    prev_day_per_code[code] = pd
        # Forward-log capture: first-cross dedup, then append.
        # Errors here are non-fatal — chip display must keep working
        # even if the log file is unwritable.
        try:
            forward_log.log_emissions(all_emit_records, prev_day_per_code)
        except Exception:  # noqa: BLE001
            pass
        total = sum(len(v) for v in fires.values())
        result = {"chips": fires, "total": total, "scanned": len(codes)}
        _today_chips_cache["date"] = today
        _today_chips_cache["data"] = result
        return {**result, "cached": False}


@app.post("/api/watchlist/refresh")
def watchlist_refresh_all():
    """Refresh every code in the watchlist sequentially. Reuses shared
    caches (TAIEX, T86, companies) across stocks, so the per-stock cost
    is dominated by one TWSE STOCK_DAY call each (incremental path)."""
    _invalidate_today_chips_cache()
    codes = _load_watchlist()
    updated = []
    failed = []
    for code in codes:
        try:
            cache = _stock_cache(code)
            if cache.exists():
                cache.unlink()
            full = _load_stock(code, 30)
            if not full:
                failed.append({"code": code, "error": "no data"})
                continue
            updated.append(_watchlist_item(code))
        except Exception as e:  # noqa: BLE001
            failed.append({"code": code, "error": str(e)})
    return {"ok": True, "updated": len(updated), "failed": failed,
            "items": updated}


@app.get("/api/taiex/today")
def taiex_today_get():
    today_iso = _today_iso()
    regime = _taiex_regime_today()
    manual = _load_taiex_manual()
    if today_iso in manual:
        return {"date": today_iso, "close": manual[today_iso],
                "source": "manual", "regime": regime}
    cache = _taiex_cache()
    if cache.exists():
        try:
            with cache.open() as f:
                data = json.load(f)
            v = data.get(today_iso)
            if v is not None:
                return {"date": today_iso, "close": v,
                        "source": "auto", "regime": regime}
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": today_iso, "close": None, "source": None,
            "regime": regime}


@app.put("/api/taiex/today")
def taiex_today_put(payload: dict = Body(...)):
    """Manually set today's TAIEX close. Patches existing same-day stock caches
    whose last row's `taiex` field is missing."""
    raw = payload.get("close") if isinstance(payload, dict) else None
    try:
        close = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "close must be a number")
    if close <= 0 or close > 100000:
        raise HTTPException(400, "close out of range")
    _invalidate_today_chips_cache()
    today_iso = _today_iso()
    with _taiex_manual_lock:
        manual = _load_taiex_manual()
        manual[today_iso] = close
        _save_taiex_manual(manual)
        # Also write into today's auto cache so next _load_taiex sees it
        # without overlay (defensive — overlay still applies regardless).
        cache = _taiex_cache()
        try:
            data = {}
            if cache.exists():
                with cache.open() as f:
                    data = json.load(f)
            data[today_iso] = close
            with cache.open("w") as f:
                json.dump(data, f)
        except (OSError, json.JSONDecodeError):
            pass
    # Patch existing per-stock caches for today: if last row's date == today
    # and taiex is None, fill it in. Restrict to numeric-prefixed files
    # (stock codes are 4-6 digits) so taiex/t86/companies caches are skipped.
    today_tag = _today_tag()
    patched = 0
    for stock_cache in CACHE_DIR.glob(f"*_{today_tag}.json"):
        prefix = stock_cache.stem.rsplit("_", 1)[0]
        if not (prefix.isdigit() and 4 <= len(prefix) <= 6):
            continue
        try:
            with stock_cache.open() as f:
                stock_payload = json.load(f)
            if not isinstance(stock_payload, dict):
                continue
            rows = stock_payload.get("rows") or []
            if not rows:
                continue
            last = rows[-1]
            if last.get("date") == today_iso and last.get("taiex") is None:
                last["taiex"] = close
                with stock_cache.open("w") as f:
                    json.dump(stock_payload, f)
                patched += 1
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return {"ok": True, "date": today_iso, "close": close,
            "source": "manual", "patched": patched}


@app.delete("/api/taiex/today")
def taiex_today_delete():
    """Clear today's manual override (does not touch auto cache or patched stock rows)."""
    today_iso = _today_iso()
    with _taiex_manual_lock:
        manual = _load_taiex_manual()
        if today_iso in manual:
            del manual[today_iso]
            _save_taiex_manual(manual)
    _invalidate_today_chips_cache()
    return {"ok": True, "date": today_iso}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
