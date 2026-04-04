"""
Monte Carlo Simulation for LastFive Strategy
- 10,000 shuffled simulations of 610 trades
- Risk of ruin, drawdown distribution, confidence intervals
- Half-trade scenario (305 trades)
- Kelly criterion calculation
"""

import numpy as np
import pandas as pd

# ── Load trades ──────────────────────────────────────────────────────────────
df = pd.read_csv("/Users/jeerapongwongchote/Documents/thetadata/lastfive/tradelogs_lastfive.csv")
pnl = df["pnl_earned"].values
n_trades = len(pnl)

print(f"Loaded {n_trades} trades")
print(f"Total P&L: ${pnl.sum():,.0f}")
print(f"Win rate: {(pnl > 0).mean():.1%}")
print(f"Avg win:  ${pnl[pnl > 0].mean():,.0f}")
print(f"Avg loss: ${pnl[pnl < 0].mean():,.0f}")
print()

# ── Parameters ───────────────────────────────────────────────────────────────
STARTING_EQUITY = 50_000
N_SIMS = 10_000
RUIN_THRESHOLD_50 = 25_000   # 50% loss
RUIN_THRESHOLD_30 = 35_000   # 30% loss
np.random.seed(42)

# ── Full Monte Carlo (610 trades) ───────────────────────────────────────────
final_equities = np.zeros(N_SIMS)
max_drawdowns = np.zeros(N_SIMS)
max_consec_losses = np.zeros(N_SIMS, dtype=int)
min_equities = np.zeros(N_SIMS)

for i in range(N_SIMS):
    shuffled = np.random.permutation(pnl)
    equity_curve = STARTING_EQUITY + np.cumsum(shuffled)
    equity_with_start = np.concatenate([[STARTING_EQUITY], equity_curve])

    # Final equity
    final_equities[i] = equity_curve[-1]

    # Min equity (for ruin check)
    min_equities[i] = equity_with_start.min()

    # Max drawdown
    running_max = np.maximum.accumulate(equity_with_start)
    drawdowns = equity_with_start - running_max
    max_drawdowns[i] = drawdowns.min()  # most negative

    # Max consecutive losses
    losses = shuffled < 0
    max_streak = 0
    current_streak = 0
    for trade_is_loss in losses:
        if trade_is_loss:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    max_consec_losses[i] = max_streak

# ── Results: Full 610 trades ────────────────────────────────────────────────
print("=" * 65)
print("  MONTE CARLO SIMULATION — FULL 610 TRADES (10,000 sims)")
print("=" * 65)

# Risk of ruin
ror_50 = (min_equities < RUIN_THRESHOLD_50).mean()
ror_30 = (min_equities < RUIN_THRESHOLD_30).mean()
print(f"\n  Risk of Ruin (equity drops below $25k / 50% loss): {ror_50:.2%}")
print(f"  Risk of Ruin (equity drops below $35k / 30% loss): {ror_30:.2%}")

# Probability of profit
prob_profit = (final_equities > STARTING_EQUITY).mean()
print(f"  Probability of Profit (end > $50k):                {prob_profit:.2%}")

# Final equity distribution
pcts = [5, 25, 50, 75, 95]
fe_pcts = np.percentile(final_equities, pcts)
print(f"\n  Final Equity Distribution:")
for p, v in zip(pcts, fe_pcts):
    print(f"    {p:>3}th percentile: ${v:>12,.0f}")
print(f"    Mean:            ${final_equities.mean():>12,.0f}")

# Total P&L confidence interval
total_pnl = final_equities - STARTING_EQUITY
ci_low, ci_high = np.percentile(total_pnl, [2.5, 97.5])
print(f"\n  95% Confidence Interval on Total P&L:")
print(f"    [{ci_low:>+12,.0f}  to  {ci_high:>+12,.0f}]")

# Max drawdown distribution
print(f"\n  Max Drawdown Distribution:")
for p, v in zip(pcts, np.percentile(max_drawdowns, pcts)):
    print(f"    {p:>3}th percentile: ${v:>12,.0f}")
print(f"    Mean:            ${max_drawdowns.mean():>12,.0f}")
print(f"    Worst (min):     ${max_drawdowns.min():>12,.0f}")

# Consecutive losses
print(f"\n  Max Consecutive Losses Distribution:")
for p, v in zip(pcts, np.percentile(max_consec_losses, pcts)):
    print(f"    {p:>3}th percentile: {v:>5.0f}")
print(f"    Mean:            {max_consec_losses.mean():>5.1f}")
print(f"    Worst (max):     {max_consec_losses.max():>5d}")

# ── Half-Trade Monte Carlo (305 trades) ─────────────────────────────────────
N_HALF = n_trades // 2
final_eq_half = np.zeros(N_SIMS)
max_dd_half = np.zeros(N_SIMS)
min_eq_half = np.zeros(N_SIMS)

for i in range(N_SIMS):
    selected = np.random.choice(pnl, size=N_HALF, replace=False)
    shuffled = np.random.permutation(selected)
    equity_curve = STARTING_EQUITY + np.cumsum(shuffled)
    equity_with_start = np.concatenate([[STARTING_EQUITY], equity_curve])

    final_eq_half[i] = equity_curve[-1]
    min_eq_half[i] = equity_with_start.min()

    running_max = np.maximum.accumulate(equity_with_start)
    drawdowns = equity_with_start - running_max
    max_dd_half[i] = drawdowns.min()

