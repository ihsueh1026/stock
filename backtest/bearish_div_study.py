"""Pure 頂背離 (bearish divergence) event study on the 50-stock pool.

Background: cross_chip_study measured bearish_div in CO-OCCURRENCE with
chip events (AVOID / reversal+綠), but bearish_div alone was never
measured against the universe. This study fills that gap.

Event = first bar (per stock) where `_divergence(window)` returns
`kind == "bearish"`. Resets only when divergence drops away from
bearish. Each event measured at 40d forward alpha (TAIEX-stripped),
then bucketed by step 7 (法人) light at event time.

Hypothesis to test: pure bearish_div has materially negative forward
alpha at the 50-stock pool level. If yes and 法人=non-green sharpens
it, this is a candidate standalone chip (current topping signals are
all gated through AVOID).

Universe: full backtest/data/ (50-stock TWSE tech pool).

Usage:
    python3 -m backtest.bearish_div_study
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, _divergence, MARKET_TWSE,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, forward_alpha,
    load_series, load_taiex,
    _compute_lights,
)

INST_IDX = 6  # step 7 法人


# ---------- event walking ---------------------------------------------

def find_bearish_div_events(rows: list[dict], start: int = 60) -> list[int]:
    """One event per first-crossing into bearish divergence.

    Resets only when the divergence drops away from bearish (returning
    None or bullish). 20-bar window mirrors `_divergence`'s expected
    input size in production (it requires ≥15 anyway).
    """
    events: list[int] = []
    last_kind: str | None = None
    for i in range(start, len(rows)):
        window = rows[max(0, i - 19): i + 1]
        if len(window) < 15:
            last_kind = None
            continue
        kind = (_divergence(window) or {}).get("kind")
        if kind == "bearish" and last_kind != "bearish":
            events.append(i)
        last_kind = kind
    return events


# ---------- helpers ---------------------------------------------------

def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _resolve_market(code: str) -> str:
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _fmt_pct(x):
    if x is None or x != x:
        return "   n/a"
    return f"{x * 100:+6.2f}%"


def _summarize(alphas):
    if not alphas:
        return {"n": 0, "median": float("nan"), "win_pct": float("nan")}
    return {
        "n": len(alphas),
        "median": statistics.median(alphas),
        "win_pct": sum(1 for a in alphas if a > 0) / len(alphas),
    }


def _volume_flag(window: list[dict]) -> bool:
    """Mirror _compute_alerts' volume_burst: today_vol > avg + 2σ
    AND ratio ≥ 1.5. Used to test the bearish_div + 爆量 combo
    parallel to AVOID + 爆量."""
    if len(window) < 21:
        return False
    today_vol = window[-1].get("lots")
    vols = [r.get("lots") for r in window[-21:-1] if r.get("lots") is not None]
    if today_vol is None or len(vols) < 5:
        return False
    avg = sum(vols) / len(vols)
    if avg <= 0:
        return False
    ratio = today_vol / avg
    sd = statistics.stdev(vols) if len(vols) >= 5 else 0
    return (today_vol > avg + 2 * sd) and (ratio >= 1.5)


# ---------- main ------------------------------------------------------

HORIZONS = (5, 10, 20, 40)


def run(codes: list[str]) -> None:
    taiex = load_taiex()

    # Buckets — keyed by horizon
    plain: dict[int, list[float]] = {h: [] for h in HORIZONS}
    by_inst: dict[str, dict[int, list[float]]] = {
        "red": {h: [] for h in HORIZONS},
        "yellow": {h: [] for h in HORIZONS},
        "green": {h: [] for h in HORIZONS},
        "gray": {h: [] for h in HORIZONS},
    }
    # Per-stock 40d (kept for heterogeneity table)
    per_stock: dict[str, list[float]] = {}

    total_events = 0
    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _resolve_market(code)
        evts = find_bearish_div_events(rows)
        if not evts:
            continue
        per_stock.setdefault(code, [])
        for idx in evts:
            # Need at least the longest horizon's alpha to count as a
            # valid event (so all horizons share the same event base).
            a_by_h = {h: forward_alpha(rows, idx, h) for h in HORIZONS}
            if any(v is None for v in a_by_h.values()):
                continue
            total_events += 1
            for h in HORIZONS:
                plain[h].append(a_by_h[h])
            per_stock[code].append(a_by_h[40])

            _, steps = _compute_lights(rows, idx, code=code, market=market)
            inst_light = (
                steps[INST_IDX]["light"]
                if steps and len(steps) > INST_IDX else None
            )
            if inst_light in by_inst:
                for h in HORIZONS:
                    by_inst[inst_light][h].append(a_by_h[h])

    print(f"\nBearish-divergence (頂背離) event study (50-stock pool)")
    print(f"Codes scanned: {len(codes)}  Events (all-horizons-valid): {total_events}\n")

    # Horizon table: rows = condition, cols = horizon
    hdr = f"{'condition':<22}" + "".join(f"{h:>3}d α / win% ".rjust(18) for h in HORIZONS)
    print(hdr)
    print("-" * len(hdr))

    def _row(label: str, by_h: dict[int, list[float]]):
        parts = [f"{label:<22}"]
        for h in HORIZONS:
            s = _summarize(by_h[h])
            if s["n"] == 0:
                parts.append(f"{'':>18}")
            else:
                parts.append(
                    f"{_fmt_pct(s['median'])} / {s['win_pct']*100:>4.1f}% ".rjust(18)
                )
        print("".join(parts))

    _row("plain", plain)
    for inst in ("red", "yellow", "green", "gray"):
        if len(by_inst[inst][40]) < 5:
            continue
        _row(f"  +法人={inst}", by_inst[inst])
    non_green = {
        h: by_inst["red"][h] + by_inst["yellow"][h] + by_inst["gray"][h]
        for h in HORIZONS
    }
    _row("  +法人非綠", non_green)

    print()
    # n by bucket
    print("Sample sizes (same across all horizons; events require all 4):")
    print(f"  plain: n={len(plain[40])}")
    for inst in ("red", "yellow", "green", "gray"):
        n = len(by_inst[inst][40])
        if n >= 5:
            print(f"  +法人={inst}: n={n}")
    print(f"  +法人非綠: n={len(non_green[40])}")

    # Per-stock heterogeneity at 40d (only codes with ≥5 events)
    print()
    print("Per-stock 40d breakdown (n≥5 events):")
    print(f"{'code':<8} {'n':>4} {'40d alpha':>12} {'win%':>7}")
    print("-" * 36)
    rows_table = []
    for code, alphas in per_stock.items():
        if len(alphas) < 5:
            continue
        st = _summarize(alphas)
        rows_table.append((code, st))
    rows_table.sort(key=lambda x: x[1]["median"])
    for code, st in rows_table:
        print(f"{code:<8} {st['n']:>4} "
              f"{_fmt_pct(st['median']):>12} {st['win_pct']*100:>6.1f}%")


def main():
    run(_all_codes())


if __name__ == "__main__":
    main()
