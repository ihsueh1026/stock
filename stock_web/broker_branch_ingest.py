"""Ingest hand-saved 券商分點 (broker branch) CSVs into a backtest log.

There is NO free automated source for per-branch trading data: TWSE BSR is
CAPTCHA-gated and FinMind's 分點 dataset is sponsor-only. So this is a
semi-manual workflow (mirrors the news_log one) — the user drops one CSV
per stock into `stock_web/broker_input/{code}.csv`, then we parse, compute
concentration metrics, and append one record per (code, date) to
`stock_web/broker_branch_log.jsonl`. The log accumulates over time so we
can eventually backtest whether 籌碼集中度 carries independent edge.

Input file shape (Big5, CRLF), as exported by 籌碼K線 / broker sites —
"券商買賣股票成交價量資訊" 分點分價明細, two records per row:

    券商買賣股票成交價量資訊
    股票代碼,="2379"
    序號,券商,價格,買進股數,賣出股數,,序號,券商,價格,買進股數,賣出股數
    1,1020合　　庫,561.00,70,0,,2,1020合　　庫,562.00,0,1
    ...

Each line carries a LEFT record (cols 0-4) and a RIGHT record (cols 6-10);
col 5 is a blank separator. Every (broker, price) pair is a row, so we
aggregate across all price levels per broker. 買進股數/賣出股數 are SHARES
(股); we report 張 (lots) = shares / 1000. The first 4 chars of the 券商
field are the branch code; the rest is the name (full-width-space padded).

The files contain no date. Rather than trust a calendar guess (BSR serves
its latest available day, which depends on when the crawl ran), we PIN each
file's trading day by volume: a 分點 file totals the same shares as that
day's matched volume, so we match the file's total against the per-stock
cache's daily `lots` and use the day that agrees within 3%. Falls back to
--date (or prev weekday) only when volume can't verify. Re-running is
idempotent: (code, date) already in the log is skipped unless --force.

Sources scanned (default): stock_web/broker_input/*.csv (manual paste) AND
twse_web/output/*.csv (the user's BSR crawler output). Both use the same
分點分價 layout; encoding is auto-detected (Big5 or UTF-8-BOM).

Usage:
    python3 -m stock_web.broker_branch_ingest                # both sources
    python3 -m stock_web.broker_branch_ingest --date 2026-05-19
    python3 -m stock_web.broker_branch_ingest 2379 2330      # subset
    python3 -m stock_web.broker_branch_ingest --indir twse_web/output
"""
from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
INPUT_DIR = HERE / "broker_input"
# The user's own TWSE BSR crawler (twse_web/twse_broker_scraper.py) writes
# one {code}.csv per stock here. Same 分點分價 layout as the manual paste,
# only UTF-8-BOM instead of Big5. We ingest it as a second source so the
# crawl → log step is one command. (We do NOT run the crawler — it solves
# a CAPTCHA, which is out of scope; the user runs it themselves.)
TWSE_WEB_DIR = REPO_ROOT / "twse_web" / "output"
CACHE_DIR = HERE / "cache"  # per-stock daily caches ({code}_{YYYYMMDD}.json)
LOG_PATH = HERE / "broker_branch_log.jsonl"

TOP_N = 15  # 主力 = top-N buy/sell branches by net
VOLUME_MATCH_TOL = 0.03  # 分點 total vs daily lots within 3% → same day


