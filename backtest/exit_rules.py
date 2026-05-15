"""Exit-rules event study: given an entry trigger, which exit rule
delivers the best risk-adjusted forward alpha?

green_entry.py / red_recovery.py both measured forward return at a
**fixed** horizon (5/10/20/40 days). That implicitly assumes the
optimal exit is "hold N days then sell" — which is a strawman. This
study fixes the entry side and varies the exit:

Entry triggers (pick one with --entry):
  green-entry  green_count crosses GREEN_THRESH after QUIET_DAYS
               of green_count<thresh. Same definition as green_entry.py.
  lead         green-entry restricted to the 法人綠+量能非 cell —
               the "watch" signal already surfaced in the dashboard.
  red-recovery red_count drops below RED_THRESH after RED_DAYS of
               red_count>=thresh. Same definition as red_recovery.py.

Exit rules (all evaluated for every event):
  hold-N        Fixed N-day hold. Baselines: hold-20, hold-40.
  regime-flip   Exit on first bar where red_count >= REGIME_RED.
  summary-bad   Exit on first bar where summary label is one of
                {"🔴 趨勢轉弱", "🟠 訊號分歧"}.
  trail-Ksig    Exit when close drops K·σ below the rolling peak
                since entry. σ = stdev of the 19 daily pct-changes
                ending on the entry bar (same window as _price_zones
                in stock_web/app.py).

All non-fixed rules are capped at MAX_HOLD trading days (default 60),
matching what a swing trader would realistically wait. Per-event we
record (exit_idx, hold_days, alpha_at_exit, max_drawdown_pct).

Output: per-rule median/mean/win-pct of alpha, mean hold days, and
median peak drawdown (lower drawdown = lower discomfort during hold).

Usage:
    python3 -m backtest.exit_rules                  # all codes, green-entry
    python3 -m backtest.exit_rules --entry lead
    python3 -m backtest.exit_rules 2330 2317 --entry red-recovery
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, MARKET_TWSE, SUMMARY_LABELS,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, STEP_NAMES,
    _compute_lights, forward_return, forward_alpha,
    load_series, load_taiex, percentile,
)

INST_IDX, VOL_IDX = 6, 3
MAX_HOLD = 60
REGIME_RED = 3
BAD_SUMMARIES = {SUMMARY_LABELS["exit"], SUMMARY_LABELS["watch"]}


# ---------- entry triggers ----------

def _green_set(steps):
    return {i for i, s in enumerate(steps) if s.get("light") == "green"}


def _red_set(steps):
    return {i for i, s in enumerate(steps) if s.get("light") == "red"}


def _entry_green(rows, code, market, *, green_thresh=3, quiet_days=5, start=60):
    """Same logic as green_entry.find_entry_events but yields just indices."""
    quiet_streak = 0
    out = []
    for i in range(start, len(rows)):
        _, steps = _compute_lights(rows, i, code=code, market=market)
        if not steps:
            continue
        n_green = sum(1 for s in steps if s.get("light") == "green")
        if n_green < green_thresh:
            quiet_streak += 1
            continue
        if quiet_streak >= quiet_days:
            out.append(i)
        quiet_streak = 0
    return out


def _entry_lead(rows, code, market, *, green_thresh=3, quiet_days=5, start=60):
    """Green entry where 法人=green AND 量能!=green on the entry day."""
    quiet_streak = 0
    out = []
    for i in range(start, len(rows)):
        _, steps = _compute_lights(rows, i, code=code, market=market)
        if not steps:
            continue
        n_green = sum(1 for s in steps if s.get("light") == "green")
        if n_green < green_thresh:
            quiet_streak += 1
            continue
        if quiet_streak >= quiet_days:
            inst_green = steps[INST_IDX]["light"] == "green"
            vol_green = steps[VOL_IDX]["light"] == "green"
            if inst_green and not vol_green:
                out.append(i)
        quiet_streak = 0
    return out


def _entry_red_recovery(rows, code, market, *,
                        red_thresh=3, red_days=5, start=60):
    """Same logic as red_recovery.find_recovery_events."""
    red_streak = 0
    out = []
    for i in range(start, len(rows)):
        _, steps = _compute_lights(rows, i, code=code, market=market)
        if not steps:
            continue
        n_red = sum(1 for s in steps if s.get("light") == "red")
        if n_red >= red_thresh:
            red_streak += 1
            continue
        if red_streak >= red_days:
            out.append(i)
        red_streak = 0
    return out


ENTRY_TRIGGERS = {
    "green-entry": _entry_green,
    "lead":        _entry_lead,
    "red-recovery": _entry_red_recovery,
}


# ---------- exit rules ----------

def _sigma_at(rows, idx, window=19):
    """Stdev of pct-changes over the last `window` daily bars ending at
    rows[idx]. Matches _price_zones() in stock_web/app.py."""
    lo = max(1, idx - window + 1)
    diffs = []
    for j in range(lo, idx + 1):
        a, b = rows[j - 1]["close"], rows[j]["close"]
        if a and b:
            diffs.append((b - a) / a)
    if len(diffs) < 2:
        return None
    return statistics.pstdev(diffs)


def _exit_hold(rows, idx, n):
    j = min(idx + n, len(rows) - 1)
    return j


def _exit_regime_flip(rows, idx, code, market, *, red_thresh=REGIME_RED):
    for k in range(1, MAX_HOLD + 1):
        j = idx + k
        if j >= len(rows):
            return len(rows) - 1
        _, steps = _compute_lights(rows, j, code=code, market=market)
        if not steps:
            continue
        n_red = sum(1 for s in steps if s.get("light") == "red")
        if n_red >= red_thresh:
            return j
    return min(idx + MAX_HOLD, len(rows) - 1)


def _exit_summary_bad(rows, idx, code, market):
    for k in range(1, MAX_HOLD + 1):
        j = idx + k
        if j >= len(rows):
            return len(rows) - 1
        summ, _ = _compute_lights(rows, j, code=code, market=market)
        if summ.get("label") in BAD_SUMMARIES:
            return j
    return min(idx + MAX_HOLD, len(rows) - 1)


def _exit_trail_sigma(rows, idx, k_sigma):
    """Trail K·σ from running peak. σ frozen at entry."""
    sigma = _sigma_at(rows, idx)
    if sigma is None or sigma <= 0:
        return min(idx + MAX_HOLD, len(rows) - 1)
    entry_close = rows[idx]["close"]
    if entry_close is None:
        return min(idx + MAX_HOLD, len(rows) - 1)
    peak = entry_close
    for k in range(1, MAX_HOLD + 1):
        j = idx + k
        if j >= len(rows):
            return len(rows) - 1
        c = rows[j]["close"]
        if c is None:
            continue
        if c > peak:
            peak = c
        if (peak - c) / peak >= k_sigma * sigma:
            return j
    return min(idx + MAX_HOLD, len(rows) - 1)


def _max_drawdown(rows, idx, exit_idx):
    """Worst peak-to-trough drop during the hold, as a fraction.
    Lower magnitude = less discomfort while holding."""
    closes = [rows[j]["close"] for j in range(idx, exit_idx + 1)
              if rows[j]["close"] is not None]
    if len(closes) < 2:
        return 0.0
    peak = closes[0]
    worst = 0.0
    for c in closes[1:]:
        if c > peak:
            peak = c
        dd = (c - peak) / peak  # negative
        if dd < worst:
            worst = dd
    return worst


def _alpha_between(rows, idx, exit_idx):
    """Stock return minus TAIEX return over the actual hold."""
    horizon = exit_idx - idx
    if horizon <= 0:
        return None
    return forward_alpha(rows, idx, horizon)


# ---------- runner ----------

def _exit_specs():
    """List of (name, fn(rows, idx, code, market) -> exit_idx)."""
    return [
        ("hold-20",       lambda rows, idx, c, m: _exit_hold(rows, idx, 20)),
        ("hold-40",       lambda rows, idx, c, m: _exit_hold(rows, idx, 40)),
        ("regime-flip",   lambda rows, idx, c, m: _exit_regime_flip(rows, idx, c, m)),
        ("summary-bad",   lambda rows, idx, c, m: _exit_summary_bad(rows, idx, c, m)),
        ("trail-1.5sig",  lambda rows, idx, c, m: _exit_trail_sigma(rows, idx, 1.5)),
        ("trail-2.0sig",  lambda rows, idx, c, m: _exit_trail_sigma(rows, idx, 2.0)),
        ("trail-3.0sig",  lambda rows, idx, c, m: _exit_trail_sigma(rows, idx, 3.0)),
    ]


def _resolve_market(code):
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _all_codes():
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _fmt_pct(x):
    if x is None or x != x:
        return "  n/a"
    return f"{x * 100:+6.2f}%"


def run(codes, entry_name):
    taiex = load_taiex()
    entry_fn = ENTRY_TRIGGERS[entry_name]
    exits = _exit_specs()

    # exit_name -> list[(alpha, hold_days, drawdown)]
    pools = {name: [] for name, _ in exits}
    total_events = 0
    per_code_events = {}

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        market = _resolve_market(code)
        events = entry_fn(rows, code, market)
        per_code_events[code] = len(events)
        total_events += len(events)
        for idx in events:
            for name, fn in exits:
                exit_idx = fn(rows, idx, code, market)
                if exit_idx is None or exit_idx <= idx:
                    continue
                a = _alpha_between(rows, idx, exit_idx)
                dd = _max_drawdown(rows, idx, exit_idx)
                hold = exit_idx - idx
                if a is None:
                    continue
                pools[name].append((a, hold, dd))

    print(f"\nExit-rules study  (entry={entry_name})")
    print(f"Codes: {', '.join(codes)}")
    print(f"Total entry events: {total_events}  "
          f"({', '.join(f'{c}:{n}' for c, n in per_code_events.items())})")
    if total_events == 0:
        print("No events.")
        return

    print(f"\n{'exit rule':<14}  {'n':>4}  "
          f"{'alpha_med':>9}  {'alpha_mean':>10}  "
          f"{'win%':>5}  {'win>0.6%':>8}  "
          f"{'hold_avg':>8}  {'dd_med':>8}  {'dd_p25':>8}")
    print("-" * 92)
    for name, _ in exits:
        rows_p = pools[name]
        if not rows_p:
            print(f"{name:<14}  {0:>4}  (no exits)")
            continue
        alphas = [r[0] for r in rows_p]
        holds = [r[1] for r in rows_p]
        dds = [r[2] for r in rows_p]
        med = statistics.median(alphas)
        mean = statistics.mean(alphas)
        win = sum(1 for a in alphas if a > 0) / len(alphas)
        win_fee = sum(1 for a in alphas if a > 0.006) / len(alphas)
        hold_avg = statistics.mean(holds)
        dd_med = statistics.median(dds)
        dd_p25 = percentile(dds, 0.25)
        print(f"{name:<14}  {len(rows_p):>4}  "
              f"{_fmt_pct(med):>9}  {_fmt_pct(mean):>10}  "
              f"{win * 100:>4.1f}%  {win_fee * 100:>7.1f}%  "
              f"{hold_avg:>8.1f}  {_fmt_pct(dd_med):>8}  "
              f"{_fmt_pct(dd_p25):>8}")

    print("\nLegend:")
    print("  alpha_med/mean  forward alpha (vs TAIEX) at the exit point")
    print("  win%            fraction of events with positive alpha")
    print("  win>0.6%        fraction with alpha clearing round-trip fees")
    print("  hold_avg        mean trading days held")
    print("  dd_med          median worst peak-to-trough during the hold")
    print("  dd_p25          25th percentile drawdown (worst quartile)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--entry", choices=list(ENTRY_TRIGGERS),
                    default="green-entry",
                    help="Entry trigger (default green-entry)")
    args = ap.parse_args()
    codes = args.codes or _all_codes()
    run(codes, args.entry)


if __name__ == "__main__":
    main()
