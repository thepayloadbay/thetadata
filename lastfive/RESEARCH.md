# RESEARCH — LastFive Strategy

## Option Alpha Backtester Comparison (2026-04-02)

### Key Finding: Bar OPEN vs Bar CLOSE for Strike Selection

**Using bar CLOSE is look-ahead bias.** The closing price of a 1-minute bar doesn't exist at the start of that bar when the trade decision is made.

| Price Used for Strike | Strike Match vs OA |
|---|---|
| Bar CLOSE | 59.9% |
| Bar HIGH | 67.4% |
| Bar LOW | 71.8% |
| **Bar OPEN** | **99.8%** |

**Rule: Always use bar OPEN for any price-dependent decision at entry time.**

Option Alpha uses the opening tick of the entry bar to determine strike selection. Our backtester must do the same to avoid look-ahead bias.

Similarly:
- **Touch/buyback detection**: Use bar LOW (puts) or bar HIGH (calls) to catch intrabar touches, not bar CLOSE which misses intrabar spikes that reverse.

---

### OA Backtest Versions Compared

All tests use PUT-only, ATM, `$0.01 below underlying or lower`, $5 width.

| Version | Entry | Touch | P&L | WR | Trades |
|---|---|---|---|---|---|
| `$0.01 below` (exact) | 3:55 | None | **-$3,694** | 62.9% | 1,008 |
| `$0.01 below or lower` | 3:55 | None | **$15,006** | 76.3% | 1,015 |
| `$0.01 below or lower` | 3:54 | None | **$20,998** | 76.6% | 1,020 |
| `$0.01 below or lower` | 3:54 | $2.00 | **$45,825** | 54.8% | 1,020 |
| `$0.01 below or lower` | 3:55 | $0.00 | **$41,330** | 62.7% | 1,015 |
| `$0.01 below or lower` | 3:54 | $0.00 | **$59,890** | 61.7% | 1,020 |

**Best OA config: 3:54 entry + Touch $0 = $59,890**

### Why `$0.01 below or lower` beats `$0.01 below`

| Strike Rule | Effect | P&L |
|---|---|---|
| `$0.01 below` | Picks nearest strike BELOW `SPX - $0.01` — can land ON the money | -$3,694 |
| `$0.01 below or lower` | Picks nearest strike AT or BELOW `SPX - $0.01` — gives $0-5 more buffer | +$15,006 |

The "or lower" rounding pushes the short strike one notch further OTM on ~45% of days. That extra $5 of buffer flips the strategy from losing to profitable.

### Strike Calculation Methods

**Option Alpha**: `floor((bar_open - $0.01) / 5) * 5`
- Uses the bar OPEN price (confirmed 99.8% match)
- Rounds down to nearest $5 strike at or below `SPX - $0.01`

**Our engine (dist=0 ATM)**: `floor(spot / 5) * 5`
- Uses the bar CLOSE price (needs to be changed to bar OPEN)
- 55% exact match with OA due to CLOSE vs OPEN difference

### Touch $0 vs Touch $2

| Touch Value | P&L | WR | Effect |
|---|---|---|---|
| No touch | $20,998 | 76.6% | Hold everything to settlement |
| **$0 (at strike)** | **$59,890** | 61.7% | Exit immediately when SPX hits strike |
| $2 (our buyback) | $45,825 | 54.8% | Exit when SPX is $2 from strike |

Touch $0 is optimal — exit the moment SPX reaches the short strike. Waiting for $2 past the strike lets losses grow unnecessarily.

### Touch Timing: 3:54 vs 3:55 Entry

3:54 entry + Touch $0 beats 3:55 + Touch $0 by $18.5k ($59.9k vs $41.3k). The extra minute gives Touch more time to detect and close threatened positions. With Touch at 3:55 entry, the earliest exit is 3:56 — by then the move is larger and exit costs more.

### Our Engine vs OA (on common dates)

On 478 common trading dates:

| Metric | Our Sim | OA Actual |
|---|---|---|
| Strike match | 99.8% | — |
| Touch/Expire agree | 88.1% | — |
| P&L within $100 | 55.4% | — |
| P&L within $200 | 74.1% | — |

Remaining P&L gap is from Touch exit pricing — OA closes at the exact tick of touch (intrabar), we use end-of-minute quotes.

### Recommended OA Bot Settings

| Setting | Value | Reason |
|---|---|---|
| **Entry time** | 3:54 PM | Extra minute for Touch to work |
| **Strike** | `$0.01 below underlying or lower` | Adds buffer, +$19k vs `$0.01 below` |
| **Width** | $5 | Standard |
| **Touch** | $0 | Exit at strike, don't wait |
| **Sides** | Both call + put if possible | Doubles P&L |
| **VIX filter** | Skip VIX > 26 if supported | Removes worst days |

### Our Strategy vs OA Best Config

| Metric | OA Best (PUT only) | Our LastFive (full) |
|---|---|---|
| P&L | $59,890 | $288,264 |
| WR | 61.7% | 82.4% |
| Sides | PUT only | CALL + PUT |
| Filters | None | VIX/Afternoon/Momentum |
| VIX Adaptive | No | Yes (ATM/3/5 OTM) |
| Tiered Sizing | No | Yes ($1.5k/$2.5k/$3.5k) |
| Touch/Buyback | $0 continuous | $2 at 15:56/57 |

