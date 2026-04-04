# SimSearch — Research Findings

## Hypothesis

Markets repeat because humans repeat. Days with similar:
- Volatility regime (VIX level + structure)
- Dealer positioning (GEX/VEX)
- Price momentum (recent returns, gaps)
- Options skew (IV surface shape)
- Calendar context (day-of-week, FOMC proximity)

...tend to produce similar intraday outcomes (direction, range, shape).

---

## Feature Design Rationale

### Tier 1: Price & Volatility (1990+, ~20 features)

These are the longest-available features. Every historical day has them.

| Feature | Rationale |
|---------|-----------|
| `spx_return_1d` through `5d` | Momentum captures trend state |
| `spx_gap_pct` | Gap direction/size signals overnight sentiment |
| `spx_range_pct` | Realized volatility proxy |
| `spx_body_pct` | Directional conviction (close vs open relative to range) |
| `spx_upper_wick_pct` | Rejection signal |
| `spx_lower_wick_pct` | Support signal |
| `vix_close` | Absolute fear level |
| `vix_change_1d` | Fear acceleration |
| `vix_percentile_20d` | Relative VIX level |
| `vix_zscore_20d` | Standardized VIX |
| `dow` (0-4) | Day-of-week effect |
| `month` (1-12) | Seasonal patterns |
| `is_fomc_week` | FOMC proximity |
| `is_opex_week` | Options expiration proximity |
| `days_since_high_20d` | Mean reversion signal |
| `spx_above_sma20` | Trend state |
| `spx_above_sma50` | Intermediate trend |
| `spx_rsi_14` | Overbought/oversold |

### Tier 2: Intraday Shape (2013+, ~15 features)

These capture HOW the day is unfolding, not just where it ends.

| Feature | Rationale |
|---------|-----------|
| `morning_range_pct` (9:30-10:30) | First hour sets the tone |
| `morning_direction` | Up/down bias in first hour |
| `midday_range_pct` (10:30-13:00) | Consolidation vs continuation |
| `afternoon_range_pct` (13:00-16:00) | Late-day behavior |
| `high_time_bucket` | When the high occurred (morning/midday/afternoon) |
| `low_time_bucket` | When the low occurred |
| `reversal_magnitude` | How far price retraced from extremes |
| `trend_consistency` | % of 30-min bars in same direction |
| `volume_profile_skew` | Volume concentration early vs late |
| `max_1min_move` | Tail risk / spike magnitude |
| `ema_14_20_signal` | MEDS-compatible EMA state at key times |

### Tier 3: Options & Positioning (2022+, ~20 features)

The richest features — only 4 years of history but highest signal.

| Feature | Rationale |
|---------|-----------|
| `iv_atm_open` | ATM implied vol at open |
| `iv_skew_25d` | 25-delta put/call IV spread |
| `iv_term_ratio` | VIX1D/VIX ratio (term structure slope) |
| `vvix_level` | Vol-of-vol (uncertainty about uncertainty) |
| `gex_regime` | stable/fragile/cushion (dealer positioning) |
| `net_gex_zscore` | Standardized gamma exposure |
| `net_vex_zscore` | Standardized vanna exposure |
| `gex_plus` | Combined GEX+VEX signal |
| `put_call_oi_ratio` | Open interest skew |
| `gamma_concentration` | How concentrated gamma is around spot |
| `call_wall_distance` | Distance to call wall from spot |
| `put_wall_distance` | Distance to put wall from spot |
| `dealer_position` | Net dealer gamma sign |

---

## Validation Plan

### Walk-Forward Protocol
1. For each day D from 2023-01-01 to 2026-03-25:
   - Build feature vector for D using only pre-D data
   - Find top-K (K=10,20,50) most similar days from history before D
   - Record what happened on D (direction, range, close-to-close return)
   - Record what happened on the K similar days
2. Compare predictions vs actuals:
   - **Direction accuracy**: did the majority of similar days predict the right direction?
   - **Range RMSE**: how close was the predicted range to actual?
   - **Conditional P&L**: if used as a MEDS filter, what's the impact?

### Success Criteria
- Direction accuracy > 55% (random baseline = 50%)
- Range prediction correlation > 0.3
- Improvement over "use yesterday's stats" naive baseline

