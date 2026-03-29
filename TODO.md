# MEDS Strategy — TODO & Research Log

---

## Strategy Baseline

**MEDS — Multi-Entry Directional Spreads**

SPXW 0DTE credit spreads (20-pt wide, qty=2), entries every 20 min from 9:35–12:45. Direction signal: prior-day VIX change — VIX fell → PUT spread, VIX rose → CALL spread. Signal is locked for the whole day.

**Confirmed baseline (2022-01-03 → 2026-03-25):**

| Metric | Value |
|---|---|
| Total P&L | $607,034 |
| Max Drawdown | -$9,922 |
| Sharpe | 12.35 |
| Calmar | 61.2 |
| Win Rate | 93.1% |
| Trades | 7,014 |
| Days traded | 957 / 1,103 |

VIX change is the only statistically significant direction signal (p=0.0). All alternatives (RSI, MACD, gap direction, SMA200, momentum, stochastic) show negative P&L.

---

## Confirmed Settings

### Dynamic Stop Loss — KEEP
- `ENABLE_DYNAMIC_SL = True`, `DYNAMIC_SL_AMOUNT = -$500`
- Triggers on: VIX < 13.0, VIX 13.0–13.5, VIX 25.0–30.0
- On danger-zone days: MTM check tightened to 1 min (vs 5 min) — adds ~$3,800, Sharpe 10.83 → 11.40

### No Daily Profit Target — KEEP
- `DAILY_TP = None` (was $750)
- Full marathon sweep: None=$607k, $900=$512k, $800=$487k, $750=$466k
- Removing TP adds +$140k with identical MaxDD. $750 was cutting off best days (2025 months hitting $18–22k)

### Calendar / Event Filters — ALL DISABLED
- **FOMC dates**: skipping costs $23,512
- **Triple Witching**: skipping costs $12,212
- **CPI/NFP**: CPI costs $29,790, NFP costs $36,214 to skip → `ENABLE_ECON_FILTER = False`
- **All other calendar events** (PCE, EOM, EOQ, PPI, OPEX, full moon, first weekly): all net profitable → `ENABLE_CALENDAR_FILTER = False`
- **VIX magnitude filter**: skipping |dVixChgPct| < any threshold (0.25%–3.0%) always costs PnL

---

## Tested & Rejected

| Test | P&L | Max DD | Decision |
|---|---|---|---|
| Skip VIX 25–30 | $597,112 | -$9,922 | Rejected — $10k P&L cost, DD unchanged, Sharpe gain cosmetic |
| No SL on CALL days | -$29k (full run) | -$24k (3×) | Rejected — sweep was unreliable (+$92k was false) |
| Gap-down CALL SL -$300 | -$69k vs baseline | -$6,338 | Rejected — DD improved but P&L cost too high |
| Month/direction SL rules | -$49k vs baseline | — | Rejected — blanket monthly rules don't account for VIX variation |
| Intraday Bayesian entry gate | -$64k best case | -$9,922 | Rejected — B/A spread noise floor defeats the signal (see research log) |
| Premium buyback exit ($0.10) | -$64k | -$9,922 | Rejected — 6,397 early closes but trades barely change (7,013 vs 7,014); positions reach $0.10 after entry window closes |
| Premium buyback exit ($0.05) | -$3k | -$9,922 | Rejected — nearly free but useless; no new entries generated, just forfeit commission |
| VIX9D/VIX ratio filter (best case) | +$1,268 | -$9,922 | Rejected — 67 trades/4 years; ratio is VIX proxy (corr=0.455); zero signal in VIX 15-25 zone |

---

## Performance Breakdown

### By VIX Zone
| VIX Range | Win Rate | Avg P&L/trade | Notes |
|---|---|---|---|
| < 13 | ~84% | $55 | Weakest; 13–15 unprotected by dynamic SL |
| 13–15 | ~86% | $55 | Low zone |
| 15–20 | 98.5% | $78 | Sweet spot |
| 20–25 | 98.1% | $79 | Also strong |
| 25–30 | 68.0% | $3 | Protected by dynamic SL |

