# Missing Data for PineScript Evaluation

## Data We Have (from ThetaData)
- SPX/SPXW option quotes (bid/ask/mid) — 1-min granularity
- SPX option Greeks (delta, gamma, theta, vega, IV)
- GEX (Gamma Exposure) by strike
- Open Interest by strike
- VIX daily close
- VVIX daily close
- VIX1D daily close
- VIX9D daily close
- SPX OHLCV daily and intraday

## Data Gaps Identified

| Data Needed | Required By Scripts | Available? | Source | Priority |
|-------------|-------------------|------------|--------|----------|
| VIX intraday (1-min) | VIX Crossing, VIX Reversal Scalper, VIX MTF Momentum | NO — only daily | ThetaData or CBOE | HIGH |
| NDX intraday | VIX Crossing | NO | ThetaData (no stock sub) | LOW |
| Volume tick-level | Volume Spread Analysis, Market Microstructure | PARTIAL — have 1-min bars | ThetaData | MEDIUM |
| Order flow / tape | Liquidity Hunter, Smart Money Concepts | NO | Not available via ThetaData | LOW |
| Futures data (ES) | Several SMC scripts | NO | Not subscribed | LOW |

## Action Items
- [ ] Investigate ThetaData VIX intraday data availability
- [ ] Check if we can derive VIX proxy from SPX option IV
- [ ] Assess if daily VIX is sufficient for most VIX-based filters
