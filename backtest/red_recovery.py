"""Red-recovery event study: which step's red->non-red flip best
predicts the start of a sustained rally?

Hypothesis: for stocks coming off a red-heavy regime (multi-step
weakness), the *order in which lights recover* is more informative
than the snapshot quality of the bottom bar. The light that flips
back first is the leading indicator of the reversal.

Event definition:
  1. Enter "red regime" when red_count >= RED_THRESH for >= RED_DAYS
     consecutive bars. Track the set of steps that were red during
     the regime ("red_set").
  2. "Recovery day" = first bar where red_count drops below RED_THRESH.
  3. On the recovery day, the "recovered steps" = members of red_set
     whose light is non-red on that bar. One event contributes to
     every recovered step's bucket.
  4. Reset state after emitting; do not re-enter the same regime.

For each event, measure forward returns and TAIEX-stripped alpha at
horizons 5/10/20/40. Pool across codes and report per-step stats,
side-by-side with a random-day baseline for the same series.

Step 7 (法人) participates only when its T86 archive is available;
otherwise it sits gray and is excluded from red_set on those bars
(which mirrors how production stock_web behaves on missing T86).

Usage:
    # all 10 codes under backtest/data/
    python3 -m backtest.red_recovery

    # explicit codes + tweak thresholds
    python3 -m backtest.red_recovery 2395 5388 --red-thresh 3 --red-days 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import _compute_rows, _market_for, MARKET_TWSE  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, STEP_NAMES,
    _compute_lights, forward_return, forward_alpha,
    load_series, load_taiex, percentile,
)


def _resolve_market(code: str) -> str:
    """Best-effort market lookup; production cache may be absent in
    backtest context, so fall back to TWSE."""
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _red_set(steps: list[dict]) -> set[int]:
    return {i for i, s in enumerate(steps) if s.get("light") == "red"}


def find_recovery_events(rows: list[dict], code: str, market: str,
                         red_thresh: int, red_days: int,
                         start: int = 60) -> list[dict]:
    """Scan rows for red-regime exits. Returns one event per recovery day."""
    events = []
    streak = 0
    in_regime = False
    regime_red_set: set[int] = set()

    for i in range(start, len(rows)):
        _, steps = _compute_lights(rows, i, code=code, market=market)
        if not steps:
            continue
        cur_red = _red_set(steps)
        n_red = len(cur_red)

        if not in_regime:
            if n_red >= red_thresh:
                streak += 1
                if streak >= red_days:
                    in_regime = True
                    regime_red_set = set(cur_red)
            else:
                streak = 0
            if in_regime:
                # absorb this bar's red set too
                regime_red_set |= cur_red
            continue

        # in_regime == True
        regime_red_set |= cur_red
        if n_red < red_thresh:
            recovered = {s for s in regime_red_set if s not in cur_red}
            events.append({
                "idx": i,
                "date": rows[i]["date"],
                "recovered_steps": sorted(recovered),
                "regime_red_set": sorted(regime_red_set),
                "still_red": sorted(cur_red),
            })
            in_regime = False
            streak = 0
            regime_red_set = set()

    return events


def _summarize(returns: list[float]) -> dict:
    rets = [r for r in returns if r is not None]
    if not rets:
        return {"n": 0}
    return {
        "n": len(rets),
        "median": statistics.median(rets),
        "mean": statistics.mean(rets),
        "p25": percentile(rets, 0.25),
        "p75": percentile(rets, 0.75),
        "win_pct": sum(1 for r in rets if r > 0) / len(rets),
        "win_above_fees_pct": sum(1 for r in rets if r > 0.006) / len(rets),
    }


def _fmt_pct(x):
    if x is None or x != x:
        return "  n/a"
    return f"{x * 100:+6.2f}%"


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def run(codes: list[str], red_thresh: int, red_days: int,
        horizons: list[int]) -> None:
    taiex = load_taiex()

    def _new_step_pool():
        return {s: {h: [] for h in horizons} for s in range(len(STEP_NAMES))}

    # Two parallel bucketings of the same event population:
    #   recovered[step] — step flipped red->non-red ON the recovery day
    #   still_red[step] — step is STILL red on the recovery day (despite
    #                     the overall red-count dropping below threshold)
    # "still_red" tests the hypothesis that an un-confirmed light is a
    # forward-looking risk signal that drags the rebound.
    recovered_rets = _new_step_pool()
    recovered_alpha = _new_step_pool()
    still_red_rets = _new_step_pool()
    still_red_alpha = _new_step_pool()
    any_rets: dict[int, list[float]] = {h: [] for h in horizons}
    any_alpha: dict[int, list[float]] = {h: [] for h in horizons}
    # 法人 × 量能 joint bucket. Sanity check showed institutional-still-red
    # is a stable negative signal and volume-still-red is positive; the
    # joint cells tell us whether stacking them gives a cleaner cut.
    # Key: (inst_still_red, vol_still_red) -> {h: [returns / alphas]}
    INST_IDX, VOL_IDX = 6, 3
    joint_rets: dict[tuple, dict[int, list[float]]] = {
        (i, v): {h: [] for h in horizons}
        for i in (True, False) for v in (True, False)
    }
    joint_alpha: dict[tuple, dict[int, list[float]]] = {
        (i, v): {h: [] for h in horizons}
        for i in (True, False) for v in (True, False)
    }
    total_events = 0
    per_code_events: dict[str, int] = {}
    # Per-code joint cell tally + per-code 40d alpha samples (for the two
    # actionable cells). Lets us check whether the joint signal is broadly
    # distributed or concentrated in 1-2 stocks.
    per_code_joint_n: dict[str, dict[tuple, int]] = {}
    per_code_joint_alpha40: dict[str, dict[tuple, list[float]]] = {}

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        market = _resolve_market(code)
        evts = find_recovery_events(rows, code, market, red_thresh, red_days)
        per_code_events[code] = len(evts)
        total_events += len(evts)

        per_code_joint_n[code] = {
            (i, v): 0 for i in (True, False) for v in (True, False)
        }
        per_code_joint_alpha40[code] = {
            (i, v): [] for i in (True, False) for v in (True, False)
        }
        for e in evts:
            idx = e["idx"]
            still = set(e["still_red"])
            inst_red = INST_IDX in still
            vol_red = VOL_IDX in still
            per_code_joint_n[code][(inst_red, vol_red)] += 1
            a40 = forward_alpha(rows, idx, 40)
            if a40 is not None:
                per_code_joint_alpha40[code][(inst_red, vol_red)].append(a40)
            for h in horizons:
                r = forward_return(rows, idx, h)
                a = forward_alpha(rows, idx, h)
                if r is not None:
                    any_rets[h].append(r)
                    joint_rets[(inst_red, vol_red)][h].append(r)
                if a is not None:
                    any_alpha[h].append(a)
                    joint_alpha[(inst_red, vol_red)][h].append(a)
                for s_idx in e["recovered_steps"]:
                    if r is not None:
                        recovered_rets[s_idx][h].append(r)
                    if a is not None:
                        recovered_alpha[s_idx][h].append(a)
                for s_idx in e["still_red"]:
                    if r is not None:
                        still_red_rets[s_idx][h].append(r)
                    if a is not None:
                        still_red_alpha[s_idx][h].append(a)

    print(f"\nRed-recovery study  (red_thresh={red_thresh}, red_days={red_days})")
    print(f"Codes: {', '.join(codes)}")
    print(f"Total recovery events: {total_events}  "
          f"({', '.join(f'{c}:{n}' for c, n in per_code_events.items())})")

    if total_events == 0:
        print("No events found — try lowering --red-thresh or --red-days.")
        return

    # Per-step return tables
    print("\n=== Forward return by RECOVERING step ===")
    _print_table(recovered_rets, horizons, "ret", any_rets)
    print("\n=== Forward alpha by RECOVERING step (vs TAIEX) ===")
    _print_table(recovered_alpha, horizons, "alpha", any_alpha)
    print("\n=== Forward return by STILL-RED step on recovery day ===")
    _print_table(still_red_rets, horizons, "ret", any_rets)
    print("\n=== Forward alpha by STILL-RED step (vs TAIEX) ===")
    _print_table(still_red_alpha, horizons, "alpha", any_alpha)
    print("\n=== JOINT: 法人 × 量能 still-red on recovery day ===")
    _print_joint(joint_rets, joint_alpha, horizons, any_rets, any_alpha)
    print("\n=== PER-CODE breakdown of the two actionable cells (40d alpha) ===")
    _print_per_code_joint(per_code_joint_n, per_code_joint_alpha40, codes)


def _print_table(per_step: dict[int, dict[int, list[float]]],
                 horizons: list[int],
                 kind: str,
                 any_pool: dict[int, list[float]]) -> None:
    h_med = " ".join(f"{h:>3}d med" for h in horizons)
    h_win = " ".join(f"{h:>3}d win" for h in horizons)
    print(f"  {'step':<10}  {'n':>4}   {h_med}   {h_win}")
    # Per-step rows
    for s_idx, name in enumerate(STEP_NAMES):
        n_h = max(len(per_step[s_idx][h]) for h in horizons)
        if n_h == 0:
            continue
        meds = []
        wins = []
        for h in horizons:
            stats = _summarize(per_step[s_idx][h])
            meds.append(_fmt_pct(stats.get("median")))
            wp = stats.get("win_pct")
            wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
        print(f"  {name:<10}  {n_h:>4}   {' '.join(meds)}   {' '.join(wins)}")
    # Pooled baseline row
    n_any = max(len(any_pool[h]) for h in horizons)
    meds = []
    wins = []
    for h in horizons:
        stats = _summarize(any_pool[h])
        meds.append(_fmt_pct(stats.get("median")))
        wp = stats.get("win_pct")
        wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
    print(f"  {'<ANY>':<10}  {n_any:>4}   {' '.join(meds)}   {' '.join(wins)}")


def _print_joint(joint_rets, joint_alpha, horizons, any_rets, any_alpha):
    """4 cells of (法人 still-red ?) × (量能 still-red ?)."""
    LABELS = {
        (True,  True):  "法人紅+量能紅",
        (True,  False): "法人紅+量能非",
        (False, True):  "法人非+量能紅",
        (False, False): "法人非+量能非",
    }
    h_med = " ".join(f"{h:>3}d med" for h in horizons)
    h_win = " ".join(f"{h:>3}d win" for h in horizons)

    def row(label, pool_h):
        n = max(len(pool_h[h]) for h in horizons)
        if n == 0:
            return f"  {label:<14}  {0:>4}   " + " ".join("   n/a" for _ in horizons)
        meds, wins = [], []
        for h in horizons:
            stats = _summarize(pool_h[h])
            meds.append(_fmt_pct(stats.get("median")))
            wp = stats.get("win_pct")
            wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
        return f"  {label:<14}  {n:>4}   {' '.join(meds)}   {' '.join(wins)}"

    print("  -- raw forward return --")
    print(f"  {'cell':<14}  {'n':>4}   {h_med}   {h_win}")
    for key in [(True, True), (True, False), (False, True), (False, False)]:
        print(row(LABELS[key], joint_rets[key]))
    print(row("<ANY>", any_rets))

    print("  -- forward alpha (vs TAIEX) --")
    print(f"  {'cell':<14}  {'n':>4}   {h_med}   {h_win}")
    for key in [(True, True), (True, False), (False, True), (False, False)]:
        print(row(LABELS[key], joint_alpha[key]))
    print(row("<ANY>", any_alpha))


def _print_per_code_joint(per_n, per_alpha, codes):
    """For each code, show n and 40d-alpha median for both actionable cells.

    The two cells:
      BUY  = (inst_red=False, vol_red=True)   -- 法人非+量能紅
      AVOID= (inst_red=True,  vol_red=False)  -- 法人紅+量能非

    A signal that's broadly distributed should have most codes contributing
    same-sign alpha; if one code dominates and the rest are noise, the
    pooled result is fragile.
    """
    BUY = (False, True)
    AVOID = (True, False)
    print(f"  {'code':<6}  {'BUY n':>5} {'BUY med':>9}    {'AVOID n':>7} {'AVOID med':>10}")
    buy_signs = {"+": 0, "-": 0, "0": 0}
    avoid_signs = {"+": 0, "-": 0, "0": 0}
    for code in codes:
        buy_n = per_n[code][BUY]
        avoid_n = per_n[code][AVOID]
        buy_alphas = per_alpha[code][BUY]
        avoid_alphas = per_alpha[code][AVOID]
        buy_med = statistics.median(buy_alphas) if buy_alphas else None
        avoid_med = statistics.median(avoid_alphas) if avoid_alphas else None
        if buy_med is not None:
            buy_signs["+" if buy_med > 0 else ("-" if buy_med < 0 else "0")] += 1
        if avoid_med is not None:
            avoid_signs["+" if avoid_med > 0 else ("-" if avoid_med < 0 else "0")] += 1
        print(f"  {code:<6}  {buy_n:>5} {_fmt_pct(buy_med):>9}    "
              f"{avoid_n:>7} {_fmt_pct(avoid_med):>10}")
    print(f"  BUY  per-code sign tally: + {buy_signs['+']}   "
          f"- {buy_signs['-']}   0 {buy_signs['0']}   "
          f"(expect mostly '+' if signal is broad)")
    print(f"  AVOID per-code sign tally: + {avoid_signs['+']}   "
          f"- {avoid_signs['-']}   0 {avoid_signs['0']}   "
          f"(expect mostly '-' if signal is broad)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--red-thresh", type=int, default=3,
                    help="Min red-light count to count as red regime (default 3)")
    ap.add_argument("--red-days", type=int, default=5,
                    help="Consecutive red-thresh days to enter regime (default 5)")
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS),
                    help="Comma-separated forward horizons in trading days")
    args = ap.parse_args()
    codes = args.codes or _all_codes()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    run(codes, args.red_thresh, args.red_days, horizons)


if __name__ == "__main__":
    main()