### PUT vs CALL Split
| Type | Win Rate | Avg P&L | Max DD |
|---|---|---|---|
| PUT | 93.7% | $68.54 | -$5,982 |
| CALL | 91.9% | $62.07 | -$9,922 |

All max drawdown comes from the CALL side.

### Premium Capture
- Overall capture rate: 67.6% (P&L / credit sold)
- Fill model: sell@bid / buy@ask (worst case) — costs ~$100k vs mid fills
- Realistic live fills at 40–50% toward mid would recover ~$80–100k
- Best zones: VIX 20–25 (82.1%), VIX 15–20 (77.4%)
- Worst zones: VIX <13 (11.6%), VIX 25–30 (10.7% — SL fires constantly)

### Gap Direction
- Gap-down + CALL: weakest combo, $369/day, 89.7% WR
- Gap-up + PUT: strongest combo, $530/day, 90.7% WR
- When VIX signal and gap signal disagree: higher avg P&L ($520/day) than when they agree ($468/day)
- Gap direction is NOT a useful confluence filter

### VIX Change Magnitude
- 0–1% change days: 83.5% WR, $238/day — lowest
- 5%+ change days: 93%+ WR, $580–680/day — strongest
- Even weak 0–1% days are net profitable; skipping always costs money

---

## Pending Upgrade

### Kelly Sizing — READY, LOCKED ON BP
- Full Kelly (max qty=4): P&L $1,168,607 (+92%), MaxDD -$19,844 (2×), Sharpe 13.18, Calmar 58.9
- Zone map: VIX <13→1, 13–15→3, 15–20→4, 20–25→4, 25–30→1, >30→4
- **Locked**: requires ~$77,720 peak BP; current account ~$40,000 (97% used at qty=2)
- **Action**: set `ENABLE_KELLY_SIZING = True` in `metf_v35_bidask.py` when BP reaches ~$80,000+

---

## Open Ideas

- **Net unusual premiums** — large institutional options sweeps/block prints as a pre-entry signal. Requires separate options flow data source. Most plausible remaining confluence candidate given VIX change is already the direction signal.
- **Black swan / tail-risk protection** — ✓ IMPLEMENTED 2026-03-28. See research log below.

---

## Black Swan Protection ✓ IMPLEMENTED — 2026-03-28

Two complementary layers implemented to protect the ~$40k account from catastrophic events.

### Layer 1: DAILY_SL = -20,000

If total daily P&L hits -$20,000, close all positions and halt entries for the day.

- **Zero P&L cost**: never fired once in 4yr backtest (2022–2026). Worst actual day was -$6,118.
- **Covers**: intraday crashes where the decline is gradual enough for the stop to execute.
- **Does not cover**: true instantaneous gaps (circuit-breaker opens) where all positions go max loss before the stop fires.
- **Tested values**: -$5k → $365,580 P&L (-$241k cost), -$10k → $497,722 (-$109k), -$15k → $578,172 (-$29k), -$20k → $606,832 ($0 cost).

### Layer 2: VIX_MAX_FILTER = 35.0

Skip entire trading day if VIX close > 35. Uses today's VIX close (from local parquet / Thetadata).

- **Covers**: most historical catastrophes — LTCM, GFC peak, COVID, Volmageddon, August 2024 carry unwind, April 2025 tariff shock.
- **Does not cover**: Flash Crash 2010 (VIX close 32.80, only spiked to 40 intraday) and early Lehman (damage spread over weeks). Both covered by Layer 1.
- **Cost**: only 8 days triggered VIX > 35 in 2022–2026 backtest. VIX 30+ zone has 97.9% WR — run marathon to quantify exact P&L impact.

### Historical VIX Levels on Major Crisis Days

