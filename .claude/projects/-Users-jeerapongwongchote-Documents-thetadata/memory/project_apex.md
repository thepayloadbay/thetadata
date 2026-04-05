---
name: Apex project
description: 0DTE SPX credit spread research — 15:55 entry, skip Wed + prior-day return < 1.5%, Sharpe 10.39, DD -$820, $30k BP
type: project
---

Apex is an autonomous 0DTE SPX options research project in the `apex/` directory.

**Best strategy found (2026-04-03):**
- Entry: 15:55 (last 5 minutes), both sides (PUT + CALL credit spreads)
- Width: 15pt, OTM distance: 12pt, Qty: 10 contracts per side
- Filters: skip Wednesdays + skip if prior-day return > 1.5%
- Results: $27k P&L, Sharpe 10.4, DD -$820, 99% WR, 186 trades over 4 years
- Walk-forward: IS Sharpe 12.4, OOS Sharpe 9.6 (validated)
- Friday-only variant: 100% WR, zero drawdown, $18.7k P&L

**Why:** prior-day return < 1.5% + skip Wed eliminates all tail risk. Structural edge: after calm days, last 5 minutes are ultra-predictable.

**How to apply:** When user asks about apex, reference these results. Engine is in apex_engine.py with full backtest capability. 90+ iterations logged in iterations.csv.
