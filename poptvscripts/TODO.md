# PineScript Evaluation Project — TODO

## Status: 10/42 scripts evaluated (Batch 1 complete)

---

## EXECUTION PLAN: How to Systematically Test Hypotheses

### Phase 0: Quick Wins — Existing Disabled Filters (1 session, ~30 min)
Both Apex and LastFive ALREADY have VIX term and VVIX filters built but disabled.
No code changes needed — just toggle config flags and run backtests.

```bash
# Apex — already has ENABLE_VIX_TERM_FILTER (VIX/VIX9D ratio, default off, threshold 1.2)
cd apex
python3 apex.py --marathon                                    # baseline
python3 apex.py --marathon --vix-term-max 1.05                # test H-VCP-1: tight contango gate
python3 apex.py --marathon --vix-term-max 1.10                # test H-VCP-1: moderate gate
python3 apex.py --marathon --vix-term-max 1.15                # test H-VCP-1: loose gate
python3 apex.py --marathon --vvix-max 140                     # test H-VHM-1: VVIX kill switch
python3 apex.py --marathon --vvix-max 130                     # test H-VHM-1: tighter VVIX
python3 apex.py --marathon --vix-term-max 1.05 --vvix-max 140 # combo
```

Record every result in `poptvscripts/hypothesis_results.csv`.

### Phase 1: New Filters to Build (~2-3 sessions)
Priority order — each takes ~15 min to implement, ~15 min to test:

| # | Filter | Strategy | Data | Hypothesis IDs | Effort |
|---|--------|----------|------|----------------|--------|
| 1 | **Williams VIX Fix** | Both | SPX OHLC (have it) | H-WVF-1 thru 10 | New function: ~20 lines |
| 2 | **VIX SMA(5) cross** | Both | VIX daily (have it) | H-VXC-1 thru 10 | ~10 lines in indicator builder |
| 3 | **VIX/16 expected range** | Both | VIX + SPX open (have it) | H-V16-1 thru 10 | ~15 lines |
| 4 | **VIX/16 remaining move** | LastFive | VIX + SPX at 15:55 (have it) | H-V16-4, H-V16-9 | Replace distance logic: ~20 lines |
| 5 | **Composite panic score** | Both | VIX9D + VVIX + WVF (have it) | H-VHM-9 | Combine filters 1-3: ~15 lines |

Implementation pattern for each:
1. Add `ENABLE_*_FILTER` + params to `*_config.py`
2. Compute indicator in `_build_daily_indicators()` or intraday section
3. Add filter check in the entry filter block
4. Add CLI flag
5. Run backtest, record result

### Phase 2: Hypothesis Testing Sweeps (~3-5 sessions)
For each new filter, run a parameter sweep across thresholds:

```bash
# Example: WVF filter sweep on Apex
for threshold in 1.0 1.5 2.0 2.5 3.0; do
    python3 apex.py --marathon --wvf-max-std $threshold
done

# Example: VIX SMA cross sweep on LastFive
for length in 3 5 7 10 14; do
    python3 lastfive.py --vix-sma-length $length
done

# Example: VIX16 remaining move sweep on LastFive
for multiplier in 1.0 1.5 2.0 2.5 3.0; do
    python3 lastfive.py --vix16-distance-mult $multiplier
done
```

Record ALL results in `hypothesis_results.csv` with columns:
`hypothesis_id, strategy, filter, parameter, value, trades, pnl, win_rate, sharpe, max_dd, skipped_days, notes`

### Phase 3: Combination Testing (~2-3 sessions)
Test the best-performing individual filters in combination:
- Pick top 3 filters per strategy from Phase 2
- Test all 2-way and 3-way combos
- Check for over-filtering (trade count must stay >60% of baseline)
- Validate with walk-forward: split data into IS (2022-2024) and OOS (2025-2026)

