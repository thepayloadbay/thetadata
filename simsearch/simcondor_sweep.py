#!/usr/bin/env python3
"""SimCondor Parameter Sweep — find optimal configuration.

Sweeps key parameters and reports P&L, Sharpe, DD, WR for each combo.
Uses the same engine as simcondor.py but overrides config in-memory.

Usage:
    python simcondor_sweep.py
"""
from __future__ import annotations

import itertools
import logging
import os
import sys
import time
from datetime import time as dt_time

import numpy as np
import pandas as pd

import simcondor
from simsearch_config import LOGS_DIR, RUN_TS
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_regime import predict_regime

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)

BACKTEST_START = "2022-06-01"
BACKTEST_END = "2026-03-25"


def run_single(engine, dates, pred_cache, config: dict) -> dict:
    """Run a single backtest with overridden config. Returns summary stats."""
    # Override simcondor module-level config
    simcondor.WIDTH = config.get("width", 20)
    simcondor.QTY = config.get("qty", 2)
    simcondor.STRIKE_MULTIPLIER = config.get("strike_mult", 1.0)
    simcondor.MIN_OTM_DISTANCE = config.get("min_otm", 30)
    simcondor.MAX_OTM_DISTANCE = config.get("max_otm", 100)
    simcondor.STOP_LOSS_PER_SIDE = config.get("sl", -400)
    simcondor.MIN_CREDIT_PER_SIDE = config.get("min_credit", 0.50)
    simcondor.ENTRY_TIME = config.get("entry_time", dt_time(9, 45))
    simcondor.TRADE_REGIMES = config.get("regimes", {"CALM"})
    simcondor.SIMSEARCH_TOP_K = config.get("top_k", 20)

    results = []
    for ds in dates:
        result = simcondor.backtest_day(ds, engine, pred_cache)
        if result is not None:
            results.append(result)

    df = pd.DataFrame(results)
    trades = df[df["action"] == "TRADE"] if len(df) > 0 else pd.DataFrame()

    if len(trades) == 0:
        return {"n_trades": 0, "pnl": 0, "sharpe": 0, "dd": 0, "wr": 0, "calmar": 0}

    pnl = trades["pnl"].sum()
    cum = trades["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()
    sharpe = trades["pnl"].mean() / trades["pnl"].std() * np.sqrt(252) if trades["pnl"].std() > 0 else 0
    calmar = pnl / abs(dd) if dd < 0 else 999
    wr = (trades["pnl"] > 0).mean()
    avg_win = trades[trades["pnl"] > 0]["pnl"].mean() if (trades["pnl"] > 0).any() else 0
    avg_loss = trades[trades["pnl"] <= 0]["pnl"].mean() if (trades["pnl"] <= 0).any() else 0
    pf = trades[trades["pnl"] > 0]["pnl"].sum() / abs(trades[trades["pnl"] <= 0]["pnl"].sum()) if (trades["pnl"] <= 0).any() and trades[trades["pnl"] <= 0]["pnl"].sum() < 0 else 999

    return {
        "n_trades": len(trades),
        "pnl": pnl,
        "dd": dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "wr": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pf": pf,
        "worst": trades["pnl"].min(),
    }


def main():
    features = load_features()
    engine = SimSearchEngine(features)

    # Get dates
    dates_df = features[
        (features["date"] >= pd.Timestamp(BACKTEST_START)) &
        (features["date"] <= pd.Timestamp(BACKTEST_END))
    ]["date"].dt.strftime("%Y%m%d").tolist()

    # Pre-compute SimSearch predictions (shared across all sweeps)
    print("Pre-computing SimSearch predictions...")
    pred_cache = {}
    for ds in dates_df:
        pred = predict_regime(engine, ds, top_k=20)
        pred_cache[ds] = pred
    print(f"Cached {len(pred_cache)} predictions")

    # =============================================
    #  Phase 1: Individual parameter sweeps
    # =============================================
    baseline = {
        "width": 20, "qty": 2, "strike_mult": 1.0, "min_otm": 30,
        "max_otm": 100, "sl": -400, "min_credit": 0.50,
        "entry_time": dt_time(9, 45), "regimes": {"CALM"}, "top_k": 20,
    }

    print("\n" + "=" * 100)
    print("SimCondor Parameter Sweep")
    print("=" * 100)
    print(f"Baseline: width={baseline['width']}, qty={baseline['qty']}, "
          f"strike_mult={baseline['strike_mult']}, min_otm={baseline['min_otm']}, "
          f"SL={baseline['sl']}, min_credit={baseline['min_credit']}, "
          f"entry={baseline['entry_time']}")

    # Run baseline
    t0 = time.time()
    base_result = run_single(engine, dates_df, pred_cache, baseline)
    base_time = time.time() - t0
    print(f"\nBASELINE: {base_result['n_trades']} trades, "
          f"${base_result['pnl']:,.0f} P&L, "
          f"DD ${base_result['dd']:,.0f}, "
          f"Sharpe {base_result['sharpe']:.2f}, "
          f"WR {base_result['wr']:.1%}, "
          f"PF {base_result['pf']:.2f} "
          f"({base_time:.1f}s)")

    sweeps = {
        "width": [10, 15, 20, 25, 30],
        "strike_mult": [0.5, 0.65, 0.75, 0.85, 1.0, 1.2],
        "min_otm": [20, 25, 30, 40, 50],
        "sl": [-200, -300, -400, -500, -700, -1000, None],  # None = no SL
        "min_credit": [0.30, 0.40, 0.50, 0.60, 0.80, 1.00],
        "entry_time": [dt_time(9, 35), dt_time(9, 45), dt_time(10, 0),
                       dt_time(10, 30), dt_time(11, 0)],
        "qty": [1, 2, 3],
        "regimes": [{"CALM"}, {"CALM", "NORMAL"}],
    }

    for param_name, values in sweeps.items():
        print(f"\n--- Sweep: {param_name} ---")
        print(f"{'Value':>12s} {'Trades':>7s} {'P&L':>10s} {'DD':>10s} "
              f"{'Sharpe':>7s} {'Calmar':>8s} {'WR':>6s} {'PF':>6s} {'Worst':>8s}")
        print("-" * 85)

        for val in values:
            config = baseline.copy()
            if param_name == "sl" and val is None:
                config["sl"] = -99999  # effectively no SL
                label = "None"
            else:
                config[param_name] = val
                label = str(val)
                if param_name == "entry_time":
                    label = val.strftime("%H:%M")
                elif param_name == "regimes":
                    label = "+".join(sorted(val))

            t0 = time.time()
            result = run_single(engine, dates_df, pred_cache, config)
            elapsed = time.time() - t0

            marker = " <<<" if (param_name in config and config[param_name] == baseline.get(param_name)) else ""
            if param_name == "regimes" and val == baseline["regimes"]:
                marker = " <<<"

            print(f"{label:>12s} {result['n_trades']:>7d} "
                  f"${result['pnl']:>9,.0f} ${result['dd']:>9,.0f} "
                  f"{result['sharpe']:>7.2f} {result['calmar']:>8.2f} "
                  f"{result['wr']:>5.1%} {result['pf']:>6.2f} "
                  f"${result['worst']:>7,.0f} ({elapsed:.1f}s){marker}")

    # =============================================
    #  Phase 2: Best combo from Phase 1
    # =============================================
    print("\n" + "=" * 100)
    print("Phase 2: Testing top combinations")
    print("=" * 100)

    combos = [
        {"label": "Baseline", **baseline},
        {"label": "Width15+SL300", **{**baseline, "width": 15, "sl": -300}},
        {"label": "Width10+SL200", **{**baseline, "width": 10, "sl": -200}},
        {"label": "Width20+SL500", **{**baseline, "width": 20, "sl": -500}},
        {"label": "Width20+NoSL", **{**baseline, "sl": -99999}},
        {"label": "MinOTM40+Width20", **{**baseline, "min_otm": 40}},
        {"label": "Entry1030", **{**baseline, "entry_time": dt_time(10, 30)}},
        {"label": "MinCredit80", **{**baseline, "min_credit": 0.80}},
        {"label": "StrikeMult0.65", **{**baseline, "strike_mult": 0.65}},
        {"label": "Tight: W10+OTM20+SM0.5", **{**baseline, "width": 10, "min_otm": 20, "strike_mult": 0.5}},
        {"label": "Wide: W20+OTM40+SM1.0", **{**baseline, "width": 20, "min_otm": 40, "strike_mult": 1.0}},
        {"label": "Safe: W20+OTM50+MC0.8", **{**baseline, "width": 20, "min_otm": 50, "min_credit": 0.80}},
    ]

    print(f"\n{'Config':>30s} {'Trades':>7s} {'P&L':>10s} {'DD':>10s} "
          f"{'Sharpe':>7s} {'Calmar':>8s} {'WR':>6s} {'PF':>6s}")
    print("-" * 95)

    for combo in combos:
        label = combo.pop("label")
        t0 = time.time()
        result = run_single(engine, dates_df, pred_cache, combo)
        elapsed = time.time() - t0

        print(f"{label:>30s} {result['n_trades']:>7d} "
              f"${result['pnl']:>9,.0f} ${result['dd']:>9,.0f} "
              f"{result['sharpe']:>7.2f} {result['calmar']:>8.2f} "
              f"{result['wr']:>5.1%} {result['pf']:>6.2f} ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
