# Zenith Fade CCS — Research TODO

## Priority 1-3: COMPLETED

See RESEARCH.md for full results.

## Priority 4: Further Optimization — MOSTLY DONE

- [x] **Spread width with intraday SL**: Width 10-20 plateau. Width 15 peaks P&L ($41.7k BS)
- [x] **Score weight re-optimization**: W_VAR=1.4 optimal. W_ROCKET has no effect (rocket too small)
- [x] **Real option pricing**: Implemented. $30k P&L, Sharpe 5.84 (vs $40k with BS). Re-optimized offset to 30pt
- [x] **Risk amount**: Current $600 is fine with max loss capped at $152
- [x] **Two-day hold**: REJECTED. 1-day is optimal; multi-day decays rapidly
- [x] **Intraday exit timing**: REJECTED. 16:00 (expiry) is optimal due to theta decay
- [x] **Monte Carlo**: 0% risk of ruin at all account sizes. Max loss $152/trade
- [x] **Walk-forward**: Train Sharpe 4.01, Test Sharpe 9.23 — NOT overfit
- [x] **EMA confluence**: Bearish EMA → 76.8% WR, Sharpe 8.31 (vs 65.7% bullish). Optional filter
- [x] **MEDS correlation**: -0.049 correlation → strategies are independent. Great diversification
- [x] **Put spread version**: Score<=40 → $18.3k, 78.2% WR, Sharpe 12.27 (BS). Both directions work!
- [ ] **FOMC-specific SL**: Test tighter SL on FOMC days instead of skipping (low priority — FOMC skip is flat)
- [ ] **Classic + score combo**: Use Dynamic Score with minimum Classic filter requirement (low priority)

## Priority 5: Signal Enhancement — MOSTLY DONE

- [x] **Bidirectional strategy**: ACCEPTED. $48.3k P&L, Sharpe 7.20, 669 trades. +61% vs call-only
- [x] **Kelly criterion**: Full Kelly 45.7%. EV $72/trade (12% edge). Quarter-Kelly = 11.4% of account
- [x] **ML enhancement**: REJECTED. RF 72.1% barely beats 68.2% baseline. Edge is structural, not feature-based
- [ ] **Volume proxy**: Get SPY or CBOE volume for proper dRelVol signal
- [ ] **Post-signal filter sweep**: Stoch>=80 → Sharpe 10.03. Sweep stoch + distSMA combos

## Priority 6: Advanced Research

- [ ] **Regime analysis**: HMM to detect favorable/unfavorable market regimes
- [x] **Slippage estimation**: Done. Real pricing already uses worst-case bid-ask fills. Mid-fill would improve P&L
- [ ] **Extended history**: Get daily SPX OHLC from 2000 for longer backtest
- [ ] **Live trading prep**: Design execution workflow for OptionFlow integration
- [ ] **Combine with MEDS**: -0.049 correlation, $373 combined daily avg P&L
