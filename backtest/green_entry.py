"""Green-entry event study: when a stock first accumulates >= N green
lights after a non-green stretch, which step turning green leads, and
which still-non-green step drags the forward return?

Mirror of `red_recovery.py` flipped to the bull side. Event definition:

  1. "Quiet phase": green_count < GREEN_THRESH for >= QUIET_DAYS bars.
  2. "Entry event": first bar where green_count >= GREEN_THRESH.
  3. On the entry bar, partition the 7 steps:
       - "newly green" = step is green now but was NOT green yesterday
       - "still non-green" = step is yellow/red/gray (i.e. NOT green)
  4. Pool forward 5/10/20/40-day raw return + TAIEX-stripped alpha by
     each partition's per-step membership; also a 法人 × 量能 2x2 joint.
  5. Reset after emitting; no overlapping events from the same regime.

Step 7 (法人) participates only when the long-history T86 archive
covers it; otherwise the step is gray (which counts as non-green).

Usage:
    python3 -m backtest.green_entry
    python3 -m backtest.green_entry 2395 5388 --green-thresh 3 --quiet-days 5
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
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _green_set(steps: list[dict]) -> set[int]:
    return {i for i, s in enumerate(steps) if s.get("light") == "green"}


def find_entry_events(rows: list[dict], code: str, market: str,
                      green_thresh: int, quiet_days: int,
                      start: int = 60) -> list[dict]:
    """Scan rows for green-regime entries. One event per crossing."""
    events = []
    quiet_streak = 0
    last_green: set[int] = set()

    for i in range(start, len(rows)):
        _, steps = _compute_lights(rows, i, code=code, market=market)
        if not steps:
            continue
        cur_green = _green_set(steps)
        n_green = len(cur_green)

        if n_green < green_thresh:
            quiet_streak += 1
            last_green = cur_green
            continue

        # n_green crossed threshold
        if quiet_streak >= quiet_days:
            newly_green = sorted(cur_green - last_green)
            still_non_green = sorted(set(range(len(steps))) - cur_green)
            events.append({
                "idx": i,
                "date": rows[i]["date"],
                "green_set": sorted(cur_green),
                "newly_green": newly_green,
                "still_non_green": still_non_green,
            })
        # Reset whether or not we emitted — a regime, once entered, is
        # one event. We require a fresh quiet phase before the next.
        quiet_streak = 0
        last_green = cur_green

    return events


def _summarize(returns):
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


def run(codes, green_thresh, quiet_days, horizons):
    taiex = load_taiex()

    def _new_step_pool():
        return {s: {h: [] for h in horizons} for s in range(len(STEP_NAMES))}

    new_green_rets = _new_step_pool()
    new_green_alpha = _new_step_pool()
    still_non_rets = _new_step_pool()
    still_non_alpha = _new_step_pool()
    any_rets = {h: [] for h in horizons}
    any_alpha = {h: [] for h in horizons}
    INST_IDX, VOL_IDX = 6, 3
    joint_rets = {(i, v): {h: [] for h in horizons}
                  for i in (True, False) for v in (True, False)}
    joint_alpha = {(i, v): {h: [] for h in horizons}
                   for i in (True, False) for v in (True, False)}
    total_events = 0
    per_code_events: dict[str, int] = {}
    per_code_joint_n: dict[str, dict[tuple, int]] = {}
    per_code_joint_alpha40: dict[str, dict[tuple, list[float]]] = {}

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        market = _resolve_market(code)
        evts = find_entry_events(rows, code, market, green_thresh, quiet_days)
        per_code_events[code] = len(evts)
        total_events += len(evts)
        per_code_joint_n[code] = {(i, v): 0 for i in (True, False)
                                  for v in (True, False)}
        per_code_joint_alpha40[code] = {(i, v): [] for i in (True, False)
                                        for v in (True, False)}

        for e in evts:
            idx = e["idx"]
            non_green = set(e["still_non_green"])
            # joint key uses "is step X still non-green?" — mirrors
            # red-recovery's "is step X still red?" semantics
            inst_non = INST_IDX in non_green
            vol_non = VOL_IDX in non_green
            per_code_joint_n[code][(inst_non, vol_non)] += 1
            a40 = forward_alpha(rows, idx, 40)
            if a40 is not None:
                per_code_joint_alpha40[code][(inst_non, vol_non)].append(a40)
            for h in horizons:
                r = forward_return(rows, idx, h)
                a = forward_alpha(rows, idx, h)
                if r is not None:
                    any_rets[h].append(r)
                    joint_rets[(inst_non, vol_non)][h].append(r)
                if a is not None:
                    any_alpha[h].append(a)
                    joint_alpha[(inst_non, vol_non)][h].append(a)
                for s_idx in e["newly_green"]:
                    if r is not None:
                        new_green_rets[s_idx][h].append(r)
                    if a is not None:
                        new_green_alpha[s_idx][h].append(a)
                for s_idx in e["still_non_green"]:
                    if r is not None:
                        still_non_rets[s_idx][h].append(r)
                    if a is not None:
                        still_non_alpha[s_idx][h].append(a)

    print(f"\nGreen-entry study  (green_thresh={green_thresh}, "
          f"quiet_days={quiet_days})")
    print(f"Codes: {', '.join(codes)}")
    print(f"Total entry events: {total_events}  "
          f"({', '.join(f'{c}:{n}' for c, n in per_code_events.items())})")

    if total_events == 0:
        print("No events found — try lowering --green-thresh or --quiet-days.")
        return

    print("\n=== Forward return by NEWLY-GREEN step ===")
    _print_table(new_green_rets, horizons, any_rets)
    print("\n=== Forward alpha by NEWLY-GREEN step (vs TAIEX) ===")
    _print_table(new_green_alpha, horizons, any_alpha)
    print("\n=== Forward return by STILL-NON-GREEN step on entry day ===")
    _print_table(still_non_rets, horizons, any_rets)
    print("\n=== Forward alpha by STILL-NON-GREEN step (vs TAIEX) ===")
    _print_table(still_non_alpha, horizons, any_alpha)
    print("\n=== JOINT: 法人 × 量能 still-non-green on entry day ===")
    _print_joint(joint_rets, joint_alpha, horizons, any_rets, any_alpha)
    print("\n=== PER-CODE breakdown of the two actionable joint cells (40d alpha) ===")
    _print_per_code_joint(per_code_joint_n, per_code_joint_alpha40, codes)


def _print_table(per_step, horizons, any_pool):
    h_med = " ".join(f"{h:>3}d med" for h in horizons)
    h_win = " ".join(f"{h:>3}d win" for h in horizons)
    print(f"  {'step':<10}  {'n':>4}   {h_med}   {h_win}")
    for s_idx, name in enumerate(STEP_NAMES):
        n_h = max(len(per_step[s_idx][h]) for h in horizons)
        if n_h == 0:
            continue
        meds, wins = [], []
        for h in horizons:
            stats = _summarize(per_step[s_idx][h])
            meds.append(_fmt_pct(stats.get("median")))
            wp = stats.get("win_pct")
            wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
        print(f"  {name:<10}  {n_h:>4}   {' '.join(meds)}   {' '.join(wins)}")
    n_any = max(len(any_pool[h]) for h in horizons)
    meds, wins = [], []
    for h in horizons:
        stats = _summarize(any_pool[h])
        meds.append(_fmt_pct(stats.get("median")))
        wp = stats.get("win_pct")
        wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
    print(f"  {'<ANY>':<10}  {n_any:>4}   {' '.join(meds)}   {' '.join(wins)}")


def _print_joint(joint_rets, joint_alpha, horizons, any_rets, any_alpha):
    LABELS = {
        (True,  True):  "法人非+量能非",
        (True,  False): "法人非+量能綠",
        (False, True):  "法人綠+量能非",
        (False, False): "法人綠+量能綠",
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
    """Three cells of interest:
      LEAD = (False, True)  = 法人綠+量能非 (institutional ahead of volume)
                              -- the cell that survived OOS validation
      BOTH = (False, False) = 法人綠+量能綠 (textbook full confirmation)
      NONE = (True,  True)  = 法人非+量能非 (no confirmation)

    Each row shows per-code n + 40d-alpha median; tallies count how many
    codes have same-sign alpha as the pooled result.
    """
    LEAD = (False, True)
    BOTH = (False, False)
    NONE = (True, True)
    print(f"  {'code':<6}  {'LEAD n':>6} {'LEAD med':>9}   "
          f"{'BOTH n':>6} {'BOTH med':>9}   {'NONE n':>6} {'NONE med':>9}")
    tallies = {"LEAD": {"+": 0, "-": 0, "0": 0},
               "BOTH": {"+": 0, "-": 0, "0": 0},
               "NONE": {"+": 0, "-": 0, "0": 0}}
    for code in codes:
        cells = [("LEAD", LEAD), ("BOTH", BOTH), ("NONE", NONE)]
        row = [f"  {code:<6}"]
        for name, key in cells:
            n = per_n[code][key]
            samples = per_alpha[code][key]
            m = statistics.median(samples) if samples else None
            if m is not None:
                tallies[name]["+" if m > 0 else ("-" if m < 0 else "0")] += 1
            row.append(f"  {n:>6} {_fmt_pct(m):>9}")
        print("".join(row))
    print(f"  LEAD per-code sign tally (expect mostly '+'): "
          f"+ {tallies['LEAD']['+']}   - {tallies['LEAD']['-']}   "
          f"0 {tallies['LEAD']['0']}")
    print(f"  BOTH per-code sign tally: "
          f"+ {tallies['BOTH']['+']}   - {tallies['BOTH']['-']}   "
          f"0 {tallies['BOTH']['0']}")
    print(f"  NONE per-code sign tally (expect mostly '-'): "
          f"+ {tallies['NONE']['+']}   - {tallies['NONE']['-']}   "
          f"0 {tallies['NONE']['0']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--green-thresh", type=int, default=3,
                    help="Green count needed to count as green regime entry (default 3)")
    ap.add_argument("--quiet-days", type=int, default=5,
                    help="Consecutive bars of green_count<thresh before entry counts (default 5)")
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    args = ap.parse_args()
    codes = args.codes or _all_codes()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    run(codes, args.green_thresh, args.quiet_days, horizons)


if __name__ == "__main__":
    main()
