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
- **Action**: set `ENABLE_KELLY_SIZING = True` in `meds.py` when BP reaches ~$80,000+

---

## Open Ideas

- **Net unusual premiums** — large institutional options sweeps/block prints as a pre-entry signal. Requires separate options flow data source. Most plausible remaining confluence candidate given VIX change is already the direction signal.
- **Black swan / tail-risk protection** — ✓ IMPLEMENTED 2026-03-28. See research log below.

### Intraday Trend Reversal Detection (VIX 15–20 loss day problem)

**Problem:** 10 of 15 worst loss days are in VIX 15–20. Pattern: market drifts *with* positions during the 9:35–12:45 entry window, then reverses hard after close and hits multiple short strikes at expiry. By the time the reversal is obvious, the entry window is already closed. Pressure filter and entry cap (`MAX_TRADES_DAY_VIX_15_20=5`) are partial mitigations but do not detect the reversal itself.

**Ideas to explore:**

1. **Strike distance decay rate** — track how fast existing positions' average OTM distance is shrinking bar-by-bar. If distance decays by more than X pts over the last N bars, it means the market is trending against us even if positions are still technically OTM. Block new entries when decay rate exceeds threshold. Advantage: purely reactive to actual price action, no look-ahead. Implementation: compute `avg_dist_now - avg_dist_N_bars_ago` at each entry bar; if delta < -threshold, suppress entry.

2. **SPX momentum / rate-of-change filter** — measure the direction and magnitude of price movement over the last 30–60 min. If price is moving *against* the spread direction at a sustained rate (e.g., > 0.3% in 30 min), suppress new entries. This is an intraday momentum signal added *on top of* the VIX direction signal. Needs care to avoid the same failure mode as other intraday filters (blocking good days more than bad ones).

3. **Accumulated position heat** — stop new entries if the sum of unrealised P&L across all active positions crosses a negative threshold (e.g., -$300 cumulative open loss across all legs). This is similar to the Bayesian gate but uses mark-to-market loss on *open* positions rather than realised daily P&L. The Bayesian gate was rejected because it fired on winning days due to B/A spread noise; this variant uses a larger threshold to get above that noise floor.

4. **EMA crossover on SPX intraday bars** — compute a fast/slow EMA on the 1-min or 5-min intraday SPX bars *within the trading day*. If the fast EMA crosses below (for PUT days) or above (for CALL days) the slow EMA during the entry window, it signals the intraday trend has reversed and new entries should be suppressed. Key risk: same overfitting concern as the global EMA direction signal — need to verify on out-of-sample data.

**Caution:** All four ideas share the same failure mode as prior rejected filters — they may fire on *winning* days where the market temporarily moves against positions before recovering. Validate any implementation by checking not just the loss days but the ratio of correctly-blocked-loss-entries vs incorrectly-blocked-win-entries across the full 4-year backtest.

---

## Next Steps — VIX 15–20 Loss Day Research (2026-03-29)

Three options ranked by priority. Context: entry cap (`MAX_TRADES_DAY_VIX_15_20`) tested at 5 and 7 both failed — cost -$144k P&L with DD unchanged at -$6,894. The cap is too blunt, penalising ~1,400 winning trades/year to protect against ~5 reversal days.

### Option 1 — Isolate pressure filter impact ✅ TESTED 2026-03-29 — REJECTED
Filter only (PRESSURE_DISTANCE_THRESHOLD=45, VIX 15–20, no cap): P&L $472,730 vs baseline $606,832 (-$134k). DD unchanged at -$6,894 across all variants. Filter eliminated 2023-10-09 and 2025-01-06 from worst days but cost 1,297 trades and -$134k P&L. Not worth it — worst day (2023-06-15, VIX 14.4) is below the 15.0 filter floor and immovable regardless. Reverted to `ENABLE_PRESSURE_FILTER = False`.

Entry cap variants also tested: cap=5 (-$161k vs baseline), cap=7 (-$144k). Both failed — DD never moved, just bled P&L.

### Option 2 — Strike distance decay signal (surgical, future research)
At each entry bar, compute how much existing positions' average OTM distance has shrunk since they were originally entered. If the average decay exceeds X pts (e.g. 15–20 pts), the market is actively trending against open positions — suppress new entries for that bar. This is more targeted than a cap because it only fires when the market is actually moving against you, not on all VIX 15–20 days. Needs a sweep to find the right decay threshold. See "Intraday Trend Reversal Detection" section above for full implementation notes. **Hold until account grows and Kelly sizing is enabled — P&L growth is higher priority.**

### Option 3a — ✗ NOT VIABLE: Consecutive loss circuit breaker (2026-03-29)
Investigated. A consecutive-*closed*-loss counter cannot fire intraday on the worst days because all positions settle as a batch — not individually:

