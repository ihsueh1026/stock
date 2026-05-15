# Bear-regime sub-sample test: do the chips survive drawdowns?

`backtest/bear_regime_test.py` classifies each TAIEX trading day by
its trailing-60-day drawdown (≥10% from the 60d high → bear regime,
otherwise bull). 30-stock universe over 2021-04 to 2026-05; the 2022
bear (TAIEX -31.6%) plus three smaller corrections in 2024 and 2025
give 147 bear days vs 1,092 bull days (12% bear share).

Each chip event is bucketed by event-day regime, then forward alpha
is pooled per (chip, regime). The question: does the chip's bull-day
signal survive when the market is in a drawdown?

## Results (40d forward alpha, vs TAIEX)

| chip | bull n | bull 40d alpha | bull win | bear n | bear 40d alpha | bear win | verdict |
|---|---|---|---|---|---|---|---|
| **AVOID** (inst_not_confirmed) | 488 | -1.94% | 42.6% | 19 | **-8.45%** | **21.1%** | **stronger in bear** |
| LEAD (inst_lead) | 141 | +1.58% | 57.6% | 70 | **-0.76%** | 47.1% | **FLIPS — bull-only** |
| 反轉 3★+綠 | 283 | +0.98% | 52.9% | 87 | +2.19% | 52.9% | stronger in bear |
| 反轉 4★+綠 | 173 | +3.59% | 57.3% | 73 | +2.96% | 55.7% | similar |
| 反轉 5★+綠 | 69 | +3.29% | 60.9% | 48 | **+3.79%** | **68.1%** | best in bear |

## Bear-regime spans (1,239 trading days, 147 bear)

| span | trading days | TAIEX context |
|---|---|---|
| 2022-05-06 ~ 2022-05-26 | 20 | early 2022 leg |
| 2022-06-16 ~ 2022-08-10 | 55 | summer drawdown |
| 2022-09-26 ~ 2022-11-10 | 45 | Oct 2022 trough |
| 2024-08-02 ~ 2024-08-13 | 11 | Aug 2024 correction |
| 2024-09-04 ~ 2024-09-18 | 14 | Sept 2024 dip |
| 2025-03-31 ~ 2025-05-13 | 43 | spring 2025 |

(plus a handful of single-day spans)

## What this means for the chips

### AVOID — strongest in bear

The "法人未確認" warning that was already validated OOS at -2.4% / 42%
sharpens dramatically in actual drawdowns: -8.45% 40d alpha with only
21% win rate. Bear sample (n=19) is small enough to want more data,
but the direction is unambiguous — the signal works precisely when
it most matters.

### LEAD — bull-only signal

The "法人提前+量能未發" pattern delivers +1.58% in bull but flips to
-0.76% in bear (n=70, decent sample). Mechanism: institutional buying
in a downtrend is often short-term cover/rebound trading, not durable
accumulation. The chip should be demoted or muted during bear regimes
because its bull-period edge does not transfer.

### Reversal+法人綠 — robust, gets better at lower magnitudes

All three star tiers stay positive in bear:
- 3★+綠: +0.98% bull → +2.19% bear (n=87)
- 4★+綠: +3.59% bull → +2.96% bear (n=73)
- 5★+綠: +3.29% bull → +3.79% bear (n=48, win 68%)

Reversal events that fire during bear regimes appear to be the most
selective — only stocks with genuine technical setup + institutional
confirmation survive the broader downtrend, which raises the average
quality. 5★+綠 in bear is the highest-confidence cell in the entire
study (68% win on 48 events).

## Implications for the production dashboard

The chips currently fire identically regardless of TAIEX regime. Two
candidate UX changes:

1. **Surface current regime** — show "今日 TAIEX 跌市" tag near the
   TAIEX bar so users have context when reading any chip.
2. **Demote LEAD in bear** — when TAIEX is in bear regime, change
   LEAD chip tone from `info` to a muted `warn` (or suppress) since
   the historical edge inverts.

Both are small changes if we surface the regime once in the dashboard
payload. Not yet implemented.

## Threshold sensitivity

The 10% drawdown threshold was an initial pick. Sweep across five
thresholds (LEAD 40d alpha, bear sub-sample):

| threshold | bear days share | LEAD bear n | LEAD bear 40d | LEAD verdict |
|---|---|---|---|---|
| 5.0% | 380 (31%) | 134 | +1.1% / 53% | ROBUST |
| 7.5% | 242 (20%) | 85 | +0.9% / 52% | ROBUST |
| **10.0%** | 147 (12%) | 70 | **-0.8% / 47%** | **FLIPS** ← current |
| 12.5% | 83 (7%) | 34 | +0.2% / 50% | ROBUST |
| 15.0% | 49 (4%) | 28 | +0.2% / 50% | ROBUST |

**The "LEAD flips in bear" verdict only holds at 10%.** At 5% / 7.5% /
12.5% / 15% LEAD's bear alpha stays positive (though weaker than bull's
+1.3-1.6%). The negative reading at 10% comes from the ~14 events that
fall in the 7.5-10% drawdown band; those events appear to cluster in a
specific time window with bad outcomes that flip the bucket median.

AVOID and reversal+綠 verdicts are stable across all thresholds — they
stay ROBUST regardless. AVOID's magnitude varies more (-1.9% to -8.5%
40d) because bear sample drops from n=67 (5%) to n=3 (15%), but
direction is consistent.

**Implication for the production LEAD demotion**: it's built on a
threshold-sensitive reading. The directional finding ("LEAD is weaker
in bear at every threshold") is robust; the magnitude ("LEAD flips
sign") is not. The chip currently muts (tone=warn) on bear regime —
that may be over-confident given this fragility. Two ways to reconcile
on next iteration: (a) keep but re-frame text from "(跌市裡反向)" to
"(跌市裡偏弱)", or (b) move threshold to 7.5% which has the largest
bear sample and consistent ROBUST verdict.

## Caveats

- Bear sample sizes are small at the 10% default (AVOID bear n=19,
  reversal_5 bear n=48 the largest). At 7.5% threshold AVOID gets
  n=43 — still small.
- All bear spans cluster in 2022 + 2024 H2 + 2025 H1. A different
  bear (different macro driver) might behave differently.
- "Drawdown from 60d high" is a regime label, not a leading indicator
  itself. By the time we classify a day as bear, the drawdown is
  already evident.
