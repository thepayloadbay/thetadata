#!/usr/bin/env python3
"""SimCondor Round 2 Sweep — stack improvements on the optimized baseline.

Baseline: width=20, qty=2, strike_mult=1.0, min_otm=30, no SL, min_credit=0.40, CALM only
"""
from __future__ import annotations

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

BACKTEST_START = "2022-06-01"
BACKTEST_END = "2026-03-25"


def run_single(engine, dates, pred_cache, config: dict) -> dict:
    """Run a single backtest with overridden config."""
    simcondor.WIDTH = config.get("width", 20)
    simcondor.QTY = config.get("qty", 2)
    simcondor.STRIKE_MULTIPLIER = config.get("strike_mult", 1.0)
    simcondor.MIN_OTM_DISTANCE = config.get("min_otm", 30)
    simcondor.MAX_OTM_DISTANCE = config.get("max_otm", 100)
    simcondor.STOP_LOSS_PER_SIDE = config.get("sl", -99999)
    simcondor.MIN_CREDIT_PER_SIDE = config.get("min_credit", 0.40)
    simcondor.ENTRY_TIME = config.get("entry_time", dt_time(9, 45))
    simcondor.TRADE_REGIMES = config.get("regimes", {"CALM"})
    simcondor.SIMSEARCH_TOP_K = config.get("top_k", 20)
    simcondor.VIX_MAX = config.get("vix_max", None)
    simcondor.VIX_MIN = config.get("vix_min", None)
    simcondor.DOW_SKIP = config.get("dow_skip", set())
    simcondor.PREV_RANGE_MAX = config.get("prev_range_max", None)
    simcondor.MIN_SIMILARITY = config.get("min_sim", None)
    simcondor.DANGER_PROB_MAX = config.get("danger_max", None)
    simcondor.PUT_CALL_BIAS = config.get("pc_bias", 0.0)

    results = []
    for ds in dates:
        result = simcondor.backtest_day(ds, engine, pred_cache)
        if result is not None:
            results.append(result)

    df = pd.DataFrame(results)
    trades = df[df["action"] == "TRADE"] if len(df) > 0 else pd.DataFrame()

    if len(trades) == 0:
        return {"n": 0, "pnl": 0, "sharpe": 0, "dd": 0, "wr": 0, "calmar": 0, "pf": 0, "worst": 0}

    pnl = trades["pnl"].sum()
    cum = trades["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()
    sharpe = trades["pnl"].mean() / trades["pnl"].std() * np.sqrt(252) if trades["pnl"].std() > 0 else 0
    calmar = pnl / abs(dd) if dd < 0 else 999
    wr = (trades["pnl"] > 0).mean()
    pf = (trades[trades["pnl"] > 0]["pnl"].sum() / abs(trades[trades["pnl"] <= 0]["pnl"].sum())
          if (trades["pnl"] <= 0).any() and trades[trades["pnl"] <= 0]["pnl"].sum() < 0 else 999)

    return {
        "n": len(trades), "pnl": pnl, "dd": dd, "sharpe": sharpe,
        "calmar": calmar, "wr": wr, "pf": pf, "worst": trades["pnl"].min(),
    }


def fmt(label, r, elapsed=0):
    return (f"{label:>35s} {r['n']:>4d} "
            f"${r['pnl']:>9,.0f} ${r['dd']:>8,.0f} "
            f"{r['sharpe']:>6.2f} {r['calmar']:>7.2f} "
            f"{r['wr']:>5.1%} {r['pf']:>5.2f} "
            f"${r['worst']:>7,.0f} ({elapsed:.0f}s)")


def main():
    features = load_features()
    engine = SimSearchEngine(features)
    dates = features[
        (features["date"] >= pd.Timestamp(BACKTEST_START)) &
        (features["date"] <= pd.Timestamp(BACKTEST_END))
    ]["date"].dt.strftime("%Y%m%d").tolist()

    print("Pre-computing predictions...")
    pred_cache = {}
    for ds in dates:
        pred_cache[ds] = predict_regime(engine, ds, top_k=20)
    print(f"Cached {len(pred_cache)}")

    baseline = {
        "width": 20, "qty": 2, "strike_mult": 1.0, "min_otm": 30,
        "max_otm": 100, "sl": -99999, "min_credit": 0.40,
        "entry_time": dt_time(9, 45), "regimes": {"CALM"}, "top_k": 20,
    }

    hdr = (f"{'Config':>35s} {'N':>4s} {'P&L':>10s} {'DD':>9s} "
           f"{'Shp':>6s} {'Calmar':>7s} {'WR':>5s} {'PF':>5s} {'Worst':>8s}")

    print("\n" + "=" * 110)
    print("SimCondor Round 2 — Stacking Improvements")
    print("=" * 110)

    # Baseline
    t0 = time.time()
    base = run_single(engine, dates, pred_cache, baseline)
    print(hdr)
    print("-" * 110)
    print(fmt("BASELINE (noSL, mc0.40, CALM)", base, time.time() - t0))

    # ---- Round 2a: Individual filters ----
    sweeps = [
        # Width combos with no SL
        ("Width=25, noSL", {**baseline, "width": 25}),
        ("Width=30, noSL", {**baseline, "width": 30}),
        # QTY
        ("QTY=3, noSL", {**baseline, "qty": 3}),
        # VIX filters
        ("VIX<20", {**baseline, "vix_max": 20}),
        ("VIX<18", {**baseline, "vix_max": 18}),
        ("VIX<16", {**baseline, "vix_max": 16}),
        ("VIX 12-18", {**baseline, "vix_min": 12, "vix_max": 18}),
        # DOW
        ("Skip Mon", {**baseline, "dow_skip": {0}}),
        ("Skip Fri", {**baseline, "dow_skip": {4}}),
        ("Skip Mon+Fri", {**baseline, "dow_skip": {0, 4}}),
        ("Tue-Thu only", {**baseline, "dow_skip": {0, 4}}),
        # Prior day range
        ("PrevRange<1.0%", {**baseline, "prev_range_max": 0.010}),
        ("PrevRange<0.8%", {**baseline, "prev_range_max": 0.008}),
        ("PrevRange<0.6%", {**baseline, "prev_range_max": 0.006}),
        # Similarity
        ("MinSim>0.70", {**baseline, "min_sim": 0.70}),
        ("MinSim>0.75", {**baseline, "min_sim": 0.75}),
        # Danger prob
        ("DangerProb<10%", {**baseline, "danger_max": 0.10}),
        ("DangerProb<5%", {**baseline, "danger_max": 0.05}),
        # Asymmetric
        ("PutBias+10 (puts wider)", {**baseline, "pc_bias": 10}),
        ("PutBias+15", {**baseline, "pc_bias": 15}),
        ("CallBias+10 (calls wider)", {**baseline, "pc_bias": -10}),
        # Min credit higher
        ("MinCredit 0.50, noSL", {**baseline, "min_credit": 0.50}),
        ("MinCredit 0.60, noSL", {**baseline, "min_credit": 0.60}),
    ]

    print("\n--- Round 2a: Individual filters ---")
    print(hdr)
    print("-" * 110)
    for label, config in sweeps:
        t0 = time.time()
        r = run_single(engine, dates, pred_cache, config)
        print(fmt(label, r, time.time() - t0))

    # ---- Round 2b: Best combos ----
    print("\n--- Round 2b: Stacked combinations ---")
    print(hdr)
    print("-" * 110)

    combos = [
        ("W25+VIX<18", {**baseline, "width": 25, "vix_max": 18}),
        ("W25+PrevRange<1%", {**baseline, "width": 25, "prev_range_max": 0.010}),
        ("W30+VIX<20", {**baseline, "width": 30, "vix_max": 20}),
        ("W25+SkipFri", {**baseline, "width": 25, "dow_skip": {4}}),
        ("VIX<18+PrevRange<1%", {**baseline, "vix_max": 18, "prev_range_max": 0.010}),
        ("VIX<18+Danger<10%", {**baseline, "vix_max": 18, "danger_max": 0.10}),
        ("W25+VIX<18+PutBias10", {**baseline, "width": 25, "vix_max": 18, "pc_bias": 10}),
        ("W25+VIX<18+PrevRange<1%", {**baseline, "width": 25, "vix_max": 18, "prev_range_max": 0.010}),
        ("W30+VIX<18+mc0.50", {**baseline, "width": 30, "vix_max": 18, "min_credit": 0.50}),
        ("W25+VIX<20+QTY3", {**baseline, "width": 25, "vix_max": 20, "qty": 3}),
        ("W25+VIX<18+SkipFri+PR<1%", {**baseline, "width": 25, "vix_max": 18, "dow_skip": {4}, "prev_range_max": 0.010}),
    ]

    for label, config in combos:
        t0 = time.time()
        r = run_single(engine, dates, pred_cache, config)
        print(fmt(label, r, time.time() - t0))


if __name__ == "__main__":
    main()