| Date | VIX | W/L | Close mechanism | Why circuit breaker fails |
|------|----:|-----|-----------------|--------------------------|
| 2023-10-09 | 17.7 | 3W/4L | EXPIRATION (15:59) | All 7 settle at EOD together |
| 2026-03-10 | 25.5 | 0W/10L | STOP_LOSS (14:15) | All 10 entries already open before SL fires |
| 2024-06-27 | 12.2 | 0W/5L | STOP_LOSS (11:29) | All 5 entries already open before SL fires |
| 2025-10-01 | 16.2 | 0W/2L | EXPIRATION (15:59) | Both settle at EOD together |

The correct version of this idea is to track **unrealized losses on open positions** intraday — halt new entries when N open positions are already underwater past a threshold. This is essentially Option 2 (strike distance decay signal). See that section.

### Option 3b — Untested: Entry window cutoff by VIX range (future research)
For VIX 15–20 days specifically, stop new entries earlier (e.g. 11:30 instead of 12:45). The failure mode on these days is market reversal *after* the entry window closes — later entries open with less time cushion and get caught by end-of-day moves. This is more surgical than the entry cap because it targets time-of-day rather than trade count. Would need a sweep over cutoff times (10:30 / 11:00 / 11:30 / 12:00 / 12:15) to find the right balance.

### Option 3c — Untested: Tighter per-trade SL when day is already negative (future research)
Apply a tighter per-position stop-loss only once total daily P&L is already negative (e.g. once down -$500 on the day, subsequent positions use -$150 SL instead of the standard dynamic SL). Targets the mixed-result days (6W/4L, 3W/7L) where early winners were wiped out by larger late-day losses. Unlike global EOM SL, this is dynamic and applies on any day that turns red. Risk: may over-tighten on days that recover.

### Option 3d — ✗ TESTED NEGATIVE: Per-position fixed SL (2026-03-29)
Sweep tested -$200 / -$300 / -$400 / -$500 / -$600 thresholds. All levels rejected.

| SL | P&L | P&L Delta | Max DD | Calmar | Sharpe |
|---|---|---|---|---|---|
| None (baseline) | $596,918 | — | -$6,894 | 86.6 | 14.05 |
| -$200 | $160,654 | -$436k | -$8,928 | 18.0 | 3.05 |
| -$300 | $211,548 | -$385k | -$10,714 | 19.8 | 3.56 |
| -$400 | $248,848 | -$348k | -$11,560 | 21.5 | 3.86 |
| -$500 | $293,058 | -$304k | -$9,880 | 29.7 | 4.53 |
| -$600 | $320,220 | -$277k | -$13,114 | 24.4 | 4.70 |

Same failure mode as per-position trailing stop — 93%+ expiry WR means positions that go temporarily negative mostly recover by EOD. Every threshold locks in the loss and misses the recovery, costing $276k–$436k P&L while making DD worse.

### Option 3e — Untested: Tighter daily SL for late-day entries (future research)
After a certain time of day (e.g. after 11:30), apply a tighter daily SL (e.g. -$2,000) to limit damage from positions that have less time to recover. The current DAILY_SL = -$20,000 is intentionally loose (black swan protection only). A tighter mid/late-day variant could cap days like 2026-03-10 where 10 entries are open by 14:15 with large unrealized losses. Risk: may fire on normal intraday volatility that resolves before EOD.

### Option 3f — Untested: Halt new entries on intraday trend reversal signal (future research)
Detect when the intraday market trend is reversing against open positions and halt new entries for the rest of the day. The failure mode on the worst loss days is a market that trends *with* positions during the morning entry window, then reverses sharply and hits multiple short strikes. If the reversal can be detected before the entry window closes, late-day entries that would open into the reversal can be blocked.

Candidate signals to detect reversal:
- **EMA cross**: short-term EMA (e.g. EMA5) crosses below long-term EMA (e.g. EMA20) on the intraday price series — signals trend flip
- **Price vs VWAP**: spot crosses below VWAP on a PUT day (or above on a CALL day) — institutional flow flipping direction
- **Rolling high/low break**: spot breaks below the session rolling high by X pts — momentum exhaustion signal
- **Average OTM distance decay**: existing positions' average OTM distance has shrunk by X pts from entry — market actively moving toward strikes (see Option 2)

Implementation approach: at each entry bar, compute the reversal signal. If triggered, set a `trend_reversed` flag and suppress all new entries for the remainder of the day (similar to `stopped_today` but without closing existing positions). Needs a parameter sweep over signal threshold and lookback window. Key risk: same failure mode as the Bayesian gate and pressure filter — may fire on temporary pullbacks on winning days that ultimately recover.

### Option 3 — Accept baseline, focus on P&L growth ✅ CURRENT DIRECTION (2026-03-29)
The baseline Calmar of 88.0 and DD of -$6,894 are already exceptional for a $40k account. All DD reduction attempts cost significant P&L with zero DD improvement. Focus shifts to growth:
- **Live fill improvement**: bid→mid fills on live trading could recover $80–100k P&L (backtest uses worst-case bid fills throughout)
- **Kelly sizing**: enable `ENABLE_KELLY_SIZING = True` once account reaches ~$80k — projected +$560k P&L over 4 years (+92%)
- **New signal research**: net unusual options flow / institutional sweeps as next confluence candidate

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
- Enable `ENABLE_KELLY_SIZING = True` in `meds.py` when account BP reaches ~$80,000+
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

