"""Pre-compute per-stock EPS-state forward-alpha stats.

Walks the full history of every code in backtest/data/ and for each
quarterly EPS announcement bucket the forward TAIEX-stripped alpha by
state at announcement:
   accel = YoY(Q) > YoY(Q-1) > YoY(Q-2) AND |EPS(Q)| >= 0.5
   decel = YoY(Q) < YoY(Q-1) < YoY(Q-2)
Same logic as `backtest/eps_acceleration_study.py` (A3 variant for accel).

Output: backtest/data/_eps_state_stats.json — consumed by app.py's
/api/eps_history endpoint to surface per-stock historical context
next to the current state badge. The frontend uses these to render a
tag like "歷史: 加速 n=7, 60d 中位 alpha +1.5%" so the user knows what
this stock's track record looks like (not the pool's).

Run after backfilling EPS history:
    python3 -m backtest.prefetch_eps
    python3 -m backtest.build_eps_state_stats
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import _compute_rows  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, load_series, load_taiex, forward_alpha,
)
from backtest.eps_acceleration_study import (  # noqa: E402
    _load_eps_series, _deadline_date, _index_for_date, _is_accelerating,
)

HORIZONS = [20, 60, 120]
MAG_THRESHOLD = 0.5
OUT_PATH = DATA_DIR / "_eps_state_stats.json"


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _stats(alphas: list[float | None]) -> dict | None:
    vals = [a for a in alphas if a is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "alpha_med": round(statistics.median(vals), 4),
        "win_pct": round(sum(1 for v in vals if v > 0) / len(vals), 3),
    }


def build():
    taiex = load_taiex()
    codes = _all_codes()
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "magnitude_threshold": MAG_THRESHOLD,
        "horizons": HORIZONS,
        "codes": {},
    }
    coverage_accel = 0
    coverage_decel = 0
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

        accel_alphas: dict[int, list] = {h: [] for h in HORIZONS}
        decel_alphas: dict[int, list] = {h: [] for h in HORIZONS}

        for i, period in enumerate(eps_series):
            if period.get("yoy_pct") is None:
                continue
            entry = _deadline_date(period["year"], period["season"])
            idx = _index_for_date(rows, entry)
            if idx is None:
                continue
            eps_val = period.get("eps")
            mag_ok = eps_val is not None and abs(eps_val) >= MAG_THRESHOLD
            accel = _is_accelerating(eps_series, i)
            decel = False
            if i >= 2:
                a, b, c = (eps_series[i].get("yoy_pct"),
                           eps_series[i - 1].get("yoy_pct"),
                           eps_series[i - 2].get("yoy_pct"))
                if None not in (a, b, c):
                    decel = a < b < c
            if accel and mag_ok:
                for h in HORIZONS:
                    accel_alphas[h].append(forward_alpha(rows, idx, h))
            if decel:
                for h in HORIZONS:
                    decel_alphas[h].append(forward_alpha(rows, idx, h))

        accel_out = {str(h): _stats(accel_alphas[h]) for h in HORIZONS}
        decel_out = {str(h): _stats(decel_alphas[h]) for h in HORIZONS}
        accel_out = {k: v for k, v in accel_out.items() if v}
        decel_out = {k: v for k, v in decel_out.items() if v}
        if not accel_out and not decel_out:
            continue
        out["codes"][code] = {"accel": accel_out, "decel": decel_out}
        if accel_out:
            coverage_accel += 1
        if decel_out:
            coverage_decel += 1

    with OUT_PATH.open("w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(f"  codes covered: {len(out['codes'])} "
          f"(accel data {coverage_accel}, decel data {coverage_decel})")


if __name__ == "__main__":
    build()
