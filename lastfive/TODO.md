# TODO — LastFive Strategy

## Phase 1: Baseline -- DONE
- [x] #1 Run baseline backtest (no indicator, trade every day) — $47,474, 90% WR, 440 trades
- [x] #2 Analyze VIX correlation — VIX 15-20 best bucket, VIX>25 still profitable
- [x] #3 Sweep entry times (15:40-15:57) x distances (3-20) — 15:55/dist=3 optimal
- [x] #4 Sweep MIN_SHORT_DISTANCE — dist=3 best P&L, dist=5+ trades drop off

## Phase 2: Indicator Research -- DONE
- [x] #5 Feature analysis (20 features, 440 trades) — VIX(15:50) and afternoon_return are top 2
- [x] #6 Test VIX(15:50) <= 26 filter — skips high-VIX days, +$3.1k P&L
- [x] #7 Test afternoon return < 1.0% filter — skips strong rally days, +$4.0k P&L
- [x] #8 Test combo VIX<=26 + afternoon<1.0% — +$6.5k P&L, DD -$6.5k->-$4.0k
- [x] #9 Test min credit >= $0.10 — +$2.3k P&L, cuts worthless $0.05 bucket
- [x] #10 Test 10-min pre-entry magnitude — no signal, rejected
- [x] #11 Test day-of-week filter — no strong signal, rejected

## Phase 3: Deep Investigation -- DONE
- [x] #12 Exit timing (15:57/58/59 vs settlement) — hold to 16:00 wins by $18-25k
- [x] #13 Put credit spread side — +$38k P&L with only $626 more DD
- [x] #14 Loss day deep patterns — losses are unpredictable EOD rips, no filter found
- [x] #15 Spread width sweep ($5/10/15/20) — $5 best P&L, $10+ better Sharpe
- [x] #16 Adaptive strike distance — VIX-adaptive +$6k but WR drops to 88%

## Phase 4: Confirmed Baseline
- [x] #17 Bidirectional mode (call+put) — $94,262 P&L, 91% WR, 610 trades, Sharpe 2.94

## Phase 5: Round 1 Research -- DONE
- [x] #18 Walk-forward validation — OOS ($126k Sharpe 8.17) beats IS ($106k Sharpe 3.43). Not overfit.
- [x] #19 Monte Carlo — 0% risk of ruin, 100% prob of profit, worst DD -$13k
- [x] #20 VIX-adaptive Rule C3 verified — +$74k, plateau VIX<17-19, walk-forward confirmed
- [x] #21 FOMC skip — overfit (helps IS, hurts OOS). TW profitable. Neither adopted.
- [x] #22 Put afternoon filter — zero effect (already covered by abs filter). Put mom30 Sharpe +0.5 but -$17k

## Phase 6: Round 2 Research -- DONE
- [x] #25 Combined configs (C3 + tiered + put filter + FOMC) — C3+tiered dominates
- [x] #26 VIX threshold plateau sweep — VIX<18 confirmed on plateau (17-19 within 5%)
- [x] #27 Walk-forward C3+tiered — OOS advantage +$77k persists. FOMC skip overfit.
- [x] #28 Credit-weighted sizing — tiered B adds +$75k, mode D aggressive $359k
- [x] #29 Stacked put filters — aftn<-0.75% + mom30<-0.10% best: +$8.6k, DD -33%

## Phase 7: Round 3 Research -- DONE
- [x] #30 Walk-forward final combined — C3+tiered validated, FOMC skip rejected OOS
- [x] #31 Monte Carlo v2 — tiered: 0% ruin, worst DD -$21.5k, median -$7.3k
- [x] #32 Dual entry 15:53+15:55 — +$17k but Sharpe drops. 15:57 is weak. Single 15:55 optimal.

## Phase 8: Creative P&L Ideas -- DONE
- [x] #33 Equity compounding — 5% eq tiered: $50k→$5.1M. 0% ruin. Theoretical due to liquidity.
- [x] #34 Post-loss cooldown — DD -3.8% better but costs $1.3M compounded. 87% WR = bad to skip.
- [x] #35 Asymmetric width — wider hurts P&L by 40%+. Keep $5.
- [x] #36 VIX momentum regime — all variants worse than static C3. Rejected.
- [x] #37 High-conviction double entry — +$17k P&L but Sharpe -0.44. Marginal.

