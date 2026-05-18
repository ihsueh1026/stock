"""4★+綠+持有非黃 missing-condition study.

For each 4★ reversal event that already passed our production gates
(法人=綠, 持有≠黃), bucket by WHICH of the 5 reversal-quality
conditions is the missing one, and measure forward alpha at
5/10/20/40 days.

The 5 conditions (from `_reversal_quality()` in stock_web/app.py):
  C1 — 近 20 日低點 (≤2%)        — close near 20d low
  C2 — 前期跌幅 ≥7.5%             — drawdown from 20d peak
  C3 — K 超賣 (<20)               — KD oversold
  C4 — RSI6 偏低 (<35)            — momentum oversold
  C5 — 量比 ≥1.0                  — volume not drying up

Why this is interesting: a 4★+綠 chip's per-stock alpha varies, and
"star order" — which 4 of the 5 happen to be true — is the cheapest
proxy for "what shape of reversal is this exactly?". If e.g. missing
C5 (volume) gives worse alpha than missing C3 (K), that's actionable:
either the chip should warn on the bad sub-shape, or we drop the
contribution of the weak condition.

Event = exact-score 4★ crossing (first bar where score crosses from
non-4 into 4), with step 7 法人=綠 and step 5 持有 ≠ yellow at event
bar — mirrors the live emission rule in `_compute_alerts` and the
chip pool in `build_stats._chip_events_for_code`.

Usage:
    python3 -m backtest.reversal_4star_missing_study
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, _reversal_quality, MARKET_TWSE,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS,
    forward_return, forward_alpha,
    load_series, load_taiex, _compute_lights,
)
from backtest.reversal_quality_study import find_exact_events  # noqa: E402


INST_IDX = 6   # step 7 法人
HOLD_IDX = 4   # step 5 持有

# Stable index/label of each reversal_quality check (order matches
# checks[] in _reversal_quality()).
COND_LABELS = [
    "C1 近 20 日低",
    "C2 前期跌幅 ≥7.5%",
    "C3 K<20",
    "C4 RSI6<35",
    "C5 量比 ≥1.0",
]


def _missing_index(rows: list[dict], idx: int) -> int | None:
    """Return 0..4 = which of the 5 conditions is missing at rows[idx].
    Returns None if window invalid or score != 4 (sanity)."""
    if idx < 19:
        return None
    window = rows[max(0, idx - 19): idx + 1]
    rq = _reversal_quality(window)
    if not rq or rq["score"] != 4:
        return None
    for i, c in enumerate(rq["checks"]):
        if not c["passed"]:
            return i
    return None


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _summarize(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "win_pct": sum(1 for v in vals if v > 0) / len(vals),
    }


def _fmt(x):
    if x is None or x != x:
        return "  n/a "
    return f"{x * 100:+6.2f}%"


def run(codes: list[str]) -> None:
    taiex = load_taiex()
    # buckets[missing_idx][horizon] = {ret: [], alpha: []}
    buckets = {
        i: {h: {"ret": [], "alpha": []} for h in HORIZONS}
        for i in range(5)
    }
    # baseline = all 4★+綠+持有非黃 pooled, regardless of missing slot
    baseline = {h: {"ret": [], "alpha": []} for h in HORIZONS}
    total = 0
    skipped_inst = 0
    skipped_hold = 0

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE

        for idx in find_exact_events(rows, target=4):
            _, steps = _compute_lights(rows, idx, code=code, market=market)
            if not steps or len(steps) <= INST_IDX:
                continue
            if steps[INST_IDX]["light"] != "green":
                skipped_inst += 1
                continue
            if len(steps) > HOLD_IDX and steps[HOLD_IDX]["light"] == "yellow":
                skipped_hold += 1
                continue
            miss = _missing_index(rows, idx)
            if miss is None:
                continue
            total += 1
            for h in HORIZONS:
                r = forward_return(rows, idx, h)
                a = forward_alpha(rows, idx, h)
                if r is not None:
                    buckets[miss][h]["ret"].append(r)
                    baseline[h]["ret"].append(r)
                if a is not None:
                    buckets[miss][h]["alpha"].append(a)
                    baseline[h]["alpha"].append(a)

    print(f"\n4★+綠+持有非黃 missing-condition study")
    print(f"Universe: {len(codes)} codes")
    print(f"Total qualifying events: {total} "
          f"(excluded {skipped_inst} non-綠 法人, {skipped_hold} 黃 持有)\n")

    # Baseline header
    print("Baseline (all qualifying 4★+綠+持有非黃):")
    print(f"  {'h':>3}  {'n':>4}  {'alpha_med':>10}  {'alpha_mean':>10}  {'win%':>6}")
    for h in HORIZONS:
        s = _summarize(baseline[h]["alpha"])
        if s:
            print(f"  {h:>3}  {s['n']:>4}  "
                  f"{_fmt(s['median']):>10}  "
                  f"{_fmt(s['mean']):>10}  "
                  f"{s['win_pct'] * 100:>5.1f}%")
    print()

    # Per missing-condition table
    for miss_idx in range(5):
        label = COND_LABELS[miss_idx]
        print(f"Missing → {label}")
        print(f"  (這個條件是缺的;其他 4 個都對)")
        print(f"  {'h':>3}  {'n':>4}  {'alpha_med':>10}  {'alpha_mean':>10}  "
              f"{'win%':>6}  {'Δvs base':>9}")
        for h in HORIZONS:
            s = _summarize(buckets[miss_idx][h]["alpha"])
            base = _summarize(baseline[h]["alpha"])
            if not s:
                print(f"  {h:>3}  {0:>4}  (no events)")
                continue
            delta = ((s["median"] - base["median"]) * 100
                     if (s["median"] is not None and base
                         and base["median"] is not None) else None)
            dstr = f"{delta:+5.2f}pp" if delta is not None else "   n/a"
            print(f"  {h:>3}  {s['n']:>4}  "
                  f"{_fmt(s['median']):>10}  "
                  f"{_fmt(s['mean']):>10}  "
                  f"{s['win_pct'] * 100:>5.1f}%  "
                  f"{dstr:>9}")
        print()

    # Rank table at each horizon — easiest summary read
    print("─" * 70)
    print("Rank by 40d alpha median (best → worst):")
    h_pick = 40
    rows_rank = []
    for miss_idx in range(5):
        s = _summarize(buckets[miss_idx][h_pick]["alpha"])
        if not s:
            continue
        rows_rank.append((s["median"], miss_idx, s["n"], s["win_pct"]))
    rows_rank.sort(reverse=True)
    for med, miss_idx, n, wp in rows_rank:
        print(f"  Missing {COND_LABELS[miss_idx]:<20}  "
              f"n={n:>3}  alpha={_fmt(med)}  win={wp * 100:>5.1f}%")
    print()


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    run(codes)


if __name__ == "__main__":
    main()
