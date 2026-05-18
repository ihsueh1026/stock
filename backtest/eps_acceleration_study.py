"""EPS YoY acceleration event study (Plan A).

Question: when a stock's quarterly EPS YoY growth strictly accelerates
for 3 consecutive quarters (YoY(Q) > YoY(Q-1) > YoY(Q-2)), does the
TAIEX-stripped forward return outperform the baseline of all
YoY-computable quarters?

Universe: 50 codes under backtest/data/, intersected with EPS cache
availability (stock_web/cache/eps_q_{code}_{Y}Q{Q}.json).

Entry date: legal MOPS filing deadline for that quarter, advanced to
the next trading day if it falls on a non-trading day:
   Q1 → May 15
   Q2 → Aug 14
   Q3 → Nov 14
   Q4 → Mar 31 of next year (annual report)
This is conservative — real-world filings usually come earlier, so
real-world entry would be earlier and the measured alpha understates
what a fast filer would capture.

Forward horizons: 20 / 60 / 120 trading days (1 / 3 / 6 months),
covering a full inter-earnings gap.

Variants tested:
   A1  strict acceleration only
   A2  A1 + YoY(Q) > 0 (positive growth filter)
   A3  A1 + |EPS(Q)| ≥ 0.5 元 (small-number sanity floor)
   A4  A1 + both A2 and A3 filters

Run:
    python3 -m backtest.eps_acceleration_study
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import _compute_rows  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, load_series, load_taiex,
    forward_alpha,
)

EPS_CACHE = ROOT / "stock_web" / "cache"
HORIZONS = [20, 60, 120]

# Legal filing deadlines per quarter (within the same calendar year for
# Q1-Q3, next year for Q4).
DEADLINE_MONTH_DAY = {
    1: (5, 15),
    2: (8, 14),
    3: (11, 14),
    4: (3, 31),  # Q4 in next calendar year
}


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _load_eps_series(code: str) -> list[dict]:
    """Reconstruct chronological [{year, season, eps_standalone, yoy_pct, ...}]
    series from per-quarter cache files. Mirrors get_history()'s Q4-derive
    logic so we don't depend on network."""
    raw: dict[tuple[int, int], float] = {}
    for p in EPS_CACHE.glob(f"eps_q_{code}_*.json"):
        try:
            with p.open() as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("eps") is not None:
            raw[(d["year"], d["season"])] = d["eps"]
        # bag prior-year same-season too
        if d.get("prior_eps") is not None:
            raw.setdefault(
                (d["prior_year"], d["season"]),
                d["prior_eps"],
            )

    if not raw:
        return []

    # Build standalone — Q1/Q2/Q3 are already standalone, Q4 raw is the
    # annual cumulative so derive Q4 = annual - (Q1+Q2+Q3).
    standalone: dict[tuple[int, int], float] = {}
    years_seen = sorted({y for (y, _q) in raw.keys()})
    for y in years_seen:
        q1 = raw.get((y, 1))
        q2 = raw.get((y, 2))
        q3 = raw.get((y, 3))
        ann = raw.get((y, 4))
        if q1 is not None:
            standalone[(y, 1)] = q1
        if q2 is not None:
            standalone[(y, 2)] = q2
        if q3 is not None:
            standalone[(y, 3)] = q3
        if ann is not None and None not in (q1, q2, q3):
            standalone[(y, 4)] = round(ann - q1 - q2 - q3, 2)

    keys = sorted(standalone.keys())
    out = []
    for (y, q) in keys:
        val = standalone[(y, q)]
        prior = standalone.get((y - 1, q))
        yoy_pct = None
        if prior is not None and prior != 0:
            yoy_pct = (val - prior) / abs(prior) * 100
        out.append({
            "year": y, "season": q, "eps": val,
            "yoy_pct": yoy_pct, "prior_eps": prior,
        })
    return out


def _deadline_date(year: int, season: int) -> date:
    m, d = DEADLINE_MONTH_DAY[season]
    target_year = year + 1 if season == 4 else year
    return date(target_year, m, d)


def _index_for_date(rows: list[dict], target: date) -> int | None:
    """First row index whose date >= target. None if target is after the
    last available row."""
    target_iso = target.isoformat()
    for i, r in enumerate(rows):
        d = r.get("date")
        if d is None:
            continue
        if isinstance(d, str):
            if d >= target_iso:
                return i
        else:
            if d >= target:
                return i
    return None


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


