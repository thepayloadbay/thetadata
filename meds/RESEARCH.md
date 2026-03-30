# MEDS Strategy — Research Log

All tested hypotheses, experiment results, performance deep-dives, and decision rationale.

---

## Strategy Baseline

**MEDS — Multi-Entry Directional Spreads**

SPXW 0DTE credit spreads (20-pt wide, qty=2), entries every 20 min from 9:35–12:45. Direction signal: prior-day VIX change — VIX fell → PUT spread, VIX rose → CALL spread. Signal is locked for the whole day.

**Confirmed baseline (2022-01-03 → 2026-03-25):**

| Metric | Value |
|---|---|
| Total P&L | $615,220 |
| Max Drawdown | -$6,356 |
| Sharpe | 14.55 |
| Calmar | 96.8 |
| Win Rate | 93.4% |
| Trades | 6,950 |

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

### SL Gap Re-entry — KEEP
- `ENABLE_SL_GAP_REENTRY = True`, `SL_GAP_MINUTES = 60`
- After batch SL fires, wait 60 min then allow re-entry
- Sweep confirmed 60 min optimal (+$4,458, Sharpe 14.15); DD unchanged

  | Gap | Trades | WR% | P&L | Delta | Sharpe |
  |---|---|---|---|---|---|
  | None (baseline) | 6,929 | 93.1% | $607,554 | — | 13.90 |
  | 30 min | 7,114 | 92.4% | $611,914 | +$4,360 | 14.07 |
  | **60 min** | **7,042** | **92.7%** | **$612,012** | **+$4,458** | **14.15** |
  | 90 min | 6,995 | 92.9% | $610,150 | +$2,596 | 14.04 |
  | 120 min | 6,957 | 93.0% | $607,802 | +$248 | 13.89 |

---

## Tested & Rejected

| Test | P&L | Max DD | Decision |
|---|---|---|---|
| Skip VIX 25–30 | $597,112 | -$9,922 | Rejected — $10k P&L cost, DD unchanged, Sharpe gain cosmetic |
| No SL on CALL days | -$29k (full run) | -$24k (3x) | Rejected — sweep was unreliable (+$92k was false) |
| Gap-down CALL SL -$300 | -$69k vs baseline | -$6,338 | Rejected — DD improved but P&L cost too high |
| Month/direction SL rules | -$49k vs baseline | — | Rejected — blanket monthly rules don't account for VIX variation |
| Intraday Bayesian entry gate | -$64k best case | -$9,922 | Rejected — B/A spread noise floor defeats the signal |
| Premium buyback exit ($0.10) | -$64k | -$9,922 | Rejected — 6,397 early closes but trades barely change; positions reach $0.10 after entry window closes |
| Premium buyback exit ($0.05) | -$3k | -$9,922 | Rejected — nearly free but useless; no new entries generated |
| VIX9D/VIX ratio filter (best case) | +$1,268 | -$9,922 | Rejected — 67 trades/4 years; ratio is VIX proxy (corr=0.455); zero signal in VIX 15-25 zone |
| Stop after 2 consecutive loss days | -$4,382 net | — | Rejected — market recovers after 2 loss days; stopping misses the bounce |
| Per-position fixed SL (-$200 to -$600) | -$276k to -$436k | worse | Rejected — 93%+ expiry WR means positions recover; SL locks in losses |
| Per-position trailing stop | all worse | — | Rejected — same root cause as fixed SL |
| Pressure filter (27pt threshold) | -$50k | -$9,922 | Rejected — fires on win-day positions that are OTM but nearby |
| P/C OI ratio skip filter | redundant | — | Rejected — extreme days already caught by VIX_MAX_FILTER |
| Prior-day VIX % change filter | opposite | — | Rejected — days after large VIX spikes are the BEST bucket |
| Intraday VIX spike circuit breaker | negative | — | Rejected — large intraday VIX spikes average $826/day above baseline |
| VRP half-sizing (bottom 20%) | -$61k | -$776 DD improvement | Rejected — gradient too weak to act on |
| VIX 15-20 dynamic SL (all levels) | -$99k to -$173k | worse | Rejected — 2,871 trades at 97.8% WR; any SL fires on too many winners |
| Calendar SLs (CPI/PCE/Pre-TW/Post-Hol) | all best with SL OFF | — | Rejected — calendar events are net profitable; SLs just cut winners |
| Low/mid-VIX half sizing | -$3,902 | — | Rejected — Sharpe improved slightly but not worth P&L cost |
| VIX rise deceleration filter | -$13k | — | Rejected — inconsistent year-to-year, likely overfitting |
| Skip VIX <12 entirely | -$1,024 | unchanged | Rejected — cosmetic Sharpe gain, no DD improvement |
| Skip VIX 13.0–13.5 entirely | -$244 | unchanged | Rejected — Sharpe gain cosmetic, DD unchanged, 60 days skipped for no benefit |
| Tighter dynamic SL for VIX <13 (-$400/-$500) | marginal | worse DD | Rejected — tighter SL fires more, days that survive at -$800 recover enough |
| Pressure filter VIX 15-20 only (45pt) | -$134k | unchanged | Rejected — eliminated 2 worst days but cost 1,297 trades |
| Entry cap VIX 15-20 (cap=5/7) | -$144k to -$161k | unchanged | Rejected — too blunt, penalises ~1,400 winning trades/year |
| Daily circuit breaker (2 intraday SLs) | not viable | — | Rejected — batch-SL architecture means all positions close simultaneously |

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

