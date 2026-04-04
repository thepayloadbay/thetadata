#!/usr/bin/env python3
"""MEIC strategy — entry point.

Afternoon SPX credit spreads, both PUT + CALL independently.
Config in meic_config.py, engine in meic_engine.py, reporting in meic_reporting.py.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import sys

import pandas as pd

import meic_config as _cfg
from meic_config import *
from meic_config import _build_calendar_event_dates

from meic_engine import *
from meic_engine import (
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

from meic_reporting import *


# ---------------------------------------------
#  MAIN RUNNER
# ---------------------------------------------
def load_existing_trades() -> tuple[list, str | None]:
    trades = []
    try:
        with open(_cfg.SAVE_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                row["pnl_earned"] = float(row["pnl_earned"]) if row.get("pnl_earned") else 0.0
                row["win"] = int(row["win"]) if row.get("win") else 0
                row["loss"] = int(row["loss"]) if row.get("loss") else 0
                trades.append(row)
    except FileNotFoundError:
        return [], None
    dates = [t["entry_date"] for t in trades if t.get("entry_date")]
    last_date = max(dates) if dates else None
    return trades, last_date


async def run():
    date_list = pd.date_range(_cfg.PILOT_YEAR_START, _cfg.PILOT_YEAR_END, freq='B')

    # Always start fresh for backtest
    init_log_file()
    all_trades = []
    total_pnl = 0.0
    total_w = 0
    total_l = 0
    _skip_next_day = False  # L5: post-loss cooldown flag

    logger.info("=" * 60)
    logger.info("MEIC: Afternoon SPX Credit Spreads")
    logger.info(f"Trades  -> {_cfg.SAVE_FILE}")
    logger.info(f"Log     -> {_cfg.LOG_FILE}")
    logger.info("=" * 60)
    print_settings_summary()

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            # L1: Thursday skip
            if _cfg.ENABLE_THURSDAY_SKIP and d.weekday() == 3:
                logger.info(f"Skipping {d_str} -- Thursday skip")
                continue
            # L5: Post-loss cooldown
            if _cfg.ENABLE_POST_LOSS_COOLDOWN and _skip_next_day:
                logger.info(f"Skipping {d_str} -- post-loss cooldown")
                _skip_next_day = False
                continue
            trades, pnl = await process_day(session, d_str)

            if trades:
                all_trades.extend(trades)
                total_pnl += pnl
                total_w += sum(t["win"] for t in trades)
                total_l += sum(t["loss"] for t in trades)

            total_trades = total_w + total_l
            wr = (total_w / total_trades * 100) if total_trades > 0 else 0.0
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} | Day P&L: ${pnl:>7,.0f} | Total: ${total_pnl:>9,.0f} | WR: {wr:.1f}%")

            # L5: Check if this day triggers cooldown
            if _cfg.ENABLE_POST_LOSS_COOLDOWN and pnl <= _cfg.POST_LOSS_THRESHOLD:
                _skip_next_day = True

    if not all_trades:
        logger.warning("No trades recorded.")
        return

    logger.info(f"DONE -- {len(all_trades)} trades logged to {_cfg.SAVE_FILE}")
    _snapshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_meic.csv")
    shutil.copy2(_cfg.SAVE_FILE, _snapshot)

    print_performance_report(all_trades, date_list)
    append_results_md(all_trades, date_list)
    print_monthly_pnl_table(all_trades)
    print_large_loss_days(all_trades)
    print_entry_time_analysis(all_trades)
    print_econ_date_analysis(all_trades)
    print_vix_analysis(all_trades)
    if RUN_STRIKE_DISTANCE_ANALYSIS:
        print_strike_distance_analysis(all_trades)
    if RUN_PUT_CALL_ANALYSIS:
        print_put_call_analysis(all_trades)


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="MEIC backtest")
    _parser.add_argument("--start", default=None, help="Override PILOT_YEAR_START")
    _parser.add_argument("--end", default=None, help="Override PILOT_YEAR_END")
    _parser.add_argument("--out", default=None, help="Override output CSV path")
    _parser.add_argument("--marathon", action="store_true", help="Run single backtest")
    _parser.add_argument("--direction", default=None, help="Override DIRECTION_MODE (both_sides|ema|always_put|always_call)")
    _parser.add_argument("--entry-start", default=None, help="Override ENTRY_START (HH:MM)")
    _parser.add_argument("--entry-end", default=None, help="Override ENTRY_END (HH:MM)")
    _parser.add_argument("--entry-interval", default=None, type=int, help="Override ENTRY_INTERVAL (minutes)")
    _parser.add_argument("--put-sl", default=None, type=float, help="Override ASYM_PUT_SL_AMOUNT")
    _parser.add_argument("--call-sl", default=None, type=float, help="Override ASYM_CALL_SL_AMOUNT")
    _parser.add_argument("--daily-sl", default=None, type=float, help="Override DAILY_SL")
    _parser.add_argument("--min-credit", default=None, type=float, help="Override MIN_NET_CREDIT")
    _parser.add_argument("--width", default=None, type=float, help="Override WIDTH")
    _parser.add_argument("--min-otm", default=None, type=float, help="Override MIN_OTM_DISTANCE")
    _parser.add_argument("--qty", default=None, type=int, help="Override QTY")
    _parser.add_argument("--mtm-gate", default=None, type=float, help="Override OPEN_MTM_GATE_THRESHOLD")
    _parser.add_argument("--aftn-sl", default=None, type=float, help="Override AFTERNOON_SL_AMOUNT")
    _parser.add_argument("--aftn-cutoff", default=None, help="Override AFTERNOON_SL_CUTOFF (HH:MM)")
    _parser.add_argument("--skip-fomc", action="store_true", help="Skip FOMC days")
    _parser.add_argument("--skip-econ", action="store_true", help="Skip econ event entries")
    _parser.add_argument("--tighten-late-sl", default=None, type=float, help="Tighten SL after 15:00 to this amount")
    _parser.add_argument("--entry-end-early", default=None, help="Stop entries earlier on high-VIX days (HH:MM)")
    _parser.add_argument("--min-credit-late", default=None, type=float, help="Raise min credit after 15:00")
    _parser.add_argument("--skip-vix-lo", default=None, type=float, help="Skip day when VIX >= this")
    _parser.add_argument("--skip-vix-hi", default=None, type=float, help="Skip day when VIX <= this")
    _parser.add_argument("--kelly", action="store_true", help="Enable Kelly zone sizing")
    _parser.add_argument("--ema-vix-gate", action="store_true", help="R6: EMA gate for VIX 20-25 zone")
    _parser.add_argument("--late-qty", action="store_true", help="R7: Reduce qty after 14:45")
    _parser.add_argument("--econ-upsize", action="store_true", help="R8: Upsize on CPI/NFP days")
    _parser.add_argument("--adaptive-otm", action="store_true", help="R9: Adaptive OTM by entry time")
    _parser.add_argument("--streak-accel", action="store_true", help="R10: Winning streak acceleration")
    _parser.add_argument("--late-credit", action="store_true", help="R10alt: Higher min credit after 15:00")
    _parser.add_argument("--skip-thursday", action="store_true", help="L1: Skip Thursdays")
    _parser.add_argument("--canary", action="store_true", help="L2: First-entry canary")
    _parser.add_argument("--loss-cap", default=None, type=float, help="L3: Daily loss cap (e.g. -2000)")
    _parser.add_argument("--morning-gate", action="store_true", help="L4: Morning range gate")
    _parser.add_argument("--morning-gate-pct", default=None, type=float, help="L4: Morning range max pct")
    _parser.add_argument("--cooldown", action="store_true", help="L5: Post-loss day cooldown")
    _parser.add_argument("--skip-pce", action="store_true", help="Skip PCE days")
    _parser.add_argument("--skip-eom", action="store_true", help="Skip end-of-month days")
    _parser.add_argument("--skip-tw", action="store_true", help="Skip triple witching days")
    _parser.add_argument("--orb-filter", action="store_true", help="ORB containment: skip entry if SPX broke 9:30-10:00 range")
    _parser.add_argument("--prior-day-bullish", action="store_true", help="Only enter when prior day was bullish (close > open)")
    _args = _parser.parse_args()

    if _args.start:
        _cfg.PILOT_YEAR_START = _args.start
    if _args.end:
        _cfg.PILOT_YEAR_END = _args.end
    if _args.out:
        _cfg.SAVE_FILE = _args.out
    if _args.direction:
        _cfg.DIRECTION_MODE = _args.direction
    if _args.entry_start:
        h, m = _args.entry_start.split(":")
        _cfg.ENTRY_START = time(int(h), int(m))
    if _args.entry_end:
        h, m = _args.entry_end.split(":")
        _cfg.ENTRY_END = time(int(h), int(m))
    if _args.entry_interval is not None:
        _cfg.ENTRY_INTERVAL = _args.entry_interval
    if _args.put_sl is not None:
        _cfg.ASYM_PUT_SL_AMOUNT = _args.put_sl
        _cfg.PER_POS_SL_AMOUNT = _args.put_sl
    if _args.call_sl is not None:
        _cfg.ASYM_CALL_SL_AMOUNT = _args.call_sl
    if _args.daily_sl is not None:
        _cfg.DAILY_SL = _args.daily_sl
    if _args.min_credit is not None:
        _cfg.MIN_NET_CREDIT = _args.min_credit
    if _args.width is not None:
        _cfg.WIDTH = _args.width
    if _args.min_otm is not None:
        _cfg.MIN_OTM_DISTANCE = _args.min_otm
    if _args.qty is not None:
        _cfg.QTY = _args.qty
    if _args.mtm_gate is not None:
        _cfg.OPEN_MTM_GATE_THRESHOLD = _args.mtm_gate
    if _args.aftn_sl is not None:
        _cfg.AFTERNOON_SL_AMOUNT = _args.aftn_sl
    if _args.aftn_cutoff:
        h, m = _args.aftn_cutoff.split(":")
        _cfg.AFTERNOON_SL_CUTOFF = time(int(h), int(m))
    if _args.skip_fomc:
        _cfg.ENABLE_FOMC_SKIP = True
    if _args.skip_econ:
        _cfg.ENABLE_ECON_FILTER = True
    if _args.skip_vix_lo is not None and _args.skip_vix_hi is not None:
        _cfg.SKIP_VIX_RANGE = (_args.skip_vix_lo, _args.skip_vix_hi)
    if _args.kelly:
        _cfg.ENABLE_KELLY_SIZING = True
    if _args.ema_vix_gate:
        _cfg.ENABLE_EMA_VIX_GATE = True
    if _args.late_qty:
        _cfg.ENABLE_LATE_QTY_REDUCTION = True
    if _args.econ_upsize:
        _cfg.ENABLE_ECON_UPSIZING = True
    if _args.adaptive_otm:
        _cfg.ENABLE_ADAPTIVE_OTM_TIME = True
    if _args.streak_accel:
        _cfg.ENABLE_STREAK_ACCEL = True
    if hasattr(_args, 'late_credit') and _args.late_credit:
        _cfg.ENABLE_LATE_CREDIT_FLOOR = True
    if _args.skip_thursday:
        _cfg.ENABLE_THURSDAY_SKIP = True
    if _args.canary:
        _cfg.ENABLE_CANARY_ENTRY = True
    if _args.loss_cap is not None:
        _cfg.DAILY_LOSS_CAP = _args.loss_cap
    if _args.morning_gate:
        _cfg.ENABLE_MORNING_RANGE_GATE = True
    if _args.morning_gate_pct is not None:
        _cfg.MORNING_RANGE_MAX_PCT = _args.morning_gate_pct
    if _args.cooldown:
        _cfg.ENABLE_POST_LOSS_COOLDOWN = True
    if _args.skip_pce:
        _cfg.ENABLE_PCE_SKIP = True
    if _args.skip_eom:
        # Add EOM skip — need to implement in engine
        _cfg.ENABLE_EOM_SKIP = True
    if _args.skip_tw:
        _cfg.ENABLE_TW_SKIP = True
    if _args.orb_filter:
        _cfg.ENABLE_ORB_FILTER = True
    if _args.prior_day_bullish:
        _cfg.ENABLE_PRIOR_DAY_DIRECTION_FILTER = True

    # Build calendar event date sets
    _cal = _build_calendar_event_dates()
    _EOM_DATES.update(_cal.get("end_of_month", set()))
    _CPI_DATES.update(_cal.get("cpi", set()))
    _PCE_DATES.update(_cal.get("pce", set()))
    _NFP_DATES.update(_cal.get("nfp", set()))
    _EOQ_DATES.update(_cal.get("end_of_quarter", set()))
    _PRE_TW_DATES.update(_cal.get("pre_triple_witching", set()))
    _POST_HOL_DATES.update(_cal.get("post_major_holiday", set()))

    load_quote_disk_cache()
    asyncio.run(run())
    save_quote_disk_cache()