### [6] Intraday Jump Detection — Pre-Entry Risk Screen ✗ TESTED NEGATIVE (2026-03-29)

Use 1-minute SPX bars to detect statistically significant jumps in the pre-market or first 30 minutes. If a jump is detected before the entry window, skip all entries that session.

**Method:** Bipower variation vs. realized variance ratio (Barndorff-Nielsen & Shephard 2004). Skip if p < 0.05.

**Result: negative, same pattern as all volatility filters.** Jump days outperform no-jump days.

| | Days | Avg P&L | Win Rate |
|---|---|---|---|
| Jump detected (p<0.05) | 167 | $654 | 89.2% |
| No jump (p≥0.05) | 783 | $623 | 86.6% |

Skipping at p<0.05 costs -$109k. Skipping at p<0.01 costs -$53k. The signal fires most often on high-volatility sessions where MIN_OTM_DISTANCE=30 still provides enough buffer to expire OTM.

**Paper:** Božović (2025), SSRN #5223127 — "Intraday Jumps and 0DTE Options: Pricing and Hedging Implications."

---

### [7] Variance Risk Premium (VRP) — Daily Position-Sizing Signal ✗ TESTED NEGATIVE (2026-03-29)

VRP = VIX² − expected realized variance (from rolling realized vol forecast). When VRP is high (implied vol expensive vs. expected): collecting above-average premium. When VRP is low (implied vol cheap): reduce size or skip.

**Result: negative.** VRP quintile gradient exists but is too weak to act on.

| VRP Quintile | Days | Avg P&L/day | Win Rate |
|---|---|---|---|
| Q1 (cheapest) | 185 | $553 | 86.5% |
| Q2 | 180 | $502 | 88.3% |
| Q3 | 182 | $640 | 86.3% |
| Q4 | 184 | $700 | 89.7% |
| Q5 (richest) | 180 | $726 | 86.1% |

Half-sizing in the bottom 20% VRP quintile costs -$60,797 P&L to reduce max DD from -$6,894 to -$6,118 — a terrible tradeoff. Even negative VRP days (realized vol > implied vol, premium literally cheap) averaged $781/day with 86.7% WR — *above* the positive VRP average. The strategy earns from theta decay on 30+ pt OTM strikes regardless of how expensive implied vol is relative to realized.

**Paper:** Bollerslev, Tauchen & Zhou (2009), *Review of Financial Studies* — VRP predicts short-term equity returns. Papagelis (2025), *Journal of Futures Markets* — overnight VRP component specifically predicts short-horizon returns (most relevant for 0DTE).

---

### [8] HAR-RV (Heterogeneous Autoregressive Realized Volatility) — Danger Zone Filter ✗ TESTED NEGATIVE (2026-03-29)

Models next-day realized vol using daily + weekly + monthly SPX realized vol components. Proposed use: reduce size when forecast in top quintile.

**Result: negative — gradient is backwards.** High HAR-RV forecast days are the best, not worst, trading days.

| HAR-RV Quintile | Days | Avg P&L | Win Rate |
|---|---|---|---|
| Q1 (low vol forecast) | 134 | $320 | 86.6% |
| Q2 | 135 | $458 | 85.9% |
| Q3 | 134 | $607 | 90.3% |
| Q4 | 134 | $874 | 97.8% ← best |
| Q5 (high vol forecast) | 133 | $720 | 85.7% |

Half-sizing on high-vol forecast days costs -$28k to -$44k. The Q1→Q4 gradient ($320→$874) is strong and would justify *increasing* size on high-forecast days — but account is already at 97% BP. Revisit when Kelly sizing is unlocked (~$80k account).

**Paper:** Corsi (2009), *Journal of Financial Econometrics* 7(2) — "A Simple Approximate Long-Memory Model of Realized Volatility."

---

### [9] Markov-Switching GARCH (MS-GARCH) — Regime-Aware Sizing ✗ TESTED NEGATIVE (2026-03-29)

Replace hand-coded VIX zone map with a 2-state GMM regime model. Use P(high-vol state) to scale qty down when regime probability exceeds threshold.

**Result: negative — superseded by VIX zone mapping.** GMM probabilities max out at 0.39; no day exceeds 0.5 threshold. The quartile gradient exists ($707→$444/day from low→high-vol quartile) but is already captured more cleanly by the existing VIX zone map. The `KELLY_ZONE_QTY` with explicit VIX thresholds is simpler, more interpretable, and already implemented. MS-GARCH adds complexity without adding new information.

**Paper:** Hamilton (1989), *Econometrica* 57(2); Dueker (1997) GARCH extension; MSGARCH R package — *Journal of Statistical Software* 91(4), 2019.