## Finding 1: Oct 9, 2023 — Single Largest Loss Day (-$6,118) defines Max DD

**VIX:** 17.7 (sweet spot zone, dynamic SL NOT active)
**Direction:** CALL spreads (VIX rose → short calls)
**Calendar event:** NONE — regular Monday
**Price action:** V-shaped rally. Opened 4289, dipped to 4284, then rallied +47 pts to close 4336. All entries in the morning dip, market ripped against them in the afternoon.

| Time | Strike | OTM | P&L | Result |
|------|--------|-----|-----|--------|
| 09:40 | 4335 | 42pt | +$46 | WIN |
| 10:00 | 4345 | 42pt | +$96 | WIN |
| 10:20 | 4340 | 43pt | +$96 | WIN |
| 10:40 | 4330 | 40pt | -$834 | LOSS |
| 11:00 | 4320 | 33pt | -$2,834 | LOSS |
| 11:20 | 4325 | 31pt | -$1,854 | LOSS |
| 11:40 | 4330 | 29pt | -$834 | LOSS |

**Root cause:** Late entries (10:40–11:40) had 29–40pt OTM distance. Market rallied ~50pt from morning lows, running through all short strikes by expiration. No dynamic SL because VIX 17.7 is in the "safe" zone. No per-position SL. DAILY_SL=-$20k never fired.

**Why existing protections don't help:**
- Dynamic SL only activates at VIX <13, 13–13.5, or 25–30 — VIX 17.7 is unprotected
- EOM SL: not an EOM day
- Per-position SL: disabled (and tested/rejected — costs $276k+ P&L)
- Pressure filter at VIX 15-20: tested at 45pt threshold, rejected (-$134k cost)

**Tested mitigations:**
- VIX 15-20 dynamic SL — MARATHON TESTED 2026-03-29:

  | SL Level | P&L | Max DD | Sharpe | WR |
  |----------|-----|--------|--------|----|
  | Baseline | $612,012 | -$6,356 | 14.15 | 92.7% |
  | -$1,500 | $439,018 | -$6,596 | 7.72 | 87.1% |
  | -$3,000 | $485,198 | -$9,956 | 7.65 | 90.3% |
  | -$5,000 | $512,866 | -$10,800 | 7.83 | 91.5% |

  **Rejected** — catastrophic at all levels. VIX 15-20 has 2,871 trades at 97.8% WR; any SL here fires on too many normal winning days.

---

## Finding 2: VIX 13.0–13.5 — Formerly Net-Negative, Now Positive

**Original stats (pre-SL gap re-entry):** 177 trades, 40 trading days, 55.4% WR, -$568 P&L, -$2,296 DD
**Current stats (with SL gap re-entry):** 191 trades, 67.0% WR, +$2,796 P&L, -$3,338 DD
**Dynamic SL:** Already ON at -$800 via `DYNAMIC_SL_VIX_MID = (13.0, 13.5)`

