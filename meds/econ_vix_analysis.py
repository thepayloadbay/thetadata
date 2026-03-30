#!/usr/bin/env python3
"""Econ Day Analysis — TODO items #8 and #9.

#9: Adjacency analysis — WR/P&L on T-1 and T+1 around each econ event.
#8: VIX-range cross-tab — WR by VIX bucket for each event type.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

# ── Load project config for event dates ───────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meds_config as _cfg
from meds_config import _build_calendar_event_dates, FOMC_DATES, TRIPLE_WITCHING_DATES


def load_trades() -> pd.DataFrame:
    """Load most recent trade log CSV."""
    logs_dir = os.path.join(os.path.dirname(__file__), "logs")
    csvs = sorted(glob.glob(os.path.join(logs_dir, "meft_v35_bidask_log_*.csv")))
    if not csvs:
        print("No trade CSV found. Run a marathon first.")
        sys.exit(1)
    df = pd.read_csv(csvs[-1])
    df["date"] = df["entry_date"].astype(str).str.replace("-", "")
    return df


def build_daily_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trades to daily P&L with VIX."""
    daily = trades.groupby("date").agg(
        pnl=("pnl_earned", "sum"),
        trades=("pnl_earned", "count"),
        wins=("win", "sum"),
        vix=("vix_level", "first"),
    ).reset_index()
    daily["wr"] = daily["wins"] / daily["trades"] * 100
    daily = daily.sort_values("date").reset_index(drop=True)
    return daily


def get_event_dates() -> dict[str, set[str]]:
    """Get all event date sets."""
    # Build calendar event dates (needs trading day list)
    all_dates_sorted = sorted(glob.glob(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "20*", "spx_ohlc", "*.parquet")
    ))
    all_bdays = set()
    for p in all_dates_sorted:
        d = os.path.basename(p).replace(".parquet", "")
        all_bdays.add(d)

    cal = _build_calendar_event_dates()

    # CPI dates from config
    cpi_dates = set()
    for attr in dir(_cfg):
        if "cpi" in attr.lower():
            pass
    # Directly grab from the function internals
    # The function returns a dict with keys like 'cpi', 'fomc', etc.
    return {
        "CPI": cal.get("cpi", set()),
        "PCE": cal.get("pce", set()),
        "FOMC": cal.get("fomc", set()),
        "NFP": cal.get("nfp", set()),
        "TW": cal.get("triple_witching", set()),
        "EOM": cal.get("eom", set()),
        "Pre-TW": cal.get("pre_tw", set()),
        "Post-Hol": cal.get("post_holiday", set()),
    }


