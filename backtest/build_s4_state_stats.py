"""Pre-compute per-stock S4 (借券 5日 ↓ ≥15%) forward-alpha stats.

Walks every code in backtest/data/, detects S4 first-cross events
using the same logic as `backtest/s4_sbl_covering_sweep.py` (with the
-15% sweet-spot threshold), and dumps per-code forward alpha at
5/10/20/40d into `backtest/data/_s4_state_stats.json`.

Mirrors the structure of `_eps_state_stats.json` so the live
endpoint can surface "歷史:S4 觸發 n=N, 40d 中位 alpha X% / 勝 Y%"
next to the current state badge in the detail-page 籌碼面 card.

Pool-level findings from `backtest/s4_sbl_covering_sweep.py`:
  threshold ≤-15%, 40d alpha +0.08% / 50% win, n=789 → +2.35pp
  vs baseline (-2.27%); per-stock breadth 61% (28/46 codes
  exceed pool baseline by ≥1pp). NOT chip-worthy — surface as
  observation-only tag.

Run after `backtest.prefetch_margin_sbl`:
    python3 -m backtest.build_s4_state_stats
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
from stock_web.margin_sbl_fetcher import _load_cached_sbl  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, load_series, load_taiex, forward_alpha,
)

S4_THRESHOLD = -0.15
LOOKBACK_DAYS = 5
OUT_PATH = DATA_DIR / "_s4_state_stats.json"


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _build_sbl_series(code: str, dates: list[str]) -> dict[str, int | None]:
    out = {}
    for d in dates:
        s = _load_cached_sbl(d)
        row = (s or {}).get(code) if s else None
        if not row:
            continue
        out[d] = row.get("bal_today")
    return out


def _summarize_alphas(vals):
    v = [a for a in vals if a is not None]
    if not v:
        return None
    return {
        "n": len(v),
        "alpha_med": round(statistics.median(v), 4),
        "win_pct": round(sum(1 for x in v if x > 0) / len(v), 3),
    }


def build():
    taiex = load_taiex()
    codes = _all_codes()
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold_pct": S4_THRESHOLD * 100,
        "lookback_days": LOOKBACK_DAYS,
        "horizons": HORIZONS,
        "codes": {},
    }
    covered = 0
    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        dates = [r["date"] if isinstance(r["date"], str)
                 else r["date"].isoformat() for r in rows]
        sbl_series = _build_sbl_series(code, dates)
        if len(sbl_series) < LOOKBACK_DAYS + 10:
            continue

        alphas: dict[int, list] = {h: [] for h in HORIZONS}
        last_fired = False
        for i, d in enumerate(dates):
            if d not in sbl_series:
                last_fired = False
                continue
            prior = [pd for pd in dates[:i] if pd in sbl_series]
            if len(prior) < LOOKBACK_DAYS:
                continue
            prev_key = prior[-LOOKBACK_DAYS]
            now_v = sbl_series[d]
            prev_v = sbl_series[prev_key]
            if now_v is None or prev_v is None or prev_v == 0:
                last_fired = False
                continue
            chg = (now_v - prev_v) / prev_v
            fires = chg <= S4_THRESHOLD
            if fires and not last_fired:
                for h in HORIZONS:
                    a = forward_alpha(rows, i, h)
                    if a is not None:
                        alphas[h].append(a)
            last_fired = fires

        cells = {str(h): _summarize_alphas(alphas[h]) for h in HORIZONS}
        cells = {k: v for k, v in cells.items() if v}
        if not cells:
            continue
        out["codes"][code] = cells
        covered += 1

    with OUT_PATH.open("w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(f"  codes covered: {covered}")


if __name__ == "__main__":
    build()
