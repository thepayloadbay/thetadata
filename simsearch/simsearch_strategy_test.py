#!/usr/bin/env python3
"""SimSearch Strategy Integration Test — apply filter to real strategy P&L.

Loads actual trade logs from MEDS, Apex, LastFive, and applies SimSearch's
TRADE/REDUCE/SKIP recommendations to measure real P&L impact.

Usage:
    python simsearch_strategy_test.py
    python simsearch_strategy_test.py --top-k 20
"""
from __future__ import annotations

import argparse
import logging
import os
import glob

import numpy as np
import pandas as pd

from simsearch_config import LOGS_DIR, RUN_TS
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_regime import predict_regime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


def load_daily_pnl(strategy: str) -> pd.DataFrame | None:
    """Load real daily P&L from strategy trade logs."""
    log_dir = os.path.join(PROJECT_ROOT, strategy, "logs")

    if strategy == "meds":
        files = sorted(glob.glob(os.path.join(log_dir, "meft_v35_bidask_log_*.csv")))
        if not files:
            return None
        df = pd.read_csv(files[-1])
        df["date"] = pd.to_datetime(df["entry_date"], format="%Y%m%d")
        daily = df.groupby("date")["pnl_earned"].sum().reset_index()
        daily.columns = ["date", "pnl"]
        return daily

    elif strategy == "apex":
        files = sorted(glob.glob(os.path.join(log_dir, "apex_trades_*.csv")))
        if not files:
            return None
        df = pd.read_csv(files[-1])
        df["date"] = pd.to_datetime(df["entry_date"], format="%Y%m%d")
        daily = df.groupby("date")["pnl"].sum().reset_index()
        return daily

    elif strategy == "lastfive":
        files = sorted(glob.glob(os.path.join(log_dir, "lastfive_trades_*.csv")))
        if not files:
            return None
        df = pd.read_csv(files[-1])
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        daily = df.groupby("date")["pnl_earned"].sum().reset_index()
        daily.columns = ["date", "pnl"]
        return daily

    elif strategy == "meic":
        files = sorted(glob.glob(os.path.join(log_dir, "meic_trades_*.csv")))
        if not files:
            return None
        df = pd.read_csv(files[-1])
        if "entry_date" in df.columns:
            df["date"] = pd.to_datetime(df["entry_date"], format="%Y%m%d")
        elif "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        else:
            return None
        pnl_col = "pnl" if "pnl" in df.columns else "pnl_earned" if "pnl_earned" in df.columns else None
        if pnl_col is None:
            return None
        daily = df.groupby("date")[pnl_col].sum().reset_index()
        daily.columns = ["date", "pnl"]
        return daily

    elif strategy == "zenith":
        files = sorted(glob.glob(os.path.join(log_dir, "zenith_trades_*.csv")))
        if not files:
            return None
        df = pd.read_csv(files[-1])
        if "trade_date" in df.columns:
            df["date"] = pd.to_datetime(df["trade_date"])
        elif "entry_date" in df.columns:
            df["date"] = pd.to_datetime(df["entry_date"], format="%Y%m%d")
        elif "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        else:
            return None
        pnl_col = "pnl" if "pnl" in df.columns else "pnl_earned" if "pnl_earned" in df.columns else None
        if pnl_col is None:
            return None
        daily = df.groupby("date")[pnl_col].sum().reset_index()
        daily.columns = ["date", "pnl"]
        return daily

    return None


def compute_stats(pnl_series: pd.Series) -> dict:
    """Compute strategy stats from daily P&L series."""
    if len(pnl_series) == 0:
        return {}
    total = pnl_series.sum()
    cumulative = pnl_series.cumsum()
    dd = (cumulative - cumulative.cummax()).min()
    sharpe = pnl_series.mean() / pnl_series.std() * np.sqrt(252) if pnl_series.std() > 0 else 0
    calmar = total / abs(dd) if dd < 0 else 999
    wr = (pnl_series > 0).mean()
    return {
        "total_pnl": total,
        "max_dd": dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": wr,
        "days": len(pnl_series),
        "worst_day": pnl_series.min(),
        "best_day": pnl_series.max(),
        "avg_pnl": pnl_series.mean(),
    }


