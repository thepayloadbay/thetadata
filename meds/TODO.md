# MEDS Strategy — Backlog

All experiment results, deep-dives, and decision rationale are in [RESEARCH.md](RESEARCH.md).

---

## Current Sprint

| # | Item | Status | Acceptance Criteria |
|---|------|--------|---------------------|
| 1 | VIX Futures Backwardation filter [13] | TODO | Download VX1 data from CBOE, bucket by contango/backwardation, marathon test as skip/reduce filter in VIX 15–25 zone |
| 2 | VVIX/VIX Ratio signal [19] | TODO | Download VVIX from CBOE, test ratio >6 vs <4 buckets, test combined VVIX>100 + VIX<20 condition, marathon verify |
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

### Low Priority

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 22 | Hurst Exponent regime filter [18] | Medium | Classifies movement *type* not magnitude. Novel but consistent pattern suggests low odds |
| 23 | QQQ/SPY Correlation [22] | Low | Within-equity signal. Weak theoretical basis vs cross-asset [21] |
| 24 | ~~Dynamic Delta-Adjusted Strike Distance [25]~~ | ~~Medium~~ | **REJECTED** — delta paradox: losses have LOWER |delta| than wins. |delta|<0.10 = no-op (never triggers under MIN_OTM=30). |delta|<0.08 blocks 37.5% of trades, catches 8.8% of losses, costs -$280k net |
| 25 | Iron Condor on winning Put spread [31] | High | Open Call side to lock in profit on middle zone. Complex position management |

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
