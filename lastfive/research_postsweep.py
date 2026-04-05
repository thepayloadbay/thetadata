#!/usr/bin/env python3
"""H2-LIQ-1: Post-sweep day detection and LastFive performance analysis.

A "sweep" occurs when:
  - Bearish sweep: today's high > yesterday's high AND today's close < yesterday's high
  - Bullish sweep: today's low < yesterday's low AND today's close > yesterday's low

The DAY AFTER a sweep is a "post-sweep day". We check if LastFive performs
differently on post-sweep days vs non-post-sweep days.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

# ---------- CONFIG ----------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# ---------- LOAD SPX DAILY OHLC ----------
print("Loading SPX daily OHLC (2022-2026)...")
rows = []
for year in range(2022, 2027):
    ohlc_dir = os.path.join(DATA_DIR, str(year), "spx_ohlc")
    if not os.path.isdir(ohlc_dir):
        continue
    for fpath in sorted(glob.glob(os.path.join(ohlc_dir, "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
            if df.empty or len(df) < 5:
                continue
            valid = df[(df["high"] > 0) & (df["close"] > 0)]
            if valid.empty:
                continue
            rows.append({
                "date": date_str,
                "open": float(valid["open"].iloc[0]),
                "high": float(valid["high"].max()),
                "low": float(valid["low"].min()),
                "close": float(valid["close"].iloc[-1]),
            })
        except Exception:
            pass

spx = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
print(f"  SPX daily bars: {len(spx)} days ({spx['date'].iloc[0]} to {spx['date'].iloc[-1]})")

# ---------- DETECT SWEEPS ----------
spx["prev_high"] = spx["high"].shift(1)
spx["prev_low"] = spx["low"].shift(1)

# Bearish sweep: today's high > yesterday's high AND today's close < yesterday's high
spx["bearish_sweep"] = (spx["high"] > spx["prev_high"]) & (spx["close"] < spx["prev_high"])

# Bullish sweep: today's low < yesterday's low AND today's close > yesterday's low
spx["bullish_sweep"] = (spx["low"] < spx["prev_low"]) & (spx["close"] > spx["prev_low"])

# Any sweep
spx["any_sweep"] = spx["bearish_sweep"] | spx["bullish_sweep"]

# Post-sweep day = the day AFTER a sweep
spx["post_sweep"] = spx["any_sweep"].shift(1).fillna(False)
spx["post_bearish_sweep"] = spx["bearish_sweep"].shift(1).fillna(False)
spx["post_bullish_sweep"] = spx["bullish_sweep"].shift(1).fillna(False)

n_bearish = spx["bearish_sweep"].sum()
n_bullish = spx["bullish_sweep"].sum()
n_any = spx["any_sweep"].sum()
n_post = spx["post_sweep"].sum()

print(f"\nSweep Detection Summary (2022-2026):")
print(f"  Bearish sweeps: {n_bearish} ({n_bearish/len(spx)*100:.1f}% of days)")
print(f"  Bullish sweeps: {n_bullish} ({n_bullish/len(spx)*100:.1f}% of days)")
print(f"  Any sweep:      {n_any} ({n_any/len(spx)*100:.1f}% of days)")
print(f"  Post-sweep days: {n_post}")

# ---------- LOAD LASTFIVE TRADE LOG ----------
print("\nLoading LastFive trade log...")
log_files = sorted(glob.glob(os.path.join(LOG_DIR, "lastfive_trades_*.csv")))
if not log_files:
    print("ERROR: No trade log files found")
    sys.exit(1)

# Pick largest
best_file = max(log_files, key=lambda f: os.path.getsize(f))
trades = pd.read_csv(best_file)
print(f"  File: {os.path.basename(best_file)} ({len(trades)} trades)")

# Aggregate to daily P&L
daily_trades = trades.groupby("date").agg(
    pnl=("pnl_earned", "sum"),
    n_trades=("pnl_earned", "count"),
    wins=("win", "sum"),
).reset_index()
daily_trades["date"] = daily_trades["date"].astype(str)

# ---------- MERGE ----------
sweep_lookup = spx.set_index("date")[["post_sweep", "post_bearish_sweep", "post_bullish_sweep", "any_sweep"]].to_dict("index")

daily_trades["post_sweep"] = daily_trades["date"].map(lambda d: sweep_lookup.get(d, {}).get("post_sweep", False))
daily_trades["post_bearish"] = daily_trades["date"].map(lambda d: sweep_lookup.get(d, {}).get("post_bearish_sweep", False))
daily_trades["post_bullish"] = daily_trades["date"].map(lambda d: sweep_lookup.get(d, {}).get("post_bullish_sweep", False))
daily_trades["is_sweep_day"] = daily_trades["date"].map(lambda d: sweep_lookup.get(d, {}).get("any_sweep", False))

# ---------- ANALYSIS ----------
def report_group(label, mask):
    subset = daily_trades[mask]
    complement = daily_trades[~mask]
    if len(subset) == 0:
        print(f"\n  {label}: no matching days")
        return

    s_pnl = subset["pnl"].sum()
    s_avg = subset["pnl"].mean()
    s_wr = (subset["pnl"] > 0).sum() / len(subset) * 100
    s_med = subset["pnl"].median()

    c_pnl = complement["pnl"].sum()
    c_avg = complement["pnl"].mean()
    c_wr = (complement["pnl"] > 0).sum() / len(complement) * 100
    c_med = complement["pnl"].median()

    print(f"\n  {label}:")
    print(f"  {'':30s} {'POST-SWEEP':>14s}  {'NON-POST-SW':>14s}  {'DELTA':>10s}")
    print(f"  {'Days':30s} {len(subset):>14d}  {len(complement):>14d}")
    print(f"  {'Total P&L':30s} {'${:,.0f}'.format(s_pnl):>14s}  {'${:,.0f}'.format(c_pnl):>14s}")
    print(f"  {'Avg Daily P&L':30s} {'${:,.1f}'.format(s_avg):>14s}  {'${:,.1f}'.format(c_avg):>14s}  {'${:,.1f}'.format(s_avg - c_avg):>10s}")
    print(f"  {'Median Daily P&L':30s} {'${:,.1f}'.format(s_med):>14s}  {'${:,.1f}'.format(c_med):>14s}  {'${:,.1f}'.format(s_med - c_med):>10s}")
    print(f"  {'Win Rate (day-level)':30s} {s_wr:>13.1f}%  {c_wr:>13.1f}%  {s_wr - c_wr:>9.1f}%")

print("\n" + "=" * 72)
print("LASTFIVE PERFORMANCE: POST-SWEEP vs NON-POST-SWEEP DAYS")
print("=" * 72)

report_group("Any Sweep (bearish or bullish)", daily_trades["post_sweep"])
report_group("Post-Bearish Sweep", daily_trades["post_bearish"])
report_group("Post-Bullish Sweep", daily_trades["post_bullish"])
report_group("Sweep Day Itself (not post)", daily_trades["is_sweep_day"])

# ---------- ADDITIONAL: worst post-sweep days ----------
print("\n" + "-" * 72)
print("WORST 10 POST-SWEEP DAYS:")
print("-" * 72)
ps_days = daily_trades[daily_trades["post_sweep"]].sort_values("pnl").head(10)
for _, row in ps_days.iterrows():
    print(f"  {row['date']}  P&L: ${row['pnl']:>8,.0f}  trades: {row['n_trades']}  wins: {row['wins']}")

print("\nBEST 10 POST-SWEEP DAYS:")
print("-" * 72)
ps_days = daily_trades[daily_trades["post_sweep"]].sort_values("pnl", ascending=False).head(10)
for _, row in ps_days.iterrows():
    print(f"  {row['date']}  P&L: ${row['pnl']:>8,.0f}  trades: {row['n_trades']}  wins: {row['wins']}")
