# PineScript Research & Evaluation Log

## Batch 1: VIX-Based + Simple Indicators (2026-04-04)

---

### 1. CM_Williams_Vix_Fix Finds Market Bottoms
**Category:** VIX-Based / Mean Reversion  
**Complexity:** 2/5 | **Potential Value:** 5/5  
**Look-Ahead Bias:** NONE — uses `highest(close, pd)` and `low` from current/past bars only  
**Repaints:** NO  

**Core Logic:**  
Williams VIX Fix is a synthetic VIX calculated from price action: `wvf = ((highest(close, 22) - low) / highest(close, 22)) * 100`. This mimics VIX behavior without needing actual VIX data. It spikes when price drops sharply from recent highs. Signals fire when WVF exceeds its Bollinger Band upper band OR a percentile-based range high — indicating extreme fear/capitulation.

**SPX Options Applicability:** HIGH  
- **As Filter:** When WVF spikes above upper BB on SPX, it signals a capitulation bottom. This could FILTER OUT put credit spread entries (MEIC, LastFive, Pikes Peak) during the spike, and then signal re-entry when WVF recedes. Alternatively, it could trigger CALL entries on the reversal.
- **As Signal:** WVF spike + recede = buy signal. Could trigger long call or call debit spread entries on SPX.
- **New Strategy Idea:** "VIX Fix Reversal" — when WVF > upper BB then crosses back below, enter call credit spread or call debit spread. This is a bottom-fishing strategy that doesn't need actual VIX data — purely price-derived.

**Key Insight:** This is entirely price-derived (no external VIX feed needed). We can compute it from SPX OHLC data we already have. This is the single most promising script in Batch 1 for immediate implementation.

**Data Available:** YES — only needs SPX OHLC (we have this)

---

### 2. VIX Crossing
**Category:** VIX-Based / Momentum  
**Complexity:** 2/5 | **Potential Value:** 4/5  
**Look-Ahead Bias:** NONE — `ta.crossunder(vixClose, vixSma)` uses completed bar data  
**Repaints:** NO (uses close values from security calls with default settings)

**Core Logic:**  
Long entry when VIX crosses UNDER its 5-period SMA AND NDX is above its 21 EMA. Exit on VIX crossing back OVER SMA, or after 10 bars, or TP $3k / SL $1.5k per contract. This is a VIX mean-reversion + trend confirmation strategy.

**SPX Options Applicability:** HIGH  
- **As Filter:** VIX < SMA(5) = "fear subsiding" — excellent filter for any bullish SPX strategy. When VIX is falling, put credit spreads are safer.
- **As Signal:** VIX crossunder SMA + SPX above EMA(21) = enter put credit spread (MEIC-style) or call debit spread.
- **New Strategy Idea:** Adapt to 0DTE: when daily VIX crosses under SMA(5) at open, sell put credit spreads on SPX. Close at 15:55 or on TP/SL.

**Key Insight:** The dual-confirmation (VIX falling + index trending up) is a clean, bias-free signal. We should test VIX SMA(5) crossunder as a filter on ALL our existing strategies.

**Data Available:** PARTIAL — we have daily VIX close. Intraday VIX would make this more powerful for 0DTE timing.

---

### 3. VIX MTF Momentum
**Category:** VIX-Based / Momentum  
**Complexity:** 1/5 | **Potential Value:** 3/5  
**Look-Ahead Bias:** NONE  
**Repaints:** NO

**Core Logic:**  
Simple VIX momentum: `mom(vix, 7)` — the 7-period rate of change of VIX. Plots as histogram. Green when VIX momentum is negative (fear declining), red when positive (fear rising). Arrow signals when momentum hits highest/lowest in lookback window.

**SPX Options Applicability:** MEDIUM  
- **As Filter:** VIX momentum < 0 = favorable for selling premium. VIX momentum > 0 = caution.
- **Novel Concept:** The "new high in VIX momentum" signal (arrow down) could be a regime change detector — avoid all credit spread strategies when VIX momentum makes a new high.

**Data Available:** YES — daily VIX close is sufficient

---