---

### [10] CVaR / Expected Shortfall Optimization — Tail-Risk-Correct Position Sizing ✗ TESTED NEGATIVE (2026-03-29)

Replace Kelly with CVaR-minimizing sizing per VIX regime.

**Result: negative — linear scaling makes CVaR optimization trivial.** Since both E[P&L] and CVaR scale linearly with qty, the return/risk ratio (Sharpe) is identical at any fixed qty. CVaR minimization simply recommends qty=1 everywhere — cutting P&L in half without improving the Sharpe ratio at all.

| Sizing Method | P&L | Max DD | CVaR (5%) | Sharpe |
|---|---|---|---|---|
| Flat qty=2 (current) | $596,788 | -$6,894 | -$1,357 | 14.16 |
| CVaR-min (qty=1) | $298,394 | -$3,447 | -$678 | 14.16 (identical) |
| Kelly zones | $1,130,372 | -$12,236 | -$2,186 | 14.75 |

Kelly actually improves Sharpe (14.75 vs 14.16) by concentrating size in best VIX zones. The paper's concern about "ruinous drawdowns" from Kelly doesn't apply here — max DD is -$6,894 on a $40k account, already tiny. CVaR adds no value beyond what Kelly + dynamic SL already provide.

**Paper:** Rockafellar & Uryasev (2000), *Journal of Risk* — "Optimization of Conditional Value-at-Risk."

---

### [11] IV Skew / Put-Call Risk Reversal — Directional Confluence Signal ✗ TESTED NEGATIVE AS FILTER (2026-03-29)

Proxy skew = mid(25-delta PUT) − mid(25-delta CALL) at 9:35 AM (IV unavailable in data; price-based proxy used instead). 967 days of data (2022–2025).

**Result: signal exists but not actionable as a skip/reduce filter.**

| Skew Quintile | Avg P&L | Win Rate |
|---|---|---|
| Q1 (call skew, lowest) | $459 | 83.8% |
| Q3 | $679 | 88.8% |
| Q5 (put skew, highest) | $733 | 89.2% |

Monotonic gradient ($459→$733) confirms put skew = favorable, call skew = weaker. But skipping CALL days on negative-skew costs -$12k (26 days, mostly still profitable). The signal again suggests *increasing* size on high-skew days — only useful when Kelly sizing is unlocked (~$80k account).

**Interesting directional finding:** PUT days with positive skew avg $705 vs $392 on negative-skew days. Sample too small (66 negative-skew days) to rely on. Revisit with more data.

**Paper:** Rehman & Vilkov (2012) on risk-neutral skewness. ScienceDirect — "The SKEW Index: Extracting what has been left" (2020).

---

### [12] Realized GARCH — Better Overnight Vol Forecast as Entry Gate ✗ TESTED NEGATIVE (2026-03-29)

Extends GARCH by including overnight RV (proxied by first 30-min SPX vol + open jump). Walk-forward OLS fit on rolling 252-day windows.

**Result: same pattern as HAR-RV.** Higher vol forecast = better trading days, not worse.

| Realized GARCH Quintile | Days | Avg P&L | Win Rate |
|---|---|---|---|
| Q1 (low forecast) | 134 | $353 | 87.3% |
| Q3 | 134 | $647 | 92.5% |
| Q4 | 134 | $753 | 94.8% |
| Q5 (high forecast) | 134 | $760 | 86.6% |

Half-sizing top 20% costs -$44k. The Q1→Q5 gradient ($353→$760) reinforces that high-vol forecast days are the strategy's best days. Adding overnight vol input over HAR-RV adds no new insight. Revisit as a size-*increase* signal when Kelly sizing is unlocked.

**Paper:** Hansen, Huang & Shek (2012), *Journal of Applied Econometrics* 27(6), 877–906.

---

### [13] VIX Futures Backwardation — Crash Warning Signal
*(Journal of Portfolio Management — "VIX Term Structure and Future Stock Returns")*

**Concept:** VIX futures normally trade in contango (longer-dated > spot). When the curve flips to backwardation (spot VIX > near-term futures), the market is pricing in an immediate, high-probability crash — "smart money" is paying up for near-term protection.

**Relationship to [5]:** [5] tested VIX9D/VIX as a term structure proxy and found it negative (0.455 correlation with VIX level, no independent signal). **True VIX futures backwardation is a different and stronger signal** — VIX9D is a 9-day implied vol index, not a traded futures contract. The futures basis (cash vs front-month futures) has a distinct economic meaning: it represents the actual cost of rolling hedges and the market's directional bet on near-term volatility.

**Data needed:** VIX futures front-month close (VX1) — available from CBOE or Quandl/FRED. Free download.

**What to test:** On days where spot VIX > VX1 (backwardation), compare P&L vs contango days. Also test as a skip/reduce filter specifically in the VIX 15–25 zone where the dynamic SL doesn't already fire.

