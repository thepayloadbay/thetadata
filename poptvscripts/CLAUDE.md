# CLAUDE.md — PineScript Research Project

model: opus

## Project Goal

Systematically evaluate 42+ popular TradingView PineScripts and extract ideas for SPX 0DTE options trading. Find novel filters, signals, and strategy concepts that can improve Apex, LastFive, and other existing strategies.

## How This Project Works

1. PineScript files are saved in this directory (no extension, raw code)
2. Each script gets a deep evaluation: core logic, bias check, SPX applicability, 10 creative hypotheses
3. Promising ideas become filter candidates or new strategy concepts
4. Filters get implemented in apex/ and lastfive/ engines and backtested
5. Results tracked in CSV files, findings in RESEARCH.md

## Session Workflow

Every session, follow the prompt in PROMPT.md:
1. Check state (TODO.md, evaluation_log.csv, hypothesis_results.csv)
2. Do highest priority work (Phase 0 quick wins > Phase 1 builds > script evaluation > sweeps)
3. Update ALL tracking files after every piece of work
4. Show results immediately (per project-wide feedback rule)

## Key Files

- `TODO.md` — master task list with execution plan and 100 hypotheses
- `RESEARCH.md` — detailed evaluation writeups
- `PROMPT.md` — copy-paste prompt for consistent sessions
- `evaluation_log.csv` — script evaluation tracker
- `ideas_for_spx.csv` — extracted trading ideas
- `filter_candidates.csv` — potential filters for strategies  
- `lessons_learned.csv` — cross-cutting insights
- `hypothesis_results.csv` — backtest results for hypothesis testing
- `trade_logs.csv` — trade-level results
- `MISSING_DATA.md` — data gaps

## Rules

- Always check for look-ahead bias in every PineScript
- Generate 10 creative hypotheses per script
- Always find specific ideas for Apex AND LastFive
- Record every backtest result in hypothesis_results.csv
- Follow anti-overfit rules: IS/OOS split, plateau rule, walk-forward
- Update TODO.md after every completed task
- Show results immediately without asking

## Current Status

- **Batch 1 COMPLETE** (10/42): VIX-Based + Simple Indicators
- **Phase 0 PENDING**: Toggle existing Apex VIX term + VVIX filters and backtest
- **Phase 1 PENDING**: Build WVF, VIX/16 range, VIX SMA(5) indicators
- **Batch 2 NEXT**: Volume & Liquidity (8 scripts)

## Strategy Context

| Strategy | Entry | Edge | What Would Help |
|----------|-------|------|-----------------|
| **Apex** | 15:55, d=12, w=20 | Time decay, 100% WR | Insurance filters for unseen black swan |
| **LastFive** | 15:55, VIX-adaptive | Time decay, 87% WR | Better strike calibration, DD reduction |
| **MEIC** | 14:00-15:30 | Afternoon put spreads | Regime filters, skip bad days |
| **Zenith** | 9:45 | Exhaustion fade | Range boundary detection |
| **Pikes Peak** | varies | Bidirectional | Directional bias from indicators |