### Phase 4: Continue Script Evaluation (ongoing, 1 batch per session)
- Batch 2: Volume & Liquidity (8 scripts)
- Batch 3: Smart Money Concepts (5 scripts)
- Batch 4: ML & Advanced (5 scripts)
- Batch 5: Momentum & Mean Reversion (9 scripts)
- Batch 6: Structure & ORB (4 scripts)
- Each batch generates new hypotheses → feed into Phases 1-3

### Anti-Overfit Rules
- [ ] Never optimize on full dataset — always IS/OOS split
- [ ] Any filter that removes >40% of trades needs strong justification
- [ ] Prefer filters that improve Sharpe AND reduce DD over those that just boost P&L
- [ ] Walk-forward validation required before any filter goes into production
- [ ] Track "improvement vs. complexity" — if 5 filters give 10% better Sharpe, not worth it

### Session Workflow (copy-paste each session)
```
1. Check hypothesis_results.csv — what's been tested
2. Pick next 3-5 hypotheses to test
3. Implement filter if needed (or just toggle existing)
4. Run backtests, record results immediately
5. Update TODO.md with findings
6. If time remains, evaluate next batch of PineScripts
7. Update evaluation_log.csv, ideas_for_spx.csv, etc.
```

---

## Current Batch Queue

### Batch 1 (Session 1) — VIX-Based + Simple Indicators -- COMPLETE
- [x] CM_Williams_Vix_Fix Finds Market Bottoms — **VALUE: 5/5** synthetic VIX, no data needed
- [x] VIX Crossing — **VALUE: 4/5** VIX SMA(5) crossunder + trend confirmation
- [x] VIX MTF Momentum — **VALUE: 3/5** simple VIX momentum histogram
- [x] VIX Rule of 16 — **VALUE: 4/5** expected daily range from VIX
- [x] VIX Reversal Scalper by Trend Friend — **VALUE: 3/5** VIX MA cross + momentum
- [x] VIX Volatility Trend Analysis With Signals — **VALUE: 3/5** VIX trend cloud
- [x] VIX Option Hedge Monitor — **VALUE: 4/5** crash detector concept
- [x] VIX Curve Pro - Real-Time Term Structure with Statistics — **VALUE: 5/5** term structure regime
- [x] Hull Suite — **VALUE: 3/5** low-lag MA for trend
- [x] Ichimoku — **VALUE: 2/5** too slow for 0DTE, skip

### Batch 2 — Volume & Liquidity
- [ ] Volume Spread Analysis IQ [TradingIQ]
- [ ] Volume Bubbles [QuantAlgo]
- [ ] Volume Acceptance Zones [BigBeluga]
- [ ] Vol Cluster Zone
- [ ] OBV with Kalman Filter Improv [TechnicalZen]
- [ ] HTF Volume Spike & Imbalance Projection [LuxAlgo]
- [ ] Liquidity Hunter [Flow Dominance + Pending Breaks]
- [ ] Liquidity Thermal Map [BigBeluga]

### Batch 3 — Smart Money Concepts
- [ ] Smart Money Concepts (SMC) [LuxAlgo]
- [ ] Smart Money Structure Decoder [JOAT]
- [ ] SMC HTF Liquidity -> 15m FVG -> 1m CHOCH -> 1m FVG OTE
- [ ] Fair Value Gap Profile + Rolling POC [BigBeluga]
- [ ] Bastion Level Sentinel [JOAT]

### Batch 4 — ML & Advanced
- [ ] Machine Learning: Lorentzian Classification
- [ ] Machine Learning Pivot Points (KNN) [SS]
- [ ] Monte Carlo CT [SS]
- [ ] Market Microstructure Analytics
- [ ] Fractal Velocity Accelerator [JOAT]

### Batch 5 — Momentum, Trend & Mean Reversion
- [ ] RSI Elite Toolkit [Clever]
- [ ] Inertial RSI [LuxAlgo]
- [ ] %R Trend Exhaustion [upslidedown]
- [ ] SuperTrendy
- [ ] Laguerre Multi-Filter [DW]
- [ ] JTrader EMA Zone Inversion - Open Source v1
- [ ] Denial [MMT]
- [ ] Koncorde Plus
- [ ] TASC 2026.04 A Synthetic Oscillator

