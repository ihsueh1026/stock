"""Lag-1 correlation of US indices/stocks vs TWSE codes, grouped by
Taiwan industry.

Question: how much of a Taiwan tech stock's daily move tracks the
prior US trading session? Is the relationship as strong as folklore
says for 半導體? Does it dilute for non-tech industries (control)?

Alignment (lag-1 = "yesterday's US drives today's TW"):
  for each TWSE trading day T,
    find the largest US trading day d such that d < T
    pair (US_return[d], TW_return[T])
This pairs each TWSE day with the most recent US close that completed
BEFORE TWSE opened that day. US session ends ~04:00 Taipei time, TWSE
opens 09:00 Taipei time same day, so this is the "overnight gap"
relationship Taiwanese investors actually watch.

Universe: 50 codes under backtest/data/. Industries resolved via
stock_web.app._company_info(). Stocks with unknown industry are
bucketed under "(unknown)".

Output: a table of Pearson correlations + sample size per industry
× US ticker cell. Also dumps per-stock correlations for the top US
driver to surface which individual codes track US most/least.

Usage:
    python3 -m backtest.us_correlation_study
"""
from __future__ import annotations

import bisect
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import _company_info  # noqa: E402
from backtest.study import DATA_DIR, load_series  # noqa: E402


US_TICKERS = ["^IXIC", "^SOX", "^GSPC", "NVDA", "TSM", "AMD", "AVGO"]


def _sanitize(t: str) -> str:
    return t.replace("^", "").replace("-", "_").replace(".", "_")


def _load_us(ticker: str) -> dict[str, float] | None:
    """date_iso → close. None if cache missing."""
    p = DATA_DIR / f"_us_{_sanitize(ticker)}.json"
    if not p.exists():
        return None
    try:
        with p.open() as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return {r["date"]: r["close"] for r in d.get("rows", [])
            if r.get("close") is not None}


def _returns_series(closes_by_date: dict[str, float]) -> dict[str, float]:
    """Map each date d to (close[d] - close[d-1]) / close[d-1], using
    consecutive available dates only (no calendar interpolation)."""
    dates = sorted(closes_by_date.keys())
    rets = {}
    prev_close = None
    for d in dates:
        c = closes_by_date[d]
        if prev_close is not None and prev_close > 0:
            rets[d] = (c - prev_close) / prev_close
        prev_close = c
    return rets


def _twse_returns(code: str) -> dict[str, float] | None:
    try:
        raw = load_series(code)
    except FileNotFoundError:
        return None
    if not raw:
        return None
    closes = {}
    for r in raw:
        d = r.get("date")
        c = r.get("close")
        if d is not None and c is not None:
            closes[d if isinstance(d, str) else d.isoformat()] = float(c)
    if len(closes) < 30:
        return None
    return _returns_series(closes)


def _align_lag1(us_rets: dict[str, float],
                tw_rets: dict[str, float]) -> list[tuple[float, float]]:
    """For each TWSE date T with a known return, find the largest US
    date < T whose US return is also known, pair them."""
    us_dates = sorted(us_rets.keys())
    out = []
    for tw_date, tw_r in tw_rets.items():
        # bisect_left to find the insertion index
        i = bisect.bisect_left(us_dates, tw_date)
        # the largest US date strictly less than tw_date is at i-1
        if i == 0:
            continue
        us_date = us_dates[i - 1]
        out.append((us_rets[us_date], tw_r))
    return out


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 20:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _industry_for(code: str) -> str:
    try:
        info = _company_info(code) or {}
    except Exception:
        info = {}
    return info.get("industry") or "(unknown)"


def run():
    codes = _all_codes()
    # Pre-compute industry + TWSE returns per code
    print(f"Loading TWSE returns for {len(codes)} codes ...", file=sys.stderr)
    code_rets: dict[str, dict[str, float]] = {}
    code_industry: dict[str, str] = {}
    for c in codes:
        r = _twse_returns(c)
        if r is None:
            continue
        code_rets[c] = r
        code_industry[c] = _industry_for(c)

    print(f"Loading {len(US_TICKERS)} US series ...", file=sys.stderr)
    us_rets: dict[str, dict[str, float]] = {}
    for t in US_TICKERS:
        ser = _load_us(t)
        if ser is None:
            print(f"  [warn] missing _us_{_sanitize(t)}.json", file=sys.stderr)
            continue
        us_rets[t] = _returns_series(ser)
        print(f"  {t}: {len(us_rets[t])} return obs", file=sys.stderr)

    # corr[industry][us_ticker] = list of per-code correlations
    by_ind: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    by_code: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)

    for code, twr in code_rets.items():
        ind = code_industry[code]
        for t, ur in us_rets.items():
            pairs = _align_lag1(ur, twr)
            cor = _pearson(pairs)
            if cor is None:
                continue
            by_ind[ind][t].append(cor)
            by_code[code][t] = (cor, len(pairs))

    # Per-industry summary table
    print("\nLag-1 correlation (US prior session → TWSE today): "
          "industry × US ticker")
    print(f"{'Industry':<14}  {'n':>3}  "
          + "  ".join(f"{t:>7}" for t in US_TICKERS))
    print("-" * 90)
    inds_sorted = sorted(by_ind.keys(), key=lambda x: -len(by_ind[x][US_TICKERS[0]] if US_TICKERS[0] in by_ind[x] else []))
    for ind in inds_sorted:
        # number of codes in this industry that have any correlation
        n_codes = max((len(by_ind[ind][t]) for t in US_TICKERS), default=0)
        row = [f"{ind:<14}", f"{n_codes:>3}"]
        for t in US_TICKERS:
            vals = by_ind[ind].get(t) or []
            if not vals:
                row.append(f"{'n/a':>7}")
            else:
                m = statistics.median(vals)
                row.append(f"{m:>7.2f}")
        print("  ".join(row))

    # Top-10 / Bottom-5 codes by correlation with ^SOX (the headline driver)
    print("\nPer-code correlation with ^SOX (the headline US driver):")
    rows = []
    for code, cells in by_code.items():
        if "^SOX" in cells:
            cor, n = cells["^SOX"]
            ind = code_industry[code]
            rows.append((cor, code, ind, n))
    rows.sort(reverse=True)
    print(f"  {'rank':>4}  {'code':<6} {'industry':<14}  {'r(SOX)':>7}  {'n':>4}")
    for i, (cor, code, ind, n) in enumerate(rows[:10], 1):
        print(f"  {i:>4}  {code:<6} {ind:<14}  {cor:>7.2f}  {n:>4}")
    print(f"  ...")
    for i, (cor, code, ind, n) in enumerate(rows[-5:], len(rows) - 4):
        print(f"  {i:>4}  {code:<6} {ind:<14}  {cor:>7.2f}  {n:>4}")

    # Median over the full universe per US ticker (sanity check vs industries)
    print(f"\nOverall median correlation per US ticker (across all {len(by_code)} codes):")
    for t in US_TICKERS:
        cors = [cells[t][0] for cells in by_code.values() if t in cells]
        if cors:
            print(f"  {t:<8}: median r = {statistics.median(cors):.2f}  "
                  f"(p25={sorted(cors)[len(cors) // 4]:.2f}, "
                  f"p75={sorted(cors)[3 * len(cors) // 4]:.2f}, n={len(cors)})")


if __name__ == "__main__":
    run()