### 4. VIX Rule of 16
**Category:** VIX-Based / Volatility Range  
**Complexity:** 2/5 | **Potential Value:** 4/5  
**Look-Ahead Bias:** POTENTIAL — uses `request.security(vixIndex, timeframe.period, open)` with default lookahead. The `open` value is fetched at bar open which is fine for daily, but be careful on intraday.  
**Repaints:** MINIMAL — uses open price which is known at bar start

**Core Logic:**  
The "Rule of 16" converts VIX into expected daily range: `daily_range = price * (VIX / 15.87) / 100`. This gives you the expected high/low for the day based on implied volatility. Lines are drawn at open +/- expected range.

**SPX Options Applicability:** VERY HIGH  
- **As Filter:** If SPX has already moved beyond the expected daily range (VIX-implied), it's likely overextended — avoid entering new positions or enter mean-reversion trades.
- **As Signal:** When SPX approaches but doesn't breach the expected range, that's a high-probability boundary for selling premium.
- **New Strategy Idea:** "VIX16 Boundary Fade" — at market open, compute VIX-implied daily range. If SPX hits the upper boundary, sell call credit spreads. If it hits lower boundary, sell put credit spreads. This is essentially a quantitative version of our Zenith exhaustion concept.
- **For Strike Selection:** Use VIX/16 range to set credit spread short strike beyond the expected range. This gives you a statistically-grounded strike selection method.

**Key Insight:** VIX/16 gives us a daily expected move that we can compute from daily VIX open. We should compute this every morning and use it across ALL strategies for strike selection and as a range filter.

**Data Available:** YES — daily VIX close/open (we have this)

---

### 5. VIX Reversal Scalper by Trend Friend
**Category:** VIX-Based / Mean Reversion  
**Complexity:** 2/5 | **Potential Value:** 3/5  
**Look-Ahead Bias:** NONE  
**Repaints:** NO

**Core Logic:**  
Signals when VIX crosses under its MA (bullish for stocks) combined with price momentum confirmation (`mom > mom[1]`). Uses configurable MA type (EMA, SMA, HMA, etc.) and length. The momentum filter ensures you're not just getting a VIX cross but also that the underlying asset has turning momentum.

**SPX Options Applicability:** MEDIUM  
- **As Filter:** The VIX MA crossunder + momentum confirmation is a stronger version of plain VIX crossing. Could filter entries for MEIC/LastFive.
- **Novel Addition:** The momentum confirmation layer on top of VIX cross is worth testing — does adding `SPX mom(4) > SPX mom(4)[1]` improve our VIX-based filters?

**Data Available:** YES — daily VIX + SPX OHLC

---

### 6. VIX Volatility Trend Analysis With Signals
**Category:** VIX-Based / Trend  
**Complexity:** 2/5 | **Potential Value:** 3/5  
**Look-Ahead Bias:** NONE — uses `barmerge.gaps_on` which is appropriate  
**Repaints:** NO

**Core Logic:**  
Smoothed VIX (RMA of 3) vs VIX MA (RMA of 10). When smoothed VIX crosses under MA = bullish for stocks. Cloud fill shows trend state. This is essentially a VIX trend-following indicator.

**SPX Options Applicability:** MEDIUM  
- **As Filter:** VIX in downtrend (smoothed < MA) = favorable for selling premium. Use as regime filter.
- **Overlaps with:** VIX Crossing and VIX Reversal Scalper — all are VIX MA crossover variants.

**Data Available:** YES — daily VIX close

---

### 7. VIX Option Hedge Monitor
**Category:** VIX-Based / Sentiment  
**Complexity:** 2/5 | **Potential Value:** 4/5  
**Look-Ahead Bias:** YES — `lookahead=barmerge.lookahead_on` on line 31 for `option_open`. This is acceptable in real-time monitoring but would be biased in a backtest.  
**Repaints:** YES (due to lookahead on daily open)

**Core Logic:**  
Monitors specific VIX option contracts for extreme intraday price spikes (200%+ = "increased hedging", 400%+ = "crash imminent"). The idea: when VIX calls spike 200-400%+ from their daily open, someone is buying massive crash protection.

