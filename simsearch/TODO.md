# SimSearch — TODO & Research Backlog

## Active Work

- [x] Phase 1: Project setup, documentation, feature design
- [ ] Phase 2: Feature engineering pipeline (simsearch_features.py)
- [ ] Phase 3: Similarity engine (simsearch_engine.py)
- [ ] Phase 4: Prediction module (simsearch_predict.py)
- [ ] Phase 5: Validation framework (simsearch_validate.py)
- [ ] Phase 6: CLI & live mode (simsearch.py)
- [x] Phase 7: Run full pipeline + show results (51.3% direction, 61% at high-conf)

---

## Research Ideas (Prioritized)

### High Priority
- [ ] **Intraday shape matching (DTW)**: Compare first-hour price curves to find days with similar morning patterns. Hypothesis: morning shape predicts afternoon continuation/reversal
- [ ] **GEX regime similarity**: Days in same GEX regime (stable/fragile/gex_cushion/vex_cushion) should cluster. Test if GEX+VEX regime is the strongest single predictor
- [ ] **VIX term structure state**: VIX1D vs VIX9D vs VIX relationship captures fear timing. Contango/backwardation regimes predict different day types
- [ ] **IV skew fingerprint**: The put/call IV skew shape at open is a fingerprint of market positioning. Similar skew shapes → similar day outcomes?

### Medium Priority
- [ ] **Multi-day context window**: Include T-1 through T-5 features to capture momentum/trend context, not just point-in-time state
- [ ] **Weighted feature importance**: Use validation results to learn which features matter most for similarity. Start uniform, then optimize
- [ ] **Cluster analysis**: K-means or DBSCAN on feature vectors to discover natural "day types" (e.g., low-vol grind, morning reversal, afternoon selloff)
- [ ] **Calendar effects**: Encode FOMC proximity, OPEX, month-end, quarter-end as features
- [ ] **Cross-asset signals**: If we get bond/gold/DXY data, these add context for macro-driven days

### Low Priority / Future
- [ ] **Real-time intraday updates**: Re-run SimSearch at 10:00, 11:00, 12:00 as more data arrives
- [ ] **Strategy integration**: Use SimSearch predictions as a filter for MEDS/Zenith/Apex entries
- [ ] **Ensemble with ML**: Use similar-day outcomes as training data for a gradient boosted model
- [ ] **Regime transitions**: Predict not just today, but regime shifts (fragile→stable transitions)

---

## Findings Log

| Date | Finding | Impact |
|------|---------|--------|
| 2026-04-04 | Project created | — |
| 2026-04-04 | First run had look-ahead bias (88% acc) — all same-day features leaked outcomes | Fixed: all features now prior-day only |
| 2026-04-04 | Honest baseline: 51.3% direction (random=50%), 0.13 return correlation | Near-random for daily direction |
| 2026-04-04 | High-confidence filter (>70%) hits 58-61% on 31-79 days | Mild signal when model is confident |
| 2026-04-04 | K=5 outperforms K=50 (53% vs 50.7%) — fewer matches = stronger | Use tight matching |
| 2026-04-04 | GEX data was 100% NaN initially — aggregate rows use right="AGG" not empty | Fixed loader |
| 2026-04-04 | Pivot to range prediction: 0.59 correlation, 47% bucket accuracy, 89% adjacent | Automatable |
| 2026-04-04 | Danger detection: 53% recall, 39% precision, SKIP days 2x avg range of TRADE days | Key filter signal |
| 2026-04-04 | Filter backtest: P&L +68%, DD -$19k→-$2k, Sharpe 2.83→8.01 | Game changer for credit spreads |
| 2026-04-04 | Fixed 11 days with 100%+ range from zero-filled bars on early close/holiday days | Data quality |
| 2026-04-04 | K=10 optimal — robust across 5-30, all give Sharpe >7.7 | Stable parameter |
| 2026-04-04 | Extended history to 8,370 days (1993-2026) via yfinance SPX OHLC download | 8x more candidates |
| 2026-04-04 | Shared-feature matching: only score on features both query+candidate have | Eliminates NaN noise from pre-2022 |
| 2026-04-04 | Deep history: K=20 optimal, range corr 0.64, bucket acc 51.6%, Sharpe 8.66 | Matches naive baseline + danger detection |
| 2026-04-04 | More matches better with deep history (K=20-50) — opposite of 4yr finding | Diversity improves averaging |
| 2026-04-04 | Feature ablation: iv_atm (0.77), vix (0.74), range (0.65) dominate. Calendar (0.02) = noise | Recalibrated weights |
| 2026-04-04 | Cross-asset (TLT/GLD/UUP/IWM) + multi-day context added. Danger recall 60%→65% | Modest improvement |
| 2026-04-04 | Ensemble (Ridge) did not beat SimSearch alone — components too correlated | Dropped |
| 2026-04-04 | **CRITICAL: Filter HURTS real strategies.** MEDS -$1.7k, LastFive -$25.8k, Apex -$4.9k | Range ≠ strategy loss |
| 2026-04-04 | Strategy losses uncorrelated with range (max r=0.10). EMA errors, not regime, drive losses | Cannot predict strategy P&L |
| 2026-04-04 | SimSearch filter only works for unhedged positions, not strategies with built-in risk mgmt | Scope narrowed |
| 2026-04-04 | SimCondor v1: iron condors on CALM+NORMAL days → -$16.4k. Risk/reward too unfavorable | IC too tight |
| 2026-04-04 | CALM regime is profitable (+$10.6k) but NORMAL loses (-$9.7k). Only CALM has edge | Key finding |
| 2026-04-04 | SimCondor CALM-only: $10.6k P&L, Sharpe 2.52, 79% WR, 102 trades/3.75yr | First profitable SimSearch strategy |
| 2026-04-04 | Sweep: No SL wins (+$14.9k, 91% WR). SL hurts on CALM days — positions recover by expiry | Key insight |
| 2026-04-04 | Sweep: Width 20-30 plateau, strike_mult 1.0 optimal, min_credit 0.40 best at scale | Broad plateaus |
| 2026-04-04 | **Optimized SimCondor: $22.5k, Sharpe 3.26, Calmar 5.03, 93% WR, 136 trades** | No SL + min_credit 0.40 |

---

## Known Limitations

1. Options data only available 2022+. Pre-2022 matching uses Tier 1+2 features only
2. GEX data ends 2026-03-25 (needs backfill for recent days)
3. SPX history CSV from CBOE goes back to ~late 2025 only; older daily data from parquet
4. VIX intraday only 2022+; pre-2022 uses daily VIX only
5. No volume data for SPX index (volume column is 0 in parquet)
