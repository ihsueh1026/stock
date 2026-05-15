# Out-of-sample validation: red_recovery and green_entry

Results from running `backtest/red_recovery.py` and `backtest/green_entry.py`
on a 20-stock TWSE out-of-sample tech universe (see `oos_tech_codes.txt`),
compared to the original 10-stock in-sample run.

All numbers below use the default regime parameters:
`--red-thresh 3 --red-days 5` / `--green-thresh 3 --quiet-days 5`,
TAIEX-stripped 40-day forward alpha, median across all events in each cell.

Codes:
- In-sample: 2317, 2324, 2357, 2379, 2395, 3033, 3034, 3231, 5388, 6285
- OOS: 2330, 2303, 2454, 3711, 3443, 3661, 5274, 2382, 6669, 3017, 2356, 2353, 2408, 2344, 3037, 8046, 2327, 2345, 3008, 2308

## red_recovery — red-regime exit events

Joint cell key: `still_red` set on the recovery day. `法人紅` means step 7
法人 was still red on the day red_count first dropped below threshold.

| cell | in-sample (n=351) | OOS (n=681) |
|---|---|---|
| 法人紅 + 量能非 (AVOID) | -5.33% / 34.5% (n=59) | **-2.39% / 42.1%** (n=100) |
| 法人非 + 量能紅 (in-sample BUY) | +0.93% / 53.7% (n=44) | +0.15% / 50.9% (n=60) |
| 法人非 + 量能非 | -1.58% / 42.9% (n=247) | +0.41% / 50.8% (n=514) |
| 法人紅 + 量能紅 | (n=0) | -2.08% / 20.0% (n=6, too small) |
| `<ANY>` baseline | -1.55% / 42.6% | -0.05% / 49.3% |

Per-code AVOID sign tally (40d alpha):
- In-sample: 8 negative / 2 positive (80% same sign)
- OOS: 13 negative / 5 positive / 2 no-events (72% same sign)

**Verdict:** AVOID filter survives OOS. Magnitude is roughly halved
(-5.3% to -2.4%) but direction and per-code breadth both hold. The
in-sample BUY filter (法人非 + 量能紅) is essentially flat in OOS
(+0.15%); attributed to in-sample concentration in 2324 and 2357 that
did not generalize.

## green_entry — green-regime entry events

Joint cell key: `still_non_green` set on the entry day. `法人非` means
step 7 法人 was non-green on the day green_count first crossed threshold.

| cell | in-sample (n=394) | OOS (n=778) |
|---|---|---|
| 法人非 + 量能非 (NONE) | -2.61% / 36.3% (n=128) | **-0.81% / 47.5%** (n=212) |
| 法人非 + 量能綠 | -1.43% / 42.0% (n=133) | +0.64% / 53.0% (n=323) |
| 法人綠 + 量能非 (LEAD) | +0.44% / 51.9% (n=82) | **+1.56% / 55.3%** (n=129) |
| 法人綠 + 量能綠 (BOTH) | -1.71% / 45.7% (n=48) | +0.83% / 52.3% (n=114) |
| `<ANY>` baseline | -1.55% / 42.6% | +0.51% / 51.8% |

Per-code 40d alpha sign tally (OOS):
- LEAD (法人綠 + 量能非): 11 positive / 8 negative / 1 no-events (58% same sign)
- BOTH (法人綠 + 量能綠): 10 positive / 9 negative (53% — coin flip)
- NONE (法人非 + 量能非): 7 positive / 13 negative (65% same sign)

**Verdict:**
- NONE acts as a moderate AVOID signal (-1.32% below baseline alpha,
  65% breadth) — consistent with red_recovery's AVOID.
- LEAD pools to +1.05% above baseline in OOS, but per-code breadth is
  only 58% and the magnitude is concentrated in high-momentum AI/ASIC
  names (3661 +41.5%, 3443 +12.5%). Treated as observation in the live
  dashboard, not as an entry signal.
- BOTH (textbook full confirmation) does not beat baseline by a
  meaningful margin in either sample. The in-sample paradox (BOTH
  underperforms LEAD) softens in OOS but the lack of edge stands.

## What landed in production

In [stock_web/app.py](../stock_web/app.py) `_compute_alerts`, two chips
were added based on these results:

1. `⚠ 法人未確認` (tone=warn) — fires when either
   - a red-regime exit leaves step 7 法人 still red, **or**
   - a green-regime entry has both step 7 法人 and step 4 量能 still
     non-green.

2. `✓ 法人提前+量能未發` (tone=info) — fires on green-regime entry when
   step 7 法人 is green but step 4 量能 is not yet green.

The chips do not gate any signal; they layer on top of the existing
7-step lights as observations. Frame for users:

- The ⚠ chip is the validated AVOID signal — skip these positions.
- The ✓ chip is contextual; it identified +1% above-baseline alpha in
  OOS pooling but with only 58% per-code breadth, so it is an
  observation that the move may have legs, not a buy command.

## Caveats

- Universe is TWSE tech only. Step 7 is gray for OTC stocks (no
  `t86otc/` archive) — 5274 in the OOS run had zero events for that
  reason.
- 5-year history (2021-04 to 2026-05) covers one full bull-bear cycle
  but is still a single regime window. A bear-heavy period (e.g.,
  2008-2009 or 2022-Q4) would test the rules under different
  conditions.
- Sample sizes per individual stock per cell are small (1-19 events),
  so per-code medians are noisy point estimates. Cross-stock sign
  tallies are the more reliable robustness check.
- "Forward alpha" strips TAIEX but not industry-specific betas. A
  semiconductor-only universe could systematically share an unmeasured
  factor that absorbs some of the observed effect.