**SPX Options Applicability:** HIGH (as a warning system, not a signal)  
- **As Filter:** This is a "circuit breaker" concept. If VIX calls are spiking 200%+, HALT all credit spread entries. This directly addresses our MEDS tail risk problem — VIX option spike detection could have warned us before the 100pt SPX spike that killed MEDS.
- **Implementation:** We have VIX option data? If not, we could proxy with VIX1D or VVIX spikes.
- **New Idea:** Build a "hedge panic detector" using VIX option IV or VVIX as a kill switch for all strategies.

**Key Insight:** The concept of monitoring VIX OPTION behavior (not just VIX level) is novel for us. Option-on-option sentiment is a leading indicator.

**Data Available:** PARTIAL — we have VVIX which is a proxy. Don't have individual VIX option quotes.

---

### 8. VIX Curve Pro - Real-Time Term Structure with Statistics
**Category:** VIX-Based / Term Structure  
**Complexity:** 3/5 | **Potential Value:** 5/5  
**Look-Ahead Bias:** NONE — explicitly uses `lookahead=barmerge.lookahead_off`  
**Repaints:** NO

**Core Logic:**  
Builds the full VIX term structure curve: VIX9D, VIX (30D), VIX3M, VIX6M, VIX1Y. Computes ratios (9D/VIX, VIX/3M, VIX/6M), percentile ranks, and determines regime (Contango/Backwardation/Mixed). Contango = sell vol; Backwardation = buy vol.

**SPX Options Applicability:** VERY HIGH  
- **As Filter:** Contango regime = safe to sell premium (all our credit spread strategies). Backwardation = halt or switch to debit spreads / long vol.
- **Ratio Signals:**
  - VIX9D/VIX > 1.0 = short-term fear exceeding 30-day → AVOID credit spreads
  - VIX/VIX3M > 0.95 = near inversion → reduce position size
  - Percentile rank of VIX9D > 90% = extreme short-term fear
- **New Strategy Idea:** "Term Structure Regime Switch" — only trade credit spreads when VIX curve is in contango. Switch to debit spreads or sit out during backwardation. This is the most sophisticated volatility filter possible.
- **For Our Data:** We have VIX, VVIX, VIX1D, VIX9D. We're missing VIX3M, VIX6M, VIX1Y — but VIX9D/VIX ratio alone is powerful.

**Key Insight:** Term structure is the single most important vol signal we're not using. VIX9D/VIX ratio can be computed TODAY with our existing data. This should be Priority 1 for implementation.

**Data Available:** PARTIAL — have VIX, VIX1D, VIX9D. Missing VIX3M, VIX6M, VIX1Y. The VIX9D/VIX ratio is immediately actionable.

---

### 9. Hull Suite
**Category:** Momentum / Trend  
**Complexity:** 2/5 | **Potential Value:** 3/5  
**Look-Ahead Bias:** NONE  
**Repaints:** NO — uses `HULL[0]` vs `HULL[2]` (current vs 2 bars ago)

**Core Logic:**  
Hull Moving Average (HMA) — a fast, low-lag moving average. HMA = WMA(2*WMA(src, n/2) - WMA(src, n), sqrt(n)). Also includes EHMA (EMA-based) and THMA (triple-weighted) variants. Trend determined by comparing current HMA to HMA 2 bars ago.

**SPX Options Applicability:** MEDIUM  
- **As Filter:** HMA trend direction on SPX daily could filter credit spread direction — only sell puts when HMA trending up, only sell calls when trending down.
- **Novel:** The HMA's low-lag property is useful for 0DTE intraday signals — less whipsaw than SMA/EMA. Could replace EMA in our MEDS-style strategies.
- **Caution:** Length 55 is designed for swing trading. For 0DTE, would need shorter period (10-20 on 1-5min bars).

**Data Available:** YES — SPX OHLC

---

### 10. Ichimoku
**Category:** Momentum / Trend / Structure  
**Complexity:** 3/5 | **Potential Value:** 2/5  
**Look-Ahead Bias:** MINIMAL — Senkou Span B uses `[basePeriods]` lookback which is standard. Chikou Span is plotted with negative displacement (lagging indicator, not forward-looking).  
**Repaints:** NO

