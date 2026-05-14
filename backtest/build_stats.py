"""Aggregate `backtest.study` outputs across every stock under data/.

Run this once after collecting new backtest data:

    python3 -m backtest.build_stats

It walks `backtest/data/*.json` (skipping `_taiex.json`), runs the
event study for every summary label in `SIGNAL_DEFS`, and pools the
forward-return / forward-alpha observations across stocks. The pooled
distribution is what the live dashboard reads to display a small
"歷史上看到此狀態後的 20 日報酬" card.

Output: `backtest/data/_summary_stats.json` with shape:

    {
      "generated_at": "...",
      "stocks": ["2357", "2395", "5388"],
      "horizons": [5, 10, 20, 40],
      "signals": {
        "🟢 多頭擴張": {
          "events_total": 42,
          "horizons": {
            "20": {"n": 38, "ret_med": 0.012, "ret_mean": 0.018,
                   "win_pct": 0.55, "alpha_med": 0.003,
                   "rand_med": 0.008, "rand_win": 0.52}
          }
        }, ...
      }
    }

This is the only consumer-facing file. The API in app.py reads it as
plain JSON. If the file is missing, the API returns `available=false`
and the frontend silently skips the card.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import random  # noqa: E402

from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, SIGNAL_DEFS,
    load_taiex, load_series,
    find_events, forward_return, forward_alpha, summarize,
)
from stock_web.app import _compute_rows  # noqa: E402

OUT_PATH = DATA_DIR / "_summary_stats.json"


def _iter_codes() -> list[str]:
    codes = []
    for p in sorted(DATA_DIR.glob("*.json")):
        if p.name.startswith("_"):
            continue
        codes.append(p.stem)
    return codes


def main() -> None:
    taiex = load_taiex()
    codes = _iter_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    # Per signal, accumulate per-horizon return + alpha lists across all
    # codes. Random baselines are pooled the same way.
    pooled: dict[str, dict[int, dict[str, list]]] = {}
    events_count: dict[str, int] = {}
    summary_signals = [k for k, v in SIGNAL_DEFS.items() if v["type"] == "summary"]

    for code in codes:
        try:
            series = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(series, taiex)
        if not rows:
            continue
        print(f"  {code}: {len(rows)} rows", file=sys.stderr)
        for sig_key in summary_signals:
            sig_def = SIGNAL_DEFS[sig_key]
            label = sig_def["label"]
            events = find_events(rows, sig_def)
            events_count[label] = events_count.get(label, 0) + len(events)
            if not events:
                continue
            buckets = pooled.setdefault(label, {})
            for h in HORIZONS:
                bh = buckets.setdefault(h, {"ret": [], "alpha": [], "rand": []})
                for i in events:
                    r = forward_return(rows, i, h)
                    a = forward_alpha(rows, i, h)
                    if r is not None:
                        bh["ret"].append(r)
                    if a is not None:
                        bh["alpha"].append(a)
                # Pool random-sample returns from the same series so the
                # baseline is comparable. Sample N events worth of random
                # indices, each row's forward return is one observation.
                rng = random.Random(42 + h)
                valid = range(60, len(rows) - h)
                if len(valid) >= len(events):
                    sampled = rng.sample(list(valid), len(events))
                    for j in sampled:
                        rr = forward_return(rows, j, h)
                        if rr is not None:
                            bh["rand"].append(rr)

    # Serialize stats per (label, horizon).
    out_signals: dict[str, dict] = {}
    for label, buckets in pooled.items():
        per_horizon: dict[str, dict] = {}
        for h, bh in buckets.items():
            ret_s = summarize(bh["ret"])
            alpha_s = summarize(bh["alpha"])
            rand_s = summarize(bh["rand"])
            per_horizon[str(h)] = {
                "n": ret_s.get("n", 0),
                "ret_med": ret_s.get("median"),
                "ret_mean": ret_s.get("mean"),
                "win_pct": ret_s.get("win_pct"),
                "alpha_med": alpha_s.get("median"),
                "alpha_mean": alpha_s.get("mean"),
                "alpha_win": alpha_s.get("win_pct"),
                "rand_med": rand_s.get("median"),
                "rand_win": rand_s.get("win_pct"),
            }
        out_signals[label] = {
            "events_total": events_count.get(label, 0),
            "horizons": per_horizon,
        }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stocks": codes,
        "horizons": HORIZONS,
        "signals": out_signals,
        "note": ("觀察用,非進場依據。事件樣本來自 "
                 + ", ".join(codes) + " 的長期歷史。"),
    }
    with OUT_PATH.open("w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {OUT_PATH} ({len(out_signals)} signal labels, "
          f"{sum(events_count.values())} total events)", file=sys.stderr)


if __name__ == "__main__":
    main()
