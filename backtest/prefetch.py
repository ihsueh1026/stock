"""Fetch ~5 years of TWSE/OTC daily OHLCV (and TAIEX) for event-study backtests.

Uses the same month fetchers as the web app, but writes the raw parsed
series (no indicators) to backtest/data/{code}.json so the study script
can recompute indicators with whatever lookback it wants.

Usage:
    python3 -m backtest.prefetch 2395 5388         # stocks
    python3 -m backtest.prefetch --taiex           # TAIEX only
    python3 -m backtest.prefetch --years 5 2395    # custom history length
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch_twse_daily as twse  # noqa: E402
from stock_web.app import _market_for, MARKET_TWSE, MARKET_OTC  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

MONTHS_PER_YEAR = 12


def _walk_back_months(month_fetcher, months: int, label: str) -> list[list[str]]:
    """Like twse.fetch_recent_rows but capped by `months`, not row count."""
    collected: list[list[str]] = []
    now = datetime.now()
    year, month = now.year, now.month
    for i in range(months):
        print(f"  {label} {year}-{month:02d}", end=" ")
        rows = month_fetcher(year, month)
        print(f"({len(rows)} rows)")
        collected = rows + collected
        if i < months - 1:
            time.sleep(twse.REQUEST_INTERVAL_SEC)
        year, month = twse.previous_month(year, month)
    return collected


def fetch_stock(code: str, years: int) -> None:
    market = _market_for(code) or MARKET_TWSE
    if market == MARKET_OTC:
        fetcher = lambda y, m: twse.fetch_otc_month(y, m, code)
    else:
        fetcher = lambda y, m: twse.fetch_stock_month(y, m, code)
    months = years * MONTHS_PER_YEAR + 2  # +2 to absorb partial first/last
    print(f"[{code}] market={market}, fetching {months} months...")
    raw = _walk_back_months(fetcher, months, code)
    series = twse.parse_stock_rows(raw)
    if not series:
        print(f"[{code}] no data parsed — aborting")
        return
    payload = {
        "code": code,
        "market": market,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "rows": [{
            "date": s["date"].isoformat(),
            "high": s["high"],
            "low": s["low"],
            "close": s["close"],
            "lots": s["lots"],
        } for s in series],
    }
    out = DATA_DIR / f"{code}.json"
    with out.open("w") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[{code}] wrote {len(series)} rows -> {out.name} "
          f"({series[0]['date']} ~ {series[-1]['date']})")


def fetch_taiex(years: int) -> None:
    months = years * MONTHS_PER_YEAR + 2
    print(f"[TAIEX] fetching {months} months...")
    raw = _walk_back_months(twse.fetch_taiex_month, months, "TAIEX")
    parsed = twse.parse_taiex_rows(raw)
    if not parsed:
        print("[TAIEX] no data — aborting")
        return
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "closes": {d.isoformat(): v for d, v in sorted(parsed.items())},
    }
    out = DATA_DIR / "_taiex.json"
    with out.open("w") as f:
        json.dump(payload, f)
    dates = list(payload["closes"].keys())
    print(f"[TAIEX] wrote {len(dates)} rows -> {out.name} "
          f"({dates[0]} ~ {dates[-1]})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", help="stock codes")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--taiex", action="store_true", help="also fetch TAIEX")
    ap.add_argument("--only-taiex", action="store_true",
                    help="fetch TAIEX only, skip codes")
    args = ap.parse_args()

    if not args.only_taiex:
        for code in args.codes:
            fetch_stock(code, args.years)
    if args.taiex or args.only_taiex:
        fetch_taiex(args.years)


if __name__ == "__main__":
    main()
