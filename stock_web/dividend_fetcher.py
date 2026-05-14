"""Fetch per-stock dividend yield + per-share cash dividend from TWSE/TPEX OpenAPI.

Two daily, whole-market dumps are used:

  - TWSE (上市):
      https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL
      Fields: Date Code Name PEratio DividendYield PBratio
      (殖利率 only — no explicit dividend amount)

  - TPEX (上櫃):
      https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis
      Fields: Date SecuritiesCompanyCode CompanyName PriceEarningRatio
              DividendPerShare YieldRatio PriceBookRatio
      (Both yield + per-share dividend.)

We normalize to a single shape: {yield_pct, cash_dividend, src_date}.
TWSE's `cash_dividend` is derived as yield * last_close / 100; the
yield in the file is calculated against the previous trading day's
close, so derived amount is a tight approximation. TPEX gives the
amount directly.

Cache: `dividends_{twse|otc}_{YYYYMMDD}.json` — daily refresh on
the standard purge cycle.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
OTC_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

REQUEST_TIMEOUT_SEC = 20
REQUEST_INTERVAL_SEC = 1

MARKET_TWSE = "twse"
MARKET_OTC = "otc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-web/1.0)",
    "Accept": "application/json",
}

_last_req_ts: float = 0.0


def _throttle() -> None:
    global _last_req_ts
    elapsed = time.time() - _last_req_ts
    if elapsed < REQUEST_INTERVAL_SEC:
        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
    _last_req_ts = time.time()


def _today_tag() -> str:
    return date.today().strftime("%Y%m%d")


def _cache_path(market: str) -> Path:
    seg = "twse" if market == MARKET_TWSE else "otc"
    return CACHE_DIR / f"dividends_{seg}_{_today_tag()}.json"


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_twse_all() -> dict[str, dict[str, Any]]:
    """Return {code: {yield_pct, cash_dividend, src_date, name}}."""
    _throttle()
    try:
        r = requests.get(TWSE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] TWSE dividend fetch failed: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row.get("Code") or ""
        if not code:
            continue
        y = _to_float(row.get("DividendYield"))
        # TWSE file doesn't include the dividend amount directly; the
        # caller can derive cash_div ≈ y * close / 100 with the latest
        # close. Leave it None here so a stale derivation isn't baked in.
        out[code] = {
            "yield_pct": y,
            "cash_dividend": None,
            "src_date": str(row.get("Date") or ""),
            "name": row.get("Name") or "",
            "market": MARKET_TWSE,
        }
    return out


def _fetch_otc_all() -> dict[str, dict[str, Any]]:
    """Return {code: {yield_pct, cash_dividend, src_date, name}}."""
    _throttle()
    try:
        r = requests.get(OTC_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] TPEX dividend fetch failed: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row.get("SecuritiesCompanyCode") or ""
        if not code:
            continue
        out[code] = {
            "yield_pct": _to_float(row.get("YieldRatio")),
            "cash_dividend": _to_float(row.get("DividendPerShare")),
            "src_date": str(row.get("Date") or ""),
            "name": row.get("CompanyName") or "",
            "market": MARKET_OTC,
        }
    return out


def fetch_all(market: str = MARKET_TWSE) -> dict[str, dict[str, Any]]:
    """Daily-cached whole-market dump."""
    p = _cache_path(market)
    if p.exists():
        try:
            with p.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    data = _fetch_twse_all() if market == MARKET_TWSE else _fetch_otc_all()
    if data:
        try:
            with p.open("w") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            print(f"  [warn] dividend cache write failed {p}: {e}",
                  file=sys.stderr)
    return data


def get_for_code(code: str, market: str = MARKET_TWSE,
                 last_close: float | None = None) -> dict[str, Any] | None:
    """Return one stock's dividend snapshot, or None if not listed.

    `last_close` lets us derive an approximate cash-dividend-per-share
    for TWSE (which only ships the yield). For TPEX the per-share value
    is taken straight from the source file.
    """
    data = fetch_all(market)
    row = data.get(code)
    if not row:
        return None
    cash = row.get("cash_dividend")
    y = row.get("yield_pct")
    if cash is None and y is not None and last_close:
        cash = round(y * last_close / 100, 2)
    return {
        "code": code,
        "name": row.get("name") or "",
        "market": market,
        "yield_pct": y,
        "cash_dividend": cash,
        "src_date": row.get("src_date"),
        "derived": (row.get("cash_dividend") is None and cash is not None),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--market", default="twse", choices=["twse", "otc"])
    ap.add_argument("--close", type=float, default=None)
    args = ap.parse_args()
    for code in args.codes:
        d = get_for_code(code, market=args.market, last_close=args.close)
        if not d:
            print(f"{code}: no data")
            continue
        y = d["yield_pct"]
        c = d["cash_dividend"]
        y_s = f"{y:.2f}%" if y is not None else "n/a"
        c_s = f"{c:.2f}" if c is not None else "n/a"
        deriv = " (derived)" if d["derived"] else ""
        print(f"{code} {d['name']}  殖利率 {y_s}  現金股利 {c_s}{deriv}  "
              f"({d['src_date']})")


if __name__ == "__main__":
    main()