**What changed:** `ENABLE_SL_GAP_REENTRY = True` (60 min) converted this zone from net-negative to net-positive. In this low-IV zone, the dynamic SL fires frequently; the re-entry feature allows a second attempt 60 min later, capturing recoveries. Trade count increased from 177 to 191 (+14 re-entry trades), wins increased more than losses, flipping the zone to +$2,796.

**Loss day breakdown (19 losing date-direction combos, original analysis):**
- 14 of 19 are PUT spreads (-$7,832 cumulative)
- 5 of 19 are CALL spreads (-$3,748 cumulative)
- Only 3 overlap calendar events (2023-06-16 TW, 2023-07-26 FOMC, 2024-03-20 FOMC)
- 84% of losses have NO calendar event — the zone itself is the problem

**Worst days:** 2024-07-16 CALL -$920 (5 trades, 0% WR), 2024-01-26 PUT -$788 (7 trades, 14% WR), 2024-06-04 CALL -$740 (10 trades, 40% WR)

**Root cause:** VIX 13.0–13.5 = extremely low IV. Credits are minimal ($0.55–0.70), leaving almost no buffer. The -$800 SL fires frequently. SL gap re-entry mitigates this but doesn't eliminate it — the zone is still the weakest by WR (67% vs 93.4% overall).

**Options tested:**
- Skip VIX 13.0–13.5 entirely — MARATHON TESTED: P&L $611,768 (-$244), DD -$6,356 (unchanged), Sharpe 15.32 (+1.17), WR 94.2%. **Rejected** — P&L cost negligible but DD unchanged. Sharpe gain is cosmetic.

---

## Finding 3: June Weakness — Regime-Driven, NOT Calendar-Driven

**June avg P&L:** $7,289/yr vs $12,210/yr for other months (40% underperformance)

| June | P&L | Trades | WR% | Avg VIX | Key Driver |
|------|-----|--------|-----|---------|------------|
| 2022 | +$10,588 | 173 | 82.7% | 28.1 | High VIX = strong |
| 2023 | +$3,080 | 65 | 96.9% | 14.0 | 1 catastrophic day (6/15 -$4,292) wiped 57% |
| 2024 | -$3,152 | 113 | 57.5% | 12.6 | 10 loss days, VIX compressed |
| 2025 | +$18,638 | 168 | 100% | 18.3 | Elevated VIX = perfect |

**Root cause:** VIX regime, not the calendar. Correlation ~0.88 between avg June VIX and monthly P&L.

**Conclusion:** June weakness = low-VIX regime problem. No June-specific filter needed — the VIX-level protections cover the root cause.

---

## Finding 4: Calendar SL at -$300 across all categories too expensive

All 5 categories tested individually at -$100, -$200, -$300, -$400, -$500, None. **Every category is best with SL OFF.**

| Category | Days | Worst Level Cost | Best Level (excl off) |
|----------|------|-----------------|----------------------|
| CPI | 47 | -$30.8k @ -$500 | -$24.3k @ -$200 |
| PCE | 42 | -$12.3k @ -$100 | -$4.2k @ -$400 |
| Pre-TW | 15 | -$1.1k @ -$200 | +$730 @ -$500 |
| Post-Hol | 12 | +$1.0k @ -$200 | +$3.1k @ -$500 |

---

## Finding 5: Strike distance 70+ shows WR degradation

| Distance | WR% | Avg P&L | Notes |
|----------|-----|---------|-------|
| 35–65 | 93.9–95.4% | $88–98 | Strong |
| 70–75 | 85.9% | $73.54 | Drop-off |
| 80–85 | 83.1% | $62.50 | Worst |
| 100+ | 87.2% | $84.23 | Recovers slightly |

---

## Finding 6: VIX <13 disproportionate DD

$7,012 P&L but -$6,382 DD = 0.91 Calmar for this zone alone. Carries 93% of portfolio max DD for only 1.2% of total P&L.

