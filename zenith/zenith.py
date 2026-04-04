#!/usr/bin/env python3
"""Zenith Fade CCS — Entry Point.

Config constants live in zenith_config.py.
Engine (simulation, data, indicators) lives in zenith_engine.py.
Reporting/analysis lives in zenith_reporting.py.
Sweep runners live in zenith_sweeps.py.
"""
from __future__ import annotations

import os
import shutil
import sys

import pandas as pd

import zenith_config as _cfg
from zenith_config import *
from zenith_engine import (
    build_daily_indicators,
    run_backtest,
    logger,
    _DAILY_INDICATORS,
)
from zenith_reporting import (
    print_performance_report,
    print_vix_analysis,
    print_put_call_split,
    print_monthly_pnl,
    print_intra_month_drawdown,
    print_spy_comparison,
    print_worst_days,
    print_seasonality,
    print_econ_date_analysis,
    append_results_md,
    save_run_summary,
    display_run_history,
)


def run():
    """Main backtest entry point."""
    display_run_history()

    logger.info("=" * 60)
    logger.info("ZENITH FADE CCS BACKTEST")
    logger.info(f"Signal Mode  : {_cfg.SIGNAL_MODE}")
    logger.info(f"Period       : {_cfg.PILOT_YEAR_START} -> {_cfg.PILOT_YEAR_END}")
    logger.info(f"Score Thresh : {_cfg.SCORE_THRESHOLD}")
    logger.info(f"Weights      : var={_cfg.W_VAR}, vol={_cfg.W_VOL}, rocket={_cfg.W_ROCKET}")
    logger.info(f"Strike       : {_cfg.STRIKE_OFFSET}pt OTM, {_cfg.SPREAD_WIDTH}pt wide")
    logger.info(f"Risk         : ${_cfg.RISK_AMOUNT}, TP {_cfg.TP_CAP*100:.0f}%, SL {_cfg.SL_CAP*100:.0f}%")
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
    snapshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_zenith.csv")
    shutil.copy2(_cfg.SAVE_FILE, snapshot)
    logger.info(f"Trade log snapshot -> {snapshot}")

    # Generate reports
    date_range = pd.date_range(_cfg.PILOT_YEAR_START, _cfg.PILOT_YEAR_END, freq="B")
    m = print_performance_report(trades, date_range)
    append_results_md(trades, date_range)
    save_run_summary(trades)

    print_vix_analysis(trades)
    print_put_call_split(trades)
    print_monthly_pnl(trades)
    print_intra_month_drawdown(trades)
    print_spy_comparison(trades)
    print_worst_days(trades, n=30)
    print_seasonality(trades)
    print_econ_date_analysis(trades)


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="Zenith Fade CCS backtest")
    _parser.add_argument("--start",            default=None, help="Override PILOT_YEAR_START (YYYY-MM-DD)")
    _parser.add_argument("--end",              default=None, help="Override PILOT_YEAR_END (YYYY-MM-DD)")
    _parser.add_argument("--out",              default=None, help="Override output trade CSV path")
    _parser.add_argument("--mode",             default=None, choices=["classic", "dynamic"], help="Signal mode")
    _parser.add_argument("--score-threshold",  default=None, type=float, help="Override SCORE_THRESHOLD")
    _parser.add_argument("--w-var",            default=None, type=float, help="Override W_VAR weight")
    _parser.add_argument("--w-vol",            default=None, type=float, help="Override W_VOL weight")
    _parser.add_argument("--w-rocket",         default=None, type=float, help="Override W_ROCKET weight")
    _parser.add_argument("--strike-offset",    default=None, type=float, help="Override STRIKE_OFFSET (pts OTM)")
    _parser.add_argument("--width",            default=None, type=float, help="Override SPREAD_WIDTH")
    _parser.add_argument("--risk",             default=None, type=float, help="Override RISK_AMOUNT ($)")
    _parser.add_argument("--tp",               default=None, type=float, help="Override TP_CAP (0-1)")
    _parser.add_argument("--sl",               default=None, type=float, help="Override SL_CAP (negative)")
    _parser.add_argument("--marathon",         action="store_true",      help="Force single marathon run, skip sweeps")
    _parser.add_argument("--variance-thresh",  default=None, type=float, help="Override VARIANCE_THRESHOLD")
    _parser.add_argument("--rel-vol-thresh",   default=None, type=float, help="Override REL_VOL_THRESHOLD")
    _parser.add_argument("--rocket-thresh",    default=None, type=float, help="Override RISING_ROCKET_THRESHOLD")
    _parser.add_argument("--orb-max-width",    default=None, type=float, help="Enable ORB width filter (max width in pts)")
    _parser.add_argument("--orb-containment",  action="store_true",      help="Enable ORB containment filter (range-bound days only)")
    _parser.add_argument("--wpr-filter",       action="store_true",      help="Enable dual Williams %%R exhaustion filter (H2-WPR-1)")
    _args = _parser.parse_args()

    # Apply CLI overrides
    if _args.start:           _cfg.PILOT_YEAR_START    = _args.start
    if _args.end:             _cfg.PILOT_YEAR_END      = _args.end
    if _args.out:             _cfg.SAVE_FILE           = _args.out
    if _args.mode:            _cfg.SIGNAL_MODE         = _args.mode
    if _args.score_threshold is not None: _cfg.SCORE_THRESHOLD = _args.score_threshold
    if _args.w_var is not None:          _cfg.W_VAR           = _args.w_var
    if _args.w_vol is not None:          _cfg.W_VOL           = _args.w_vol
    if _args.w_rocket is not None:       _cfg.W_ROCKET        = _args.w_rocket
    if _args.strike_offset is not None:  _cfg.STRIKE_OFFSET   = _args.strike_offset
    if _args.width is not None:          _cfg.SPREAD_WIDTH    = _args.width
    if _args.risk is not None:           _cfg.RISK_AMOUNT     = _args.risk
    if _args.tp is not None:             _cfg.TP_CAP          = _args.tp
    if _args.sl is not None:             _cfg.SL_CAP          = _args.sl
    if _args.variance_thresh is not None:  _cfg.VARIANCE_THRESHOLD = _args.variance_thresh
    if _args.rel_vol_thresh is not None:   _cfg.REL_VOL_THRESHOLD  = _args.rel_vol_thresh
    if _args.rocket_thresh is not None:    _cfg.RISING_ROCKET_THRESHOLD = _args.rocket_thresh
    if _args.orb_max_width is not None:
        _cfg.USE_ORB_FILTER = True
        _cfg.ORB_MAX_WIDTH = _args.orb_max_width
    if _args.orb_containment:
        _cfg.USE_ORB_CONTAINMENT_FILTER = True
    if _args.wpr_filter:
        _cfg.USE_WPR_FILTER = True

    # Dispatch
    if _args.marathon or not any([
        RUN_SCORE_THRESHOLD_SWEEP, RUN_WEIGHT_SWEEP, RUN_STRIKE_OFFSET_SWEEP,
        RUN_WIDTH_SWEEP, RUN_FILTER_SWEEP, RUN_TP_SL_SWEEP,
        RUN_RISK_AMOUNT_SWEEP, RUN_VARIANCE_SWEEP, RUN_REL_VOL_SWEEP,
        RUN_ROCKET_SWEEP, RUN_VIX_FILTER_SWEEP,
    ]):
        run()
    else:
        from zenith_sweeps import dispatch_sweep
        dispatch_sweep()