**Feasibility:** High — one CSV download from CBOE. Worth testing given it's mechanistically different from [5].

---

### [14] Variance Risk Premium Revisit — JF Formulation
*(Journal of Finance — "The Variance Risk Premium and the Predictability of Stock Returns")*

**Concept:** VRP = implied variance (VIX²) − realized variance. When VRP narrows or flips negative (realized vol exceeds implied), "insurance" is underpriced relative to actual risk — historically precedes vol spikes.

**Relationship to [7]:** **Already fully tested in [7] — negative.** The gradient existed (Q1: $553 → Q5: $726/day) but both E[P&L] and CVaR scale linearly with qty, so no sizing improvement is possible without increasing BP. Negative VRP days (realized > implied) actually averaged $781/day with 86.7% WR — better than positive VRP days.

**Verdict: do not retest.** The JF paper's predictability finding applies to equity index returns, not to short-premium 0DTE P&L. The strategy's edge comes from theta decay and OTM distance, not from implied vol being expensive.

---

### [15] True IV Skew — Volatility Smile Slope
*(Quantitative Finance — "Extracting Risk-Neutral Probability Density Functions")*

**Concept:** When puts become exponentially more expensive than calls (steepening skew), "smart money" is buying crash protection en masse. A steepening slope signals tail-risk accumulation before a spike.

**Relationship to [11]:** [11] tested a **price-based proxy** (mid(25-delta PUT) − mid(25-delta CALL)) since the IV column in the option greeks data is all zeros. The proxy showed a meaningful monotonic gradient ($459→$733 Q1→Q5) but was not actionable as a filter due to small sample size on negative-skew days.

**What's needed for true IV skew:** Either (a) compute IV from first principles using Black-Scholes with the bid/ask mid price and current SPX spot — all inputs are available (strike, expiry, rate, spot, option price); or (b) obtain a pre-computed IV feed.

**Feasibility:** Medium — computing IV from bid/ask mid + BSM is straightforward. The delta column already provides an approximation, but the exact 25-delta strikes shift daily. Worth implementing if the price-proxy gradient ($274/day Q1→Q5) strengthens with true IV. Priority: test after VIX futures backwardation [13].

**Extension (from VIX Decomposition whitepaper):** Once true IV is computed, also test the **Wings vs. Shoulders ratio** — 10-delta IV divided by 30-delta IV ("skew of skew"). When the wings (10-delta, deep OTM) rise significantly faster than the shoulders (30-delta, where MEDS trades), professional hedgers are pricing in crash risk even while ATM vol looks calm. Use as an early warning signal: if wings/shoulders ratio spikes above a threshold, tighten stop losses or reduce size. This requires true IV first — the IV column in existing data is all zeros.

---

### [16] Kalman Filter — ✗ SKIP

**Concept:** Adaptive price trend smoother that adjusts to regime changes in real-time. Flat/choppy Kalman slope = ranging; steep slope = trending.

**Verdict: do not test.** Every direction/trend signal tested (RSI, MACD, SMA200, momentum, stochastic) returned negative P&L. The strategy's VIX direction signal only adds ~$19k over 4 years — better trend filtering will not improve on that. Same failure mode as all prior direction alternatives.

---

### [17] Parkinson Volatility Estimator — ✗ SKIP

**Concept:** Intraday volatility estimator using high-low range instead of close-to-close. More sensitive to intraday vol moves than standard realized variance.

**Verdict: do not test.** Already covered by VRP [7], HAR-RV [8], and Realized GARCH [12] — all negative. High-volatility days are consistently *better* for this strategy (MIN_OTM_DISTANCE=30 provides structural buffer). A more accurate vol estimator won't change this finding.

---

### [18] Hurst Exponent — LOW PRIORITY

**Concept:** Measures market regime — trending (H > 0.5), mean-reverting (H < 0.5), or random walk (H ≈ 0.5). H < 0.5 should theoretically favor credit spreads (choppy = less likely to move far).

**Assessment:** Conceptually the most novel of the "regime filter" family — it classifies *type* of movement, not just magnitude. But the consistent empirical pattern across all regime filters tested is that the "dangerous" regime days are actually profitable. Low probability of a different outcome. Would test as a sizing signal (not a skip filter) after [13] and [15] are completed.

---

### [19] VVIX/VIX Ratio — WORTH TESTING

**Concept:** VVIX measures volatility-of-volatility (how uncertain the market is about future VIX levels). The VVIX/VIX ratio captures instability of VIX itself relative to its current level. Readings above 6 = vol instability; below 4 = vol stability.

**Why different:** This is the only signal in this group targeting *signal quality* rather than market regime. When VIX is itself unpredictable (high VVIX/VIX), the prior-day VIX change direction signal becomes less reliable — the day-to-day VIX reading is noisy. This hypothesis is mechanistically distinct from all prior tests.