| Date | Event | VIX Close | VIX High | Caught by VIX>35? |
|---|---|---|---|---|
| 1998-08-31 | LTCM/Russia | 44.28 | 45.02 | Yes |
| 2002-07-19 | Dotcom bottom | 38.17 | 38.17 | Yes |
| 2008-09-15 | Lehman collapse | 31.70 | 31.87 | No (DAILY_SL covers) |
| 2008-10-10 | GFC peak panic | 69.95 | 76.94 | Yes |
| 2010-05-06 | Flash Crash | 32.80 | 40.71 | No (close < 35; DAILY_SL covers) |
| 2011-08-08 | US Downgrade | 48.00 | 48.00 | Yes |
| 2015-08-24 | China Black Monday | 40.74 | 53.29 | Yes |
| 2018-02-05 | Volmageddon | 37.32 | 38.80 | Yes |
| 2020-03-09 | COVID circuit breaker 1 | 54.46 | 62.12 | Yes |
| 2020-03-12 | COVID worst day | 75.47 | 76.83 | Yes |
| 2020-03-16 | COVID peak VIX | 82.69 | 83.56 | Yes |
| 2024-08-05 | Yen carry unwind | 38.57 | 65.73 | Yes |
| 2025-04-04 | Tariff shock | 45.31 | 45.61 | Yes |
| 2025-04-07 | Tariff escalation | 46.98 | 60.13 | Yes |
| 2025-04-08 | Tariff peak | 52.33 | 57.52 | Yes |

### Theoretical Max Loss (Worst Case)

```
~10 positions open by 12:45, all go max loss simultaneously
= 10 × (20pt width - $0.55 credit) × 100 × 2 contracts
≈ $38,900 theoretical maximum
```
With `DAILY_SL = -20,000`: loss capped at ~$20,000 if crash is gradual. If instantaneous gap, all positions hit max loss before stop fires — account takes ~$38,900 hit and survives at ~$1,100. Defined-risk spreads prevent loss exceeding this.

### What Was Tested and Rejected

- **Pressure filter** (`ENABLE_PRESSURE_FILTER`): blocks new entries when any open position's short strike is within 27 pts of spot. Cost -$50k P&L — fires too often on the 92%+ win-rate days where positions are near but ultimately expire OTM. Same failure mode as Bayesian entry gate. Disabled.

### Additional Signal Candidates Ranked by Actionability

| Signal | Data Available | Actionability | Verdict |
|---|---|---|---|
| P/C OI ratio | Yes (2022–2025) | High — pre-market | ✗ Tested — redundant (see below) |
| VIX intraday spike | Yes (1-min bars) | Medium — fires after entry | ✗ Tested — negative (see below) |
| Prior-day VIX % chg | Yes (history CSV) | High — pre-market | ✗ Tested — negative (see below) |
| VVIX | No (free CBOE download) | High — pre-market | Not yet tested; signals vol-of-vol regime instability |
| CBOE SKEW | No (free CBOE download) | Medium | Not yet tested; slow-moving tail risk indicator |
| News sentiment | No (API needed) | Low | Not actionable without paid data source |

### P/C OI Ratio — ✗ TESTED NEGATIVE (2026-03-28)

**Result: redundant with VIX_MAX_FILTER.** The two most extreme P/C days (April 4, 2025: ratio=4.11, z=7.9; August 5, 2024: ratio=3.68, z=6.7) are already skipped by `VIX_MAX_FILTER = 35` since their VIX was 45 and 38 respectively.

All moderate-high P/C days (ratio 2.0–3.0, VIX < 35) turned out to be **profitable trading days**, not crash signals. These are quarter-end / month-end mechanical hedging events where institutions roll put hedges regardless of market direction.

| Threshold | Days | P&L on those days |
|---|---|---|
| P/C > 3.5 | 2 | $0 (already skipped by VIX_MAX_FILTER) |
| P/C > 2.5 | 6 | $1,920 profit forfeited |
| P/C > 2.0 | 30 | $11,956 profit forfeited |