- Tighter dynamic SL for VIX <13 — MARATHON TESTED:

  | SL Level | P&L | Max DD | Sharpe | WR |
  |----------|-----|--------|--------|----|
  | -$800 (baseline) | $612,012 | -$6,356 | 14.15 | 92.7% |
  | -$500 | $612,818 | -$7,952 | 14.47 | 93.2% |
  | -$400 | $614,146 | -$7,028 | 14.60 | 93.1% |

  **Rejected** — both tighter levels WORSEN max DD. The tighter SL fires more frequently but days that survive at -$800 recover enough.

- Skip VIX <12 entirely — MARATHON TESTED: P&L $610,988 (-$1,024), DD -$6,356 (unchanged). **Rejected**.

---

## Low-VIX Trending Regime — Structural Gap

**Finding:** 87% of all loss days when VIX <13 are complete 0-win wipeouts (13 of 15 days). 23 zero-win days cluster in VIX <15, costing $-16,626 over 4 years.

**Deep-dive: June–July 2024 cluster** — 11 complete 0-win days, $-9,638 total in 6 weeks. Root cause: SPX VIX stuck at 11–13 while making directional moves of 0.5–1%+/day (~27–54 pts). MIN_OTM_DISTANCE=30 was barely outside 1 standard deviation.

**Two failure modes identified:**

1. **Full-day trending (6–10 entries, all losing)** — Jun 6, 7, 21, 26, 27; Jul 15, 16: market trended all day in one direction against the VIX signal.

2. **Fast early move, entries choked off (1–3 entries)** — Jun 10, 11, 13, 24, 28; Jul 1: market blew through short strikes in first 40 minutes, then entries stopped because 0DTE options become too cheap to meet MIN_NET_CREDIT=0.55.

**Why nothing protects against this:**
- Dynamic SL doesn't activate (VIX 11–14 is outside trigger ranges except VIX <13.0 / 13.0–13.5)
- Raising MIN_OTM_DISTANCE when VIX <15 would push strikes further out but at low VIX the options at +40pts OTM may not meet MIN_NET_CREDIT=0.55

**Bottom line:** $16k cost over 4 years is manageable (2.6% of total P&L). Low priority vs Kelly sizing unlock.

---

## Intraday Trend Reversal Detection (VIX 15–20 loss day problem)

**Problem:** 10 of 15 worst loss days are in VIX 15–20. Pattern: market drifts *with* positions during the 9:35–12:45 entry window, then reverses hard after close and hits multiple short strikes at expiry.

**Ideas explored:**

1. **Strike distance decay rate** — track how fast existing positions' average OTM distance is shrinking bar-by-bar. Most targeted approach but needs sweep for threshold. **Deferred until Kelly unlock.**

2. **SPX momentum / rate-of-change filter** — measure direction/magnitude over last 30–60 min. Risk of same failure mode as other intraday filters.

3. **Accumulated position heat** — stop entries if sum of unrealised P&L crosses negative threshold. Similar to Bayesian gate but uses larger threshold to get above B/A noise floor.

4. **EMA crossover on SPX intraday bars** — fast/slow EMA on 1-min or 5-min intraday bars. Key risk: same overfitting concern as global EMA direction signal.

**Caution:** All four ideas share the same failure mode as prior rejected filters — may fire on *winning* days where the market temporarily moves against positions before recovering.

---

## Consecutive Loss Circuit Breaker — NOT VIABLE

A consecutive-*closed*-loss counter cannot fire intraday because all positions settle as a batch:

| Date | VIX | W/L | Close mechanism | Why circuit breaker fails |
|------|----:|-----|-----------------|--------------------------|
| 2023-10-09 | 17.7 | 3W/4L | EXPIRATION (15:59) | All 7 settle at EOD together |
| 2026-03-10 | 25.5 | 0W/10L | STOP_LOSS (14:15) | All 10 entries already open before SL fires |
| 2024-06-27 | 12.2 | 0W/5L | STOP_LOSS (11:29) | All 5 entries already open before SL fires |
| 2025-10-01 | 16.2 | 0W/2L | EXPIRATION (15:59) | Both settle at EOD together |

The correct version is to track **unrealized losses on open positions** intraday — essentially the strike distance decay signal.