**Lead-lag refinement (2026-03-29):** Federal Reserve research confirms VVIX negatively predicts tail risk hedge returns 3–4 weeks ahead. More importantly, the key early warning pattern is **VVIX spiking while VIX is still subdued** — not just the ratio alone. When VVIX > 100 and VIX < 20, options traders are pricing in future vol stress before it hits the headline VIX number. Conversely, when VIX spikes WITHOUT a matching VVIX move, the vol tends to be transitory and fade quickly (no regime change). This means the combined condition (high VVIX + low VIX) is a stronger signal than VVIX/VIX ratio alone.

**What to test:**
1. VVIX/VIX ratio buckets: compare P&L on ratio >6 vs <4 days
2. Combined condition: VVIX > 100 AND VIX < 20 (early warning flag) — skip or reduce
3. Asymmetric VIX spike filter: VIX > 25 AND VVIX flat (transitory) → hold full size; VIX subdued AND VVIX spiking → reduce

**Data needed:** Daily VVIX close — available free from CBOE (vvix_history.csv).

**Feasibility:** High — one CSV download. Priority: test after [13]. Probability of finding something actionable: low given the consistent pattern, but the lead-lag refinement makes the hypothesis more precise.

---

### [20] GEX (Gamma Exposure) / Dealer Positioning — WORTH TESTING

**Concept:** Net dealer gamma exposure (GEX) measures whether market makers are net long or net short gamma. When dealers are net long gamma (positive GEX), they hedge by fading price moves — selling rallies, buying dips — which makes markets mean-reverting. This is the ideal environment for credit spreads. When dealers are net short gamma (negative GEX), they must hedge by chasing moves, amplifying volatility — the worst environment for short premium.

**Why different from all prior tests:** This is a market microstructure signal, not a volatility forecasting signal. It doesn't predict *how much* vol there will be — it predicts *how the market will behave* given the existing vol. All prior tests (VRP, HAR-RV, MS-GARCH, Hurst) measured volatility magnitude or regime; GEX measures the mechanical hedging behavior of the dominant liquidity providers.

**What to test:** Bucket trading days by prior-day GEX sign (positive vs negative) and by GEX magnitude quintiles. Compare P&L, WR, and max DD. Test as a skip filter on deeply negative GEX days and as a sizing signal on strongly positive GEX days.

**Data needed:** Daily GEX — published by SpotGamma (paid) or SqueezeMetrics (free tier available). Also computable from CBOE open interest data if available.

**Feasibility:** Medium — depends on data source access. SpotGamma historical data requires subscription. SqueezeMetrics offers some free data. Priority: test after [13] and [19].

---

### [21] MOVE/VIX Ratio — WORTH TESTING

**Concept:** MOVE index measures bond market implied volatility (equivalent of VIX for Treasuries). The MOVE/VIX ratio captures cross-asset vol divergence. When MOVE is elevated relative to VIX, bond markets are pricing in significantly more uncertainty than equity markets — historically, equity vol tends to catch up. High MOVE/VIX ratio = potential equity vol spike incoming.

**Why different:** All prior signals were purely equity/options-based. MOVE is a cross-asset signal — it captures macro/rates uncertainty that can precede equity dislocations before they show up in VIX. LTCM (1998), GFC (2008), and COVID (2020) all saw MOVE spike before or alongside VIX.

**What to test:** Compute prior-day MOVE/VIX ratio. Bucket trading days into quintiles. Compare P&L and WR across buckets. Test as a skip filter on extreme high-ratio days (top decile) and as a sizing reducer.

**Data needed:** Daily MOVE index close — available free from FRED (BAMLMOVE index) or ICE/Bloomberg. VIX already available in existing parquet files.

**Feasibility:** High — one FRED CSV download. Straightforward to merge with existing VIX data. Priority: test alongside [19] (both are one-file downloads).

---

### [22] QQQ/SPY Correlation — LOW PRIORITY

**Concept:** Use the rolling correlation between QQQ (Nasdaq-100) and SPY (S&P 500) as a market internals regime signal. When correlation breaks down, sector rotation is occurring — potential instability. Also testable as a direction signal: QQQ outperforming SPY (tech leading) = risk-on = CALL spreads safer; SPY outperforming (defensive/value leading) = risk-off = PUT spreads safer.

**Why weaker than [21]:**
- SPY and ES are essentially the same instrument (~0.999 correlation) — no independent signal between them.
- QQQ/SPY is within-equity. Equity-equity correlations *spike* during crises (everything sells off together), which is the opposite of a useful early warning signal.
- [21] MOVE/VIX captures cross-asset divergence (bonds vs equities) — a fundamentally different and more macro information source.
- Every directional alternative (RSI, MACD, gap, SMA200, momentum) already failed; QQQ/SPY ratio as a direction signal is unlikely to break that pattern.

**What to test if pursued:** (a) Rolling 20-day QQQ/SPY correlation as a regime filter — skip or reduce on low-correlation days. (b) Prior-day QQQ/SPY return ratio as a direction signal — QQQ outperforming → CALL, underperforming → PUT.

