# CLAUDE.md — LastFive Strategy Project

model: opus

## Project Goal

Backtest and optimize a **LastFive** strategy that sells SPX short call + put credit spreads 5 minutes before market close. The primary objective is to **maximize total P&L** while keeping max drawdown and risk-adjusted metrics strong.

**Roadmap priorities:**
1. Further indicator research — find additional filters to cut remaining losses
2. Risk management improvements — better drawdown control
3. Eventually: live trading infrastructure on Tradestation

---

## Strategy Overview

**LastFive — Short Call + Put Spread 5 Min Before Close**
- SPXW 0DTE credit spreads (both call and put sides), single entry at 15:55
- Call side: short call >= SPX + $3 (rounded up to 5pt grid), long call $5 above
- Put side: short put <= SPX - $3 (rounded down to 5pt grid), long put $5 below
- Risk: $2,500 per side (position sized by max loss per contract)
- Min credit: $0.10 per spread (skip if less)
- Exit: 16:00 close (0DTE expiration — settles at intrinsic value)
- Filters: VIX(15:50) <= 26, afternoon SPX return (13:00->15:54) < 1.0%

**Key edge**: In the last 5 minutes of trading, SPX movement is typically small enough that $3+ OTM spreads expire worthless. The strategy collects small credits with 91% reliability.

---

## Environment

- **Historical data**: ThetaData (local parquets, shared with MEDS/Zenith)
- **Data available**: SPX 1-min OHLC (2022-2026), VIX (daily + intraday), option quotes (bid/ask)
- **0DTE availability**: SPXW daily expirations started mid-2022. Early 2022 (Jan-May) only had Mon/Wed/Fri expirations — missing ~31 trading days is expected, NOT a download gap.
- **Broker (live)**: Tradestation
- **Standard backtest range**: 2022-01-03 -> 2026-03-25
- **Result format**: markdown table with P&L, Max DD, Sharpe, Calmar, WR, Trades

---

## Behavior Rules

**Always show backtest results immediately when a run finishes** — don't ask "want me to show results?". Display them as soon as the process finishes.

**No look-ahead bias** — all indicators and prices must use data available at the moment of the decision:
- Prior-day (T-1) indicators are always safe
- Same-day indicators up to 15:54 are safe (already happened)
- VIX close, SPX close, daily return are NOT available at 15:55 (look-ahead!)
- Use 15:50 or earlier snapshots for same-day signals
- **Strike selection MUST use bar OPEN, not bar CLOSE** — using bar close at 15:54 or 15:55 means you're using a price that doesn't exist yet when the trade decision is made. Verified by matching against Option Alpha's backtester: bar OPEN gives 99.8% strike match vs 59.9% with bar CLOSE. This is a critical look-ahead bias that affects every trade.
- **Touch/buyback detection should use bar LOW** (for puts) or bar HIGH (for calls) — this captures intrabar touches that bar CLOSE misses

**Plateau Rule**: Only adopt a parameter when it sits on a broad plateau of consistent performance across neighboring values. Single-peak optima are overfitting.

---

## Current Config (Confirmed Baseline — 2026-04-04)

| Parameter | Value | Reason |
|---|---|---|
| `ENABLE_CALL_SIDE` | `True` | Sell call credit spreads above spot |
| `ENABLE_PUT_SIDE` | `True` | Sell put credit spreads below spot |
| `MIN_SHORT_DISTANCE` | `3.0` | Short leg at least $3 from spot. Sweep: dist=3 best P&L ($47k call-only), dist=5+ too few trades |
| `SPREAD_WIDTH` | `5.0` | $5 wide spread. Sweep: $5 best P&L, wider ($10/$20) better Sharpe but less total $ |
| `RISK_AMOUNT` | `$2,500` | Max risk per side per trade |
| `MIN_NET_CREDIT` | `$0.10` | Skip if credit < $0.10. $0.05 bucket was net -$2,320; $0.10 adds +$2.3k P&L |
| `MAX_QTY_PER_SIDE` | `99` | Effectively uncapped |
| `ENABLE_TOUCH_EXIT` | `True` | OA-style continuous touch exit using bar CLOSE |
| `CALL_TOUCH_VALUE` | `$0` | Exit call when bar CLOSE >= short call strike |
| `PUT_TOUCH_VALUE` | `-$1` | Exit put when bar CLOSE <= short put strike - $1 |
| `ENABLE_VIX_INTRADAY_FILTER` | `True` | Skip if VIX at 15:50 > 26 |
| `VIX_INTRADAY_MAX` | `26.0` | High VIX = more EOD movement. Top separating feature (d=0.50) |
| `ENABLE_AFTERNOON_FILTER` | `True` | Skip if SPX return 13:00->15:54 >= 1.0% |
| `AFTERNOON_RETURN_MAX` | `1.0%` | Strong afternoon rally = SPX blows past short strike. 2nd best feature (d=0.43) |
| `COMMISSION` | `$0.50/leg` | TradeStation rate |

