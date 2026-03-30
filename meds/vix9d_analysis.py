#!/usr/bin/env python3
"""VIX9D / VIX Term Structure Analysis — Backlog Rank 3.

Tests:
  1. Quintile analysis of VIX9D/VIX ratio vs daily P&L
  2. Inversion filter (VIX9D > VIX → skip day)
  3. Contango bonus (VIX/VIX9D > 1.15 → size up)
  4. Gap narrowing as SL tightener
  5. VIX cross-tab (VIX9D signal × VIX bucket)
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


def load_vix9d() -> pd.DataFrame:
    """Load VIX9D from parquet files in data/YYYY/vix9d/."""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    rows = []
    for fpath in sorted(glob.glob(os.path.join(data_dir, "*", "vix9d", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix9d_close" in vdf.columns:
                rows.append({
                    "date": str(vdf["date"].iloc[0]),
                    "vix9d": float(vdf["vix9d_close"].iloc[0]),
                })
        except Exception:
            pass
    if not rows:
        print("No VIX9D parquet files found.")
        sys.exit(1)
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df


def quintile_analysis(merged: pd.DataFrame, col: str, label: str):
    """Quintile P&L analysis on a column (prior-day value)."""
    merged[f"{col}_prev"] = merged[col].shift(1)
    valid = merged.dropna(subset=[f"{col}_prev"])

    print(f"\n{'='*75}")
    print(f"QUINTILE ANALYSIS — {label} (prior-day value)")
    print(f"{'='*75}")

    valid["quintile"] = pd.qcut(valid[f"{col}_prev"], 5, labels=["Q1","Q2","Q3","Q4","Q5"], duplicates="drop")

    print(f"{'Quintile':<10} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12} {'Avg Signal':>14}")
    print("-" * 65)

    for q in ["Q1","Q2","Q3","Q4","Q5"]:
        subset = valid[valid["quintile"] == q]
        if len(subset) == 0:
            continue
        n = len(subset)
        wr = (subset["pnl"] > 0).mean() * 100
        avg = subset["pnl"].mean()
        total = subset["pnl"].sum()
        avg_sig = subset[f"{col}_prev"].mean()
        print(f"{q:<10} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f} {avg_sig:>14.4f}")

    q1 = valid[valid["quintile"]=="Q1"]["pnl"].mean()
    q5 = valid[valid["quintile"]=="Q5"]["pnl"].mean()
    print(f"\nQ1→Q5 gradient: ${q5 - q1:.2f}/day")
    return valid


def inversion_analysis(merged: pd.DataFrame):
    """Test: skip days when VIX9D > VIX (term structure inversion)."""
    print(f"\n{'='*75}")
    print("INVERSION FILTER — Skip days when VIX9D > VIX (backwardation)")
    print(f"{'='*75}")

    # Use prior-day values for signal
    merged["ratio_prev"] = merged["ratio"].shift(1)
    valid = merged.dropna(subset=["ratio_prev"])

    inverted = valid[valid["ratio_prev"] > 1.0]
    normal = valid[valid["ratio_prev"] <= 1.0]

    print(f"\nInverted (VIX9D > VIX):  {len(inverted)} days, "
          f"WR={(inverted['pnl']>0).mean()*100:.1f}%, "
          f"avg=${inverted['pnl'].mean():.2f}, "
          f"total=${inverted['pnl'].sum():.2f}")
    print(f"Normal   (VIX9D <= VIX): {len(normal)} days, "
          f"WR={(normal['pnl']>0).mean()*100:.1f}%, "
          f"avg=${normal['pnl'].mean():.2f}, "
          f"total=${normal['pnl'].sum():.2f}")

    skip_cost = inverted["pnl"].sum()
    print(f"\nCost of skipping inverted days: ${skip_cost:.2f}")

    # Also test with stricter thresholds
    for thresh in [1.02, 1.05, 1.10]:
        inv = valid[valid["ratio_prev"] > thresh]
        if len(inv) > 0:
            print(f"  VIX9D/VIX > {thresh:.2f}: {len(inv)} days, "
                  f"WR={(inv['pnl']>0).mean()*100:.1f}%, "
                  f"avg=${inv['pnl'].mean():.2f}, "
                  f"skip cost=${inv['pnl'].sum():.2f}")


def contango_analysis(merged: pd.DataFrame):
    """Test: size up when VIX/VIX9D > 1.15 (deep contango = vol risk premium)."""
    print(f"\n{'='*75}")
    print("CONTANGO BONUS — Size up when VIX/VIX9D > threshold")
    print(f"{'='*75}")

    merged["inv_ratio_prev"] = (1.0 / merged["ratio"]).shift(1)  # VIX/VIX9D
    valid = merged.dropna(subset=["inv_ratio_prev"])

    for thresh in [1.05, 1.10, 1.15, 1.20]:
        deep = valid[valid["inv_ratio_prev"] > thresh]
        rest = valid[valid["inv_ratio_prev"] <= thresh]
        if len(deep) > 0:
            print(f"  VIX/VIX9D > {thresh:.2f}: {len(deep)} days, "
                  f"WR={(deep['pnl']>0).mean()*100:.1f}%, "
                  f"avg=${deep['pnl'].mean():.2f}, "
                  f"total=${deep['pnl'].sum():.2f}")
        if len(rest) > 0:
            print(f"  VIX/VIX9D <= {thresh:.2f}: {len(rest)} days, "
                  f"WR={(rest['pnl']>0).mean()*100:.1f}%, "
                  f"avg=${rest['pnl'].mean():.2f}, "
                  f"total=${rest['pnl'].sum():.2f}")
        print()


def vix_crosstab(merged: pd.DataFrame):
    """Cross-tab: VIX9D/VIX ratio quintile × VIX range."""
    merged["ratio_prev"] = merged["ratio"].shift(1)
    valid = merged.dropna(subset=["ratio_prev"])
    valid["quintile"] = pd.qcut(valid["ratio_prev"], 5, labels=["Q1","Q2","Q3","Q4","Q5"], duplicates="drop")

    vix_bins = [(0, 13), (13, 15), (15, 20), (20, 25), (25, 35)]
    vix_labels = ["<13", "13-15", "15-20", "20-25", "25-35"]

    print(f"\n{'='*75}")
    print("VIX9D/VIX RATIO × VIX CROSS-TAB")
    print(f"{'='*75}")
    print(f"{'Quintile':<10} {'VIX':<8} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    print("-" * 55)

    for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        for (lo, hi), vlabel in zip(vix_bins, vix_labels):
            subset = valid[(valid["quintile"] == q) & (valid["vix"] >= lo) & (valid["vix"] < hi)]
            if len(subset) < 3:
                continue
            wr = (subset["pnl"] > 0).mean() * 100
            print(f"{q:<10} {vlabel:<8} {len(subset):>5} {wr:>5.1f}% ${subset['pnl'].mean():>9.2f} ${subset['pnl'].sum():>11.2f}")
        print("-" * 55)


def gap_narrowing_analysis(merged: pd.DataFrame):
    """Test: does rapid gap narrowing (VIX9D approaching VIX) predict losses?"""
    print(f"\n{'='*75}")
    print("GAP NARROWING — Does rapid ratio change predict losses?")
    print(f"{'='*75}")

    # Compute 1-day change in ratio
    merged["ratio_chg"] = merged["ratio"].diff()
    merged["ratio_chg_prev"] = merged["ratio_chg"].shift(1)
    valid = merged.dropna(subset=["ratio_chg_prev"])

    # Quintile on ratio change (positive = VIX9D rising relative to VIX = narrowing)
    valid["chg_quintile"] = pd.qcut(valid["ratio_chg_prev"], 5,
                                     labels=["Q1(falling)","Q2","Q3","Q4","Q5(rising)"],
                                     duplicates="drop")

    print(f"\n{'Quintile':<15} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Avg Δ Ratio':>14}")
    print("-" * 55)

    for q in ["Q1(falling)","Q2","Q3","Q4","Q5(rising)"]:
        subset = valid[valid["chg_quintile"] == q]
        if len(subset) == 0:
            continue
        wr = (subset["pnl"] > 0).mean() * 100
        print(f"{q:<15} {len(subset):>5} {wr:>5.1f}% ${subset['pnl'].mean():>9.2f} {subset['ratio_chg_prev'].mean():>14.4f}")


def correlation_analysis(merged: pd.DataFrame):
    """Correlations between VIX9D signals and P&L."""
    merged["ratio_prev"] = merged["ratio"].shift(1)
    merged["vix9d_prev"] = merged["vix9d"].shift(1)
    valid = merged.dropna(subset=["ratio_prev", "vix9d_prev"])

    print(f"\n{'='*75}")
    print("CORRELATION MATRIX")
    print(f"{'='*75}")
    cols = ["ratio_prev", "vix9d_prev", "vix", "pnl"]
    corr = valid[cols].corr()
    print(corr.round(3).to_string())

    # How correlated is VIX9D/VIX ratio with VIX level?
    print(f"\nVIX9D/VIX ratio vs VIX level correlation: {valid['ratio_prev'].corr(valid['vix']):.3f}")
    print(f"VIX9D vs VIX correlation: {valid['vix9d_prev'].corr(valid['vix']):.3f}")


if __name__ == "__main__":
    print("Loading data...")
    trades = load_trades()
    daily = build_daily_pnl(trades)
    vix9d_df = load_vix9d()

    # Merge on date
    merged = daily.merge(vix9d_df, on="date", how="inner")
    merged["ratio"] = merged["vix9d"] / merged["vix"].clip(lower=0.01)  # VIX9D/VIX
    print(f"Merged: {len(merged)} days with both backtest P&L and VIX9D data")

    # Baseline
    bl_wr = (merged["pnl"] > 0).mean() * 100
    print(f"Baseline: {len(merged)} days, WR={bl_wr:.1f}%, "
          f"avg=${merged['pnl'].mean():.2f}, total=${merged['pnl'].sum():.2f}")
    print(f"VIX9D range: {merged['vix9d'].min():.2f} – {merged['vix9d'].max():.2f}")
    print(f"VIX9D/VIX ratio range: {merged['ratio'].min():.3f} – {merged['ratio'].max():.3f}")

    # Analyses
    quintile_analysis(merged.copy(), "ratio", "VIX9D/VIX Ratio")
    quintile_analysis(merged.copy(), "vix9d", "VIX9D (absolute level)")
    inversion_analysis(merged.copy())
    contango_analysis(merged.copy())
    vix_crosstab(merged.copy())
    gap_narrowing_analysis(merged.copy())
    correlation_analysis(merged.copy())
