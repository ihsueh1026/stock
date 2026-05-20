"""Standalone watchlist refresh — mirrors `/api/watchlist/refresh`.

Runs without the FastAPI server; imports the same helpers the
endpoint uses internally. Designed for launchd-driven morning
pre-fetch (07:00 weekdays) so when the user opens the dashboard
after 9am, every code is already cached and the page loads
instantly instead of waiting 30-60s per cold stock.

Why standalone vs. curl-ing the running server: the FastAPI app
runs interactively in the user's terminal — it isn't a persistent
daemon. The morning launchd job needs to work even when the server
isn't running, so we invoke the helpers in-process.

Side effect: deletes each per-stock cache and rewrites it (incremental
path within `_load_stock` — usually just the current month). TAIEX,
T86, and company-info caches are shared across stocks so each is
fetched at most once per run.

Also bumps the US market cache (`backtest/data/_us_*.json`) by calling
`backtest.prefetch_us`. The watchlist page's US strip self-tags as
stale when as_of is more than 4 days old; refreshing here keeps it
fresh for the morning open.

Usage:
    python3 -m tools.refresh_watchlist
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _load_watchlist, _load_stock, _stock_cache,
    _invalidate_today_chips_cache,
)


def _refresh_margin_sbl() -> None:
    """Pre-fetch the most recent available 融資/融券 + 借券 dumps.

    The 07:00 morning run happens BEFORE TWSE publishes today's data
    (close is 13:30, margin/SBL appears ~16:00). So asking for today's
    date returns an empty dump. Walk back up to 7 calendar days
    (skipping weekends) and stop at the first non-empty response,
    which will be either yesterday (Mon-Fri morning) or last Friday
    (Mon morning).

    Both fetchers are idempotent (skip if cache exists) so empty
    in-between calls cost a single TWSE request each."""
    try:
        from stock_web import margin_sbl_fetcher as msbl
    except ImportError as e:
        print(f"  (Margin/SBL refresh skipped — import failed: {e})",
              flush=True)
        return
    from datetime import date, timedelta
    print("\nRefreshing margin + SBL caches (walking back to last "
          "published trading day)...", flush=True)
    m_done = s_done = False
    for back in range(7):
        d = date.today() - timedelta(days=back)
        if d.weekday() >= 5:  # skip weekends
            continue
        iso = d.isoformat()
        if not m_done:
            try:
                m = msbl.fetch_margin(iso)
                if m:
                    print(f"  margin {iso}: {len(m)} codes",
                          flush=True)
                    m_done = True
            except Exception as e:  # noqa: BLE001
                print(f"  margin {iso}: ERROR {e}", flush=True)
                m_done = True  # bail rather than spam errors
        if not s_done:
            try:
                s = msbl.fetch_sbl(iso)
                if s:
                    print(f"  sbl    {iso}: {len(s)} codes",
                          flush=True)
                    s_done = True
            except Exception as e:  # noqa: BLE001
                print(f"  sbl    {iso}: ERROR {e}", flush=True)
                s_done = True
        if m_done and s_done:
            break
    if not m_done:
        print("  margin: no published data in last 7 calendar days",
              flush=True)
    if not s_done:
        print("  sbl: no published data in last 7 calendar days",
              flush=True)


def _refresh_us_market() -> None:
    """Pre-fetch latest US closes so the watchlist's 美股昨夜 strip is
    fresh in the morning. Cheap (~30s for 7 tickers via yfinance) and
    optional — if yfinance import fails (not installed in this Python),
    we skip silently and let the strip self-tag as stale instead."""
    try:
        from backtest.prefetch_us import fetch_ticker, DEFAULT_TICKERS, _cache_path
        import json
    except ImportError as e:
        print(f"  (US refresh skipped — yfinance not available: {e})",
              flush=True)
        return
    print(f"\nRefreshing US market data ({len(DEFAULT_TICKERS)} tickers)...",
          flush=True)
    for t in DEFAULT_TICKERS:
        try:
            data = fetch_ticker(t, 5)
            with _cache_path(t).open("w") as f:
                json.dump(data, f, ensure_ascii=False)
            last = data["rows"][-1]["date"] if data["rows"] else "?"
            print(f"  {t}: {len(data['rows'])} rows (last {last})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: ERROR {e}", flush=True)


def main() -> int:
    _invalidate_today_chips_cache()
    codes = _load_watchlist()
    if not codes:
        print("Watchlist is empty — nothing to refresh.", flush=True)
        return 0
    print(f"Refreshing {len(codes)} watchlist codes ...", flush=True)
    updated, failed = [], []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            cache = _stock_cache(code)
            if cache.exists():
                cache.unlink()
            full = _load_stock(code, 30)
            if not full:
                failed.append((code, "no data"))
                print(f"  [{i}/{len(codes)}] {code}: NO DATA", flush=True)
                continue
            updated.append(code)
            print(f"  [{i}/{len(codes)}] {code}: ok", flush=True)
        except Exception as e:  # noqa: BLE001
            failed.append((code, str(e)))
            print(f"  [{i}/{len(codes)}] {code}: ERROR {e}", flush=True)
    elapsed = time.time() - t0
    print(f"\nTWSE refresh done in {elapsed:.0f}s — "
          f"updated {len(updated)}, failed {len(failed)}",
          flush=True)
    if failed:
        for c, e in failed:
            print(f"  FAILED {c}: {e}", flush=True)

    _refresh_margin_sbl()
    _refresh_us_market()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
