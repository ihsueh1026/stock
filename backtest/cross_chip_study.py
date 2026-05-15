"""Cross-chip interaction study: when AVOID / 反轉+法人到位 fire, do
co-occurring observational alerts (divergence, volume burst/dry)
materially shift forward alpha?

Question form: "AVOID fires today. Does it predict MORE downside if
頂背離 also fires the same day?" If yes, the dashboard could surface
that combo as a stronger warning. If the conditional alpha is the same
as the chip's pooled alpha, the context alerts don't add information
on top of the chip.

We pair each chip event with the divergence + volume flag values on
the event bar, bucket by flag presence, and compare 40d alpha medians.
T86 streak flags are skipped here — they require reading the T86 archive
per date and the analysis adds enough complexity without them.

Usage:
    python3 -m backtest.cross_chip_study
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
)
from backtest.build_stats import CHIP_KINDS, _chip_events_for_code  # noqa: E402


def _resolve_market(code: str) -> str:
    try:
        m = _market_for(code)
    except Exception:
        m = None
    return m or MARKET_TWSE


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _volume_flags(window: list[dict]) -> tuple[bool, bool]:
    """Return (volume_burst, volume_dry) for the window's last bar.

    Mirrors the thresholds in stock_web.app._compute_alerts so a flag
    we set here is the same condition that would fire the live chip.
    """
    if len(window) < 2:
        return False, False
    today_vol = window[-1].get("lots")
    vols = [r.get("lots") for r in window[:-1] if r.get("lots") is not None]
    if today_vol is None or len(vols) < 5:
        return False, False
    recent20 = vols[-20:]
    avg = sum(recent20) / len(recent20)
    if avg <= 0:
        return False, False
    ratio = today_vol / avg
    sd = statistics.stdev(recent20) if len(recent20) >= 5 else 0
    burst = (today_vol > avg + 2 * sd) and (ratio >= 1.5)
    dry = ratio < 0.5
    return burst, dry


def _fmt_pct(x):
    if x is None or x != x:
        return "   n/a"
    return f"{x * 100:+5.2f}%"


def _summarize(alphas):
    if not alphas:
        return {"n": 0}
    return {
        "n": len(alphas),
        "median": statistics.median(alphas),
        "win_pct": sum(1 for a in alphas if a > 0) / len(alphas),
    }


CONTEXT_FLAGS = [
    "bearish_div", "bullish_div", "volume_burst", "volume_dry",
]


def run(codes: list[str]) -> None:
    taiex = load_taiex()
    # chip_kind -> flag_name -> {"true": [...], "false": [...]}
    by_chip: dict[str, dict[str, dict[str, list[float]]]] = {
        k: {f: {"true": [], "false": []} for f in CONTEXT_FLAGS}
        for k in CHIP_KINDS
    }
    chip_counts: dict[str, int] = {k: 0 for k in CHIP_KINDS}

    for code in codes:
        raw = load_series(code)
        rows = _compute_rows(raw, taiex)
        market = _resolve_market(code)
        chip_evts = _chip_events_for_code(rows, code, market)
        for chip_kind, idxs in chip_evts.items():
            for idx in idxs:
                window = rows[max(0, idx - 19): idx + 1]
                if len(window) < 20:
                    continue
                a40 = forward_alpha(rows, idx, 40)
                if a40 is None:
                    continue
                chip_counts[chip_kind] += 1
                div = _divergence(window)
                flags = {
                    "bearish_div": bool(div and div.get("kind") == "bearish"),
                    "bullish_div": bool(div and div.get("kind") == "bullish"),
                }
                burst, dry = _volume_flags(window)
                flags["volume_burst"] = burst
                flags["volume_dry"] = dry
                for fname, fval in flags.items():
                    by_chip[chip_kind][fname][
                        "true" if fval else "false"
                    ].append(a40)

    print(f"\nCross-chip interaction study (50-stock universe)")
    print(f"Codes: {len(codes)}")
    print()
    print(f"{'chip':<28} {'flag':<14} {'flag=True':>20} {'flag=False':>20}  {'diff':>9}  {'verdict':>14}")
    print("-" * 115)
    for chip_kind in CHIP_KINDS:
        # Only iterate flags whose true-count is at least 5 (else not
        # meaningful as a sub-sample).
        for fname in CONTEXT_FLAGS:
            t = by_chip[chip_kind][fname]["true"]
            f = by_chip[chip_kind][fname]["false"]
            if len(t) < 5 or len(f) < 5:
                continue
            ts = _summarize(t)
            fs = _summarize(f)
            diff = ts["median"] - fs["median"]
            t_str = f"n={ts['n']:>4} {_fmt_pct(ts['median'])} ({ts['win_pct']*100:.0f}%)"
            f_str = f"n={fs['n']:>4} {_fmt_pct(fs['median'])} ({fs['win_pct']*100:.0f}%)"
            # Verdict heuristic: does the flag move alpha by >= 1pp?
            if abs(diff) < 0.01:
                verdict = "no shift"
            elif (diff < 0) == (fs["median"] < 0):
                verdict = "AMPLIFIES" if abs(fs["median"]) < abs(ts["median"]) else "DAMPENS"
            else:
                verdict = "FLIPS" if abs(diff) >= 0.02 else "weak shift"
            print(f"  {chip_kind:<26} {fname:<14} {t_str:>20} {f_str:>20}  {_fmt_pct(diff):>9}  {verdict:>14}")
        print()

    print("Total chip events per kind:")
    for k, n in chip_counts.items():
        print(f"  {k}: {n}")


def main():
    run(_all_codes())


if __name__ == "__main__":
    main()
