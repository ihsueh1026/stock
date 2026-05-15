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

## Caveats

- Bear sample sizes are small (AVOID bear n=19 most worrying). The
  direction is consistent but 95% CI is wide.
- 10% drawdown threshold is somewhat arbitrary. Sensitivity tests
  with 7.5% / 12.5% / 15% would tell us how robust the bull/bear
  partition is.
- All bear spans cluster in 2022 + 2024 H2 + 2025 H1. A different
  bear (different macro driver) might behave differently.
- "Drawdown from 60d high" is a regime label, not a leading indicator
  itself. By the time we classify a day as bear, the drawdown is
  already evident.