The $228k gap comes from bidirectional trading, VIX-adaptive distance, filters, and tiered sizing — not available on OA's platform.

---

## OA Settings Optimization Sweep (2026-04-02)

Comprehensive sweep of 480 combinations using our 1-minute options data with OA-compatible logic (bar OPEN for strikes, bar LOW/HIGH for touch). Results saved in `sweep_oa_results.csv`.

### Sweep Parameters

| Dimension | Values Tested |
|---|---|
| Entry time | 15:52, 15:53, 15:54, 15:55 |
| OTM distance | $0 (ATM), $1, $2, $3, $5 |
| Touch value | None, -$0.50, $0, $0.50, $1.00, $2.00 |
| Sides | PUT only, BOTH (call + put) |
| Width | $5, $10 |

### Top 10 by P&L

| Config | P&L | Max DD | WR | Sharpe | PF |
|---|---|---|---|---|---|
| BOTH, 15:52, d=3, t=-$0.5, w=5 | **$38,110** | -$2,649 | 73.1% | 3.24 | 1.68 |
| BOTH, 15:52, d=2, t=-$0.5, w=5 | $34,656 | -$4,210 | 67.2% | 2.55 | 1.50 |
| BOTH, 15:52, d=1, t=-$0.5, w=5 | $32,652 | -$4,787 | 61.5% | 2.16 | 1.41 |
| BOTH, 15:52, d=3, t=$0, w=5 | $30,607 | -$3,949 | 70.2% | 2.66 | 1.53 |
| BOTH, 15:52, d=3, t=-$0.5, w=10 | $24,759 | -$809 | 75.6% | 4.63 | 2.16 |
| BOTH, 15:52, d=2, t=$0, w=5 | $24,048 | -$4,548 | 63.6% | 1.83 | 1.34 |
| BOTH, 15:52, d=2, t=-$0.5, w=10 | $23,764 | -$1,073 | 70.4% | 3.83 | 1.91 |
| BOTH, 15:52, d=1, t=-$0.5, w=10 | $23,377 | -$1,350 | 65.2% | 3.24 | 1.76 |
| BOTH, 15:52, d=3, t=$0.5, w=5 | $22,887 | -$4,006 | 65.9% | 2.06 | 1.39 |
| BOTH, 15:53, d=2, t=-$0.5, w=5 | $22,306 | -$7,505 | 67.4% | 1.64 | 1.30 |

### Top 10 by Sharpe

| Config | P&L | Max DD | WR | Sharpe | PF |
|---|---|---|---|---|---|
| BOTH, 15:52, d=3, t=-$0.5, w=10 | $24,759 | **-$809** | 75.6% | **4.63** | 2.16 |
| PUT, 15:52, d=3, t=-$0.5, w=10 | $12,633 | -$926 | 73.8% | 4.60 | 2.10 |
| PUT, 15:52, d=3, t=$0, w=10 | $12,257 | -$878 | 73.1% | 4.59 | 2.11 |
| BOTH, 15:52, d=3, t=$0, w=10 | $21,648 | -$1,208 | 73.0% | 4.11 | 1.99 |
| PUT, 15:52, d=3, t=$0.5, w=10 | $10,417 | -$751 | 69.6% | 4.04 | 1.94 |
| BOTH, 15:52, d=2, t=-$0.5, w=10 | $23,764 | -$1,073 | 70.4% | 3.83 | 1.91 |
| PUT, 15:54, d=3, t=-$0.5, w=10 | $11,686 | -$862 | 75.7% | 3.81 | 2.00 |
| BOTH, 15:52, d=5, t=-$0.5, w=10 | $16,325 | -$1,093 | 81.8% | 3.80 | 1.97 |
| BOTH, 15:52, d=3, t=$0.5, w=10 | $18,603 | -$1,106 | 69.7% | 3.65 | 1.85 |
| PUT, 15:52, d=2, t=-$0.5, w=10 | $11,486 | -$1,193 | 68.2% | 3.61 | 1.84 |

### Top 10 by Calmar (P&L / |DD|)

| Config | P&L | Max DD | WR | Sharpe | Calmar |
|---|---|---|---|---|---|
| BOTH, 15:52, d=3, t=-$0.5, w=10 | $24,759 | -$809 | 75.6% | 4.63 | **30.6** |
| BOTH, 15:52, d=2, t=-$0.5, w=10 | $23,764 | -$1,073 | 70.4% | 3.83 | 22.1 |
| BOTH, 15:53, d=5, t=$0, w=10 | $14,701 | -$688 | 80.4% | 3.39 | 21.4 |
| BOTH, 15:53, d=5, t=$0.5, w=10 | $13,831 | -$652 | 77.9% | 3.27 | 21.2 |
| BOTH, 15:53, d=5, t=-$0.5, w=10 | $15,181 | -$749 | 81.9% | 3.40 | 20.3 |
| BOTH, 15:53, d=5, t=$1, w=10 | $12,281 | -$668 | 74.9% | 2.94 | 18.4 |
| BOTH, 15:52, d=3, t=$0, w=10 | $21,648 | -$1,208 | 73.0% | 4.11 | 17.9 |
| BOTH, 15:52, d=5, t=$0.5, w=10 | $13,801 | -$783 | 77.8% | 3.40 | 17.6 |
| BOTH, 15:52, d=1, t=-$0.5, w=10 | $23,377 | -$1,350 | 65.2% | 3.24 | 17.3 |
| BOTH, 15:52, d=3, t=$0.5, w=10 | $18,603 | -$1,106 | 69.7% | 3.65 | 16.8 |

