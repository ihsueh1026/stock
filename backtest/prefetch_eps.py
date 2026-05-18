"""Backfill quarterly EPS for every code under backtest/data/.

Reuses `stock_web.eps_history_fetcher.get_history(years=N)`, which
walks (current_ad - N) .. current_ad inclusive (6 calendar years when
N=5) × 4 seasons per stock. Each request returns both the requested
quarter AND prior-year same-quarter, so we get ~2 quarter-observations
per round trip. All results are cached as
`stock_web/cache/eps_q_{code}_{Y}Q{Q}.json` and reused across runs.

Network cost: MOPS throttles at ~2s/req. For 50 codes × ~24 req each
the first cold run is ~40 min; subsequent runs are seconds because
the cache fully covers the requested range.

Usage:
    python3 -m backtest.prefetch_eps              # default years=5, all codes
    python3 -m backtest.prefetch_eps --years 7
    python3 -m backtest.prefetch_eps 2330 2317    # subset
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.eps_history_fetcher import get_history  # noqa: E402
from stock_web.app import _market_for, MARKET_TWSE  # noqa: E402

DATA_DIR = ROOT / "backtest" / "data"


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--years", type=int, default=5,
                    help="EPS history depth in years (default 5)")
    args = ap.parse_args()

    codes = args.codes or _all_codes()
    print(f"Backfilling EPS for {len(codes)} codes, "
          f"{args.years}y depth ...", flush=True)
    for i, code in enumerate(codes, 1):
        market = _market_for(code) or MARKET_TWSE
        try:
            h = get_history(code, market=market, years=args.years)
        except Exception as e:
            print(f"  [{i}/{len(codes)}] {code} ({market}): ERROR {e}",
                  flush=True)
            continue
        periods = h.get("periods", [])
        with_yoy = sum(1 for p in periods if p.get("eps_yoy_pct") is not None)
        print(f"  [{i}/{len(codes)}] {code} ({market}): "
              f"{len(periods)} periods, {with_yoy} with YoY",
              flush=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