print()
print("=" * 65)
print(f"  MONTE CARLO SIMULATION — HALF TRADES ({N_HALF} trades, 10,000 sims)")
print("=" * 65)

ror_50_h = (min_eq_half < RUIN_THRESHOLD_50).mean()
ror_30_h = (min_eq_half < RUIN_THRESHOLD_30).mean()
prob_profit_h = (final_eq_half > STARTING_EQUITY).mean()

print(f"\n  Risk of Ruin (below $25k / 50% loss): {ror_50_h:.2%}")
print(f"  Risk of Ruin (below $35k / 30% loss): {ror_30_h:.2%}")
print(f"  Probability of Profit (end > $50k):   {prob_profit_h:.2%}")

fe_half_pcts = np.percentile(final_eq_half, pcts)
print(f"\n  Final Equity Distribution:")
for p, v in zip(pcts, fe_half_pcts):
    print(f"    {p:>3}th percentile: ${v:>12,.0f}")
print(f"    Mean:            ${final_eq_half.mean():>12,.0f}")

pnl_half = final_eq_half - STARTING_EQUITY
ci_low_h, ci_high_h = np.percentile(pnl_half, [2.5, 97.5])
print(f"\n  95% CI on Total P&L: [{ci_low_h:>+12,.0f}  to  {ci_high_h:>+12,.0f}]")

print(f"\n  Max Drawdown Distribution:")
for p, v in zip(pcts, np.percentile(max_dd_half, pcts)):
    print(f"    {p:>3}th percentile: ${v:>12,.0f}")
print(f"    Mean:            ${max_dd_half.mean():>12,.0f}")

# ── Kelly Criterion ──────────────────────────────────────────────────────────
wins = pnl[pnl > 0]
losses = pnl[pnl < 0]
win_rate = len(wins) / n_trades
loss_rate = 1 - win_rate
avg_win = wins.mean()
avg_loss = abs(losses.mean())
win_loss_ratio = avg_win / avg_loss  # b in Kelly formula

# Kelly: f* = (bp - q) / b  where b = avg_win/avg_loss, p = win_rate, q = loss_rate
kelly_fraction = (win_loss_ratio * win_rate - loss_rate) / win_loss_ratio
half_kelly = kelly_fraction / 2
quarter_kelly = kelly_fraction / 4

print()
print("=" * 65)
print("  KELLY CRITERION")
print("=" * 65)
print(f"\n  Win rate (p):          {win_rate:.4f}")
print(f"  Loss rate (q):         {loss_rate:.4f}")
print(f"  Avg win:               ${avg_win:,.0f}")
print(f"  Avg loss:              ${avg_loss:,.0f}")
print(f"  Win/Loss ratio (b):    {win_loss_ratio:.4f}")
print(f"\n  Full Kelly fraction:   {kelly_fraction:.2%}")
print(f"  Half Kelly fraction:   {half_kelly:.2%}")
print(f"  Quarter Kelly fraction:{quarter_kelly:.2%}")
print(f"\n  Interpretation:")
print(f"    Full Kelly says risk {kelly_fraction:.1%} of bankroll per trade.")
print(f"    On $50k account: ${STARTING_EQUITY * kelly_fraction:,.0f} per trade (Full Kelly)")
print(f"    On $50k account: ${STARTING_EQUITY * half_kelly:,.0f} per trade (Half Kelly)")
print(f"    On $50k account: ${STARTING_EQUITY * quarter_kelly:,.0f} per trade (Quarter Kelly)")
print(f"    Current risk: $2,500/side x 2 sides = $5,000 max risk per day")
print(f"    Current risk as % of $50k: {5000/STARTING_EQUITY:.1%}")

# Compare current sizing to Kelly
current_risk_pct = 5000 / STARTING_EQUITY
if current_risk_pct > kelly_fraction:
    print(f"\n    ⚠ OVER-BET: Current risk ({current_risk_pct:.1%}) EXCEEDS Full Kelly ({kelly_fraction:.1%})")
elif current_risk_pct > half_kelly:
    print(f"\n    Current risk ({current_risk_pct:.1%}) is between Half Kelly and Full Kelly — aggressive but acceptable")
else:
    print(f"\n    Current risk ({current_risk_pct:.1%}) is at or below Half Kelly — conservative")

print()
print("=" * 65)
print("  SUMMARY")
print("=" * 65)
print(f"""
  The LastFive strategy with 610 historical trades shows:
  - {prob_profit:.1%} probability of ending profitable over the full sample
  - {ror_50:.2%} risk of ever hitting a 50% drawdown from $50k start
  - Median final equity: ${np.median(final_equities):,.0f} (median P&L: ${np.median(total_pnl):,.0f})
  - 95% of simulations see max DD between ${np.percentile(max_drawdowns, 5):,.0f} and ${np.percentile(max_drawdowns, 95):,.0f}
  - Kelly optimal bet size: {kelly_fraction:.1%} of bankroll per trade
  - Even with only half the trades, {prob_profit_h:.1%} chance of profit
""")
