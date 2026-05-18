"""Sub-shape study for the two chips that were REMOVED from production
because their 50-stock pool alpha was ~0:

  1. 反轉 3★+綠 (`reversal_inst_confirm_3`) — pool +0% / 50% so dropped.
     5 reversal-quality conditions, 3 present, 2 missing → 10 possible
     "which-pair-is-missing" combinations.

  2. LEAD 法人提前 (`inst_lead`) — pool +0.06% / 50% so dropped.
     Cuts to try:
       (a) 量能 light flavor at entry (yellow vs red)
       (b) TAIEX regime at entry (bull vs bear)
       (c) Combined (a) × (b) cells
       (d) How many lights green at entry (entry-quality intensity)

The point wasn't to bring them back to production but to confirm the
pool-level null isn't hiding a useful sub-shape.

Universe: full backtest/data/ (50 codes).

Usage:
    python3 -m backtest.removed_chips_subshape_study

═══════════════════════════════════════════════════════════════════════
RESULTS (run on 2026-05-18, 50-stock universe)
═══════════════════════════════════════════════════════════════════════

Conclusion: BOTH chips correctly removed. No sub-shape clears the
n≥30 + 40d alpha ≥+3% ship bar. Two findings worth noting but neither
shipped (would require new chip plumbing for marginal payoff):

Part 1 — 反轉 3★+綠+持有非黃 (n=425, baseline 40d +0.42% / 51.8%):
  Best:  缺 C1低+C4RSI → +8.35% / 64% (n=28, JUST under ship threshold)
  Worst: 缺 C3K+C4RSI  → -2.94% / 36% (n=50, real counter-signal — "V
         反彈後回測" shape: 近低+前跌+有量 but K and RSI both already
         normalised). Could be a ⚠️ chip if 3★+綠 ever returns, but
         since the parent chip is removed, no plumbing to attach it to.

Part 2 — LEAD 法人提前 (n=377, baseline 40d +0.06% / 50.4%):
  All 4 (vol_light × regime) cells stay within ±1.5pp of baseline.
  Clean null. Entry-quality (#greens at entry) also no edge.

If/when the 50-stock pool grows or 3★ ever returns to production, the
C1+C4-missing watch cell (currently n=28) is the one to re-check.
"""
from __future__ import annotations

import statistics
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, _reversal_quality, MARKET_TWSE,
    TAIEX_BEAR_THRESH, TAIEX_LOOKBACK,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS,
    forward_return, forward_alpha,
    load_series, load_taiex, _compute_lights,
)
from backtest.reversal_quality_study import find_exact_events  # noqa: E402
from backtest.green_entry import find_entry_events  # noqa: E402


INST_IDX = 6
VOL_IDX = 3
HOLD_IDX = 4

COND_SHORT = ["C1低", "C2跌", "C3K", "C4RSI", "C5量"]


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


def _classify_regimes(taiex: dict) -> dict:
    """Mirror of build_stats._classify_taiex_regimes — date → 'bull'|'bear'."""
    sd = sorted(taiex.keys())
    sv = [taiex[d] for d in sd]
    out = {}
    for i, d in enumerate(sd):
        lo = max(0, i - TAIEX_LOOKBACK + 1)
        peak = max(sv[lo: i + 1])
        if peak <= 0:
            out[d] = "bull"
            continue
        dd = (sv[i] - peak) / peak
        out[d] = "bear" if dd <= -TAIEX_BEAR_THRESH else "bull"
    return out


