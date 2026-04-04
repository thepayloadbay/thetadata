#!/usr/bin/env python3
"""SimSearch Filter Backtest — test regime filter as a real strategy overlay.

Simulates applying SimSearch's TRADE/REDUCE/SKIP recommendations to
credit spread strategies. Uses actual daily P&L data where available,
or a range-based P&L proxy.

Usage:
    python simsearch_filter_backtest.py
    python simsearch_filter_backtest.py --top-k 15
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import glob

import numpy as np
import pandas as pd

from simsearch_config import LOGS_DIR, RUN_TS, VALIDATION_START, VALIDATION_END
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_regime import predict_regime, DANGER_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def load_strategy_pnl(strategy_dir: str, strategy_name: str) -> pd.DataFrame | None:
    """Load daily P&L from a strategy's trade logs.

    Looks for the most recent trade log CSV and aggregates to daily P&L.
    """
    log_dir = os.path.join(strategy_dir, "logs")
    if not os.path.exists(log_dir):
        return None

    # Find trade log files
    patterns = [
        f"{strategy_name}_trades_*.csv",
        f"meft_*_log_*.csv",  # MEDS uses different naming
    ]

    csv_files = []
    for pat in patterns:
        csv_files.extend(glob.glob(os.path.join(log_dir, pat)))

    if not csv_files:
        return None

    # Use most recent file
    latest = max(csv_files, key=os.path.getmtime)
    log.info("Loading %s P&L from %s", strategy_name, os.path.basename(latest))

    try:
        df = pd.read_csv(latest)
    except Exception as e:
        log.warning("Failed to read %s: %s", latest, e)
        return None

    # Try to find date and P&L columns
    date_col = None
    pnl_col = None

    for c in df.columns:
        cl = c.lower()
        if cl in ("date", "trade_date", "entry_date"):
            date_col = c
        elif cl in ("pnl", "daily_pnl", "net_pnl", "realized_pnl", "total_pnl"):
            pnl_col = c

    if date_col is None:
        # Try timestamp column
        for c in df.columns:
            if "timestamp" in c.lower() or "time" in c.lower():
                date_col = c
                break

    if date_col is None or pnl_col is None:
        log.warning("Cannot find date/pnl columns in %s. Columns: %s", latest, list(df.columns))
        return None

    df[date_col] = pd.to_datetime(df[date_col])
    df["date"] = df[date_col].dt.normalize()

    daily = df.groupby("date")[pnl_col].sum().reset_index()
    daily.columns = ["date", "pnl"]
    return daily


def simulate_range_proxy_pnl(features: pd.DataFrame) -> pd.DataFrame:
    """Create a simple credit spread P&L proxy based on actual daily ranges.

    Model: selling 0DTE credit spreads daily.
    - If range < 1.0%: win $120 (typical credit collected)
    - If range 1.0-1.5%: win $60 (partial credit, some SL hits)
    - If range 1.5-2.0%: lose $200 (SL hits on most positions)
    - If range > 2.0%: lose $600 (cascade SL blowthrough)
    - If range > 3.0%: lose $1500 (massive spike day, like 2026-04-02)

    These are calibrated to approximate MEDS historical P&L per day vs range.
    """
    df = features[["date", "outcome_range_pct"]].dropna().copy()

    def range_to_pnl(r):
        if r < 0.007:
            return 150    # very calm day, full credit
        elif r < 0.010:
            return 120    # calm
        elif r < 0.015:
            return 60     # normal, some SL
        elif r < 0.020:
            return -200   # volatile
        elif r < 0.030:
            return -600   # danger
        else:
            return -1500  # catastrophic

    df["pnl"] = df["outcome_range_pct"].apply(range_to_pnl)
    return df[["date", "pnl"]]


def run_filter_backtest(start: str = "2023-06-01",
                        end: str = "2026-03-25",
                        top_k: int = 15) -> str:
    """Run the full filter backtest simulation."""
    features = load_features()
    engine = SimSearchEngine(features)

    # Get daily P&L proxy
    pnl_df = simulate_range_proxy_pnl(features)
    pnl_df = pnl_df[(pnl_df["date"] >= pd.Timestamp(start)) &
                     (pnl_df["date"] <= pd.Timestamp(end))]

    log.info("Running filter backtest: %d days, K=%d", len(pnl_df), top_k)

    # Generate predictions for each day
    results = []
    for _, row in pnl_df.iterrows():
        ds = row["date"].strftime("%Y%m%d")
        pred = predict_regime(engine, ds, top_k=top_k)

        if "error" in pred:
            action = "TRADE"  # default if no prediction
        else:
            action = pred["action"]

        # Apply filter
        if action == "TRADE":
            filtered_pnl = row["pnl"]
            size = 1.0
        elif action == "REDUCE":
            filtered_pnl = row["pnl"] * 0.5  # half size
            size = 0.5
        else:  # SKIP
            filtered_pnl = 0
            size = 0.0

        results.append({
            "date": row["date"],
            "unfiltered_pnl": row["pnl"],
            "action": action,
            "size": size,
            "filtered_pnl": filtered_pnl,
            "actual_range": features[features["date"] == row["date"]]["outcome_range_pct"].iloc[0]
            if len(features[features["date"] == row["date"]]) > 0 else np.nan,
        })

    df = pd.DataFrame(results)

    # Compute metrics
    lines = []
    lines.append("=" * 70)
    lines.append("SimSearch Filter Backtest — Credit Spread P&L Proxy")
    lines.append("=" * 70)
    lines.append(f"Period: {start} to {end} ({len(df)} days)")
    lines.append(f"SimSearch: top_k={top_k}")
    lines.append("")

    # Unfiltered
    uf_total = df["unfiltered_pnl"].sum()
    uf_dd = df["unfiltered_pnl"].cumsum().expanding().apply(
        lambda x: x.iloc[-1] - x.max()).min()
    uf_sharpe = (df["unfiltered_pnl"].mean() / df["unfiltered_pnl"].std() * np.sqrt(252)
                 if df["unfiltered_pnl"].std() > 0 else 0)
    uf_calmar = uf_total / abs(uf_dd) if uf_dd < 0 else 999

    lines.append("UNFILTERED (trade every day)")
    lines.append("-" * 50)
    lines.append(f"  Total P&L:    ${uf_total:,.0f}")
    lines.append(f"  Max DD:       ${uf_dd:,.0f}")
    lines.append(f"  Sharpe:       {uf_sharpe:.2f}")
    lines.append(f"  Calmar:       {uf_calmar:.2f}")
    lines.append(f"  Win rate:     {(df['unfiltered_pnl'] > 0).mean():.1%}")
    lines.append(f"  Days traded:  {len(df)}")

    # Filtered
    f_total = df["filtered_pnl"].sum()
    f_dd = df["filtered_pnl"].cumsum().expanding().apply(
        lambda x: x.iloc[-1] - x.max()).min()
    f_sharpe = (df["filtered_pnl"].mean() / df["filtered_pnl"].std() * np.sqrt(252)
                if df["filtered_pnl"].std() > 0 else 0)
    f_calmar = f_total / abs(f_dd) if f_dd < 0 else 999
    traded = df[df["size"] > 0]

    lines.append("")
    lines.append("FILTERED (SimSearch regime filter)")
    lines.append("-" * 50)
    lines.append(f"  Total P&L:    ${f_total:,.0f}")
    lines.append(f"  Max DD:       ${f_dd:,.0f}")
    lines.append(f"  Sharpe:       {f_sharpe:.2f}")
    lines.append(f"  Calmar:       {f_calmar:.2f}")
    lines.append(f"  Win rate:     {(traded['filtered_pnl'] > 0).mean():.1%}" if len(traded) > 0 else "")
    lines.append(f"  Days traded:  {len(traded)} ({len(traded)/len(df):.0%})")
    lines.append(f"  Days skipped: {(df['action'] == 'SKIP').sum()}")
    lines.append(f"  Days reduced: {(df['action'] == 'REDUCE').sum()}")

    # Improvement
    lines.append("")
    lines.append("IMPROVEMENT")
    lines.append("-" * 50)
    lines.append(f"  P&L change:   ${f_total - uf_total:+,.0f} ({(f_total/uf_total - 1)*100:+.1f}%)"
                 if uf_total != 0 else f"  P&L change: ${f_total - uf_total:+,.0f}")
    lines.append(f"  DD change:    ${f_dd - uf_dd:+,.0f} ({'better' if f_dd > uf_dd else 'worse'})")
    lines.append(f"  Sharpe change: {f_sharpe - uf_sharpe:+.2f}")

    # What got skipped
    skipped = df[df["action"] == "SKIP"]
    reduced = df[df["action"] == "REDUCE"]
    if len(skipped) > 0:
        lines.append(f"\n  Skipped days avg unfiltered P&L: ${skipped['unfiltered_pnl'].mean():,.0f}")
        lines.append(f"  Skipped days total saved:        ${-skipped['unfiltered_pnl'].sum():+,.0f}")
    if len(reduced) > 0:
        lines.append(f"  Reduced days avg unfiltered P&L:  ${reduced['unfiltered_pnl'].mean():,.0f}")
        lines.append(f"  Reduced days total saved:         ${-(reduced['unfiltered_pnl'].sum() - reduced['filtered_pnl'].sum()):+,.0f}")

    # Worst days analysis
    lines.append("")
    lines.append("WORST DAY ANALYSIS")
    lines.append("-" * 50)
    worst = df.nsmallest(10, "unfiltered_pnl")
    for _, r in worst.iterrows():
        lines.append(f"  {r['date'].strftime('%Y-%m-%d')}: "
                     f"unfiltered=${r['unfiltered_pnl']:+,.0f}  "
                     f"action={r['action']:6s}  "
                     f"filtered=${r['filtered_pnl']:+,.0f}  "
                     f"range={r['actual_range']:.2%}")

    report = "\n".join(lines)

    # Save
    out = os.path.join(LOGS_DIR, f"filter_backtest_{RUN_TS}.csv")
    df.to_csv(out, index=False)

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=15)
    parser.add_argument("--start", type=str, default="2023-06-01")
    parser.add_argument("--end", type=str, default="2026-03-25")
    args = parser.parse_args()

    report = run_filter_backtest(start=args.start, end=args.end, top_k=args.top_k)
    print(report)


if __name__ == "__main__":
    main()