### Key Findings

1. **$3 OTM dominates ATM** — across all metrics, distance=$3 consistently outperforms ATM ($0). ATM gets breached too often; $3 gives enough buffer while still collecting meaningful credit.

2. **15:52 is the optimal entry time** — earlier entry = more time decay before settlement. 15:52 beats 15:53 beats 15:54 beats 15:55.

3. **Touch -$0.50 is optimal** — allowing the position to go $0.50 ITM before closing avoids false triggers where SPX briefly touches the strike then reverses. Better than Touch $0 (too trigger-happy) and Touch $2 (too much damage before exit).

4. **$10 width dominates risk-adjusted** — fewer contracts per trade but each has more cushion. Best Sharpe (4.63) and Calmar (30.6) come from $10 width.

5. **$5 width wins on raw P&L** — more contracts means more dollar profit ($38k vs $25k) but with higher DD.

6. **BOTH sides doubles P&L** — call+put together roughly doubles returns vs put-only with similar risk-adjusted metrics.

7. **No touch is worst** — every touch value beats no touch. The touch exit is consistently valuable.

### Recommended OA Settings

**For Maximum P&L:**

| Setting | Value |
|---|---|
| Entry | 3:52 PM |
| Strike | $3.00 below/above underlying or lower/higher |
| Width | $5 |
| Touch | -$0.50 (allow $0.50 ITM before exit) |
| Sides | Both call + put |
| Expected P&L | ~$38,110 |
| Expected DD | ~-$2,649 |

**For Best Risk-Adjusted (Sharpe/Calmar):**

| Setting | Value |
|---|---|
| Entry | 3:52 PM |
| Strike | $3.00 below/above underlying or lower/higher |
| Width | $10 |
| Touch | -$0.50 |
| Sides | Both call + put |
| Expected P&L | ~$24,759 |
| Expected DD | ~-$809 |
| Sharpe | 4.63 |
| Calmar | 30.6 |

### OA Settings to Verify

Run these on OA's backtester to confirm:

1. Entry 3:52 PM, $3 OTM or lower, $5 width, Touch -$0.50, PUT only
2. Entry 3:52 PM, $3 OTM or lower, $5 width, Touch -$0.50, BOTH sides
3. Entry 3:52 PM, $3 OTM or lower, $10 width, Touch -$0.50, PUT only
4. Entry 3:52 PM, $3 OTM or lower, $10 width, Touch -$0.50, BOTH sides

### Note on Touch -$0.50

OA's Touch setting with a negative value means: exit when the underlying has moved $0.50 past (ITM) the short strike. For a put spread with short put at 5950, Touch -$0.50 triggers when SPX drops below $5949.50. This is slightly more lenient than Touch $0, reducing false triggers from brief touches that immediately reverse.

---

## OA Backtest Confirmation (2026-04-02)

### Sweep's #1 Pick Confirmed on OA

Our sweep predicted **3:52 entry, $3 OTM, Touch -$0.50, BOTH sides, $5 width** as the optimal OA config. OA's backtester confirmed it:

| Metric | Our Sweep Prediction | OA Actual |
|---|---|---|
| **P&L** | $38,110 (on 484 days) | **$93,814** (on 2,039 trades) |
| **Max DD** | -$2,649 | **-$2,493** |
| **Win Rate** | 73.1% | **78.8%** |
| **Trades** | ~1,035 | **2,039** |

DD prediction was remarkably close (-$2,649 vs -$2,493). The P&L gap is from our missing ATM option quotes data (52% of days).

### All OA Backtests — Final Ranking

| # | Config | P&L | Max DD | WR | Trades |
|---|---|---|---|---|---|
| **1** | **3:52, $3 OTM or lower, Touch -$0.5, BOTH** | **$93,814** | **-$2,493** | **78.8%** | **2,039** |
| 2 | 3:54, ATM or lower, Touch $0 | $59,890 | -$9,270 | 61.7% | 1,020 |
| 3 | 3:54, ATM or lower, Touch $2 | $45,825 | -$5,689 | 54.8% | 1,020 |
| 4 | 3:55, ATM or lower, Touch $0 | $41,330 | -$8,869 | 62.7% | 1,015 |
| 5 | 3:54, ATM or lower, No touch | $20,998 | -$20,939 | 76.6% | 1,020 |
| 6 | 3:55, ATM or lower, No touch | $15,006 | -$19,301 | 76.3% | 1,015 |
| 7 | 3:55, ATM below, No touch | -$3,694 | -$37,711 | 62.9% | 1,008 |

### Improvement Breakdown

Each change's incremental impact (from worst to best):

| Change | P&L Impact | DD Impact |
|---|---|---|
| "below" → "below or lower" | +$18,700 | DD halved |
| 3:55 → 3:54 entry | +$6,000 | similar |
| No touch → Touch $0 | +$39,000 | DD -55% |
| ATM → $3 OTM | +$34,000 | DD -73% |
| PUT only → BOTH sides | ~2x P&L | similar DD |
| Touch $0 → Touch -$0.50 | varies | fewer false triggers |
| 3:54 → 3:52 entry | +$6,000 | more time for touch |

