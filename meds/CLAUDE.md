# CLAUDE.md — MEDS Strategy Project

model: opus

## Project Goal

Backtest and optimize a **Multi-Entry Directional Spreads (MEDS)** strategy trading SPXW 0DTE credit spreads, using prior-day VIX change as the direction signal. The primary objective is to **maximize total P&L** while keeping max drawdown and risk-adjusted metrics (Sharpe, Calmar) strong. All research, parameter sweeps, and feature experiments are in service of this goal.

**Roadmap priorities:**
1. New signal research — find additional signals or filters that improve P&L
2. Risk management improvements — better drawdown control or position sizing
3. Eventually: live trading infrastructure on Tradestation once strategy is fully optimized

---

## Strategy Overview

**MEDS — Multi-Entry Directional Spreads**
- SPXW 0DTE credit spreads, entries every 20 min from 9:35–12:45
- Direction signal: prior-day VIX change (`dVixChgPct`). VIX fell → PUT spread; VIX rose → CALL spread
- Signal is locked in for the whole day (no intraday update)
- Main backtest file: `meds.py`
- Confirmed baseline (2022-01-03 → 2026-03-25): $612,012 P&L, DD -$6,356, Sharpe 14.15, WR 92.7%, 7,042 trades

---

## Environment

- **Historical data**: Thetadata
- **Broker (live)**: Tradestation
- **Standard backtest range**: 2022-01-03 → latest available date
- **Result format**: always present backtest results as a markdown table with columns: P&L, Max DD, Sharpe, Calmar, WR, Trades

---

## Behavior Rules

**Always show backtest results immediately when a run finishes** — don't ask "want me to show results?". Tail the log for summary stats and display them as soon as the process finishes.

**Never implement config changes based solely on sweep pool results** — sweep runners (pre-fetched day pools) produce ~2,800 trades vs the full run's ~6,954 due to EMA seeding not carrying across days. Always verify promising sweep findings with a full marathon backtest (`--marathon` flag) before acting. The CALL-no-SL sweep is a documented example: showed +$92k in sweep, was -$29k P&L with 3× drawdown in the full run.

**Explain changes before editing** — briefly state what is changing and why before making any code edits.

---

## Current Config (Confirmed Optimal)

| Parameter | Value | Reason |
|---|---|---|
| `WIDTH` | `20` | Spread width in points |
| `QTY` | `2` | Contracts per entry (BP-constrained) |
| `MIN_NET_CREDIT` | `0.55` | Calibrated to bid fills; effectively looser at mid |
| `MAX_NET_CREDIT` | `None` | Capping at any level always hurt P&L; high-credit days contribute ~$73k |
| `MIN_OTM_DISTANCE` | `30` | Critical — transformed baseline from ~$62k → $607k by eliminating close-in entries |
| `ENTRY_START/END` | `9:35–12:45` | Sweep-validated window |
| `ENTRY_INTERVAL` | `20 min` | Sweet spot; 5-min boosts P&L but doubles drawdown to -$66k |
| `COMMISSION` | `$0.50/contract/leg` | TradeStation rate |
| `ENABLE_DYNAMIC_SL` | `True`, `-$800` | Triggers in VIX danger zones; -$800 is optimal ($607,424 PnL vs -$500 at $596,788) |
| `ENABLE_EOM_SL` | `True`, `-$200` | EOM-only SL on last trading day of each month; costs -$202 P&L but cuts max DD from -$9,922 → -$6,894 (31%), Sharpe 12.35 → 13.99, Calmar 61.2 → 88.0 |
| `DAILY_TP` | `None` | Removing $750 cap added +$140k with same max DD |
| `ENABLE_ECON_FILTER` | `False` | CPI costs $29,790, NFP costs $36,214 to skip |
| `ENABLE_CALENDAR_FILTER` | `False` | All calendar events net profitable — skipping costs money |
| `ENABLE_KELLY_SIZING` | `False` (locked) | Requires ~$80k BP; current account ~$40k |
| `MARKET_HOLIDAYS` | FOMC and Triple Witching removed | Skipping FOMC costs $23,512; Triple Witching costs $12,212 |
| `DAILY_SL` | `-20000` | Black swan protection — never fired in 4yr backtest (worst day -$6,118); zero P&L cost |
| `VIX_MAX_FILTER` | `35.0` | Black swan protection — skips days VIX > 35; only 8 days in 4yr backtest; **marathon cost TBD** |
| `ENABLE_SL_GAP_REENTRY` | `True`, `60 min` | After batch SL fires, wait 60 min then allow re-entry; sweep confirmed 60 min optimal (+$4,458, Sharpe 14.15); DD unchanged |

