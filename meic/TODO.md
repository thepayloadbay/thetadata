# MEIC TODO

## Completed
- [x] Phase 1: Baseline engine (both_sides, 50-wide, $1.20 credit) → -$13k (unprofitable)
- [x] Phase 2: Direction sweep → always_put wins ($85k Sharpe 0.86)
- [x] Phase 3: Entry time sweep → 14:00-15:30 @5min ($97k Sharpe 2.38)
- [x] Phase 4: Per-pos SL sweep → -$500 optimal (plateau -$300 to -$600)
- [x] Phase 5: Daily SL sweep → never fires, keep -$3000 safety
- [x] Improvement 1: FOMC Skip → Sharpe 2.38→2.80, DD -$20k→-$16.5k
- [x] Improvement 2: Per-pos SL -$400 → DD further protected
- [x] Improvement 3: Late SL -$300 @15:15 → Sharpe 2.96, DD -$14.7k
- [x] R1: VIX 30-35 skip → Sharpe 2.96→3.75, DD unchanged
- [x] R2: Open MTM gate -$300 → DD -$14.7k→-$11.8k, Sharpe 3.77
- [x] R3: Kelly sizing VIX 15-20 → qty 3 → P&L +$5k, DD -$10.3k, Sharpe 3.68
- [x] R4: Dynamic width 60 @VIX 25+ → P&L $101k, Sharpe 3.72
- [x] R5: Momentum filter → REJECTED (flat, per-pos SL handles bad entries)
- [x] R6: EMA VIX gate for VIX 20-25 → Sharpe 3.72→4.00, filters weak zone
- [x] R7: Late qty reduction → REJECTED (costs -$19k P&L)
- [x] R8: Econ event upsizing → REJECTED (adds variance, hurts Sharpe)
- [x] R9: Adaptive OTM by time → DD -$10.3k→-$9.2k, Sharpe 3.92
- [x] R10: Streak acceleration → REJECTED (no intraday effect, trades settle EOD)

## Final Optimized (R6+R9 stack, 10 total improvements)
**$85,905 P&L | DD -$9,236 | Sharpe 3.92 | Sortino 3.78 | WR 70.6% | 1,109 trades | Calmar 2.55**

### Full Improvement Stack
| Step | Change | P&L | DD | Sharpe |
|------|--------|-----|----|--------|
| Raw baseline | both_sides | -$13,468 | -$77,860 | -0.19 |
| 1. always_put | Direction | $84,764 | -$47,762 | 0.86 |
| 2. Entry 14:00-15:30 @5min | Timing | $97,108 | -$20,148 | 2.38 |
| 3. FOMC skip | Calendar | $97,060 | -$16,502 | 2.80 |
| 4. SL -$400 + Late SL | Risk | $97,896 | -$14,736 | 2.96 |
| 5. VIX 30-35 skip | Zone filter | $99,874 | -$14,736 | 3.75 |
| 6. MTM gate -$300 | Entry gate | $94,468 | -$11,784 | 3.77 |
| 7. Kelly VIX 15-20 qty=3 | Sizing | $99,142 | -$10,251 | 3.68 |
| 8. Dynamic width 60 @VIX>25 | Width | $101,428 | -$10,797 | 3.72 |
| 9. EMA VIX gate 20-25 | Signal filter | $93,508 | -$10,315 | 4.00 |
| **10. Adaptive OTM by time** | **Risk** | **$85,905** | **-$9,236** | **3.92** |

## Loss Mitigation Research (L1-L5)
- [x] L1: Thursday skip → +$8.8k P&L, Sharpe 2.80→4.01. Thu 55% WR only losing DOW. ADOPTED.
- [x] L2: Canary entry → DD -$9.5k→-$5.1k, Sharpe→4.41. Cuts trades 45% but DD drops 46%. ADOPTED.
- [x] L3: Daily loss cap -$2000 → REJECTED. No effect (canary stops entries before cumulative loss).
- [x] L4: Morning range gate 2% → DD -$5.1k→-$3.7k, Sharpe 4.44. ADOPTED (conservative).
- [x] L5: Post-loss cooldown → +$1k, DD -$5.1k→-$4.4k. ADOPTED.

### Best Loss Mitigation Stack: L1+L2+L5
**$54,702 P&L | DD -$4,442 | Sharpe 4.54 | ~520 trades**

### Max Risk-Adjusted (Option A): L1+L2+L5 + 15m interval + SL -350 + credit $1.30 + morning gate 1.9%
**$22,903 P&L | DD -$1,505 | Sharpe 6.70 | Sortino 8.66 | PF 2.93 | WR 83.9% | 192 trades**

### Key Finding: min credit $1.30 (from $1.20)
Plateau $1.25-$1.35 all Sharpe >5.67. Filters out low-quality entries near breakeven.

## Tested & Rejected
- both_sides: -$13k. CALL side destroys PUT profit
- always_call: -$79k. Catastrophic
- EMA direction: -$1.5k. Structural PUT bias dominates
- Min credit $1.50: kills P&L
- Entry end 15:00/15:05: costs P&L
- Econ event skip: costs -$25k
- Momentum filter: flat (per-pos SL handles)
- Min OTM 40/50: kills P&L
- Kelly B (VIX 20-25 downsize): Sharpe drops
- Late qty reduction: -$19k P&L cost
- Econ upsizing: adds variance, hurts Sharpe
- Streak acceleration: no intraday effect
- Late credit floor: hurts DD
- EMA gate wider (18-25): costs P&L
- EMA gate all VIX: kills trade count
- Interval 3m: more DD for modest P&L gain
- Width 40: worse on all metrics

## Future Research
- [ ] EOQ days negative (-$1.5k). Test skip or downsize
- [ ] VIX 20-25 further investigation: EMA gate helps but zone still weak
- [ ] Live trading infrastructure on TradeStation