def _prev_weekday(d: date) -> date:
    """Most recent weekday strictly before d (skips Sat/Sun)."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat 6=Sun
        d -= timedelta(days=1)
    return d


def _stock_lots_by_date(code: str) -> dict[str, float]:
    """Return {iso-date: daily volume (張)} from the newest per-stock cache.

    Used to pin a dateless 分點 file to a trading day: BSR 分點 totals the
    same shares as the day's matched volume, so the day whose `lots` equals
    the file's 分點 total IS the file's trading day."""
    caches = sorted(CACHE_DIR.glob(f"{code}_2*.json"))
    if not caches:
        return {}
    try:
        with caches[-1].open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, float] = {}
    # Only the last few trading days are plausible — BSR serves its latest
    # day, never weeks back. Limiting the window avoids false "ambiguous"
    # hits from some unrelated old day with a coincidentally similar volume.
    for r in (data.get("rows") or [])[-6:]:
        dt, lots = r.get("date"), r.get("lots")
        if dt and lots:
            out[dt] = lots
    return out


def _detect_date(code: str, day_vol_lots: float,
                 fallback: str) -> tuple[str, str]:
    """Pin a dateless 分點 file to a trading day by volume.

    Matches the file's total volume against the stock cache's daily `lots`.
    Auto-corrects ONLY on an unambiguous hit (exactly one day within
    VOLUME_MATCH_TOL); on no-match or ambiguity it returns `fallback` with
    a warning note. Returns (date, note) — note is "" when the fallback was
    confirmed, else a human-readable reason for the date/warning.
    """
    lots = _stock_lots_by_date(code)
    if not lots:
        return fallback, "no cache to verify date"
    errs = {dt: abs(v - day_vol_lots) / v for dt, v in lots.items() if v}
    good = sorted((e, dt) for dt, e in errs.items() if e <= VOLUME_MATCH_TOL)
    if len(good) == 1:
        dt = good[0][1]
        return dt, (f"volume → {dt} (not {fallback})" if dt != fallback
                    else "")
    if not good:
        return fallback, (f"unverified — vol off by "
                          f"{min(errs.values()) * 100:.0f}%")
    return fallback, "unverified — multiple days match volume"


def _decode(path: Path) -> str:
    """Decode a 分點 CSV regardless of source encoding.

    The crawler writes UTF-8 with a BOM; manually-pasted exports are Big5.
    BOM → utf-8-sig; otherwise try strict UTF-8, falling back to Big5.
    """
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("big5", errors="replace")


def parse_file(path: Path) -> tuple[str | None, dict[str, list]]:
    """Return (stock_code, {broker_code: [name, buy_sh, sell_sh,
    buy_value, sell_value]}). Aggregates every (broker, price) row."""
    rows = list(csv.reader(io.StringIO(_decode(path))))
    code: str | None = None
    agg: dict[str, list] = {}
    for r in rows:
        if r and r[0].startswith("股票代碼") and len(r) > 1:
            code = "".join(ch for ch in r[1] if ch.isalnum())
            continue
        for off in (0, 6):  # left record, right record
            if len(r) < off + 5:
                continue
            seq, broker, price, buy, sell = r[off:off + 5]
            if not seq.strip().isdigit() or not broker.strip():
                continue
            bcode = broker[:4]
            bname = broker[4:].replace("　", " ").strip()
            try:
                p = float(price)
                b = int((buy or "0").replace(",", "") or 0)
                s = int((sell or "0").replace(",", "") or 0)
            except ValueError:
                continue
            a = agg.setdefault(bcode, [bname, 0, 0, 0.0, 0.0])
            if not a[0] and bname:
                a[0] = bname
            a[1] += b
            a[2] += s
            a[3] += p * b
            a[4] += p * s
    return code, agg


def compute_metrics(code: str, agg: dict[str, list],
                    d: str) -> dict[str, Any]:
    """Aggregate per-branch nets into concentration metrics.

    集中度 (concentration_pct) = main-force net / day volume * 100, where
    main-force net = Σ(top-15 buyers' net) + Σ(top-15 sellers' net). It is
    positive when the concentrated players are net buyers. day volume is
    total shares traded (= Σ buy = Σ sell across all branches).
    """
    tot_buy_sh = sum(a[1] for a in agg.values())
    branches = []
    for bc, (nm, bsh, ssh, bval, sval) in agg.items():
        net = bsh - ssh
        branches.append({
            "broker": bc,
            "name": nm,
            "net_lots": round(net / 1000, 2),
            "buy_lots": round(bsh / 1000, 2),
            "sell_lots": round(ssh / 1000, 2),
            "avg_buy_price": round(bval / bsh, 2) if bsh else None,
            "avg_sell_price": round(sval / ssh, 2) if ssh else None,
        })
    buyers = sorted([b for b in branches if b["net_lots"] > 0],
                    key=lambda x: -x["net_lots"])
    sellers = sorted([b for b in branches if b["net_lots"] < 0],
                     key=lambda x: x["net_lots"])
    top_buy = buyers[:TOP_N]
    top_sell = sellers[:TOP_N]

    mf_buy = sum(b["net_lots"] for b in top_buy)
    mf_sell = sum(b["net_lots"] for b in top_sell)  # negative
    mf_net = round(mf_buy + mf_sell, 2)
    day_lots = round(tot_buy_sh / 1000, 2)
    conc = round(mf_net / day_lots * 100, 2) if day_lots else None

    # Volume-weighted main-force buy cost (top-15 buyers only).
    mf_buy_val = sum((b["avg_buy_price"] or 0) * b["buy_lots"]
                     for b in top_buy)
    mf_buy_lots = sum(b["buy_lots"] for b in top_buy)
    mf_cost = round(mf_buy_val / mf_buy_lots, 2) if mf_buy_lots else None

    return {
        "date": d,
        "code": code,
        "day_volume_lots": day_lots,
        "n_branches": len(agg),
        "n_buy_branches": len(buyers),
        "n_sell_branches": len(sellers),
        "main_force_buy_lots": round(mf_buy, 2),
        "main_force_sell_lots": round(mf_sell, 2),
        "main_force_net_lots": mf_net,
        "concentration_pct": conc,
        "main_force_buy_cost": mf_cost,
        "top_buy": top_buy,
        "top_sell": top_sell,
        "fetched_at": date.today().isoformat(),
    }


def _load_existing_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if LOG_PATH.exists():
        with LOG_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    keys.add((rec["code"], rec["date"]))
                except (json.JSONDecodeError, KeyError):
                    continue
    return keys


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="subset of codes to ingest (default: all *.csv)")
    ap.add_argument("--date", default=None,
                    help="force trading date YYYY-MM-DD (overrides volume "
                         "auto-detect; default fallback: prev weekday)")
    ap.add_argument("--indir", default=None,
                    help="ingest only this directory (overrides defaults)")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest even if (code, date) already logged")
    args = ap.parse_args()

    # `d` is the FALLBACK date — used only when a file's volume can't pin
    # its trading day. Files carry no date, and the BSR-latest day depends
    # on crawl timing, so we prefer volume-verified per-file dates.
    d = args.date or _prev_weekday(date.today()).isoformat()
    existing = _load_existing_keys()

    # Sources: manual paste dir + the crawler's output dir. A given code
    # may appear in both; the within-run `seen` set keeps the first win.
    if args.indir:
        src_dirs = [Path(args.indir)]
    else:
        src_dirs = [p for p in (INPUT_DIR, TWSE_WEB_DIR) if p.is_dir()]
    files = sorted(
        (f for sd in src_dirs for f in sd.glob("*.csv")),
        key=lambda f: f.name,
    )
    if args.codes:
        want = set(args.codes)
        files = [f for f in files if f.stem in want]
    if not files:
        print("no input CSVs found in:", ", ".join(str(s) for s in src_dirs))
        return

    seen: set[tuple[str, str]] = set()
    new_records: list[dict[str, Any]] = []
    for path in files:
        code, agg = parse_file(path)
        code = code or path.stem
        if not agg:
            print(f"  [skip] {path.name}: no parseable rows")
            continue
        # Pin the trading day by volume (auto-corrects the fallback when a
        # cache match is unambiguous; warns when it can't verify). If the
        # user forced --date explicitly, still verify and warn on conflict
        # but honour their choice.
        day_vol = sum(a[1] for a in agg.values()) / 1000
        rec_date, note = _detect_date(code, day_vol, d)
        if args.date and note.startswith("volume →"):
            # explicit --date wins, but surface the disagreement
            print(f"  [warn] {code}: cache volume suggests {rec_date} "
                  f"but --date={d} forced; using {d}")
            rec_date = d
        elif rec_date != d:
            print(f"  [date] {code}: {note}")
        elif note:
            print(f"  [warn] {code}: {note}; using {d}")
        if (code, rec_date) in seen:
            continue  # already ingested from the other source this run
        if (code, rec_date) in existing and not args.force:
            print(f"  [skip] {code} {rec_date}: already logged")
            continue
        seen.add((code, rec_date))
        rec = compute_metrics(code, agg, rec_date)
        new_records.append(rec)
        conc = rec["concentration_pct"]
        print(f"  {code} {rec_date}  vol={rec['day_volume_lots']:>8,.0f}張  "
              f"主力淨={rec['main_force_net_lots']:+8,.0f}張  "
              f"集中度={conc:+.2f}%  "
              f"買家{rec['n_buy_branches']}/賣家{rec['n_sell_branches']}")

    if not new_records:
        print("nothing to write.")
        return
    with LOG_PATH.open("a") as f:
        for rec in new_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\nappended {len(new_records)} record(s) to {LOG_PATH.name}")


if __name__ == "__main__":
    main()