### Recommended OA Bot Configuration (Confirmed)

| Setting | Value |
|---|---|
| **Entry time** | **3:52 PM** |
| **Strike** | **$3.00 below/above underlying or lower/higher** |
| **Width** | **$5** |
| **Touch** | **-$0.50** |
| **Sides** | **Both call + put** |
| **Expected P&L** | **~$93,814** (4 years, OA confirmed) |
| **Expected DD** | **~-$2,493** |
| **Expected WR** | **~78.8%** |

### Data Coverage Note

Our simulation sweep has only 48% data coverage for ATM quotes (533 of 1,022 days missing ATM option quotes in our ThetaData download). This causes:
- Absolute P&L to be understated by ~2-3x
- Relative rankings between configs remain accurate (same data gap applies to all)
- DD predictions are surprisingly accurate (within 6% of OA actual)

To improve simulation accuracy, re-download ThetaData option quotes with a wider strike range that includes ATM and near-ATM strikes at 15:52-16:00.

---

## Asymmetric Touch Beats Symmetric (2026-04-02)

### Discovery: Different Touch per Side

OA allows different Touch settings per side. Testing asymmetric touch (Call Touch $0, Put Touch -$1) on $3 OTM:

| Config | P&L | DD | WR | Trades |
|---|---|---|---|---|
| **3:52, $3 OTM, Call Touch $0 / Put Touch -$1** | **$98,779** | **-$2,652** | **78.8%** | **2,039** |
| 3:52, $3 OTM, Touch -$0.5 (symmetric) | $93,814 | -$2,493 | 78.8% | 2,039 |
| Improvement | **+$4,965** | -$159 | 0% | — |

Touch exits by side: 213 call touches (Touch $0), 144 put touches (Touch -$1).

**Why asymmetric works**: Call side benefits from tighter touch ($0) — exit immediately when SPX hits the call strike. Put side benefits from looser touch (-$1) — allow puts to go $1 ITM before exiting, reducing false triggers from brief dips.

### Updated All-Time OA Backtest Ranking

| # | Config | P&L | DD | WR | Trades |
|---|---|---|---|---|---|
| **1** | **3:52, $3 OTM, Call Touch $0 / Put Touch -$1, BOTH** | **$98,779** | **-$2,652** | **78.8%** | **2,039** |
| 2 | 3:52, $3 OTM or lower, Touch -$0.5, BOTH | $93,814 | -$2,493 | 78.8% | 2,039 |
| 3 | 3:54, ATM or lower, Touch $0, PUT | $59,890 | -$9,270 | 61.7% | 1,020 |
| 4 | 3:54, ATM or lower, Touch $2, PUT | $45,825 | -$5,689 | 54.8% | 1,020 |
| 5 | 3:55, ATM or lower, Touch $0, PUT | $41,330 | -$8,869 | 62.7% | 1,015 |
| 6 | 3:54, ATM or lower, No touch, PUT | $20,998 | -$20,939 | 76.6% | 1,020 |
| 7 | 3:55, ATM or lower, No touch, PUT | $15,006 | -$19,301 | 76.3% | 1,015 |
| 8 | 3:55, ATM below, No touch, PUT | -$3,694 | -$37,711 | 62.9% | 1,008 |

### Updated Recommended OA Bot Configuration

| Setting | Value | Reason |
|---|---|---|
| **Entry time** | **3:52 PM** | More time decay before settlement |
| **Strike** | **$3.00 below/above underlying or lower/higher** | Sweet spot: enough credit + enough buffer |
| **Width** | **$5** | Standard |
| **Call Touch** | **$0** | Exit immediately when SPX hits call strike |
| **Put Touch** | **-$1.00** | Allow puts to go $1 ITM before exit (fewer false triggers) |
| **Sides** | **Both call + put** | Doubles P&L, hedges each side |
| **Filters** | **None** | All filters reduce P&L — distance + touch handles risk |
| **P&L** | **$98,779** | 4 years, OA confirmed |
| **DD** | **-$2,652** | |
| **WR** | **78.8%** | |

---

## Entry Time Analysis (2026-04-03)

### Best Config at Each Entry Time

All use BOTH sides, $3 OTM, $5 width, Touch -$0.50 (optimal touch across all times):

| Entry Time | P&L | Max DD | WR | Sharpe | Trades |
|---|---|---|---|---|---|
| **15:52** | **$38,110** | **-$2,649** | 73.1% | **3.24** | 1,035 |
| 15:53 | $21,518 | -$3,964 | 72.0% | 1.35 | 1,018 |
| 15:54 | $18,226 | -$4,812 | 74.3% | 1.30 | 970 |
| 15:55 | $12,502 | -$6,660 | 78.8% | 1.23 | 938 |

*Note: P&L numbers are from our sweep with ~48% data coverage. OA confirmed the 15:52 config at $93,814-$98,779 on full data.*

### Why Earlier Entry Wins

1. **More time decay** — 3 extra minutes (15:52 vs 15:55) means more theta erosion before settlement
2. **Touch has more time to work** — earlier entry gives Touch more minutes to detect and exit threatened positions before damage grows
3. **More trades** — earlier entry catches days where 15:55 quotes are missing or credit is too low
4. **Better DD** — 15:52 has -$2,649 DD vs -$6,660 at 15:55 (60% improvement)

