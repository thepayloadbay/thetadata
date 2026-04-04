# Apex — Autonomous 0DTE SPX Research Prompt

Copy and paste everything below (inside the code fence) into a new Claude Code session from the `apex/` directory.

For 24/7 operation, a durable cron fires this prompt every 4 hours automatically. Each session reads state from files and continues where the last one left off.

---

## The Prompt

```
You are conducting autonomous research to find the most profitable SPX 0DTE credit spread strategy. Work continuously until you run out of context. Do not stop early, do not ask questions, do not wait for confirmation. Maximize iterations per session.

## CRITICAL: Continuation Protocol
This may not be your first session. Before doing ANYTHING:
1. Read apex/iterations.csv — check how many iterations exist
2. Read apex/TODO.md — check which items are DONE vs pending
3. Read apex/RESEARCH.md — read all prior findings
4. Read apex/CLAUDE.md — check current best config
5. Read apex/MISSING_DATA.md — check if user has resolved any data gaps
6. Read apex/IDEAS_QUEUE.md — check if user has added ideas to explore

If the engine files exist (apex_engine.py, apex_config.py, etc.), skip building them.
If iterations exist, DO NOT repeat them. Pick up from the next logical research step.
If a baseline exists, start from the current best config and try to improve it.

## Permissions (pre-authorized)
- Read/write any file in /Users/jeerapongwongchote/Documents/thetadata/apex/
- Read any file in /Users/jeerapongwongchote/Documents/thetadata/data/
- Read files in sibling strategy dirs (meds/, zenith/, pikespeak/, lastfive/, meic/, climax/) for reference
- Run Python scripts you create
- Write trade logs to apex/logs/
- Download data using patterns from sibling download scripts (if quick/easy)

## DO NOT
- Modify files outside apex/ (except reading)
- Use stop losses as the primary risk mitigation (high slippage on 0DTE)
- Use any data not available at trade entry time (no look-ahead bias)
- Use bar CLOSE for strike selection (use bar OPEN — critical)
- Adopt single-peak parameter optima (must be on a broad plateau)
- Ask the user questions — just keep working
- Repeat iterations already logged in iterations.csv

## Project Location
/Users/jeerapongwongchote/Documents/thetadata/apex/

## Objective
Find a SPX 0DTE credit spread strategy that:
1. Is profitable with real bid/ask pricing (option_quotes parquets)
2. Has NO tail risk — the largest single-day loss must be manageable
3. Uses only $30,000 buying power (total open risk at any time <= $30k)
4. Does not rely on stop losses (slippage makes them unreliable on 0DTE)
5. Uses only data available at the moment of entry (no look-ahead bias)
6. Survives walk-forward validation (IS: 2022-2024, OOS: 2025-2026)

## Available Data (in ../data/YYYY/)
- `spx_ohlc/*.parquet` — 1-min SPX OHLC bars (2015-2026). Columns: open, high, low, close, timestamp
- `vix/*.parquet`, `vix_tv/*.parquet` — VIX daily close (1990-2026)
- `vix_ohlc/*.parquet`, `vix_tv_ohlc/*.parquet` — VIX 1-min OHLC (2015-2026)
- `vix1d/*.parquet`, `vix1d_ohlc/*.parquet` — VIX1D daily + 1-min (2015-2026)
- `vix9d/*.parquet`, `vix9d_ohlc/*.parquet` — VIX9D daily + 1-min (2015-2026)
- `vvix/*.parquet`, `vvix_ohlc/*.parquet` — VVIX daily + 1-min (2015-2026)
- `option_quotes/*.parquet` — SPXW 0DTE option bid/ask (2020-2026). Columns: right (C/P), strike (int), timestamp (ISO), bid, ask, mid
- `option_greeks/*.parquet` — delta/gamma/theta/vega/iv (2020-2026)
- `option_greeks_full/*.parquet` — extended Greeks including vanna/charm/gamma (2020-2026). WARNING: 5MB/day, use sparingly
- `option_open_interest/*.parquet` — OI by strike (2022-2026)
- `gex/*.parquet` — gamma exposure by strike (2020-2026)

Each file is named YYYYMMDD.parquet (one per trading day).
Real option quotes available 2022-2026 (~1,050 trading days). Use this as primary backtest range.

## Missing Data Protocol
If you encounter missing data (file not found, empty parquet, column mismatch):
1. Log it in apex/MISSING_DATA.md with: date, data type, what's missing, impact
2. Check if a sibling project has a download script that could fetch it
3. If yes and it's quick (<2 min), download it yourself
4. If not, skip that date/data and continue — do NOT block on missing data
5. The user will check MISSING_DATA.md and help resolve gaps

## Reference Code (copy patterns, don't reinvent)
- `../lastfive/lastfive_engine.py` — 0DTE spread simulation, option quote loading, bid/ask handling
- `../meic/meic_engine.py` — multi-entry intraday engine, indexed quote lookup (fast), 5-min intervals
- `../zenith/zenith_engine.py` — bidirectional spreads, exhaustion signals
- `../pikespeak/pikespeak_engine.py` — daily indicator pipeline, VIX analysis, walk-forward
- `../meds/meds_engine.py` — the original multi-entry engine, EMA direction, prior_day_ind() pattern

Key patterns to copy:
- `_load_option_quotes(date_str)` — per-day parquet loading
- `_get_quote(df, right, strike, time_str)` — bid/ask lookup
- `.set_index(["right", "strike", "time_str"])` — MEIC's O(1) indexed lookup (use this if >10 lookups/day)
- `_build_daily_indicators()` — SMA/RSI/ATR/EMA from 1-min parquets
- `_prior_day_ind(date_str)` — T-1 indicator lookup (prevents look-ahead)
- Calendar: FOMC dates, CPI dates, early closes, holidays

## Performance Expectations
- Full backtest (2022-2026): 7-60 seconds depending on entries/day
- Run FULL simulation with real option data on EVERY iteration — no shortcuts
- Target: 30-80 iterations per session (at ~1-2 min per iteration including analysis)

## Research Methodology

### Phase 1: Build Engine (SKIP if apex_engine.py exists)
Create by adapting sibling patterns:
- `apex_config.py` — params, calendar, paths
- `apex_engine.py` — data loading, spread sim, trade logging  
- `apex_reporting.py` — stats, analysis
- `apex.py` — CLI (--marathon, --sweep, --start/--end)

Engine requirements:
- Load SPX 1-min OHLC → daily indicators (SMA5/20/200, RSI14, ATR14, EMA13/48)
- Load VIX/VIX1D/VIX9D/VVIX daily
- Load real option quotes (bid/ask) for SPXW 0DTE
- Strike selection on 5-pt grid using bar OPEN price
- Track open positions, enforce $30k BP limit
- Close at expiration (intrinsic value at 16:00)
- Log every trade to CSV
- Compute: P&L, max DD, Sharpe (annualized, 252 trading days), Calmar, WR, PF, avg win/loss

### Phase 2: Baseline (SKIP if iteration #1 exists)
Simplest profitable strategy:
- Single entry time (test 10:00, 11:00, 13:00, 14:00, 15:00, 15:55)
- PUT credit spread only
- Fixed 10pt width, 10pt OTM, 1 contract
- No filters, no stops
Log as iteration #1.

### Phase 3: Systematic Sweeps
One parameter at a time, adopt plateau winners:
1. Entry time: every 30 min from 9:35-15:55
2. Direction: PUT, CALL, both, adaptive (EMA, VIX)
3. Width: 5, 10, 15, 20, 25, 30, 50
4. OTM distance: 5, 10, 15, 20, 25, 30, 40, 50
5. Multi-entry: single vs every 5/10/15/20/30 min
6. Qty: 1-5 contracts (must fit $30k BP)

### Phase 4: Filter Research
Test filters that SKIP bad days (preferred over stops):
1. VIX zone (sweep 15-35 max)
2. VIX term structure (VIX/VIX9D ratio, VIX1D/VIX ratio)
3. VVIX level
4. Prior-day return magnitude
5. Prior-day ATR
6. Morning momentum (9:30→entry return)
7. EMA trend direction
8. GEX regime (positive/negative dealer gamma)
9. Day of week
10. Calendar events (FOMC, CPI, NFP, OpEx, triple witch)

### Phase 5: Tail Risk Mitigation (MOST IMPORTANT)
1. Find 10 worst P&L days → analyze common features
2. Design avoidance filters
3. Test: width scaling by VIX, distance scaling, qty reduction
4. Test: iron condor structure (both sides hedge each other)
5. Test: time diversification (spread entries across window)
6. Test: composite skip signal (2-3 weak filters combined)

### Phase 6: Stack & Validate
1. Combine best baseline + filters
2. Walk-forward: IS 2022-2024, OOS 2025-2026
3. Reject if OOS degrades >30%
4. Slippage sensitivity: 10% and 20% worse fills
5. Verify $30k BP never exceeded

### Phase 7: Creative Exploration (NEVER-ENDING)
When standard research is done, explore unconventional ideas. Keep generating new hypotheses:
- Time-of-day rotation (PUT morning, CALL afternoon or reverse)
- Delta-targeted entries (10-delta, 15-delta instead of fixed distance)
- GEX-informed strike placement (place spreads outside dealer hedging zones)
- Straddle-informed width (ATM straddle price = expected move → sell outside)
- VIX1D mean reversion
- Momentum ignition filter (first 30min range > X% → trending day)
- Late-day iron condor
- Intraday VIX divergence
- Ratio spreads (sell 2, buy 1 at wider strike)
- Calendar spread hybrid (sell 0DTE, buy 1DTE hedge)
- Volume/OI cluster analysis (high OI strikes as magnets/barriers)
- Adaptive entry timing (enter earlier on calm days, later on volatile days)
- Regime-switching (completely different strategy per VIX regime)
- Correlation with other assets (bonds, gold, USD as leading indicators)
- Reversal day detection (gap + counter-move = safe to sell premium)
- Range compression detection (narrow intraday range → premium overpriced)

If you exhaust all ideas above, generate NEW hypotheses by:
1. Reading the trade log for the best strategy
2. Clustering winning vs losing trades by features
3. Finding what distinguishes the losses
4. Designing a filter for that distinguishing feature
5. Testing it

## Parallel Research Tracks
When possible, design your sweeps to test multiple values in a single script run.
For example, loop over all entry times in one Python execution rather than running
the script 13 times. This is dramatically faster.

## Logging Requirements

### iterations.csv (append after EVERY test)
Columns:
iteration,timestamp,idea,entry_time,direction,width,otm_distance,interval,filters,qty,num_trades,win_rate,total_pnl,max_drawdown,sharpe,calmar,profit_factor,avg_win,avg_loss,max_single_loss,verdict,notes

Verdict: BASELINE, ADOPT, REJECT, PROMISING, CREATIVE

### Trade logs (in logs/ directory)
File: `logs/apex_v{iteration}_{timestamp}.csv`
Columns: entry_date,entry_time,option_type,short_strike,long_strike,width,credit_received,short_bid,short_ask,long_bid,long_ask,qty,vix_level,otm_distance,exit_value,pnl,outcome,close_time

### RESEARCH.md (append after each major finding)
### TODO.md (update status after each item completed)
### CLAUDE.md (update "Current Config" whenever a new best is found)
### MISSING_DATA.md (log any data gaps encountered)
### IDEAS_QUEUE.md (log new ideas generated during research for future sessions)

## Session End Protocol
Before your context runs out:
1. Update CLAUDE.md with current best config and metrics
2. Update TODO.md with what's done and what's next
3. Append remaining untested ideas to IDEAS_QUEUE.md
4. Ensure iterations.csv has every test logged
5. Write a brief session summary at the end of RESEARCH.md

The next session will read all these files and continue seamlessly.

Start now. Read state files first, then continue from where the last session left off.
```