**Data needed:** Daily QQQ and SPY OHLC — freely available from Yahoo Finance or existing data sources.

**Feasibility:** High — data easily sourced. But low priority given weak theoretical basis relative to [21] and consistent failure of within-equity regime signals. Test only after [13], [19], [20], and [21] are completed.

---

### [24] DSPX (Dispersion Index) — WORTH TESTING

**Concept:** DSPX measures the spread between single-stock implied volatility and S&P 500 index implied volatility. When this spread is elevated, the "dispersion trade" is crowded — hedge funds are short index vol and long single-stock vol. When correlation suddenly spikes (all stocks move together), dispersion traders face rapid losses and are forced to unwind simultaneously, creating violent, self-reinforcing market moves. This is the mechanism behind Feb 2018 Volmageddon.

**Key metric:** DSPX − VIX spread. Historical average: ~5–15 points. When spread exceeds 20 points (85th percentile), the trade is dangerously crowded. At these levels, any correlation shock can cascade into a vol spike far larger than the underlying macro event would justify.

**Why different from all prior signals:** This is a market crowding / positioning signal, not a vol-forecasting or momentum signal. It captures structural fragility — the market is fine until it isn't. Mechanistically distinct from VRP [7], HAR-RV [8], VVIX [19], and GEX [20].

**What to test:** Download daily DSPX and compute the DSPX−VIX spread. Bucket trading days by spread quintile. Test as a skip/reduce filter when spread > 20 (crowded) — specifically check if P&L degrades on high-spread days.

**Data needed:** Daily DSPX close — published by CBOE. May require CBOE data subscription or Bloomberg. Check if freely available via CBOE website.

**Feasibility:** Medium — data availability unclear (CBOE may not publish historical DSPX freely). Priority: investigate data access first, then test alongside [20] GEX. If data is paywalled, skip.

---

### [25] Dynamic Delta-Adjusted Strike Distance — LOW PRIORITY

**Concept:** Replace the fixed `MIN_OTM_DISTANCE=30` with a VIX-adjusted distance that maintains a constant short-strike delta (~10-delta) regardless of VIX level. From the CBOE VIX Decomposition whitepaper (Exhibit 8), the delta-to-moneyness mapping changes significantly with VIX:

| VIX Level | 10-delta put OTM % | Equivalent SPX pts (SPX=5500) |
|---|---|---|
| 15 | ~3.8% | ~209 pts |
| 20 | ~5.0% | ~275 pts |
| 25 | ~6.2% | ~341 pts |
| 30 | ~7.4% | ~407 pts |

At current fixed 30pts, the delta of the short strike shrinks as VIX rises — the strike becomes relatively *safer* in delta terms but not in point terms. A VIX-adjusted distance would push strikes further out as VIX rises, theoretically maintaining constant probability of profit.

**Why likely counterproductive:** High-VIX days (15–25) are the strategy's best days (98%+ WR). Pushing strikes further out on these days reduces credit received, risks dropping below `MIN_NET_CREDIT=0.55`, and reduces the number of qualifying entries — all of which cost P&L. The fixed 30-pt distance was sweep-validated and the VIX 25–30 zone is already handled by `ENABLE_DYNAMIC_SL`.

**What to test if pursued:** Implement a `VIX_ADJUSTED_OTM` function that scales `MIN_OTM_DISTANCE` linearly with VIX (e.g., base 30pts at VIX 15, scaled up proportionally). Compare total P&L, WR, and entry count vs. fixed 30pts baseline.

**Priority:** Low — after [13], [15], [19], [20], [21]. Risk of hurting P&L is real given consistent pattern that high-VIX days are best days.

---

### [23] Intraday Price Action Signals — ✗ SKIP (Category Rejected)

The following signals were evaluated and rejected as a category. All are intraday price/momentum-based and share the same failure mode as prior tested signals.

| Signal | Concept | Reason to Skip |
|---|---|---|
| Minimum price swing | Only enter if SPX has moved ≥ X pts from open | Same failure mode as Bayesian entry gate — fires on win days, costs P&L |
| Max retracement | Skip if market pulled back > X% from today's high | Pressure filter reframed — already cost -$50k |
| Minimum volume spike | Require volume confirmation before entry | SPX index has no volume; ES/SPY proxy is noisy; 0DTE entries span 9:35–12:45 anyway |
| Momentum score | Composite of RSI, MACD, momentum oscillator | Combination of individually-failed signals; redundant |
| Max gap % of swing | Overnight gap as % of total daily range | Variation on gap direction signal — already tested negative |

**Root cause:** The strategy's structural edge (theta decay + MIN_OTM_DISTANCE=30) is immune to intraday price-behavior filters. Every attempt to gate entries based on how the market is moving intraday results in filtering out winning positions. The premium in the spread IS the edge — preventing entry costs money.

---

## Statistical Validation ✓ COMPLETED (2026-03-29)

