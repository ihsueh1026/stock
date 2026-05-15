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
    _compute_rows, _market_for, MARKET_TWSE,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, forward_alpha,
    load_series, load_taiex,
    _compute_lights,
)

INST_IDX = 6  # step 7 法人 in the 7-step array


# ---------- topping-quality score ---------------------------------------

def _topping_quality(window: list[dict]) -> dict | None:
    """Score how 'topping-shaped' the current bar is, 0..5.

    Mirror of `stock_web.app._reversal_quality()` — same 5-condition
    framework, but flipped: near 20d high, recent rally, overbought
    KD/RSI, and volume that's either burst (出貨) or dry (價漲量縮背離).
    """
    if len(window) < 20:
        return None
    last = window[-1]
    close = last.get("close")
    if close is None:
        return None
    closes20 = [r["close"] for r in window[-20:] if r.get("close") is not None]
    lows20 = [r["low"] for r in window[-20:] if r.get("low") is not None]
    highs20 = [r["high"] for r in window[-20:] if r.get("high") is not None]
    if len(closes20) < 20 or not lows20 or not highs20:
        return None
    peak_high_20 = max(highs20)
    min_low_20 = min(lows20)
    near_high_pct = (peak_high_20 - close) / peak_high_20 * 100  # ≥ 0
    runup_pct = (close - min_low_20) / min_low_20 * 100  # ≥ 0

    k = last.get("kd_k")
    rsi6 = last.get("rsi6")
    lots = last.get("lots")
    lots5 = [r["lots"] for r in window[-6:-1] if r.get("lots") is not None]

    checks = []
    # 1. close 在 20 日高點附近 (≤2%)
    checks.append({
        "name": "近 20 日高點 (≤2%)",
        "passed": near_high_pct <= 2.0,
        "detail": f"距 20 日高 -{near_high_pct:.1f}%",
    })
    # 2. 前期漲幅 ≥5%
    checks.append({
        "name": "前期漲幅 ≥5%",
        "passed": runup_pct >= 5.0,
        "detail": f"自 20 日低 +{runup_pct:.1f}%",
    })
    # 3. K > 75 (KD 超買)
    checks.append({
        "name": "K 超買 (>75)",
        "passed": k is not None and k > 75,
        "detail": f"K={k:.1f}" if k is not None else "K 資料不足",
    })
    # 4. RSI6 > 65
    checks.append({
        "name": "RSI6 偏高 (>65)",
        "passed": rsi6 is not None and rsi6 > 65,
        "detail": f"RSI6={rsi6:.1f}" if rsi6 is not None else "RSI6 資料不足",
    })
    # 5. 量比 ≥ 1.0 (爆量出貨) OR ≤0.7 (價漲量縮)
    if lots is not None and lots5:
        avg = sum(lots5) / len(lots5)
        ratio = (lots / avg) if avg > 0 else 0
        c5_pass = (ratio >= 1.0) or (ratio < 0.7)
        c5_detail = f"量比={ratio:.2f}x"
    else:
        c5_pass = False
        c5_detail = "量資料不足"
    checks.append({"name": "量比 出貨/背離", "passed": c5_pass, "detail": c5_detail})

    score = sum(1 for c in checks if c["passed"])
    return {"score": score, "checks": checks}


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

def run(codes: list[str]) -> None:
    taiex = load_taiex()

    # By score (3/4/5) × inst bucket: alpha lists
    by_score_inst: dict[int, dict[str, list[float]]] = {
        s: {"red": [], "yellow": [], "green": [], "gray": []}
        for s in (3, 4, 5)
    }
    # Plain (no inst filter) per score
    by_score_plain: dict[int, list[float]] = {s: [] for s in (3, 4, 5)}
    counts_total: dict[int, int] = {s: 0 for s in (3, 4, 5)}

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
                a40 = forward_alpha(rows, idx, 40)
                if a40 is None:
                    continue
                counts_total[score] += 1
                by_score_plain[score].append(a40)
                _, steps = _compute_lights(rows, idx, code=code, market=market)
                if not steps or len(steps) <= INST_IDX:
                    continue
                inst_light = steps[INST_IDX]["light"]
                if inst_light in by_score_inst[score]:
                    by_score_inst[score][inst_light].append(a40)

    print(f"\nTopping-quality study (50-stock universe)")
    print(f"Codes: {len(codes)}\n")

    print(f"{'score':<8} {'cond':<14} {'n':>6} {'40d alpha':>12} {'win%':>7}")
    print("-" * 56)
    for score in (3, 4, 5):
        stars = "★" * score
        plain = _summarize(by_score_plain[score])
        print(f"{stars:<8} {'plain':<14} {plain['n']:>6} "
              f"{_fmt_pct(plain['median']):>12} "
              f"{plain['win_pct']*100:>6.1f}%")
        for inst in ("red", "yellow", "green", "gray"):
            s = _summarize(by_score_inst[score][inst])
            if s["n"] < 5:
                continue
            label = f"  +法人={inst}"
            print(f"{'':<8} {label:<14} {s['n']:>6} "
                  f"{_fmt_pct(s['median']):>12} "
                  f"{s['win_pct']*100:>6.1f}%")
        # Bonus: pooled 法人非綠 (red+yellow+gray) — the "topping with
        # institutional weakness" combo that mirrors AVOID logic.
        non_green = (by_score_inst[score]["red"] +
                     by_score_inst[score]["yellow"] +
                     by_score_inst[score]["gray"])
        ng = _summarize(non_green)
        if ng["n"] >= 5:
            print(f"{'':<8} {'  +法人非綠':<14} {ng['n']:>6} "
                  f"{_fmt_pct(ng['median']):>12} "
                  f"{ng['win_pct']*100:>6.1f}%")
        print()

    print("Total event counts (40d forward alpha available):")
    for s in (3, 4, 5):
        print(f"  {s}★: {counts_total[s]}")


def main():
    run(_all_codes())


if __name__ == "__main__":
    main()