---

## Results — Walk-Forward Validation (2023-06-01 to 2026-03-25)

### Initial Discovery: Look-Ahead Bias

First run showed 88% direction accuracy and 0.93 return correlation. This was **look-ahead bias** — features like `spx_return_1d`, `spx_range_pct`, `morning_direction` were computed from the SAME day being predicted. The model was matching on outcomes, not inputs.

**Fix**: All features restructured to use PRIOR-DAY data only. Today's gap (known at open) is the sole same-day feature.

### Honest Baseline (No Look-Ahead)

| Config | Direction Acc | High-Conf (>60%) | Very-High (>70%) | Return Corr | Sharpe |
|--------|-------------|------------------|------------------|-------------|--------|
| K=20, Tier 3, Cosine | **51.3%** | 54.9% (173 days) | 61.3% (31 days) | 0.128 | 0.40 |
| K=10, Tier 3, Cosine | 49.2% | 52.3% (239 days) | **58.2% (79 days)** | 0.146 | 0.84 |
| K=5, Tier 3, Cosine | **53.0%** | 54.2% (277 days) | 54.2% (277 days) | 0.120 | **1.35** |
| K=3, Tier 3, Cosine | 51.6% | 51.6% (708 days) | 54.0% (189 days) | 0.146 | 0.49 |
| K=50, Tier 3, Cosine | 50.7% | 48.9% (92 days) | 33.3% (3 days) | 0.076 | 0.01 |
| K=10, Tier 1 only | 50.6% | 50.6% (231 days) | 53.9% (76 days) | 0.037 | 1.71 |
| K=10, Tier 2 only | 50.1% | 49.0% (202 days) | 47.0% (66 days) | 0.167 | 1.69 |

### Key Findings

1. **Direction prediction is near-random**: 51.3% overall (random = 50%). This is consistent with MEDS research — daily SPX direction cannot be predicted from prior-day features.

2. **High-confidence filter has mild signal**: When the model is very confident (>70% agreement among matches), accuracy reaches 58-61%. This is ~31-79 actionable days per ~700 tested.

3. **Fewer matches = stronger signal**: K=5 beats K=50. The closest few days carry more information than averaging across many. Diluting with weaker matches destroys signal.

4. **Return correlation is weak but positive (0.13-0.15)**: Not useful for magnitude prediction, but suggests some clustering of day types.

5. **Range prediction is poor (r=0.07)**: Prior-day state does not predict today's range.

6. **No next-day predictive power (49.6%)**: SimSearch does not predict T+1 direction.

7. **Tier 3 (options) adds marginal value**: Tier 3 (0.128 corr) slightly beats Tier 1 only (0.037) and Tier 2 only (0.167), but the improvement is within noise. With only 4 years of options data, Tier 3 has limited history to match against.

### Implications

SimSearch in its current form is **not a standalone trading signal**. However, the high-confidence filter (>70%) could potentially be used as a supplementary filter for existing strategies — reducing exposure on uncertain days and sizing up on high-confidence days. This needs further testing with actual strategy P&L.

The primary value of SimSearch for direction is **qualitative**: showing a trader what similar days looked like, not generating automated signals.

---

## Results — Regime Prediction (Range/Volatility)

### The Pivot: Predict Range, Not Direction

Direction is coin-flip. But **range prediction is automatable** because:
- Volatility clusters (yesterday's range predicts today's)
- VIX level predicts range
- GEX regime correlates with range (r=-0.57)
- Credit spread strategies care about HOW MUCH it moves, not which way

### Regime Validation (2023-06-01 to 2026-03-25, 706 days)