def adjacency_analysis(daily: pd.DataFrame, event_dates: dict[str, set[str]]):
    """TODO #9: Check WR/P&L on T-1 and T+1 around each event type."""
    dates_list = daily["date"].tolist()
    date_to_idx = {d: i for i, d in enumerate(dates_list)}

    print("=" * 80)
    print("ECON DAY ADJACENCY ANALYSIS (T-1, Event Day, T+1)")
    print("=" * 80)
    print(f"{'Event':<10} {'Window':<8} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    print("-" * 60)

    # Baseline
    bl_wr = (daily["pnl"] > 0).mean() * 100
    bl_avg = daily["pnl"].mean()
    print(f"{'Baseline':<10} {'all':<8} {len(daily):>5} {bl_wr:>5.1f}% ${bl_avg:>9.2f} ${daily['pnl'].sum():>11.2f}")
    print("-" * 60)

    results = []
    for event_name, dates in sorted(event_dates.items()):
        for window_name, offset in [("T-1", -1), ("T=0", 0), ("T+1", 1)]:
            target_dates = set()
            for ed in dates:
                if ed in date_to_idx:
                    idx = date_to_idx[ed] + offset
                    if 0 <= idx < len(dates_list):
                        target_dates.add(dates_list[idx])

            subset = daily[daily["date"].isin(target_dates)]
            if len(subset) == 0:
                continue

            n = len(subset)
            wr = (subset["pnl"] > 0).mean() * 100
            avg = subset["pnl"].mean()
            total = subset["pnl"].sum()
            results.append({
                "event": event_name, "window": window_name,
                "days": n, "wr": wr, "avg_pnl": avg, "total_pnl": total
            })
            # Flag if WR is notably below baseline
            flag = " <<<" if wr < bl_wr - 5 else ""
            print(f"{event_name:<10} {window_name:<8} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f}{flag}")

        print("-" * 60)

    return results


def vix_range_crosstab(daily: pd.DataFrame, event_dates: dict[str, set[str]]):
    """TODO #8: Cross-tab WR by VIX bucket for each event type."""
    vix_bins = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 100)]
    vix_labels = ["<15", "15-20", "20-25", "25-30", "30+"]

    print("\n" + "=" * 80)
    print("VIX-RANGE × ECON EVENT CROSS-TAB")
    print("=" * 80)
    print(f"{'Event':<10} {'VIX':<8} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12} {'Flag':>6}")
    print("-" * 65)

    # Baseline by VIX range
    for lo, hi in vix_bins:
        label = f"{lo}-{hi}" if hi < 100 else f"{lo}+"
        subset = daily[(daily["vix"] >= lo) & (daily["vix"] < hi)]
        if len(subset) > 0:
            wr = (subset["pnl"] > 0).mean() * 100
            print(f"{'Baseline':<10} {label:<8} {len(subset):>5} {wr:>5.1f}% ${subset['pnl'].mean():>9.2f} ${subset['pnl'].sum():>11.2f}")
    print("-" * 65)

    weak_combos = []
    for event_name, dates in sorted(event_dates.items()):
        event_daily = daily[daily["date"].isin(dates)]
        if len(event_daily) == 0:
            continue

        for (lo, hi), label in zip(vix_bins, vix_labels):
            subset = event_daily[(event_daily["vix"] >= lo) & (event_daily["vix"] < hi)]
            if len(subset) == 0:
                continue

            n = len(subset)
            wr = (subset["pnl"] > 0).mean() * 100
            avg = subset["pnl"].mean()
            total = subset["pnl"].sum()

            # Get baseline WR for this VIX range
            bl_vix = daily[(daily["vix"] >= lo) & (daily["vix"] < hi)]
            bl_wr = (bl_vix["pnl"] > 0).mean() * 100 if len(bl_vix) > 0 else 0

            flag = ""
            if wr < bl_wr - 10 and n >= 3:
                flag = "WEAK"
                weak_combos.append({
                    "event": event_name, "vix_range": label,
                    "days": n, "wr": wr, "bl_wr": bl_wr,
                    "avg_pnl": avg, "total_pnl": total,
                })

            print(f"{event_name:<10} {label:<8} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f} {flag:>6}")

        print("-" * 65)

    if weak_combos:
        print("\n" + "=" * 80)
        print("WEAK COMBOS (WR >10pp below VIX-range baseline, n≥3)")
        print("=" * 80)
        print(f"{'Event':<10} {'VIX':<8} {'Days':>5} {'Event WR':>9} {'BL WR':>7} {'Δ WR':>7} {'Avg P&L':>10} {'Total P&L':>12}")
        print("-" * 75)
        for c in sorted(weak_combos, key=lambda x: x["wr"]):
            delta = c["wr"] - c["bl_wr"]
            print(f"{c['event']:<10} {c['vix_range']:<8} {c['days']:>5} {c['wr']:>8.1f}% {c['bl_wr']:>6.1f}% {delta:>6.1f}% ${c['avg_pnl']:>9.2f} ${c['total_pnl']:>11.2f}")

    return weak_combos


if __name__ == "__main__":
    print("Loading trades...")
    trades = load_trades()
    daily = build_daily_pnl(trades)
    event_dates = get_event_dates()

    print(f"Daily P&L: {len(daily)} days, Event types: {list(event_dates.keys())}")
    for name, dates in sorted(event_dates.items()):
        in_bt = len(dates & set(daily["date"].tolist()))
        print(f"  {name}: {in_bt} days in backtest")
    print()

    adjacency_analysis(daily, event_dates)
    weak_combos = vix_range_crosstab(daily, event_dates)
