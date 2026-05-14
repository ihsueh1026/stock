"""Fetch historical T86 (三大法人買賣超) for every TAIEX trading day.

The production stock_web/app.py only caches T86 per-day inside its
auto-purged daily cache window (7 days). For the backtest to compute
the step 7 (法人認養) light over years of history we need long-running
T86 storage — that's what this script populates.

Strategy:
  - Read every date in backtest/data/_taiex.json
  - For each, fetch TWSE T86 via stock_web.app._fetch_t86_twse and (if
    --otc) the TPEX equivalent. Save to:
        backtest/data/t86/{YYYYMMDD}.json       (TWSE)
        backtest/data/t86otc/{YYYYMMDD}.json    (OTC)
  - Resumable — skips dates whose file already exists.
  - Throttles at 3s (TWSE rate-limits aggressively, same as the OHLCV
    fetcher in fetch_twse_daily.py).

These files are NOT shared with the production cache — production has
its own daily cache, and the backtest copy is permanent / never purged.

Usage:
    python3 -m backtest.prefetch_t86                # TWSE only (default)
    python3 -m backtest.prefetch_t86 --otc          # OTC only
    python3 -m backtest.prefetch_t86 --both         # both markets
    python3 -m backtest.prefetch_t86 --since 2024-01-01  # subset

At 3s throttle, full TWSE backfill of ~1240 trading days is ~62 min.
Both markets is ~2h. Re-running after partial completion just fills
the gaps.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _fetch_t86_twse, _fetch_t86_otc,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
TWSE_DIR = DATA_DIR / "t86"
OTC_DIR = DATA_DIR / "t86otc"
TAIEX_PATH = DATA_DIR / "_taiex.json"

REQUEST_INTERVAL_SEC = 3.0


def _trading_dates() -> list[str]:
    with TAIEX_PATH.open() as f:
        data = json.load(f)
    return sorted(data["closes"].keys())


def _fetch_one_twse(date_iso: str) -> int:
    """Returns 1 if a file was newly written, 0 if skipped, -1 if empty."""
    TWSE_DIR.mkdir(exist_ok=True)
    compact = date_iso.replace("-", "")
    out = TWSE_DIR / f"{compact}.json"
    if out.exists():
        return 0
    data = _fetch_t86_twse(compact)
    if not data:
        return -1  # non-trading day or transient failure — don't cache
    with out.open("w") as f:
        json.dump(data, f)
    return 1


def _fetch_one_otc(date_iso: str) -> int:
    OTC_DIR.mkdir(exist_ok=True)
    compact = date_iso.replace("-", "")
    out = OTC_DIR / f"{compact}.json"
    if out.exists():
        return 0
    data = _fetch_t86_otc(date_iso)
    if not data:
        return -1
    with out.open("w") as f:
        json.dump(data, f)
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--otc", action="store_true",
                    help="fetch OTC instead of TWSE")
    ap.add_argument("--both", action="store_true",
                    help="fetch both markets sequentially")
    ap.add_argument("--since", default=None,
                    help="ISO date YYYY-MM-DD; only fetch dates >= this")
    ap.add_argument("--max", type=int, default=None,
                    help="stop after this many newly-fetched dates "
                         "(for incremental runs)")
    args = ap.parse_args()

    if args.both:
        markets = ["twse", "otc"]
    elif args.otc:
        markets = ["otc"]
    else:
        markets = ["twse"]

    dates = _trading_dates()
    if args.since:
        dates = [d for d in dates if d >= args.since]
    print(f"target: {len(dates)} dates × {len(markets)} markets "
          f"({dates[0]}..{dates[-1]})")

    new_count = 0
    for market in markets:
        print(f"\n=== {market.upper()} ===")
        for i, d in enumerate(dates, 1):
            fn = _fetch_one_twse if market == "twse" else _fetch_one_otc
            try:
                r = fn(d)
            except Exception as e:
                print(f"  [error] {d}: {e}")
                r = -1
            if r == 1:
                tag = "OK"
                new_count += 1
                # Only sleep after a real fetch — cached/skipped is free
                if args.max is not None and new_count >= args.max:
                    print(f"  hit --max={args.max}, stopping")
                    return
                time.sleep(REQUEST_INTERVAL_SEC)
            elif r == 0:
                tag = "cached"
            else:
                tag = "empty/fail"
            if i % 20 == 0 or r != 0:
                print(f"  [{i:>4}/{len(dates)}] {d}  {tag}")

    print(f"\ndone. {new_count} new files written.")


if __name__ == "__main__":
    main()