### Batch 6 — Structure & ORB
- [ ] Swing Structure Forecast [BOSWaves]
- [ ] HTF Candle Direction Strategy V1
- [ ] Kanes Indices ORB
- [ ] Ultimate Trader's Toolbox - Top 20

### Skip / Low Priority
- [ ] Watermark V6.2 (utility script, not a strategy)

## High Priority Action Items (from Batch 1)

### Apex Specific
- [ ] **P1:** Test VIX16 adaptive distance: `d = 2 * SPX*(VIX/15.87/100)*sqrt(5/390)` — replaces fixed d=12
- [ ] **P1:** Test VIX9D/VIX > 1.05 as skip filter (term structure gate)
- [ ] **P1:** Test VVIX > 140 as emergency halt
- [ ] **P2:** Test side selection by VIX16 range position (>70% = call-only, <30% = put-only)
- [ ] **P2:** Test WVF intraday check as additional filter (catches today's panic, not just yesterday's)

### LastFive Specific
- [ ] **P1:** Test VIX9D instead of VIX for adaptive distance (9-day vol more relevant for 0DTE)
- [ ] **P1:** Test VIX16 continuous distance formula: `dist = ceil(1.5 * expected_5min_move / 5) * 5`
- [ ] **P1:** INVESTIGATE: LastFive d=0 (ATM) at VIX<18 — VIX/16 math says expected 5-min move is ~6pts, ATM w/5pt spread may be too aggressive
- [ ] **P1:** Test contango-only mode: skip when VIX9D/VIX > 1.0, half-size when > 0.95
- [ ] **P1:** Test WVF put guard: skip PUT side when WVF > percentile(85)
- [ ] **P2:** Test VIX-scaled protective buyback trigger instead of fixed $2

### Shared (Both Apex + LastFive)
- [ ] **P1:** Build universal 15:50 pre-trade check module (contango + VVIX + WVF + range position)

### General
- [ ] **P1:** Compute VIX9D/VIX ratio daily and test as contango/backwardation regime filter on ALL strategies
- [ ] **P1:** Build Williams VIX Fix in Python and test as capitulation filter
- [ ] **P1:** Implement VIX/16 expected daily range for strike selection across all strategies
- [ ] **P1:** Test VIX SMA(5) crossunder as universal entry filter
- [ ] **P1:** Build VVIX kill switch (VVIX > 140 = halt trading)
- [ ] **P2:** Test HMA as EMA replacement in MEDS-style strategies
- [ ] **P2:** Build VIX16 Boundary Fade strategy (sell spreads at VIX-implied range boundaries)
- [ ] **P3:** Evaluate ORB breakout levels as 0DTE entry triggers (Batch 6)
- [ ] **P3:** Check if Williams %R exhaustion concept overlaps with Zenith (Batch 5)

## Ideas to Backtest (from Batch 1)
- [ ] VIX Fix Reversal — call entries when synthetic VIX spikes then recedes
- [ ] VIX16 Boundary Fade — credit spreads at VIX-implied daily boundaries
- [ ] Term Structure Regime Switch — credit spreads only in contango
- [ ] VVIX Crash Detector — halt all strategies when VVIX spikes
- [ ] HMA Trend Filter — replace EMA with Hull MA for trend determination

## Creative Hypotheses to Test (10 per script, Batch 1)

