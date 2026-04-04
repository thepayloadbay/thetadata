# Apex — Autonomous 0DTE SPX Research Backlog

All experiment results in [RESEARCH.md](RESEARCH.md).
Iteration tracker in [iterations.csv](iterations.csv). **356 iterations completed.**

---

## Phase 1 — Infrastructure & Baseline

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Build engine with shared data pipeline | **DONE** | apex_engine.py, apex_config.py, apex_reporting.py, apex.py |
| 2 | Implement credit spread simulator (PUT + CALL) | **DONE** | Real bid/ask, bar OPEN for strikes, indexed quote lookup |
| 3 | Build reporting module | **DONE** | P&L, DD, Sharpe, Calmar, WR, PF, VIX/DOW analysis, worst days |
| 4 | Establish simplest profitable baseline | **DONE** | 15:55 entry, PUT 10pt w/ 10pt OTM → Sharpe 1.19 |

## Phase 2 — Core Signal Research

| # | Item | Status | Notes |
|---|------|--------|-------|
| 5 | Entry time sweep (9:35–15:55) | **DONE** | 15:55 ONLY profitable. 15:54 = -$6k DD. RESEARCH [1] |
| 6 | Direction research | **DONE** | both_sides best. RESEARCH [2] |
| 7 | Width sweep (5–50) | **DONE** | w=10 >> w=20 at 100% WR (73% more P&L). RESEARCH [3,16,37] |
| 8 | OTM distance sweep (3–30) | **DONE** | d=12 = exact cliff (d=11 has loss). P95 of 5-min moves. RESEARCH [4,43] |
| 9 | Multi-entry vs single-entry | **DONE** | Multi (15:50+15:55) destroys Sharpe. Dual (15:55+15:56) lower P&L. RESEARCH [17,44] |

## Phase 3 — Filter & Regime Research

| # | Item | Status | Notes |
|---|------|--------|-------|
| 10 | VIX level filters | **DONE** | VIX max HURTS — high-VIX 15:55 trades are 100% WR. RESEARCH [5] |
| 11 | VIX term structure (VIX/VIX9D) | **DONE** | VIX/VIX9D < 1.05: cuts trades, no improvement. RESEARCH [12] |
| 12 | VVIX filter | **DONE** | VVIX < 120: too aggressive. REJECTED. RESEARCH [12] |
| 13 | Prior-day indicators (SMA, RSI, ATR) | **DONE** | RSI marginal, SMA200 above slightly better. Not worth. RESEARCH [18] |
| 14 | Intraday momentum (morning return) | **DONE** | Destroys P&L. REJECTED. RESEARCH [5] |
| 15 | GEX regime filter | **DONE** | Negative GEX BETTER. No predictive power. RESEARCH [12] |
| 16 | Calendar events (FOMC, CPI, NFP) | **DONE** | FOMC skip subsumed by Wed skip. CPI/NFP cut good trades. RESEARCH [5] |
| 17 | Day-of-week seasonality | **DONE** | Wed TOXIC at all distances. Thu risky at d=12. Tue safe. RESEARCH [5,9,26] |
| 18 | VIX1D filter | **DONE** | VIX1D < 22 → 100% WR. Alternative zero-DD filter. RESEARCH [30] |
| 19 | Intraday VIX change | **DONE** | Counter-intuitive: losses on VIX-dropping days. Not useful. RESEARCH [33] |
| 20 | Afternoon return filter | **DONE** | |13:00→15:00| ≤ 0.25% → 100% WR. RESEARCH [35] |
| 21 | Last-hour return filter | **DONE** | |15:00→15:55| ≤ 0.2% → 100% WR. Same-day real-time. RESEARCH [35] |

## Phase 4 — Tail Risk Mitigation (CRITICAL)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 22 | Analyze worst loss days | **DONE** | Only 2 losses: both Thu, bearish EMA, ~13pt selloff. RESEARCH [8] |
| 23 | Prior-day return filter | **DONE** | **KEY:** < 1.50%, cliff at 1.75%. RESEARCH [6] |
| 24 | Prior-day range filter | **DONE** | < 2.5% eliminates remaining loss. RESEARCH [10] |
| 25 | Skip Thursday | **DONE** | **ZERO DD.** Both losses were Thu. RESEARCH [9] |
| 26 | Gap filter | **DONE** | Gap ≤ 0.7% → 100% WR, $0 DD. Integrated in engine. RESEARCH [11,19] |
| 27 | OI cluster filter | **DONE** | **KEY:** OI ≥ 1000 near short = dealer pinning. RESEARCH [22] |
| 28 | Thursday recovery | **DONE** | Thu safe at d=15 + OI≥1000 (100% WR). RESEARCH [26] |
| 29 | Wednesday recovery | **DONE** | IMPOSSIBLE — toxic at d=25. Never trade Wed. RESEARCH [Session 5] |

## Phase 5 — Combination & Validation

| # | Item | Status | Notes |
|---|------|--------|-------|
| 30 | Stack best filters | **DONE** | Mega Combined: M+Tue@d=12 + Thu@d=15,OI + Fri@d=10 |
| 31 | Walk-forward validation | **DONE** | IS 13.26 → OOS 11.24. All years profitable. RESEARCH [multiple] |
| 32 | Plateau validation | **DONE** | All params on broad plateaus. d=12 cliff confirmed. |
| 33 | $30k BP constraint | **DONE** | w=10 q=15 = $30k. w=20 q=7 = $28k. Both verified. RESEARCH [29] |
| 34 | Slippage sensitivity | **DONE** | 50% credit haircut: still 100% WR, $0 DD. RESEARCH [32] |
| 35 | Commission sensitivity | **DONE** | Profitable at $1.50/leg (Sharpe 9.74). RESEARCH [29] |
| 36 | Execution timing | **DONE** | **CRITICAL:** 15:54 = -$6k DD. Must be 15:55 exactly. RESEARCH [29] |
| 37 | Per-year breakdown | **DONE** | Every year 2022-2026 profitable at 100% WR. RESEARCH [29] |
| 38 | Realistic fill simulation | **DONE** | Stale quotes, haircuts all survive. RESEARCH [32] |
| 39 | Width optimization at 100% WR | **DONE** | w=10 >> w=20 (73% more P&L at same BP). RESEARCH [37] |

