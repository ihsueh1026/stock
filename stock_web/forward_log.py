"""Forward-looking chip validation log.

Captures each chip emission day-of-trigger, then backfills the
actual 5/10/20/40-day forward alpha when those horizons mature.
The log accumulates true OOS samples (post-deploy), letting us
compare what the chip *predicted* (from `_summary_stats.json`,
which is computed on the same data the chip was tuned on) against
what it *actually delivered* on dates the original calibration
never saw.

Persistence model:
- JSONL at `stock_web/forward_log.jsonl` — append-only writer,
  atomic-rewrite when filling. Sits OUTSIDE `cache/` so the 7-day
  purge in `_purge_old_caches` doesn't touch it.
- Each line is one emission record. Schema is stable; new fields
  can be added at the tail (old records simply lack them).

Capture model (Q1 = "only in watchlist_chips"):
- The capture hook lives inside the watchlist chip scan, not on
  every compute_dashboard call. One emission per (code, stat_key,
  trading-day). Continuation detection: if the same (code,
  stat_key) was logged on the previous trading day, today is a
  continuation, not a first-cross, and we skip.

Fill model (Q2 = "lazy + cron"):
- Lazy: every read of /api/forward_log/summary runs `fill_matured_records`
  first, so the user always sees up-to-date numbers.
- Cron: a daemon thread runs the same fill once every 6 hours so
  long-running servers with quiet API traffic stay current.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date as _date
from pathlib import Path
from typing import Callable, Optional

LOG_PATH = Path(__file__).resolve().parent / "forward_log.jsonl"
HORIZONS = (5, 10, 20, 40)

# Single writer lock — JSONL append is small enough to be atomic
# on POSIX without locking, but we still serialize against the
# atomic-rewrite path (fill operation rewrites the whole file).
_log_lock = threading.Lock()


def _read_all() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out: list[dict] = []
    with LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # Corrupt line — skip rather than fail the whole read.
                continue
    return out


def _write_all(records: list[dict]) -> None:
    """Atomic rewrite via temp file + rename."""
    tmp = LOG_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, LOG_PATH)


def _append(record: dict) -> None:
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _index_emitted() -> dict[tuple[str, str], set[str]]:
    """Build a (code, stat_key) → set(emitted_at) index from disk.
    Used by `is_recent` for the first-cross dedup check, and by
    batch capture to avoid re-reading the file per record."""
    idx: dict[tuple[str, str], set[str]] = {}
    for r in _read_all():
        key = (r.get("code"), r.get("stat_key"))
        if key[0] and key[1]:
            idx.setdefault(key, set()).add(r.get("emitted_at"))
    return idx


def log_emissions(records: list[dict], prev_trading_day_per_code: dict[str, str]) -> int:
    """Batch-append `records` after first-cross dedup.

    For each record, skip when the same (code, stat_key) was emitted
    on the previous trading day (continuation), and also when an
    entry for the same (code, stat_key, emitted_at) already exists
    in the log (replay safety — e.g. server restart, manual replay).

    Returns the number of records actually written.
    """
    if not records:
        return 0
    with _log_lock:
        idx = _index_emitted()
        written = 0
        for r in records:
            key = (r.get("code"), r.get("stat_key"))
            emitted_at = r.get("emitted_at")
            if not key[0] or not key[1] or not emitted_at:
                continue
            existing = idx.get(key, set())
            # Skip if today already logged
            if emitted_at in existing:
                continue
            # Skip if previous trading day for this code had the same chip
            prev_day = prev_trading_day_per_code.get(key[0])
            if prev_day and prev_day in existing:
                continue
            # Initialize the alpha slots so the schema is stable.
            for h in HORIZONS:
                r.setdefault(f"alpha_{h}d", None)
            r.setdefault("filled_at", None)
            _append(r)
            existing.add(emitted_at)
            idx[key] = existing
            written += 1
    return written


def fill_matured_records(load_stock_rows: Callable[[str], Optional[list[dict]]]) -> int:
    """Scan every record's null alpha fields and fill those whose
    horizon has matured (i.e. emit_idx + h < len(rows) for the
    stock's currently-cached series).

    `load_stock_rows(code) → list[dict] | None` reads the stock's
    cached price series. Cache windows are typically 13 months, so
    even the 40d horizon is reachable as long as the emission isn't
    older than ~12 months.

    Returns the count of alpha fields newly filled.
    """
    records = _read_all()
    if not records:
        return 0
    # Group records by code so we read each stock's series only once.
    by_code: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        c = r.get("code")
        if c:
            by_code.setdefault(c, []).append(i)
    filled_total = 0
    changed = False
    for code, idxs in by_code.items():
        # Skip if no record needs filling for this code
        if all(
            all(records[i].get(f"alpha_{h}d") is not None for h in HORIZONS)
            for i in idxs
        ):
            continue
        rows = load_stock_rows(code)
        if not rows:
            continue
        # Build a date → index map for O(1) lookups.
        date_idx = {row.get("date"): i for i, row in enumerate(rows)}
        for ri in idxs:
            r = records[ri]
            emit_idx = date_idx.get(r.get("emitted_at"))
            if emit_idx is None:
                continue
            close_at_emit = r.get("close_at_emit")
            taiex_at_emit = r.get("taiex_at_emit")
            if close_at_emit is None or taiex_at_emit is None:
                continue
            for h in HORIZONS:
                key = f"alpha_{h}d"
                if r.get(key) is not None:
                    continue
                target_idx = emit_idx + h
                if target_idx >= len(rows):
                    continue  # not yet matured
                target = rows[target_idx]
                close_then = target.get("close")
                taiex_then = target.get("taiex")
                if close_then is None or taiex_then is None:
                    continue
                stock_ret = (close_then / close_at_emit) - 1.0
                taiex_ret = (taiex_then / taiex_at_emit) - 1.0
                r[key] = stock_ret - taiex_ret
                filled_total += 1
                changed = True
            if r.get("filled_at") is None and any(
                r.get(f"alpha_{h}d") is not None for h in HORIZONS
            ):
                r["filled_at"] = _date.today().isoformat()
    if changed:
        with _log_lock:
            _write_all(records)
    return filled_total


def summarize(pool_stats: Optional[dict] = None) -> dict:
    """Aggregate matured records per chip; compare actual to pool prediction.

    `pool_stats` is the loaded `_summary_stats.json` content; if
    provided, the per-chip rows include `pool_alpha` from there and
    a `delta = actual - pool` field for calibration.
    """
    records = _read_all()
    pool_chips = (pool_stats or {}).get("chips", {})
    by_chip: dict[str, dict[int, list[float]]] = {}
    counts: dict[str, int] = {}
    for r in records:
        sk = r.get("stat_key")
        if not sk:
            continue
        counts[sk] = counts.get(sk, 0) + 1
        b = by_chip.setdefault(sk, {h: [] for h in HORIZONS})
        for h in HORIZONS:
            v = r.get(f"alpha_{h}d")
            if v is not None:
                b[h].append(v)

    def _median(xs: list[float]) -> Optional[float]:
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def _winrate(xs: list[float]) -> Optional[float]:
        if not xs:
            return None
        return sum(1 for x in xs if x > 0) / len(xs)

    out_chips: dict[str, dict] = {}
    for sk, buckets in by_chip.items():
        pool_horizons = (pool_chips.get(sk) or {}).get("horizons", {})
        rows = {}
        for h in HORIZONS:
            actual = buckets[h]
            actual_alpha = _median(actual)
            pool_alpha = (pool_horizons.get(str(h)) or {}).get("alpha_med")
            delta = (
                actual_alpha - pool_alpha
                if (actual_alpha is not None and pool_alpha is not None)
                else None
            )
            rows[str(h)] = {
                "n_matured": len(actual),
                "actual_alpha": actual_alpha,
                "actual_win": _winrate(actual),
                "pool_alpha": pool_alpha,
                "delta": delta,
            }
        out_chips[sk] = {
            "count": counts.get(sk, 0),
            "horizons": rows,
        }

    first = min((r.get("emitted_at") for r in records if r.get("emitted_at")), default=None)
    return {
        "total_emissions": len(records),
        "first_emission": first,
        "by_chip": out_chips,
    }