| Metric | SimSearch | Naive Baseline |
|--------|----------|----------------|
| Range correlation | **0.59** | 0.64 (yesterday's range) |
| Bucket accuracy | **46.9%** | 44.0% |
| Adjacent accuracy | **89.2%** | — |
| Danger recall | **53.4%** | — |
| Danger precision | **39.2%** | — |

### Danger Day Detection

- **Actual danger days**: 88 (12.5% of all days)
- **Flagged**: 120 days (caught 53% of actual dangers, 39% precision)
- **TRADE days avg range**: 0.87%
- **SKIP days avg range**: 1.71% (2x higher — massive separation)
- **Danger rate on TRADE days**: 7.0%
- **Danger rate on SKIP days**: 39.2%

### Filter Backtest (Credit Spread P&L Proxy)

| Metric | Unfiltered | SimSearch Filter | Change |
|--------|-----------|-----------------|--------|
| Total P&L | $29,940 | **$50,430** | **+68%** |
| Max DD | -$18,950 | **-$1,980** | **+$17k better** |
| Sharpe | 2.83 | **8.01** | **+5.18** |
| Calmar | 1.58 | **25.47** | +23.89 |
| Win rate | 87.5% | **92.0%** | +4.5pp |
| Days traded | 706 | 639 (91%) | skip 67 days |

**What the filter does:**
- Skips 67 days (9%) — avg unfiltered P&L on skipped days: **-$297/day**
- Reduces size on 53 days (8%) — avg unfiltered P&L: -$22/day
- Trades 639 days (83%) — clean, safe days

**Worst day analysis:** The filter correctly SKIPs the 2025 April tariff meltdown (04-04 through 04-11), each day -$1,500 unfiltered → $0 filtered. Only 2 catastrophic days escape: 2024-12-18 and 2025-10-10.

### K-Value Sweep (Filter Backtest)

| K | P&L (Filtered) | Max DD | Sharpe | Days Skipped |
|---|---------------|--------|--------|-------------|
| 5 | $49,630 | -$2,100 | 7.73 | 78 |
| 10 | **$50,430** | **-$1,980** | **8.01** | 67 |
| 15 | $49,930 | -$1,980 | 7.90 | 70 |
| 20 | $50,290 | -$1,980 | 7.96 | 69 |
| 30 | $50,810 | -$1,980 | 8.01 | 64 |

All K values perform similarly. K=10 is robust and efficient.

### Phase 2: Deep History (30 years, 8,370 days)

Downloaded SPX daily OHLC from Yahoo Finance (1993-2026) to extend the matching pool from 1,066 to 8,370 days.

**Key fix**: Shared-feature matching — when comparing a Tier 3 query against a Tier 1-only candidate (pre-2022), only score on features that both actually have (no median imputation noise).

| Metric | 4yr only (K=10) | 30yr (K=10) | 30yr (K=20) | 30yr (K=50) |
|--------|----------------|-------------|-------------|-------------|
| Range correlation | 0.59 | 0.62 | **0.64** | **0.64** |
| Bucket accuracy | 46.9% | 49.0% | **51.6%** | 51.4% |
| Danger recall | 53.4% | 60.2% | 59.1% | 60.2% |
| Danger rate TRADE | 7.0% | 6.3% | 6.4% | **6.2%** |
| Danger rate SKIP | 39.2% | 34.6% | 37.1% | 38.1% |

**Key insight**: With deeper history, MORE matches is better (K=20-50 beats K=5). The opposite of the 4-year result. 30 years provides enough diversity that averaging over more matches improves rather than dilutes.

At K=50, range correlation **matches the naive baseline** (0.6448 vs 0.6405) — the model now equals "use yesterday's range" while providing the additional danger day detection that the naive baseline cannot.

### Filter Backtest — Deep History (K=20)

| Metric | Unfiltered | SimSearch Filter | Change |
|--------|-----------|-----------------|--------|
| Total P&L | $29,940 | **$51,465** | **+72%** |
| Max DD | -$18,950 | **-$1,840** | **+$17k better** |
| Sharpe | 2.83 | **8.66** | **+5.83** |
| Calmar | 1.58 | **27.97** | +26.39 |
| Win rate | 87.5% | **92.5%** | +5pp |
| Days traded | 706 | 614 (87%) | skips 92 |

### Conclusion: Automatable

SimSearch as a **range/regime predictor** is automatable:
1. Compute prior-day features (VIX, GEX, returns, intraday shape)
2. Find 10 most similar historical days
3. If >25% were danger days → SKIP/REDUCE
4. Otherwise → TRADE

The filter catches ~50% of danger days while only skipping ~9% of all days. Applied to a **naive credit spread P&L proxy**, it transforms risk-adjusted returns dramatically (Sharpe 2.83 → 8.91).

**HOWEVER**: see "Real Strategy Integration" below for the critical caveat.

---

## Improvement Round: Cross-Asset + Ablation + Multi-Day Context

### Feature Ablation (2026-04-04)

Direct correlation of each feature with daily range:

| Feature | |r| with Range | Notes |
|---------|---------------|-------|
| iv_atm_open | **0.77** | Dominant predictor |
| vix_prev_close | **0.74** | Close second |
| spx_dist_from_20d_high | **0.69** | Selloff depth |
| spx_prev_range_pct | **0.65** | Vol clustering |
| realized_vol_20d | **0.64** | Backward vol |
| gamma_concentration | **0.58** | Options positioning |
| prev_midday_range_pct | **0.55** | Intraday vol |
| vix1d_vix_ratio | **0.53** | Term structure |
| calendar features | **0.02** | Pure noise |
| iv_skew_25d | **0.004** | No signal |

**Action**: Upweighted top features 2-3x, downweighted calendar to 0.1x. Result: danger recall 60% → **65%**, danger rate on TRADE days 6.3% → **5.7%**.

### Cross-Asset Data (TLT, GLD, UUP, IWM)

Downloaded from Yahoo Finance. Adds prior-day returns and ranges for bonds, gold, dollar, and small caps. Extends matching context for macro-driven days.

### Multi-Day Context

Added: `spx_prev2_range_pct`, `spx_prev3_range_pct`, `vix_momentum_3d`, `spx_streak`, `range_change_2d`. These capture volatility persistence and trend momentum.

### Combined Result

| Metric | Before | After Cross-Asset + Ablation |
|--------|--------|-----|
| Features | 65 | 82 |
| Danger recall | 60% | **65%** |
| Danger rate TRADE | 6.3% | **5.7%** |
| Filter Sharpe | 8.66 | **8.91** |
| Filter DD | -$1,840 | **-$1,640** |

### Ensemble (Ridge Regression)

Blended SimSearch + naive + VIX + realized vol via walk-forward Ridge regression (252-day training window). Result: **did not beat SimSearch alone** (corr 0.585 vs 0.594). Components too correlated to benefit from linear blending. Danger detection marginally improved (66% recall).

---

## Real Strategy Integration (Critical Finding)

### The P&L Proxy Was Wrong

The proxy model assumed: high range → big losses. This is true for a naked directional bet, but **false for well-designed credit spread strategies**.

### What Actually Happened

| Strategy | Unfiltered | Filtered | Change | Problem |
|----------|-----------|----------|--------|---------|
| **MEDS** | $42,726 | $41,004 | **-$1,722** | Skipped profitable days |
| **Apex** | $12,495 | $7,552 | **-$4,942** | 100% WR; every skip loses money |
| **LastFive** | $191,050 | $165,290 | **-$25,760** | Avg skip-day P&L was +$181 |

### Why Range Filtering Fails for Real Strategies

1. **MEDS worst day (-$1,254)**: range was only 1.02%. Losses come from EMA direction errors on normal days, not from range.

2. **LastFive worst day (-$2,639)**: range was 1.03%. Loss days average 0.90% range vs 0.94% on winners. **Losses are INVERSELY correlated with range**.

3. **Apex**: 100% win rate. Every day is profitable. Skipping any day is pure loss.

4. **MEDS already skips truly dangerous days** (VIX>35 filter, tariff period = 0 trades). SimSearch can't add value over existing filters.

5. **Maximum feature-to-loss correlation: 0.10**. No prior-day feature in our 82-feature set predicts when these strategies will lose. Strategy losses are effectively random noise with respect to market state entering the day.

### Conclusion

SimSearch's range-based filter is valuable for:
- **Unhedged directional positions** (the P&L proxy scenario)
- **Strategies without built-in risk management**
- **Qualitative "what do similar days look like?" analysis**

It is **NOT valuable** as an automated filter for strategies that already have:
- Per-position stop losses
- VIX-based filters
- Intraday direction adaptation (EMA)
- Width/strike distance calibration

These strategies' losses are driven by **execution quality and direction noise**, not by market regime — exactly the factors SimSearch cannot observe.
