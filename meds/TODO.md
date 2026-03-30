# MEDS Strategy — Backlog

All experiment results, deep-dives, and decision rationale are in [RESEARCH.md](RESEARCH.md).

---

## Current Sprint

| # | Item | Status | Acceptance Criteria |
|---|------|--------|---------------------|
| 1 | ~~VIX Futures Backwardation filter [13]~~ | **REJECTED** | Marathon tested: backwardation SL costs -$91,642 P&L (Sharpe 14.55→11.80). VIX9D/VIX ratio is a proxy for VIX level (r=0.455). Only positive filter (VIX<13 + backwardation) yields +$1,268 on 67 trades/4yr; 64% already caught by dynamic SL. See [5] in RESEARCH.md |
| 2 | ~~VVIX/VIX Ratio signal [19]~~ | **REJECTED** | VVIX data loaded. Full sweep: Back+VVIX>=100 (67 days) costs -$39k to -$47k; Back+VVIX>=120 (14 days) costs -$5k to -$10k. VVIX skip (>120/130) also costs P&L. VVIX 110-120 is the best WR bucket (97.2%). No actionable filter found |
| 3 | ~~VIX9D/VIX term structure filter~~ | **REJECTED** | Full sweep: 4 conditions × 5 SL levels. VIX9D back+VIX15-20 (71 days) has 95.7% WR — one of best subsets. SL costs -$16k at loosest. Covers TODO backlog item 3. No VX1 download needed — VIX9D proxy conclusive |
| 4 | MOVE/VIX Ratio cross-asset signal [21] | TODO | Download MOVE from FRED, compute prior-day ratio quintiles, marathon test as skip filter on extreme high-ratio days |

---

## Prioritized Backlog

Priority order reflects expected signal quality, data availability, and distinctiveness from prior tests.

### High Priority — New Signal Research

| # | Item | Effort | Data Needed | Notes |
|---|------|--------|-------------|-------|
| 4 | ~~True IV Skew from BSM [15]~~ | ~~Medium~~ | ~~Compute from existing bid/ask data~~ | **TESTED** — Q1→Q5 gradient $104/day, weaker than credit proxy ($274). Only useful for Kelly sizing. See [15] in RESEARCH.md |
| 5 | ~~GEX / Dealer Positioning [20]~~ | ~~Medium~~ | ~~SqueezeMetrics (free tier)~~ | **TESTED** — GEX 66% corr w/ VIX (redundant). No added signal within VIX zones. DIX gradient $139/day too weak. Both useful only for Kelly sizing |
| 6 | DSPX Dispersion Index [24] | Medium | CBOE (availability unclear) | Market crowding signal. Thresholds: DSPX ~37 vs avg 23; DSPX-VIX spread >20 = 85th %ile danger zone. Correlation spike → dispersion unwind → violent moves (cf. Feb 2018 Volmageddon) |

### Ranked Backlog — Untested Ideas

Ranked by expected signal quality, data availability, and distinctiveness from prior tests.

