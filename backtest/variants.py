"""Experimental rule variants for backtest A/B comparison.

These do NOT replace the production functions in stock_web.app — they are
forks used only by backtest/study.py. If a variant proves itself in
backtest, port the change back to stock_web/app.py deliberately.
"""
from __future__ import annotations


def step_3_momentum_v2(window, last, prev):
    """Same as _step_3_momentum but downgrades overbought (K>80 or RSI6>75)
    to red so they cannot lift summary into 'sub-strong'.

    Hypothesis from 2395 dump: many sub-strong events fired with K=83/85,
    RSI6=75-83 — already in the late stage of a rally — and were followed
    by mean-reverting drawdowns. Treating those as warnings (not yellow)
    should remove the worst events from the entry set.
    """
    needed = (last.get("ma5"), last.get("ma10"), last.get("rsi6"),
              last.get("kd_k"), last.get("kd_d"),
              prev.get("rsi6"), prev.get("kd_k"), prev.get("kd_d"))
    base = {"step": 3, "title": "動能三合 (v2:超買濾網)",
            "condition": "MA5>MA10 + K>D + RSI6>50 + 非超買 (K≤80 且 RSI6≤75)"}
    if any(v is None for v in needed):
        return {**base, "light": "gray", "detail": "資料不足"}
    ma5, ma10, rsi6, k, d = (last["ma5"], last["ma10"], last["rsi6"],
                              last["kd_k"], last["kd_d"])
    rsi6_p, k_p, d_p = prev["rsi6"], prev["kd_k"], prev["kd_d"]

    c_ma = ma5 > ma10
    c_kd = k > d
    c_rsi = rsi6 > 50
    overbought = (k > 80) or (rsi6 > 75)

    rsi6_recent = [r["rsi6"] for r in window[-11:-1] if r.get("rsi6") is not None]
    rebounded_from_oversold = bool(rsi6_recent) and min(rsi6_recent) < 30

    kd_low_zone = (k_p < 20) or (40 <= k_p < 50)
    kd_golden_low = (k_p < d_p) and (k > d) and kd_low_zone
    rsi6_first_50 = (rsi6_p < 50) and (rsi6 >= 50) and rebounded_from_oversold
    k_first_50 = (k_p < 50) and (k >= 50)
    trigger = kd_golden_low or rsi6_first_50 or k_first_50

    if overbought:
        # Late-stage rally — never qualifies as bullish entry, regardless
        # of how many other conditions look healthy.
        light = "red"
    elif c_ma and c_kd and c_rsi and trigger:
        light = "green"
    elif c_ma and c_kd and c_rsi:
        light = "yellow"
    elif sum([c_ma, c_kd, c_rsi]) >= 2:
        light = "yellow"
    else:
        light = "red"

    kd_cross_today = "✓" if (k_p < d_p and k > d) else "✗"
    suffix = "(自<30反彈首破50)" if rsi6_first_50 else ""
    ob_tag = "  ⚠超買" if overbought else ""
    detail = (f"MA金叉:{'✓' if c_ma else '✗'}  "
              f"KD金叉:{kd_cross_today}(K={k:.0f})  "
              f"RSI6:{rsi6:.0f}{suffix}{ob_tag}")
    return {**base, "light": light, "detail": detail}


def filter_s3_green(rows, idx, steps) -> bool:
    """Keep event only if step 3 is green at the event day. Compensates
    for the production summary's `first_4_green >= 3` rule, which lets
    step 3 stay red/yellow yet still upgrade to 'sub-strong'."""
    return steps[2]["light"] == "green"


VARIANTS = {
    # (step3_fn, post_filter)
    "v1": (None, None),                       # production
    "v2": (step_3_momentum_v2, None),         # overbought filter only
    "v3": (step_3_momentum_v2, filter_s3_green),  # + require s3 green
}