Skipping based on P/C ratio at any threshold either costs P&L on safe days or adds no protection on days already caught by VIX filter.

### Prior-Day VIX % Change — ✗ TESTED NEGATIVE (2026-03-28)

**Result: opposite of expected.** Days following a large prior-day VIX spike are some of the *best* trading days, not the worst.

| Prior-Day VIX Chg | Days | Win Rate | Avg P&L |
|---|---|---|---|
| 5–10% | 100 | 90.0% | $728 |
| 10–15% | 49 | 83.7% | $683 |
| 15–20% | 24 | 75.0% | $662 |
| **> 20%** | **13** | **92.3%** | **$927** ← best bucket |

After a VIX shock day, the market tends to be range-bound or slowly recovering — MIN_OTM_DISTANCE=30 keeps strikes safely OTM. Aug 6, 2024 (after 65% VIX spike): +$884. Dec 19, 2024 (after 74% VIX spike): +$1,060. Skipping these days would cost P&L, not save it. `DAY_FILTER_VIX_CHG_MAX` remains `None`.

### VIX Intraday Spike Circuit Breaker — ✗ TESTED NEGATIVE (2026-03-28)

**Result: negative, same pattern.** Days with the largest intraday VIX spikes are above-average trading days.

| Intraday Spike Threshold | Days Affected | Avg P&L | Outcome |
|---|---|---|---|
| >= 20% | 19 days | $826 | Would cost P&L |
| >= 30% | 6 days | $821 | Would cost P&L |
| >= 40% | 3 days | $1,016 | Would cost P&L |
| >= 50% | 1 day | $1,080 | Would cost P&L |

Key finding: the Dec 18, 2024 VIX spike (57% intraday) first crossed 20% at 15:04 PM — *after the 12:45 entry window closed*. All entries were already placed and expired profitably at $1,080. The genuinely dangerous spike days (April 2025, VIX open >35) are already caught by `VIX_MAX_FILTER = 35`. MIN_OTM_DISTANCE=30 provides enough buffer to survive intraday VIX spikes that don't also blow out the absolute VIX level above 35.

---

## Statistical Models to Improve & Robustify the Strategy

### [1] Loss Classification Model ✓ DONE — 2026-03-28

**Result: negative.** Losses are regime-driven (VIX level), not multi-factor predictable.

- VIX alone explains 74% of signal (AUC 0.742); full model adds only +0.026 AUC
- 53% of losses live in VIX 25–30 (already handled by dynamic SL). No actionable filter found
- Safe-zone losses (VIX 15–25) cluster on flat EMA spread + CALL direction but too sparse (51 trades over 4 years) to build a reliable rule

---

### [2] Kelly-Inspired Dynamic Position Sizing by VIX Regime ✓ DONE — 2026-03-28

**Result: positive but locked by buying power.**

- Full Kelly works (+92% P&L, $607k → $1,168k) but requires ~$77,720 peak BP
- Current account has ~$40,000 (97% used at qty=2)
- Enable `ENABLE_KELLY_SIZING = True` in `metf_v35_bidask.py` when account BP reaches ~$80,000+
- Zone map: VIX <13→1, 13–15→3, 15–20→4, 20–25→4, 25–30→1, >30→4

---

### [3] GARCH(1,1) on VIX for a Better Direction Signal ✓ DONE — 2026-03-28

**Result: negative.** GARCH fits VIX well but adds no predictive value.

- Volatility clustering confirmed (lag-1 autocorr of squared returns = 0.23)
- AUC for loss day: VIX alone = 0.554, VIX + GARCH = 0.578 (+0.025 — not actionable)
- GARCH directional forecast is useless: P(VIX falls) ranges only 0.4967–0.4992 across all 947 days — mean VIX return is so close to zero that vol swamps the direction
- GARCH captures vol-of-vol; strategy losses are driven by absolute VIX regime, not vol-of-vol

---

### [4] Intraday Bayesian Updating ✓ DONE — 2026-03-28

