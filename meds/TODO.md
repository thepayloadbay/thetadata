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
| 6 | DSPX Dispersion Index [24] | Medium | CBOE (availability unclear) | Market crowding signal; check data access first |

### Medium Priority — Untested Tactical Ideas

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 7 | ~~FOMC VIX 15–20 tighter SL~~ | ~~Medium~~ | **REJECTED** — marathon sweep -$500 to -$3,000: all levels cost P&L (-$4k to -$16k), DD unchanged. Same root cause as blanket VIX 15–20 SL |
| 8 | ~~VIX-range econ day SL sweep~~ | ~~Medium~~ | **REJECTED** — cross-tabbed all events × VIX buckets; 4 net-negative combos found but marathon skip costs -$8.7k P&L (Sharpe +0.50 not worth it) |
| 9 | ~~Econ day adjacency analysis~~ | ~~Low~~ | **DONE** — no T-1/T+1 pattern; FOMC/PCE weakness is event-day-only, neighbors are normal WR |
| 10 | VIX-conditional PCE skip | Low | PCE has 69% WR (p=0.00002 vs 91.8% baseline). Test: skip PCE only when VIX <15 or 25–30 (weak zones). Full skip costs $17k; conditional may preserve most P&L while filtering worst days |
| 11 | ~~Strike distance decay signal (delta gate)~~ | ~~High~~ | **REJECTED** — delta chg=0.000 at worst entries on 4/5 top loss days (V-shape reversals invisible to signal). Best threshold (0.05) saves $6.5k, costs $97k wins. 20.8:1 false positive ratio |
| 12 | Entry window cutoff by VIX range (Option 3b) | Low | For VIX 15–20, stop entries at 11:30 instead of 12:45. Sweep over cutoff times |
| 13 | Tighter per-trade SL when day is already negative (Option 3c) | Medium | Once daily P&L < -$500, subsequent positions use -$150 SL. Targets mixed-result days |
| 14 | Halt entries on intraday trend reversal (Option 3f) | High | EMA cross / VWAP cross / rolling high break as entry suppression signal |
| 15 | MAX_OTM_DISTANCE cap | Low | Skip entries where strike >75pt OTM. Likely overlaps VIX 25–30 zone. From Finding 5 |
| 16 | Widen danger zone to VIX 13.5–15.0 | Low | Extend dynamic SL coverage to fill unprotected gap. From Finding 2 |
| 17 | "Wonging" Entry — wait for 0.3% adverse move [30] | Medium | Enter only after SPX moves against spread direction. Gets better credit |
| 18 | ~~Bid/ask spread width filter [33]~~ | ~~Low~~ | **REJECTED** — SPXW 0DTE spreads discrete ($0.05/0.10/0.15 cover 99%). Wide-spread entries still 91.6% WR, $86.6 avg. Any filter costs P&L (>$0.10 threshold = -$137k). Correlates with VIX 25-30, already handled by dynamic SL |
| 19 | Pot Odds filter — MIN_NET_CREDIT to 3.5% of width [34] | Low | Raise MIN_NET_CREDIT from $0.55 to $0.70. Related to existing sweep |
| 20 | Chop Rule — close at 80% max profit with 2h+ remaining [13-ideas] | Medium | Similar to DAILY_TP but % capture + time filter. All fixed TP levels rejected but this framing untested |
| 21 | "Semi-Bluff" half-size on near-EMA-cross [41] | Medium | EMA alignment as sizing trigger (not gate). EMA as gate rejected; sizing untested |
| 22 | Hard time exit — close all by 3:15 PM [gamma] | Low | Hour-15 losses avg -$952 (8x noon). Close positions early to avoid 0DTE gamma spike. Backtestable |
| 23 | Time-decayed position sizing [gamma] | Medium | Reduce qty for later entries (e.g., qty=1 after noon). Distinct from Kelly (VIX-based) |
| 24 | Acceleration SL — speed-based exit [gamma] | Medium | Exit on rapid adverse move (e.g., 10pt in 5min) regardless of price level. Distinct from static SLs |
| 25 | Dynamic WIDTH by entry time [gamma] | Low | Narrower spreads (10pt) for later entries to cap gamma/convexity risk |
| 26 | VIX/GEX divergence signal [gamma] | Medium | VIX rising + GEX falling = danger. Divergence may add info even though GEX alone is redundant (#5) |
| 27 | VVIX-adjusted strike distance [vvix] | Medium | Add 5pt to MIN_OTM per 10pt VVIX above 100. Dynamic OTM buffer based on vol-of-vol |
| 28 | VVIX-adjusted minimum credit [vvix] | Low | Demand higher MIN_NET_CREDIT when VVIX elevated. Higher VVIX = higher risk = need more premium |
| 29 | VIX/VVIX divergence signal [vvix] | Medium | VIX new high + VVIX lower high = vol exhaustion, hold positions. Opposite divergence = exit early |
| 30 | VVIX mean reversion sizing [vvix] | Medium | At 52-week VVIX highs, sell wider/more aggressively for premium. Contrarian vol-of-vol signal |
| 31 | VVIX term structure [vvix] | Medium | Short-term VVIX > long-term = panic regime. Needs CBOE term structure data; check availability |
| 32 | Systemic reset indicator [vvix] | Low | VVIX drops 10%+ in a day after crash week = green light to resume full size. Post-crisis re-entry signal |
| 33 | VIX9D/VIX term structure filter [vix9d] | Medium | Inversion (VIX9D > VIX) as skip signal; gap narrowing as SL tightener; contango (VIX/VIX9D > 1.15) as size-up; slope steepening as green light. Download VIX9D from CBOE |
| 34 | VIX9D-based dynamic strike distance [vix9d] | Medium | MIN_OTM = Base + VIX9D × 2. Auto-widens in high near-term vol (VIX9D=15→30pt, VIX9D=25→50pt) |
| 35 | VIX9D regime sizing [vix9d] | Medium | <15 small, 15–25 full, >25 reduce/skip. More granular than VIX-based Kelly; 9-day horizon matches 0DTE better |
| 36 | VIX9D vs Realized Vol edge [vix9d] | Medium | Size up when VIX9D >> 9-day realized vol (selling overpriced insurance); reduce when VIX9D < realized (underpaid) |
| 37 | VIX9D convergence filter [vix9d] | Low | Skip PUT spreads when SPX rallying but VIX9D flat/rising (fake rally detection; "bluff" signal) |
| 38 | VIX1D/VIX9D spread [vix9d] | Low | Track theta gap; VIX1D >> VIX9D = prime time for 0DTE premium selling. Needs VIX1D data from CBOE |

### Low Priority

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 39 | Hurst Exponent regime filter [18] | Medium | Classifies movement *type* not magnitude. Novel but consistent pattern suggests low odds |
| 40 | QQQ/SPY Correlation [22] | Low | Within-equity signal. Weak theoretical basis vs cross-asset [21] |
| 41 | ~~Dynamic Delta-Adjusted Strike Distance [25]~~ | ~~Medium~~ | **REJECTED** — delta paradox: losses have LOWER |delta| than wins. |delta|<0.10 = no-op (never triggers under MIN_OTM=30). |delta|<0.08 blocks 37.5% of trades, catches 8.8% of losses, costs -$280k net |
| 42 | Iron Condor on winning Put spread [31] | High | Open Call side to lock in profit on middle zone. Complex position management |

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
