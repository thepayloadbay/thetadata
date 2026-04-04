# CLAUDE.md — Zenith Fade CCS Strategy Project

model: opus

## Project Goal

Backtest and optimize a **Zenith Fade CCS (Credit Call Spread)** strategy that sells SPX credit call spreads on upside exhaustion signals. Ported from a TradingView PineScript indicator. The primary objective is to **maximize total P&L** while keeping max drawdown and risk-adjusted metrics (Sharpe, Calmar) strong.

**Roadmap priorities:**
1. Enhance with real option pricing from ThetaData bid/ask quotes (replace BS model)
2. Add SPX volume data for proper relative volume signal
3. Research additional filters and signal enhancements
4. Eventually: integrate into OptionFlow live trading platform

---

## Strategy Overview

**Zenith Fade CCS — Credit Call Spread on Upside Exhaustion**
- Sells SPX credit call spreads when upside exhaustion signal fires
- Two signal modes: Classic (AND filters) and Dynamic (weighted score)
- Entry: signal day close (~3:50 PM), sell 25pt OTM short call + 10pt wide spread
- Exit: next day 4:00 PM close (spread settles at intrinsic)
- Fixed $600 risk per trade, TP cap 66%, SL cap -100%
- Always bearish (call spreads only) — no directional signal needed
- One trade max per signal day
- VIX < 22 filter removes the high-volatility danger zone

**Key difference from MEDS**: Zenith is a daily strategy (1 trade/day max) vs MEDS (multiple intraday 0DTE entries). Zenith uses daily exhaustion indicators vs MEDS's intraday EMA crossover.

---

## Environment

- **Historical data**: ThetaData (local parquets, shared with MEDS)
- **Data available**: SPX 1-min OHLC (2022-2026), VIX (1990-2026), option quotes
- **Data limitation**: No SPX volume — relative volume filter uses default 1.0
- **Standard backtest range**: 2022-01-03 → 2026-03-25
- **Result format**: markdown table with P&L, Max DD, Sharpe, Calmar, WR, Trades

---

## Behavior Rules

**Always show backtest results immediately when a run finishes** — don't ask "want me to show results?". Display them as soon as the process finishes.

