"""Backfill historical 融資/融券 (MI_MARGN) + 借券 (TWT93U) whole-market
dumps so backtest/margin_sbl_study.py has per-day data going back N years.

Walks weekdays (Mon-Fri) — skips weekends since TWSE doesn't trade then.
TWSE returns stat="OK" with empty tables on actual holidays; the fetcher
discards those so the cache files don't bloat.

Uses the existing `stock_web.margin_sbl_fetcher` helpers (same caches
that the live `/api/margin_sbl/{code}` endpoint reads), so backtest
data and live data share the same store — no duplicate cache layout
to maintain.

Cost estimate (5 years):
  ~1300 weekdays × 2 endpoints × 2s throttle = ~5200s = ~87 min
Re-runs are cheap (~seconds) since the fetcher short-circuits when a
day's cache already exists.

Usage:
    python3 -m backtest.prefetch_margin_sbl              # default 5y
    python3 -m backtest.prefetch_margin_sbl --years 3
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.margin_sbl_fetcher import (  # noqa: E402
    fetch_margin, fetch_sbl,
    _margin_cache, _sbl_cache,
)


def _weekdays_back(years: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    out: list[date] = []
    d = today
    total_days = 365 * years + 5
    for _ in range(total_days):
        if d.weekday() < 5:  # Mon=0..Fri=4
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))  # oldest → newest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--start", default=None,
                    help="Override start date (YYYY-MM-DD), useful for "
                         "resuming a partial backfill")
    args = ap.parse_args()

    weekdays = _weekdays_back(args.years)
    if args.start:
        cutoff = date.fromisoformat(args.start)
        weekdays = [d for d in weekdays if d >= cutoff]

    n = len(weekdays)
    print(f"Backfilling {n} weekdays of margin + SBL data "
          f"({weekdays[0]} → {weekdays[-1]}) ...", flush=True)
    t0 = time.time()
    margin_filled = sbl_filled = margin_skipped = sbl_skipped = 0
    margin_empty = sbl_empty = 0

    for i, d in enumerate(weekdays, 1):
        iso = d.isoformat()
        # Margin
        mp = _margin_cache(d.strftime("%Y%m%d"))
        if mp.exists():
            margin_skipped += 1
        else:
            m = fetch_margin(iso)
            if m:
                margin_filled += 1
            else:
                margin_empty += 1
        # SBL
        sp = _sbl_cache(d.strftime("%Y%m%d"))
        if sp.exists():
            sbl_skipped += 1
        else:
            s = fetch_sbl(iso)
            if s:
                sbl_filled += 1
            else:
                sbl_empty += 1
        # Per-100 progress line
        if i % 50 == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate if rate > 0 else 0
            print(f"  [{i}/{n}] {iso}  "
                  f"margin filled={margin_filled} skipped={margin_skipped} "
                  f"empty={margin_empty}; "
                  f"sbl filled={sbl_filled} skipped={sbl_skipped} "
                  f"empty={sbl_empty}; "
                  f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                  flush=True)

    print(f"\nDone in {time.time() - t0:.0f}s.", flush=True)
    print(f"  margin: filled {margin_filled}, skipped (cached) "
          f"{margin_skipped}, empty/holiday {margin_empty}",
          flush=True)
    print(f"  sbl:    filled {sbl_filled}, skipped (cached) "
          f"{sbl_skipped}, empty/holiday {sbl_empty}",
          flush=True)


if __name__ == "__main__":
    main()