**Confirmed Baseline (2022-01-03 -> 2026-03-25, run 2026-04-04):**

| Metric | Value |
|---|---|
| **Total P&L** | $369,763 |
| **CAGR** | 65.5% |
| **Max Drawdown** | -$2,739 (-0.7%) |
| **Sharpe Ratio** | 10.90 |
| **Calmar Ratio** | 11.96 |
| **Win Rate** | 72.7% |
| **Profit Factor** | 2.98 |
| **Total Trades** | ~1,675 (call + put) |
| **Worst Day** | -$1,188 |
| **Entry** | 15:52, bar OPEN (99.8% match with OA) |
| **Touch Exits** | OA-style, bar CLOSE, Call $0 / Put -$1 |
| **Range Budget** | Tighten $2 on quiet days (<50% VIX-range consumed) |

---

## Tested and Validated

**Entry time sweep (15:40, 15:45, 15:50, 15:53, 15:55, 15:57) x distance (3, 5, 7, 10, 15, 20):**
- 15:40 and 15:45 are losers at ALL distances — too much time for adverse movement
- 15:53 is breakeven
- 15:55 is the sweet spot for P&L with high WR
- 15:57 has higher WR but fewer trades (less premium available)
- Closer distance = more trades + more P&L but lower WR

**Indicator research (20 features, 440 trades at dist=3):**
- Top features: VIX(15:50) d=0.50, afternoon_return d=0.43, mom_60 d=0.36, mom_30 d=0.35
- Best combo: VIX<=26 + afternoon<1.0% — skips 64 days (17L/47W), +$6.5k P&L, DD -$6.5k->-$4.0k
- Day-of-week: no strong signal (Tue best at 94.9% WR, but not enough to filter)
- 10-min pre-entry magnitude: no signal (wins and losses have identical distribution)

**Min credit sweep:**
- $0.10 is optimal — only threshold that improves P&L (+$2.3k) while keeping same DD
- $0.05 bucket is net -$2,320 (94.3% WR but many tiny credits that lose big)

---

## Tested and Rejected

| Test | Result | Reason |
|---|---|---|
| **Exit at 15:57/15:58/15:59** | -$18k to -$25k P&L cost | Buying back spread costs more on 92% winners than it saves on 8% losers |
| **10-min pre-entry absolute change filter** | No signal | Wins and losses have identical magnitude distribution (mean $5.1 vs $3.9) |
| **Near session high filter** | -$4.2k P&L cost | Removes 10L but also 64W — too aggressive |
| **Day-of-week filter** | Not worth it | Best day (Tue 94.9%) vs worst (Mon 87.2%) — gap too small |
| **Spread width $10/$15/$20** | Better Sharpe but less P&L | $10: $31k, Sharpe 7.14, DD -$1,064. Good for risk-adjusted but $21k less P&L |
| **Adaptive strike (VIX-based)** | $62k P&L, Calmar 16.19 | Interesting but WR drops to 88%. Not adopted yet; worth revisiting |
| **Protective buyback at 15:56/57** | +$34k P&L, DD -52% | Replaced by OA-style continuous touch exit. Buyback was unrealistic due to slippage |
| **Touch using bar LOW/HIGH** | 583 touches, P&L $75.9k | Over-triggers on intrabar wicks that reverse. OA uses bar CLOSE (445 touches, $121k). Confirmed by matching OA trade counts |
| **Credit velocity early exit** | d=1.54 signal, all configs lose P&L | Spread value at 15:56 is strongest feature ever (d=1.54) but exit cost at 15:56 exceeds savings. 47% of 2x underwater trades recover by 16:00. Mean-reversion saves them. |
| **Min distance $1-2 at low VIX** | Better Sharpe/DD but -$24k to -$57k P&L | Model says ATM needs $1.6-2.3 for 50% safe. But touch handles the breach; removing ATM trades costs too much credit |
| **Asymmetric put distance** | -$7k to -$16k P&L, no DD improvement | Down-tail is fatter (σ 1.31x at VIX 22-26) but touch already handles it |
| **Pre-entry range filter** | -$18k to -$48k P&L | Max range 15:45-15:54 is best predictor (ρ=0.294) but removes too many profitable high-vol days |
| **VIX range budget filter (skip)** | Sharpe +0.07 max, -$11k P&L | Range consumed has ρ=0.201 but skipping costs too much P&L. Tightening on quiet days adopted instead. |
| **Exit at 15:59** | -$115k P&L, DD doubles | Misses final minute of mean-reversion |
| **Pin risk / round numbers** | No edge | SPX doesn't pin meaningfully in last 8 min |
| **Day-after-large-move** | Marginal, -$3.6k | Costs P&L, not enough signal |
| **Day-of-week filter** | No actionable signal | Best (Wed) vs worst (Mon) gap too small |
| **Vol-scaled sizing** | Neutral | Current tiered sizing already near-optimal |
| **Asymmetric risk by momentum** | DD +46% worse, Sharpe -0.43 | When "safe" side loses, 2x allocation doubles damage. Mom30 is noise for last 5 min |
| **One-side-only on directional days** | -$49k to -$100k P&L, DD unchanged | Skipped "threatened" trades are overwhelmingly profitable. Momentum doesn't predict last 5 min |
| **Widen threatened side distance** | DD -60%, Sharpe +2.41 | Only tested on subset. Needs full verification. Graduated +3/+5 by |mom30| |
| **Vol-scaled qty (ATM only)** | DD -53%, Sharpe +0.29, Calmar 19.29 | Best DD reduction but costs $75k P&L. Optional risk lever |