---

## Per-Position Fixed SL Sweep

| SL | P&L | P&L Delta | Max DD | Calmar | Sharpe |
|---|---|---|---|---|---|
| None (baseline) | $596,918 | — | -$6,894 | 86.6 | 14.05 |
| -$200 | $160,654 | -$436k | -$8,928 | 18.0 | 3.05 |
| -$300 | $211,548 | -$385k | -$10,714 | 19.8 | 3.56 |
| -$400 | $248,848 | -$348k | -$11,560 | 21.5 | 3.86 |
| -$500 | $293,058 | -$304k | -$9,880 | 29.7 | 4.53 |
| -$600 | $320,220 | -$277k | -$13,114 | 24.4 | 4.70 |

Same failure mode as per-position trailing stop — 93%+ expiry WR means positions that go temporarily negative mostly recover by EOD.

---

## Black Swan Protection — IMPLEMENTED 2026-03-28

### Layer 1: DAILY_SL = -20,000

If total daily P&L hits -$20,000, close all positions and halt entries for the day.

- **Zero P&L cost**: never fired once in 4yr backtest. Worst actual day was -$6,118.
- **Tested values**: -$5k → $365,580 (-$241k cost), -$10k → $497,722 (-$109k), -$15k → $578,172 (-$29k), -$20k → $606,832 ($0 cost).

### Layer 2: VIX_MAX_FILTER = 35.0

Skip entire trading day if VIX close > 35. Only 8 days triggered in 2022–2026 backtest.

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

### Theoretical Max Loss

```
~10 positions open by 12:45, all go max loss simultaneously
= 10 x (20pt width - $0.55 credit) x 100 x 2 contracts
= $38,900 theoretical maximum
```
With DAILY_SL = -20,000: loss capped at ~$20,000 if crash is gradual. If instantaneous gap, all positions hit max loss before stop fires — account takes ~$38,900 hit and survives at ~$1,100.

---

## Statistical Models — Full Results

### [1] Loss Classification Model — NEGATIVE (2026-03-28)

VIX alone explains 74% of signal (AUC 0.742); full model adds only +0.026 AUC. 53% of losses live in VIX 25–30 (already handled by dynamic SL). No actionable filter found. Safe-zone losses (VIX 15–25) cluster on flat EMA spread + CALL direction but too sparse (51 trades over 4 years).

### [2] Kelly-Inspired Dynamic Position Sizing — POSITIVE, LOCKED (2026-03-28)

Full Kelly works (+92% P&L, $607k → $1,168k) but requires ~$77,720 peak BP. Current account has ~$40,000 (97% used at qty=2). Zone map: VIX <13→1, 13–15→3, 15–20→4, 20–25→4, 25–30→1, >30→4. Enable when BP reaches ~$80,000+.

### [3] GARCH(1,1) on VIX — NEGATIVE (2026-03-28)

GARCH fits VIX well but adds no predictive value. AUC: VIX alone = 0.554, VIX + GARCH = 0.578 (+0.025). GARCH directional forecast is useless: P(VIX falls) ranges only 0.4967–0.4992 across all 947 days. GARCH captures vol-of-vol; strategy losses are driven by absolute VIX regime, not vol-of-vol.

### [4] Intraday Bayesian Updating — NEGATIVE (2026-03-28)

Entry gate on `current_day_pnl` costs P&L at every threshold.

| Threshold | Trades | P&L | Max DD |
|---|---|---|---|
| Baseline (no gate) | 7,014 | $607,034 | -$9,922 |
| $0 | 1,314 | $95,694 | -$3,992 |
| -$150 | 5,925 | $493,880 | -$9,922 |
| -$300 | 6,313 | $527,578 | -$9,922 |
| -$400 | 6,469 | $543,234 | -$9,922 |

**Root cause:** Each position immediately shows ~-$29 paper loss from bid-ask spread. With 5–6 simultaneously open positions, the noise floor is -$150 to -$200 even on perfect win days.

### [5] VIX Term Structure Slope (VIX9D/VIX Ratio) — NEGATIVE (2026-03-28)

