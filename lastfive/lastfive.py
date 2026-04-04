#!/usr/bin/env python3
"""LastFive — Entry Point.

Config constants live in lastfive_config.py.
Engine (simulation, data) lives in lastfive_engine.py.
Reporting/analysis lives in lastfive_reporting.py.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

import pandas as pd

import lastfive_config as _cfg
from lastfive_config import *
from lastfive_engine import (
    build_daily_indicators,
    run_backtest,
    logger,
    _DAILY_INDICATORS,
)
from lastfive_reporting import (
    print_performance_report,
    print_vix_analysis,
    print_put_call_split,
    print_monthly_pnl,
    print_monthly_vs_spy,
    print_worst_days,
    print_seasonality_dow,
    print_seasonality_month,
    print_econ_event_analysis,
    append_results_md,
    save_run_summary,
    display_run_history,
)


def run():
    """Main backtest entry point."""
    display_run_history()

    logger.info("=" * 60)
    logger.info("LASTFIVE BACKTEST — Short Call Spread 5 Min Before Close")
    logger.info(f"Period       : {_cfg.PILOT_YEAR_START} -> {_cfg.PILOT_YEAR_END}")
    logger.info(f"Sides        : {'CALL' if _cfg.ENABLE_CALL_SIDE else ''}{'+PUT' if _cfg.ENABLE_PUT_SIDE else ''}")
    logger.info(f"VIX adaptive : {'ON' if _cfg.ENABLE_VIX_ADAPTIVE else 'OFF'} (ATM<{_cfg.VIX_ATM_CUTOFF}, d3<{_cfg.VIX_MID_CUTOFF}, d5<{_cfg.VIX_WIDE_CUTOFF})")
    logger.info(f"Spread width : ${_cfg.SPREAD_WIDTH}")
    logger.info(f"Tiered sizing: {'ON' if _cfg.ENABLE_TIERED_SIZING else 'OFF'} (${_cfg.RISK_LOW}/${_cfg.RISK_MID}/${_cfg.RISK_HIGH})")
    logger.info(f"Min credit   : ${_cfg.MIN_NET_CREDIT}")
    logger.info(f"Put filter   : aftn<{_cfg.PUT_AFTERNOON_MIN}% + mom30<{_cfg.PUT_MOM30_MIN}% ({'ON' if _cfg.ENABLE_PUT_MOMENTUM_FILTER else 'OFF'})")
    logger.info(f"Stop-loss    : buy back if SPX moves ${_cfg.STOP_THRESHOLD}+ at {','.join(t[:5] for t in _cfg.STOP_CHECK_TIMES)} ({'ON' if _cfg.ENABLE_PROTECTIVE_BUYBACK else 'OFF'})")
    logger.info(f"Afternoon    : < {_cfg.AFTERNOON_RETURN_MAX}% ({'ON' if _cfg.ENABLE_AFTERNOON_FILTER else 'OFF'})")
    logger.info(f"VIX(15:50)   : <= {_cfg.VIX_INTRADAY_MAX} ({'ON' if _cfg.ENABLE_VIX_INTRADAY_FILTER else 'OFF'})")
    logger.info("=" * 60)

    # Build daily indicators
    logger.info("Building daily indicators...")
    indicators = build_daily_indicators()
    logger.info(f"  -> {len(indicators)} daily bars loaded")

    # Run backtest
    trades = run_backtest(indicators=indicators)

    if not trades:
        logger.warning("No trades recorded.")
        return

    # Save trade log snapshot
    snapshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_lastfive.csv")
    shutil.copy2(_cfg.SAVE_FILE, snapshot)
    logger.info(f"Trade log snapshot -> {snapshot}")

    # Generate reports
    date_range = pd.date_range(_cfg.PILOT_YEAR_START, _cfg.PILOT_YEAR_END, freq="B")
    m = print_performance_report(trades, date_range)
    append_results_md(trades, date_range)
    save_run_summary(trades)

    if RUN_VIX_ANALYSIS:
        print_vix_analysis(trades)
    print_put_call_split(trades)
    if RUN_MONTHLY_PNL:
        print_monthly_pnl(trades)
        print_monthly_vs_spy(trades)
    if RUN_WORST_DAYS:
        print_worst_days(trades, n=30)
    print_seasonality_dow(trades)
    print_seasonality_month(trades)
    print_econ_event_analysis(trades)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, help="Override start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="Override end date YYYY-MM-DD")
    parser.add_argument("--width", type=float, help="Spread width (default 5)")
    parser.add_argument("--vix-term-max", type=float, help="Max VIX/VIX9D ratio (backwardation filter)")
    parser.add_argument("--vvix-max", type=float, help="Max VVIX level (crash kill switch)")
    parser.add_argument("--wvf-bb", action="store_true", help="Enable WVF Bollinger Band filter")
    parser.add_argument("--wvf-pct", type=float, help="Enable WVF percentile filter (max percentile)")
    parser.add_argument("--vix16", type=float, help="Enable VIX/16 continuous distance (multiplier, e.g. 1.5)")
    parser.add_argument("--vix16-hybrid", type=float, help="Enable hybrid mode: buckets + VIX/16 floor (multiplier)")
    parser.add_argument("--spread-compression", type=float, help="Enable spread compression filter (skip if bar range percentile > this)")
    parser.add_argument("--parkinson-max", type=float, help="Enable Parkinson vol filter (skip if annualized vol > this %%)")
    parser.add_argument("--er-max", type=float, help="Enable Kaufman ER filter (skip if ER > this, e.g. 0.65)")
    parser.add_argument("--range-budget-min", type=float, help="Enable range budget filter (skip if budget_used < this, e.g. 0.7)")
    parser.add_argument("--vix16-min", type=float, help="Min distance floor for VIX/16 mode")
    args = parser.parse_args()

    if args.start:
        _cfg.PILOT_YEAR_START = args.start
    if args.end:
        _cfg.PILOT_YEAR_END = args.end
    if args.width:
        _cfg.SPREAD_WIDTH = args.width
    if args.vix_term_max:
        _cfg.ENABLE_VIX_TERM_FILTER = True
        _cfg.VIX_TERM_MAX_RATIO = args.vix_term_max
    if args.vvix_max:
        _cfg.ENABLE_VVIX_FILTER = True
        _cfg.VVIX_MAX = args.vvix_max
    if args.wvf_bb:
        _cfg.ENABLE_WVF_FILTER = True
        _cfg.WVF_MODE = "bb"
    if args.wvf_pct:
        _cfg.ENABLE_WVF_FILTER = True
        _cfg.WVF_MODE = "percentile"
        _cfg.WVF_PERCENTILE_MAX = args.wvf_pct
    if args.vix16:
        _cfg.VIX_ADAPTIVE_MODE = "vix16"
        _cfg.VIX16_MULTIPLIER = args.vix16
    if args.vix16_hybrid:
        _cfg.VIX_ADAPTIVE_MODE = "hybrid"
        _cfg.VIX16_MULTIPLIER = args.vix16_hybrid
    if args.spread_compression:
        _cfg.ENABLE_SPREAD_COMPRESSION_FILTER = True
        _cfg.SPREAD_COMPRESSION_SKIP_PCTILE = args.spread_compression
    if args.parkinson_max:
        _cfg.ENABLE_PARKINSON_FILTER = True
        _cfg.PARKINSON_MAX = args.parkinson_max
    if args.er_max:
        _cfg.ENABLE_ER_FILTER = True
        _cfg.ER_MAX = args.er_max
    if args.range_budget_min:
        _cfg.ENABLE_RANGE_BUDGET_FILTER = True
        _cfg.RANGE_BUDGET_MIN = args.range_budget_min
    if args.vix16_min:
        _cfg.VIX16_MIN_DIST = args.vix16_min

    run()
