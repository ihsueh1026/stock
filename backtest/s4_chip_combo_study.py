"""Does S4 (借券 5日↓≥15%) strengthen existing chips when they
co-fire?

S4 alone is a marginal signal (40d alpha +0.08% / 50% win, but
+2.35pp vs baseline thanks to a negative pool baseline). The
hypothesis tested here: when S4 fires within the trailing 5
trading days of a known-edge chip event (反轉 4★/5★+綠, 高點 5★+紅,
強勢延伸 5★+黃, 頂背離), does the chip's forward alpha widen?

"S4 active" definition: in the 5-trading-day window [i-4, i] for the
chip's event bar i, did 借券_5d_pct ≤ -15% on any day? This is
LOOSE on purpose — S4 can lead the chip by a few days as the short
interest unwinds before price confirmation kicks in.

Universe: 50 codes in backtest/data/, ~700 trading days of margin/SBL
history (mid-2023 to 2026 via TWSE retention).

Output: per chip, two cells (combo vs solo) × 4 horizons. Report
the deltas. If any combo widens by ≥2pp at 40d AND keeps n ≥ 20,
that's a ship-worthy sub-tag.

Run:
    python3 -m backtest.s4_chip_combo_study
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, MARKET_TWSE,
)
from stock_web.margin_sbl_fetcher import _load_cached_sbl  # noqa: E402
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, load_series, load_taiex, forward_alpha,
)
from backtest.build_stats import (  # noqa: E402
    _chip_events_for_code, CHIP_KINDS,
)

S4_THRESHOLD = -0.15   # 借券 5日 ↓ ≥ 15%
LOOKBACK_DAYS = 5      # window for S4 % change
COMBO_WINDOW = 5       # check S4 fired within last N trading days


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


def _s4_active_mask(dates: list[str],
                    sbl_series: dict[str, int | None]) -> list[bool]:
    """Return a boolean list parallel to `dates`: True if S4
    (借券_5d_pct ≤ S4_THRESHOLD) fires on that bar."""
    out = [False] * len(dates)
    indexed_dates = [d for d in dates if d in sbl_series]
    for i, d in enumerate(dates):
        if d not in sbl_series:
            continue
        # Find SBL value LOOKBACK_DAYS trading days prior (in sbl_series)
        prior = [pd for pd in dates[:i] if pd in sbl_series]
        if len(prior) < LOOKBACK_DAYS:
            continue
        prev_key = prior[-LOOKBACK_DAYS]
        now_v = sbl_series[d]
        prev_v = sbl_series[prev_key]
        if now_v is None or prev_v is None or prev_v == 0:
            continue
        chg = (now_v - prev_v) / prev_v
        if chg <= S4_THRESHOLD:
            out[i] = True
    return out


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


def run(codes: list[str]):
    taiex = load_taiex()
    # chip_key -> {"combo": {h: list}, "solo": {h: list}}
    buckets: dict[str, dict[str, dict[int, list]]] = {
        ck: {"combo": {h: [] for h in HORIZONS},
             "solo":  {h: [] for h in HORIZONS}}
        for ck in CHIP_KINDS
    }

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE
        dates = [r["date"] if isinstance(r["date"], str)
                 else r["date"].isoformat() for r in rows]
        sbl_series = _build_sbl_series(code, dates)
        # If we have no SBL data at all for this code, treat S4 as
        # always inactive (skip rather than mask everything).
        if not sbl_series:
            continue
        s4_mask = _s4_active_mask(dates, sbl_series)

        # Build set of bar indices where S4 was active within the last
        # COMBO_WINDOW trading days (inclusive of current bar).
        combo_eligible: set[int] = set()
        for i in range(len(dates)):
            for j in range(max(0, i - COMBO_WINDOW + 1), i + 1):
                if s4_mask[j]:
                    combo_eligible.add(i)
                    break

        chip_evts = _chip_events_for_code(rows, code, market)
        for ck, idxs in chip_evts.items():
            for i in idxs:
                bucket_name = "combo" if i in combo_eligible else "solo"
                for h in HORIZONS:
                    a = forward_alpha(rows, i, h)
                    if a is not None:
                        buckets[ck][bucket_name][h].append(a)

    print("\nS4 × chip combo study")
    print(f"S4 def: 借券 {LOOKBACK_DAYS}日 ↓ ≥ {-S4_THRESHOLD * 100:.0f}%; "
          f"combo = S4 active within last {COMBO_WINDOW} trading days "
          f"of chip event\n")

    print(f"{'Chip':<28} {'Bucket':<6} {'h':>3} {'n':>4}  "
          f"{'alpha_med':>10}  {'win%':>6}")
    print("-" * 70)
    for ck in CHIP_KINDS:
        for bucket_name in ("solo", "combo"):
            for h in HORIZONS:
                s = _summarize(buckets[ck][bucket_name][h])
                if not s:
                    continue
                print(f"{ck:<28} {bucket_name:<6} {h:>3} {s['n']:>4}  "
                      f"{_fmt(s['median']):>10}  "
                      f"{s['win_pct'] * 100:>5.1f}%")
        # Delta at 40d
        c40 = _summarize(buckets[ck]["combo"][40])
        s40 = _summarize(buckets[ck]["solo"][40])
        if c40 and s40:
            delta = (c40["median"] - s40["median"]) * 100
            wpd = (c40["win_pct"] - s40["win_pct"]) * 100
            print(f"{ck:<28} {'Δ40d':<6} {'':>3} {'':>4}  "
                  f"combo − solo = {delta:+5.2f}pp median, "
                  f"{wpd:+5.1f}pp win")
        print()

    # Ranking summary
    print("─" * 70)
    print("Ranked by |Δ combo − solo at 40d| (where both n ≥ 20):")
    rows_rank = []
    for ck in CHIP_KINDS:
        c40 = _summarize(buckets[ck]["combo"][40])
        s40 = _summarize(buckets[ck]["solo"][40])
        if not c40 or not s40:
            continue
        if c40["n"] < 20 or s40["n"] < 20:
            continue
        delta = (c40["median"] - s40["median"]) * 100
        rows_rank.append((delta, ck, c40, s40))
    rows_rank.sort(key=lambda x: -abs(x[0]))
    for delta, ck, c40, s40 in rows_rank:
        print(f"  {ck:<28}  Δ{delta:+5.2f}pp  "
              f"(combo {c40['n']}/{c40['win_pct'] * 100:.0f}% vs "
              f"solo {s40['n']}/{s40['win_pct'] * 100:.0f}%)")


def main():
    codes = _all_codes()
    if not codes:
        print(f"no stock files under {DATA_DIR}", file=sys.stderr)
        sys.exit(1)
    run(codes)


if __name__ == "__main__":
    main()
