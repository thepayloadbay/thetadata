# MEDS Strategy — Backlog

All experiment results, deep-dives, and decision rationale are in [RESEARCH.md](RESEARCH.md).

---

## Current Sprint

| # | Item | Status | Acceptance Criteria |
|---|------|--------|---------------------|
| 1 | VIX Futures Backwardation filter [13] | TODO | Download VX1 data from CBOE, bucket by contango/backwardation, marathon test as skip/reduce filter in VIX 15–25 zone |
| 2 | VVIX/VIX Ratio signal [19] | TODO | Download VVIX from CBOE, test ratio >6 vs <4 buckets, test combined VVIX>100 + VIX<20 condition, marathon verify. Also test: VVIX hard ceiling (>115 skip), VVIX floor (<80 skip), VVIX percentile sizing (quartile-based 0.25x–1x) |
| 3 | MOVE/VIX Ratio cross-asset signal [21] | TODO | Download MOVE from FRED, compute prior-day ratio quintiles, marathon test as skip filter on extreme high-ratio days |

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
| 3 | VIX9D/VIX term structure filter [vix9d] | Medium | Inversion (VIX9D > VIX) as skip signal; gap narrowing as SL tightener; contango (VIX/VIX9D > 1.15) as size-up; slope steepening as green light. Download VIX9D from CBOE |
| 4 | Tighter per-trade SL when day is already negative (Option 3c) | Medium | Once daily P&L < -$500, subsequent positions use -$150 SL. Targets mixed-result days. No external data needed |
| 5 | RV/IV ratio regime filter [research] | Medium | 20-day realized vol / VIX as sizing signal. 0.5–0.8 normal (full size), >0.8 stress (reduce/tighten SL). Well-documented thresholds. Distinct from VIX9D vs RV (#9) — uses 30-day horizon |
| 6 | VIX-conditional PCE skip | Low | PCE has 69% WR (p=0.00002 vs 91.8% baseline). Test: skip PCE only when VIX <15 or 25–30 (weak zones). Full skip costs $17k; conditional may preserve most P&L while filtering worst days |
| 7 | VIX9D regime sizing [vix9d] | Medium | <15 small, 15–25 full, >25 reduce/skip. More granular than VIX-based Kelly; 9-day horizon matches 0DTE better |
| 8 | Widen danger zone to VIX 13.5–15.0 | Low | Extend dynamic SL coverage to fill unprotected gap. From Finding 2 |
| 9 | Entry window cutoff by VIX range (Option 3b) | Low | For VIX 15–20, stop entries at 11:30 instead of 12:45. Sweep over cutoff times |
| 10 | VIX9D vs Realized Vol edge [vix9d] | Medium | Size up when VIX9D >> 9-day realized vol (selling overpriced insurance); reduce when VIX9D < realized (underpaid). Consider Parkinson estimator (high-low range) instead of close-to-close RV for better 0DTE accuracy |
| 11 | VIX9D-based dynamic strike distance [vix9d] | Medium | MIN_OTM = Base + VIX9D × 2. Auto-widens in high near-term vol (VIX9D=15→30pt, VIX9D=25→50pt). Elegant but MIN_OTM=30 already works well |
| 12 | Non-standard delta strikes (9–12Δ) [research] | Medium | Test selling at 9–12 delta instead of fixed 30pt OTM. Research shows higher WR, smaller DD, smaller avg gain. Fundamentally different strike selection approach vs fixed distance |
| 13 | Time-decayed position sizing [gamma] | Medium | Reduce qty for later entries (e.g., qty=1 after noon). Distinct from Kelly (VIX-based). Limited impact since entry window ends 12:45 |
| 14 | Normalized skew richness signal [research] | Medium | (25Δ put IV − 25Δ call IV) / ATM IV. >0.35 expensive (good for selling), <0.15 cheap (reduce size). Different from BSM IV Skew (#4 tested) — uses normalized ratio with documented thresholds. Needs vol surface data |
| 15 | VIX/VVIX divergence signal [vvix] | Medium | VIX new high + VVIX lower high = vol exhaustion, hold positions. Opposite divergence = exit early. Fed research: VVIX negatively predicts tail risk hedge returns 3–4 weeks ahead |
| 16 | VVIX-adjusted strike distance [vvix] | Medium | Add 5pt to MIN_OTM per 10pt VVIX above 100. Dynamic OTM buffer based on vol-of-vol. Overlaps with VIX9D version (#11) |
| 17 | Acceleration SL — speed-based exit [gamma] | Medium | Exit on rapid adverse move (e.g., 10pt in 5min) regardless of price level. Needs intraday SPX tick data. High effort |
| 18 | MAX_OTM_DISTANCE cap | Low | Skip entries where strike >75pt OTM. Likely overlaps VIX 25–30 zone. From Finding 5 |
| 19 | "Wonging" Entry — wait for 0.3% adverse move [30] | Medium | Enter only after SPX moves against spread direction. Gets better credit. Needs intraday SPX tracking |
| 20 | VIX9D convergence filter [vix9d] | Low | Skip PUT spreads when SPX rallying but VIX9D flat/rising (fake rally detection). Speculative |
| 21 | VIX decomposition factor-based signal [research] | High | Use put slope (Factor 3 = real fear) vs parallel shift (Factor 2 = event prep) vs wing expansion (Factor 5 = tail risk) to classify VIX moves. Needs vol surface data; complex but most granular VIX signal possible |
| 22 | Halt entries on intraday trend reversal (Option 3f) | High | EMA cross / VWAP cross / rolling high break as entry suppression signal. Related signals tested poorly |
| 23 | VVIX mean reversion sizing [vvix] | Medium | At 52-week VVIX highs, sell wider/more aggressively for premium. Contrarian — goes against risk reduction |
| 24 | Dynamic WIDTH by entry time [gamma] | Low | Narrower spreads (10pt) for later entries. Limited value since entry window ends 12:45 |
| 25 | VVIX-adjusted minimum credit [vvix] | Low | Demand higher MIN_NET_CREDIT when VVIX elevated. Raising MIN_NET_CREDIT always cost P&L in sweeps |
| 26 | VIX/GEX divergence signal [gamma] | Medium | VIX rising + GEX falling = danger. GEX tested redundant with VIX; divergence speculative |
| 27 | Chop Rule — close at 80% max profit with 2h+ remaining [13-ideas] | Medium | Similar to DAILY_TP but % capture + time filter. All fixed TP levels rejected; this framing unlikely to differ |
| 28 | "Semi-Bluff" half-size on near-EMA-cross [41] | Medium | EMA alignment as sizing trigger (not gate). EMA as gate rejected; sizing variant speculative |
| 29 | VVIX term structure [vvix] | Medium | Short-term VVIX > long-term = panic regime. Needs specialized CBOE term structure data; availability unclear |
| 30 | Systemic reset indicator [vvix] | Low | VVIX drops 10%+ in a day after crash week = green light. Rare event (2–3 times in 4yr backtest) |
| 31 | VIX1D/VIX9D spread [vix9d] | Low | Track theta gap; VIX1D >> VIX9D = prime time for 0DTE. Needs VIX1D data; availability unclear |
| 32 | Pot Odds filter — MIN_NET_CREDIT to 3.5% of width [34] | Low | Raise MIN_NET_CREDIT from $0.55 to $0.70. Raising always cost P&L in sweeps |

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
| 28 | Hurst Exponent regime filter [18] | Medium | Classifies movement *type* not magnitude. Novel but consistent pattern suggests low odds |
| 29 | QQQ/SPY Correlation [22] | Low | Within-equity signal. Weak theoretical basis vs cross-asset [21] |
| 30 | ~~Dynamic Delta-Adjusted Strike Distance [25]~~ | ~~Medium~~ | **REJECTED** — delta paradox: losses have LOWER |delta| than wins. |delta|<0.10 = no-op (never triggers under MIN_OTM=30). |delta|<0.08 blocks 37.5% of trades, catches 8.8% of losses, costs -$280k net |
| 31 | Iron Condor on winning Put spread [31] | High | Open Call side to lock in profit on middle zone. Complex position management |

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