**Core Logic:**  
Classic Ichimoku Cloud: Tenkan (9-period mid), Kijun (26-period mid), Chikou (close displaced back 26), Senkou A/B (cloud). Bullish when price above cloud, Tenkan > Kijun.

**SPX Options Applicability:** LOW  
- **As Filter:** Price above cloud = bullish regime. This is too slow for 0DTE trading.
- **Lesson:** Ichimoku is a daily/weekly timeframe tool. Not useful for intraday 0DTE unless heavily modified. Skip for our purposes.

**Data Available:** YES — SPX OHLC

---

## Batch 1 Summary

### Top Findings (Priority Order)
1. **VIX Curve Pro (Term Structure)** — VIX9D/VIX ratio as regime filter. Highest priority. We have the data.
2. **CM Williams VIX Fix** — Synthetic VIX from price only. No external data needed. Bottom-fishing signal.
3. **VIX Rule of 16** — Daily expected range from VIX. Strike selection + range boundaries.
4. **VIX Option Hedge Monitor** — Crash detection concept. Use VVIX as proxy.
5. **VIX Crossing** — VIX SMA(5) crossunder as universal filter.

### Key Lessons
- VIX-based scripts cluster into 3 types: (a) VIX MA crosses (b) VIX-derived ranges (c) Term structure/regime
- Look-ahead bias is rare in VIX indicators since they mostly use completed daily values
- The biggest gap in our data is VIX3M/VIX6M/VIX1Y for full term structure
- VIX9D/VIX ratio is immediately actionable with our existing data
- The Williams VIX Fix concept (synthetic VIX from price) is clever — no data dependency

### Patterns Across Scripts
- 5 of 8 VIX scripts use some form of VIX MA crossover — the core concept is the same
- Confirmation layers (NDX trend, price momentum) differentiate the better ones
- Term structure is fundamentally different from VIX level — regime vs. signal

### What We Haven't Tried
- Term structure regime filtering (contango/backwardation)
- VIX-implied daily range for strike selection
- Synthetic VIX (Williams VIX Fix) as an alternative to actual VIX
- VIX option sentiment as a crash detector

---

## Apex & LastFive Specific Ideas (from Batch 1)

Both strategies enter at 15:55 with 0DTE SPX credit spreads. Their edge is structural time-decay in the last 5 minutes. The key question: **what can go wrong in 5 minutes, and can PineScript concepts help prevent it?**

### For Apex (d=12, w=20, both-sides, 100% WR, Sharpe 11.65)

Apex's 100% WR over 4.25 years is extraordinary but means we haven't seen the tail event yet. Every filter here is **insurance** — it may slightly reduce trade count but protects against the black swan.

| Idea | Source Script | Concept | Priority | Why |
|------|--------------|---------|----------|-----|
| **VIX16 Adaptive Distance** | VIX Rule of 16 | Replace fixed d=12 with VIX-scaled: `d = 2 * SPX * (VIX/15.87/100) * sqrt(5/390)` | P1 | On calm days (VIX 12) d would be ~4, capturing more premium. On scary days (VIX 30) d would be ~10, staying safer. Dynamic > fixed. |
| **Term Structure Gate** | VIX Curve Pro | Skip when VIX9D/VIX > 1.05 | P1 | Backwardation = fear is front-loaded. Even 5 minutes can be dangerous. Conservative 1.05 threshold keeps most trades. |
| **Side Selection by Range Position** | VIX Rule of 16 | If SPX at 15:55 is >70% of VIX-implied daily range → call-only. <30% → put-only. | P2 | When SPX is near the upper expected boundary, the call side is more exposed. Smart-skip the dangerous side. |
| **WVF Intraday Check** | CM Williams VIX Fix | Skip if today's WVF > 2 std above 22-day mean | P2 | Prior-day return filter catches yesterday. WVF catches TODAY's intraday panic. Belt AND suspenders. |
| **VVIX Emergency Halt** | VIX Option Hedge Monitor | Skip when VVIX > 140 | P1 | Nuclear option. Only triggers ~5 days/year. Prevents trading during institutional panic. |