VIX9D/VIX ratio is mostly a proxy for VIX level (correlation = 0.455). The only positive filter (VIX <13 + backwardation) yields +$1,268 on 67 trades over 4 years (~17/yr). 64% of those trades already stopped by dynamic SL.

**Paper:** Yoon (2022), *Journal of Futures Markets*

### [6] Intraday Jump Detection — NEGATIVE (2026-03-29)

Bipower variation vs. realized variance ratio (Barndorff-Nielsen & Shephard 2004). Jump days outperform no-jump days ($654 vs $623 avg P&L). Skipping at p<0.05 costs -$109k.

**Paper:** Bozovic (2025), SSRN #5223127

### [7] Variance Risk Premium (VRP) — NEGATIVE (2026-03-29)

VRP quintile gradient exists but too weak. Half-sizing bottom 20% costs -$61k P&L to reduce DD by only $776. Negative VRP days still avg $781/day with 86.7% WR.

**Paper:** Bollerslev, Tauchen & Zhou (2009), *Review of Financial Studies*; Papagelis (2025), *Journal of Futures Markets*

### [8] HAR-RV — NEGATIVE (2026-03-29)

Gradient is backwards. High HAR-RV forecast days are the best trading days (Q4: $874, 97.8% WR). Half-sizing on high-vol forecast days costs -$28k to -$44k.

**Paper:** Corsi (2009), *Journal of Financial Econometrics* 7(2)

### [9] Markov-Switching GARCH (MS-GARCH) — NEGATIVE (2026-03-29)

GMM probabilities max out at 0.39; no day exceeds 0.5 threshold. Already captured more cleanly by existing VIX zone map.

**Paper:** Hamilton (1989), *Econometrica* 57(2); MSGARCH R package — *Journal of Statistical Software* 91(4), 2019

### [10] CVaR / Expected Shortfall Optimization — NEGATIVE (2026-03-29)

Both E[P&L] and CVaR scale linearly with qty, so return/risk ratio is identical at any fixed qty. CVaR minimization simply recommends qty=1 everywhere. Kelly actually improves Sharpe (14.75 vs 14.16) by concentrating size in best VIX zones.

**Paper:** Rockafellar & Uryasev (2000), *Journal of Risk*

### [11] IV Skew / Put-Call Risk Reversal — NEGATIVE AS FILTER (2026-03-29)

Monotonic gradient ($459→$733 Q1→Q5) confirms put skew = favorable. But skipping CALL days on negative-skew costs -$12k. Signal suggests *increasing* size on high-skew days — only useful when Kelly sizing unlocked.

**Paper:** Rehman & Vilkov (2012); ScienceDirect (2020)

### [12] Realized GARCH — NEGATIVE (2026-03-29)

Same pattern as HAR-RV. Higher vol forecast = better trading days. Half-sizing top 20% costs -$44k.

**Paper:** Hansen, Huang & Shek (2012), *Journal of Applied Econometrics* 27(6)

### [14] Variance Risk Premium Revisit (JF Formulation) — SKIP

Already fully tested in [7] — negative. Do not retest.

### [16] Kalman Filter — SKIP

Every direction/trend signal tested returned negative P&L. Same failure mode as all prior direction alternatives.

### [17] Parkinson Volatility Estimator — SKIP

Already covered by VRP [7], HAR-RV [8], and Realized GARCH [12]. High-volatility days are consistently *better* for this strategy.

### [23] Intraday Price Action Signals — SKIP (Category Rejected)

| Signal | Concept | Reason to Skip |
|---|---|---|
| Minimum price swing | Only enter if SPX has moved >= X pts from open | Same failure mode as Bayesian entry gate |
| Max retracement | Skip if market pulled back > X% from today's high | Pressure filter reframed — already cost -$50k |
| Minimum volume spike | Require volume confirmation before entry | SPX index has no volume; ES/SPY proxy is noisy |
| Momentum score | Composite of RSI, MACD, momentum oscillator | Combination of individually-failed signals |
| Max gap % of swing | Overnight gap as % of total daily range | Variation on gap direction signal — already negative |

**Root cause:** The strategy's structural edge (theta decay + MIN_OTM_DISTANCE=30) is immune to intraday price-behavior filters.