# ───────────────────────────────────────────────────────────────────────
# Part 1: 反轉 3★+綠 — which pair of conditions is missing
# ───────────────────────────────────────────────────────────────────────
def study_3star(codes: list[str], taiex: dict) -> None:
    print("\n" + "=" * 72)
    print("PART 1: 反轉 3★+綠 (+持有非黃) — missing-pair breakdown")
    print("=" * 72)
    print("3 of 5 reversal-quality conditions present, 2 missing.")
    print("10 possible missing-pairs. Filter: 法人=綠, 持有≠黃 "
          "(same exclusion as 4★ in prod).\n")

    # missing_pair (frozenset of 2 indices) → horizon → list of alpha
    buckets: dict[frozenset, dict[int, list[float]]] = {}
    baseline: dict[int, list[float]] = {h: [] for h in HORIZONS}
    total = 0

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE

        for idx in find_exact_events(rows, target=3):
            _, steps = _compute_lights(rows, idx, code=code, market=market)
            if not steps or len(steps) <= INST_IDX:
                continue
            if steps[INST_IDX]["light"] != "green":
                continue
            if len(steps) > HOLD_IDX and steps[HOLD_IDX]["light"] == "yellow":
                continue
            window = rows[max(0, idx - 19): idx + 1]
            rq = _reversal_quality(window)
            if not rq or rq["score"] != 3:
                continue
            missing = frozenset(
                i for i, c in enumerate(rq["checks"]) if not c["passed"])
            if len(missing) != 2:
                continue
            total += 1
            b = buckets.setdefault(missing, {h: [] for h in HORIZONS})
            for h in HORIZONS:
                a = forward_alpha(rows, idx, h)
                if a is not None:
                    b[h].append(a)
                    baseline[h].append(a)

    print(f"Total qualifying events: {total}\n")
    print("Baseline (all 3★+綠+持有非黃 pooled):")
    print(f"  {'h':>3}  {'n':>4}  {'alpha_med':>10}  {'alpha_mean':>10}  {'win%':>6}")
    for h in HORIZONS:
        s = _summarize(baseline[h])
        if s:
            print(f"  {h:>3}  {s['n']:>4}  {_fmt(s['median']):>10}  "
                  f"{_fmt(s['mean']):>10}  {s['win_pct'] * 100:>5.1f}%")
    print()

    # Rank table: per pair × horizon
    print("All 10 missing-pairs at 40d (ranked best → worst):")
    print(f"  {'missing pair':<22} {'n':>4}  {'5d':>8} {'10d':>8} "
          f"{'20d':>8} {'40d':>8}  {'40dwin':>6}")
    rows_sorted = []
    for pair, hd in buckets.items():
        s40 = _summarize(hd[40])
        if s40 is None:
            continue
        rows_sorted.append((s40["median"], pair, hd, s40))
    rows_sorted.sort(reverse=True)
    for med40, pair, hd, s40 in rows_sorted:
        i1, i2 = sorted(pair)
        label = f"{COND_SHORT[i1]}+{COND_SHORT[i2]}"
        per_h = " ".join(
            _fmt(_summarize(hd[h])["median"] if _summarize(hd[h]) else None)
            for h in HORIZONS)
        print(f"  {label:<22} {s40['n']:>4}  {per_h}  "
              f"{s40['win_pct'] * 100:>5.1f}%")
    print()

    # Highlight cells with n≥30 and 40d alpha ≥+3% (would be worth ship)
    candidates = [
        (med40, pair, s40)
        for med40, pair, _hd, s40 in rows_sorted
        if s40["n"] >= 30 and med40 >= 0.03
    ]
    if candidates:
        print("** Ship-worthy candidates (n≥30 and 40d alpha ≥+3%):")
        for med40, pair, s40 in candidates:
            i1, i2 = sorted(pair)
            label = f"{COND_SHORT[i1]}+{COND_SHORT[i2]}"
            print(f"   missing {label}: n={s40['n']}, "
                  f"40d alpha={_fmt(med40)}, win={s40['win_pct'] * 100:.1f}%")
    else:
        print("** No 3★ sub-shape clears n≥30 + 40d alpha ≥+3% bar.")
    print()