## Phase 9: Loss Mitigation -- DONE
- [x] #38 Per-side loss cap — $1k cap: +$45k P&L, DD -63%, Calmar 41. THEORETICAL (can't enforce exactly).
- [x] #39 Intraday vol gate — costs $19-55k P&L. Rejected.
- [x] #40 ATM size limiter — ATM is 71% of profit. Capping hurts. Rejected.
- [x] #41 Offset entry reduction — +$11k, DD -25%, Sharpe +0.55. Unrealistic due to slippage. DISABLED.
- [x] #42 Dynamic width on high credit — costs $80-150k P&L. Rejected.

## Phase 10: Realistic Loss Cap Research -- DONE
- [x] #43 Narrow spread width — SPX has $5 min strike spacing. Impossible for $2/$3.
- [x] #44 Butterfly/condor — $5 strikes limit options. $10 wide best Sharpe 6.07 but -$90k P&L.
- [x] #45 Max loss budget — cap at 4 contracts: $100k P&L, DD -$3.8k. Steep P&L cost.
- [x] #46 Protective buy-back at 15:56 — +$34k, DD -52%. THEORETICAL — slippage makes it impractical.
- [x] #47 Hybrid combo — offset entry only viable piece. Others add complexity without benefit.

## Phase 11: Pre-Entry Loss Mitigation (no stop-losses) -- DONE
- [x] #48 Asymmetric risk by momentum — DD worsens 46%, Sharpe -0.43. Rejected.
- [x] #49 Widen threatened side distance — DD -60%, Sharpe +2.41 (subset). Needs full verification.
- [x] #50 Max contract cap (max 7) — Sharpe 5.12→5.48, DD -21%, worst day -$2,919. **ADOPTED.**
- [x] #51 One-side-only on directional — -$49-100k P&L, DD unchanged. Rejected.
- [x] #52 Vol-scaled qty (ATM only) — DD -53%, Calmar 19.29. Optional risk lever if DD priority.

## Phase 12: Data & Validation — DONE
- [x] #54 Document entry time sweep findings in RESEARCH.md
- [x] #55 Download 2025 option quotes with wide strike range (OTM_MIN=-150) — 248 large files
- [x] #56 Validate 2025 data coverage — 98.4% ATM, 98.8% at $3 OTM across all years
- [x] #57 Re-run full OA-matching backtest — $75,906 (77% of OA's $98,779), 1,910 trades
- [x] #58 Discover touch detection gap — OA uses bar CLOSE, not LOW/HIGH (+$46k difference)
- [x] #59 Update engine: continuous touch exit (bar CLOSE), bar OPEN for entry strike
- [x] #60 New baseline: $214,538, Sharpe 6.08, Calmar 4.82, 1,501 trades
- [x] #61 SPX last-10-min statistical model — Student-t df=6.58, mean-reverting (0.56x √t)
- [x] #62 Model-implied improvements — min dist, asym put, pre-entry range. None beat baseline P&L.
- [x] #63 Credit velocity research — d=1.54 signal but exit cost kills P&L. Touch is optimal.
- [x] #64 50 hypotheses generated, top 15 backtested
- [x] #65 Entry 15:52 adopted — +$143k P&L, DD -45%, Sharpe 10.77, Calmar 11.76
- [x] #66 VIX range budget research — signal real (ρ=0.201) but doesn't beat VIX-adaptive. Not adopted.
- [ ] #67 Test 15:48 and 15:45 entry times on OA backtester
- [ ] #68 Test VIX1D as distance driver (#43)
- [ ] #69 ML ensemble of top 5 features (#50)

## Future
- [ ] #62 Combine with MEDS or Zenith for portfolio-level analysis
- [ ] #63 TradeStation live trading integration
- [ ] #64 Implement offset entry into engine
