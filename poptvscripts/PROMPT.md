# PineScript Research Project — Session Prompt

Copy-paste this at the start of each session:

---

```
Continue the PineScript research project in poptvscripts/.

## Step 1: Check State
- Read poptvscripts/TODO.md for current status and priorities
- Read poptvscripts/evaluation_log.csv to see which scripts are evaluated
- Read poptvscripts/hypothesis_results.csv to see what's been tested
- Check what phase we're in (Phase 0-4 in TODO.md execution plan)

## Step 2: Do the Highest Priority Work
Priority order each session:
A) If there are UNTESTED Phase 0 quick wins (existing filters to toggle) — run those backtests first
B) If Phase 1 filters need building — implement the next one and test it
C) If new PineScripts haven't been evaluated — evaluate next batch (5-8 scripts)
D) If Phase 2 sweeps are pending — run parameter sweeps on built filters
E) If Phase 3 combos are pending — test filter combinations with walk-forward

## Step 3: For Each PineScript Evaluation
- Read the full code
- Identify core logic, check for look-ahead bias/repainting
- Categorize and rate (complexity 1-5, value 1-5)
- Generate 10 creative hypotheses specific to SPX 0DTE options
- Find specific ideas for Apex (15:55, d=12, w=20, 100% WR) and LastFive (15:55, VIX-adaptive, w=5, 87% WR)
- Update ALL tracking files: evaluation_log.csv, ideas_for_spx.csv, filter_candidates.csv, lessons_learned.csv

## Step 4: For Each Hypothesis Test
- Run backtest with specific parameter
- Record result in hypothesis_results.csv (hypothesis_id, strategy, filter, param, value, trades, pnl, wr, sharpe, dd, verdict)
- Update TODO.md marking it done
- Show results immediately

## Step 5: Update Everything
- TODO.md — mark completed items, add new action items
- RESEARCH.md — add detailed findings
- MISSING_DATA.md — if new data gaps found
- hypothesis_results.csv — every backtest result

## Anti-Overfit Rules
- Never optimize on full dataset — always IS/OOS split
- Any filter removing >40% of trades needs strong justification
- Prefer Sharpe + DD improvement over raw P&L boost
- Walk-forward validation before production
- Plateau rule: only adopt on broad plateau, never single peak

Work autonomously. Show summary when done.
```

---

## Quick Reference

### Key Files
| File | Purpose |
|------|---------|
| `TODO.md` | Master task list, batch queue, execution plan, 100 hypotheses |
| `RESEARCH.md` | Detailed evaluation writeups per script |
| `evaluation_log.csv` | Which scripts evaluated, ratings, summaries |
| `ideas_for_spx.csv` | Trading ideas extracted (8 general + 5 Apex + 6 LastFive) |
| `filter_candidates.csv` | Filter candidates for existing strategies |
| `lessons_learned.csv` | Cross-cutting insights |
| `hypothesis_results.csv` | Every backtest result for hypothesis testing |
| `trade_logs.csv` | For individual trade-level backtest results |
| `MISSING_DATA.md` | Data gaps identified |

### Strategy Quick Stats (for context)
| Strategy | Entry | Key Params | Performance |
|----------|-------|------------|-------------|
| **Apex** | 15:55 | d=12, w=20, both-sides, skip Wed+Thu | 100% WR, $0 DD, Sharpe 12+ |
| **LastFive** | 15:55 | VIX-adaptive d=0/3/5, w=5, both-sides | 87% WR, -$5.6k DD, Sharpe 5.48 |
| **MEIC** | 14:00-15:30 | Put credit spreads | Sharpe 3.92, -$9.2k DD |
| **Pikes Peak** | varies | Bidirectional | $95k, Sharpe 8.33 |
| **Zenith** | 9:45 | Exhaustion fade, credit spreads | $32k, Sharpe 8.28 |

### Existing Infrastructure
- Apex has `ENABLE_VIX_TERM_FILTER` and `ENABLE_VVIX_FILTER` already built but disabled
- Both engines load VIX, VIX9D, VVIX, VIX1D daily data at startup
- Adding a new filter = ~15 lines (config flag + engine check + CLI flag)
- Pattern: `_cfg.ENABLE_*_FILTER` + threshold in config, check in engine filter block
- All filters use T-1 data (no look-ahead bias)

### Hypothesis ID Format
- `H-WVF-1` through `H-WVF-10` = Williams VIX Fix hypotheses
- `H-VXC-*` = VIX Crossing
- `H-VMM-*` = VIX MTF Momentum
- `H-V16-*` = VIX Rule of 16
- `H-VRS-*` = VIX Reversal Scalper
- `H-VTA-*` = VIX Volatility Trend Analysis
- `H-VHM-*` = VIX Option Hedge Monitor
- `H-VCP-*` = VIX Curve Pro (Term Structure)
- `H-HMA-*` = Hull Suite
- `H-ICH-*` = Ichimoku

### Evaluation Criteria
| Criterion | What to Check |
|-----------|---------------|
| **Core Logic** | What signal? Entry/exit rules? |
| **Look-Ahead Bias** | Future bars? Repainting? Peeking at close? |
| **Timeframe** | Designed for? Works intraday for 0DTE? |
| **Data Requirements** | What needed? Do we have it? |
| **SPX Applicability** | As filter? Signal? New strategy? |
| **Novelty** | Concept we haven't explored? |
| **Apex/LastFive Fit** | Specific ideas for our best two strategies? |

### Categories
VIX-Based | Volume | Smart Money (SMC) | Momentum/Trend | Mean Reversion | Machine Learning | Market Structure | Opening Range | Volatility