# ───────────────────────────────────────────────────────────────────────
# Part 2: LEAD (法人提前) — vol-flavor × regime cuts
# ───────────────────────────────────────────────────────────────────────
def study_lead(codes: list[str], taiex: dict) -> None:
    print("\n" + "=" * 72)
    print("PART 2: LEAD 法人提前 — sub-shape breakdown")
    print("=" * 72)
    print("Cuts: vol_light flavor (yellow/red) × TAIEX regime (bull/bear)\n")

    regimes = _classify_regimes(taiex)

    # cells[(vol, regime)][h] = list of alpha
    cells: dict[tuple, dict[int, list[float]]] = {}
    # green-count entry quality (how many of 7 lights green when LEAD fires)
    by_greens: dict[int, dict[int, list[float]]] = {}
    baseline: dict[int, list[float]] = {h: [] for h in HORIZONS}
    total = 0
    vol_counts = Counter()
    regime_counts = Counter()

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE

        for evt in find_entry_events(rows, code, market,
                                     green_thresh=3, quiet_days=5):
            non_green = set(evt["still_non_green"])
            inst_non = INST_IDX in non_green
            vol_non = VOL_IDX in non_green
            # LEAD = 法人=綠 + 量能 non-green
            if inst_non or not vol_non:
                continue
            idx = evt["idx"]
            _, steps = _compute_lights(rows, idx, code=code, market=market)
            if not steps or len(steps) <= INST_IDX:
                continue
            vol_light = steps[VOL_IDX]["light"]  # 'yellow' or 'red'
            n_greens = sum(1 for s in steps if s.get("light") == "green")
            date = rows[idx]["date"]
            from datetime import datetime
            try:
                date_obj = datetime.fromisoformat(date).date() \
                    if isinstance(date, str) else date
            except Exception:
                date_obj = None
            regime = regimes.get(date_obj, "bull") if date_obj else "bull"
            vol_counts[vol_light] += 1
            regime_counts[regime] += 1
            total += 1
            key = (vol_light, regime)
            cell = cells.setdefault(key, {h: [] for h in HORIZONS})
            gb = by_greens.setdefault(n_greens, {h: [] for h in HORIZONS})
            for h in HORIZONS:
                a = forward_alpha(rows, idx, h)
                if a is not None:
                    cell[h].append(a)
                    gb[h].append(a)
                    baseline[h].append(a)

    print(f"Total LEAD events: {total}")
    print(f"Vol light distribution: {dict(vol_counts)}")
    print(f"Regime distribution: {dict(regime_counts)}\n")

    print("Baseline (all LEAD pooled):")
    print(f"  {'h':>3}  {'n':>4}  {'alpha_med':>10}  {'alpha_mean':>10}  {'win%':>6}")
    for h in HORIZONS:
        s = _summarize(baseline[h])
        if s:
            print(f"  {h:>3}  {s['n']:>4}  {_fmt(s['median']):>10}  "
                  f"{_fmt(s['mean']):>10}  {s['win_pct'] * 100:>5.1f}%")
    print()

    # By vol_light × regime cells
    print("By (vol_light × regime):")
    print(f"  {'cell':<22} {'n':>4}  {'5d':>8} {'10d':>8} "
          f"{'20d':>8} {'40d':>8}  {'40dwin':>6}")
    for vol in ("yellow", "red"):
        for reg in ("bull", "bear"):
            cell = cells.get((vol, reg))
            if not cell:
                continue
            s40 = _summarize(cell[40])
            if not s40:
                continue
            per_h = " ".join(
                _fmt(_summarize(cell[h])["median"] if _summarize(cell[h]) else None)
                for h in HORIZONS)
            label = f"vol={vol}, regime={reg}"
            print(f"  {label:<22} {s40['n']:>4}  {per_h}  "
                  f"{s40['win_pct'] * 100:>5.1f}%")
    print()

    # By entry quality (how many greens at the entry bar)
    print("By entry-quality (# greens at entry bar):")
    print(f"  {'#greens':<10} {'n':>4}  {'5d':>8} {'10d':>8} "
          f"{'20d':>8} {'40d':>8}  {'40dwin':>6}")
    for ng in sorted(by_greens.keys()):
        hd = by_greens[ng]
        s40 = _summarize(hd[40])
        if not s40 or s40["n"] < 5:
            continue
        per_h = " ".join(
            _fmt(_summarize(hd[h])["median"] if _summarize(hd[h]) else None)
            for h in HORIZONS)
        print(f"  {ng:<10} {s40['n']:>4}  {per_h}  "
              f"{s40['win_pct'] * 100:>5.1f}%")
    print()

    # Ship test
    candidates = []
    for key, cell in cells.items():
        s40 = _summarize(cell[40])
        if s40 and s40["n"] >= 30 and s40["median"] >= 0.03:
            candidates.append((s40["median"], key, s40))
    candidates.sort(reverse=True)
    if candidates:
        print("** Ship-worthy LEAD cells (n≥30 and 40d alpha ≥+3%):")
        for med, key, s40 in candidates:
            vol, reg = key
            print(f"   vol={vol}, regime={reg}: n={s40['n']}, "
                  f"40d alpha={_fmt(med)}, win={s40['win_pct'] * 100:.1f}%")
    else:
        print("** No LEAD sub-shape clears n≥30 + 40d alpha ≥+3% bar.")
    print()


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    taiex = load_taiex()
    study_3star(codes, taiex)
    study_lead(codes, taiex)


if __name__ == "__main__":
    main()