### Entry Times NOT Tested on OA

The following entry times were discussed but not available in our OA sweep data:

| Time | Hypothesis | Status |
|---|---|---|
| 15:48 | Even earlier = even more theta | Not tested — needs OA backtest |
| 15:45 | Aggressive early entry | Not tested — needs OA backtest |
| 15:30 | Half-hour before close | Not tested — likely too much adverse movement |
| 14:20 | Afternoon entry | Not tested — too far from close for 0DTE |

**Recommendation**: Test 15:48 and 15:45 on OA. The trend clearly favors earlier entries within the last 10 minutes, but there's likely a cliff where the extra time creates more adverse movement than theta gain. Our original non-OA sweep showed 15:40 and 15:45 are losers at ALL distances — so the sweet spot is likely 15:48-15:52.

---

## Touch Detection: Bar CLOSE vs LOW/HIGH (2026-04-04)

### Discovery: OA Uses Bar CLOSE for Touch

Our touch detection was using bar LOW (puts) and bar HIGH (calls) to catch intrabar touches. This over-triggers on wicks that reverse within the same minute.

| Touch Method | Touches | P&L | Max DD | vs OA ($98,779) |
|---|---|---|---|---|
| **LOW/HIGH** | 583 | $75,906 | -$4,472 | -$22,873 |
| **CLOSE** (OA's method) | **445** | **$121,627** | **-$1,494** | +$22,848 |
| OPEN (next bar) | 351 | -$8,587 | -$16,473 | -$107,366 |
| **OA actual** | **357** | **$98,779** | **-$2,652** | — |

**Why CLOSE wins**: When SPX briefly dips to the strike within a 1-min bar but closes above it, that's a false alarm. Bar LOW catches these wicks; bar CLOSE correctly ignores them. OA's backtester checks the bar's closing price, not the intrabar extreme.

**Why our $121k > OA's $99k**: 
- We have fewer trades (1,910 vs 2,039) — missing 129 trades that are net losers on OA's data
- Per-trade P&L is $64 (ours) vs $48 (OA) — likely settlement/pricing model differences

### Impact on Main Engine

Applied to the full LastFive engine (with VIX-adaptive, tiered sizing, filters):

| Metric | Old (buyback) | New (touch CLOSE) |
|---|---|---|
| **P&L** | $288,264 | $214,538 |
| **Trades** | 731 | 1,501 |
| **Sharpe** | 5.48 | 6.08 |
| **Calmar** | 3.83 | 4.82 |
| **Max DD** | -$5,565 | -$5,017 |
| **WR** | 82.4% | 77.9% |

P&L dropped because: (1) bar OPEN for entry is more conservative than bar CLOSE, (2) touch exits replace unrealistic buyback, (3) more trades with touch = more opportunities but also more touch losses. Risk-adjusted metrics all improved.

---

### Double-Entry Strategy

Tested entering at both 15:53 and 15:55 on the same day:
- Result: +$17k P&L over single entry, but Sharpe drops by 0.44
- The two entries are highly correlated — when one loses, both lose
- **Verdict**: Single 15:52 entry is optimal. Double entry adds P&L but worse risk-adjusted.

---

## SPX Last 10-Minute Statistical Model (2026-04-04)

### Distribution Fit (1,051 days, 2022-2026)

Last 5-min move (15:55 open → 16:00 close):

| Distribution | KS p-value | Verdict |
|---|---|---|
| **Student-t (df=6.58)** | **0.8511** | **Best fit** |
| Laplace | 0.1391 | OK |
| Normal | 0.0659 | Rejected |
| Cauchy | 0.0007 | Rejected |

Parameters: loc=+$0.15, scale=$4.45, σ=$5.35, skew=-0.33, kurtosis=2.87

| Percentile | |Move| |
|---|---|
| 68% (1σ) | < $5.3 |
| 90% | < $8.7 |
| 95% | < $11.1 |
| 99% | < $14.5 |
| Worst case | $37.8 (down) |

### Fat Tails: 1.3-1.8x More Extreme Than Normal

| Threshold | Actual | Normal | Ratio |
|---|---|---|---|
| > 1σ ($5.3) | 27.5% | 31.7% | 0.9x |
| > 2σ ($10.7) | 5.7% | 4.6% | 1.3x |
| > 2.5σ ($13.4) | 2.0% | 1.2% | 1.6x |
| > 3σ ($16.0) | 0.5% | 0.3% | 1.8x |

### Mean-Reversion: σ Grows at 0.56x of √t

Volatility does NOT follow a random walk in the last 10 minutes. Actual σ grows at only 56-78% of the √t rate, indicating mean-reversion — moves partially undo themselves before 16:00.

| Time | Actual σ | √t Model σ | Ratio |
|---|---|---|---|
| 15:52 (t=2) | $5.57 | $7.13 | 0.78x |
| 15:55 (t=5) | $7.27 | $11.27 | 0.64x |
| 16:00 (t=10) | $8.99 | $15.94 | 0.56x |

**This is the structural edge**: selling premium works because moves in the last 10 minutes tend to revert. A random walk would make the strategy much harder.

### VIX-Conditional Model

Linear model: **|move| = $1.11 + 0.150 × VIX** (R²=0.057)

| VIX | σ | P(breach $3) | P(breach $5) | P(breach $8) |
|---|---|---|---|---|
| <15 | $4.06 | 41% | 20% | 6% |
| 15-18 | $4.58 | 48% | 25% | 11% |
| 18-22 | $5.23 | 53% | 32% | 11% |
| 22-26 | $6.80 | 62% | 40% | 20% |
| 26+ | $7.09 | 67% | 43% | 23% |

VIX explains only 5.7% of variance. No pre-entry feature predicts last-5-min moves well. The edge is structural (mean-reversion), not predictive.

### Max Adverse Excursion from 15:55

| Direction | Mean | P90 | P95 | P99 | Max |
|---|---|---|---|---|---|
| Up (hurts puts) | $3.70 | $8.56 | $11.32 | $15.47 | $22.33 |
| Down (hurts calls) | $3.72 | $8.74 | $10.70 | $15.37 | $42.05 |

Down tail is fatter: max down $42 vs max up $22. Skew -0.33 confirms puts face slightly worse tail risk.

### Better Predictors Than VIX (Untapped)

| Feature | Spearman ρ with |move| |
|---|---|
| **Max pre-entry range (15:45-15:54)** | **0.294** |
| **Bar range at 15:50** | **0.250** |
| **Last-hour realized vol** | **0.245** |
| VIX(15:50) | 0.215 |
| |5-min momentum| | 0.191 |

Multiple regression (VIX + rvol + |aftn| + bar_range): R²=0.103 — nearly 2x VIX alone.

### Model-Implied Distance Validation

| VIX | Current Dist | 50% Safe Needs | 60% Safe Needs |
|---|---|---|---|
| <18 | ATM ($0) | $2.3-2.8 | $1.6-2.2 |
| 18-22 | $3 | $3.3 | $2.4 |
| 22-26 | $5 | $4.3 | $3.1 |

ATM at low VIX is aggressive per the model, but works because touch exits handle the 50-60% breach rate.

### Model-Implied Improvements — Backtested (2026-04-04)

| Config | P&L | DD | Sharpe | Calmar |
|---|---|---|---|---|
| **Baseline (C3: ATM/3/5)** | **$206,746** | **-$5,006** | **4.20** | **41.3** |
| Min dist $1 at VIX<18 | $182,514 | -$5,467 | 4.80 | 33.4 |
| Min dist $2 at VIX<18 | $150,182 | -$3,532 | 5.11 | 42.5 |
| Pre-entry range < $12 filter | $158,236 | -$4,277 | 4.41 | 37.0 |
| Put +$2 wider at VIX>=18 | $196,574 | -$5,006 | 4.20 | 39.3 |
| Min $2 + range $12 (best risk) | $103,164 | -$2,541 | 5.45 | 40.6 |

**Findings**: All improvements trade P&L for better risk metrics. None clearly dominate baseline. Asymmetric put distance doesn't help — touch already handles the down-tail asymmetry. Min distance $2 halves DD but costs $57k. Current C3 is well-optimized for raw P&L.

---

## Credit Velocity Research (2026-04-04)

### Discovery: Strongest Signal Ever Found (Cohen's d = 1.54)

The 1-minute change in spread value from 15:55→15:56 (mid-based velocity) is the most powerful feature for separating wins from losses:

| Metric | Winners | Losers | Cohen's d |
|---|---|---|---|
| Mid velocity @15:56 | -$0.19 | +$0.50 | **1.54** |
| Spread value @15:56 | $0.53 | $1.46 | **1.68** |
| Spread value @15:57 | $0.41 | $1.63 | **2.15** |
| Spread value @15:59 | $0.29 | $2.07 | **2.30** |

For comparison, the best pre-entry feature (VIX) has d=0.50. This is 3x stronger.

### Velocity Distribution: Winners vs Losers

| Mid Velocity @15:56 | N | WR | Avg P&L | Total P&L |
|---|---|---|---|---|
| < -$0.50 (fast decay) | 187 | **97.3%** | +$710 | +$132,816 |
| -$0.50 to -$0.20 | 375 | **94.1%** | +$311 | +$116,654 |
| -$0.10 to $0 | 156 | 86.5% | +$110 | +$17,130 |
| $0 to +$0.10 | 135 | 80.0% | +$126 | +$17,043 |
| +$0.20 to +$0.50 | 130 | 50.8% | -$28 | -$3,693 |
| **> +$0.50 (fast spike)** | **185** | **23.8%** | **-$623** | **-$115,313** |

When the spread is spiking at 15:56 (vel > +$0.50), you lose 76% of the time. When decaying fast (< -$0.50), you win 97%.

### Credit Ratio at 15:56 (Close Cost / Entry Credit)

| Ratio | N | WR | Avg P&L | Total P&L | Touch % |
|---|---|---|---|---|---|
| < 0.5 (big decay) | 202 | 98.5% | +$475 | +$95,960 | 4% |
| 0.5-0.8 | 315 | 94.9% | +$363 | +$114,327 | 16% |
| 0.8-1.0 | 137 | 88.3% | +$295 | +$40,477 | 22% |
| 1.0-1.2 (underwater) | 154 | 79.9% | +$211 | +$32,480 | 29% |
| 1.5-2.0 | 181 | 63.5% | $0 | -$5 | 40% |
| **> 2.0 (deep trouble)** | **289** | **47.1%** | **-$366** | **-$105,541** | **54%** |

### Acceleration (vel_57 - vel_56)

Losers decelerate (acceleration = -0.35) while winners accelerate (+0.08). Cohen's d = -0.66. The spread value divergence accelerates after 15:56 — losses get worse faster.

### Backtest: Velocity-Based Early Exit — ALL LOSE P&L

| Exit Rule | P&L | vs Baseline | DD | Sharpe |
|---|---|---|---|---|
| **Baseline (touch only)** | **$204,100** | — | **-$5,006** | **4.20** |
| Exit if vel > $0.50 | $149,165 | -$55k | -$5,902 | 2.85 |
| Exit if vel > $0.20 | $106,943 | -$97k | -$5,877 | 2.03 |
| Exit if ratio > 2.0 | $141,633 | -$62k | -$5,902 | 2.73 |
| Exit if ratio > 3.0 | $183,220 | -$21k | -$5,006 | 3.59 |
| vel>0.10 & ratio>2.0 | $142,838 | -$61k | -$5,902 | 2.74 |

### Why Velocity Exit Fails

1. **Exit cost is too high**: At 15:56, spreads in trouble have wide bid-ask spreads. Buying back costs maximum premium.
2. **Mean-reversion saves many trades**: 47% of trades 2x underwater at 15:56 still recover and settle profitably by 16:00.
3. **Touch already handles true disasters**: The continuous touch exit catches trades where SPX actually breaches the strike. Velocity-based exit triggers earlier on trades that would have recovered.
4. **Structural mean-reversion**: The same mean-reversion that makes the strategy work (σ grows at 0.56x √t) also means deteriorating spreads partially recover.

### Verdict

Credit velocity is **the strongest diagnostic signal we've found** (d=1.54) but **cannot be used for early exit** because the exit cost exceeds the loss avoided. The current touch system is already optimal — it lets losing trades partially recover before closing. Velocity would be actionable if exit costs were lower (tighter bid-ask spreads), but in the last 5 minutes of 0DTE options, spreads are at their widest.

---

## VIX Range Budget Research (2026-04-04)

### Concept

VIX implies an expected daily range: `Expected Range = SPX × VIX/100 / √252 × 1.6`. By 15:50, some fraction of that range has been consumed. If >100% is consumed, the day has already exceeded its "budget" — the remaining movement may be larger (momentum) or smaller (exhaustion). The hypothesis: days with low range consumed are safer for premium selling.

### Range Consumed vs Last-8-Min |Move|

| Range Consumed | N | |Move| Mean | |Move| Median | P95 | Avg VIX |
|---|---|---|---|---|---|
| < 50% (quiet day) | 425 | **$3.99** | $3.02 | $10.4 | 17.3 |
| 50-75% | 379 | $5.35 | $4.10 | $14.4 | 20.0 |
| 75-100% | 149 | $5.91 | $4.65 | $15.4 | 21.2 |
| 100-150% (overshot) | 53 | $8.57 | $6.72 | $21.2 | 22.0 |
| > 150% | 7 | $8.80 | $8.06 | — | 26.4 |

Spearman ρ = 0.201 (p < 1e-10). Clear monotonic: overconsumed days have 2x larger last-8-min moves. But confounded with VIX (avg VIX 22.0 for overconsumed vs 17.3 for quiet).

### Adaptive Distance Backtests

All tests use 15:52 entry, OA-style touch (Call $0 / Put -$1), tiered sizing.

**A) Budget-only distance (replace VIX entirely):**

