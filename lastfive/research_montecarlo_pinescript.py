#!/usr/bin/env python3
"""Monte Carlo confidence intervals for LastFive strategy.

Bootstrap resampling of daily P&L to compute confidence intervals
for total P&L, max drawdown, Sharpe ratio, and win rate.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

# ---------- CONFIG ----------
N_RESAMPLES = 10_000
SEED = 42
ANNUAL_RF_RATE = 0.045
TRADING_DAYS_PER_YEAR = 252
PERCENTILES = [5, 25, 50, 75, 95]

# ---------- FIND TRADE LOG ----------
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
log_files = sorted(glob.glob(os.path.join(log_dir, "lastfive_trades_*.csv")))

if not log_files:
    print("ERROR: No trade log files found in", log_dir)
    sys.exit(1)

# Pick the largest file with ~1300+ trades (both range_budget and park_ratio filters)
best_file = None
best_count = 0
for f in log_files:
    try:
        n = sum(1 for _ in open(f)) - 1  # subtract header
        if n > best_count:
            best_count = n
            best_file = f
    except Exception:
        pass

if best_file is None:
    print("ERROR: Could not find a suitable trade log")
    sys.exit(1)

print(f"Loading trade log: {os.path.basename(best_file)} ({best_count} trades)")

# ---------- LOAD DATA ----------
df = pd.read_csv(best_file)
print(f"  Columns: {list(df.columns)}")

# Aggregate to daily P&L (sum all trades per day)
daily_pnl = df.groupby("date")["pnl_earned"].sum().values
n_days = len(daily_pnl)
print(f"  Trading days: {n_days}")
print(f"  Total P&L: ${daily_pnl.sum():,.0f}")
print(f"  Mean daily P&L: ${daily_pnl.mean():.2f}")
print()

# ---------- BOOTSTRAP ----------
rng = np.random.RandomState(SEED)

results = {
    "total_pnl": [],
    "max_dd": [],
    "sharpe": [],
    "win_rate": [],
}

daily_rf = ANNUAL_RF_RATE / TRADING_DAYS_PER_YEAR

for i in range(N_RESAMPLES):
    # Sample with replacement
    sample = rng.choice(daily_pnl, size=n_days, replace=True)

    # Total P&L
    total = sample.sum()
    results["total_pnl"].append(total)

    # Max drawdown
    cum = np.cumsum(sample)
    running_max = np.maximum.accumulate(cum)
    dd = cum - running_max
    max_dd = dd.min()
    results["max_dd"].append(max_dd)

    # Sharpe ratio (annualized)
    excess = sample - daily_rf
    if excess.std() > 0:
        sharpe = (excess.mean() / excess.std()) * np.sqrt(TRADING_DAYS_PER_YEAR)
    else:
        sharpe = 0.0
    results["sharpe"].append(sharpe)

    # Win rate (day-level: positive P&L days / total days)
    wr = (sample > 0).sum() / len(sample) * 100
    results["win_rate"].append(wr)

# ---------- REPORT ----------
print("=" * 72)
print(f"MONTE CARLO BOOTSTRAP RESULTS  ({N_RESAMPLES:,} resamples, {n_days} trading days)")
print("=" * 72)

header = f"{'Metric':<18}" + "".join(f"{'P' + str(p):>10}" for p in PERCENTILES)
print(header)
print("-" * len(header))

for metric, label, fmt in [
    ("total_pnl",  "Total P&L ($)",   "${:>,.0f}"),
    ("max_dd",     "Max Drawdown ($)", "${:>,.0f}"),
    ("sharpe",     "Sharpe Ratio",     "{:>.2f}"),
    ("win_rate",   "Win Rate (%)",     "{:>.1f}%"),
]:
    vals = np.array(results[metric])
    pcts = np.percentile(vals, PERCENTILES)
    row = f"{label:<18}"
    for v in pcts:
        if "P&L" in label or "Drawdown" in label:
            row += f"{'${:,.0f}'.format(v):>10}"
        elif "Sharpe" in label:
            row += f"{v:>10.2f}"
        else:
            row += f"{v:>9.1f}%"
    print(row)

print()
print("Interpretation:")
print(f"  - 90% CI for Total P&L: ${np.percentile(results['total_pnl'], 5):,.0f} to ${np.percentile(results['total_pnl'], 95):,.0f}")
print(f"  - 90% CI for Max DD:    ${np.percentile(results['max_dd'], 5):,.0f} to ${np.percentile(results['max_dd'], 95):,.0f}")
print(f"  - 90% CI for Sharpe:    {np.percentile(results['sharpe'], 5):.2f} to {np.percentile(results['sharpe'], 95):.2f}")
print(f"  - Median Sharpe:        {np.percentile(results['sharpe'], 50):.2f}")
print(f"  - P(Total P&L > 0):     {(np.array(results['total_pnl']) > 0).mean() * 100:.1f}%")
print(f"  - P(Max DD < -$20k):    {(np.array(results['max_dd']) < -20000).mean() * 100:.1f}%")
