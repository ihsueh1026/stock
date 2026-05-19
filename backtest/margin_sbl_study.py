"""Margin / SBL signal event study on the 50-stock universe.

Builds per-stock daily series of (融資餘額, 融券餘額, 借券餘額,
資券比, 融資使用率) from the whole-market caches written by
`backtest/prefetch_margin_sbl.py`, then sweeps a few signal
hypotheses and measures forward TAIEX-stripped alpha at 5/10/20/40
trading days.

Signals tested (first-cross events — transition from non-event to
event state, reset when condition lapses):

  S1   融資 5日變化 ≥ +5%      retail crowding into long
  S2   融資 5日變化 ≤ -5%      retail unwinding
  S3   借券 5日變化 ≥ +20%     institutional short build-up
  S4   借券 5日變化 ≤ -20%     short covering
  S5   融券 5日變化 ≥ +30%     retail short add (short-squeeze setup?)
  S6   資券比 < 10            very heavy short relative to long
  S7   資券比 > 200            very heavy long relative to short
  S8   融資使用率 ≥ 50%        margin quota stretched (NOT a TWSE
                              forced-sell threshold — that's ~90% —
                              just a "leverage rising" tag)

Each signal is binary (fires / doesn't). For shipped chips we'd
want per-stock breadth like the EPS study did (pool stat vs
per-code positive median).

Universe: 50 codes under backtest/data/, OHLC + TAIEX already
computed by `backtest.study._compute_rows`.

Usage (after running prefetch_margin_sbl):
    python3 -m backtest.margin_sbl_study
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import date
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

# Signal threshold parameters — kept centralised for sweep iteration.
S1_F_UP_PCT   = 0.05
S2_F_DOWN_PCT = -0.05
S3_SBL_UP_PCT   = 0.20
S4_SBL_DOWN_PCT = -0.20
S5_S_UP_PCT   = 0.30
S6_RATIO_LOW  = 10
S7_RATIO_HIGH = 200
S8_USAGE_HI   = 50.0
LOOKBACK_DAYS = 5   # window for N-day % changes


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


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


def _build_margin_sbl_series(code: str, dates: list[str]) -> dict[str, dict]:
    """Walk each date's whole-market cache, extract this stock's row.
    Returns {date: {f, s, sbl, f_limit}} for dates that had data."""
    out: dict[str, dict] = {}
    for d in dates:
        m = _load_cached_margin(d)
        s = _load_cached_sbl(d)
        m_row = (m or {}).get(code) if m else None
        s_row = (s or {}).get(code) if s else None
        if not m_row and not s_row:
            continue
        out[d] = {
            "f": (m_row or {}).get("f_today"),
            "s": (m_row or {}).get("s_today"),
            "sbl": (s_row or {}).get("bal_today"),
            "f_limit": (m_row or {}).get("f_limit"),
        }
    return out


def _ratio_pct(now, prev):
    if now is None or prev is None or prev == 0:
        return None
    return (now - prev) / prev


def _classify(snapshot: dict) -> set[str]:
    """Given a row dict {f_now, f_prev, s_now, s_prev, sbl_now, sbl_prev,
    ratio, usage}, return the set of signal keys it currently fires."""
    out: set[str] = set()
    f_chg = _ratio_pct(snapshot.get("f_now"), snapshot.get("f_prev"))
    s_chg = _ratio_pct(snapshot.get("s_now"), snapshot.get("s_prev"))
    sbl_chg = _ratio_pct(snapshot.get("sbl_now"), snapshot.get("sbl_prev"))
    ratio = snapshot.get("ratio")
    usage = snapshot.get("usage")
    if f_chg is not None and f_chg >= S1_F_UP_PCT:   out.add("S1")
    if f_chg is not None and f_chg <= S2_F_DOWN_PCT: out.add("S2")
    if sbl_chg is not None and sbl_chg >= S3_SBL_UP_PCT:   out.add("S3")
    if sbl_chg is not None and sbl_chg <= S4_SBL_DOWN_PCT: out.add("S4")
    if s_chg is not None and s_chg >= S5_S_UP_PCT:   out.add("S5")
    if ratio is not None and ratio < S6_RATIO_LOW:   out.add("S6")
    if ratio is not None and ratio > S7_RATIO_HIGH:  out.add("S7")
    if usage is not None and usage >= S8_USAGE_HI:   out.add("S8")
    return out


SIGNAL_DESCS = {
    "S1": f"融資 5日↑ ≥ +{S1_F_UP_PCT * 100:.0f}%",
    "S2": f"融資 5日↓ ≤ {S2_F_DOWN_PCT * 100:.0f}%",
    "S3": f"借券 5日↑ ≥ +{S3_SBL_UP_PCT * 100:.0f}%",
    "S4": f"借券 5日↓ ≤ {S4_SBL_DOWN_PCT * 100:.0f}%",
    "S5": f"融券 5日↑ ≥ +{S5_S_UP_PCT * 100:.0f}%",
    "S6": f"資券比 < {S6_RATIO_LOW}",
    "S7": f"資券比 > {S7_RATIO_HIGH}",
    "S8": f"融資使用率 ≥ {S8_USAGE_HI:.0f}%",
}


def run(codes: list[str]):
    taiex = load_taiex()
    # Per-signal forward-alpha buckets at each horizon
    buckets: dict[str, dict[int, list]] = {
        sk: {h: [] for h in HORIZONS} for sk in SIGNAL_DESCS
    }
    baseline: dict[int, list] = {h: [] for h in HORIZONS}
    per_code_n: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_code_alpha40: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    eligible_bars = 0
    eligible_codes = 0
    skipped_no_msbl = 0

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
        msbl = _build_margin_sbl_series(code, dates)
        if len(msbl) < LOOKBACK_DAYS + 10:
            skipped_no_msbl += 1
            continue
        eligible_codes += 1

        last_fired: set[str] = set()  # for first-cross event detection
        for i, d in enumerate(dates):
            cur = msbl.get(d)
            if not cur:
                last_fired = set()
                continue
            # Walk back LOOKBACK_DAYS trading days in msbl for the
            # change baseline.
            prev_d = None
            for j in range(i - 1, -1, -1):
                pd = dates[j]
                if pd in msbl:
                    # Find the date LOOKBACK_DAYS trading days before
                    pass
            # Simpler: iterate backwards over msbl chronological order
            prior_dates_in_msbl = [pd for pd in dates[:i] if pd in msbl]
            if len(prior_dates_in_msbl) < LOOKBACK_DAYS:
                last_fired = set()
                continue
            prev_key = prior_dates_in_msbl[-LOOKBACK_DAYS]
            prev = msbl[prev_key]
            f_now = cur.get("f"); f_prev = prev.get("f")
            s_now = cur.get("s"); s_prev = prev.get("s")
            sbl_now = cur.get("sbl"); sbl_prev = prev.get("sbl")
            ratio = (f_now / s_now) if (f_now is not None and s_now
                                        and s_now > 0) else None
            usage = (f_now / cur["f_limit"] * 100
                     if f_now is not None and cur.get("f_limit") else None)
            snap = {
                "f_now": f_now, "f_prev": f_prev,
                "s_now": s_now, "s_prev": s_prev,
                "sbl_now": sbl_now, "sbl_prev": sbl_prev,
                "ratio": ratio, "usage": usage,
            }
            fired = _classify(snap)
            # Baseline counts every analyzable bar (i.e. enough lookback
            # and at least one signal-computable cell)
            eligible_bars += 1
            for h in HORIZONS:
                a = forward_alpha(rows, i, h)
                if a is not None:
                    baseline[h].append(a)
            # First-cross events
            new_fires = fired - last_fired
            for sk in new_fires:
                per_code_n[code][sk] += 1
                for h in HORIZONS:
                    a = forward_alpha(rows, i, h)
                    if a is not None:
                        buckets[sk][h].append(a)
                        if h == 40:
                            per_code_alpha40[code][sk].append(a)
            last_fired = fired

    print(f"\nMargin / SBL signal study")
    print(f"Universe: {len(codes)} codes, eligible "
          f"{eligible_codes} (skipped {skipped_no_msbl} for "
          f"insufficient margin/SBL history)")
    print(f"Analyzable bars: {eligible_bars}\n")

    # Baseline
    b40 = _summarize(baseline[40])
    if b40:
        print(f"Baseline (every bar with computable signals, no fire required):")
        print(f"  {'h':>3}  {'n':>5}  {'alpha_med':>10}  {'alpha_mean':>10}  "
              f"{'win%':>6}")
        for h in HORIZONS:
            s = _summarize(baseline[h])
            if s:
                print(f"  {h:>3}  {s['n']:>5}  {_fmt(s['median']):>10}  "
                      f"{_fmt(s['mean']):>10}  {s['win_pct'] * 100:>5.1f}%")
        print()

    # Per-signal table
    print(f"{'Signal':<5} {'Description':<24} {'h':>3} {'n':>4}  "
          f"{'alpha_med':>10}  {'win%':>6}  {'Δvs base':>9}")
    print("-" * 80)
    for sk, desc in SIGNAL_DESCS.items():
        for h in HORIZONS:
            s = _summarize(buckets[sk][h])
            bs = _summarize(baseline[h])
            if not s:
                print(f"{sk:<5} {desc:<24} {h:>3} {'0':>4}  (no events)")
                continue
            delta = ((s["median"] - bs["median"]) * 100
                     if (bs and bs["median"] is not None) else None)
            dstr = f"{delta:+5.2f}pp" if delta is not None else "   n/a"
            print(f"{sk:<5} {desc:<24} {h:>3} {s['n']:>4}  "
                  f"{_fmt(s['median']):>10}  {s['win_pct'] * 100:>5.1f}%  "
                  f"{dstr:>9}")
        print()

    # Highlight signals with |Δvs base 40d| ≥ 2pp AND n ≥ 30 (worth
    # deeper look)
    print("─" * 80)
    print("Candidate signals (40d |Δvs base| ≥ 2pp and n ≥ 30):")
    base40 = _summarize(baseline[40])
    cands = []
    for sk in SIGNAL_DESCS:
        s = _summarize(buckets[sk][40])
        if not s or s["n"] < 30:
            continue
        if base40 and base40["median"] is not None:
            delta = (s["median"] - base40["median"]) * 100
            if abs(delta) >= 2:
                cands.append((delta, sk, s))
    cands.sort(key=lambda x: -abs(x[0]))
    if not cands:
        print("  (none — all signals either thin or near baseline)")
    for delta, sk, s in cands:
        print(f"  {sk} {SIGNAL_DESCS[sk]:<24} n={s['n']:>3}  "
              f"40d alpha={_fmt(s['median'])} / win {s['win_pct'] * 100:.1f}%  "
              f"Δ={delta:+.2f}pp")
    print()

    # Per-stock breadth for any candidates
    if cands:
        print("Per-stock 40d alpha sign for candidate signals:")
        for delta, sk, s in cands:
            pos = neg = zero = 0
            for code, by_sig in per_code_alpha40.items():
                vals = by_sig.get(sk) or []
                if len(vals) < 3:
                    continue
                m = statistics.median(vals)
                if m > 0: pos += 1
                elif m < 0: neg += 1
                else: zero += 1
            total = pos + neg + zero
            print(f"  {sk}: {pos}+ / {neg}- / {zero}0 of {total} codes "
                  f"with n≥3 — breadth = "
                  f"{pos / total * 100:.0f}% positive"
                  if total else f"  {sk}: no per-code n≥3")


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    run(codes)


if __name__ == "__main__":
    main()
