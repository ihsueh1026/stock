"""SOX bear regime × Taiwan chip conditioning study.

Question: when the PHLX Semiconductor Index (^SOX) is in a drawdown
regime, do Taiwan tech chips (反轉 4★+綠 / 5★+綠, 高點 5★+red, 強勢延伸
5★+yellow, 頂背離) deliver materially different forward alpha than
when SOX is in expansion?

Regime definition mirrors the TAIEX regime classifier already used in
`backtest/build_stats.py`: trailing 60-day drawdown ≥ 7.5% from the
rolling peak → "bear", else "bull". Applied to ^SOX daily closes.

For each chip event in the 50-stock universe (using exactly the same
event-detection logic as build_stats._chip_events_for_code), look up
the SOX regime on the event date and bucket the forward alpha at
5/10/20/40d. Spread between bull and bear cells = the conditioning
effect SOX would add over the unconditional pool.

If a chip shows ≥3pp 40d alpha gap (bull − bear), it's a candidate
for the same kind of soft-downgrade we already do for TAIEX bear.

Universe: 50 codes under backtest/data/, full chip set from
build_stats.CHIP_KINDS.

Usage:
    python3 -m backtest.sox_regime_chip_study
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_web.app import (  # noqa: E402
    _compute_rows, _market_for, MARKET_TWSE,
    TAIEX_BEAR_THRESH, TAIEX_LOOKBACK,
)
from backtest.study import (  # noqa: E402
    DATA_DIR, HORIZONS, load_series, load_taiex,
    forward_alpha,
)
from backtest.build_stats import (  # noqa: E402
    _chip_events_for_code, CHIP_KINDS,
)


SOX_PATH = DATA_DIR / "_us_SOX.json"


def _load_sox() -> dict[str, float]:
    """date_iso → close."""
    with SOX_PATH.open() as f:
        d = json.load(f)
    return {r["date"]: r["close"] for r in d.get("rows", [])
            if r.get("close") is not None}


def _classify_sox_regimes(sox: dict[str, float]) -> dict[str, str]:
    """date → 'bull'|'bear' using trailing-{TAIEX_LOOKBACK}d drawdown
    ≥{TAIEX_BEAR_THRESH}. Mirrors `build_stats._classify_taiex_regimes`."""
    sd = sorted(sox.keys())
    sv = [sox[d] for d in sd]
    out = {}
    for i, d in enumerate(sd):
        lo = max(0, i - TAIEX_LOOKBACK + 1)
        peak = max(sv[lo: i + 1])
        if peak <= 0:
            out[d] = "bull"
            continue
        dd = (sv[i] - peak) / peak
        out[d] = "bear" if dd <= -TAIEX_BEAR_THRESH else "bull"
    return out


def _all_codes() -> list[str]:
    return sorted(
        p.stem for p in DATA_DIR.glob("*.json")
        if not p.stem.startswith("_")
    )


def _summarize(vals: list[float]) -> dict | None:
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


def run():
    print("Loading SOX + classifying regimes ...", file=sys.stderr)
    sox = _load_sox()
    regimes = _classify_sox_regimes(sox)
    n_bull = sum(1 for v in regimes.values() if v == "bull")
    n_bear = sum(1 for v in regimes.values() if v == "bear")
    print(f"  SOX regime days: bull={n_bull}, bear={n_bear} "
          f"(bear share {n_bear / (n_bull + n_bear) * 100:.1f}%)",
          file=sys.stderr)

    taiex = load_taiex()
    codes = _all_codes()

    # chip_kind -> regime -> horizon -> list[alpha]
    buckets: dict[str, dict[str, dict[int, list[float]]]] = defaultdict(
        lambda: {"bull": {h: [] for h in HORIZONS},
                 "bear": {h: [] for h in HORIZONS}})
    counts_by_regime: dict[str, dict[str, int]] = defaultdict(
        lambda: {"bull": 0, "bear": 0, "unknown": 0})

    for code in codes:
        try:
            raw = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(raw, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE
        chip_evts = _chip_events_for_code(rows, code, market)
        for kind, idxs in chip_evts.items():
            for i in idxs:
                d = rows[i].get("date")
                d_iso = d if isinstance(d, str) else (d.isoformat()
                                                     if d else None)
                if d_iso is None:
                    counts_by_regime[kind]["unknown"] += 1
                    continue
                reg = regimes.get(d_iso)
                if reg is None:
                    # SOX hadn't started yet (early TWSE history) — skip
                    counts_by_regime[kind]["unknown"] += 1
                    continue
                counts_by_regime[kind][reg] += 1
                for h in HORIZONS:
                    a = forward_alpha(rows, i, h)
                    buckets[kind][reg][h].append(a)

    print("\nSOX bear regime × chip forward-alpha conditioning")
    print("(unconditional pool counts and stats from "
          "backtest/data/_summary_stats.json may not include SOX-classified "
          "events older than the SOX history start)\n")

    print(f"{'Chip':<28} {'Regime':<5} {'n':>4}  "
          + "  ".join(f"{h:>3}d med {h:>3}d win" for h in HORIZONS))
    print("-" * 100)
    for kind in CHIP_KINDS:
        for reg in ("bull", "bear"):
            cnt = counts_by_regime[kind][reg]
            cells = []
            for h in HORIZONS:
                s = _summarize(buckets[kind][reg][h])
                if not s:
                    cells.append(f"{'n/a':>8}  {'n/a':>8}")
                else:
                    cells.append(f"{_fmt(s['median']):>8}  "
                                 f"{s['win_pct'] * 100:>6.1f}%")
            print(f"{kind:<28} {reg:<5} {cnt:>4}  " + "  ".join(cells))
        # Compute bull − bear spread @ 40d for headline ranking
        s_bull = _summarize(buckets[kind]["bull"][40])
        s_bear = _summarize(buckets[kind]["bear"][40])
        if s_bull and s_bear:
            spread = (s_bull["median"] - s_bear["median"]) * 100
            print(f"{'':<28} {'spread':<5} {'':>4}  "
                  f"bull − bear @ 40d = {spread:+.2f}pp "
                  f"(bull n={s_bull['n']}, bear n={s_bear['n']})")
        print()

    # Headline: which chips have the largest regime conditioning?
    print("Headline — chips ranked by |bull − bear| @ 40d alpha median:")
    rows_sort = []
    for kind in CHIP_KINDS:
        sb = _summarize(buckets[kind]["bull"][40])
        sx = _summarize(buckets[kind]["bear"][40])
        if not sb or not sx:
            continue
        spread = (sb["median"] - sx["median"]) * 100
        rows_sort.append((abs(spread), spread, kind, sb, sx))
    rows_sort.sort(reverse=True)
    print(f"  {'chip':<28} {'spread':>9}  {'bull':>15} {'bear':>15}")
    for _, spread, kind, sb, sx in rows_sort:
        bull_str = f"{_fmt(sb['median'])}/{sb['win_pct'] * 100:.0f}% (n={sb['n']})"
        bear_str = f"{_fmt(sx['median'])}/{sx['win_pct'] * 100:.0f}% (n={sx['n']})"
        print(f"  {kind:<28} {spread:+8.2f}pp  {bull_str:>15} {bear_str:>15}")


if __name__ == "__main__":
    run()