def _is_accelerating(eps_series: list[dict], i: int) -> bool:
    """eps_series[i] has YoY, and YoY[i] > YoY[i-1] > YoY[i-2]."""
    if i < 2:
        return False
    a, b, c = eps_series[i].get("yoy_pct"), \
              eps_series[i - 1].get("yoy_pct"), \
              eps_series[i - 2].get("yoy_pct")
    if a is None or b is None or c is None:
        return False
    return a > b > c


def run(codes: list[str]) -> None:
    taiex = load_taiex()

    # variants: dict[name -> dict[horizon -> list[alpha]]]
    variants = {
        "Baseline (all YoY-computable)": {h: [] for h in HORIZONS},
        "A1 strict acceleration":         {h: [] for h in HORIZONS},
        "A2 A1 + YoY>0":                  {h: [] for h in HORIZONS},
        "A3 A1 + |EPS|>=0.5":             {h: [] for h in HORIZONS},
        "A4 A1 + YoY>0 + |EPS|>=0.5":     {h: [] for h in HORIZONS},
        "Anti: deceleration":             {h: [] for h in HORIZONS},
    }
    per_code_events: dict[str, int] = {}
    codes_with_data = 0
    skipped_no_eps = 0
    skipped_no_daily = 0
    skipped_no_entry_row = 0
    entry_dates: list[date] = []

    for code in codes:
        eps_series = _load_eps_series(code)
        if not eps_series:
            skipped_no_eps += 1
            continue
        try:
            raw = load_series(code)
        except FileNotFoundError:
            skipped_no_daily += 1
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            skipped_no_daily += 1
            continue
        codes_with_data += 1

        ev_count = 0
        for i, period in enumerate(eps_series):
            yoy = period.get("yoy_pct")
            if yoy is None:
                continue  # baseline still skipped — no YoY, no event
            entry = _deadline_date(period["year"], period["season"])
            idx = _index_for_date(rows, entry)
            if idx is None:
                skipped_no_entry_row += 1
                continue
            entry_dates.append(entry)

            alphas = {h: forward_alpha(rows, idx, h) for h in HORIZONS}

            # Baseline records every YoY-computable observation
            for h in HORIZONS:
                variants["Baseline (all YoY-computable)"][h].append(alphas[h])

            accel = _is_accelerating(eps_series, i)
            decel = False
            if i >= 2:
                a, b, c = (eps_series[i].get("yoy_pct"),
                           eps_series[i - 1].get("yoy_pct"),
                           eps_series[i - 2].get("yoy_pct"))
                if None not in (a, b, c):
                    decel = a < b < c

            eps_val = period.get("eps")
            pos = yoy > 0
            mag = eps_val is not None and abs(eps_val) >= 0.5

            if accel:
                ev_count += 1
                for h in HORIZONS:
                    variants["A1 strict acceleration"][h].append(alphas[h])
                if pos:
                    for h in HORIZONS:
                        variants["A2 A1 + YoY>0"][h].append(alphas[h])
                if mag:
                    for h in HORIZONS:
                        variants["A3 A1 + |EPS|>=0.5"][h].append(alphas[h])
                if pos and mag:
                    for h in HORIZONS:
                        variants["A4 A1 + YoY>0 + |EPS|>=0.5"][h].append(
                            alphas[h])
            if decel:
                for h in HORIZONS:
                    variants["Anti: deceleration"][h].append(alphas[h])

        per_code_events[code] = ev_count

    print("\nEPS YoY acceleration study (Plan A)")
    print(f"Universe: {len(codes)} codes; with both EPS+daily data: "
          f"{codes_with_data}")
    print(f"Skipped: no EPS cache {skipped_no_eps}, no daily {skipped_no_daily},"
          f" entry beyond series {skipped_no_entry_row}")
    if entry_dates:
        print(f"Entry-date range: {min(entry_dates)} → {max(entry_dates)}")
    print()

    # Per-horizon table
    print(f"{'Variant':<32}  {'h':>3}  {'n':>4}  {'alpha_med':>10}  "
          f"{'alpha_mean':>10}  {'win%':>6}  {'Δvs base':>9}")
    base = variants["Baseline (all YoY-computable)"]
    for name, hd in variants.items():
        for h in HORIZONS:
            s = _summarize(hd[h])
            if not s:
                print(f"  {name:<30}  {h:>3}  {'0':>4}  (no events)")
                continue
            bs = _summarize(base[h])
            delta = ((s["median"] - bs["median"]) * 100
                     if (bs and bs["median"] is not None) else None)
            dstr = f"{delta:+5.2f}pp" if delta is not None else "   n/a"
            print(f"  {name:<30}  {h:>3}  {s['n']:>4}  "
                  f"{_fmt(s['median']):>10}  {_fmt(s['mean']):>10}  "
                  f"{s['win_pct'] * 100:>5.1f}%  {dstr:>9}")
        print()

    print("Per-code event counts (A1):")
    interesting = [(c, n) for c, n in per_code_events.items() if n > 0]
    interesting.sort(key=lambda x: -x[1])
    if not interesting:
        print("  (no codes produced any acceleration events)")
    else:
        cols = 6
        for i in range(0, len(interesting), cols):
            row = " ".join(f"{c}:{n}"
                           for c, n in interesting[i: i + cols])
            print(f"  {row}")
    print()

    # Per-stock breadth — for the strongest variant (A3) at 60d.
    # We want to know if the +alpha is broadly distributed or driven by
    # a handful of codes. Re-traverse to bucket per code.
    print("Per-stock breadth — A3 (acceleration + |EPS|>=0.5) at 60d:")
    per_code_a3_60d: dict[str, list[float]] = {}
    per_code_anti_60d: dict[str, list[float]] = {}
    for code in codes:
        eps_series = _load_eps_series(code)
        if not eps_series:
            continue
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        for i, period in enumerate(eps_series):
            yoy = period.get("yoy_pct")
            if yoy is None:
                continue
            entry = _deadline_date(period["year"], period["season"])
            idx = _index_for_date(rows, entry)
            if idx is None:
                continue
            a = forward_alpha(rows, idx, 60)
            if a is None:
                continue
            accel = _is_accelerating(eps_series, i)
            decel = False
            if i >= 2:
                aa, bb, cc = (eps_series[i].get("yoy_pct"),
                              eps_series[i - 1].get("yoy_pct"),
                              eps_series[i - 2].get("yoy_pct"))
                if None not in (aa, bb, cc):
                    decel = aa < bb < cc
            eps_val = period.get("eps")
            mag = eps_val is not None and abs(eps_val) >= 0.5
            if accel and mag:
                per_code_a3_60d.setdefault(code, []).append(a)
            if decel:
                per_code_anti_60d.setdefault(code, []).append(a)

    a3_codes_with_n3 = sum(1 for v in per_code_a3_60d.values() if len(v) >= 3)
    a3_codes_pos = sum(1 for v in per_code_a3_60d.values()
                       if len(v) >= 3 and statistics.median(v) > 0)
    a3_codes_neg = sum(1 for v in per_code_a3_60d.values()
                       if len(v) >= 3 and statistics.median(v) < 0)
    print(f"  Codes with n>=3 A3 events: {a3_codes_with_n3}")
    print(f"    median 60d alpha > 0: {a3_codes_pos}")
    print(f"    median 60d alpha < 0: {a3_codes_neg}")
    print(f"    median 60d alpha = 0: "
          f"{a3_codes_with_n3 - a3_codes_pos - a3_codes_neg}")
    print()

    # Per-stock A3 vs Anti dispersion (codes with both n>=2)
    spreads = []
    for code in per_code_a3_60d:
        if code in per_code_anti_60d:
            a3v = per_code_a3_60d[code]
            anv = per_code_anti_60d[code]
            if len(a3v) >= 2 and len(anv) >= 2:
                spreads.append((code, statistics.median(a3v),
                                statistics.median(anv),
                                statistics.median(a3v) - statistics.median(anv),
                                len(a3v), len(anv)))
    spreads.sort(key=lambda x: -x[3])
    print(f"A3 - Anti spread per code at 60d (codes with n>=2 each side, "
          f"{len(spreads)} codes):")
    pos_spread = sum(1 for s in spreads if s[3] > 0)
    neg_spread = sum(1 for s in spreads if s[3] < 0)
    print(f"  spread > 0 (acceleration beats deceleration): {pos_spread}")
    print(f"  spread < 0 (deceleration beats acceleration): {neg_spread}")
    if spreads:
        median_spread = statistics.median(s[3] for s in spreads)
        print(f"  median spread across codes: {median_spread * 100:+.2f}pp")
    print()
    print("Top 5 / Bottom 3 spreads:")
    for code, a3m, anm, sp, na3, nan in spreads[:5]:
        print(f"  {code}: A3 {_fmt(a3m)} (n={na3}) - Anti {_fmt(anm)} "
              f"(n={nan}) = {sp * 100:+.2f}pp")
    print("  ...")
    for code, a3m, anm, sp, na3, nan in spreads[-3:]:
        print(f"  {code}: A3 {_fmt(a3m)} (n={na3}) - Anti {_fmt(anm)} "
              f"(n={nan}) = {sp * 100:+.2f}pp")


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    run(codes)


if __name__ == "__main__":
    main()
