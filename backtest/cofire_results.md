# Cross-chip same-day co-fire study

For every bar in the 50-stock universe (2021-04 ~ 2026-05), enumerate
which production chips emit on that bar, group by chip-combo, and
measure forward alpha at 5/10/20/40 trading days.

The 8 active chips have largely **mutually exclusive trigger
geometries**:

| Chip | Geometric requirement |
|---|---|
| AVOID | 紅燈延伸 + 法人=紅 (regime context) |
| REV-4 / REV-5 | 近 20 日低 + KD/RSI 超賣 + 法人=綠 |
| TOP-RED / TOP-YEL | 近 20 日高 + KD/RSI 超買 + 法人 紅/黃 |
| BEAR-DIV | 近期價新高 + RSI 不跟 |

A bar can be near the 20-day low OR near the 20-day high but not both;
法人=綠 and 法人=紅/黃 are mutually exclusive; so REV chips can't
co-fire with TOP chips, AVOID can't co-fire with REV (different
regime context), etc.

## Result: only 2 co-fire combos exist in 5 years × 50 stocks

| co-fire | n (40d-valid) | 5d α/win | 10d α/win | 20d α/win | 40d α/win |
|---|---|---|---|---|---|
| **BEAR-DIV + TOP-YEL** | **15** | -0.09% / 41% | +0.96% / 50% | **+5.32% / 60%** | **+4.48% / 60%** |
| BEAR-DIV + TOP-RED | 2 | -7.20% / 0% | -10.02% / 0% | -7.60% / 50% | -13.97% / 50% |

Compare to single-chip baselines:

| chip alone | n | 5d | 10d | 20d | 40d |
|---|---|---|---|---|---|
| TOP-YEL | 117 | +0.27 | +2.62 | +3.62 | +1.20 |
| TOP-RED | 40 | -2.27 | -0.57 | +2.19 | +5.41 |
| BEAR-DIV | 960 | -0.24 | -0.05 | +0.08 | +0.02 |

## Key finding: BEAR-DIV amplifies in TOP-YEL direction

When 強勢延伸 5★+黃 fires AND 頂背離 also fires on the same bar:
- 20d alpha jumps from +3.62% (TOP-YEL alone) to **+5.32%** (combo)
- 40d alpha jumps from +1.20% to **+4.48%**

Counter-intuitive: 頂背離 alone is a bearish-looking pattern, but in
the strong-momentum + institutions-holding-fire configuration it
appears to mark the *peak of strength* and the move continues. n=15
is small but the magnitude is striking.

The TOP-RED variant (n=2) inverts as expected — bearish institutions
+ bearish divergence = -14% at 40d. Too few samples to ship.

## UI implication: side-by-side multi-chip display NOT needed

Originally on the roadmap as "Item 2: 多空 chip 同日並排顯示". The
研究 reveals that co-fires are so rare (17 events in 5 years across
50 stocks ≈ 0.07 events/stock/year) that a dedicated UI surface for
multi-chip cases would be wasted real estate.

What we ship instead: the existing alerts strip already shows all
firing chips inline, and the backtest card picks the highest-priority
single chip for the historical breakdown. When BEAR-DIV + TOP-YEL
both fire, the card already displays TOP-YEL stats (higher priority),
and the BEAR-DIV chip is visible in the alerts strip.

If we later accumulate more samples and want to surface the combo
specifically as a known amplifier, it'd become a derived chip
(stat_key `bearish_div_and_topping_yellow` or similar) rather than a
display-time merge.

## Caveats

- Sample sizes for co-fires are tiny (15 + 2). Direction is consistent
  with the single-chip stories (BEAR-DIV is bearish technical pattern;
  with institutional split into yellow vs red, the resolution differs)
  but magnitudes have wide CIs.
- Other potentially-meaningful combos (AVOID + something, REV + BEAR-DIV)
  literally never occur in this dataset, so we can't test them.
- The cross_chip_study.py (committed earlier) tested chip × context-flag
  pairs where the context flag (divergence, volume_burst) was
  computed at the bar but didn't require it to be a CHIP emission.
  This co-fire study is stricter: both chips must independently
  trigger on the same bar.
