#!/usr/bin/env python3
"""GEX / DIX Analysis — TODO item #5.

SqueezeMetrics data:
  - GEX (Gamma Exposure): positive = dealers long gamma (market pinned),
    negative = dealers short gamma (market volatile, trend-following).
  - DIX (Dark Index): dark pool short volume ratio. High DIX = institutions
    buying (bullish), low DIX = institutions selling (bearish).

Tests both as skip filters and sizing signals against MEDS backtest P&L.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

def load_trades() -> pd.DataFrame:
    logs_dir = os.path.join(os.path.dirname(__file__), "logs")
    csvs = sorted(glob.glob(os.path.join(logs_dir, "meft_v35_bidask_log_*.csv")))
    if not csvs:
        print("No trade CSV found. Run a marathon first.")
        sys.exit(1)
    df = pd.read_csv(csvs[-1])
    df["date"] = df["entry_date"].astype(str).str.replace("-", "")
    return df


def build_daily_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    daily = trades.groupby("date").agg(
        pnl=("pnl_earned", "sum"),
        trades=("pnl_earned", "count"),
        wins=("win", "sum"),
        vix=("vix_level", "first"),
    ).reset_index()
    daily["wr"] = daily["wins"] / daily["trades"] * 100
    return daily.sort_values("date").reset_index(drop=True)


def load_squeezemetrics() -> pd.DataFrame:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "squeezemetrics.csv")
    df = pd.read_csv(path)
    # Convert date to YYYYMMDD format
    df["date_str"] = df["date"].str.replace("-", "")
    return df


def quintile_analysis(merged: pd.DataFrame, col: str, label: str):
    """Quintile P&L analysis on a column."""
    # Use prior-day value (signal available before market open)
    merged[f"{col}_prev"] = merged[col].shift(1)
    valid = merged.dropna(subset=[f"{col}_prev"])

    print(f"\n{'='*75}")
    print(f"QUINTILE ANALYSIS — {label} (prior-day value)")
    print(f"{'='*75}")

    valid["quintile"] = pd.qcut(valid[f"{col}_prev"], 5, labels=["Q1","Q2","Q3","Q4","Q5"])

    print(f"{'Quintile':<10} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12} {'Avg Signal':>14}")
    print("-" * 65)

    for q in ["Q1","Q2","Q3","Q4","Q5"]:
        subset = valid[valid["quintile"] == q]
        n = len(subset)
        wr = (subset["pnl"] > 0).mean() * 100
        avg = subset["pnl"].mean()
        total = subset["pnl"].sum()
        avg_sig = subset[f"{col}_prev"].mean()
        print(f"{q:<10} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f} {avg_sig:>14.4f}")

    q1 = valid[valid["quintile"]=="Q1"]["pnl"].mean()
    q5 = valid[valid["quintile"]=="Q5"]["pnl"].mean()
    print(f"\nQ1→Q5 gradient: ${q5 - q1:.2f}/day")

    # Also check negative GEX specifically (dealers short gamma = volatile)
    if col == "gex":
        neg = valid[valid[f"{col}_prev"] < 0]
        pos = valid[valid[f"{col}_prev"] >= 0]
        print(f"\nGEX < 0 (dealers short gamma): {len(neg)} days, WR={((neg['pnl']>0).mean()*100):.1f}%, avg=${neg['pnl'].mean():.2f}")
        print(f"GEX >= 0 (dealers long gamma):  {len(pos)} days, WR={((pos['pnl']>0).mean()*100):.1f}%, avg=${pos['pnl'].mean():.2f}")

    return valid


def vix_crosstab(merged: pd.DataFrame, col: str, label: str):
    """Cross-tab signal quintile × VIX range."""
    merged[f"{col}_prev"] = merged[col].shift(1)
    valid = merged.dropna(subset=[f"{col}_prev"])
    valid["quintile"] = pd.qcut(valid[f"{col}_prev"], 5, labels=["Q1","Q2","Q3","Q4","Q5"])

    vix_bins = [(0, 15), (15, 20), (20, 25), (25, 35)]
    vix_labels = ["<15", "15-20", "20-25", "25-35"]

    print(f"\n{'='*75}")
    print(f"{label} × VIX CROSS-TAB")
    print(f"{'='*75}")
    print(f"{'Quintile':<10} {'VIX':<8} {'Days':>5} {'WR%':>6} {'Avg P&L':>10}")
    print("-" * 45)

    for q in ["Q1", "Q5"]:  # Just show extremes
        for (lo, hi), vlabel in zip(vix_bins, vix_labels):
            subset = valid[(valid["quintile"] == q) & (valid["vix"] >= lo) & (valid["vix"] < hi)]
            if len(subset) < 3:
                continue
            wr = (subset["pnl"] > 0).mean() * 100
            print(f"{q:<10} {vlabel:<8} {len(subset):>5} {wr:>5.1f}% ${subset['pnl'].mean():>9.2f}")
        print("-" * 45)


def correlation_analysis(merged: pd.DataFrame):
    """Check correlations between GEX/DIX and existing signals."""
    merged["gex_prev"] = merged["gex"].shift(1)
    merged["dix_prev"] = merged["dix"].shift(1)
    valid = merged.dropna(subset=["gex_prev", "dix_prev", "vix"])

    print(f"\n{'='*75}")
    print("CORRELATION MATRIX")
    print(f"{'='*75}")
    cols = ["gex_prev", "dix_prev", "vix", "pnl"]
    corr = valid[cols].corr()
    print(corr.round(3).to_string())


if __name__ == "__main__":
    print("Loading data...")
    trades = load_trades()
    daily = build_daily_pnl(trades)
    sm = load_squeezemetrics()

    # Merge on date
    merged = daily.merge(sm[["date_str", "gex", "dix"]], left_on="date", right_on="date_str", how="inner")
    print(f"Merged: {len(merged)} days with both backtest P&L and SqueezeMetrics data")

    # Baseline
    bl_wr = (merged["pnl"] > 0).mean() * 100
    print(f"Baseline: {len(merged)} days, WR={bl_wr:.1f}%, avg=${merged['pnl'].mean():.2f}, total=${merged['pnl'].sum():.2f}")

    # Quintile analyses
    quintile_analysis(merged.copy(), "gex", "GEX (Gamma Exposure)")
    quintile_analysis(merged.copy(), "dix", "DIX (Dark Index)")

    # VIX cross-tabs
    vix_crosstab(merged.copy(), "gex", "GEX")
    vix_crosstab(merged.copy(), "dix", "DIX")

    # Correlations
    correlation_analysis(merged.copy())