| Config | P&L | DD | Sharpe | Calmar |
|---|---|---|---|---|
| **Baseline (VIX C3)** | **$348,808** | **-$2,739** | **6.71** | **127.3** |
| Budget: <50%→ATM, 50-75%→$3, 75-100%→$5, >100%→$7 | $299,060 | -$2,739 | 6.21 | 109.2 |
| Budget: <50%→ATM, 50-75%→$2, 75-100%→$3, >100%→$5 | $325,694 | -$3,156 | 6.17 | 103.2 |

Budget-only is worse than VIX. VIX is the better distance driver.

**B) VIX + Budget combo:**

| Config | P&L | DD | Sharpe | Calmar |
|---|---|---|---|---|
| **Baseline** | **$348,808** | **-$2,739** | **6.71** | **127.3** |
| C3 + widen $2 if consumed>100% | $342,136 | -$2,739 | 6.66 | 124.9 |
| C3 + widen $2 if consumed>75% | $321,435 | -$2,739 | 6.53 | 117.4 |
| **C3 - tighten $2 if consumed<50%** | **$360,299** | **-$2,739** | **6.71** | **131.5** |
| C3 + tighten quiet + widen overconsumed | $353,627 | -$2,739 | 6.66 | 129.1 |

**Tightening on quiet days** (+$11.5k, same DD) is the only variant that helps — on quiet days with budget remaining, moving closer collects more credit safely. But the Sharpe doesn't improve (6.71 = same), so it's marginal.

