"""Reverse-engineer reversal-then-rally events.

Find days where:
  1) close is the lowest of the past LOOKBACK trading days (a fresh local low),
  2) at least one of the prior LOOKBACK days had a high >= close * (1+DRAWDOWN),
     i.e. the move into this low was preceded by a meaningful drop (so it's
     a 'reversal' candidate, not just a sideways grind), AND
  3) within the next HORIZON trading days, max(high) >= close * (1+threshold).

For each event, dump trigger-day indicator readings. Then compare each
indicator's distribution to the population of non-event days (sampled
from the same series), so we can see which indicators meaningfully
differ when the rally actually came.

Usage:
    python3 -m backtest.find_winners 2395 5388 2357
    python3 -m backtest.find_winners 2395 --thresholds 0.10,0.15,0.20 --horizon 90
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import _compute_rows  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"


def load_taiex():
    p = DATA_DIR / "_taiex.json"
    if not p.exists():
        return {}
    with p.open() as f:
        d = json.load(f)
    return {datetime.fromisoformat(k).date(): v for k, v in d["closes"].items()}


def load_series(code: str):
    with (DATA_DIR / f"{code}.json").open() as f:
        d = json.load(f)
    return [{
        "date": datetime.fromisoformat(r["date"]).date(),
        "high": r["high"], "low": r["low"],
        "close": r["close"], "lots": r["lots"],
    } for r in d["rows"]]


def find_reversal_rallies(rows, lookback: int, horizon: int,
                          drawdown: float, threshold: float):
    """Return list of (idx, max_gain_pct) for events meeting all 3 criteria."""
    events = []
    n = len(rows)
    for i in range(60, n - horizon):
        c = rows[i]["close"]
        if c is None:
            continue
        prior = rows[i - lookback: i + 1]
        prior_closes = [r["close"] for r in prior if r["close"] is not None]
        prior_highs = [r["high"] for r in prior if r["high"] is not None]
        if len(prior_closes) < lookback or not prior_highs:
            continue
        if c != min(prior_closes):
            continue
        # Was there a real drop into this low? (peak high in window >= c*(1+drawdown))
        if max(prior_highs) < c * (1 + drawdown):
            continue
        # Forward window
        future = rows[i + 1: i + 1 + horizon]
        future_highs = [r["high"] for r in future if r["high"] is not None]
        if not future_highs:
            continue
        max_gain = (max(future_highs) - c) / c
        if max_gain >= threshold:
            events.append((i, max_gain))
    return events


def features_at(rows, idx):
    """Indicator snapshot at rows[idx], with a few derived ratios."""
    r = rows[idx]
    out = {
        "k": r.get("kd_k"),
        "d": r.get("kd_d"),
        "rsi6": r.get("rsi6"),
        "rsi12": r.get("rsi12"),
        "adx": r.get("adx"),
    }
    close = r.get("close")
    ma5, ma10, ma20 = r.get("ma5"), r.get("ma10"), r.get("ma20")
    if close is not None and ma20:
        out["close_vs_ma20_pct"] = (close - ma20) / ma20 * 100
    if ma5 is not None and ma10:
        out["ma5_vs_ma10_pct"] = (ma5 - ma10) / ma10 * 100
    # 5-day volume ratio
    recent = [rr["lots"] for rr in rows[idx - 5: idx] if rr.get("lots")]
    if recent and r.get("lots"):
        avg = sum(recent) / len(recent)
        if avg > 0:
            out["vol_ratio"] = r["lots"] / avg
    # 20-day drawdown from peak high (how deep was the prior drop)
    prior20 = rows[idx - 20: idx + 1]
    highs = [rr["high"] for rr in prior20 if rr.get("high") is not None]
    if highs and close:
        peak = max(highs)
        out["drawdown_20d_pct"] = (close - peak) / peak * 100
    return out


def percentile(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize_feature(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "p25": percentile(vals, 0.25),
        "p75": percentile(vals, 0.75),
    }


def fmt(v):
    if v is None or v != v:
        return "  n/a"
    return f"{v:>7.2f}"


FEATURE_NAMES = [
    ("k", "K"),
    ("d", "D"),
    ("rsi6", "RSI6"),
    ("rsi12", "RSI12"),
    ("adx", "ADX"),
    ("close_vs_ma20_pct", "vsMA20%"),
    ("ma5_vs_ma10_pct", "MA5/10%"),
    ("vol_ratio", "VolRatio"),
    ("drawdown_20d_pct", "DD20d%"),
]


def run(code: str, lookback: int, horizon: int, drawdown: float,
        thresholds: list[float]) -> None:
    rows = _compute_rows(load_series(code), load_taiex())
    if not rows:
        print(f"[{code}] empty rows")
        return
    print(f"\n=== {code}  rows={len(rows)}  "
          f"lookback={lookback}  horizon={horizon}  "
          f"drawdown_in≥{drawdown*100:.0f}% ===")

    # Reference distribution: every valid trigger day (was-low + had-drawdown),
    # so the comparison isolates "what predicted the rally" vs "what just
    # marked a local low that didn't bounce".
    candidates = find_reversal_rallies(rows, lookback, horizon, drawdown, 0.0)
    cand_idxs = [i for i, _ in candidates]
    print(f"  reversal candidates (any forward gain): {len(cand_idxs)}")

    for thr in thresholds:
        winners = find_reversal_rallies(rows, lookback, horizon, drawdown, thr)
        win_idxs = [i for i, _ in winners]
        loser_idxs = [i for i in cand_idxs if i not in set(win_idxs)]
        print(f"\n  threshold ≥{thr*100:.0f}%: winners={len(winners)}  "
              f"non-winners={len(loser_idxs)}")
        if not winners:
            continue

        win_feats = {k: [] for k, _ in FEATURE_NAMES}
        lose_feats = {k: [] for k, _ in FEATURE_NAMES}
        for i in win_idxs:
            f = features_at(rows, i)
            for k in win_feats:
                win_feats[k].append(f.get(k))
        for i in loser_idxs:
            f = features_at(rows, i)
            for k in lose_feats:
                lose_feats[k].append(f.get(k))

        print(f"  {'feature':>9}  | {'win med':>8} {'win p25':>8} "
              f"{'win p75':>8} | {'lose med':>9} {'lose p25':>9} "
              f"{'lose p75':>9} | Δ median")
        for key, name in FEATURE_NAMES:
            ws = summarize_feature(win_feats[key])
            ls = summarize_feature(lose_feats[key])
            if ws["n"] == 0 or ls["n"] == 0:
                continue
            delta = ws["median"] - ls["median"]
            print(f"  {name:>9}  |"
                  f" {fmt(ws['median'])} {fmt(ws['p25'])} {fmt(ws['p75'])} "
                  f"| {fmt(ls['median'])} {fmt(ls['p25'])} {fmt(ls['p75'])}"
                  f" | {fmt(delta)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--lookback", type=int, default=20,
                    help="trigger day must be this-many-day low (default 20)")
    ap.add_argument("--horizon", type=int, default=60,
                    help="forward window for max-high gain (default 60)")
    ap.add_argument("--drawdown", type=float, default=0.05,
                    help="min prior peak-to-trigger drop, e.g. 0.05 = 5%")
    ap.add_argument("--thresholds", default="0.15,0.20",
                    help="comma-separated rally thresholds")
    args = ap.parse_args()
    thresholds = [float(t) for t in args.thresholds.split(",")]
    for code in args.codes:
        run(code, args.lookback, args.horizon, args.drawdown, thresholds)


if __name__ == "__main__":
    main()
