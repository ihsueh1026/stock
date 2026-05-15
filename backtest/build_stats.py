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
    _compute_lights,
)
from backtest.red_recovery import find_recovery_events  # noqa: E402
from backtest.green_entry import find_entry_events  # noqa: E402
from backtest.reversal_quality_study import find_exact_events  # noqa: E402
from stock_web.app import _compute_rows, _market_for, MARKET_TWSE  # noqa: E402

INST_IDX = 6  # step 7 (法人) in the steps array
VOL_IDX = 3  # step 4 (量能) in the steps array
INST_BUCKETS = ("green", "non_green")

# Chip kinds that the dashboard surfaces and that we have validated
# conditional stats for. Each entry is a stat-bucket key written into
# the output JSON's `chips` block; the frontend maps a firing chip to
# one of these keys to display its historical track record.
CHIP_KINDS = (
    "inst_not_confirmed",
    "inst_lead",
    "reversal_inst_confirm_3",
    "reversal_inst_confirm_4",
    "reversal_inst_confirm_5",
)


def _chip_events_for_code(rows: list[dict], code: str, market: str
                          ) -> dict[str, list[int]]:
    """Find all chip-trigger events in this stock's history.

    AVOID (`inst_not_confirmed`) has two trigger variants — red-regime
    exit with 法人 still red, OR green-regime entry with both 法人 and
    量能 non-green — pooled into one bucket because the chip is the
    same. LEAD (`inst_lead`) is green-regime entry with 法人=綠 and
    量能 non-green. `reversal_inst_confirm_{N}` for N in {4,5} is
    reversal-quality score == N AND step 7 法人 = green on that bar.
    """
    out: dict[str, list[int]] = {k: [] for k in CHIP_KINDS}

    # AVOID — red-exit variant
    for evt in find_recovery_events(rows, code, market,
                                    red_thresh=3, red_days=5):
        if INST_IDX in set(evt["still_red"]):
            out["inst_not_confirmed"].append(evt["idx"])

    # AVOID — green-entry variant; LEAD — green-entry variant
    for evt in find_entry_events(rows, code, market,
                                 green_thresh=3, quiet_days=5):
        non_green = set(evt["still_non_green"])
        inst_non = INST_IDX in non_green
        vol_non = VOL_IDX in non_green
        if inst_non and vol_non:
            out["inst_not_confirmed"].append(evt["idx"])
        elif (not inst_non) and vol_non:
            out["inst_lead"].append(evt["idx"])

    # Reversal-quality + 法人=綠
    for score in (3, 4, 5):
        for idx in find_exact_events(rows, target=score):
            _, steps = _compute_lights(rows, idx, code=code, market=market)
            if not steps or len(steps) <= INST_IDX:
                continue
            if steps[INST_IDX]["light"] == "green":
                out[f"reversal_inst_confirm_{score}"].append(idx)

    return out

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
    # Conditional pools split by step 7 (法人) state at event time —
    # OOS validation showed inst state materially shifts forward alpha
    # (法人未確認 chip), so showing the conditional distribution lets
    # the dashboard tell the user "given today's 法人 light, what did
    # this same summary label deliver historically".
    pooled_inst: dict[str, dict[str, dict[int, dict[str, list]]]] = {}
    events_count: dict[str, int] = {}
    events_count_inst: dict[str, dict[str, int]] = {}
    # Chip-condition pools — keyed by chip kind, one bucket per chip.
    chip_pools: dict[str, dict[int, dict[str, list]]] = {}
    chip_events_count: dict[str, int] = {}
    summary_signals = [k for k, v in SIGNAL_DEFS.items() if v["type"] == "summary"]

    for code in codes:
        try:
            series = load_series(code)
        except FileNotFoundError:
            continue
        rows = _compute_rows(series, taiex)
        if not rows:
            continue
        market = _market_for(code) or MARKET_TWSE
        print(f"  {code}: {len(rows)} rows ({market})", file=sys.stderr)
        for sig_key in summary_signals:
            sig_def = SIGNAL_DEFS[sig_key]
            label = sig_def["label"]
            events = find_events(rows, sig_def, code=code, market=market)
            events_count[label] = events_count.get(label, 0) + len(events)
            if not events:
                continue
            # Bucket each event by step 7 light at event time.
            events_by_inst: dict[str, list[int]] = {b: [] for b in INST_BUCKETS}
            for i in events:
                _, steps = _compute_lights(rows, i, code=code, market=market)
                if not steps or len(steps) <= INST_IDX:
                    continue
                inst_light = steps[INST_IDX]["light"]
                bucket = "green" if inst_light == "green" else "non_green"
                events_by_inst[bucket].append(i)
            ec_inst = events_count_inst.setdefault(label, {b: 0 for b in INST_BUCKETS})
            for b in INST_BUCKETS:
                ec_inst[b] += len(events_by_inst[b])

            buckets = pooled.setdefault(label, {})
            inst_pool = pooled_inst.setdefault(
                label, {b: {} for b in INST_BUCKETS})
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
                # Per-inst-bucket pools (no random baseline — the
                # unconditional rand_med is already shown; the conditional
                # view is for comparing to the unconditional pooled stat,
                # not to a re-sampled random).
                for bucket, evs in events_by_inst.items():
                    if not evs:
                        continue
                    bhi = inst_pool[bucket].setdefault(
                        h, {"ret": [], "alpha": []})
                    for i in evs:
                        r = forward_return(rows, i, h)
                        a = forward_alpha(rows, i, h)
                        if r is not None:
                            bhi["ret"].append(r)
                        if a is not None:
                            bhi["alpha"].append(a)

        # Per-code chip events — only computed once per code, then
        # poured into the global chip_pools across all horizons.
        chip_evts = _chip_events_for_code(rows, code, market)
        for chip_key, idxs in chip_evts.items():
            chip_events_count[chip_key] = (
                chip_events_count.get(chip_key, 0) + len(idxs))
            if not idxs:
                continue
            buckets = chip_pools.setdefault(chip_key, {})
            for h in HORIZONS:
                bh = buckets.setdefault(h, {"ret": [], "alpha": []})
                for i in idxs:
                    r = forward_return(rows, i, h)
                    a = forward_alpha(rows, i, h)
                    if r is not None:
                        bh["ret"].append(r)
                    if a is not None:
                        bh["alpha"].append(a)

    def _serialize_horizons(buckets: dict, include_rand: bool = True) -> dict:
        per_horizon: dict[str, dict] = {}
        for h, bh in buckets.items():
            ret_s = summarize(bh["ret"])
            alpha_s = summarize(bh["alpha"])
            entry = {
                "n": ret_s.get("n", 0),
                "ret_med": ret_s.get("median"),
                "ret_mean": ret_s.get("mean"),
                "win_pct": ret_s.get("win_pct"),
                "alpha_med": alpha_s.get("median"),
                "alpha_mean": alpha_s.get("mean"),
                "alpha_win": alpha_s.get("win_pct"),
            }
            if include_rand:
                rand_s = summarize(bh.get("rand", []))
                entry["rand_med"] = rand_s.get("median")
                entry["rand_win"] = rand_s.get("win_pct")
            per_horizon[str(h)] = entry
        return per_horizon

    # Serialize stats per (label, horizon).
    out_signals: dict[str, dict] = {}
    for label, buckets in pooled.items():
        ec_inst = events_count_inst.get(label, {b: 0 for b in INST_BUCKETS})
        out_signals[label] = {
            "events_total": events_count.get(label, 0),
            "horizons": _serialize_horizons(buckets, include_rand=True),
            "by_inst": {
                bucket: {
                    "events_total": ec_inst.get(bucket, 0),
                    "horizons": _serialize_horizons(
                        pooled_inst.get(label, {}).get(bucket, {}),
                        include_rand=False,
                    ),
                }
                for bucket in INST_BUCKETS
            },
        }

    out_chips: dict[str, dict] = {}
    for chip_key in CHIP_KINDS:
        buckets = chip_pools.get(chip_key, {})
        out_chips[chip_key] = {
            "events_total": chip_events_count.get(chip_key, 0),
            "horizons": _serialize_horizons(buckets, include_rand=False),
        }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stocks": codes,
        "horizons": HORIZONS,
        "signals": out_signals,
        "chips": out_chips,
        "note": ("觀察用,非進場依據。事件樣本來自 "
                 + ", ".join(codes) + " 的長期歷史。"),
    }
    with OUT_PATH.open("w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {OUT_PATH} ({len(out_signals)} signal labels, "
          f"{sum(events_count.values())} signal events; "
          f"{len(out_chips)} chip kinds, "
          f"{sum(chip_events_count.values())} chip events)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