### CM_Williams_Vix_Fix (Synthetic VIX from Price)
1. **H-WVF-1:** WVF > upper BB at 15:50 predicts SPX will move >10pts in last 10 min (danger for Apex/LastFive)
2. **H-WVF-2:** Days where WVF spikes then recedes by 14:00 are the BEST days for afternoon credit spreads (fear exhaustion = theta harvest)
3. **H-WVF-3:** WVF percentile rank > 90 on daily bars predicts next-day SPX reversal with >60% accuracy — call debit spread signal
4. **H-WVF-4:** WVF computed on 5-min SPX bars diverging from actual VIX indicates "hidden stress" not yet priced into options
5. **H-WVF-5:** WVF crossing below upper BB after a spike is a stronger entry signal than VIX SMA crossunder (because it's real-time, no data lag)
6. **H-WVF-6:** Combining WVF < median AND VIX < SMA(5) creates a "double calm" filter that eliminates >80% of losing trades across all strategies
7. **H-WVF-7:** WVF on weekly timeframe > upper BB predicts multi-day selloff — skip ALL 0DTE strategies for entire week
8. **H-WVF-8:** The RATE OF CHANGE of WVF (not just level) is more predictive — fast WVF spikes are mean-reverting, slow WVF rises are trend-following
9. **H-WVF-9:** WVF works as a PUT-side-only filter: high WVF = skip puts but calls are still safe (because puts blow up on selloffs, calls don't)
10. **H-WVF-10:** WVF computed on SPX is more accurate for SPX options than actual VIX (which includes out-of-money option skew noise)

### VIX Crossing (VIX SMA Cross + Trend Confirmation)
1. **H-VXC-1:** VIX < SMA(5) at 9:30 predicts the day will close higher than open with >58% accuracy
2. **H-VXC-2:** The NUMBER of consecutive days VIX < SMA(5) predicts credit spread profitability (more consecutive = safer, but with diminishing returns after 10 days)
3. **H-VXC-3:** VIX crossing ABOVE SMA(5) during market hours (not just daily close) is a real-time kill switch — test on 15-min VIX bars
4. **H-VXC-4:** Combining VIX < SMA(5) with SPX > EMA(21) as a dual filter reduces LastFive DD by >30% with <15% trade reduction
5. **H-VXC-5:** The DISTANCE of VIX below its SMA(5) (not just the cross) correlates with optimal credit spread width — wider distance = wider safe spread
6. **H-VXC-6:** VIX SMA(3) crossunder is better than SMA(5) for 0DTE because faster response catches intraday regime shifts
7. **H-VXC-7:** The day VIX crosses under SMA(5) is actually the WORST day to enter (whipsaw risk) — enter the DAY AFTER the cross
8. **H-VXC-8:** VIX < SMA(5) on a Monday after a Friday where VIX > SMA(5) = weekend fear resolved = highest-probability day for credit spreads
9. **H-VXC-9:** VIX SMA(5) cross combined with VIX absolute level creates quadrants: {low VIX + falling} = max size, {high VIX + falling} = normal size, {low VIX + rising} = caution, {high VIX + rising} = skip
10. **H-VXC-10:** Replace SMA(5) with EMA(5) or HMA(5) for fewer false crosses — test which MA type produces the cleanest filter for credit spreads

### VIX MTF Momentum (VIX Rate of Change)
1. **H-VMM-1:** VIX momentum(7) < -2 (sharp fear decline) is the single best one-variable filter for put credit spread profitability
2. **H-VMM-2:** VIX momentum making a NEW LOW in 25-bar lookback = complacency extreme — actually DANGEROUS for premium sellers (vol about to spike)
3. **H-VMM-3:** VIX momentum crossing zero from positive to negative is a regime change signal — next 5 trading days have >65% bullish bias
4. **H-VMM-4:** VIX momentum on 1-hour timeframe at 15:00 predicts last-hour SPX direction better than daily VIX momentum
5. **H-VMM-5:** Absolute VIX momentum > 5 (either direction) predicts high SPX movement — avoid ALL credit spreads on these days
6. **H-VMM-6:** VIX momentum combined with VVIX momentum creates a 2D regime map — only trade in the {VIX falling, VVIX falling} quadrant
7. **H-VMM-7:** The ACCELERATION of VIX (momentum of momentum) catches turning points earlier — VIX accel going negative while VIX is still rising = early safe signal
8. **H-VMM-8:** VIX momentum(7) as a continuous sizing multiplier: more negative = larger position. Zero = minimum size. Positive = skip.
9. **H-VMM-9:** VIX momentum divergence from SPX momentum (VIX falling but SPX also falling) = hidden risk — skip these days
10. **H-VMM-10:** VIX momentum at 3:50 PM specifically predicts last-10-minute SPX behavior for Apex/LastFive entries

### VIX Rule of 16 (Expected Daily Range)
1. **H-V16-1:** Days where SPX stays within 70% of VIX/16 expected range by 15:00 are the highest-probability days for 15:55 credit spreads
2. **H-V16-2:** SPX breaching 100% of VIX/16 expected range before 14:00 predicts continued momentum (NOT mean reversion) — skip credit spreads
3. **H-V16-3:** The RATIO of actual daily range to VIX-implied range over rolling 5 days predicts next-day realized vol better than VIX itself
4. **H-V16-4:** Setting LastFive short strike at 1.5x the VIX/16 remaining-move (scaled by sqrt(time_remaining/390)) produces better risk-adjusted returns than fixed distance buckets
5. **H-V16-5:** On days where VIX/16 implied range < 0.7% of SPX, credit spreads have >95% win rate — these are the "free money" days
6. **H-V16-6:** VIX/16 range computed from VIX OPEN vs VIX at 15:50 — if VIX has risen intraday, the MORNING range estimate is stale and underestimates risk
7. **H-V16-7:** The VIX/16 range can be used to set dynamic stop losses: if SPX moves beyond 80% of remaining expected range toward your strike, buy back
8. **H-V16-8:** Comparing VIX/16 range to ACTUAL 5-day realized range creates an IV/RV ratio — when IV >> RV (range overestimation), premium selling is juiciest
9. **H-V16-9:** VIX/16 range at open PLUS afternoon momentum creates a "remaining budget" metric — how much of the expected range has been used. >90% used = trade, <50% used = too much room left for movement
10. **H-V16-10:** Using VIX1D instead of VIX in the rule-of-16 formula is more accurate for 0DTE because VIX1D captures same-day implied vol specifically

### VIX Reversal Scalper (VIX MA Cross + Momentum Confirmation)
1. **H-VRS-1:** Adding momentum confirmation (SPX mom(4) > SPX mom(4)[1]) to VIX SMA cross filter reduces false signals by >40%
2. **H-VRS-2:** The HMA variant of VIX MA crossunder produces fewer whipsaws than EMA or SMA — test all 6 MA types as filters
3. **H-VRS-3:** VIX reversals confirmed by positive SPX momentum are 2x more reliable than VIX reversals without momentum confirmation
4. **H-VRS-4:** Short VIX MA length (5-10) is better for 0DTE timing; long VIX MA (50-100) is better for regime filtering — use BOTH simultaneously
5. **H-VRS-5:** Momentum confirmation on SPX 5-min bars at 15:50 (not daily) can time Apex/LastFive entries more precisely
6. **H-VRS-6:** The INVERSE signal (VIX crossing ABOVE MA + negative momentum) predicts high-risk days with >70% accuracy — use as skip filter
7. **H-VRS-7:** VIX RMA(10) crossunder produces the smoothest signal for daily regime detection — fewer reversals than EMA(10)
8. **H-VRS-8:** Momentum acceleration (mom > mom[1] AND mom[1] > mom[2]) is a stronger confirmation than single momentum check
9. **H-VRS-9:** VIX reversal + momentum signal at 10:00 AM predicts whether the rest-of-day will be trending or mean-reverting
10. **H-VRS-10:** Combining VIX reversal with VOLUME confirmation (high volume on reversal = more reliable) could work if we have SPX tick volume

### VIX Volatility Trend Analysis (VIX Trend Cloud)
1. **H-VTA-1:** Days where smoothed VIX has been below VIX MA for >5 consecutive days are the safest for maximum position sizing
2. **H-VTA-2:** The DISTANCE between smoothed VIX and VIX MA (cloud width) correlates with credit spread profitability — wider cloud = more confident
3. **H-VTA-3:** VIX cloud color change (green → red) is a 1-day-early warning before VIX SMA(5) cross — use as a pre-filter to reduce position size
4. **H-VTA-4:** The VIX 20 baseline (line 109 in script) combined with cloud direction creates 4 regimes: {below 20, green cloud} = aggressive, {above 20, red cloud} = skip
5. **H-VTA-5:** Smoothing VIX with RMA(3) before applying any other VIX filter reduces noise and improves all VIX-based filter accuracy
6. **H-VTA-6:** VIX trend cloud computed on VIX9D instead of VIX is more responsive and better suited for 0DTE decisions
7. **H-VTA-7:** The cloud flip (VIX trend changing from bullish to bearish for stocks) within the last 2 hours predicts bad 15:55 entries
8. **H-VTA-8:** When VIX cloud is green but VVIX cloud is red = divergence = hidden danger. VVIX trend should confirm VIX trend.
9. **H-VTA-9:** Using 3 VIX MAs (fast/medium/slow: 3/10/30) creates a traffic light: all aligned = green, mixed = yellow, all bearish = red
10. **H-VTA-10:** VIX trend persistence (how long has it been green?) is more predictive than the cross event itself — test duration as a continuous variable

### VIX Option Hedge Monitor (Crash Detection)
1. **H-VHM-1:** VVIX > 130 at any point during the trading day predicts SPX will move >1.5% — skip all credit spread strategies
2. **H-VHM-2:** VVIX rate of change > 20% in a single day is a 2-day forward crash predictor — skip today AND tomorrow
3. **H-VHM-3:** VVIX/VIX ratio > 6 indicates "vol of vol is extreme relative to vol" = imminent move — the direction is unknown but magnitude will be large
4. **H-VHM-4:** VVIX spike + VIX9D/VIX > 1.0 (backwardation) together = "maximum danger" — should have caught 100% of tail events in backtest period
5. **H-VHM-5:** The 200% and 400% thresholds from the script translate to VVIX thresholds — calibrate what VVIX level corresponds to 200%+ VIX option moves
6. **H-VHM-6:** VVIX mean reversion after a spike (VVIX drops from >130 to <110) = all-clear signal for aggressive re-entry with larger size
7. **H-VHM-7:** Tracking VVIX percentile rank over 252 days — when > 95th percentile, the next day has elevated tail risk even if VVIX drops
8. **H-VHM-8:** VVIX rising while VIX is flat = "quiet fear" — options market pricing a move that hasn't happened yet. Most dangerous regime.
9. **H-VHM-9:** Building a composite panic score: `panic = 0.4*VVIX_pctile + 0.3*VIX9D_VIX_ratio + 0.3*WVF_pctile` as a universal risk meter
10. **H-VHM-10:** The time-of-day when VVIX spikes matters — morning VVIX spikes that recede by 14:00 are false alarms, but 14:00+ VVIX spikes persist into close

### VIX Curve Pro (Term Structure / Regime)
1. **H-VCP-1:** VIX9D/VIX < 0.85 (deep contango) identifies the top 20% safest days for credit spreads — test if win rate exceeds 95%
2. **H-VCP-2:** VIX9D/VIX ratio CHANGE (today vs yesterday) is more predictive than the level — a rising ratio (toward backwardation) is a warning even if still < 1.0
3. **H-VCP-3:** VIX9D/VIX > 1.0 for 2+ consecutive days = sustained fear, not a spike — different trading rule than single-day inversion
4. **H-VCP-4:** The percentile rank of VIX9D/VIX ratio over 252 days as a continuous sizing multiplier: low percentile = full size, high percentile = scaled down
5. **H-VCP-5:** VIX9D/VIX combined with VIX absolute level creates a 2x2 matrix: {low VIX, contango} = paradise, {high VIX, backwardation} = death, the two mixed states need different handling
6. **H-VCP-6:** The SPEED of transition from contango to backwardation predicts severity — fast transitions (within 1 day) = crash, slow transitions (over 5 days) = manageable correction
7. **H-VCP-7:** After a backwardation episode ends (ratio drops back below 1.0), the first 3 days of resumed contango are the highest-premium days — enter aggressively
8. **H-VCP-8:** VIX1D/VIX9D ratio (ultra-short-term structure) may be even better than VIX9D/VIX for 0DTE decisions — captures same-day fear specifically
9. **H-VCP-9:** Term structure regime can replace the afternoon return filter in LastFive — backwardation already captures "bad afternoon" days more systematically
10. **H-VCP-10:** Building a "term structure slope" metric (weighted average of all available ratios) as a single continuous number for portfolio-level risk management

### Hull Suite (Low-Lag Moving Average)
1. **H-HMA-1:** HMA(20) on 5-min SPX bars at 15:50 predicts last-10-minute SPX direction with >55% accuracy (better than EMA due to lower lag)
2. **H-HMA-2:** HMA crossover (MHULL > SHULL) on 1-min SPX bars at 15:54 can time Apex/LastFive side selection in real-time
3. **H-HMA-3:** HMA trend on 15-min bars as a replacement for the prior-day return filter in Apex — catches intraday trend rather than overnight
4. **H-HMA-4:** The HMA BAND width (distance between MHULL and SHULL) measures volatility — narrow band at 15:50 = low vol = safe for credit spreads
5. **H-HMA-5:** HMA(55) on daily SPX as a trend filter: only sell puts when above HMA(55), only sell calls when below
6. **H-HMA-6:** THMA (triple Hull) is smoother than HMA — test if THMA produces fewer false signals at the cost of slightly more lag
7. **H-HMA-7:** HMA computed on SPX returns (not price) captures momentum direction more cleanly for options directional decisions
8. **H-HMA-8:** HMA/EMA divergence (HMA says up, EMA says down) = indecision = avoid trading. Only trade when both agree.
9. **H-HMA-9:** The SLOPE of HMA (not just direction) as a continuous variable for position sizing — steep slope = strong trend = larger directional bet
10. **H-HMA-10:** HMA on VIX instead of SPX — HMA(10) on VIX produces smoother regime detection than SMA(5) crossunder

### Ichimoku (Cloud System)
1. **H-ICH-1:** SPX above Ichimoku cloud on WEEKLY timeframe = macro bullish — only sell puts (never calls) during these weeks
2. **H-ICH-2:** Tenkan/Kijun cross on daily SPX as a regime switch: bullish cross = put credit spread weeks, bearish cross = call credit spread weeks
3. **H-ICH-3:** The Ichimoku cloud THICKNESS on daily SPX correlates with support/resistance strength — thick cloud = strong support = safe put spreads
4. **H-ICH-4:** Chikou Span (lagging line) above price and cloud = triple bullish confirmation — these days have the highest put credit spread win rates
5. **H-ICH-5:** Kumo twist (future cloud color change) is a LEADING indicator — when future cloud turns bearish, reduce credit spread sizing 5 days ahead
6. **H-ICH-6:** Price inside the cloud = indecision zone — skip ALL credit spreads when SPX is within the daily Ichimoku cloud
7. **H-ICH-7:** Ichimoku applied to VIX chart instead of SPX — VIX below cloud = stable fear environment = safe to sell premium
8. **H-ICH-8:** The Kijun line (26-period midpoint) as a daily support/resistance for SPX put spread strike selection — set short strike below Kijun
9. **H-ICH-9:** Combine Ichimoku weekly regime with 0DTE intraday signals: only trade intraday strategies in the direction the weekly Ichimoku supports
10. **H-ICH-10:** Ichimoku cloud on VIX9D — if VIX9D is below its Ichimoku cloud, short-term fear is contained, 0DTE selling is safe

## Completed
- [x] Batch 1: 10 VIX-Based + Simple Indicator scripts evaluated (2026-04-04)