---

## Account Constraints

- Available BP: ~$40,000
- Baseline qty=2 uses $38,860 peak BP — already at 97% of budget
- **Do not suggest increasing QTY above 2** without flagging this constraint
- Full Kelly sizing needs ~$77,720 peak BP → enable `ENABLE_KELLY_SIZING = True` only when account reaches ~$80,000+
- Full Kelly projected: +$560k P&L improvement over 4-year period (verified marathon run)

---

## Tested and Rejected

- **No SL on CALL days**: sweep said +$92k → full run gave -$29k, DD tripled to -$24k
- **Gap-down CALL SL (-$300)**: DD improved but P&L dropped -$69k
- **Month/direction SL rules**: costs -$49k vs baseline
- **Per-position trailing stop**: all combos worse than baseline; 93%+ expiry WR means trailing stops just cut winning premium
- **Per-position fixed SL** (-$200 to -$600 sweep): all levels rejected — same root cause. Costs $276k–$436k P&L and worsens DD. Positions that temporarily go negative mostly recover by EOD.
- **Premium buyback exit** ($0.10/$0.05): costs -$64k; freed BP almost never enables a new entry — positions hit threshold after 12:45 entry window closes, so capital resets at EOD anyway
- **Intraday Bayesian entry gate** (all thresholds $0–-$400): all worse than baseline; B/A spread noise floor (~-$150 to -$200) causes false fires on win days
- **Low/mid-VIX half sizing**: Sharpe improved slightly but cost -$3,902 P&L; not worth it
- **VIX rise deceleration filter**: Sharpe +2.5 but costs -$13k P&L; inconsistent year-to-year, likely overfitting
- **VIX magnitude filter**: skipping low-VIX-change days always costs P&L at every tested threshold
- **Skip VIX 25–30**: P&L -$10k vs baseline, max DD unchanged — not worth it
- **All direction alternatives** (RSI, MACD, gap, SMA200, momentum, stochastic): negative P&L, not statistically significant
- **EOM skip (no trades)**: costs -$5,774 P&L — EOM days are net positive so skipping them is worse than trading them with a SL
- **Pressure filter** (`ENABLE_PRESSURE_FILTER`): blocks entries when any short strike is within 27 pts of spot — costs -$50k P&L; same failure mode as Bayesian gate (fires on win-day positions that are OTM but nearby)
- **P/C OI ratio skip filter**: redundant with VIX_MAX_FILTER — the only extreme-ratio days (Aug 5 2024, Apr 4 2025) already had VIX > 35; moderate-ratio days are profitable quarter-end mechanical hedging
- **Prior-day VIX % change filter**: opposite of expected — days after large VIX spikes (>20%) have 92.3% WR and $927 avg P&L (best bucket); MIN_OTM_DISTANCE=30 provides sufficient buffer
- **Intraday VIX spike circuit breaker**: same pattern — large intraday VIX spikes average $826/day above baseline; Dec 18 2024 spike (57%) hit 20% threshold at 15:04 PM after entry window closed; dangerous days already caught by VIX_MAX_FILTER
- **VRP (Variance Risk Premium) half-sizing**: Q1→Q5 gradient exists ($553→$726/day) but too weak — half-sizing bottom 20% costs -$61k P&L to reduce DD by only $776; negative VRP days (premium "cheap") still avg $781/day with 86.7% WR
- **VIX 15-20 dynamic SL** (all levels -$1,500/-$3,000/-$5,000): catastrophic — costs $99k-$173k P&L, DD worsens. Zone has 2,871 trades at 97.8% WR; any SL fires on too many winning days
- **Calendar SLs (CPI/PCE/Pre-TW/Post-Hol)**: all 5 categories best with SL OFF at every tested level (-$100 to -$500). CPI costs up to -$31k, PCE -$12k. Calendar events are net profitable; SLs just cut winners. Only EOM SL confirmed positive.

---

## Key Performance Insights

- VIX 15–20 and 20–25 are the sweet spots (98%+ WR, $78–79/trade avg)
- VIX 25–30: 68% WR, $3/trade — protected by dynamic SL (53% of all losses live here)
- VIX <13: 85.5% WR, $55/trade — weakest, partially unprotected
- Max drawdown is now balanced between PUT (-$6,114) and CALL (-$6,356) after EOM SL; previously CALL-dominated
- Gap direction is NOT a useful filter — disagreement between VIX signal and gap actually has higher avg PnL

---

## CLI Tools

- `--marathon`: bypass all sweeps, run single full backtest
- `--skip-vix-lo` / `--skip-vix-hi`: SKIP_VIX_RANGE testing