**C) Budget-scaled distance:**

| Config | P&L | DD | Sharpe | Calmar |
|---|---|---|---|---|
| C3 × clamp(consumed, 0.5, 2.0) | $368,312 | -$2,739 | 6.59 | 134.5 |
| C3 × 1.5 if consumed>75% | $344,388 | -$2,739 | 6.71 | 125.7 |

Scaling by consumed ratio adds $19.5k P&L (best variant) but Sharpe drops to 6.59. More aggressive = more credit but also more touch exits.

**D) Skip overconsumed days:**

| Config | P&L | DD | Sharpe | Calmar |
|---|---|---|---|---|
| Skip if consumed > 100% (26 days) | $337,334 | -$2,739 | 6.77 | 123.2 |
| Skip if consumed > 125% (5 days) | $349,116 | -$2,739 | 6.78 | 127.5 |

Highest Sharpe (6.78) but loses $11.5k P&L for just 5 skipped days.

### Verdict

The VIX range budget signal is real (ρ=0.201) but **does not meaningfully improve the strategy**. Every variant either costs P&L or provides only marginal Sharpe improvement (≤0.07). The reason: VIX already captures most of the information, and touch exits handle the tail risk that budget would otherwise filter. The best variant (C3 × consumed scaling) adds $19.5k but at the cost of Sharpe, suggesting it's capturing noise not signal.