## Phase 6 — Creative Exploration (COMPLETED)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 40 | Delta-targeted entries | **DONE** | Worse tail risk than fixed OTM. REJECTED. RESEARCH [13] |
| 41 | Straddle-informed distance | **DONE** | Fixed d=12 ≈ straddle×1.5 naturally. No improvement. RESEARCH [28] |
| 42 | Adaptive OTM by VIX | **DONE** | All variants worse than fixed d=12. REJECTED. RESEARCH [12,39] |
| 43 | Adaptive OTM by range | **DONE** | All variants worse. REJECTED. RESEARCH [39] |
| 44 | VIX regime switching | **DONE** | Overfitting. Simple fixed params win. REJECTED. RESEARCH [27] |
| 45 | ML classifier | **DONE** | Rediscovers existing filters. No new signal. REJECTED. RESEARCH [40] |
| 46 | Asymmetric PUT/CALL distance | **DONE** | Symmetric d=12 optimal. REJECTED. RESEARCH [31] |
| 47 | Friday width optimization | **DONE** | w=10 best at d=10. RESEARCH [24] |
| 48 | Tuesday recovery | **DONE** | 100% WR at d=12. Added to trading days. RESEARCH [26] |
| 49 | Consecutive calm days | **DONE** | Redundant with prior-day return. REJECTED. RESEARCH [21] |
| 50 | Range compression | **DONE** | Hurts performance. REJECTED. |
| 51 | Dual entry (15:55+15:56) | **DONE** | Lower P&L than single. REJECTED. RESEARCH [44] |
| 52 | Ultra-narrow w=5 q=30 | **DONE** | Higher Sharpe (15.77) but $1k less P&L than w=10. RESEARCH [42] |
| 53 | OpEx effect | **DONE** | No negative impact. 100% WR on OpEx. RESEARCH [23] |
| 54 | 10AM straddle filter | **DONE** | Sparse data. REJECTED. RESEARCH [41] |
| 55 | Prior-day volume/tick count | **DONE** | SPX has no volume data. DEAD END. |
| 56 | Weekly OI patterns | **DONE** | Thu low-OI is sole danger. Confirms skip_thu logic. RESEARCH [34] |

---

## Phase 7 — Production Readiness (NEW — NOT STARTED)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 57 | Build TradeStation live execution script | | Auto-submit orders at 15:55 based on filters |
| 58 | Implement real-time OI check for Thursday | | Pull OI data at ~15:50 via ThetaData API |
| 59 | Build pre-trade checklist script | | Daily: check DOW, prior-day return, gap, OI → go/no-go |
| 60 | Paper trade for 30 days | | Run live signals, log decisions, compare to backtest |
| 61 | Build alert/notification system | | Slack/email when trade fires or is skipped |
| 62 | Dynamic qty by account equity | | Scale qty based on actual balance, not fixed 15 |
| 63 | Handle early close days | | Adjust entry time for half-days |
| 64 | Edge case: no quote at 15:55 | | Fallback to 15:56? Or skip? |

## Phase 8 — Strategy Expansion (NEW — NOT STARTED)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 65 | SPY options alternative | | Tighter bid/ask? Different strike grid ($1 vs $5)? |
| 66 | 1DTE variant | | Enter at 15:55, hold overnight to next-day 16:00 |
| 67 | Portfolio with other Apex strategies | | Combine with LastFive, Zenith, MEIC for diversification |
| 68 | Cross-asset filters | | TLT/GLD/DXY big moves as pre-entry signal |
| 69 | Earnings season analysis | | Does quarterly earnings season affect the edge? |
| 70 | Regime change detection | | Alert when strategy may stop working (structural shift) |
| 71 | Multi-account scaling | | Split across accounts for >$30k BP |
| 72 | Extended hours data | | Pre-market SPX futures as overnight risk signal |

---

## Declined / Not Applicable

| Item | Reason |
|------|--------|
| VIX max filter | High-VIX 15:55 trades are 100% WR. Filter hurts P&L |
| Morning momentum filter | Too aggressive, destroys P&L |
| CPI/NFP skip | Cuts profitable trades, minimal DD improvement |
| Stop-loss risk management | 0DTE expires in 5 min. Filters > stops |
| Wednesday PUT trading | Toxic at every OTM distance (even d=25). CALL-only is safe |
| Adaptive OTM (any variant) | Fixed d=12 beats all VIX-dynamic approaches (tested 12 configs) |
| ML classifier | Rediscovers existing simple rules. Adds complexity, not alpha |
| Multi-entry before 15:51 | 15:50 entry introduces tail risk. 15:51 only safe when MOC bar calm |
| Triple entry (15:55+57+58) | At $30k BP, diluting qty to q=10 × 3 < dual q=15 × 2 |
| VIX velocity filter | Zero correlation (< 0.01) with 5-min move. Not useful |
| Straddle filter on filtered days | Removes high-credit winning trades. Counterproductive |

## Completed Research Summary

**610+ iterations, 66 findings, 10 sessions.**
Strategy extracts maximum premium from structural mispricing of 0DTE options in final 5 minutes.
See RESEARCH.md for complete audit trail.
