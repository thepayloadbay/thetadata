"""Apex — Reporting & Analysis."""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime

import numpy as np

import apex_config as _cfg
from apex_engine import compute_metrics, logger

# ——————————————————————————————————————
#  PERFORMANCE REPORT
# ——————————————————————————————————————
def print_performance_report(trades: list, label: str = "BACKTEST") -> dict:
    """Print and return performance metrics."""
    m = compute_metrics(trades)
    logger.info(f"\n{'='*60}")
    logger.info(f"  {label} RESULTS ({_cfg.PILOT_YEAR_START} → {_cfg.PILOT_YEAR_END})")
    logger.info(f"{'='*60}")
    logger.info(f"  Trades      : {m['num_trades']}")
    logger.info(f"  Win Rate    : {m['win_rate']:.1f}%")
    logger.info(f"  Total P&L   : ${m['total_pnl']:,.2f}")
    logger.info(f"  Max DD      : ${m['max_drawdown']:,.2f}")
    logger.info(f"  Sharpe      : {m['sharpe']:.2f}")
    logger.info(f"  Calmar      : {m['calmar']:.2f}")
    logger.info(f"  Profit Factor: {m['profit_factor']:.2f}")
    logger.info(f"  Avg Win     : ${m['avg_win']:,.2f}")
    logger.info(f"  Avg Loss    : ${m['avg_loss']:,.2f}")
    logger.info(f"  Max Single Loss: ${m['max_single_loss']:,.2f}")
    logger.info(f"{'='*60}\n")
    return m


def format_results_table(m: dict) -> str:
    """Format metrics as markdown table."""
    return f"""| Metric | Value |
|---|---|
| Trades | {m['num_trades']} |
| Win Rate | {m['win_rate']:.1f}% |
| Total P&L | ${m['total_pnl']:,.2f} |
| Max DD | ${m['max_drawdown']:,.2f} |
| Sharpe | {m['sharpe']:.2f} |
| Calmar | {m['calmar']:.2f} |
| Profit Factor | {m['profit_factor']:.2f} |
| Avg Win | ${m['avg_win']:,.2f} |
| Avg Loss | ${m['avg_loss']:,.2f} |
| Max Single Loss | ${m['max_single_loss']:,.2f} |"""


# ——————————————————————————————————————
#  VIX ANALYSIS
# ——————————————————————————————————————
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-30", 25.0, 30.0),
    ("30+",   30.0, None),
]

def print_vix_analysis(trades: list):
    """Break down performance by VIX bucket."""
    logger.info("\n--- VIX ZONE ANALYSIS ---")
    for label, lo, hi in VIX_BUCKETS:
        bucket = []
        for t in trades:
            v = t.get("vix_level")
            if v == "" or v is None:
                continue
            v = float(v)
            if lo is not None and v < lo:
                continue
            if hi is not None and v >= hi:
                continue
            bucket.append(t)
        if not bucket:
            continue
        m = compute_metrics(bucket)
        logger.info(f"  VIX {label:<6}: {m['num_trades']:>4} trades, "
                    f"WR {m['win_rate']:>5.1f}%, P&L ${m['total_pnl']:>10,.0f}, "
                    f"DD ${m['max_drawdown']:>8,.0f}, Sharpe {m['sharpe']:>5.2f}")


# ——————————————————————————————————————
#  DAY-OF-WEEK ANALYSIS
# ——————————————————————————————————————
DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]

def print_dow_analysis(trades: list):
    """Break down by day of week."""
    logger.info("\n--- DAY-OF-WEEK ANALYSIS ---")
    buckets = defaultdict(list)
    for t in trades:
        try:
            dt = datetime.strptime(t["entry_date"], "%Y%m%d")
            buckets[dt.weekday()].append(t)
        except ValueError:
            pass
    for dow in range(5):
        if dow not in buckets:
            continue
        m = compute_metrics(buckets[dow])
        logger.info(f"  {DOW_NAMES[dow]}: {m['num_trades']:>4} trades, "
                    f"WR {m['win_rate']:>5.1f}%, P&L ${m['total_pnl']:>10,.0f}, "
                    f"Sharpe {m['sharpe']:>5.2f}")


# ——————————————————————————————————————
#  WORST DAYS ANALYSIS
# ——————————————————————————————————————
def print_worst_days(trades: list, n: int = 10):
    """Show the N worst P&L days with details."""
    daily = defaultdict(lambda: {"pnl": 0.0, "trades": []})
    for t in trades:
        d = t["entry_date"]
        daily[d]["pnl"] += t["pnl"]
        daily[d]["trades"].append(t)

    worst = sorted(daily.items(), key=lambda x: x[1]["pnl"])[:n]

    logger.info(f"\n--- TOP {n} WORST DAYS ---")
    for date_str, info in worst:
        sample = info["trades"][0]
        vix = sample.get("vix_level", "?")
        n_trades = len(info["trades"])
        logger.info(f"  {date_str}: P&L=${info['pnl']:>10,.0f} | "
                    f"{n_trades} trades | VIX={vix}")


# ——————————————————————————————————————
#  ITERATION LOGGING
# ——————————————————————————————————————
def log_iteration(iteration: int, idea: str, m: dict, verdict: str,
                  notes: str = "", **extra_params):
    """Append a row to iterations.csv."""
    csv_path = os.path.join(_cfg.PROJECT_ROOT, "iterations.csv")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = {
        "iteration": iteration,
        "timestamp": ts,
        "idea": idea,
        "entry_time": extra_params.get("entry_time", _cfg.ENTRY_TIME),
        "direction": extra_params.get("direction", _cfg.DIRECTION_MODE),
        "width": extra_params.get("width", _cfg.WIDTH),
        "otm_distance": extra_params.get("otm_distance", _cfg.MIN_OTM_DISTANCE),
        "interval": extra_params.get("interval", _cfg.ENTRY_INTERVAL if _cfg.ENABLE_MULTI_ENTRY else "single"),
        "filters": extra_params.get("filters", "none"),
        "qty": extra_params.get("qty", _cfg.QTY),
        "num_trades": m["num_trades"],
        "win_rate": m["win_rate"],
        "total_pnl": m["total_pnl"],
        "max_drawdown": m["max_drawdown"],
        "sharpe": m["sharpe"],
        "calmar": m["calmar"],
        "profit_factor": m["profit_factor"],
        "avg_win": m["avg_win"],
        "avg_loss": m["avg_loss"],
        "max_single_loss": m["max_single_loss"],
        "verdict": verdict,
        "notes": notes,
    }

    fieldnames = list(row.keys())
    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    # Check if file only has header
    if os.path.exists(csv_path):
        with open(csv_path, "r") as f:
            lines = f.readlines()
            if len(lines) <= 1:
                write_header = True

    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            pass  # Header already exists from initialization
        w.writerow(row)

    logger.info(f"[ITER {iteration}] {idea} → {verdict} | "
                f"P&L=${m['total_pnl']:,.0f}, Sharpe={m['sharpe']:.2f}, DD=${m['max_drawdown']:,.0f}")