| Rank | Item | Effort | Notes |
|------|------|--------|-------|
| 1 | Hard time exit — close all by 3:15 PM [gamma] | Low | Hour-15 losses avg -$952 (8x noon). Close positions early to avoid 0DTE gamma spike. Backtestable now with existing logs |
| 2 | Early profit-taking at % of credit [research] | Low | Close at 50% or 65% of credit received. Iron condor research (8–20Δ) shows improved equity curve + smaller DD with early close. Different from Chop Rule (% of max profit) and DAILY_TP (fixed $). Backtestable with existing logs |
| 3 | ~~VIX9D/VIX term structure filter [vix9d]~~ | ~~Medium~~ | **REJECTED** — VIX9D is 96.5% correlated with VIX (pure proxy). Ratio vs P&L r=0.067 (zero signal). Inversion skip costs $182k. Contango bonus is backwards (deep contango = LOWER P&L). Gap narrowing has zero gradient. Confirmed by both trade-log analysis and marathon sweep |
| 4 | Tighter per-trade SL when day is already negative (Option 3c) | Medium | Once daily P&L < -$500, subsequent positions use -$150 SL. Targets mixed-result days. No external data needed |
| 5 | RV/IV ratio regime filter [research] | Medium | 20-day realized vol / VIX as sizing signal. 0.5–0.8 normal (full size), >0.8 stress (reduce/tighten SL). Well-documented thresholds. Distinct from VIX9D vs RV (#9) — uses 30-day horizon |
| 6 | VIX-conditional PCE skip | Low | PCE has 69% WR (p=0.00002 vs 91.8% baseline). Test: skip PCE only when VIX <15 or 25–30 (weak zones). Full skip costs $17k; conditional may preserve most P&L while filtering worst days |
| 7 | ~~VIX9D regime sizing [vix9d]~~ | ~~Medium~~ | **REJECTED** — VIX9D is 96.5% correlated with VIX; this is just VIX-based Kelly in disguise. Already have Kelly sizing (blocked on BP) |
| 8 | Survival Analysis — time-in-trade exit [ml] | Medium | Wins avg 299 min, losses avg 86 min. Cox Proportional Hazards model: "if trade hasn't recovered by 90 min, failure probability triples." Smart dynamic exit using time-to-event data. Backtestable with existing logs. 90% of trades go to expiration (censored) — perfect for survival modeling |
| 9 | Widen danger zone to VIX 13.5–15.0 | Low | Extend dynamic SL coverage to fill unprotected gap. From Finding 2 |
| 10 | Entry window cutoff by VIX range (Option 3b) | Low | For VIX 15–20, stop entries at 11:30 instead of 12:45. Sweep over cutoff times |
| 11 | ~~VIX9D vs Realized Vol edge [vix9d]~~ | ~~Medium~~ | **SUPERSEDED** — VIX9D ≈ VIX (r=0.965). Use RV/IV ratio (#5) with VIX instead; same signal, no extra data needed |
| 12 | ~~VIX9D-based dynamic strike distance [vix9d]~~ | ~~Medium~~ | **SUPERSEDED** — VIX9D ≈ VIX (r=0.965). If dynamic strike distance is worth testing, use VIX directly. MIN_OTM=30 already works well |
| 13 | Non-standard delta strikes (9–12Δ) [research] | Medium | Test selling at 9–12 delta instead of fixed 30pt OTM. Research shows higher WR, smaller DD, smaller avg gain. Fundamentally different strike selection approach vs fixed distance |
| 14 | Time-decayed position sizing [gamma] | Medium | Reduce qty for later entries (e.g., qty=1 after noon). Distinct from Kelly (VIX-based). Limited impact since entry window ends 12:45 |
| 15 | Conformal Prediction [ml] | Medium | Mathematical guarantee on win probability (e.g., "95% confident this is a win"). More rigorous than calibrated RF — explicitly outputs "I don't know" when confidence is insufficient. Skip trade when confidence < threshold |
| 16 | Normalized skew richness signal [research] | Medium | (25Δ put IV − 25Δ call IV) / ATM IV. >0.35 expensive (good for selling), <0.15 cheap (reduce size). Different from BSM IV Skew (#4 tested) — uses normalized ratio with documented thresholds. Needs vol surface data |
| 17 | Anomaly detection — Isolation Forest [ml] | Medium | Flag "weird" entries (unusual VIX/EMA/credit combos) before entering. Unsupervised — avoids class imbalance problem. Precursor pattern may catch loss days that rule-based filters miss |
| 18 | Monte Carlo Risk of Ruin [ml] | Low | Formalize max drawdown probability from WR + avg win/loss. Run 10k simulated months. Validates qty=2 and Kelly projections. If ruin prob >1%, reduce size |
| 19 | VIX/VVIX divergence signal [vvix] | Medium | VIX new high + VVIX lower high = vol exhaustion, hold positions. Opposite divergence = exit early. Fed research: VVIX negatively predicts tail risk hedge returns 3–4 weeks ahead |
| 20 | t-SNE/UMAP loss cluster visualization [ml] | Medium | Squash features into 2D map to visualize loss clusters. If today's conditions land in a "loss cluster," skip. Discovery tool — may reveal structure invisible to rule-based analysis |
| 21 | VVIX-adjusted strike distance [vvix] | Medium | Add 5pt to MIN_OTM per 10pt VVIX above 100. Dynamic OTM buffer based on vol-of-vol. Overlaps with VIX9D version (#12) |
| 22 | Regime clustering (K-Means/HMM) [ml] | High | Group days into regimes using VIX/VVIX/SPX returns. Only trade in profitable clusters. HMM detects hidden state shifts before obvious. ⚠️ Overfitting risk: ~1,050 trading days is small for unsupervised ML |
| 23 | Acceleration SL — speed-based exit [gamma] | Medium | Exit on rapid adverse move (e.g., 10pt in 5min) regardless of price level. Needs intraday SPX tick data. High effort |
| 24 | MAX_OTM_DISTANCE cap | Low | Skip entries where strike >75pt OTM. Likely overlaps VIX 25–30 zone. From Finding 5 |
| 25 | "Wonging" Entry — wait for 0.3% adverse move [30] | Medium | Enter only after SPX moves against spread direction. Gets better credit. Needs intraday SPX tracking |
| 26 | Cost-Sensitive Learning [ml] | Medium | Penalize model more for missing big losses than small wins. Avg loss ~2x avg win — weight accordingly. Addresses class imbalance better than SMOTE by focusing on economic impact |
| 27 | GARCH volatility forecasting [ml] | Medium | Predict tomorrow's vol with GARCH model; adjust strikes/sizing preemptively before VIX moves. Well-established method but adds model complexity |
| 28 | KNN Historical Analogues [ml] | Low | "What happened on the 10 most similar days?" Match on VIX/EMA/credit features. Intuitive, interpretable, no black box. Could surface patterns invisible to parametric models |
| 29 | ~~VIX9D convergence filter [vix9d]~~ | ~~Low~~ | **SUPERSEDED** — VIX9D ≈ VIX (r=0.965). VIX9D rising = VIX rising; no independent signal |
| 30 | ML entry gate — calibrated classifier [ml] | High | RF/XGBoost to predict win probability; Platt scaling for calibration; skip if P(win) < 92%. SMOTE for class imbalance. ⚠️ Major overfitting risk: 91.7% WR means model defaults to "always win"; same failure mode as Bayesian gate (false positives on wins) |
| 31 | VIX decomposition factor-based signal [research] | High | Use put slope (Factor 3 = real fear) vs parallel shift (Factor 2 = event prep) vs wing expansion (Factor 5 = tail risk) to classify VIX moves. Needs vol surface data; complex but most granular VIX signal possible |
| 32 | Halt entries on intraday trend reversal (Option 3f) | High | EMA cross / VWAP cross / rolling high break as entry suppression signal. Related signals tested poorly |
| 33 | Propensity Score Matching on losses [ml] | Medium | Compare losses to near-identical wins (matched on VIX/EMA/credit). Isolates hidden "X-factor" (time of day? day of week? sequence?). Analysis/discovery tool, not a live signal |
| 34 | Copula tail dependency modeling [ml] | High | Models non-linear tail risk (VIX explosion during SPX crash). Better risk pricing than linear correlation. Useful for stress-testing but complex implementation |
| 35 | VVIX mean reversion sizing [vvix] | Medium | At 52-week VVIX highs, sell wider/more aggressively for premium. Contrarian — goes against risk reduction |
| 36 | Dynamic WIDTH by entry time [gamma] | Low | Narrower spreads (10pt) for later entries. Limited value since entry window ends 12:45 |
| 37 | VVIX-adjusted minimum credit [vvix] | Low | Demand higher MIN_NET_CREDIT when VVIX elevated. Raising MIN_NET_CREDIT always cost P&L in sweeps |
| 38 | VIX/GEX divergence signal [gamma] | Medium | VIX rising + GEX falling = danger. GEX tested redundant with VIX; divergence speculative |
| 39 | SVM non-linear boundaries [ml] | Medium | Find complex VIX×EMA×time interactions for win/loss separation. But RF/XGBoost (#30) already handle non-linear boundaries; likely redundant |
| 40 | Time Series Decomposition (trend/noise) [ml] | Medium | Decompose SPX into trend/seasonality/noise. If noise >80%, EMA signals may be false positives. Interesting but EMA already captures trend component |
| 41 | Bayesian Structural Time Series [ml] | High | Detect structural breaks (Fed policy shifts) that cause loss clusters. Useful for diagnosing but rare events; low predictive value for daily decisions |
| 42 | Chop Rule — close at 80% max profit with 2h+ remaining [13-ideas] | Medium | Similar to DAILY_TP but % capture + time filter. All fixed TP levels rejected; this framing unlikely to differ |
| 43 | "Semi-Bluff" half-size on near-EMA-cross [41] | Medium | EMA alignment as sizing trigger (not gate). EMA as gate rejected; sizing variant speculative |
| 44 | VVIX term structure [vvix] | Medium | Short-term VVIX > long-term = panic regime. Needs specialized CBOE term structure data; availability unclear |
| 45 | Systemic reset indicator [vvix] | Low | VVIX drops 10%+ in a day after crash week = green light. Rare event (2–3 times in 4yr backtest) |
| 46 | ~~VIX1D/VIX9D spread [vix9d]~~ | ~~Low~~ | **SUPERSEDED** — Both VIX1D and VIX9D are high-correlation VIX proxies. VIX1D backwardation already tested and rejected (costs -$5k to -$91k). No independent signal in spread |
| 47 | Pot Odds filter — MIN_NET_CREDIT to 3.5% of width [34] | Low | Raise MIN_NET_CREDIT from $0.55 to $0.70. Raising always cost P&L in sweeps |

### Tested / Rejected / Done

| Item | Result |
|------|--------|
| ~~FOMC VIX 15–20 tighter SL~~ | **REJECTED** — marathon sweep -$500 to -$3,000: all levels cost P&L (-$4k to -$16k), DD unchanged |
| ~~VIX-range econ day SL sweep~~ | **REJECTED** — 4 net-negative combos found but marathon skip costs -$8.7k P&L (Sharpe +0.50 not worth it) |
| ~~Econ day adjacency analysis~~ | **DONE** — no T-1/T+1 pattern; FOMC/PCE weakness is event-day-only |
| ~~Strike distance decay signal (delta gate)~~ | **REJECTED** — delta chg=0.000 at worst entries on 4/5 top loss days. Best threshold saves $6.5k, costs $97k. 20.8:1 false positive ratio |
| ~~Bid/ask spread width filter [33]~~ | **REJECTED** — SPXW 0DTE spreads discrete ($0.05/0.10/0.15 = 99%). Any filter costs P&L. Correlates with VIX 25-30, handled by dynamic SL |

### Low Priority

| Rank | Item | Effort | Notes |
|------|------|--------|-------|
| 48 | Hurst Exponent regime filter [18] | Medium | Classifies movement *type* not magnitude. Novel but consistent pattern suggests low odds |
| 49 | QQQ/SPY Correlation [22] | Low | Within-equity signal. Weak theoretical basis vs cross-asset [21] |
| 50 | ~~Dynamic Delta-Adjusted Strike Distance [25]~~ | ~~Medium~~ | **REJECTED** — delta paradox: losses have LOWER |delta| than wins. |delta|<0.10 = no-op (never triggers under MIN_OTM=30). |delta|<0.08 blocks 37.5% of trades, catches 8.8% of losses, costs -$280k net |
| 51 | Iron Condor on winning Put spread [31] | High | Open Call side to lock in profit on middle zone. Complex position management |

### Not Doing — Declined Ideas

Ideas proposed but not pursued. Kept here for reference to avoid re-proposing.

**Insufficient data / too complex for dataset size (~1,050 trading days):**

| Item | Reason |
|------|--------|
| LSTM/GRU (Recurrent Neural Networks) | ~1,050 days far too small for RNNs; requires sequence data not in trade logs |
| CNN for chart imaging | Requires intraday price image generation pipeline; massive engineering for speculative benefit |
| Transformer/Attention architectures | Same data scarcity problem as LSTM; overkill for ~1,050 samples |
| Transfer Learning (pre-trained SPX model) | No pre-trained 0DTE options model exists; SPX price model wouldn't transfer to credit spread P&L |
| Stacking / Meta-Learning | Stacking 3 models that each struggle with 91.7% WR class imbalance won't fix the root problem |
| Semi-Supervised Learning | Unlabeled SPX minutes don't map to trade outcomes — labels are at position level, not tick level |
| Reinforcement Learning for sizing | Massive complexity; ~1,050 days far too small for RL agent to generalize |

**Outside scope / not applicable to this strategy:**

| Item | Reason |
|------|--------|
| NLP on News headlines | Needs entire news data pipeline; far outside scope of a mechanical signal system |
| Weekend VVIX/Gamma Rule | Strategy is 0DTE only — no overnight or weekend positions to manage |
| Short Deck / Trend Following switch | Strategy framework doesn't support intraday strategy switching (credit spreads only) |

**Redundant with tested/existing items:**

| Item | Redundant with |
|------|---------------|
| Gamma Flip Table Change (GEX signal) | GEX TESTED (#5) — 66% correlated with VIX, redundant |
| Zero GEX Neutral Deck | GEX TESTED (#5) — same redundancy |
| Absolute vs Relative GEX (percentile) | GEX TESTED (#5) — percentile variant still redundant with VIX |
| Shadow GEX / Dark Pools as strike selector | GEX TESTED (#5) — redundant |
| VVIX/VIX Ratio as standalone | Already Sprint #2 acceptance criteria |
| VVIX/Skew correlation | IV Skew TESTED (#4) — gradient too weak ($104/day) |
| Pre-Event VIX9D inflation | Econ day analysis DONE (#9) — events already handled |
| VIX9D Delta Adjustment | Delta paradox REJECTED — losses have LOWER delta than wins |
| VIX9D > 35 Circuit Breaker | Already in config: VIX_MAX_FILTER = 35.0 |
| VVIX9D (Meta-Vol index) | No separate VVIX9D index available from CBOE |
| VVIX and Vig (bid-ask impact) | Bid/ask spread filter REJECTED (#33) — SPXW spreads too discrete |
| Pin Play (OpEx pinning) | Triple Witching already handled in config (removed from holidays) |
| Gamma Scalping / Small Ball (20% TP) | Overlaps with Chop Rule (Rank 42) |
| Volatility Cluster Filter (pause after spike) | Overlaps with halt on trend reversal (Rank 32) |

**Methodology items (not standalone backlog items):**

| Item | Where it lives |
|------|---------------|
| PCA for dimensionality reduction | Premature until more uncorrelated signals exist; VIX/VVIX correlation already documented |
| Recursive Feature Elimination (RFE) | Standard ML step — covered in ML Methodology Standards section |
| Genetic Algorithms for EMA optimization | Direction alternatives all tested poorly vs VIX change; optimizing EMA params risks overfitting |
| Bayesian Optimization for strike distance | MIN_OTM=30 already sweep-validated across full range |
| Online Learning (SGD updates) | Methodology concern — covered by walk-forward validation requirement |
| Logistic Regression with Elastic Net | Standard ML baseline — covered in ML Methodology Standards |
| Naive Bayes probabilistic baseline | Standard ML baseline — not a standalone backlog item |
| LDA/QDA discriminant analysis | Standard ML baseline — not a standalone backlog item |

**Educational/conceptual only (no actionable test):**

| Item | Notes |
|------|-------|
| Dealer's Hand (MM Hedging mechanics) | Educational context — informs understanding but not testable as a filter |
| Vanna/Charm time decay mechanics | Educational — variant of hard time exit (Rank 1) |
| Sunset Rule (2 PM cutoff) | Variant of hard time exit (Rank 1) already captured |
| Insurance Hedge (buy OTM put) | Already in Live Trading Prep as "Variance insurance" |
| FFT cycle decomposition | Overlaps with Time Series Decomposition (Rank 40) |

**Validation ideas already covered:**

| Item | Where it lives |
|------|---------------|
| Walk-Forward Optimization | ML Methodology Standards section (walk-forward cross-validation) |
| Bootstrap Resampling | Overlaps with Monte Carlo Risk of Ruin (Rank 18) + Monte Carlo Permutation (Validation #6) |
| CVaR / Expected Shortfall | Subsumed by Monte Carlo Risk of Ruin (Rank 18) — both quantify tail risk |
| Commission Drag Analysis | Already accounted for: COMMISSION = $0.50/contract/leg included in all backtests |

---

## Pending Upgrade — BLOCKED on Account Size

### Kelly Sizing — READY, LOCKED ON BP
- Full Kelly (max qty=4): P&L $1,168,607 (+92%), MaxDD -$19,844 (2x), Sharpe 13.18, Calmar 58.9
- Zone map: VIX <13→1, 13–15→3, 15–20→4, 20–25→4, 25–30→1, >30→4
- **Locked**: requires ~$77,720 peak BP; current account ~$40,000 (97% used at qty=2)
- **Action**: set `ENABLE_KELLY_SIZING = True` in `meds.py` when BP reaches ~$80,000+

---

## Engineering

### Persistent Quote Cache — TODO
Save each day's fetched quote data to disk on first access. On subsequent runs, load from disk instead of hitting the API. Recommended: parquet per day in `data/quote_cache/YYYYMMDD.parquet`.

**Expected impact:**
- First run: same speed (fetches and writes cache)
- Subsequent runs: near-instant — all API calls replaced by local disk reads
- Sweeps: 6-level sweep runs at single-run speed

**Hook point:** `fetch_quotes_for_strikes_cached()` — check for cached parquet before calling API.

### ML Methodology Standards — Apply to All ML Backlog Items

⚠️ **Overfitting warning:** ~1,050 trading days with 91.7% WR. Any classifier will default to "always predict win." ML adds value only if it catches the ~8% loss pattern — the same problem every manual filter has failed at (20:1+ false positive ratios).

- **Walk-forward cross-validation** — never test on training data. Train Jan–Mar, test Apr. Train Feb–Apr, test May. Required for all ML items
- **SHAP feature importance** — run on any model to explain predictions. Prevents black-box decisions
- **SMOTE / class imbalance** — use synthetic minority oversampling for any classifier targeting the rare loss class
- **Out-of-sample requirement** — any ML signal must show positive P&L on walk-forward OOS data before marathon verification
- **RF feature importance caveat** — constant features (e.g., WIDTH=20) show 0% importance because they never vary, NOT because they don't matter

### Validation & Hardening — Ranked by Priority

Tests to validate that the strategy edge is real and robust, not overfitted or fragile. Apply before going live.

**OOS baseline (already computed):** In-sample (70%): WR 90.5%, Sharpe 10.48, DD -$6,894. Out-of-sample (30%): WR 94.5%, Sharpe 12.25, DD -$3,376. OOS *improved* — positive sign for robustness.

| Priority | Test | Effort | Notes |
|----------|------|--------|-------|
| 1 | Parameter Sensitivity — 3D surface maps | Low | Test EMA 12–14 × 47–49, MIN_OTM 28–32, MIN_NET_CREDIT 0.50–0.60. If profit drops >30% from ±1 change, strategy is fragile overfit. Robust strategy should show a "plateau" |
| 2 | Random Entry Baseline | Low | Replace EMA/VIX direction signal with random PUT/CALL each day. If random performs similarly, the direction signal is placebo and all edge comes from MIN_OTM + credit structure |
| 3 | Slippage Degradation Test | Low | Deduct extra $0.05 from every credit received + add $0.05 to every SL exit. If still profitable, survives retail execution in fast markets |
| 4 | Deflated Sharpe Ratio (DSR) | Low | Penalize Sharpe for number of parameter combos tested across all experiments. Prevents Multiple Testing Bias from 50+ sweep iterations |
| 5 | PnL Attribution (Alpha vs Beta) | Medium | Linear model: how much P&L from "market went up" (beta) vs strategy rules (alpha). If >70% is beta, edge disappears in bear markets |
| 6 | Monte Carlo Permutation ("Monkey" test) | Low | Shuffle P&L order 10k times. If real max DD is in bottom 5% of shuffled distribution, sequence was lucky and true DD risk is higher |
| 7 | CPCV (Purged Cross-Validation) | Medium | López de Prado's method: purge data between train/test to prevent information leakage across 0DTE positions that straddle day boundaries |
| 8 | KS Test — regime shift detection | Low | Compare win P&L distribution 2022 vs 2024. If statistically different (p<0.05), market regime shifted and old validation metrics may not apply |
| 9 | VIX-Regime Stress Test | Medium | Inflate historical VIX by 20% and re-run. Tests if dynamic SL and VIX_MAX_FILTER hold under amplified vol conditions |
| 10 | Information Decay Analysis | Low | Track WR and avg P&L in rolling 6-month windows. If edge is declining year-over-year, strategy may be "aging" as market adapts |
| 11 | Probabilistic Sharpe Ratio (PSR) | Low | Probability that true Sharpe exceeds a threshold (e.g., 2.0) given track record length. Accounts for sample size uncertainty |
| 12 | Broken Wing Sensitivity | Low | Shift long leg 5pt further OTM. If strategy breaks, edge is too thin to survive slippage or fill quality issues |
| 13 | Omega Ratio | Low | Downside-only volatility metric. Better than Sharpe for asymmetric P&L (high WR + rare large losses). Easy to compute from existing logs |
| 14 | Gain-to-Pain Ratio (Schwager) | Low | Sum of monthly gains / sum of monthly losses. Raw measure of emotional cost per dollar earned |
| 15 | Equity Curve R² | Low | Linear regression on cumulative P&L. R² > 0.90 = smooth, psychologically tradeable curve |
| 16 | Max Drawdown Duration | Low | Time-to-recovery from worst DD. If >3 months, psychological tilt risk is high regardless of dollar DD |

---

## Maintenance

### Periodic Regression Test (every 5 sessions)
- [ ] Run full marathon with current config
- [ ] Compare P&L, Max DD, Sharpe, WR, trade count against baseline ($615,220 / -$6,356 / 14.55 / 93.4% / 6,950)
- [ ] If any metric drifts >1%, investigate before proceeding

---

## Live Trading Prep (Future)

Operational and psychological discipline items for when the strategy goes live. Not backtest-able.

- Unit system — track P&L in units ($200 = 1U) instead of dollars
- Closing Line Value — check credit vs mid-price 15 min after entry to track fill quality
- EV over Results — evaluate trades on rule-adherence, not just outcome
- Bankroll segregation — separate trading and personal capital
- Monthly re-buy limit — -10% monthly circuit breaker, paper trade to return
- Tilt sensor checklist — "Am I trading the signal or emotion?"
- C-Game rules — if feeling off, trade 1 contract only
- Post-game muck review — log skipped trades to check if risk aversion is too high
- Bankroll milestone reward — withdraw $1k per $25k profit
- Mid-point + $0.05 fill discipline — limit orders at mid + $0.05, cancel if unfilled after 60s
- Variance insurance — buy OTM puts with 5% of weekly profits as tail-risk hedge
- Table positioning / morning edge — track which entry times have best edge; weight early entries more
- Steam / institutional flow — watch for sudden credit spikes as institutional activity signal
- Backer mindset — treat account as a staked bankroll; report P&L as ROI on capital
- Prop bets / incubation — paper-trade experimental configs alongside live for N days before adopting
- Walk away number — pre-set daily loss limit where you stop trading entirely (psychological, distinct from DAILY_SL black-swan backstop)
- Sunk cost trap — don't revenge-trade or override signals after a loss day; next day is independent
- GTO vs exploitative — default to baseline rules (GTO); only deviate when edge is proven with data (≥30 samples)
- Minimum sample size — don't act on any pattern with <30 observations; document threshold before testing
- Betting correlation check — track correlation between consecutive day outcomes to detect regime shifts
- Delta-neutral audit — check portfolio delta every 30 min; if directionally exposed, rebalance or exit
- House Edge recap — weekly Gamma vs Theta audit; if gamma losses exceed theta collected, house edge is gone
- VVIX divergence intraday — if VVIX rising while VIX flat, exit spreads early (smart money buying tail insurance)
- VVIX crush entry — enter only after VVIX peaks and turns down (9-EMA cross); sell the peak of fear
- VVIX-based intraday SL — close spreads if VVIX jumps 5%+ in 15 min (fire alarm before price crash)
- Morning VVIX check — if VVIX up >3% pre-bell (9:00–9:30), delay first entry to 10:00 AM
- VVIX-conditional early close — exit by 2:30 PM when VVIX elevated (gamma/vanna combo is late-day killer)
- VVIX impulse filter — cancel pending limit orders on VVIX spike candle (dealers widening spreads)
- VIX9D spike entry — wait for 10% VIX9D spike + first 1-min down candle before entering (sell the vol crush)
- VIX9D stop-start rule — move SL to breakeven if VIX9D jumps 1.5+ pts intraday (probability of touch shifting)
- VIX9D lunchtime fade — close positions if VIX9D curls up at noon after falling all morning (afternoon gamma amplifier)
- VIX9D/SPX correlation reversal — SPX down + VIX9D down = fear exhaustion; aggressive PUT spread entry signal
- If-Then scenario planning (MTS) — pre-define rules for ambiguous price action: "If price closes under X, I exit; if it reclaims Y, I hold"
- 9:30 AM skew check — compute (25Δ put IV − 25Δ call IV) / ATM IV at open. >0.35 = rich skew (favorable for selling), <0.15 = cheap (cautious)
- Multi-timeframe zoom-out (MTS) — before reacting to single bar, check higher timeframe structure; one bar may be noise in an intact trend
- Confirmation patience (MTS) — let next bar confirm/negate concern before exiting; don't trade bar-to-bar emotionally
- SL distribution awareness (Taleb) — stop-losses lower variance but amplify skew/kurtosis of P&L distribution; evaluate SLs holistically, not just on max DD
- Weekly SHAP audit — run SHAP on trade features monthly to track which variables drive wins/losses; catch regime drift early
- If-Then scenario planning (MTS) — pre-define rules for ambiguous price action: "If price closes under X, I exit; if it reclaims Y, I hold"
- 9:30 AM skew check — compute (25Δ put IV − 25Δ call IV) / ATM IV at open. >0.35 = rich skew (favorable for selling), <0.15 = cheap (cautious)
- Multi-timeframe zoom-out (MTS) — before reacting to single bar, check higher timeframe structure; one bar may be noise in an intact trend
- Confirmation patience (MTS) — let next bar confirm/negate concern before exiting; don't trade bar-to-bar emotionally
- SL distribution awareness (Taleb) — stop-losses lower variance but amplify skew/kurtosis of P&L distribution; evaluate SLs holistically, not just on max DD
