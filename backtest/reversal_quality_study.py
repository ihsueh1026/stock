"""Reversal-quality event study: when stock_web's `_reversal_quality()`
fires at 3★/4★/5★, what does the next 5/10/20/40 trading days look
like in terms of TAIEX-stripped alpha and hit rate?

Background: `_reversal_quality()` in stock_web/app.py scores each
bar 0-5 on 5 binary checks (近 20 日低點 / 前期跌幅 ≥5% / K<25 /
RSI6<35 / 量比 ≥1.0). It's labelled as "OBSERVATION AID — not entry
signal" but we never actually measured the forward-return distribution
conditional on a high score in this 30-stock universe. This study
fills that gap.

Event = first bar where score crosses up to the threshold (transition
from <thresh to >=thresh). Crossings are non-overlapping; one event
per regime entry.

Universe: full backtest/data/ (in-sample 10 + OOS-tech 20 = 30 stocks).

Usage:
    python3 -m backtest.reversal_quality_study             # 3/4/5
    python3 -m backtest.reversal_quality_study --threshold 4
"""
from __future__ import annotations

import argparse
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
    load_series, load_taiex, percentile,
)


def _score_at(rows: list[dict], idx: int) -> int | None:
    """Replay reversal_quality at rows[idx] using the same 20-bar
    window the production helper would see."""
    if idx < 19:
        return None
    window = rows[max(0, idx - 19): idx + 1]
    rq = _reversal_quality(window)
    return rq["score"] if rq else None


def find_threshold_events(rows: list[dict], threshold: int,
                          start: int = 60) -> list[int]:
    """One event per first-crossing into score >= threshold from <threshold.
    Resets only when score drops back below threshold.
    """
    events = []
    in_regime = False
    for i in range(start, len(rows)):
        s = _score_at(rows, i)
        if s is None:
            continue
        if s >= threshold:
            if not in_regime:
                events.append(i)
            in_regime = True
        else:
            in_regime = False
    return events


def find_exact_events(rows: list[dict], target: int,
                      start: int = 60) -> list[int]:
    """One event per first-crossing where score == target (allows
    measuring 3★ vs 4★ vs 5★ as distinct buckets, not overlapping)."""
    events = []
    last_score = None
    for i in range(start, len(rows)):
        s = _score_at(rows, i)
        if s is None:
            continue
        if s == target and last_score != target:
            events.append(i)
        last_score = s
    return events


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _fmt_pct(x):
    if x is None or x != x:
        return "  n/a"
    return f"{x * 100:+6.2f}%"


def _summarize(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "p25": percentile(vals, 0.25),
        "p75": percentile(vals, 0.75),
        "win_pct": sum(1 for v in vals if v > 0) / len(vals),
        "win_above_fees_pct": sum(1 for v in vals if v > 0.006) / len(vals),
    }


def run(codes: list[str], targets: list[int]) -> None:
    taiex = load_taiex()
    # Buckets: target score -> per-horizon list of {ret, alpha}
    buckets: dict[int, dict[int, dict[str, list]]] = {
        t: {h: {"ret": [], "alpha": []} for h in HORIZONS}
        for t in targets
    }
    # Per-code 40d alpha for breadth tally
    per_code_alpha40: dict[int, dict[str, list[float]]] = {
        t: {c: [] for c in codes} for t in targets
    }
    per_code_count: dict[int, dict[str, int]] = {
        t: {c: 0 for c in codes} for t in targets
    }
    total: dict[int, int] = {t: 0 for t in targets}

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        for target in targets:
            evts = find_exact_events(rows, target)
            per_code_count[target][code] = len(evts)
            total[target] += len(evts)
            for idx in evts:
                a40 = forward_alpha(rows, idx, 40)
                if a40 is not None:
                    per_code_alpha40[target][code].append(a40)
                for h in HORIZONS:
                    r = forward_return(rows, idx, h)
                    a = forward_alpha(rows, idx, h)
                    if r is not None:
                        buckets[target][h]["ret"].append(r)
                    if a is not None:
                        buckets[target][h]["alpha"].append(a)

    print(f"\nReversal-quality study (exact star match)")
    print(f"Codes: {', '.join(codes)}")
    for target in targets:
        print(f"  {target}★ total events: {total[target]}")
    print()

    # Pooled table
    h_med = " ".join(f"{h:>3}d alpha" for h in HORIZONS)
    h_win = " ".join(f"{h:>3}d win" for h in HORIZONS)
    print(f"  {'tier':<6}  {'n':>4}   {h_med}   {h_win}")
    print("  " + "-" * 84)
    for target in targets:
        # max n across horizons for that target
        max_n = max(len(buckets[target][h]["alpha"]) for h in HORIZONS)
        if max_n == 0:
            print(f"  {f'{target}★':<6}  {0:>4}   (no events)")
            continue
        meds, wins = [], []
        for h in HORIZONS:
            stats = _summarize(buckets[target][h]["alpha"])
            meds.append(_fmt_pct(stats.get("median")))
            wp = stats.get("win_pct")
            wins.append(f"{wp * 100:5.1f}%" if wp is not None else "  n/a")
        print(f"  {f'{target}★':<6}  {max_n:>4}   {' '.join(meds)}   {' '.join(wins)}")

    # Per-code breadth: how many stocks have positive median 40d alpha
    print(f"\nPer-stock 40d alpha breadth (median across that stock's events):")
    print(f"  {'tier':<6}  {'+':>3} {'-':>3} {'0':>3} {'na':>3}  per-code-medians")
    for target in targets:
        pos = neg = zero = na = 0
        meds_str_parts = []
        for code in codes:
            samples = per_code_alpha40[target][code]
            n = per_code_count[target][code]
            if not samples:
                na += 1
                meds_str_parts.append(f"{code}:n={n}")
                continue
            m = statistics.median(samples)
            tag = "+" if m > 0 else ("-" if m < 0 else "0")
            if tag == "+": pos += 1
            elif tag == "-": neg += 1
            else: zero += 1
            meds_str_parts.append(f"{code}:{n}/{_fmt_pct(m).strip()}")
        tally = f"{pos:>3} {neg:>3} {zero:>3} {na:>3}"
        print(f"  {f'{target}★':<6}  {tally}  (codes with events / 40d-alpha median)")
        # Print compact code-level breakdown if reasonable size
        codes_with_events = [s for s in meds_str_parts if not s.endswith("n=0")]
        if 1 <= len(codes_with_events) <= 30:
            for s in codes_with_events:
                print(f"      {s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*",
                    help="Stock codes; default = all under backtest/data/")
    ap.add_argument("--targets", default="3,4,5",
                    help="Comma-separated exact star scores to study (default 3,4,5)")
    args = ap.parse_args()
    codes = args.codes or _all_codes()
    targets = [int(x) for x in args.targets.split(",") if x.strip()]
    run(codes, targets)


if __name__ == "__main__":
    main()
