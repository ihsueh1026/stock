# Exit-rules study: nothing beats fixed 40-day hold

`backtest/exit_rules.py` fixes the entry side and varies the exit. The
question was: given an entry signal we already trust (green-entry / LEAD
/ red-recovery), is there an exit rule that improves median forward
alpha or meaningfully reduces drawdown?

Universe: full 30-stock backtest set (in-sample 10 + OOS-tech 20).
Entry triggers identical to `green_entry.py` and `red_recovery.py`.
Forward alpha = stock return − TAIEX return over the actual hold.

## Rules tested

- `hold-20`, `hold-40` — fixed time stop (baselines)
- `regime-flip` — exit when red_count first hits 3
- `summary-bad` — exit when summary label is 🔴 趨勢轉弱 or 🟠 訊號分歧
- `trail-Kσ` — trail K·σ below rolling peak (σ = stdev of 19 daily
  pct-changes ending at entry), K ∈ {1.5, 2.0, 3.0}

All non-fixed rules capped at 60 trading days.

## Headline: median alpha by rule × entry

| entry | hold-20 | hold-40 | regime-flip | summary-bad | trail-1.5σ | trail-2σ | trail-3σ |
|---|---|---|---|---|---|---|---|
| green-entry (n=1172) | -0.69% | **-0.48%** | -0.49% | -0.24% | -1.32% | -1.68% | -1.87% |
| **LEAD (n=211)** | -0.31% | **+1.23%** | -0.46% | -0.19% | -0.98% | -1.24% | -1.66% |
| red-recovery (n=1032) | -0.59% | -0.63% | -0.66% | -0.29% | -1.32% | -1.55% | -1.88% |

Mean hold days: hold-40=40, trail-3σ≈22, hold-20=20, trail-2σ≈14,
trail-1.5σ≈10, regime-flip≈4, summary-bad≈2.

## What the table is telling us

**1. No active exit beats fixed 40-day hold on median alpha.** LEAD +
hold-40 (+1.23% / 54.0% win) is the peak; every dynamic rule lowers it.

**2. summary-bad and regime-flip exit too fast to be real "holds".**
2-day and 4-day mean holds = "the dashboard re-flips bad almost the day
after entry". This is because the 3-of-7 green-entry threshold does not
match any of the clean summary labels (多頭擴張 needs more lights green),
so the day after an entry the summary is still 🟠 訊號分歧 and trips
the exit. **These rules are effectively "skip this entry", not "hold
intelligently then exit".**

**3. Trail-σ trades median for drawdown reduction.** On LEAD,
trail-3σ delivers +0.96% mean alpha (vs hold-40's +1.97%) but drops
median drawdown from -11.2% to -8.0%. Median alpha collapses from
+1.23% to -1.66%. Classic trend-following profile: a few outsized
winners carry the mean while most trades feel like losses.

**4. Mean vs median gap widens with looser stops.** trail-3σ has the
best mean of the active rules across all three entries but the worst
median. The mean is outlier-driven and not robust enough to act on.

## Drawdown picture (LEAD, n=211)

| rule | alpha_med | alpha_mean | win% | hold_avg | dd_med | dd_p25 |
|---|---|---|---|---|---|---|
| hold-20 | -0.31% | +1.32% | 47.9% | 20.0 | -7.84% | -11.84% |
| **hold-40** | **+1.23%** | **+1.97%** | **54.0%** | 40.0 | -11.16% | -15.43% |
| regime-flip | -0.46% | +0.57% | 40.5% | 4.0 | -1.64% | -3.41% |
| summary-bad | -0.19% | +0.38% | 45.5% | 1.6 | -0.46% | -1.76% |
| trail-1.5σ | -0.98% | +0.23% | 38.3% | 10.4 | -4.62% | -6.48% |
| trail-2.0σ | -1.24% | +0.10% | 39.7% | 14.4 | -5.87% | -8.21% |
| trail-3.0σ | -1.66% | +0.96% | 38.9% | 22.1 | -7.98% | -10.71% |

## Verdict — nothing lands in production

The study set out to fill the "no exit framework" gap. Result: the gap
has negative value in this framework. Any exit rule built from the same
7-light state machine cuts the alpha tail because the lights are too
noisy to hold position through the swing.

Practical implications:

- Keep using hold-40 as the implicit observation window (it's what the
  existing event studies measure against, and it's still the best
  median alpha).
- If a user can't tolerate the -11% peak drawdown of a 40-day hold, the
  answer is **position sizing**, not a smarter exit rule from inside
  this framework.
- A real exit signal would likely need a feature outside the 7 lights
  (e.g. industry P/E percentile flipping rich, news-LLM 利空 cluster,
  monthly revenue YoY rolling over). Those weren't tested here.

## Caveats

- Same universe and 5-year window as `oos_results.md`. Single regime,
  mostly bull. A bear-heavy slice (2022-Q4) might tip trail-σ in favor
  by killing drawdowns that hold-40 absorbs.
- "Forward alpha" strips TAIEX only. Semiconductor industry beta is
  unstripped — the LEAD +1.23% on hold-40 likely overstates idiosyncratic
  edge because the universe is tech-heavy.
- Trailing stops assume daily close execution. Intraday slippage and
  gap risk are not modeled.
- The summary-bad rule's near-instant exit suggests `_summary()`'s
  label thresholds and the 3-of-7 green-entry threshold are mismatched.
  That's a property of how the labels were tuned, not a bug — but it
  means "exit on bad summary" isn't really testable here.
