# Cross-chip interaction study

When a validated chip fires, do co-occurring observational alerts
(divergence, volume burst/dry) shift the historical forward alpha?
The shipped chips are independent signals; this study asks whether
**combinations** carry additional information.

Universe: 50-stock TWSE tech pool, 2021-04 ~ 2026-05.
Method: for each chip event, evaluate divergence + volume flags on the
event bar (same logic as `_compute_alerts`), bucket events by flag
presence, compare 40d alpha medians + win rates.

## Headline results

| chip × flag | n=true | true 40d / win | false 40d / win | shift | note |
|---|---|---|---|---|---|
| **AVOID + 爆量** | 38 | **-6.40% / 42%** | -1.84% / 44% | **-4.56pp** | AMPLIFIES |
| AVOID + bullish_div (頂背離) | 52 | -4.57% / 35% | -1.71% / 44% | -2.86pp | AMPLIFIES |
| AVOID + volume_dry (量縮) | 151 | -1.93% / 41% | -1.86% / 44% | -0.06pp | no shift |
| **4★+綠 + 爆量** | 29 | **+4.79% / 69%** | +1.27% / 54% | **+3.52pp** | AMPLIFIES |
| 4★+綠 + bullish_div | 44 | +0.49% / 55% | +2.02% / 55% | -1.53pp | weak dampen |
| 4★+綠 + volume_dry | 57 | +2.73% / 56% | +1.53% / 55% | +1.20pp | mild amp |
| 5★+綠 + bullish_div | 32 | +2.27% / 69% | +0.38% / 52% | +1.88pp | AMPLIFIES |
| 5★+綠 + 爆量 | 30 | +1.84% / 60% | +0.75% / 53% | +1.10pp | AMPLIFIES |

## Key finding: 爆量 is a strong directional amplifier

The volume_burst (today's lots > 1.5× 20-day average AND > +2σ)
flag, when co-occurring with a chip, materially amplifies the chip's
edge in the same direction:

- **AVOID + 爆量**: 40d alpha drops to -6.4% with 42% win (n=38). A
  selling climax / failed breakout during an institutional non-
  confirmation appears to confirm the bearish setup.
- **4★+綠 + 爆量**: 40d alpha jumps to +4.8% with 69% win (n=29). Volume
  confirming a reversal-quality + 法人=綠 setup is the strongest
  positive combination we've found.
- **5★+綠 + 爆量**: +1.8% with 60% win (n=30). Smaller magnitude than
  4★+綠 because 5★+綠 alone is already smaller, but still amplifies.

The asymmetry — volume amplifies in whatever direction the chip points
— suggests volume bursts are a "conviction" filter, not a directional
predictor on their own.

## Secondary findings

- **bullish_div (頂背離) + AVOID** also amplifies the downside (-2.9pp).
  Counter-intuitive: bullish divergence in isolation usually predicts up,
  but during an AVOID it appears to be a trap signal (the divergence
  is false). 35% win rate is striking.
- **bullish_div + 5★+綠** amplifies upside by +1.9pp. The combination
  of overbought-bouncing-back divergence + reversal quality + 法人 is
  the highest-confidence bullish setup, even though 5★+綠 alone is
  modest (+1.0%).
- **volume_dry** mostly doesn't shift — the chip's direction is
  preserved regardless of low-volume environment.

## Production implications

Two candidate UI changes:

1. **Upgrade AVOID + 爆量 chip tone** from `warn` to `danger` when both
   fire on the same day. Make it visually distinguishable from the
   plain AVOID chip.
2. **Highlight 反轉 4★+綠 + 爆量 as the "high-conviction reversal"
   combo**. Could be a special chip badge or a column accent.

Both are small additions that surface findings the user wouldn't see
otherwise (the chips currently render side-by-side with no indication
of which combinations matter).

## Caveats

- Sample sizes vary: AVOID + 爆量 has n=38, which is small. The -6.4%
  median is striking but a 95% CI is wide.
- The bullish_div amplification of AVOID was unexpected — the pattern
  may be tech-stock specific.
- We only paired chips with single context flags. Three-way combos
  (AVOID + 爆量 + 頂背離) likely have even stronger effects but tiny n.
- Flag definitions match `_compute_alerts` thresholds; changing those
  thresholds would shift the bucket counts.
