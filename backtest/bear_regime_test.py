"""Bear-regime sub-sample test: do the validated chips still deliver
in TAIEX drawdowns, or are they bull-only signals?

Each calendar date is classified bull / bear by TAIEX's trailing-60-day
peak-to-current drawdown: <=DRAWDOWN_THRESH = bull, >DRAWDOWN_THRESH = bear.
Default threshold 10% catches meaningful corrections without firing on
typical 5-7% pullbacks. The 2022 TAIEX drop (18,526 → 12,666 = -31.6%)
spans most of the year under this rule.

Chip events from `_chip_events_for_code()` (same helper that build_stats
uses) get bucketed by event-day regime. Forward alpha is then pooled
per (chip, regime). A chip is "robust" if its bear-regime alpha keeps
the same sign as bull-regime; flipped sign = bull-only signal.

Usage:
    python3 -m backtest.bear_regime_test
    python3 -m backtest.bear_regime_test --drawdown 0.15
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import _compute_rows, _market_for, MARKET_TWSE  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS,
    forward_alpha, load_series, load_taiex, percentile,
)
from backtest.build_stats import (  # noqa: E402
    CHIP_KINDS, _chip_events_for_code,
)

DEFAULT_DRAWDOWN = 0.10
LOOKBACK = 60


def classify_regimes(taiex: dict, drawdown_thresh: float
                     ) -> dict[date, str]:
    """Return {date_obj: 'bull'|'bear'} from trailing-60-day drawdown."""
    sorted_dates = sorted(taiex.keys())
    sorted_vals = [taiex[d] for d in sorted_dates]
    out: dict[date, str] = {}
    for i, d in enumerate(sorted_dates):
        lo = max(0, i - LOOKBACK + 1)
        peak = max(sorted_vals[lo: i + 1])
        if peak <= 0:
            out[d] = "bull"
            continue
        dd = (sorted_vals[i] - peak) / peak
        out[d] = "bear" if dd <= -drawdown_thresh else "bull"
    return out


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _fmt_pct(x):
    if x is None or x != x:
        return "  n/a"
    return f"{x * 100:+6.2f}%"


def _resolve_market(code: str) -> str:
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _summarize(alphas):
    if not alphas:
        return {"n": 0}
    return {
        "n": len(alphas),
        "median": statistics.median(alphas),
        "mean": statistics.mean(alphas),
        "win_pct": sum(1 for a in alphas if a > 0) / len(alphas),
        "p25": percentile(alphas, 0.25),
        "p75": percentile(alphas, 0.75),
    }


def run(codes: list[str], drawdown_thresh: float) -> None:
    taiex = load_taiex()
    regimes = classify_regimes(taiex, drawdown_thresh)

    # Sanity: how many trading days fall in each regime?
    bull_days = sum(1 for r in regimes.values() if r == "bull")
    bear_days = sum(1 for r in regimes.values() if r == "bear")
    print(f"Bear regime: trailing-{LOOKBACK}d drawdown >= "
          f"{drawdown_thresh*100:.0f}%")
    print(f"TAIEX universe: {len(regimes)} days "
          f"({bull_days} bull, {bear_days} bear "
          f"= {bear_days/len(regimes)*100:.0f}% bear)")
    if bear_days == 0:
        print("\nNo bear days — try a lower --drawdown.")
        return

    # Spans of bear regime (for context)
    bear_spans = []
    in_bear = False
    span_start = None
    for d in sorted(regimes.keys()):
        if regimes[d] == "bear":
            if not in_bear:
                span_start = d
                in_bear = True
            span_end = d
        else:
            if in_bear:
                bear_spans.append((span_start, span_end))
                in_bear = False
    if in_bear:
        bear_spans.append((span_start, span_end))
    print(f"Bear spans ({len(bear_spans)}):")
    for s, e in bear_spans:
        days = (e - s).days
        print(f"  {s} ~ {e}  ({days} 日)")

    # Pool: chip_kind -> regime -> horizon -> [alpha]
    pools: dict[str, dict[str, dict[int, list[float]]]] = {
        k: {"bull": {h: [] for h in HORIZONS},
            "bear": {h: [] for h in HORIZONS}}
        for k in CHIP_KINDS
    }
    counts: dict[str, dict[str, int]] = {
        k: {"bull": 0, "bear": 0} for k in CHIP_KINDS
    }

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        market = _resolve_market(code)
        chip_evts = _chip_events_for_code(rows, code, market)
        for chip_kind, idxs in chip_evts.items():
            for idx in idxs:
                # _compute_rows returns rows with date as ISO string,
                # but regime keys are date objects (from load_taiex).
                # Normalise here.
                raw_d = rows[idx]["date"]
                if isinstance(raw_d, str):
                    event_date = datetime.fromisoformat(raw_d).date()
                else:
                    event_date = raw_d
                regime = regimes.get(event_date, "bull")
                counts[chip_kind][regime] += 1
                for h in HORIZONS:
                    a = forward_alpha(rows, idx, h)
                    if a is not None:
                        pools[chip_kind][regime][h].append(a)

    # Print summary table
    print(f"\n{'='*100}")
    print(f"{'chip':<28} {'regime':>6}  {'n':>4}    "
          f"{'5d':>8}  {'10d':>8}  {'20d':>8}  {'40d':>8}    "
          f"{'40d win':>7}")
    print("-" * 100)
    for chip_kind in CHIP_KINDS:
        for regime in ("bull", "bear"):
            n = counts[chip_kind][regime]
            alphas_per_h = pools[chip_kind][regime]
            if n == 0:
                print(f"  {chip_kind:<26} {regime:>6}  {0:>4}    (no events)")
                continue
            meds = []
            for h in HORIZONS:
                s = _summarize(alphas_per_h[h])
                meds.append(_fmt_pct(s.get("median")))
            s40 = _summarize(alphas_per_h[40])
            wp = s40.get("win_pct")
            wp_s = f"{wp*100:5.1f}%" if wp is not None else "  n/a"
            print(f"  {chip_kind:<26} {regime:>6}  {n:>4}    "
                  f"{' '.join(m.rjust(8) for m in meds)}    {wp_s:>7}")

    # Sign-flip / robustness check
    print(f"\n{'='*60}")
    print("Robustness check (40d alpha sign across regimes):")
    print(f"  {'chip':<28} {'bull':>9}  {'bear':>9}  {'verdict':>10}")
    for chip_kind in CHIP_KINDS:
        bull40 = _summarize(pools[chip_kind]["bull"][40])
        bear40 = _summarize(pools[chip_kind]["bear"][40])
        bm = bull40.get("median")
        rm = bear40.get("median")
        if bm is None or rm is None:
            verdict = "n/a"
        else:
            same_sign = (bm >= 0) == (rm >= 0)
            verdict = "ROBUST" if same_sign else "FLIPS"
        bm_s = _fmt_pct(bm) if bm is not None else "  n/a"
        rm_s = _fmt_pct(rm) if rm is not None else "  n/a"
        print(f"  {chip_kind:<28} {bm_s:>9}  {rm_s:>9}  {verdict:>10}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--drawdown", type=float, default=DEFAULT_DRAWDOWN,
                    help="TAIEX drawdown threshold for bear (default 0.10)")
    args = ap.parse_args()
    codes = args.codes or _all_codes()
    run(codes, args.drawdown)


if __name__ == "__main__":
    main()
