"""Topping-quality event study — symmetric counterpart to
`reversal_quality_study.py`.

Question: we have low-point reversal quality (0-5 score, validated to
≥4 + 法人=綠 = strong positive alpha). Is there an equivalent top-point
quality score whose 4★/5★ events (especially conditional on 法人=non-
green) predict negative forward alpha better than the plain AVOID chip?

The score mirrors `_reversal_quality()` rotated 180°:
  1. close ≥ 20 日高點 -2% (near recent peak, not bottom)
  2. 前期漲幅 ≥5%       (extended rally, not drawdown)
  3. K > 75            (overbought, not oversold)
  4. RSI6 > 65          (overbought)
  5. 量比 ≥1.0 (爆量出貨) OR 量比 <0.7 (價漲量縮背離)

Conditioning: bucket each event by step 7 (法人) light at the event
bar. The hypothesis is that overbought-AND-法人未確認 sharpens the
AVOID -1.8% pool toward something closer to AVOID+爆量's -6.4%.

Universe: full backtest/data/ (50-stock TWSE tech pool).

Usage:
    python3 -m backtest.topping_quality_study
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, _topping_quality, MARKET_TWSE,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, forward_alpha,
    load_series, load_taiex,
    _compute_lights,
)

INST_IDX = 6  # step 7 法人 in the 7-step array


# ---------- event walking ----------------------------------------------

def _score_at(rows: list[dict], idx: int) -> int | None:
    if idx < 19:
        return None
    window = rows[max(0, idx - 19): idx + 1]
    tq = _topping_quality(window)
    return tq["score"] if tq else None


def find_exact_topping_events(rows: list[dict], target: int,
                              start: int = 60) -> list[int]:
    """One event per first-bar where topping score == target. Resets
    only when score moves away from target."""
    events: list[int] = []
    last_score: int | None = None
    for i in range(start, len(rows)):
        s = _score_at(rows, i)
        if s is None:
            continue
        if s == target and last_score != target:
            events.append(i)
        last_score = s
    return events


# ---------- helpers ----------------------------------------------------

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


# ---------- main study -------------------------------------------------

HORIZONS = (5, 10, 20, 40)

# Cells flagged as having pool-level edge in the multi-horizon study.
# We compute per-stock heterogeneity for these only — others were flat
# at the pool level, so per-stock breakdown is uninformative.
PER_STOCK_CELLS = [
    (5, "red", 5),    # 5★ + 法人=red @ 5d   pool: -1.01% / 39% (n=120)
    (3, "red", 10),   # 3★ + 法人=red @ 10d  pool: -0.76% / 45% (n=437)
    (3, "red", 20),   # 3★ + 法人=red @ 20d  pool: -0.97% / 46% (n=437)
    # 4★ rows look flat at pool level (-0.12% to +0.14% across all
    # horizons), but the monotonic break is suspicious — could be
    # two-tail offset rather than genuinely flat. Per-stock view
    # answers which.
    (4, "red", 5),    # 4★ + 法人=red @ 5d   pool: -0.12% / 48% (n=282)
    (4, "red", 10),   # 4★ + 法人=red @ 10d  pool: -0.17% / 49% (n=282)
    (4, "red", 20),   # 4★ + 法人=red @ 20d  pool: +0.14% / 51% (n=282)
    # 5★+yellow looks bullish at pool level — opposite direction of
    # 5★+red. Could be momentum continuation (institutions mixed but
    # technicals overbought + extended). Per-stock view confirms
    # whether the signal is broad or concentrated.
    (5, "yellow", 5),   # 5★ + 法人=yellow @ 5d   pool: +0.29% / 53% (n=263)
    (5, "yellow", 10),  # 5★ + 法人=yellow @ 10d  pool: +1.46% / 56% (n=263)
    (5, "yellow", 20),  # 5★ + 法人=yellow @ 20d  pool: +1.96% / 58% (n=263)
]


def run(codes: list[str]) -> None:
    taiex = load_taiex()

    # By score (3/4/5) × inst bucket × horizon: alpha lists
    by_score_inst: dict[int, dict[str, dict[int, list[float]]]] = {
        s: {inst: {h: [] for h in HORIZONS}
            for inst in ("red", "yellow", "green", "gray")}
        for s in (3, 4, 5)
    }
    by_score_plain: dict[int, dict[int, list[float]]] = {
        s: {h: [] for h in HORIZONS} for s in (3, 4, 5)
    }
    counts_total: dict[int, int] = {s: 0 for s in (3, 4, 5)}
    # Per-stock alphas keyed by (score, inst, horizon) for the cells we
    # care about. code -> list[alpha].
    per_stock_cells: dict[tuple, dict[str, list[float]]] = {
        cell: {} for cell in PER_STOCK_CELLS
    }

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _resolve_market(code)
        for score in (3, 4, 5):
            for idx in find_exact_topping_events(rows, target=score):
                # Require all horizons valid so each row shares the
                # same event base (apples-to-apples horizon comparison).
                a_by_h = {h: forward_alpha(rows, idx, h) for h in HORIZONS}
                if any(v is None for v in a_by_h.values()):
                    continue
                counts_total[score] += 1
                for h in HORIZONS:
                    by_score_plain[score][h].append(a_by_h[h])
                _, steps = _compute_lights(rows, idx, code=code, market=market)
                if not steps or len(steps) <= INST_IDX:
                    continue
                inst_light = steps[INST_IDX]["light"]
                if inst_light in by_score_inst[score]:
                    for h in HORIZONS:
                        by_score_inst[score][inst_light][h].append(a_by_h[h])
                # Per-stock accumulation for the flagged cells
                for (cell_score, cell_inst, cell_h) in PER_STOCK_CELLS:
                    if score == cell_score and inst_light == cell_inst:
                        per_stock_cells[(cell_score, cell_inst, cell_h)] \
                            .setdefault(code, []).append(a_by_h[cell_h])

    print(f"\nTopping-quality study — multi-horizon (50-stock universe)")
    print(f"Codes: {len(codes)}\n")

    hdr = (f"{'score':<6} {'cond':<14}"
           + "".join(f"{h:>3}d α / win% ".rjust(18) for h in HORIZONS))
    print(hdr)
    print("-" * len(hdr))

    def _row(score_lbl: str, cond: str, by_h: dict[int, list[float]]):
        parts = [f"{score_lbl:<6} {cond:<14}"]
        for h in HORIZONS:
            s = _summarize(by_h[h])
            if s["n"] == 0:
                parts.append(f"{'':>18}")
            else:
                parts.append(
                    f"{_fmt_pct(s['median'])} / {s['win_pct']*100:>4.1f}% ".rjust(18)
                )
        print("".join(parts))

    for score in (3, 4, 5):
        stars = "★" * score
        _row(stars, "plain", by_score_plain[score])
        for inst in ("red", "yellow", "green", "gray"):
            if len(by_score_inst[score][inst][40]) < 5:
                continue
            _row("", f"  +法人={inst}", by_score_inst[score][inst])
        non_green = {
            h: (by_score_inst[score]["red"][h]
                + by_score_inst[score]["yellow"][h]
                + by_score_inst[score]["gray"][h])
            for h in HORIZONS
        }
        if len(non_green[40]) >= 5:
            _row("", "  +法人非綠", non_green)
        print()

    print("Total event counts (all-horizons-valid):")
    for s in (3, 4, 5):
        print(f"  {s}★: {counts_total[s]}")

    # Per-stock heterogeneity for the flagged cells
    for (cell_score, cell_inst, cell_h) in PER_STOCK_CELLS:
        cell_data = per_stock_cells[(cell_score, cell_inst, cell_h)]
        if not cell_data:
            continue
        total_n = sum(len(v) for v in cell_data.values())
        all_alphas = [a for v in cell_data.values() for a in v]
        pool = _summarize(all_alphas)
        stars = "★" * cell_score
        print()
        print(f"Per-stock breakdown: {stars} +法人={cell_inst} @ {cell_h}d "
              f"(n={total_n}, pool {_fmt_pct(pool['median'])}/"
              f"{pool['win_pct']*100:.1f}%)")
        rows_table = []
        for code, alphas in cell_data.items():
            if len(alphas) < 3:
                continue
            s = _summarize(alphas)
            rows_table.append((code, s))
        if not rows_table:
            print("  (no stock has n≥3 events)")
            continue
        rows_table.sort(key=lambda x: x[1]["median"])
        print(f"  {'code':<6} {'n':>3} {'alpha':>10} {'win%':>7}")
        print(f"  {'-'*32}")
        neg_count = 0
        pos_count = 0
        flat_count = 0
        for code, s in rows_table:
            if s["median"] < -0.01:
                neg_count += 1
            elif s["median"] > 0.01:
                pos_count += 1
            else:
                flat_count += 1
            print(f"  {code:<6} {s['n']:>3} "
                  f"{_fmt_pct(s['median']):>10} "
                  f"{s['win_pct']*100:>6.1f}%")
        print(f"  -- {neg_count} stocks negative (<-1%) · "
              f"{flat_count} flat · {pos_count} positive (>+1%)")


def main():
    run(_all_codes())


if __name__ == "__main__":
    main()
