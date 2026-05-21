"""One entry point for the 券商分點 (broker branch) refresh.

Brackets the three-step workflow into a single command, BUT the middle
step — the BSR crawl — stays manual. The crawler (twse_web/twse_broker_
scraper.py) solves a CAPTCHA with OCR; running or chaining it into an
automated pipeline is out of scope, so this wrapper never invokes it. It
only runs the two steps it owns and pauses for the user to crawl:

    1. export_watchlist_codes.py   → twse_web/watchlist_twse.txt   (auto)
    2. twse_broker_scraper.py      → twse_web/output/*.csv         (YOU)
    3. broker_branch_ingest.py     → broker_branch_log.jsonl       (auto)

Step 3 auto-detects each file's trading day by volume, so no --date is
needed. Any extra args are forwarded to the ingest (e.g. --force, --date).

--no-crawl: skip steps 1 & 2 (export + the interactive pause) and ONLY
ingest. Use this when the crawl was already done beforehand (CSVs already
in twse_web/output/). It needs no terminal, so this is the form the 20:00
launchd job runs headlessly — the user prepares step 2 earlier in the day.

Usage:
    python3 -m tools.broker_branch_pull              # interactive
    python3 tools/broker_branch_pull.py --force
    python3 tools/broker_branch_pull.py --no-crawl   # ingest-only (scheduled)
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXPORT = REPO / "twse_web" / "export_watchlist_codes.py"
INPUT_LIST = REPO / "twse_web" / "watchlist_twse.txt"


def main() -> int:
    py = sys.executable
    # --no-crawl is consumed here; everything else is forwarded to ingest.
    raw = sys.argv[1:]
    no_crawl = "--no-crawl" in raw
    raw = [a for a in raw if a != "--no-crawl"]
    # Default the ingest fallback to today — this wrapper runs in the
    # evening (after BSR publishes the day's 分點), so an unverifiable file
    # is today's, not prev-weekday's. The volume safeguard still overrides
    # per-file when the cache can pin it. User args come AFTER so an
    # explicit --date/--fallback-date wins (argparse takes the last value).
    ingest_args = ["--fallback-date", date.today().isoformat(), *raw]

    if no_crawl:
        # Crawl was prepared earlier — go straight to ingest. No export, no
        # pause, no terminal needed (this is the headless scheduled path).
        print("券商分點 ingest（--no-crawl：步驟 2 已事先備妥）", flush=True)
    else:
        print("① 產生 watchlist 上市代號清單 …")
        subprocess.run([py, str(EXPORT)], check=True)

        print("\n" + "=" * 64)
        print("② 你來跑 — 過 CAPTCHA 的分點爬蟲（我不能代跑/自動串接）：")
        print(f"     cd twse_web && python3 twse_broker_scraper.py -i {INPUT_LIST.name}")
        print("   （可在另一個終端機跑，跑完回來這裡）")
        print("=" * 64)
        try:
            input("\n爬完後按 Enter 繼續 ingest（Ctrl-C 取消）… ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未 ingest。")
            return 1

    print("\n③ 解析 output 並寫入 log（自動對日）…", flush=True)
    subprocess.run([py, "-m", "stock_web.broker_branch_ingest", *ingest_args],
                   cwd=str(REPO), check=True)
    print("\n完成。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