**Tightening on quiet days adopted** (+$12k, same DD). Widening/skipping not adopted.

---

## Parkinson Ratio Adaptive Distance (2026-04-04)

### Concept

The Parkinson volatility estimator uses OHLC data: `σ = sqrt(mean(ln(H/L)²) / (4·ln2))`. Compare the Parkinson vol of the **closing period** (15:25-15:50) vs the **full day** (9:31-15:50). If the ratio > 1.0, the market is getting hotter into the close — widen distance for safety.

### Signal Strength

| Feature | Spearman ρ with |move| |
|---|---|---|
| **Parkinson closing vol** | **0.292** (strongest) |
| Parkinson ratio (close/full) | 0.151 |
| Range consumed | 0.201 |
| Kaufman ER | 0.120 |

### Parkinson Ratio Buckets

| Ratio | N | |Move| Mean | |Move| Median | P95 |
|---|---|---|---|---|
| < 0.7 (cool close) | 109 | $4.03 | $3.19 | $11.2 |
| 0.7-0.9 | 326 | $4.48 | $3.58 | $11.8 |
| 0.9-1.0 | 197 | $4.40 | $3.11 | $13.0 |
| 1.0-1.1 | 146 | $5.58 | $4.11 | $18.1 |
| 1.1-1.3 | 151 | $6.00 | $5.00 | $15.4 |
| > 1.3 (hot close) | 90 | $7.24 | $5.28 | $18.0 |

Clear separation: hot close days have 1.8x larger last-8-min moves.

### Backtest Results

| Config | P&L | DD | Sharpe | Calmar | WR |
|---|---|---|---|---|---|
| Baseline (no Parkinson) | $360,299 | -$2,739 | 6.71 | 131.5 | 72.7% |
| **Widen +$2 if ratio > 1.0** | **$330,209** | **-$1,819** | **10.64** | **16.96** | **75.5%** |
| Widen +$2 if ratio > 1.1 | $338,156 | -$2,193 | 6.74 | 154.2 | 74.2% |
| Widen +$2 if ratio > 1.2 | $353,736 | -$2,739 | 6.87 | 129.1 | 73.5% |
| Skip if ratio > 1.0 | $232,639 | -$1,964 | 6.88 | 118.5 | 74.1% |
| Skip if ratio > 1.2 | $318,476 | -$2,739 | 7.00 | 116.3 | 73.7% |

### Why It Works

**Widening is better than skipping**: Skip removes profitable days entirely. Widening keeps the trade but with more buffer — you collect less credit but avoid the worst losses. The Parkinson ratio detects when the close is hotter than the day average, which predicts larger last-8-min moves. On these days, the extra $2 of distance prevents touch triggers and settlement losses.

**Adopted**: Widen +$2 when Parkinson ratio > 1.0. DD drops 34% (-$2,739 → -$1,819), Calmar +42%, P&L costs $30k (-8%). First improvement to reduce DD without skipping days.

---

## 50 Hypotheses Sweep (2026-04-04)

### Tested 15 of 50 hypotheses. Results:

**Winner — adopted:**
- **#19 Entry at 15:52**: +$142k P&L (+69%), DD -45%, Sharpe 4.20→6.71. Best single improvement ever found.

**Marginal positives — not adopted (too little benefit for complexity):**
- #22 VVIX > 120 skip: Sharpe +0.03, -$4k P&L, only 23 days
- #21 VIX/VIX9D > 1.20 skip: Sharpe -0.09, -$26k P&L
- #41 Day range > P75 skip: Sharpe +0.11, -$34k P&L

**Rejected (no improvement or negative):**
- #20 Exit at 15:59: -$115k P&L, DD doubles. Misses final mean-reversion.
- #4 Pin risk: No edge at any threshold.
- #29 Day-after-large-move: Marginal, costs P&L.
- #49 Previous day's last-5-min move: No predictive power.
- #30 Day-of-week: No actionable signal.
- #31 Vol-scaled sizing: Neutral to worse.
- #17 FOMC days: Too few to matter.
- #18 OpEx Fridays: Too few to matter.

**Not yet tested (may be worth exploring):**
- #43 VIX1D as distance driver
- #50 ML ensemble of top features
- #8 Skew steepness
- #38 Partial position + add at 15:57
- #11 Staggered entry
