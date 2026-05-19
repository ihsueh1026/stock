"""Sweep S4 (借券 5日下降) threshold + measure per-stock breadth.

Headline finding from `backtest/margin_sbl_study.py` v1: S4 at
threshold ≤ -20% delivers 40d alpha -0.30% / 49% win vs baseline
-2.28% / 43%, a +1.98pp delta — just at the ship-worthy edge.

Two questions this sweep answers:
  1. Is -20% the sweet spot, or does a tighter / looser threshold
     widen the edge?
  2. Is the pool-level edge broadly distributed across codes, or
     concentrated in a few names? (Per-stock breadth test —
     analog to the EPS state study which split 48/52.)

Run after backfill_margin_sbl:
    python3 -m backtest.s4_sbl_covering_sweep
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import _compute_rows  # noqa: E402
from stock_web.margin_sbl_fetcher import (  # noqa: E402
    _load_cached_margin, _load_cached_sbl,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, load_series, load_taiex, forward_alpha,
)

THRESHOLDS = [-0.10, -0.15, -0.20, -0.25, -0.30, -0.40, -0.50]
LOOKBACK_DAYS = 5


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _build_sbl_series(code: str, dates: list[str]) -> dict[str, int | None]:
    out = {}
    for d in dates:
        s = _load_cached_sbl(d)
        row = (s or {}).get(code) if s else None
        if not row:
            continue
        out[d] = row.get("bal_today")
    return out


def _summarize(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return {
        "n": len(v),
        "median": statistics.median(v),
        "mean": statistics.mean(v),
        "win_pct": sum(1 for x in v if x > 0) / len(v),
    }


def _fmt(x):
    if x is None or x != x:
        return "  n/a "
    return f"{x * 100:+6.2f}%"


def run(codes: list[str]):
    taiex = load_taiex()

    # threshold -> horizon -> list[alpha]
    by_thresh: dict[float, dict[int, list]] = {
        t: {h: [] for h in HORIZONS} for t in THRESHOLDS
    }
    baseline: dict[int, list] = {h: [] for h in HORIZONS}
    # per-stock 40d alpha for the headline threshold
    per_code_40d: dict[float, dict[str, list[float]]] = {
        t: defaultdict(list) for t in THRESHOLDS
    }
    eligible_codes = 0

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        dates = [r["date"] if isinstance(r["date"], str)
                 else r["date"].isoformat() for r in rows]
        sbl_series = _build_sbl_series(code, dates)
        if len(sbl_series) < LOOKBACK_DAYS + 10:
            continue
        eligible_codes += 1

        # Track last-fired state per threshold for first-cross events
        last_fired: dict[float, bool] = {t: False for t in THRESHOLDS}

        for i, d in enumerate(dates):
            if d not in sbl_series:
                for t in THRESHOLDS:
                    last_fired[t] = False
                continue
            prior = [pd for pd in dates[:i] if pd in sbl_series]
            if len(prior) < LOOKBACK_DAYS:
                continue
            prev_key = prior[-LOOKBACK_DAYS]
            now_v = sbl_series[d]
            prev_v = sbl_series[prev_key]
            if now_v is None or prev_v is None or prev_v == 0:
                for t in THRESHOLDS:
                    last_fired[t] = False
                continue
            chg = (now_v - prev_v) / prev_v
            # Baseline (every analyzable bar)
            for h in HORIZONS:
                a = forward_alpha(rows, i, h)
                if a is not None:
                    baseline[h].append(a)
            # Per-threshold first-cross
            for t in THRESHOLDS:
                fires = chg <= t
                if fires and not last_fired[t]:
                    for h in HORIZONS:
                        a = forward_alpha(rows, i, h)
                        if a is not None:
                            by_thresh[t][h].append(a)
                            if h == 40:
                                per_code_40d[t][code].append(a)
                last_fired[t] = fires

    print(f"\nS4 sweep — 借券 5日下降 N% threshold sweep")
    print(f"Universe: {eligible_codes} codes\n")
    print(f"Baseline:")
    print(f"  {'h':>3}  {'n':>5}  {'alpha_med':>10}  {'win%':>6}")
    for h in HORIZONS:
        s = _summarize(baseline[h])
        if s:
            print(f"  {h:>3}  {s['n']:>5}  {_fmt(s['median']):>10}  "
                  f"{s['win_pct'] * 100:>5.1f}%")
    print()

    print(f"Per-threshold (all horizons):")
    print(f"  {'thresh':<8} {'h':>3} {'n':>5}  {'alpha_med':>10}  {'win%':>6}  {'Δbase':>8}")
    print("  " + "-" * 60)
    base_med = {h: (_summarize(baseline[h]) or {}).get("median") for h in HORIZONS}
    for t in THRESHOLDS:
        for h in HORIZONS:
            s = _summarize(by_thresh[t][h])
            if not s:
                continue
            delta = ((s["median"] - base_med[h]) * 100
                     if base_med[h] is not None else None)
            if delta is not None:
                dstr = f"{delta:+5.2f}pp"
            else:
                dstr = "   n/a"
            t_str = f"≤{t * 100:.0f}%"
            print(f"  {t_str:<8} {h:>3} {s['n']:>5}  "
                  f"{_fmt(s['median']):>10}  {s['win_pct'] * 100:>5.1f}%  "
                  f"{dstr:>8}")
        print()

    # Per-stock breadth for each threshold at 40d
    print(f"\nPer-stock breadth at 40d (codes with ≥3 events):")
    print(f"  {'thresh':<8} {'codes':>5} {'+':>3} {'-':>3} {'~0':>3}  {'breadth':>8}")
    for t in THRESHOLDS:
        pos = neg = zero = 0
        for code, vals in per_code_40d[t].items():
            if len(vals) < 3:
                continue
            m = statistics.median(vals)
            # delta vs THIS CODE's baseline would be ideal but we don't
            # have that handy; using pool baseline as quick proxy
            if base_med[40] is not None:
                delta_to_base = m - base_med[40]
                if delta_to_base > 0.01: pos += 1
                elif delta_to_base < -0.01: neg += 1
                else: zero += 1
            else:
                if m > 0: pos += 1
                elif m < 0: neg += 1
                else: zero += 1
        total = pos + neg + zero
        if total == 0:
            print(f"  ≤{t * 100:.0f}%      0  (no codes with n≥3)")
            continue
        breadth = pos / total * 100
        print(f"  ≤{t * 100:.0f}%       {total:>3} {pos:>3} {neg:>3} {zero:>3}  "
              f"{breadth:>6.0f}%")
    print()
    print("'+' = code's 40d median alpha exceeds pool baseline by ≥1pp")
    print("'-' = falls short of pool baseline by ≥1pp")
    print("'~0' = within ±1pp of pool baseline")


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    run(codes)


if __name__ == "__main__":
    main()