**Result: negative.** Entry gate on `current_day_pnl` (live MTM) costs P&L at every threshold.

| Threshold | Trades | P&L | Max DD |
|---|---|---|---|
| Baseline (no gate) | 7,014 | $607,034 | -$9,922 |
| $0 | 1,314 | $95,694 | -$3,992 |
| -$150 | 5,925 | $493,880 | -$9,922 |
| -$300 | 6,313 | $527,578 | -$9,922 |
| -$400 | 6,469 | $543,234 | -$9,922 |

**Root cause:** Each position immediately shows ~-$29 paper loss from bid-ask spread. With 5–6 simultaneously open positions, the noise floor is -$150 to -$200 even on perfect win days. The gate fires on win-day entries that would have been profitable.

Pre-analysis using final P&L as proxy was misleading — correlated with day outcome by construction. Real intraday MTM (option quote mark) cannot separate B/A noise from true directional loss within the -$500 range the dynamic SL doesn't already cover.

**Future path:** A per-position delta signal (MTM change from entry mark, not absolute level) could isolate directional loss from B/A spread — but would require storing entry-time option marks and computing deterioration relative to that baseline.

---

### [5] VIX Term Structure Slope (VIX9D/VIX Ratio) — Pre-Entry Filter ✓ DONE — 2026-03-28

**Result: negative.** VIX9D/VIX ratio is mostly a proxy for VIX level (correlation = 0.455) and adds no independent signal in the zones that matter.

| Filter | Trades skipped | P&L impact |
|---|---|---|
| Skip PUT when ratio > 1.05 | 705 | -$55,700 |
| Skip PUT when ratio > 1.00 | 1,463 | -$120,418 |
| Skip ALL when ratio > 1.05 | 1,099 | -$87,454 |
| Skip ALL when ratio > 1.10 | 416 | -$39,386 |
| Skip PUT + VIX<13 + ratio > 1.00 | 67 | **+$1,268** (only positive case) |

The only positive filter (VIX <13 + backwardation) yields +$1,268 on 67 trades over 4 years (~17/yr). 64% of those trades (43 of 67) are already stopped by the dynamic SL. The remaining signal is not actionable.

In the main sweet spot (VIX 15–25, 4,096 trades), ratio has zero predictive power — WR ranges 97.8–100% across all ratio bins. The bad backwardation behavior overlaps completely with zones the SL already handles. Data saved at `data/vix9d_history.csv` (CBOE download).

**Paper:** Yoon (2022), *Journal of Futures Markets* — findings may apply to multi-day short-vol strategies; 0DTE is protected enough by the VIX-level dynamic SL that the term structure adds nothing.

---

### [6] Intraday Jump Detection — Pre-Entry Risk Screen

Use 1-minute SPX bars to detect statistically significant jumps in the pre-market or first 30 minutes. If a jump is detected before the entry window, skip all entries that session — intraday jumps propagate vol risk across the session and 0DTE gamma exposure becomes unmanageable.

**Method:** Bipower variation vs. realized variance ratio (Barndorff-Nielsen & Shephard 2004). If ratio implies a significant jump (p < 0.05), skip the session.

**Paper:** Božović (2025), SSRN #5223127 — "Intraday Jumps and 0DTE Options: Pricing and Hedging Implications." Shows standard delta hedging fails dramatically when intraday jumps occur — pricing error is systematic and large. Oxford JRSSSC (2025) — new jump detection method validated on S&P 500.

---

### [7] Variance Risk Premium (VRP) — Daily Position-Sizing Signal

VRP = VIX² − expected realized variance (from rolling realized vol forecast). When VRP is high (implied vol expensive vs. expected): collecting above-average premium. When VRP is low (implied vol cheap): reduce size or skip.

**What to test:** Halve size when VRP is in the bottom quintile of rolling 252-day history; allow full size in the top quintile. Use as a complement to the VIX-change direction signal.