---

## Key Performance Insights

- Losses are caused by unpredictable SPX rips in the final 5 minutes (avg +$7.2 on losses vs -$0.8 on wins)
- No pre-entry feature cleanly separates wins from losses — the last-5-min move is structurally unpredictable
- Low-credit trades that lose are catastrophic (credit $0.15 with 15-17x loss ratio) — min credit $0.10 helps
- Put side is weaker than call side (Sharpe 3.19 vs 6.49) but adds $38k with only $626 more DD — excellent diversification
- Both sides profitable every year 2022-2026
- Worst 10 days: avg 5-min |move| = $10.3, split 5 up / 5 down, 9/10 are single-side losses
- ATM (dist=0) trades generate 71% of total P&L ($181k of $253k) — the profit engine

---

## Loss Mitigation Research (2026-04-01)

Two theoretical improvements identified but need realistic implementation:

### 1. Per-Side Loss Cap ($1,000) — THEORETICAL
- Clips each side's loss at -$1,000 per trade
- Result: +$45k P&L, DD -63%, Sharpe 5.12→6.93, Calmar 12.91→41.28
- **Problem**: Can't enforce exact stop on 0DTE in last 5 minutes (slippage, gaps, settlement)
- **Realistic alternatives being researched**: narrow spread width, max loss budget, protective buy-back

### 2. Offset Entry Reduction — REALISTIC
- Enter both sides at 15:55. At 15:56, if SPX moved >$3 toward a short strike, reduce that side's qty by 50%
- Result: +$11k P&L, DD -25%, Sharpe +0.55, Calmar +5
- Triggers on 8.7% of days. Mechanically executable on TradeStation.
- **Realism: 80-90%** — small slippage cost from buying back at 15:56 ask

### Loss Mitigation — Tested & Rejected
| Idea | Result | Why rejected |
|---|---|---|
| Intraday volatility gate | -$19k to -$55k P&L | High-vol bars carry profitable trades too |
| ATM size limiter | -$58k to -$118k P&L | ATM is 71% of profit — capping it kills P&L |
| Dynamic wider width on high credit | -$80k to -$150k P&L | Fewer contracts per trade hurts more than DD savings |

---

## Creative P&L Ideas — Tested

### Equity Compounding (5% of equity tiered) — VALIDATED
- $50k → $5.1M over 4 years, Sharpe 3.72, DD 17%
- Theoretical — liquidity caps real size well before $5M
- 0% risk of ruin (Monte Carlo)

### Other tested & rejected:
- Post-loss cooldown: DD -3.8% better but costs $1.3M compounded (87% WR = most post-loss days are winners)
- VIX momentum regime: all variants worse than static Rule C3
- Asymmetric width: wider hurts P&L by 40%+
- Dual entry (15:53 + 15:55): +$17k but Sharpe drops 0.44

---

## CLI Tools

- `python lastfive.py`: Run full backtest with current config