**Creative Apex idea:** Apex's d=12, w=20 means the short strike is 12pts OTM and the spread is 20pts wide. The VIX/16 math tells us the expected 5-minute SPX move is: `SPX * (VIX/15.87/100) * sqrt(5/390) ≈ 0.11% of SPX ≈ 6pts at SPX 5700`. So d=12 gives ~2x the expected move — healthy margin. But on VIX=30 days, the expected 5-min move jumps to ~15pts, breaching d=12. **This is where the black swan hides.**

### For LastFive ($174k P&L, Sharpe 5.48, -$5.6k DD, 87% WR)

LastFive's 87% WR means it has losing trades. The DD of -$5.6k is manageable but could be reduced. LastFive already has VIX-adaptive strikes and put filters — the ideas here are **upgrades** to existing mechanisms.

| Idea | Source Script | Concept | Priority | Why |
|------|--------------|---------|----------|-----|
| **VIX9D for Distance** | VIX Curve Pro | Use VIX9D instead of VIX for strike distance. VIX9D better captures near-term fear for 0DTE. | P1 | VIX is 30-day expectation. VIX9D is 9-day. For a 5-minute trade, shorter-term vol is more relevant. We have VIX9D data. |
| **VIX16 Continuous Distance** | VIX Rule of 16 | Replace discrete buckets (d=0/3/5) with `dist = ceil(1.5 * SPX*(VIX/15.87/100)*sqrt(5/390) / 5) * 5` | P1 | Current buckets create cliff effects at VIX 18 and 22. Continuous function is smoother and adapts better. |
| **Contango-Only Mode** | VIX Curve Pro | Skip when VIX9D/VIX > 1.0. Half-size when > 0.95. | P1 | Addresses the DD directly. Backwardation days are when credit spreads blow up. |
| **WVF Put Guard** | CM Williams VIX Fix | Skip PUT side when WVF > percentile(85) | P1 | Different from existing put momentum filter. Momentum catches sharp afternoon drops. WVF catches slow grinding multi-day selloffs. Both matter. |
| **VIX-Scaled Buyback Trigger** | VIX Rule of 16 | Scale the $2 protective buyback trigger: `trigger = 3 * SPX*(VIX/15.87/100)*sqrt(1/390)` | P2 | $2 is too tight on VIX=30 days (causing unnecessary buybacks, losing credit) and too loose on VIX=12 days (not protecting enough). |
| **VVIX Emergency Halt** | VIX Option Hedge Monitor | Skip when VVIX > 140 | P1 | Same as Apex. Even 5 minutes can destroy you during a crash. |

**Creative LastFive idea:** LastFive's VIX-adaptive distance currently uses 4 fixed buckets. The VIX/16 formula gives a continuous, mathematically-grounded alternative. At 15:55, the expected remaining move is:
- VIX 15: `5700 * 0.15/15.87 * sqrt(5/390) = 6.1pts` → dist should be ~10 (1.5x buffer)
- VIX 20: `5700 * 0.20/15.87 * sqrt(5/390) = 8.1pts` → dist should be ~15
- VIX 25: `5700 * 0.25/15.87 * sqrt(5/390) = 10.2pts` → dist should be ~15

This suggests LastFive's current d=0 at VIX<18 (ATM!) is actually aggressive — the expected 5-min move is ~6pts. Going ATM with w=5 means the short strike is AT the money with only 5pts of spread width. The expected move alone could breach it. **VIX16 math says d should never be 0 — minimum d=10 for safety.**

### Cross-Strategy Synergy

Both Apex and LastFive enter at 15:55. They could share a **universal 15:55 pre-check module**:

```
15:50 Pre-Trade Check (for both Apex and LastFive):
1. VIX9D/VIX ratio < 1.0? (contango check)
2. VVIX < 140? (crash check)  
3. Williams VIX Fix < 2 std? (capitulation check)
4. SPX within VIX/16 expected range? (range check)

If all pass → TRADE
If any fail → SKIP or REDUCE SIZE
```

This shared module could be implemented once and used by both strategies. It addresses the tail risk that killed MEDS and provides insurance for Apex's unseen black swan.