**Paper:** Bollerslev, Tauchen & Zhou (2009), *Review of Financial Studies* — VRP predicts short-term equity returns. Papagelis (2025), *Journal of Futures Markets* — overnight VRP component specifically predicts short-horizon returns (most relevant for 0DTE).

---

### [8] HAR-RV (Heterogeneous Autoregressive Realized Volatility) — Danger Zone Filter

Models next-day realized vol using daily + weekly + monthly SPX realized vol components. HAR-RV doesn't predict direction — it predicts the *magnitude* of upcoming volatility. Use as a risk-scaling factor: reduce size when 1-day-ahead forecast exceeds the 90th-percentile rolling threshold AND VIX is in the 15–25 zone.

**Paper:** Corsi (2009), *Journal of Financial Econometrics* 7(2) — "A Simple Approximate Long-Memory Model of Realized Volatility." 2,100+ citations; the workhorse of realized vol forecasting.

---

### [9] Markov-Switching GARCH (MS-GARCH) — Regime-Aware Sizing

Replace the hand-coded VIX zone map with a 2–3 state MS-GARCH model fitted to daily SPX returns. The model learns latent vol states and transition probabilities without imposing manual thresholds. Use posterior P(high-vol state) to scale qty — reduce proportionally when P > 0.6.

Advantage over fixed VIX bins: current zones were calibrated in-sample. MS-GARCH detects regime shifts dynamically and outperforms single-regime GARCH for VaR at horizons under 1 week.

**Paper:** Hamilton (1989), *Econometrica* 57(2); Dueker (1997) GARCH extension; MSGARCH R package — *Journal of Statistical Software* 91(4), 2019.

---

### [10] CVaR / Expected Shortfall Optimization — Tail-Risk-Correct Position Sizing

Replace Kelly (maximizes expected log-utility, indifferent to tail severity) with CVaR-minimizing sizing per VIX regime. Kelly allows ruinous drawdowns as long as geometric mean is optimal — exactly the problem for short-vol strategies (thin right tail, fat left tail). CVaR explicitly prices the tail.

**What to test:** Using the 947-day backtest P&L series, compute the CVaR-optimal quantity for each VIX regime separately. Minimize 5% CVaR subject to a minimum expected return constraint (solvable as a linear program). Compare to Kelly-optimal sizes — difference will be largest in the VIX 25–30 zone.

**Paper:** Rockafellar & Uryasev (2000), *Journal of Risk* — "Optimization of Conditional Value-at-Risk."

---

### [11] IV Skew / Put-Call Risk Reversal — Directional Confluence Signal

Compute daily SPX 25-delta put IV minus 25-delta call IV at session open. Extreme put skew = market over-hedged downside → contrarian signal, favorable for PUT spreads. Extreme call skew = market fearful of upside.

**What to test:** If CALL skew > 2 vol points (calls priced richer than puts), skip the CALL spread entry that day. Requires SPX options chain at 9:35 AM; CBOE SKEW index is a daily proxy.

**Paper:** Rehman & Vilkov (2012) on risk-neutral skewness. ScienceDirect — "The SKEW Index: Extracting what has been left" (2020): implied skewness predicts market downturns and serves as a crisis indicator.

---

### [12] Realized GARCH — Better Overnight Vol Forecast as Entry Gate

Unlike GARCH(1,1) which uses only daily close-to-close returns, Realized GARCH ingests overnight realized volatility as a direct measurement equation input — exactly the window used by the VIX-change signal. Outperforms standard GARCH for 1-day-ahead SPX option pricing errors both in-sample and out-of-sample.

**What to test:** Fit on rolling 252-day windows. Use conditional variance forecast as a regime gate: skip or halve size when forecast variance is in the top quintile of its own history, regardless of VIX direction.

**Paper:** Hansen, Huang & Shek (2012), *Journal of Applied Econometrics* 27(6), 877–906. Option pricing extension: Huang (2017), *Journal of Futures Markets*.
