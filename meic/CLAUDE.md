# CLAUDE.md — MEIC Strategy Project

model: opus

## Project Goal

Backtest and optimize **MEIC (Multi-Entry Intraday Credit spreads)** — afternoon SPX PUT credit spreads. The strategy exploits the afternoon settlement bias where SPX tends to stabilize or rally into the close.

---

## Strategy Overview

**MEIC — Afternoon SPX Put Credit Spreads**
- SPXW 0DTE put credit spreads, entries every 5 min from 14:00–15:30
- Direction: always PUT (afternoon naturally favors selling put spreads)
- 50pt wide, min $1.20 credit, 30pt min OTM distance
- Per-position SL at -$400, late SL tightened to -$300 after 15:15
- FOMC days skipped (worst loss days)
- **Confirmed optimized (2022-01-05 → 2026-03-24): $101,428 P&L, DD -$10,797, Sharpe 3.72, ~1,500 trades (8 stacked improvements)**

---

## Current Config (Confirmed Optimal — 2026-04-01)

| Parameter | Value | Reason |
|---|---|---|
| `DIRECTION_MODE` | `always_put` | PUT: $85k Sharpe 2.38 vs CALL: -$79k. Afternoon bias favors puts |
| `WIDTH` | `50` | 50-point wide spreads |
| `QTY` | `2` | Contracts per entry |
| `MIN_NET_CREDIT` | `1.20` | $120/contract minimum credit |
| `MIN_OTM_DISTANCE` | `30` | 30pts minimum OTM |
| `ENTRY_START/END` | `14:00–15:30` | Sweep: 14:00 start optimal. Sharpe 2.38 (vs 12:00 at 0.86) |
| `ENTRY_INTERVAL` | `5 min` | 5min optimal. 10min drops P&L -$50k |
| `PER_POS_SL` | `-$400` | Plateau -$300 to -$600. Combined with late SL: Sharpe 2.96 |
| `AFTERNOON_SL` | `-$300` after 15:15 | Tightens SL in final 15 min. DD -$16.5k→-$14.7k |
| `ENABLE_FOMC_SKIP` | `True` | FOMC days are worst loss days. DD -$20k→-$16.5k |
| `VIX_MAX_FILTER` | `35.0` | Black swan protection |
| `DAILY_SL` | `-$3000` | Safety net (never fires — per-pos SL catches everything) |

---

## Key Findings

### Direction Signal
- **PUT-only is the only profitable direction** for afternoon entries
- CALL spreads: -$79k (catastrophic). EMA: -$1.5k. Both-sides: -$13.5k
- Afternoon bias: market tends to stabilize/rally into close, destroying CALL premium

### Entry Timing
- 14:00-15:30 @5min is optimal (Sharpe 2.38)
- Broad plateau: 13:30-14:00 start all Sharpe >1.9
- 5min interval critical — 10min loses half the P&L
- Late entries (15:10-15:15) go slightly negative but contribute to overall edge

### Stop Loss
- Per-pos SL -$400 to -$500 is the broad plateau
- Combined with late SL -$300 after 15:15: Sharpe jumps to 2.96
- Daily SL never fires — per-pos SL prevents cascading losses

### VIX Analysis
- VIX 15-20: Best zone ($43.9k, 79.9% WR)
- VIX 25-30: Second best ($45.9k, 71.7% WR)
- VIX 30-35: Negative (-$2k, 56.7% WR)

---

## Tested and Rejected

- **Both-sides (iron condor style)**: -$13.5k, DD -$78k. CALL side destroys all PUT profit
- **EMA direction**: -$1.5k. EMA doesn't help in afternoon — structural PUT bias dominates
- **always_call**: -$79k. Catastrophic
- **Min credit $1.50**: Kills P&L ($60k→$97k). Too few entries
- **Entry end 15:00/15:05**: Costs P&L without proportional DD reduction
- **MTM gate -$400**: Costs -$10k P&L, only -$1k DD benefit
- **Econ event skip**: Costs -$25k P&L. CPI/NFP days are highly profitable!
- **Width 40**: Lower P&L ($75k) and worse Sharpe
- **Width 60**: Similar P&L but regresses DD reduction from late SL

---

## Behavior Rules

- Always show results immediately after backtest
- Parameter plateau rule: only adopt if broad plateau exists
- No look-ahead bias: all signals use closed bars only
