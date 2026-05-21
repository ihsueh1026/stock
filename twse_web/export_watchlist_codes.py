#!/usr/bin/env python3
"""Export the dashboard watchlist's 上市 (TWSE) codes for the BSR crawler.

Reads ../stock_web/watchlist.json, resolves each code's market against the
newest daily company-info dumps in ../stock_web/cache/, and writes the 上市
codes (one per line) to twse_web/watchlist_twse.txt — the input file for
twse_broker_scraper.py:

    python3 export_watchlist_codes.py
    python3 twse_broker_scraper.py -i watchlist_twse.txt

上櫃 (OTC) codes are reported but excluded — BSR covers them too, but the
user asked to integrate 上市 only. Run this whenever the watchlist changes
so the crawler input stays in sync.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
WATCHLIST = REPO_ROOT / "stock_web" / "watchlist.json"
CACHE_DIR = REPO_ROOT / "stock_web" / "cache"
OUT_FILE = HERE / "watchlist_twse.txt"


def _newest(pattern: str) -> str | None:
    files = sorted(glob.glob(str(CACHE_DIR / pattern)))
    return files[-1] if files else None


def _codes_in(path: str | None) -> set[str]:
    if not path:
        return set()
    data = json.load(open(path))
    if isinstance(data, dict):
        return set(data.keys())
    return {str(r.get("code") or r.get("公司代號") or "") for r in data}


def main() -> None:
    wl = json.load(open(WATCHLIST))["codes"]
    # companies_otc_*.json must be matched before companies_*.json so the
    # OTC dump isn't swallowed by the broader glob.
    otc = _codes_in(_newest("companies_otc_*.json"))
    twse = _codes_in(_newest("companies_2*.json"))

    twse_codes, otc_codes, unknown = [], [], []
    for c in wl:
        if c in otc:
            otc_codes.append(c)
        elif c in twse:
            twse_codes.append(c)
        else:
            unknown.append(c)  # new listing not yet in dump → treat as TWSE
    twse_codes += unknown

    OUT_FILE.write_text(
        "# watchlist 上市 codes for twse_broker_scraper.py\n"
        "# regenerate via export_watchlist_codes.py\n"
        + "\n".join(twse_codes) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(twse_codes)} 上市 codes → {OUT_FILE.name}")
    print("上市:", " ".join(twse_codes))
    if otc_codes:
        print(f"skipped {len(otc_codes)} 上櫃 (BSR covers them, but excluded "
              f"per 上市-only request):", " ".join(otc_codes))
    if unknown:
        print("note: not found in dumps, defaulted to 上市:",
              " ".join(unknown))


if __name__ == "__main__":
    main()