**No look-ahead bias** — all signals use prior-day (T-1) indicators for T's signal:
- Signal fires based on T-1's close/OHLC data
- Trade enters at T-1's close, exits at T's close
- The PineScript uses `signal[1]` (yesterday's value) — we replicate this with `prior_day_ind()`

**Parameter sensitivity (Plateau Rule)**: from MEDS methodology — only adopt a parameter when it sits on a broad plateau of consistent performance across neighboring values. Single-peak optima are overfitting.

**Volume limitation**: SPX has no native volume. The relative volume filter (dRelVol) defaults to 1.0. This means:
- Dynamic Score is effectively: score = varPct * 1.4 + 8.9 - rocket * 274
- **TODO**: Use SPY volume or CBOE composite volume as proxy

---

## Current Config (Confirmed Optimal — 2026-03-31)

### Core Signal
| Parameter | Value | Reason |
|---|---|---|
| `SIGNAL_MODE` | `"dynamic"` | Weighted exhaustion score |
| `SCORE_THRESHOLD` | `68.0` | Plateau-validated: 66-72 all Sharpe > 1.8 |
| `W_VAR` / `W_VOL` / `W_ROCKET` | `1.4` / `8.9` / `274.0` | W_ROCKET has no effect; signal is effectively "close near the high" |
| `ENABLE_PUT_SIDE` | `True` | Bidirectional: call spreads on high variance, put spreads on low variance |
| `PUT_SCORE_MAX` | `40.0` | Put signal fires when score <= 40 |
| `VIX_MAX_FILTER` | `22.0` | VIX 20-30 is danger zone |

### Strike & Credit
| Parameter | Value | Reason |
|---|---|---|
| `ENABLE_VIX_ADAPTIVE_OFFSET` | `True` | VIX<13: 15pt, VIX 13-18: 20pt, VIX 18-22: 30pt OTM |
| `SPREAD_WIDTH` | `10.0 pts` | 10-20 plateau |
| `CREDIT_MODEL` | `"real"` | Actual bid/ask from ThetaData (falls back to BS) |
| `ENABLE_CREDIT_FLOOR` | `True` | Skip trades with credit < $0.75 |

### Risk Management
| Parameter | Value | Reason |
|---|---|---|
| `RISK_AMOUNT` | `$600` | Base risk per trade |
| `ENABLE_SCORE_SIZING` | `True` | Score 85+: 2x, 75-85: 1.5x, 68-75: 1x |
| `ENABLE_INTRADAY_SL` | `True` | Close at trigger price when SL breached |
| `INTRADAY_SL_CAP` | `-0.25` | Default SL: 25% of max risk |
| `ENABLE_GAP_DYNAMIC_SL` | `True` | Adverse gap: tighten to -0.15. Favorable gap: widen to -0.40 |
| `ENABLE_SL_REENTRY` | `True` | Re-enter after SL if SPX pulls back 50%+ of the spike |
| `COMMISSION` | `$0.50/leg` | TradeStation rate |

---

## Confirmed Baseline (2022-01-03 → 2026-03-25, run 2026-03-31)

| Metric | Value |
|---|---|
| **Total P&L** | $107,723 |
| **CAGR** | 19.2% |
| **Max Drawdown** | -$1,631 (0.9%) |
| **Sharpe Ratio** | 7.53 |
| **Calmar Ratio** | 22.40 |
| **Win Rate** | 63.9% |
| **Profit Factor** | 2.85 |
| **Total Trades** | 681 |
| **Avg Win / Avg Loss** | $381 / -$236 |
| **Worst Trade** | -$602 |
| **Recovery Factor** | 66.04 |
| **Risk of Ruin** | 0% (Monte Carlo) |

### Improvement Stack (compound impact)

| Step | P&L | Sharpe | DD |
|---|---|---|---|
| Baseline (bidirectional, real pricing, SL) | $47,472 | 7.08 | -$1,006 |
| + VIX-adaptive offset | $48,374 | 6.03 | -$1,325 |
| + Credit floor $0.75 | $49,300 | 6.67 | -$1,326 |
| + Score-proportional sizing | $87,279 | 6.41 | -$2,078 |
| + Gap dynamic SL | $99,196 | 7.69 | -$1,450 |
| **+ SL re-entry (ALL ON)** | **$107,723** | **7.53** | **-$1,631** |

### Prior Baselines (for reference)

| Config | P&L | Sharpe | DD | Trades |
|---|---|---|---|---|
| **Current (all improvements)** | **$107,723** | **7.53** | **-$1,631** | **681** |
| Bidirectional (no improvements) | $48,257 | 7.20 | -$1,006 | 669 |
| Call only (real pricing) | $29,994 | 5.84 | -$1,243 | 513 |
| Call only (BS pricing) | $40,531 | 6.37 | -$728 | 513 |
| PineScript defaults | -$5,861 | -0.24 | -$16,642 | 815 |

---

## Tested and Rejected

| Parameter | Result | Reason |
|---|---|---|
| `STRIKE_OFFSET=5` (PineScript default) | -$7.7k P&L | Too close to ATM, low WR 58.8% |
| `SCORE_THRESHOLD=41.5` (PineScript default) | -$5.9k P&L | Fires 77% of days — not selective enough |
| `CREDIT_MODEL="fixed"` ($4) | Unrealistic at far OTM | BS model gives $3 at offset=30 vs fixed $4 |
| `VIX_MAX_FILTER=50` (no filter) | P&L $27k but DD -$3.7k | VIX 25-35 zone costs $7k; only 4% of trades |

## Key Findings

1. **Strike offset is the most impactful parameter**: monotonically improves from 0-40pt. At 25pt OTM, SPX needs to rally 25pts in one day for the spread to breach. This happens ~25% of the time.

2. **VIX < 22 is the sweet spot**: VIX < 15 gives 85.5% WR, VIX 15-20 gives 74.2%. VIX 20-25 drops to 52.7% — effectively random.

3. **Score threshold 68 vs 41.5**: raising the threshold from PineScript default to 68 reduces signals from 815 to 513, but improves Sharpe from -0.32 to 1.90. Quality over quantity.

4. **SL cap -0.5 shows $101k**: but this requires intraday stop loss monitoring. Worth investigating for live trading implementation.

5. **No SPX volume data**: dRelVol is a constant 1.0. Getting real volume data could change the Dynamic Score significantly.

---

## CLI Tools

- `python zenith.py --marathon`: Run single full backtest
- `python zenith.py --mode classic`: Classic filter mode
- `python zenith.py --mode dynamic`: Dynamic score mode
- `python zenith.py --score-threshold 68`: Override score threshold
- `python zenith.py --strike-offset 25`: Override strike offset
- `python zenith.py --start 2023-01-01 --end 2025-12-31`: Custom date range
