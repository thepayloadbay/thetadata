#!/usr/bin/env python3
"""MEDS strategy — entry point.

Config constants live in meds_config.py.
Engine (simulation, data, indicators) lives in meds_engine.py.
Reporting/analysis lives in meds_reporting.py.
Sweep runners live in meds_sweeps.py.
meds_core.py is a thin re-exporter for backwards compatibility.
This file contains run(), CLI parsing, and the dispatch table.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import sys

import pandas as pd

# ---------------------------------------------------------------------------
#  Config constants + engine functions.
#  CLI-mutable vars are accessed via _cfg.VAR to pick up runtime overrides.
# ---------------------------------------------------------------------------
import meds_config as _cfg
from meds_config import *
from meds_config import _build_calendar_event_dates

from meds_engine import *
from meds_engine import (
    _build_daily_indicators,
    _DAILY_INDICATORS,
    _EOM_DATES,
    _CPI_DATES,
    _PCE_DATES,
    _NFP_DATES,
    _EOQ_DATES,
    _PRE_TW_DATES,
    _POST_HOL_DATES,
    _get_session,
)

# Reporting functions (extracted to meds_reporting.py)
from meds_reporting import *
from meds_reporting import _display_run_history, _save_run_summary

# Module-level state (populated in __main__)
_CALENDAR_SKIP_DATES: set[str] = set()


# ---------------------------------------------
#  MAIN RUNNER
# ---------------------------------------------
def load_existing_trades() -> tuple[list, str | None]:
    """Read SAVE_FILE and return (trades, last_processed_date_str).
    last_processed_date_str is the max entry_date found (YYYYMMDD), or None."""
    trades = []
    try:
        with open(_cfg.SAVE_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                row["pnl_earned"] = float(row["pnl_earned"]) if row.get("pnl_earned") else 0.0
                row["win"]        = int(row["win"])  if row.get("win")  else 0
                row["loss"]       = int(row["loss"]) if row.get("loss") else 0
                trades.append(row)
    except FileNotFoundError:
        return [], None
    dates = [t["entry_date"] for t in trades if t.get("entry_date")]
    last_date = max(dates) if dates else None
    return trades, last_date

def is_portfolio_under_pressure(active_positions, current_spot, threshold_pct=0.50):
    """
    Checks if any open position has an unrealized loss exceeding threshold_pct
    of the credit received.
    """
    if not active_positions:
        return False

    for pos in active_positions:
        short_strike = pos['short_strike']
        side = pos['option_type']

        distance = current_spot - short_strike if side == 'PUT' else short_strike - current_spot

        if distance < 15.0:
            return True

    return False

async def run():
    date_list = pd.date_range(_cfg.PILOT_YEAR_START, _cfg.PILOT_YEAR_END, freq='B')

    # -- Resume or new run? --
    resume_from = None
    if os.path.exists(_cfg.SAVE_FILE):
        print(f"\nFound existing log: {_cfg.SAVE_FILE}")
        if sys.stdin.isatty():
            choice = input("Resume where you left off? [y/n]: ").strip().lower()
        else:
            choice = "n"  # non-interactive (background/sweep) -- always start fresh
            print("Non-interactive mode -- starting fresh.")
        if choice == "y":
            existing_trades, resume_from = load_existing_trades()
            print(f"Resuming -- {len(existing_trades)} existing trades loaded"
                  + (f", skipping up to {resume_from}." if resume_from else "."))
        else:
            existing_trades = []
            init_log_file()
            print("Starting fresh -- existing log overwritten.")
    else:
        existing_trades = []
        init_log_file()

    all_trades = list(existing_trades)
    total_pnl  = sum(t["pnl_earned"] for t in all_trades)
    total_w    = sum(t["win"]  for t in all_trades)
    total_l    = sum(t["loss"] for t in all_trades)

    _display_run_history()

    logger.info("=" * 60)
    logger.info("MEFT v35: BID/ASK DRIVEN MARATHON")
    logger.info(f"Trades  -> {_cfg.SAVE_FILE}")
    logger.info(f"Log     -> {_cfg.LOG_FILE}")
    logger.info("=" * 60)
    print_settings_summary()

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue
            if d_str in _CALENDAR_SKIP_DATES:
                logger.info(f"Skipping {d_str} -- calendar filter ({', '.join(sorted(CALENDAR_FILTER_EVENTS))})")
                continue
            if resume_from and d_str <= resume_from:
                logger.info(f"Skipping {d_str} -- already processed (resume)")
                continue
            trades, pnl = await process_day(session, d_str)

            if trades:
                all_trades.extend(trades)
                total_pnl += pnl
                total_w   += sum(t["win"]  for t in trades)
                total_l   += sum(t["loss"] for t in trades)

            total_trades = total_w + total_l
            wr = (total_w / total_trades * 100) if total_trades > 0 else 0.0
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} | Day P&L: ${pnl:>7,.0f} | Total: ${total_pnl:>9,.0f} | WR: {wr:.1f}%")

    if not all_trades:
        logger.warning("No trades recorded.")
        return

    logger.info(f"DONE -- {len(all_trades)} trades logged to {_cfg.SAVE_FILE}")
    _snapshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_meds.csv")
    shutil.copy2(_cfg.SAVE_FILE, _snapshot)
    logger.info(f"Trade log snapshot -> {_snapshot}")
    print_performance_report(all_trades, date_list)
    append_results_md(all_trades, date_list)
    print_dynamic_sl_vix_analysis(all_trades)
    print_monthly_pnl_table(all_trades)
    print_spy_comparison(all_trades)
    print_large_loss_days(all_trades)
    print_entry_time_analysis(all_trades)
    print_seasonality_analysis(all_trades)
    print_econ_date_analysis(all_trades)
    _save_run_summary(all_trades, date_list)
    print_vix_analysis(all_trades)
    if RUN_STRIKE_DISTANCE_ANALYSIS:
        print_strike_distance_analysis(all_trades)
    if RUN_PUT_CALL_ANALYSIS:
        print_put_call_analysis(all_trades)


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="MEFT v35 backtest")
    _parser.add_argument("--start",           default=None, help="Override PILOT_YEAR_START (YYYY-MM-DD)")
    _parser.add_argument("--end",             default=None, help="Override PILOT_YEAR_END   (YYYY-MM-DD)")
    _parser.add_argument("--out",             default=None, help="Override output trade CSV path")
    _parser.add_argument("--sl-vix-mid-low",   default=None, type=float, help="Override DYNAMIC_SL_VIX_MID lower bound")
    _parser.add_argument("--sl-vix-mid-high",  default=None, type=float, help="Override DYNAMIC_SL_VIX_MID upper bound")
    _parser.add_argument("--skip-vix-lo",      default=None, type=float, help="Skip day entirely when VIX >= this value (lower bound of SKIP_VIX_RANGE)")
    _parser.add_argument("--skip-vix-hi",      default=None, type=float, help="Skip day entirely when VIX <= this value (upper bound of SKIP_VIX_RANGE)")
    _parser.add_argument("--entry-gate",        default=None, type=float, help="Set INTRADAY_ENTRY_GATE threshold (e.g. 0 = stop new entries when open positions show any loss)")
    _parser.add_argument("--buyback-exit",      default=None, type=float, help="Close position early when remaining premium <= this value (e.g. 0.10)")
    _parser.add_argument("--marathon",         action="store_true",      help="Force a single marathon backtest run, skipping all RUN_* sweeps")
    _parser.add_argument("--kelly",            action="store_true",      help="Enable Kelly zone sizing (ENABLE_KELLY_SIZING=True)")
    _parser.add_argument("--min-otm-distance", default=None, type=float, help="Override MIN_OTM_DISTANCE (pts)")
    _parser.add_argument("--max-credit",       default=None, type=float, help="Override MAX_NET_CREDIT cap")
    _parser.add_argument("--fomc-vix1520-sl",  default=None, type=float, help="Enable FOMC VIX 15-20 SL at this amount (e.g. -800)")
    _parser.add_argument("--econ-vix-skip",    action="store_true",      help="Enable VIX-conditional econ day skips")
    _args = _parser.parse_args()

    # -- Apply CLI overrides to meds_core (before importing sweeps) --
    if _args.start:           _cfg.PILOT_YEAR_START    = _args.start
    if _args.end:             _cfg.PILOT_YEAR_END      = _args.end
    if _args.out:             _cfg.SAVE_FILE           = _args.out
    if _args.sl_vix_mid_low is not None and _args.sl_vix_mid_high is not None:
        _cfg.DYNAMIC_SL_VIX_MID = (_args.sl_vix_mid_low, _args.sl_vix_mid_high)
    if _args.skip_vix_lo is not None and _args.skip_vix_hi is not None:
        _cfg.SKIP_VIX_RANGE = (_args.skip_vix_lo, _args.skip_vix_hi)
    if _args.kelly:
        _cfg.ENABLE_KELLY_SIZING = True
    if _args.entry_gate is not None:
        _cfg.INTRADAY_ENTRY_GATE = _args.entry_gate
    if _args.buyback_exit is not None:
        _cfg.PREMIUM_BUYBACK_EXIT = _args.buyback_exit
    if _args.min_otm_distance is not None:
        _cfg.MIN_OTM_DISTANCE = _args.min_otm_distance
    if _args.max_credit is not None:
        _cfg.MAX_NET_CREDIT = _args.max_credit
    if _args.fomc_vix1520_sl is not None:
        _cfg.ENABLE_FOMC_VIX1520_SL = True
        _cfg.FOMC_VIX1520_SL_AMOUNT = _args.fomc_vix1520_sl
    if _args.econ_vix_skip:
        _cfg.ENABLE_ECON_VIX_SKIP = True

    # Build EOM date set (used by EOM SL and EOM SL sweep)
    _cal_event_sets_startup = _build_calendar_event_dates()
    _EOM_DATES.update(_cal_event_sets_startup.get("end_of_month", set()))
    _CPI_DATES.update(_cal_event_sets_startup.get("cpi", set()))
    _PCE_DATES.update(_cal_event_sets_startup.get("pce", set()))
    _NFP_DATES.update(_cal_event_sets_startup.get("nfp", set()))
    _EOQ_DATES.update(_cal_event_sets_startup.get("end_of_quarter", set()))
    _PRE_TW_DATES.update(_cal_event_sets_startup.get("pre_triple_witching", set()))
    _POST_HOL_DATES.update(_cal_event_sets_startup.get("post_major_holiday", set()))

    # Build calendar skip set (PCE + End-of-Month hard skip)
    if ENABLE_CALENDAR_FILTER and CALENDAR_FILTER_EVENTS:
        for _ev in CALENDAR_FILTER_EVENTS:
            _CALENDAR_SKIP_DATES |= _cal_event_sets_startup.get(_ev, set())
        logger.info(
            f"Calendar filter active: {sorted(CALENDAR_FILTER_EVENTS)} -- "
            f"{len(_CALENDAR_SKIP_DATES)} dates will be hard-skipped"
        )

    # Build daily indicators: always load basic (VIX change, gap%) for direction signal;
    # only compute full technical indicators (RSI, ADX, MACD, etc.) when day filters are active.
    _needs_full_indicators = any([
        DAY_FILTER_VARPC_MIN, DAY_FILTER_RSI_MIN, DAY_FILTER_RSI_MAX,
        DAY_FILTER_DIST_SMA_MIN, DAY_FILTER_ATR_MAX, DAY_FILTER_BODY_MAX,
        DAY_FILTER_KNIFE_MAX, DAY_FILTER_STOCH_MIN, DAY_FILTER_GAP_MAX,
        DAY_FILTER_ABOVE_SMA5, DAY_FILTER_ABOVE_SMA200,
        DAY_FILTER_ADX_MIN, DAY_FILTER_RANGE_MAX, DAY_FILTER_RANGE_MIN,
        DAY_FILTER_EXP_MOVE_MAX, DAY_FILTER_EXP_MOVE_MIN,
        DAY_FILTER_CCI_MAX, DAY_FILTER_IVR_MIN,
        DAY_FILTER_SKIP_VIX_RISE_DECEL,
        RUN_DAY_FILTER_SWEEP,
    ])
    _needs_indicators = DIRECTION_MODE == "vix_change" or _needs_full_indicators
    if _needs_indicators:
        _mode = "full" if _needs_full_indicators else "basic"
        logger.info(f"Building daily indicators ({_mode} mode)...")
        _DAILY_INDICATORS.update(_build_daily_indicators(compute_full=_needs_full_indicators))
        logger.info(f"  -> {len(_DAILY_INDICATORS)} daily bars loaded")

    load_quote_disk_cache()

    # -- Dispatch: import sweep functions lazily (after CLI overrides are applied) --
    if _args.marathon:
        asyncio.run(run())
    elif RUN_MAX_BP_SWEEP:
        from meds_sweeps import run_max_bp_sweep
        asyncio.run(run_max_bp_sweep())
    elif RUN_TOUCH_SWEEP:
        from meds_sweeps import run_touch_sweep
        asyncio.run(run_touch_sweep())
    elif RUN_DAY_FILTER_SWEEP:
        from meds_sweeps import run_day_filter_sweep
        asyncio.run(run_day_filter_sweep())
    elif RUN_DAILY_TP_SWEEP:
        from meds_sweeps import run_daily_tp_sweep
        asyncio.run(run_daily_tp_sweep())
    elif RUN_PNL_SAMPLE_SWEEP:
        from meds_sweeps import run_pnl_sample_sweep
        asyncio.run(run_pnl_sample_sweep())
    elif RUN_ENTRY_TIME_SWEEP:
        from meds_sweeps import run_entry_time_sweep
        asyncio.run(run_entry_time_sweep())
    elif RUN_DIRECTION_TIME_SWEEP:
        from meds_sweeps import run_direction_time_sweep
        asyncio.run(run_direction_time_sweep())
    elif RUN_BIAS_SWEEP:
        from meds_sweeps import run_bias_sweep
        asyncio.run(run_bias_sweep())
    elif RUN_COMBO_SWEEP:
        from meds_sweeps import run_combo_sweep
        asyncio.run(run_combo_sweep())
    elif RUN_MASTER_SWEEP:
        from meds_sweeps import run_master_sweep
        asyncio.run(run_master_sweep())
    elif RUN_POS_TRAIL_SWEEP:
        from meds_sweeps import run_pos_trail_sweep
        asyncio.run(run_pos_trail_sweep())
    elif RUN_MIN_CREDIT_SWEEP:
        from meds_sweeps import run_min_credit_sweep
        asyncio.run(run_min_credit_sweep())
    elif RUN_MIN_OTM_SWEEP:
        from meds_sweeps import run_min_otm_sweep
        asyncio.run(run_min_otm_sweep())
    elif RUN_MAX_CREDIT_SWEEP:
        from meds_sweeps import run_max_credit_sweep
        asyncio.run(run_max_credit_sweep())
    elif RUN_CALENDAR_SWEEP:
        from meds_sweeps import run_calendar_event_sweep
        asyncio.run(run_calendar_event_sweep())
    elif RUN_CALL_SL_SWEEP:
        from meds_sweeps import run_call_sl_sweep
        asyncio.run(run_call_sl_sweep())
    elif RUN_VIX_MAG_SWEEP:
        from meds_sweeps import run_vix_mag_sweep
        asyncio.run(run_vix_mag_sweep())
    elif RUN_GAP_CALL_SL_SWEEP:
        from meds_sweeps import run_gap_call_sl_sweep
        asyncio.run(run_gap_call_sl_sweep())
    elif RUN_VIX_SUB12_SL_SWEEP:
        from meds_sweeps import run_vix_sub12_sl_sweep
        asyncio.run(run_vix_sub12_sl_sweep())
    elif RUN_EOM_SL_SWEEP:
        from meds_sweeps import run_eom_sl_sweep
        asyncio.run(run_eom_sl_sweep())
    elif RUN_PER_POS_SL_SWEEP:
        from meds_sweeps import run_per_pos_sl_sweep
        asyncio.run(run_per_pos_sl_sweep())
    elif RUN_SL_GAP_SWEEP:
        from meds_sweeps import run_sl_gap_sweep
        asyncio.run(run_sl_gap_sweep())
    elif RUN_VIX_ENTRY_CUTOFF_SWEEP:
        from meds_sweeps import run_vix_entry_cutoff_sweep
        asyncio.run(run_vix_entry_cutoff_sweep())
    elif RUN_CALENDAR_RISK_SL_SWEEP:
        from meds_sweeps import run_calendar_risk_sl_sweep
        asyncio.run(run_calendar_risk_sl_sweep())
    elif RUN_PRESSURE_VIX_SWEEP:
        from meds_sweeps import run_pressure_vix_sweep
        asyncio.run(run_pressure_vix_sweep())
    elif RUN_SPREAD_WIDTH_SWEEP:
        from meds_sweeps import run_spread_width_sweep
        asyncio.run(run_spread_width_sweep())
    elif RUN_TRAILING_STOP_SWEEP:
        from meds_sweeps import run_trailing_stop_sweep
        asyncio.run(run_trailing_stop_sweep())
    elif RUN_EMA_SWEEP:
        from meds_sweeps import run_ema_sweep
        asyncio.run(run_ema_sweep())
    elif RUN_SL_SWEEP:
        from meds_sweeps import run_sweep
        asyncio.run(run_sweep())
    else:
        asyncio.run(run())
        if RUN_BASELINE_COMPARISON:
            from meds_sweeps import run_baseline_comparison
            asyncio.run(run_baseline_comparison())

    save_quote_disk_cache()
