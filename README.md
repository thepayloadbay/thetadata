# MEDS — Multi-Entry Directional Spreads

A SPXW 0DTE options backtesting system for the **Multi-Entry Directional Spreads** strategy, using historical data from [ThetaData](https://thetadata.net/).

## Strategy Overview

**Signal:** Prior-day VIX % change (`dVixChgPct`)
- VIX fell overnight → sell **PUT credit spread** (market calming, bullish lean)
- VIX rose overnight → sell **CALL credit spread** (market fearful, bearish lean)

**Execution:**
- Instrument: SPXW (S&P 500 weekly options), 0DTE
- Spread width: 20 points
- Quantity: 2 contracts
- Entry window: 9:35 AM – 12:45 PM ET, every 20 minutes
- Min OTM distance: 30 points from spot
- Min net credit: $0.55

**Risk management:**
- Dynamic stop-loss: -$500/day when VIX is in a danger zone (< 13.0, 13.0–13.5, or 25.0–30.0)
- On danger-zone days: MTM check every 1 min (vs 5 min normally) for faster SL response
- Daily profit target: $750

## Confirmed Baseline (2022-01-03 → 2026-03-25)

| Metric | Value |
|--------|-------|
| Total P&L | $466,244 |
| Max drawdown | -$9,922 |
| Sharpe ratio | 11.40 |
| Calmar ratio | 47.0 |
| Win rate | 92.9% |
| Avg monthly P&L | ~$9,142 |
| Trades | 6,954 |
| Days traded | 958 / 1,103 |

## Setup

### Requirements

- Python 3.11+
- [ThetaData Terminal](https://thetadata.net/) (for live data) or local parquet cache
- Java (for ThetaTerminal.jar)

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pandas numpy pyarrow mcp
```

### Configuration

Key config flags in `metf_v35_bidask.py`:
```python
USE_LOCAL_DATA = True     # True = read local parquet; False = live ThetaData MCP
PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"
```

### Local Data

Local parquet data lives in `data/YYYY/` with subdirectories:
- `spx_ohlc/` — SPX 1-minute OHLC bars
- `vix/` — daily VIX close
- `option_quotes/` — SPXW bid/ask quotes by strike

## Running

### Full backtest
```bash
.venv/bin/python3 metf_v35_bidask.py
```

### Parameter sweeps
Set the relevant flag to `True` in the config section, then run:

| Flag | What it tests |
|------|--------------|
| `RUN_SL_SWEEP` | Stop-loss levels |
| `RUN_EMA_SWEEP` | EMA fast/slow combinations |
| `RUN_SPREAD_WIDTH_SWEEP` | Spread widths |
| `RUN_MIN_CREDIT_SWEEP` | Minimum credit thresholds |
| `RUN_CALENDAR_SWEEP` | Calendar event filters (FOMC, TW, CPI, PCE, etc.) |
| `RUN_DAY_FILTER_SWEEP` | 43+ daily indicators as day-selection filters |
| `RUN_VIX_MAG_SWEEP` | VIX change magnitude filter |
| `RUN_CALL_SL_SWEEP` | CALL-side stop-loss levels |
| `RUN_GAP_CALL_SL_SWEEP` | Stop-loss on gap-down CALL days |

### Distributed backtest (3-machine setup)
```bash
bash run_distributed.sh
```
Splits the date range across 3 Mac Minis and merges results.

## Output

All output files are timestamped and written to `logs/`:
- `meft_v35_bidask_log_*.csv` — individual trade log
- `metf_v35_bidask_vix_analysis_*.csv` — performance by VIX range
- `metf_v35_bidask_put_call_split_*.csv` — PUT vs CALL breakdown
- `metf_v35_bidask_strike_distance_analysis_*.csv` — performance by strike distance
- `run_history.json` — persistent run history (last 10 runs)

## Key Research Findings

- **VIX change is the only statistically significant direction signal** (p=0.0). RSI, MACD, gap direction, SMA, momentum all show negative P&L as direction routers.
- **All calendar filters cost money**: FOMC, Triple Witching, CPI, NFP, PCE, EOM — every one is net profitable to trade. All filters removed.
- **VIX 15–20 is the sweet spot**: 98.5% win rate. VIX < 15 drops to 85.5%.
- **All max drawdown comes from CALL spreads**: PUT max DD -$5,982 vs CALL max DD -$9,922.
- **Gap direction is not a useful confluence**: days where gap signal disagrees with VIX signal actually have higher per-day PnL ($520 vs $468).
- **Tighter MTM interval on danger-zone days**: checking every 1 min instead of 5 min when dynamic SL is active adds ~$3,800 and raises Sharpe from 10.83 → 11.40.