def run_strategy_test(top_k: int = 20):
    """Apply SimSearch filter to each strategy's actual trade log."""
    features = load_features()
    engine = SimSearchEngine(features)

    strategies = ["meds", "apex", "lastfive", "meic", "zenith"]

    lines = []
    lines.append("=" * 80)
    lines.append("SimSearch Strategy Integration Test — Real P&L")
    lines.append("=" * 80)
    lines.append(f"SimSearch config: K={top_k}, all tiers, cosine, calibrated weights")
    lines.append("")

    for strat in strategies:
        daily = load_daily_pnl(strat)
        if daily is None:
            lines.append(f"\n{strat.upper()}: No trade log found, skipping")
            continue

        lines.append(f"\n{'='*70}")
        lines.append(f"  {strat.upper()}")
        lines.append(f"{'='*70}")
        lines.append(f"Trade log: {len(daily)} days "
                     f"({daily['date'].min().date()} to {daily['date'].max().date()})")

        # Generate SimSearch predictions for each trading day
        results = []
        for _, row in daily.iterrows():
            ds = row["date"].strftime("%Y%m%d")
            pred = predict_regime(engine, ds, top_k=top_k)

            if "error" in pred:
                action = "TRADE"
            else:
                action = pred["action"]

            if action == "TRADE":
                filtered_pnl = row["pnl"]
            elif action == "REDUCE":
                filtered_pnl = row["pnl"] * 0.5
            else:
                filtered_pnl = 0.0

            results.append({
                "date": row["date"],
                "unfiltered_pnl": row["pnl"],
                "action": action,
                "filtered_pnl": filtered_pnl,
            })

        rdf = pd.DataFrame(results)
        if rdf.empty:
            lines.append(f"  No overlapping dates with feature cache")
            continue

        # Compute stats
        uf = compute_stats(rdf["unfiltered_pnl"])
        ff = compute_stats(rdf["filtered_pnl"])
        traded = rdf[rdf["action"] == "TRADE"]
        skipped = rdf[rdf["action"] == "SKIP"]
        reduced = rdf[rdf["action"] == "REDUCE"]

        lines.append("")
        lines.append(f"  {'':20s} {'UNFILTERED':>12s} {'FILTERED':>12s} {'CHANGE':>12s}")
        lines.append(f"  {'-'*56}")
        lines.append(f"  {'Total P&L':20s} ${uf['total_pnl']:>11,.0f} ${ff['total_pnl']:>11,.0f} "
                     f"${ff['total_pnl']-uf['total_pnl']:>+11,.0f}")
        lines.append(f"  {'Max DD':20s} ${uf['max_dd']:>11,.0f} ${ff['max_dd']:>11,.0f} "
                     f"${ff['max_dd']-uf['max_dd']:>+11,.0f}")
        lines.append(f"  {'Sharpe':20s} {uf['sharpe']:>12.2f} {ff['sharpe']:>12.2f} "
                     f"{ff['sharpe']-uf['sharpe']:>+12.2f}")
        lines.append(f"  {'Calmar':20s} {uf['calmar']:>12.2f} {ff['calmar']:>12.2f} "
                     f"{ff['calmar']-uf['calmar']:>+12.2f}")
        lines.append(f"  {'Win Rate':20s} {uf['win_rate']:>11.1%} {ff['win_rate']:>11.1%}")
        lines.append(f"  {'Days':20s} {uf['days']:>12d} {len(traded):>12d}")
        lines.append(f"  {'Worst Day':20s} ${uf['worst_day']:>11,.0f} ${ff['worst_day']:>11,.0f}")

        if len(skipped) > 0:
            skip_pnl = skipped["unfiltered_pnl"]
            lines.append(f"\n  Skipped {len(skipped)} days:")
            lines.append(f"    Avg P&L on skipped days: ${skip_pnl.mean():,.0f}")
            lines.append(f"    Total saved:             ${-skip_pnl.sum():+,.0f}")
            lines.append(f"    Worst skipped day:       ${skip_pnl.min():,.0f}")
            lines.append(f"    Best skipped day:        ${skip_pnl.max():,.0f}")

        if len(reduced) > 0:
            red_pnl = reduced["unfiltered_pnl"]
            lines.append(f"  Reduced {len(reduced)} days:")
            lines.append(f"    Avg P&L on reduced days: ${red_pnl.mean():,.0f}")

    report = "\n".join(lines)
    print(report)

    # Save
    out = os.path.join(LOGS_DIR, f"strategy_test_{RUN_TS}.txt")
    with open(out, "w") as f:
        f.write(report)
    print(f"\nSaved: {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()
    run_strategy_test(top_k=args.top_k)


if __name__ == "__main__":
    main()