### Completed Tests

**t-test:** t=27.50, p=6.5e-123. Edge is not random. Trivially significant — not worth re-running.

**Bootstrap confidence intervals (10,000 resamples):**
| Metric | Observed | 95% CI |
|---|---|---|
| Sharpe | 14.16 | [11.79 – 17.12] |
| Win Rate | 87.1% | [84.8% – 89.2%] |
| Max Drawdown | -$6,894 | [-$9,488 – -$3,370] |

**Permutation test:** Sharpe and total P&L are order-invariant (shuffling trade dates doesn't change them — credit spread P&L accrues at expiry regardless of sequence). Max DD IS sequence-dependent: observed -$6,894 is near the permutation mean of -$6,555 (p=0.19 — not unusually lucky or unlucky). A proper permutation test for this strategy should shuffle the VIX direction signal, not trade dates.

**Walk-forward / year-by-year:**
| Year | P&L | Sharpe | WR | Max DD |
|---|---|---|---|---|
| 2022 | $140,470 | 13.92 | 78.0% | -$3,936 |
| 2023 | $123,666 | 11.83 | 91.6% | -$6,118 |
| 2024 | $123,828 | 13.51 | 84.6% | -$6,894 |
| 2025 | $175,400 | 18.15 | 93.6% | -$3,370 |
| 2026 YTD | $33,424 | 13.47 | 87.3% | -$2,940 |

Every year profitable. Sharpe never below 11.83. Strongest evidence of robustness.

**Monte Carlo (10,000 paths, 957 days each with replacement):**
- P(lose money over full period): **0.00%**
- P(Max DD > $10,000): **1.7%**
- P(Max DD > $15,000): **0.0%**
- P(Max DD > $20,000): **0.0%**
- DAILY_SL = -20,000 is effectively unreachable under resampled normal conditions.

### Direction Signal Permutation Test ✓ COMPLETED (2026-03-29)

**Question:** Does the VIX direction signal (PUT vs CALL) add real value, or does the strategy win purely from theta decay regardless of direction?

**Method:** Using 950 trading days from the trade log (561 PUT days, 389 CALL days), compared per-day P&L distributions between PUT-assigned and CALL-assigned days. Permutation null: any random assignment of 561/389 days to PUT/CALL should give similar P&L totals.

**Results:**
| Metric | Value |
|---|---|
| Observed total P&L | $596,788 |
| Estimated P&L if signal FLIPPED | $577,598 |
| Signal advantage vs contrarian | +$19,190 |
| PUT days avg P&L/day | $674 (561 days, median $974) |
| CALL days avg P&L/day | $562 (389 days, median $706) |
| Trade-level WR: PUT days | 89.5% |
| Trade-level WR: CALL days | 89.6% |
| t-test (PUT vs CALL day quality) | t=2.41, p=0.016 |
| Permutation p-value (split significance) | p=0.007 |

**Conclusion:** The strategy is primarily a **theta decay engine** — both PUT and CALL days are highly profitable with nearly identical 89.5–89.6% win rates. The VIX direction signal adds ~$19k of incremental value (statistically significant, p=0.016), but the dominant driver is theta. The larger PUT day P&L ($674 vs $562) reflects the well-known **volatility risk premium**: PUT sellers consistently capture a structural premium. The VIX signal correctly assigns PUT on 59% of days (when VIX fell = calmer next day = PUT spread safer). Flipping the signal only costs ~$19k over 4 years — the strategy would remain highly profitable even with inverted direction logic.

---

## Engineering — Persistent Quote Cache

**Problem:** Every run (marathon or sweep) re-fetches all historical options quote data from the Thetadata API. A 4-year backtest makes thousands of API calls per run. Sweeps multiply this — a 6-level sweep makes 6× the calls. This is the main bottleneck for iteration speed.

**Proposed solution: disk-based persistent quote cache**

Save each day's fetched quote data to disk on first access. On subsequent runs, load from disk instead of hitting the API. Since historical data never changes, the cache is permanent and never needs invalidation.

**Implementation options:**

| Option | Speed | Complexity | Notes |
|--------|-------|------------|-------|
| **Parquet per day** | Fast | Low | Matches existing VIX data pattern; one file per trading day in `data/quote_cache/YYYYMMDD.parquet` |
| **SQLite** | Medium | Medium | Single file, queryable; good for ad-hoc analysis but slower than parquet for bulk reads |
| **Redis** | Fastest | High | True in-memory; survives between runs but requires a running server |

**Recommended: parquet per day** — zero new infrastructure, same pattern as VIX data already in the repo.

**Expected impact:**
- First run: same speed as today (fetches and writes cache)
- Subsequent runs: near-instant data load — all API calls replaced by local disk reads
- Sweeps: 6-level sweep would run at the same speed as a single run today

**Where to hook in:** `fetch_quotes_for_strikes_cached()` — check for a cached parquet file before calling the API. If found, load from disk. If not, fetch from API and write to disk.
