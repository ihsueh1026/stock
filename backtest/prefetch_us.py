"""Fetch daily OHLCV for US indices + tech leaders via yfinance.

Sources daily closes used by:
  - backtest/us_correlation_study.py   — lag-0/lag-1 correlations vs TWSE
  - backtest/sox_regime_chip_study.py  — SOX bear regime chip-conditioning

Why yfinance: Yahoo Finance v8 chart API works for the live web app
but rate-limits aggressively per-IP at the WAF (429 after 1-2 raw
requests). yfinance handles the crumb + cookie session dance and
falls back across endpoints, which is what we need for a bulk
backtest backfill. Pip dep: `pip3 install --user yfinance`.

Stooq was the obvious backup but they put their /q/d/l/?s= CSV
endpoint behind an API key gate, so the simple HTTP route is dead.

Output: backtest/data/_us_{TICKER}.json shaped as:
  {"ticker": "^SOX", "rows": [{"date": "YYYY-MM-DD",
    "open": float, "close": float, "high": float, "low": float,
    "volume": int|null}, ...]}

Usage:
    python3 -m backtest.prefetch_us           # default tickers, 5y
    python3 -m backtest.prefetch_us --years 3 NVDA TSM
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "backtest" / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_TICKERS = ["^DJI", "^IXIC", "^SOX",
                   "NVDA", "TSM", "AAPL", "GOOGL"]


def _sanitize_filename(ticker: str) -> str:
    """`^SOX` → `SOX`, `BRK-A` → `BRK_A`, etc."""
    return ticker.replace("^", "").replace("-", "_").replace(".", "_")


def _cache_path(ticker: str) -> Path:
    return DATA_DIR / f"_us_{_sanitize_filename(ticker)}.json"


def _safe_float(v):
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    f = _safe_float(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def fetch_ticker(ticker: str, years: int) -> dict:
    end = date.today()
    start = end - timedelta(days=365 * years + 10)
    # auto_adjust=False keeps raw close (we want price levels for
    # drawdown / regime detection, not split-adjusted return series).
    df = yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        raise ValueError(f"{ticker}: yfinance returned empty frame")
    # yfinance may return a MultiIndex with the ticker as level 1
    # when called with a single ticker — flatten it.
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    rows = []
    for ts, r in df.iterrows():
        d = ts.date().isoformat()
        close = _safe_float(r.get("Close"))
        if close is None:
            continue
        rows.append({
            "date": d,
            "open":  _safe_float(r.get("Open")),
            "high":  _safe_float(r.get("High")),
            "low":   _safe_float(r.get("Low")),
            "close": close,
            "volume": _safe_int(r.get("Volume")),
        })
    rows.sort(key=lambda r: r["date"])
    return {"ticker": ticker, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*",
                    help=f"Tickers; default = {DEFAULT_TICKERS}")
    ap.add_argument("--years", type=int, default=5,
                    help="History depth in years (default 5)")
    args = ap.parse_args()

    tickers = args.tickers or DEFAULT_TICKERS
    print(f"Fetching {len(tickers)} tickers × {args.years}y via yfinance ...",
          flush=True)
    for i, ticker in enumerate(tickers, 1):
        try:
            data = fetch_ticker(ticker, args.years)
        except Exception as e:
            print(f"  [{i}/{len(tickers)}] {ticker}: ERROR {e}", flush=True)
            continue
        p = _cache_path(ticker)
        with p.open("w") as f:
            json.dump(data, f, ensure_ascii=False)
        n = len(data["rows"])
        first = data["rows"][0]["date"] if n else "?"
        last = data["rows"][-1]["date"] if n else "?"
        print(f"  [{i}/{len(tickers)}] {ticker}: {n} rows "
              f"({first} → {last}) → {p.name}", flush=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