---

## Statistical Validation — COMPLETED (2026-03-29)

### t-test
t=27.50, p=6.5e-123. Edge is not random.

### Bootstrap confidence intervals (10,000 resamples)
| Metric | Observed | 95% CI |
|---|---|---|
| Sharpe | 14.16 | [11.79 – 17.12] |
| Win Rate | 87.1% | [84.8% – 89.2%] |
| Max Drawdown | -$6,894 | [-$9,488 – -$3,370] |

### Walk-forward / year-by-year
| Year | P&L | Sharpe | WR | Max DD |
|---|---|---|---|---|
| 2022 | $140,470 | 13.92 | 78.0% | -$3,936 |
| 2023 | $123,666 | 11.83 | 91.6% | -$6,118 |
| 2024 | $123,828 | 13.51 | 84.6% | -$6,894 |
| 2025 | $175,400 | 18.15 | 93.6% | -$3,370 |
| 2026 YTD | $33,424 | 13.47 | 87.3% | -$2,940 |

Every year profitable. Sharpe never below 11.83.

### Monte Carlo (10,000 paths, 957 days each with replacement)
- P(lose money over full period): **0.00%**
- P(Max DD > $10,000): **1.7%**
- P(Max DD > $15,000): **0.0%**
- P(Max DD > $20,000): **0.0%**

### Direction Signal Permutation Test

| Metric | Value |
|---|---|
| Observed total P&L | $596,788 |
| Estimated P&L if signal FLIPPED | $577,598 |
| Signal advantage vs contrarian | +$19,190 |
| PUT days avg P&L/day | $674 (561 days, median $974) |
| CALL days avg P&L/day | $562 (389 days, median $706) |
| t-test (PUT vs CALL day quality) | t=2.41, p=0.016 |
| Permutation p-value | p=0.007 |

**Conclusion:** The strategy is primarily a **theta decay engine**. The VIX direction signal adds ~$19k of incremental value (p=0.016), but the dominant driver is theta. Flipping the signal only costs ~$19k over 4 years.

---

## Additional Signal Candidates — Status

| Signal | Data Available | Actionability | Verdict |
|---|---|---|---|
| P/C OI ratio | Yes | High | Tested — redundant with VIX_MAX_FILTER |
| VIX intraday spike | Yes | Medium | Tested — negative |
| Prior-day VIX % chg | Yes | High | Tested — negative |
| VVIX | No (free CBOE download) | High | Not yet tested |
| CBOE SKEW | No (free CBOE download) | Medium | Not yet tested |
| News sentiment | No (API needed) | Low | Not actionable without paid data source |

### P/C OI Ratio — TESTED NEGATIVE (2026-03-28)

The two most extreme P/C days (April 4, 2025: ratio=4.11; August 5, 2024: ratio=3.68) are already skipped by VIX_MAX_FILTER = 35. All moderate-high P/C days (ratio 2.0–3.0, VIX < 35) are profitable trading days — quarter-end mechanical hedging.

### Prior-Day VIX % Change — TESTED NEGATIVE (2026-03-28)

Days following a large prior-day VIX spike are some of the *best* trading days.

| Prior-Day VIX Chg | Days | Win Rate | Avg P&L |
|---|---|---|---|
| 5–10% | 100 | 90.0% | $728 |
| 10–15% | 49 | 83.7% | $683 |
| 15–20% | 24 | 75.0% | $662 |
| **> 20%** | **13** | **92.3%** | **$927** |

### VIX Intraday Spike Circuit Breaker — TESTED NEGATIVE (2026-03-28)

Days with the largest intraday VIX spikes are above-average trading days. Dec 18, 2024 VIX spike (57%) first crossed 20% at 15:04 PM — after the 12:45 entry window closed.

| Intraday Spike Threshold | Days Affected | Avg P&L | Outcome |
|---|---|---|---|
| >= 20% | 19 days | $826 | Would cost P&L |
| >= 30% | 6 days | $821 | Would cost P&L |
| >= 40% | 3 days | $1,016 | Would cost P&L |
| >= 50% | 1 day | $1,080 | Would cost P&L |
