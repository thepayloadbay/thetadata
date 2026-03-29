from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
from datetime import time

import numpy as np
import pandas as pd

# All config constants and engine functions from meds_core
from meds_core import *
import meds_core as _mc

# Underscore names not included in * import
from meds_core import (
    _apply_fdr_to_rows,
    _build_calendar_event_dates,
    _build_daily_indicators,
    _fetch_day_data,
    _get_baseline_mode,
    _get_effective_sl,
    _get_session,
    _passes_filter,
    _prior_day_ind,
    _quote_cache,
    _simulate_day,
    _sl_label,
)

# CLI-mutable vars: always read from _mc at call time
# _mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END

# ---------------------------------------------
#  STOP-LOSS SWEEP RUNNER
# ---------------------------------------------
async def run_sweep():
    """Run a stop-loss parameter sweep.

    For each date the market data is fetched exactly once.
    The simulation is then re-run independently for every level in SWEEP_SL_LEVELS
    using the in-memory quote cache, so no extra API calls are needed.
    Results are written to SWEEP_SAVE_FILE and printed as a summary table.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: STOP-LOSS SWEEP MODE")
    logger.info(f"SL levels : {[_sl_label(sl) for sl in SWEEP_SL_LEVELS]}")
    logger.info(f"Output    : {SWEEP_SAVE_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per SL level
    sweep_trades: dict[str, list] = {_sl_label(sl): [] for sl in SWEEP_SL_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each SL level using the cached quotes
            for sl in SWEEP_SL_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, sl)
                label = _sl_label(sl)
                sweep_trades[label].extend(trades)
                logger.debug(f"  SL={label:>6} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Write summary CSV --
    sweep_cols = [
        "stop_loss", "num_trades", "num_wins", "num_losses",
        "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    with open(SWEEP_SAVE_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sweep_cols)
        w.writeheader()
        for sl in SWEEP_SL_LEVELS:
            label = _sl_label(sl)
            m     = compute_metrics(sweep_trades[label])
            pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
            w.writerow({
                "stop_loss":     label,
                "num_trades":    m["num_trades"],
                "num_wins":      m["num_wins"],
                "num_losses":    m["num_losses"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{m['total_pnl']:.2f}",
                "avg_win":       f"{m['avg_win']:.2f}",
                "avg_loss":      f"{m['avg_loss']:.2f}",
                "profit_factor": pf,
                "max_drawdown":  f"{m['max_drawdown']:.2f}",
            })

    logger.info(f"\nSweep complete -- summary saved to {SWEEP_SAVE_FILE}")

    # -- Print comparison table --
    col_w = 12
    hdr   = (f"{'SL':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "-" * len(hdr)
    logger.info(sep)
    logger.info("  STOP-LOSS SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for sl in SWEEP_SL_LEVELS:
        label = _sl_label(sl)
        m     = compute_metrics(sweep_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"{label:>{col_w}} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"${m['max_drawdown']:>9,.2f} | {pf:>6}"
        )
    logger.info(sep)

    # -- VIX analysis across all sweep trades (combined) --
    all_sweep_trades = []
    for trades in sweep_trades.values():
        all_sweep_trades.extend(trades)
    # Deduplicate by (entry_date, entry_time, short_strike) to avoid counting each trade N times
    seen = set()
    unique_sweep_trades = []
    for t in all_sweep_trades:
        key = (t.get("entry_date"), t.get("entry_time"), t.get("short_strike"))
        if key not in seen:
            seen.add(key)
            unique_sweep_trades.append(t)
    print_vix_analysis(unique_sweep_trades)
    if RUN_STRIKE_DISTANCE_ANALYSIS:
        print_strike_distance_analysis(unique_sweep_trades)
    if RUN_PUT_CALL_ANALYSIS:
        print_put_call_analysis(unique_sweep_trades)


# ---------------------------------------------
#  EMA PARAMETER SWEEP RUNNER
# ---------------------------------------------
async def run_ema_sweep():
    """Run an EMA fast/slow parameter sweep.

    For each date the market data is fetched exactly once (seeded with enough
    bars for the largest slow EMA period).  The simulation is then re-run for
    every (fast, slow) combination against the in-memory quote cache -- no extra
    API calls required.

    Results are written to EMA_SWEEP_SAVE_FILE sorted by total_pnl descending.
    """
    date_list  = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')
    combos     = [(f, s) for f in SWEEP_EMA_FAST for s in SWEEP_EMA_SLOW]
    max_seed   = max(SWEEP_EMA_SLOW)

    logger.info("=" * 70)
    logger.info("MEFT v35: EMA PARAMETER SWEEP MODE")
    logger.info(f"Fast EMA  : {SWEEP_EMA_FAST}")
    logger.info(f"Slow EMA  : {SWEEP_EMA_SLOW}")
    logger.info(f"Combos    : {len(combos)}")
    logger.info(f"Output    : {EMA_SWEEP_SAVE_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per (fast, slow) combo
    ema_trades: dict[tuple, list] = {(f, s): [] for f, s in combos}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once, seeding with enough bars for the largest slow EMA
            day_data = await _fetch_day_data(session, d_str, seed_bars=max_seed)
            if day_data is None:
                continue

            # Re-simulate for each (fast, slow) combo using the cached quotes
            for fast, slow in combos:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, ema_fast=fast, ema_slow=slow)
                ema_trades[(fast, slow)].extend(trades)
                logger.debug(
                    f"  EMA({fast},{slow}) | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows sorted by total_pnl descending --
    ema_sweep_cols = [
        "ema_fast", "ema_slow",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
        "p_value", "p_fdr", "sig_fdr",
    ]

    rows = []
    for fast, slow in combos:
        m  = compute_metrics(ema_trades[(fast, slow)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "ema_fast":      fast,
            "ema_slow":      slow,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
            "p_value":       round(m["p_value"], 4),
            "_total_pnl_num": m["total_pnl"],
        })

    _apply_fdr_to_rows(rows)
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV (drop sort key) --
    with open(EMA_SWEEP_SAVE_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ema_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in ema_sweep_cols})

    logger.info(f"\nEMA sweep complete -- summary saved to {EMA_SWEEP_SAVE_FILE}")

    # -- Print comparison table --
    hdr = (
        f"  {'Fast':>4} | {'Slow':>4} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  EMA PARAMETER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"  {row['ema_fast']:>4} | {row['ema_slow']:>4} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{pf:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MINIMUM NET CREDIT SWEEP RUNNER
# ---------------------------------------------
async def run_min_credit_sweep():
    """Sweep minimum net credit threshold for spread entry.

    Market data is fetched once per day; the simulation is re-run for each
    credit level from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MINIMUM NET CREDIT SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MIN_CREDIT_LEVELS}")
    logger.info(f"Output : {MIN_CREDIT_SWEEP_FILE}")
    logger.info("=" * 70)

    mc_trades: dict[float, list] = {mc: [] for mc in SWEEP_MIN_CREDIT_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for mc in SWEEP_MIN_CREDIT_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, min_credit=mc)
                mc_trades[mc].extend(trades)
                logger.debug(f"  min_credit={mc} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    mc_cols = [
        "min_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    rows = []
    for mc in SWEEP_MIN_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[mc])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in mc_trades[mc]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        rows.append({
            "min_credit":          mc,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MIN_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=mc_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in mc_cols})

    logger.info(f"\nMin credit sweep complete -- saved to {MIN_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MinCredit':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['min_credit']:>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MINIMUM OTM DISTANCE SWEEP RUNNER
# ---------------------------------------------
async def run_min_otm_sweep():
    """Sweep minimum OTM distance (pts) required for spread entry.

    Market data is fetched once per day; the simulation is re-run for each
    OTM floor from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MINIMUM OTM DISTANCE SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MIN_OTM_LEVELS}")
    logger.info(f"Output : {MIN_OTM_SWEEP_FILE}")
    logger.info("=" * 70)

    otm_trades: dict = {str(otm): [] for otm in SWEEP_MIN_OTM_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for otm in SWEEP_MIN_OTM_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    min_otm_distance=otm,
                )
                otm_trades[str(otm)].extend(trades)
                logger.debug(f"  min_otm={otm} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    otm_cols = [
        "min_otm_distance", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for otm in SWEEP_MIN_OTM_LEVELS:
        m  = compute_metrics(otm_trades[str(otm)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "min_otm_distance": "None" if otm is None else otm,
            "num_trades":       m["num_trades"],
            "win_rate_pct":     f"{m['win_rate']:.1f}",
            "total_pnl":        f"{m['total_pnl']:.2f}",
            "avg_win":          f"{m['avg_win']:.2f}",
            "avg_loss":         f"{m['avg_loss']:.2f}",
            "profit_factor":    pf,
            "max_drawdown":     f"{m['max_drawdown']:.2f}",
            "calmar":           f"{calmar:.2f}",
            "_total_pnl_num":   m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MIN_OTM_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=otm_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in otm_cols})

    logger.info(f"\nMin OTM sweep complete -- saved to {MIN_OTM_SWEEP_FILE}")

    hdr = (
        f"  {'MinOTM':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN OTM DISTANCE SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['min_otm_distance']):>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MAX NET CREDIT SWEEP RUNNER
# ---------------------------------------------
async def run_max_credit_sweep():
    """Sweep maximum net credit cap for spread entry.

    Entries where the only qualifying spread exceeds the cap are skipped.
    Market data is fetched once per day and re-used for each level.
    Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MAX NET CREDIT SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MAX_CREDIT_LEVELS}")
    logger.info(f"Output : {MAX_CREDIT_SWEEP_FILE}")
    logger.info("=" * 70)

    mc_trades: dict = {str(mc): [] for mc in SWEEP_MAX_CREDIT_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for mc in SWEEP_MAX_CREDIT_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    max_credit=mc,
                )
                mc_trades[str(mc)].extend(trades)
                logger.debug(f"  max_credit={mc} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "max_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for mc in SWEEP_MAX_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[str(mc)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "max_credit":     "None" if mc is None else mc,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "calmar":         f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MAX_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nMax credit sweep complete -- saved to {MAX_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MaxCredit':>10} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MAX CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['max_credit']):>10} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  DAILY PROFIT TARGET SWEEP RUNNER
# ---------------------------------------------
async def run_daily_tp_sweep():
    """Sweep the intraday daily profit target level.

    Market data is fetched once per day; the simulation is re-run for each
    TP level from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: DAILY PROFIT TARGET SWEEP MODE")
    logger.info(f"Levels : {SWEEP_DAILY_TP_LEVELS}")
    logger.info(f"Output : {DAILY_TP_SWEEP_FILE}")
    logger.info("=" * 70)

    tp_trades: dict = {str(tp): [] for tp in SWEEP_DAILY_TP_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for tp in SWEEP_DAILY_TP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    daily_tp=tp,
                )
                tp_trades[str(tp)].extend(trades)
                logger.debug(f"  daily_tp={tp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    tp_cols = [
        "daily_tp", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for tp in SWEEP_DAILY_TP_LEVELS:
        m  = compute_metrics(tp_trades[str(tp)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "daily_tp":     "None" if tp is None else tp,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(DAILY_TP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tp_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in tp_cols})

    logger.info(f"\nDaily TP sweep complete -- saved to {DAILY_TP_SWEEP_FILE}")

    hdr = (
        f"  {'DailyTP':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  DAILY TP SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['daily_tp']):>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  PNL SAMPLE INTERVAL SWEEP RUNNER
# ---------------------------------------------
async def run_pnl_sample_sweep():
    """Sweep how frequently (in minutes) MTM checks run to evaluate open positions.

    Market data is fetched once per day; the simulation is re-run for each
    interval from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: PNL SAMPLE INTERVAL SWEEP MODE")
    logger.info(f"Intervals : {SWEEP_PNL_SAMPLE_INTERVALS} minutes")
    logger.info(f"Output    : {PNL_SAMPLE_SWEEP_FILE}")
    logger.info("=" * 70)

    interval_trades: dict[int, list] = {iv: [] for iv in SWEEP_PNL_SAMPLE_INTERVALS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for iv in SWEEP_PNL_SAMPLE_INTERVALS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    pnl_sample_interval=iv,
                )
                interval_trades[iv].extend(trades)
                logger.debug(f"  pnl_sample={iv}m | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "pnl_sample_interval_min", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for iv in SWEEP_PNL_SAMPLE_INTERVALS:
        m  = compute_metrics(interval_trades[iv])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "pnl_sample_interval_min": iv,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(PNL_SAMPLE_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nPnL sample interval sweep complete -- saved to {PNL_SAMPLE_SWEEP_FILE}")

    hdr = (
        f"  {'Interval':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PNL SAMPLE INTERVAL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['pnl_sample_interval_min']) + 'min':>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MAX BUYING POWER SWEEP RUNNER
# ---------------------------------------------
async def run_max_bp_sweep():
    """Sweep max total committed buying power across open positions.

    At each entry, qty is reduced so (committed_bp + new_bp) stays within limit.
    BP per contract = (spread_width - credit) × 100.
    Data is fetched once per day; each BP level is re-simulated from cache.
    Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MAX BUYING POWER SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MAX_BP_LEVELS}")
    logger.info(f"Output : {MAX_BP_SWEEP_FILE}")
    logger.info("=" * 70)

    bp_trades: dict = {str(bp): [] for bp in SWEEP_MAX_BP_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for bp in SWEEP_MAX_BP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    max_buying_power=bp,
                )
                bp_trades[str(bp)].extend(trades)
                logger.debug(f"  max_bp={bp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "max_bp", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
        "avg_qty",
    ]
    rows = []
    for bp in SWEEP_MAX_BP_LEVELS:
        trades = bp_trades[str(bp)]
        m   = compute_metrics(trades)
        pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
        avg_qty = sum(float(t.get("qty", 0)) for t in trades) / len(trades) if trades else 0.0
        rows.append({
            "max_bp":       "None" if bp is None else bp,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{cal:.2f}",
            "avg_qty":      f"{avg_qty:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MAX_BP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nMax BP sweep complete -- saved to {MAX_BP_SWEEP_FILE}")

    hdr = (
        f"  {'Max BP':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8} | {'Avg Qty':>7}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MAX BUYING POWER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['max_bp']):>8} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8} | {float(row['avg_qty']):>7.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  TOUCH EXIT SWEEP RUNNER
# ---------------------------------------------
async def run_touch_sweep():
    """Sweep Touch Exit thresholds -- both dollar and % variants.

    Dollar touch: close PUT when curr_price <= short_strike + touch_dollars
                  close CALL when curr_price >= short_strike - touch_dollars
    Percent touch: close PUT when curr_price <= short_strike * (1 + touch_pct/100)
                   close CALL when curr_price >= short_strike * (1 - touch_pct/100)

    Positive values = OTM buffer (exit before ITM).
    Zero = exit at-the-money.
    Negative = allow going ITM by that amount before exiting.
    None (baseline) = no touch exit, hold to daily_tp / expiration.

    Data is fetched once per day; each threshold is re-simulated from cache.
    Results sorted by Calmar descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    # Build the full list: dollar levels + pct levels + None baseline
    dollar_keys = [("$", v) for v in SWEEP_TOUCH_DOLLARS]
    pct_keys    = [("%", v) for v in SWEEP_TOUCH_PCT]
    all_keys    = [("none", None)] + dollar_keys + pct_keys  # "none" = baseline

    trade_map: dict = {str(k): [] for k in all_keys}

    logger.info("=" * 70)
    logger.info("MEFT v35: TOUCH EXIT SWEEP MODE")
    logger.info(f"Dollar levels : {SWEEP_TOUCH_DOLLARS}")
    logger.info(f"Pct    levels : {SWEEP_TOUCH_PCT}")
    logger.info(f"Output        : {TOUCH_SWEEP_FILE}")
    logger.info("=" * 70)

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for kind, val in all_keys:
                if kind == "none":
                    t_dollars, t_pct = None, None
                elif kind == "$":
                    t_dollars, t_pct = val, None
                else:
                    t_dollars, t_pct = None, val

                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    touch_exit_dollars=t_dollars,
                    touch_exit_pct=t_pct,
                )
                trade_map[str((kind, val))].extend(trades)

    # -- Build result rows --
    cols = [
        "touch_type", "touch_value",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for kind, val in all_keys:
        trades = trade_map[str((kind, val))]
        m   = compute_metrics(trades)
        pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "touch_type":    kind,
            "touch_value":   "None" if val is None else val,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
            "calmar":        f"{cal:.2f}",
            "_calmar_num":   cal,
        })
    rows.sort(key=lambda r: r["_calmar_num"], reverse=True)

    with open(TOUCH_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nTouch exit sweep complete -- saved to {TOUCH_SWEEP_FILE}")

    hdr = (
        f"  {'Type':>6} | {'Value':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TOUCH EXIT SWEEP RESULTS  (sorted by Calmar)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['touch_type']:>6} | {str(row['touch_value']):>8} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>5}% | "
            f"${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  DAILY BAR INDICATOR FILTER SWEEP RUNNER
# ---------------------------------------------
_DAY_FILTER_CHECKPOINT = os.path.join(LOGS_DIR, "meds_day_filter_sweep_checkpoint.csv")


async def run_day_filter_sweep():
    """Test each daily indicator as an independent day-selection filter.

    Data is fetched once per day (respecting VIX and other global filters).
    For each (indicator, threshold) combo the same day pool is re-filtered and
    re-simulated. Results sorted by Calmar descending within each indicator group.
    """
    # -- Suppress DEBUG during sweep to avoid log bloat and I/O pressure --
    _saved_log_level = logger.level
    logger.setLevel(logging.INFO)

    global _DAILY_INDICATORS
    if not _DAILY_INDICATORS:
        logger.info("Building daily indicator table from local parquets (full mode for sweep)...")
        _DAILY_INDICATORS = _build_daily_indicators(compute_full=True)
        logger.info(f"  -> {len(_DAILY_INDICATORS)} daily bars loaded")

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: DAILY BAR INDICATOR FILTER SWEEP MODE")
    logger.info(f"Filters   : {list(SWEEP_DAY_FILTERS.keys())}")
    logger.info(f"Output    : {DAY_FILTER_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Resume: load already-completed combos from checkpoint --
    _completed: set[tuple] = set()
    _checkpoint_rows: list[dict] = []
    if os.path.exists(_DAY_FILTER_CHECKPOINT):
        with open(_DAY_FILTER_CHECKPOINT, newline="") as _f:
            for _row in csv.DictReader(_f):
                _completed.add((_row["filter_name"], _row["threshold"]))
                _checkpoint_rows.append(_row)
        logger.info(f"Resuming -- loaded {len(_checkpoint_rows)} completed combos from checkpoint.")

    # -- Step 1: Pre-fetch all days with VIX filter OFF so vix_max can be swept --
    # We temporarily disable the global VIX filters so every trading day enters
    # the pool. The vix_max sweep filter then applies VIX thresholds in the loop.
    global VIX_MAX_FILTER, VIX_MIN_FILTER
    _saved_vix_max = VIX_MAX_FILTER
    _saved_vix_min = VIX_MIN_FILTER
    VIX_MAX_FILTER = None
    VIX_MIN_FILTER = None

    day_pool: dict[str, dict] = {}   # date_str -> day_data
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data

    VIX_MAX_FILTER = _saved_vix_max
    VIX_MIN_FILTER = _saved_vix_min
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days (VIX filter suspended during fetch).")

    async with _get_session() as session:
        # -- Step 2: Baseline (no extra filter) --
        baseline_trades = []
        for d_str, day_data in day_pool.items():
            trades, _ = await _simulate_day(
                session, day_data, DAILY_SL,
                baseline_mode=DIRECTION_MODE,
                spread_width=WIDTH,
                min_credit=MIN_NET_CREDIT,
                entry_start=ENTRY_START,
                entry_end=ENTRY_END,
                entry_interval=ENTRY_INTERVAL,
            )
            baseline_trades.extend(trades)

        # -- Step 3: For each filter × threshold, re-filter the day pool and simulate --
        cols = [
            "filter_name", "threshold", "days_traded", "days_filtered_out",
            "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
        ]
        rows = []

        # Open checkpoint file for appending (write header only if starting fresh)
        _ckpt_is_new = not os.path.exists(_DAY_FILTER_CHECKPOINT)
        _ckpt_f = open(_DAY_FILTER_CHECKPOINT, "a", newline="")
        _ckpt_w = csv.DictWriter(_ckpt_f, fieldnames=cols)
        if _ckpt_is_new:
            _ckpt_w.writeheader()

        # Baseline row (skip if already in checkpoint)
        if ("BASELINE (no filter)", "--") not in _completed:
            bm = compute_metrics(baseline_trades)
            bpf = f"{bm['profit_factor']:.2f}" if bm['profit_factor'] != float("inf") else "inf"
            bcalmar = bm["total_pnl"] / abs(bm["max_drawdown"]) if bm["max_drawdown"] != 0 else float("inf")
            _baseline_row = {
                "filter_name":       "BASELINE (no filter)",
                "threshold":         "--",
                "days_traded":       len(day_pool),
                "days_filtered_out": 0,
                "num_trades":        bm["num_trades"],
                "win_rate_pct":      f"{bm['win_rate']:.1f}",
                "total_pnl":         f"{bm['total_pnl']:.2f}",
                "avg_win":           f"{bm['avg_win']:.2f}",
                "avg_loss":          f"{bm['avg_loss']:.2f}",
                "profit_factor":     bpf,
                "max_drawdown":      f"{bm['max_drawdown']:.2f}",
                "calmar":            f"{bcalmar:.2f}",
                "_calmar_num":       bcalmar,
                "_filter_sort":      "000_BASELINE",
            }
            rows.append(_baseline_row)
            _ckpt_w.writerow({k: _baseline_row[k] for k in cols})
            _ckpt_f.flush()

        for filter_name, thresholds in SWEEP_DAY_FILTERS.items():
            for threshold in thresholds:
                if (filter_name, str(threshold)) in _completed:
                    logger.info(
                        f"  {filter_name:<22} threshold={str(threshold):<8} "
                        f"[skipped -- already in checkpoint]"
                    )
                    continue

                filtered_trades = []
                days_in = 0
                days_out = 0
                _is_intraday = filter_name in (
                    "priceChgPct_max", "priceChgPct_min",
                    "priceChgSD_max",  "priceChgSD_min",
                    "openChgPct_max",  "openChgPct_min",
                )
                for d_str, day_data in day_pool.items():
                    if not _is_intraday and not _passes_filter(filter_name, threshold, d_str, day_data):
                        days_out += 1
                        continue
                    days_in += 1
                    trades, _ = await _simulate_day(
                        session, day_data, DAILY_SL,
                        baseline_mode=DIRECTION_MODE,
                        spread_width=WIDTH,
                        min_credit=MIN_NET_CREDIT,
                        entry_start=ENTRY_START,
                        entry_end=ENTRY_END,
                        entry_interval=ENTRY_INTERVAL,
                        price_chg_pct_max=threshold if filter_name == "priceChgPct_max" else None,
                        price_chg_pct_min=threshold if filter_name == "priceChgPct_min" else None,
                        price_chg_sd_max=threshold if filter_name == "priceChgSD_max" else None,
                        price_chg_sd_min=threshold if filter_name == "priceChgSD_min" else None,
                        open_chg_pct_max=threshold if filter_name == "openChgPct_max" else None,
                        open_chg_pct_min=threshold if filter_name == "openChgPct_min" else None,
                    )
                    filtered_trades.extend(trades)

                m   = compute_metrics(filtered_trades)
                pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
                cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
                _row = {
                    "filter_name":       filter_name,
                    "threshold":         str(threshold),
                    "days_traded":       days_in,
                    "days_filtered_out": days_out,
                    "num_trades":        m["num_trades"],
                    "win_rate_pct":      f"{m['win_rate']:.1f}",
                    "total_pnl":         f"{m['total_pnl']:.2f}",
                    "avg_win":           f"{m['avg_win']:.2f}",
                    "avg_loss":          f"{m['avg_loss']:.2f}",
                    "profit_factor":     pf,
                    "max_drawdown":      f"{m['max_drawdown']:.2f}",
                    "calmar":            f"{cal:.2f}",
                    "_calmar_num":       cal,
                    "_filter_sort":      filter_name,
                }
                rows.append(_row)
                _ckpt_w.writerow({k: _row[k] for k in cols})
                _ckpt_f.flush()
                logger.info(
                    f"  {filter_name:<22} threshold={str(threshold):<8} "
                    f"days={days_in:>3} | trades={m['num_trades']:>5} | "
                    f"pnl=${m['total_pnl']:>10,.2f} | calmar={cal:>7.2f}"
                )

        _ckpt_f.close()

    # -- Merge checkpoint rows (from prior runs) with newly computed rows --
    # Rebuild _calmar_num / _filter_sort for checkpoint rows so the summary works.
    for _cr in _checkpoint_rows:
        try:
            _cn = float(_cr["calmar"]) if _cr["calmar"] not in ("inf", "-inf") else float(_cr["calmar"].replace("inf", "inf"))
        except (ValueError, KeyError):
            _cn = 0.0
        _cr["_calmar_num"] = _cn
        _cr["_filter_sort"] = _cr["filter_name"] if _cr["filter_name"] != "BASELINE (no filter)" else "000_BASELINE"
    all_rows = _checkpoint_rows + rows

    cols = [
        "filter_name", "threshold", "days_traded", "days_filtered_out",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]

    # -- Save final CSV --
    with open(DAY_FILTER_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in all_rows:
            w.writerow({k: row[k] for k in cols})

    # -- Delete checkpoint -- clean finish --
    if os.path.exists(_DAY_FILTER_CHECKPOINT):
        os.remove(_DAY_FILTER_CHECKPOINT)

    logger.info(f"\nDay filter sweep complete -- saved to {DAY_FILTER_SWEEP_FILE}")
    logger.setLevel(_saved_log_level)

    # -- Print grouped summary (best threshold per indicator by Calmar) --
    hdr = (
        f"  {'Filter':<22} | {'Threshold':>10} | {'Days':>5} | {'Trades':>7} | "
        f"{'WR%':>6} | {'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  DAY FILTER SWEEP -- BEST THRESHOLD PER INDICATOR (by Calmar)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    # Group by filter, pick best Calmar per group
    best: dict[str, dict] = {}
    for row in all_rows:
        fn = row["_filter_sort"]
        if fn not in best or row["_calmar_num"] > best[fn]["_calmar_num"]:
            best[fn] = row
    for row in sorted(best.values(), key=lambda r: r["_calmar_num"], reverse=True):
        logger.info(
            f"  {row['filter_name']:<22} | {row['threshold']:>10} | {row['days_traded']:>5} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>5}% | "
            f"${float(row['total_pnl']):>11,.2f} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  CALENDAR EVENT FILTER SWEEP RUNNER
# ---------------------------------------------
async def run_calendar_event_sweep():
    """Test P&L impact of skipping each calendar event type independently.

    Pre-fetches all trading days (ECON_FILTER suspended so the full pool is
    available). Baseline = trade every day. For each event type the days in
    that set are removed from the pool and the full backtest is re-simulated.
    A positive pnl_delta means skipping those days *improves* P&L.
    """
    global ENABLE_ECON_FILTER

    event_sets = _build_calendar_event_dates()

    logger.info("=" * 70)
    logger.info("MEFT v35: CALENDAR EVENT FILTER SWEEP MODE")
    logger.info(f"Events  : {list(event_sets.keys())}")
    logger.info(f"Output  : {CALENDAR_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Step 1: Pre-fetch all days --
    # Suspend ECON_FILTER and temporarily remove FOMC + TW from MARKET_HOLIDAYS
    # so those dates enter the pool and can be tested as event filters.
    global MARKET_HOLIDAYS, ENABLE_ECON_FILTER
    _saved_econ      = ENABLE_ECON_FILTER
    _saved_holidays  = MARKET_HOLIDAYS
    ENABLE_ECON_FILTER = False
    # Strip out FOMC and TW so we can fetch and test them
    _fomc_set = {
        "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
        "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
        "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
        "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
        "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
    }
    _tw_set = {
        "20220318","20220617","20220916","20221216",
        "20230317","20230616","20230915","20231215",
        "20240315","20240621","20240920","20241220",
        "20250321","20250620","20250919","20251219",
        "20260320","20260619","20260918","20261218",
    }
    MARKET_HOLIDAYS = _saved_holidays - _fomc_set - _tw_set

    date_list  = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data

    ENABLE_ECON_FILTER = _saved_econ
    MARKET_HOLIDAYS    = _saved_holidays
    logger.info(
        f"Pre-fetched {len(day_pool)} qualifying days "
        f"(ECON_FILTER + FOMC + TW suspended during fetch)."
    )

    cols = [
        "event_type", "days_in_pool", "days_skipped", "days_traded",
        "num_trades", "win_rate_pct", "total_pnl", "pnl_delta",
        "max_drawdown", "calmar", "sharpe", "sortino",
    ]
    rows = []

    async def _sim(session, d_str: str, day_data: dict):
        """Simulate one day with full current settings (dynamic SL, all params)."""
        effective_sl = _get_effective_sl(day_data, d_str)
        return await _simulate_day(
            session, day_data, effective_sl,
            baseline_mode=_get_baseline_mode(d_str),
            pos_trail_activation=POS_TRAIL_ACTIVATION,
            pos_trail_pullback=POS_TRAIL_PULLBACK,
            min_otm_distance=MIN_OTM_DISTANCE,
            max_credit=MAX_NET_CREDIT,
        )

    async with _get_session() as session:
        # -- Step 2: Baseline (no calendar filter, full settings) --
        baseline_trades: list = []
        for d_str, day_data in day_pool.items():
            trades, _ = await _sim(session, d_str, day_data)
            baseline_trades.extend(trades)
        bm       = compute_metrics(baseline_trades)
        base_pnl = bm["total_pnl"]
        base_dd  = bm["max_drawdown"]
        base_cal = base_pnl / abs(base_dd) if base_dd != 0 else float("inf")

        rows.append({
            "event_type":    "BASELINE (no filter)",
            "days_in_pool":  len(day_pool),
            "days_skipped":  0,
            "days_traded":   len(day_pool),
            "num_trades":    bm["num_trades"],
            "win_rate_pct":  f"{bm['win_rate']:.1f}",
            "total_pnl":     f"{base_pnl:.2f}",
            "pnl_delta":     "--",
            "max_drawdown":  f"{base_dd:.2f}",
            "calmar":        f"{base_cal:.2f}",
            "sharpe":        f"{bm['sharpe']:.2f}",
            "sortino":       f"{bm['sortino']:.2f}",
            "_pnl_num":      base_pnl,
            "_cal_num":      base_cal,
        })
        logger.info(
            f"  {'BASELINE':<20} | skipped={0:>3} | days={len(day_pool):>4} | "
            f"trades={bm['num_trades']:>5} | pnl=${base_pnl:>10,.2f} | calmar={base_cal:.2f}"
        )

        # -- Step 3: For each event type, remove those dates and re-simulate --
        for event_name, event_dates in event_sets.items():
            in_pool       = {d for d in event_dates if d in day_pool}
            skip_count    = len(in_pool)
            filtered_pool = {d: v for d, v in day_pool.items() if d not in in_pool}

            filtered_trades: list = []
            for d_str, day_data in filtered_pool.items():
                trades, _ = await _sim(session, d_str, day_data)
                filtered_trades.extend(trades)

            m     = compute_metrics(filtered_trades)
            pnl   = m["total_pnl"]
            dd    = m["max_drawdown"]
            cal   = pnl / abs(dd) if dd != 0 else float("inf")
            delta = pnl - base_pnl

            rows.append({
                "event_type":   event_name,
                "days_in_pool": len(day_pool),
                "days_skipped": skip_count,
                "days_traded":  len(filtered_pool),
                "num_trades":   m["num_trades"],
                "win_rate_pct": f"{m['win_rate']:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    f"{delta:+.2f}",
                "max_drawdown": f"{dd:.2f}",
                "calmar":       f"{cal:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
                "sortino":      f"{m['sortino']:.2f}",
                "_pnl_num":     pnl,
                "_cal_num":     cal,
            })
            sign = "▲" if delta >= 0 else "▼"
            logger.info(
                f"  {event_name:<20} | skipped={skip_count:>3} | days={len(filtered_pool):>4} | "
                f"trades={m['num_trades']:>5} | pnl=${pnl:>10,.2f} | "
                f"delta={sign}${abs(delta):>8,.2f} | calmar={cal:.2f}"
            )

    # -- Save CSV --
    with open(CALENDAR_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in cols})
    logger.info(f"\nCalendar event sweep saved -> {CALENDAR_SWEEP_FILE}")

    # -- Print summary table sorted by P&L delta --
    data_rows = [r for r in rows if r["pnl_delta"] != "--"]
    data_rows.sort(key=lambda r: float(r["pnl_delta"]), reverse=True)

    hdr = (
        f"  {'Event Type':<20} | {'Skipped':>7} | {'Traded':>6} | "
        f"{'Trades':>7} | {'WR%':>5} | {'Total P&L':>12} | "
        f"{'Delta':>11} | {'Max DD':>10} | {'Calmar':>8} | {'Sharpe':>7}"
    )
    sep = "-" * len(hdr)
    logger.info("")
    logger.info(sep)
    logger.info("  CALENDAR EVENT SWEEP -- sorted by P&L delta (positive = skipping helps)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    # Print baseline first
    br = rows[0]
    logger.info(
        f"  {br['event_type']:<20} | {'--':>7} | {br['days_traded']:>6} | "
        f"{br['num_trades']:>7} | {br['win_rate_pct']:>4}% | "
        f"${float(br['total_pnl']):>11,.2f} | {'--':>11} | "
        f"${float(br['max_drawdown']):>9,.2f} | {br['calmar']:>8} | {br['sharpe']:>7}"
    )
    logger.info(sep)
    for row in data_rows:
        delta_val = float(row["pnl_delta"])
        sign = "▲ +" if delta_val >= 0 else "▼ "
        logger.info(
            f"  {row['event_type']:<20} | {row['days_skipped']:>7} | {row['days_traded']:>6} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {sign}${abs(delta_val):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info(sep)


# ---------------------------------------------
#  VIX MAGNITUDE FILTER SWEEP RUNNER
# ---------------------------------------------
async def run_vix_mag_sweep():
    """Sweep minimum |dVixChgPct| required to trade a day.

    Days where the overnight VIX change is smaller than the threshold are skipped
    entirely -- the directional signal is too weak. Baseline = threshold 0.0 (trade all days).
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX MAGNITUDE FILTER SWEEP")
    logger.info(f"Thresholds : {VIX_MAG_THRESHOLDS}")
    logger.info(f"Output     : {VIX_MAG_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all qualifying days --
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["min_vix_chg_pct", "days_traded", "days_skipped", "num_trades",
            "win_rate_pct", "total_pnl", "pnl_delta", "max_drawdown",
            "dd_delta", "calmar", "sharpe", "sortino"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for threshold in VIX_MAG_THRESHOLDS:
            all_trades: list = []
            skipped = 0
            for d_str, day_data in day_pool.items():
                ind = _DAILY_INDICATORS.get(d_str) or {}
                chg = ind.get("dVixChgPct")
                if chg is not None and abs(chg) < threshold:
                    skipped += 1
                    continue
                effective_sl = _get_effective_sl(day_data, d_str)
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if effective_sl is not None else PNL_SAMPLE_INTERVAL
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=_get_baseline_mode(d_str),
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "min_vix_chg_pct": f"{threshold:.2f}",
                "days_traded":     len(day_pool) - skipped,
                "days_skipped":    skipped,
                "num_trades":      m["num_trades"],
                "win_rate_pct":    f"{m['win_rate']:.1f}",
                "total_pnl":       f"{pnl:.2f}",
                "pnl_delta":       pnl_delta,
                "max_drawdown":    f"{dd:.2f}",
                "dd_delta":        dd_delta,
                "calmar":          f"{calmar:.2f}",
                "sharpe":          f"{m['sharpe']:.2f}",
                "sortino":         f"{m['sortino']:.2f}",
            })
            logger.info(
                f"  threshold={threshold:.2f}% | skipped={skipped:>3} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    # Save CSV
    with open(VIX_MAG_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Print summary table
    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  VIX MAGNITUDE FILTER SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'MIN|dVIX%|':>10} | {'TRADED':>6} | {'SKIPPED':>7} | {'TRADES':>7} | "
                f"{'WIN%':>5} | {'TOTAL_PNL':>12} | {'PNL_DELTA':>10} | "
                f"{'MAX_DD':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['min_vix_chg_pct']:>10} | {row['days_traded']:>6} | {row['days_skipped']:>7} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {VIX_MAG_SWEEP_FILE}")


# ---------------------------------------------
#  GAP-DOWN CALL SL SWEEP RUNNER
# ---------------------------------------------
async def run_gap_call_sl_sweep():
    """Sweep SL levels applied only on gap-down CALL days.

    Gap-down + CALL days are the weakest combo ($369/day, 89.7% WR vs 93%+ baseline).
    Theory: intraday gap-fill rallies threaten CALL strikes on bearish opens.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: GAP-DOWN CALL SL SWEEP")
    logger.info(f"Levels  : {GAP_CALL_SL_LEVELS}")
    logger.info(f"Output  : {GAP_CALL_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days --
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    # Count gap-down CALL days for reference
    gap_down_call_days = sum(
        1 for d_str in day_pool
        if (_DAILY_INDICATORS.get(d_str) or {}).get("dGapPercent", 0) < 0
        and _get_baseline_mode(d_str) == "always_call"
    )
    logger.info(f"Gap-down CALL days in pool: {gap_down_call_days}")

    cols = ["gap_call_sl", "gap_down_call_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in GAP_CALL_SL_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                ind       = _DAILY_INDICATORS.get(d_str) or {}
                gap_pct   = ind.get("dGapPercent", 0) or 0
                direction = _get_baseline_mode(d_str)
                is_gap_down_call = (gap_pct < 0) and (direction == "always_call")

                if is_gap_down_call and sl_level is not None:
                    # Use test SL; still respect existing dynamic SL if it's tighter
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    effective_sl = sl_level if dyn_sl is None else max(sl_level, dyn_sl)
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "gap_call_sl":        label,
                "gap_down_call_days": gap_down_call_days,
                "num_trades":         m["num_trades"],
                "win_rate_pct":       f"{m['win_rate']:.1f}",
                "total_pnl":          f"{pnl:.2f}",
                "pnl_delta":          pnl_delta,
                "max_drawdown":       f"{dd:.2f}",
                "dd_delta":           dd_delta,
                "calmar":             f"{calmar:.2f}",
                "sharpe":             f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  gap_call_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(GAP_CALL_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  GAP-DOWN CALL SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['gap_call_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {GAP_CALL_SL_SWEEP_FILE}")


# ---------------------------------------------
#  VIX SUB-12 TIGHTER SL SWEEP RUNNER
# ---------------------------------------------
async def run_vix_sub12_sl_sweep():
    """Sweep tighter SL levels applied exclusively on days where VIX < 12.

    Jun 2024 (VIX 11.95-12.03) saw 4 straight 0% win-rate days driving most of
    the month's -$4,660 loss. The existing dyn SL (-$500) kicked in but wasn't
    tight enough. Tests whether a tighter per-trade SL on extreme low-VIX days
    improves risk-adjusted returns without hurting the wider backtest.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX SUB-12 TIGHTER SL SWEEP")
    logger.info(f"VIX threshold : VIX < {VIX_SUB12_THRESHOLD}")
    logger.info(f"Levels        : {VIX_SUB12_SL_LEVELS}")
    logger.info(f"Output        : {VIX_SUB12_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days --
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    vix_sub12_days = sum(
        1 for d_str, dd in day_pool.items()
        if (dd.get("vix_level") or 99) < VIX_SUB12_THRESHOLD
    )
    logger.info(f"VIX < {VIX_SUB12_THRESHOLD} days in pool: {vix_sub12_days}")

    cols = ["vix_sub12_sl", "vix_sub12_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in VIX_SUB12_SL_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                vix = day_data.get("vix_level") or 99
                is_sub12 = vix < VIX_SUB12_THRESHOLD

                if is_sub12 and sl_level is not None:
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    # use whichever is tighter (less negative = closer to zero = triggers sooner)
                    effective_sl = max(sl_level, dyn_sl) if dyn_sl is not None else sl_level
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "vix_sub12_sl":   label,
                "vix_sub12_days": vix_sub12_days,
                "num_trades":     m["num_trades"],
                "win_rate_pct":   f"{m['win_rate']:.1f}",
                "total_pnl":      f"{pnl:.2f}",
                "pnl_delta":      pnl_delta,
                "max_drawdown":   f"{dd:.2f}",
                "dd_delta":       dd_delta,
                "calmar":         f"{calmar:.2f}",
                "sharpe":         f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  vix_sub12_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(VIX_SUB12_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  VIX SUB-12 SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['vix_sub12_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {VIX_SUB12_SL_SWEEP_FILE}")


# ---------------------------------------------
#  EOM SL SWEEP RUNNER
# ---------------------------------------------
async def run_eom_sl_sweep():
    """Sweep tighter SL levels applied exclusively on the last trading day of each month.

    EOM days have 74% WR and $105/day avg vs 93%+ WR and $618/day for normal days.
    The 4 largest single-day losses all fell on EOM dates. Tests whether a tighter
    daily SL on EOM days improves risk-adjusted returns without hurting normal days.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: EOM SL SWEEP")
    logger.info(f"EOM days      : {len(_EOM_DATES)}")
    logger.info(f"Levels        : {EOM_SL_SWEEP_LEVELS}")
    logger.info(f"Output        : {EOM_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    eom_in_pool = sum(1 for d_str in day_pool if d_str in _EOM_DATES)
    logger.info(f"EOM days in pool: {eom_in_pool}")

    cols = ["eom_sl", "eom_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in EOM_SL_SWEEP_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                is_eom = d_str in _EOM_DATES

                if is_eom and sl_level is not None:
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    effective_sl = max(sl_level, dyn_sl) if dyn_sl is not None else sl_level
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "eom_sl":       label,
                "eom_days":     eom_in_pool,
                "num_trades":   m["num_trades"],
                "win_rate_pct": f"{m['win_rate']:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  eom_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(EOM_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  EOM SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['eom_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {EOM_SL_SWEEP_FILE}")


async def run_per_pos_sl_sweep():
    """Sweep per-position fixed SL thresholds to test Option 3d.

    Closes an individual position when its own MTM loss exceeds the threshold,
    independent of total daily P&L. Tests whether capping single-position losses
    (e.g. the -$2,834 position on 2023-10-09) improves overall metrics.
    None = baseline (no per-position SL).
    """
    logger.info("=" * 70)
    logger.info("MEDS: PER-POSITION FIXED SL SWEEP")
    logger.info(f"Levels        : {PER_POS_SL_SWEEP_LEVELS}")
    logger.info(f"Output        : {PER_POS_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["per_pos_sl", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in PER_POS_SL_SWEEP_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    per_pos_sl=sl_level,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "per_pos_sl":   label,
                "num_trades":   len(all_trades),
                "win_rate_pct": f"{wr:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(f"  SL={label:>6}  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(PER_POS_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['per_pos_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {PER_POS_SL_SWEEP_FILE}")


# ---------------------------------------------
#  SL GAP RE-ENTRY SWEEP RUNNER
# ---------------------------------------------
async def run_sl_gap_sweep():
    """Sweep SL gap re-entry cooldown periods: None (baseline), 30, 60, 90, 120 minutes.

    None = current behaviour (stopped_today forever after batch SL).
    30/60/90/120 = wait that many minutes after SL fires, then allow re-entry.
    """
    logger.info("=" * 70)
    logger.info("MEDS: SL GAP RE-ENTRY SWEEP")
    logger.info(f"Levels (min)  : {SL_GAP_SWEEP_LEVELS}  (None=baseline/no-reentry)")
    logger.info(f"Output        : {SL_GAP_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["gap_minutes", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for gap in SL_GAP_SWEEP_LEVELS:
            label = "none" if gap is None else str(gap)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    sl_gap_minutes=gap,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "gap_minutes":  label,
                "num_trades":   len(all_trades),
                "win_rate_pct": f"{wr:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(f"  gap={label:>4} min  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(SL_GAP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info(f"  {'GAP(min)':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['gap_minutes']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {SL_GAP_SWEEP_FILE}")


# ---------------------------------------------
#  VIX-RANGE ENTRY CUTOFF SWEEP RUNNER
# ---------------------------------------------
async def run_vix_entry_cutoff_sweep():
    """Sweep earlier entry window cutoffs applied only on VIX LO-HI days (Option 3b).

    On VIX 15-20 days the market often drifts with positions during the morning entry
    window then reverses in the afternoon. Tests whether stopping new entries earlier
    on those days reduces late-day losses without hurting the rest of the backtest.
    Non-VIX-15-20 days always use the global ENTRY_END (12:45).
    None = use global ENTRY_END for VIX 15-20 days too (baseline).
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX-RANGE ENTRY CUTOFF SWEEP")
    logger.info(f"VIX range     : {VIX_ENTRY_CUTOFF_VIX_LO}-{VIX_ENTRY_CUTOFF_VIX_HI}")
    logger.info(f"Cutoff times  : {[t.strftime('%H:%M') if t else 'None' for t in VIX_ENTRY_CUTOFF_SWEEP_TIMES]}")
    logger.info(f"Output        : {VIX_ENTRY_CUTOFF_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["cutoff_time", "vix_days_affected", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for cutoff in VIX_ENTRY_CUTOFF_SWEEP_TIMES:
            label = cutoff.strftime("%H:%M") if cutoff else "none"
            all_trades: list = []
            vix_days_affected = 0

            for d_str, day_data in day_pool.items():
                vix_level = day_data.get("vix_level")
                in_vix_range = (
                    vix_level is not None
                    and VIX_ENTRY_CUTOFF_VIX_LO <= vix_level < VIX_ENTRY_CUTOFF_VIX_HI
                )
                effective_entry_end = cutoff if (cutoff is not None and in_vix_range) else None
                if in_vix_range and cutoff is not None:
                    vix_days_affected += 1

                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    entry_end=effective_entry_end,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "cutoff_time":       label,
                "vix_days_affected": vix_days_affected,
                "num_trades":        len(all_trades),
                "win_rate_pct":      f"{wr:.1f}",
                "total_pnl":         f"{pnl:.2f}",
                "pnl_delta":         pnl_delta,
                "max_drawdown":      f"{dd:.2f}",
                "dd_delta":          dd_delta,
                "calmar":            f"{calmar:.2f}",
                "sharpe":            f"{m['sharpe']:.2f}",
            })
            logger.info(f"  Cutoff={label}  vix_days={vix_days_affected}  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(VIX_ENTRY_CUTOFF_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 108
    logger.info("")
    logger.info("═" * 108)
    logger.info(f"  {'CUTOFF':>7} | {'VIX DAYS':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['cutoff_time']:>7} | {row['vix_days_affected']:>8} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>4}% | ${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 108)
    logger.info(f"  Full results: {VIX_ENTRY_CUTOFF_SWEEP_FILE}")


# ---------------------------------------------
#  CALENDAR RISK SL SWEEP RUNNER
# ---------------------------------------------
async def run_calendar_risk_sl_sweep():
    """Test each recurring-date SL category independently across SL levels.

    For each category (CPI, PCE, EOQ, pre-TW, post-holiday), runs every level in
    CALENDAR_RISK_SL_SWEEP_LEVELS with all OTHER calendar risk SLs disabled, so
    the P&L delta shows the isolated impact of that one category.
    Finishes with a combined row using the best level found for each category.
    """
    global ENABLE_CPI_SL, CPI_SL_AMOUNT
    global ENABLE_PCE_SL, PCE_SL_AMOUNT
    global ENABLE_EOQ_SL, EOQ_SL_AMOUNT
    global ENABLE_PRE_TW_SL, PRE_TW_SL_AMOUNT
    global ENABLE_POST_HOL_SL, POST_HOL_SL_AMOUNT

    logger.info("=" * 110)
    logger.info("MEDS: CALENDAR RISK SL SWEEP")
    logger.info(f"Levels: {CALENDAR_RISK_SL_SWEEP_LEVELS}")
    logger.info(f"Output: {CALENDAR_RISK_SL_SWEEP_FILE}")
    logger.info("=" * 110)

    # Disable all calendar risk SLs during sweep -- each test enables only one
    ENABLE_CPI_SL = ENABLE_PCE_SL = ENABLE_EOQ_SL = False
    ENABLE_PRE_TW_SL = ENABLE_POST_HOL_SL = False

    # Pre-fetch day pool
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    # Category definitions: (label, date_set, enable_setter, amount_setter)
    categories = [
        ("CPI",       _CPI_DATES,      "_cpi"),
        ("PCE",       _PCE_DATES,      "_pce"),
        ("EOQ",       _EOQ_DATES,      "_eoq"),
        ("pre-TW",    _PRE_TW_DATES,   "_pretw"),
        ("post-hol",  _POST_HOL_DATES, "_posthol"),
    ]

    cols = ["category", "sl_level", "cat_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []

    async with _get_session() as session:

        # -- Baseline: all calendar risk SLs off ------------------------------
        base_trades: list = []
        for d_str, day_data in day_pool.items():
            effective_sl = _get_effective_sl(day_data, d_str)
            direction    = _get_baseline_mode(d_str)
            trades, _    = await _simulate_day(
                session, day_data, effective_sl,
                baseline_mode=direction,
                pos_trail_activation=POS_TRAIL_ACTIVATION,
                pos_trail_pullback=POS_TRAIL_PULLBACK,
                min_otm_distance=MIN_OTM_DISTANCE,
                max_credit=MAX_NET_CREDIT,
                pnl_sample_interval=PNL_SAMPLE_INTERVAL,
            )
            base_trades.extend(trades)
        bm       = compute_metrics(base_trades)
        base_pnl = bm["total_pnl"]
        base_dd  = bm["max_drawdown"]
        base_cal = base_pnl / abs(base_dd) if base_dd != 0 else float("inf")
        rows.append({
            "category": "BASELINE", "sl_level": "--", "cat_days": "--",
            "num_trades": bm["num_trades"], "win_rate_pct": f"{bm['win_rate']:.1f}",
            "total_pnl": f"{base_pnl:.2f}", "pnl_delta": "--",
            "max_drawdown": f"{base_dd:.2f}", "dd_delta": "--",
            "calmar": f"{base_cal:.2f}", "sharpe": f"{bm['sharpe']:.2f}",
        })
        logger.info(f"  {'BASELINE':>10} | sl=    -- | pnl=${base_pnl:>10,.2f} |  -- |"
                    f" dd=${base_dd:>9,.2f} |  -- | calmar={base_cal:.2f} | sharpe={bm['sharpe']:.2f}")

        # -- Per-category sweep ------------------------------------------------
        best_level: dict[str, "float|None"] = {}

        for cat_label, cat_dates, cat_key in categories:
            cat_days = sum(1 for d in day_pool if d in cat_dates)
            best_pnl = base_pnl
            best_lv  = None
            logger.info(f"  --- {cat_label} ({cat_days} days) ---")

            for sl_level in CALENDAR_RISK_SL_SWEEP_LEVELS:
                # Enable only this category at this level
                ENABLE_CPI_SL      = (cat_key == "_cpi"    and sl_level is not None)
                ENABLE_PCE_SL      = (cat_key == "_pce"    and sl_level is not None)
                ENABLE_EOQ_SL      = (cat_key == "_eoq"    and sl_level is not None)
                ENABLE_PRE_TW_SL   = (cat_key == "_pretw"  and sl_level is not None)
                ENABLE_POST_HOL_SL = (cat_key == "_posthol" and sl_level is not None)
                if cat_key == "_cpi":      CPI_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_pce":    PCE_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_eoq":    EOQ_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_pretw":  PRE_TW_SL_AMOUNT   = sl_level or -300.0
                elif cat_key == "_posthol":POST_HOL_SL_AMOUNT = sl_level or -300.0

                all_trades: list = []
                for d_str, day_data in day_pool.items():
                    effective_sl = _get_effective_sl(day_data, d_str)
                    in_danger    = effective_sl is not None
                    sample_int   = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                    direction    = _get_baseline_mode(d_str)
                    trades, _    = await _simulate_day(
                        session, day_data, effective_sl,
                        baseline_mode=direction,
                        pos_trail_activation=POS_TRAIL_ACTIVATION,
                        pos_trail_pullback=POS_TRAIL_PULLBACK,
                        min_otm_distance=MIN_OTM_DISTANCE,
                        max_credit=MAX_NET_CREDIT,
                        pnl_sample_interval=sample_int,
                    )
                    all_trades.extend(trades)

                m      = compute_metrics(all_trades)
                pnl    = m["total_pnl"]
                dd     = m["max_drawdown"]
                calmar = pnl / abs(dd) if dd != 0 else float("inf")
                label  = "none" if sl_level is None else str(sl_level)
                rows.append({
                    "category":    cat_label, "sl_level": label, "cat_days": cat_days,
                    "num_trades":  m["num_trades"], "win_rate_pct": f"{m['win_rate']:.1f}",
                    "total_pnl":   f"{pnl:.2f}",  "pnl_delta":  f"{pnl - base_pnl:+.2f}",
                    "max_drawdown": f"{dd:.2f}",   "dd_delta":   f"{dd - base_dd:+.2f}",
                    "calmar":      f"{calmar:.2f}", "sharpe":     f"{m['sharpe']:.2f}",
                })
                logger.info(
                    f"  {cat_label:>10} | sl={label:>6} | pnl=${pnl:>10,.2f} | delta={pnl-base_pnl:>+9,.2f} |"
                    f" dd=${dd:>9,.2f} | dd_delta={dd-base_dd:>+8,.2f} | calmar={calmar:.2f}"
                )
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_lv  = sl_level

            best_level[cat_key] = best_lv

        # -- Combined: best level per category --------------------------------
        ENABLE_CPI_SL      = best_level["_cpi"]     is not None
        ENABLE_PCE_SL      = best_level["_pce"]     is not None
        ENABLE_EOQ_SL      = best_level["_eoq"]     is not None
        ENABLE_PRE_TW_SL   = best_level["_pretw"]   is not None
        ENABLE_POST_HOL_SL = best_level["_posthol"] is not None
        if best_level["_cpi"]:      CPI_SL_AMOUNT      = best_level["_cpi"]
        if best_level["_pce"]:      PCE_SL_AMOUNT      = best_level["_pce"]
        if best_level["_eoq"]:      EOQ_SL_AMOUNT      = best_level["_eoq"]
        if best_level["_pretw"]:    PRE_TW_SL_AMOUNT   = best_level["_pretw"]
        if best_level["_posthol"]:  POST_HOL_SL_AMOUNT = best_level["_posthol"]

        combo_trades: list = []
        for d_str, day_data in day_pool.items():
            effective_sl = _get_effective_sl(day_data, d_str)
            in_danger    = effective_sl is not None
            sample_int   = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
            direction    = _get_baseline_mode(d_str)
            trades, _    = await _simulate_day(
                session, day_data, effective_sl,
                baseline_mode=direction,
                pos_trail_activation=POS_TRAIL_ACTIVATION,
                pos_trail_pullback=POS_TRAIL_PULLBACK,
                min_otm_distance=MIN_OTM_DISTANCE,
                max_credit=MAX_NET_CREDIT,
                pnl_sample_interval=sample_int,
            )
            combo_trades.extend(trades)

        cm     = compute_metrics(combo_trades)
        cpnl   = cm["total_pnl"]
        cdd    = cm["max_drawdown"]
        ccal   = cpnl / abs(cdd) if cdd != 0 else float("inf")
        combo_label = " + ".join(
            f"{k}={v}" for k, v in [
                ("CPI", best_level["_cpi"]), ("PCE", best_level["_pce"]),
                ("EOQ", best_level["_eoq"]), ("preTW", best_level["_pretw"]),
                ("postHol", best_level["_posthol"]),
            ] if v is not None
        ) or "none"
        rows.append({
            "category": "COMBINED", "sl_level": combo_label, "cat_days": "--",
            "num_trades": cm["num_trades"], "win_rate_pct": f"{cm['win_rate']:.1f}",
            "total_pnl": f"{cpnl:.2f}", "pnl_delta": f"{cpnl - base_pnl:+.2f}",
            "max_drawdown": f"{cdd:.2f}", "dd_delta": f"{cdd - base_dd:+.2f}",
            "calmar": f"{ccal:.2f}", "sharpe": f"{cm['sharpe']:.2f}",
        })
        logger.info(f"  {'COMBINED':>10} | {combo_label} |"
                    f" pnl=${cpnl:>10,.2f} | delta={cpnl-base_pnl:>+9,.2f} |"
                    f" dd=${cdd:>9,.2f} | calmar={ccal:.2f}")

    # Save CSV
    with open(CALENDAR_RISK_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    W = 115
    logger.info("")
    logger.info("═" * W)
    logger.info("  CALENDAR RISK SL SWEEP RESULTS")
    logger.info("═" * W)
    logger.info(f"  {'CATEGORY':>10} | {'SL':>7} | {'DAYS':>5} | {'TRADES':>7} | {'WIN%':>5} | "
                f"{'TOTAL_PNL':>12} | {'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>9} | "
                f"{'CALMAR':>8} | {'SHARPE':>7}")
    logger.info("-" * W)
    for row in rows:
        logger.info(
            f"  {row['category']:>10} | {row['sl_level']:>7} | {str(row['cat_days']):>5} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>9} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * W)
    logger.info(f"  Full results: {CALENDAR_RISK_SL_SWEEP_FILE}")


# ---------------------------------------------
#  CALL-SIDE SL SWEEP RUNNER
# ---------------------------------------------
async def run_call_sl_sweep():
    """Sweep stop-loss levels applied exclusively on CALL spread days.

    PUT days continue to use _get_effective_sl() (dynamic VIX-based SL) unchanged.
    For each CALL_SL level, the full date range is re-simulated from pre-fetched data.
    A tighter CALL SL reduces drawdown but may also cut profitable CALL days early.
    """
    logger.info("=" * 70)
    logger.info("MEDS: CALL-SIDE SL SWEEP")
    logger.info(f"Levels  : {CALL_SL_SWEEP_LEVELS}")
    logger.info(f"Output  : {CALL_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days once --
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["call_sl_level", "num_trades", "win_rate_pct", "total_pnl",
            "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe", "sortino"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for call_sl in CALL_SL_SWEEP_LEVELS:
            label = "none" if call_sl is None else str(call_sl)

            all_trades: list = []
            for d_str, day_data in day_pool.items():
                direction = _get_baseline_mode(d_str)
                if direction == "always_call":
                    # Apply the test CALL SL level directly on CALL days
                    effective_sl = call_sl
                else:
                    # PUT days: use standard dynamic SL logic unchanged
                    effective_sl = _get_effective_sl(day_data, d_str)

                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                )
                all_trades.extend(trades)

            m        = compute_metrics(all_trades)
            pnl      = m["total_pnl"]
            dd       = m["max_drawdown"]
            calmar   = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl = pnl
                base_dd  = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "call_sl_level": label,
                "num_trades":    m["num_trades"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{pnl:.2f}",
                "pnl_delta":     pnl_delta,
                "max_drawdown":  f"{dd:.2f}",
                "dd_delta":      dd_delta,
                "calmar":        f"{calmar:.2f}",
                "sharpe":        f"{m['sharpe']:.2f}",
                "sortino":       f"{m['sortino']:.2f}",
            })
            logger.info(
                f"  call_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    # Save CSV
    with open(CALL_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Print summary table
    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info("  CALL-SIDE SL SWEEP RESULTS")
    logger.info("═" * 100)
    logger.info(f"  {'CALL_SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['call_sl_level']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {CALL_SL_SWEEP_FILE}")


# ---------------------------------------------
#  PRESSURE FILTER VIX SWEEP RUNNER
# ---------------------------------------------
async def run_pressure_vix_sweep():
    """Sweep pressure filter with VIX-conditional gating.

    Row 0 (baseline): pressure filter disabled -- current confirmed config.
    Row 1 (threshold=None): filter active at all VIX levels -- previously -$50k globally.
    Rows 2-N: filter active only when VIX >= threshold (20, 22, 25, 27, 28, 30).

    Goal: find if restricting the filter to high-VIX days recovers P&L while
    still cutting drawdown in the dangerous VIX 25-30 zone.
    """
    logger.info("=" * 70)
    logger.info("MEDS: PRESSURE FILTER VIX SWEEP")
    logger.info(f"Thresholds    : {PRESSURE_VIX_SWEEP_THRESHOLDS}  (None = all VIX levels)")
    logger.info(f"Distance      : {PRESSURE_DISTANCE_THRESHOLD} pts")
    logger.info(f"Output        : {PRESSURE_VIX_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["label", "pressure_enabled", "vix_min", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    # Build sweep list: (label, enable_filter, vix_min)
    sweep = [("baseline (off)", False, None)] + [
        (f"vix>={t}" if t is not None else "all VIX", True, t)
        for t in PRESSURE_VIX_SWEEP_THRESHOLDS
    ]

    async with _get_session() as session:
        for label, enable_filter, vix_min in sweep:
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)

                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                    enable_pressure_filter=enable_filter,
                    pressure_vix_min=vix_min,
                    pressure_vix_max=None,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "label":         label,
                "pressure_enabled": str(enable_filter),
                "vix_min":       str(vix_min) if vix_min is not None else "all" if enable_filter else "off",
                "num_trades":    m["num_trades"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{pnl:.2f}",
                "pnl_delta":     pnl_delta,
                "max_drawdown":  f"{dd:.2f}",
                "dd_delta":      dd_delta,
                "calmar":        f"{calmar:.2f}",
                "sharpe":        f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  {label:<18} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(PRESSURE_VIX_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 110
    logger.info("")
    logger.info("═" * 110)
    logger.info("  PRESSURE FILTER VIX SWEEP RESULTS")
    logger.info("═" * 110)
    logger.info(f"  {'LABEL':<18} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['label']:<18} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 110)
    logger.info(f"  Full results: {PRESSURE_VIX_SWEEP_FILE}")


# ---------------------------------------------
#  TRAILING STOP SWEEP RUNNER
# ---------------------------------------------
def _ts_label(ts) -> str:
    """Human-readable label for a trailing stop level (None -> 'none')."""
    return "none" if ts is None else str(int(ts))


async def run_trailing_stop_sweep():
    """Run a trailing stop parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every level in SWEEP_TS_LEVELS using the
    in-memory quote cache -- no extra API calls are needed.

    The regular DAILY_SL remains active alongside the trailing stop so that
    trades that never go positive are still protected by the hard stop.

    Results are printed to console and saved to TRAILING_STOP_SWEEP_FILE.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: TRAILING STOP SWEEP MODE")
    logger.info(f"TS levels : {[_ts_label(ts) for ts in SWEEP_TS_LEVELS]}")
    logger.info(f"Daily SL  : {DAILY_SL}  (applied alongside trailing stop)")
    logger.info(f"Output    : {TRAILING_STOP_SWEEP_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per trailing stop level
    ts_trades: dict[str, list] = {_ts_label(ts): [] for ts in SWEEP_TS_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each trailing stop level using the cached quotes
            for ts in SWEEP_TS_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, trailing_stop=ts)
                label = _ts_label(ts)
                ts_trades[label].extend(trades)
                logger.debug(
                    f"  TS={label:>6} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Write summary CSV --
    ts_sweep_cols = [
        "trailing_stop", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    rows = []
    for ts in SWEEP_TS_LEVELS:
        label = _ts_label(ts)
        m     = compute_metrics(ts_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "trailing_stop":  label,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
        })

    with open(TRAILING_STOP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ts_sweep_cols)
        w.writeheader()
        w.writerows(rows)

    logger.info(f"\nTrailing stop sweep complete -- summary saved to {TRAILING_STOP_SWEEP_FILE}")

    # -- Print comparison table --
    col_w = 10
    hdr   = (f"{'Trail $':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TRAILING STOP SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"{row['trailing_stop']:>{col_w}} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {pf:>6}"
        )
    logger.info(sep)


# ---------------------------------------------
#  PER-POSITION TRAILING STOP SWEEP RUNNER
# ---------------------------------------------
async def run_pos_trail_sweep():
    """Sweep all (activation%, pullback%) combos for the per-position trailing stop.

    A position's trail activates once it has profited >= activation% of credit;
    it then closes if P&L pulls back >= pullback% of credit from the position's
    peak.  Market data is fetched once per day; every combo re-simulates from
    the cache.  Results are sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')
    combos    = [(a, p) for a in SWEEP_POS_TRAIL_ACTIVATION for p in SWEEP_POS_TRAIL_PULLBACK]

    logger.info("=" * 70)
    logger.info("MEFT v35: PER-POSITION TRAILING STOP SWEEP")
    logger.info(f"Activation : {SWEEP_POS_TRAIL_ACTIVATION}% of credit")
    logger.info(f"Pullback   : {SWEEP_POS_TRAIL_PULLBACK}% of credit")
    logger.info(f"Combos     : {len(combos)}")
    logger.info(f"Output     : {POS_TRAIL_SWEEP_FILE}")
    logger.info("=" * 70)

    pos_trail_trades: dict[tuple, list] = {(a, p): [] for a, p in combos}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for act, pull in combos:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    pos_trail_activation=act,
                    pos_trail_pullback=pull,
                )
                pos_trail_trades[(act, pull)].extend(trades)
                logger.debug(
                    f"  act={act}% pull={pull}% | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows --
    pt_cols = [
        "activation_pct", "pullback_pct",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    rows = []
    for act, pull in combos:
        m  = compute_metrics(pos_trail_trades[(act, pull)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "activation_pct":  act,
            "pullback_pct":    pull,
            "num_trades":      m["num_trades"],
            "win_rate_pct":    f"{m['win_rate']:.1f}",
            "total_pnl":       f"{m['total_pnl']:.2f}",
            "avg_win":         f"{m['avg_win']:.2f}",
            "avg_loss":        f"{m['avg_loss']:.2f}",
            "profit_factor":   pf,
            "max_drawdown":    f"{m['max_drawdown']:.2f}",
            "_total_pnl_num":  m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV --
    with open(POS_TRAIL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pt_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in pt_cols})

    logger.info(f"\nPos trail sweep complete -- saved to {POS_TRAIL_SWEEP_FILE}")

    # -- Print table --
    hdr = (
        f"  {'Act%':>5} | {'Pull%':>5} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PER-POSITION TRAIL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['activation_pct']:>5} | {row['pullback_pct']:>5} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  SPREAD WIDTH SWEEP RUNNER
# ---------------------------------------------
async def run_spread_width_sweep():
    """Run a spread width parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every width in SWEEP_SPREAD_WIDTHS using the
    in-memory quote cache -- no extra API calls are needed.

    Results are printed to console and saved to SPREAD_WIDTH_SWEEP_FILE,
    sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: SPREAD WIDTH SWEEP MODE")
    logger.info(f"Widths    : {SWEEP_SPREAD_WIDTHS}")
    logger.info(f"Output    : {SPREAD_WIDTH_SWEEP_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per width
    width_trades: dict[int, list] = {w: [] for w in SWEEP_SPREAD_WIDTHS}

    init_log_file()  # reset meft_v35_bidask_log.csv for the default WIDTH run

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each width using the cached quotes
            for w in SWEEP_SPREAD_WIDTHS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, spread_width=float(w))
                width_trades[w].extend(trades)
                # Write the default-width trades to the main trade log
                if w == WIDTH:
                    for pos in trades:
                        append_trade(pos)
                logger.debug(
                    f"  Width={w:>3} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows --
    sw_sweep_cols = [
        "spread_width", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]

    rows = []
    for w in SWEEP_SPREAD_WIDTHS:
        trades = width_trades[w]
        m      = compute_metrics(trades)
        pf     = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in trades]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        rows.append({
            "spread_width":        w,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            # numeric key for sorting
            "_total_pnl_num":      m["total_pnl"],
        })

    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV (drop sort key) --
    with open(SPREAD_WIDTH_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sw_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in sw_sweep_cols})

    logger.info(f"\nSpread width sweep complete -- summary saved to {SPREAD_WIDTH_SWEEP_FILE}")

    # -- Print comparison table --
    hdr = (
        f"  {'Width':>6} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  SPREAD WIDTH SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"  {row['spread_width']:>6} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{pf:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)

    # -- Full performance report for the default WIDTH --
    default_trades = width_trades.get(WIDTH, [])
    if default_trades:
        logger.info(f"\n  Full performance report for default width={WIDTH}:")
        print_performance_report(default_trades, date_list)


# ---------------------------------------------
#  NAIVE BASELINE COMPARISON
# ---------------------------------------------
_BASELINES = [
    ("EMA Strategy",   None),
    ("Always PUT",     "always_put"),
    ("Always CALL",    "always_call"),
    ("No EMA Filter",  "no_ema_filter"),
    ("Both Sides",     "both_sides"),
]


async def run_baseline_comparison():
    """Re-run the full date range for the main strategy and three naive baselines.

    Market data is fetched once per day; each baseline re-simulates from the cache.
    Results are printed to console and saved to BASELINE_COMPARISON_FILE.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: NAIVE BASELINE COMPARISON")
    logger.info(f"Baselines : {[name for name, _ in _BASELINES]}")
    logger.info(f"Output    : {BASELINE_COMPARISON_FILE}")
    logger.info("=" * 70)

    baseline_trades: dict[str, list] = {name: [] for name, _ in _BASELINES}
    spx_daily: list[dict] = []   # {date, close} for SPX benchmark

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            # Fetch market data once (re-populates _quote_cache for this day)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Collect SPX close for benchmark
            ind = _DAILY_INDICATORS.get(d_str)
            if ind and ind.get("close"):
                spx_daily.append({"date": d_str, "close": ind["close"]})

            # Simulate once per baseline using the shared cache
            for name, mode in _BASELINES:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                baseline_trades[name].extend(trades)
                logger.debug(f"  [{name}] {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build comparison rows --
    comp_cols = [
        "strategy", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
        "p_value", "p_fdr", "sig_fdr",
    ]
    rows = []
    for name, _ in _BASELINES:
        m = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "strategy":       name,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "p_value":        round(m["p_value"], 4),
        })
    _apply_fdr_to_rows(rows)

    # -- SPX buy-and-hold benchmark --
    spx_row = None
    if len(spx_daily) >= 2:
        import math as _math
        spx_closes  = [r["close"] for r in sorted(spx_daily, key=lambda x: x["date"])]
        spx_returns = [(spx_closes[i] - spx_closes[i-1]) / spx_closes[i-1]
                       for i in range(1, len(spx_closes))]
        rf_daily_r  = ANNUAL_RISK_FREE_RATE / 252
        n_spx       = len(spx_returns)
        mean_r      = sum(spx_returns) / n_spx
        var_r       = sum((r - mean_r) ** 2 for r in spx_returns) / n_spx
        spx_sharpe  = ((mean_r - rf_daily_r) / _math.sqrt(var_r) * _math.sqrt(252)) if var_r else 0
        down_r      = [r for r in spx_returns if r < mean_r]
        down_var_r  = sum((r - mean_r) ** 2 for r in down_r) / len(down_r) if down_r else 0
        spx_sortino = ((mean_r - rf_daily_r) / _math.sqrt(down_var_r) * _math.sqrt(252)) if down_var_r else 0
        spx_total   = (spx_closes[-1] / spx_closes[0] - 1) * 100
        n_years     = n_spx / 252
        spx_ann     = ((spx_closes[-1] / spx_closes[0]) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0
        # Max drawdown on SPX price curve
        spx_peak = spx_dd = 0.0
        for c in spx_closes:
            spx_peak = max(spx_peak, c)
            spx_dd   = min(spx_dd, (c - spx_peak) / spx_peak * 100)
        spx_row = {
            "total_return_pct": round(spx_total, 2),
            "ann_return_pct":   round(spx_ann, 2),
            "sharpe":           round(spx_sharpe, 3),
            "sortino":          round(spx_sortino, 3),
            "max_drawdown_pct": round(spx_dd, 2),
            "start":            spx_daily[0]["date"],
            "end":              spx_daily[-1]["date"],
            "start_price":      round(spx_closes[0], 2),
            "end_price":        round(spx_closes[-1], 2),
        }

    # -- Save CSV --
    with open(BASELINE_COMPARISON_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=comp_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"Baseline comparison saved to {BASELINE_COMPARISON_FILE}")

    # -- Print comparison table --
    sep = "-" * 90
    hdr = (f"  {'Strategy':<18} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Profit Factor':>14} | {'Max DD':>10}")
    logger.info(sep)
    logger.info(f"  NAIVE BASELINE COMPARISON  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, acct=${ACCOUNT_SIZE:,.0f})")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for name, _ in _BASELINES:
        m  = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"  {name:<18} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"{pf:>14} | ${m['max_drawdown']:>9,.2f}"
        )
    logger.info(sep)

    # -- Print SPX benchmark --
    if spx_row:
        logger.info("")
        logger.info(f"  SPX BUY-AND-HOLD BENCHMARK  "
                    f"({spx_row['start']} -> {spx_row['end']} | "
                    f"{spx_row['start_price']:,.0f} -> {spx_row['end_price']:,.0f})")
        logger.info(sep)
        logger.info(f"  Total return    : {spx_row['total_return_pct']:>8.1f}%")
        logger.info(f"  Ann. return     : {spx_row['ann_return_pct']:>8.1f}%  (CAGR)")
        logger.info(f"  Sharpe ratio    : {spx_row['sharpe']:>8.3f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, daily returns)")
        logger.info(f"  Sortino ratio   : {spx_row['sortino']:>8.3f}")
        logger.info(f"  Max drawdown    : {spx_row['max_drawdown_pct']:>8.1f}%  (price-based)")
        logger.info(sep)


# ---------------------------------------------
#  MASTER SWEEP RUNNER
# ---------------------------------------------
async def run_master_sweep():
    """Run ALL sweep variants in a single pass.

    For each trading day:
      1. Market data is fetched exactly once (seeded for the largest EMA period).
      2. Every sweep variant is re-simulated against the in-memory quote cache --
         no extra API calls.

    Sweep variants:
      • Stop-loss sweep              (SWEEP_SL_LEVELS)
      • EMA parameter sweep          (SWEEP_EMA_FAST × SWEEP_EMA_SLOW)
      • Trailing stop sweep          (SWEEP_TS_LEVELS)
      • Spread width sweep           (SWEEP_SPREAD_WIDTHS)
      • Per-position trailing stop   (SWEEP_POS_TRAIL_ACTIVATION × SWEEP_POS_TRAIL_PULLBACK)
      • Naive baseline comparison    (_BASELINES)

    Default-width trades are written to meft_v35_bidask_log.csv as usual.
    """
    date_list   = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq='B')
    ema_combos  = [(f, s) for f in SWEEP_EMA_FAST for s in SWEEP_EMA_SLOW]
    pt_combos   = [(a, p) for a in SWEEP_POS_TRAIL_ACTIVATION for p in SWEEP_POS_TRAIL_PULLBACK]
    max_seed    = max(SWEEP_EMA_SLOW)

    logger.info("=" * 70)
    logger.info("MEFT v35: MASTER SWEEP MODE")
    logger.info(f"SL levels : {[_sl_label(sl) for sl in SWEEP_SL_LEVELS]}")
    logger.info(f"EMA combos: {len(ema_combos)}  ({SWEEP_EMA_FAST} × {SWEEP_EMA_SLOW})")
    logger.info(f"TS levels : {[_ts_label(ts) for ts in SWEEP_TS_LEVELS]}")
    logger.info(f"Widths    : {SWEEP_SPREAD_WIDTHS}")
    logger.info(f"Pos trail : {len(pt_combos)}  (act={SWEEP_POS_TRAIL_ACTIVATION} × pull={SWEEP_POS_TRAIL_PULLBACK})")
    logger.info(f"Min credit: {SWEEP_MIN_CREDIT_LEVELS}")
    logger.info(f"Baselines : {[name for name, _ in _BASELINES]}")
    logger.info("=" * 70)

    # -- Accumulators --
    sl_trades:       dict[str,   list] = {_sl_label(sl):   [] for sl in SWEEP_SL_LEVELS}
    ema_trades:      dict[tuple, list] = {(f, s):           [] for f, s in ema_combos}
    ts_trades:       dict[str,   list] = {_ts_label(ts):    [] for ts in SWEEP_TS_LEVELS}
    width_trades:    dict[float, list] = {float(w):         [] for w in SWEEP_SPREAD_WIDTHS}
    pt_trades:       dict[tuple, list] = {(a, p):           [] for a, p in pt_combos}
    mc_trades:       dict[float, list] = {mc:               [] for mc in SWEEP_MIN_CREDIT_LEVELS}
    baseline_trades: dict[str,   list] = {name:             [] for name, _ in _BASELINES}

    init_log_file()  # reset meft_v35_bidask_log.csv

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # -- Fetch market data once per day --
            clear_day_cache()
            day_data = await _fetch_day_data(session, d_str, seed_bars=max_seed)
            if day_data is None:
                continue

            logger.info(
                f"Day {i+1:>3}/{len(date_list)} | {d_str} -- running all sweep variants..."
            )

            # SL sweep
            for sl in SWEEP_SL_LEVELS:
                trades, _ = await _simulate_day(session, day_data, sl)
                sl_trades[_sl_label(sl)].extend(trades)

            # EMA sweep
            for fast, slow in ema_combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, ema_fast=fast, ema_slow=slow
                )
                ema_trades[(fast, slow)].extend(trades)

            # Trailing stop sweep
            for ts in SWEEP_TS_LEVELS:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, trailing_stop=ts
                )
                ts_trades[_ts_label(ts)].extend(trades)

            # Spread width sweep (write default-width trades to main log)
            for w in SWEEP_SPREAD_WIDTHS:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, spread_width=float(w)
                )
                width_trades[float(w)].extend(trades)
                if float(w) == float(WIDTH):
                    for pos in trades:
                        append_trade(pos)

            # Per-position trailing stop sweep
            for act, pull in pt_combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    pos_trail_activation=act, pos_trail_pullback=pull,
                )
                pt_trades[(act, pull)].extend(trades)

            # Min credit sweep
            for mc in SWEEP_MIN_CREDIT_LEVELS:
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, min_credit=mc)
                mc_trades[mc].extend(trades)

            # Baseline comparison
            for name, mode in _BASELINES:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, baseline_mode=mode
                )
                baseline_trades[name].extend(trades)

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- SL SWEEP
    # ════════════════════════════════════════════════════════════
    sweep_cols = [
        "stop_loss", "num_trades", "num_wins", "num_losses",
        "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    with open(SWEEP_SAVE_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=sweep_cols)
        w_csv.writeheader()
        for sl in SWEEP_SL_LEVELS:
            label = _sl_label(sl)
            m     = compute_metrics(sl_trades[label])
            pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
            w_csv.writerow({
                "stop_loss":     label,
                "num_trades":    m["num_trades"],
                "num_wins":      m["num_wins"],
                "num_losses":    m["num_losses"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{m['total_pnl']:.2f}",
                "avg_win":       f"{m['avg_win']:.2f}",
                "avg_loss":      f"{m['avg_loss']:.2f}",
                "profit_factor": pf,
                "max_drawdown":  f"{m['max_drawdown']:.2f}",
            })
    col_w = 12
    hdr = (f"{'SL':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Max DD':>10} | {'PF':>6}")
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  STOP-LOSS SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for sl in SWEEP_SL_LEVELS:
        label = _sl_label(sl)
        m     = compute_metrics(sl_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"{label:>{col_w}} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"${m['max_drawdown']:>9,.2f} | {pf:>6}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- EMA SWEEP
    # ════════════════════════════════════════════════════════════
    ema_sweep_cols = [
        "ema_fast", "ema_slow",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    ema_rows = []
    for fast, slow in ema_combos:
        m  = compute_metrics(ema_trades[(fast, slow)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        ema_rows.append({
            "ema_fast":        fast,
            "ema_slow":        slow,
            "num_trades":      m["num_trades"],
            "win_rate_pct":    f"{m['win_rate']:.1f}",
            "total_pnl":       f"{m['total_pnl']:.2f}",
            "avg_win":         f"{m['avg_win']:.2f}",
            "avg_loss":        f"{m['avg_loss']:.2f}",
            "profit_factor":   pf,
            "max_drawdown":    f"{m['max_drawdown']:.2f}",
            "_total_pnl_num":  m["total_pnl"],
        })
    ema_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(EMA_SWEEP_SAVE_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=ema_sweep_cols)
        w_csv.writeheader()
        for row in ema_rows:
            w_csv.writerow({k: row[k] for k in ema_sweep_cols})
    hdr = (
        f"  {'Fast':>4} | {'Slow':>4} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  EMA PARAMETER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in ema_rows:
        logger.info(
            f"  {row['ema_fast']:>4} | {row['ema_slow']:>4} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {EMA_SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- TRAILING STOP SWEEP
    # ════════════════════════════════════════════════════════════
    ts_sweep_cols = [
        "trailing_stop", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    ts_rows = []
    for ts in SWEEP_TS_LEVELS:
        label = _ts_label(ts)
        m     = compute_metrics(ts_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        ts_rows.append({
            "trailing_stop":  label,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
        })
    with open(TRAILING_STOP_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=ts_sweep_cols)
        w_csv.writeheader()
        w_csv.writerows(ts_rows)
    col_w = 10
    hdr = (f"{'Trail $':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Max DD':>10} | {'PF':>6}")
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TRAILING STOP SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in ts_rows:
        logger.info(
            f"{row['trailing_stop']:>{col_w}} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['profit_factor']:>6}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {TRAILING_STOP_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- SPREAD WIDTH SWEEP
    # ════════════════════════════════════════════════════════════
    sw_sweep_cols = [
        "spread_width", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    sw_rows = []
    for w in SWEEP_SPREAD_WIDTHS:
        m      = compute_metrics(width_trades[float(w)])
        pf     = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in width_trades[float(w)]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        sw_rows.append({
            "spread_width":        w,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    sw_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(SPREAD_WIDTH_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=sw_sweep_cols)
        w_csv.writeheader()
        for row in sw_rows:
            w_csv.writerow({k: row[k] for k in sw_sweep_cols})
    hdr = (
        f"  {'Width':>6} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  SPREAD WIDTH SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in sw_rows:
        logger.info(
            f"  {row['spread_width']:>6} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {SPREAD_WIDTH_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- PER-POSITION TRAILING STOP SWEEP
    # ════════════════════════════════════════════════════════════
    pt_cols = [
        "activation_pct", "pullback_pct",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    pt_rows = []
    for act, pull in pt_combos:
        m  = compute_metrics(pt_trades[(act, pull)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        pt_rows.append({
            "activation_pct": act,
            "pullback_pct":   pull,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    pt_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(POS_TRAIL_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=pt_cols)
        w_csv.writeheader()
        for row in pt_rows:
            w_csv.writerow({k: row[k] for k in pt_cols})
    hdr = (
        f"  {'Act%':>5} | {'Pull%':>5} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PER-POSITION TRAIL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in pt_rows:
        logger.info(
            f"  {row['activation_pct']:>5} | {row['pullback_pct']:>5} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {POS_TRAIL_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- MIN CREDIT SWEEP
    # ════════════════════════════════════════════════════════════
    mc_cols = [
        "min_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    mc_rows = []
    for mc in SWEEP_MIN_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[mc])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in mc_trades[mc]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        mc_rows.append({
            "min_credit":          mc,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    mc_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(MIN_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=mc_cols)
        w_csv.writeheader()
        for row in mc_rows:
            w_csv.writerow({k: row[k] for k in mc_cols})
    hdr = (
        f"  {'MinCredit':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in mc_rows:
        logger.info(
            f"  {row['min_credit']:>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {MIN_CREDIT_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- BASELINE COMPARISON
    # ════════════════════════════════════════════════════════════
    comp_cols = [
        "strategy", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    base_rows = []
    for name, _ in _BASELINES:
        m  = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        base_rows.append({
            "strategy":      name,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
        })
    with open(BASELINE_COMPARISON_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=comp_cols)
        w_csv.writeheader()
        w_csv.writerows(base_rows)
    sep = "-" * 90
    hdr = (f"  {'Strategy':<18} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Profit Factor':>14} | {'Max DD':>10}")
    logger.info(sep)
    logger.info("  NAIVE BASELINE COMPARISON")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in base_rows:
        logger.info(
            f"  {row['strategy']:<18} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>14} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {BASELINE_COMPARISON_FILE}")

    # ════════════════════════════════════════════════════════════
    #  FULL PERFORMANCE REPORT -- default config (SL=DAILY_SL, WIDTH=WIDTH)
    # ════════════════════════════════════════════════════════════
    default_trades = width_trades.get(float(WIDTH), [])
    if default_trades:
        logger.info(f"\n  Full performance report (default: SL={DAILY_SL}, width={WIDTH}):")
        print_performance_report(default_trades, date_list)
        print_vix_analysis(default_trades)
        if RUN_STRIKE_DISTANCE_ANALYSIS:
            print_strike_distance_analysis(default_trades)
        if RUN_PUT_CALL_ANALYSIS:
            print_put_call_analysis(default_trades)

    logger.info("\nMaster sweep complete.")


# ---------------------------------------------
#  ENTRY TIME SWEEP RUNNER
# ---------------------------------------------
async def run_entry_time_sweep():
    """Sweep all combinations of entry_start × entry_end × entry_interval.

    For each trading day:
      1. Market data is fetched exactly once.
      2. Every (start × end × interval) combo is re-simulated from the cache.

    Results saved to ENTRY_TIME_SWEEP_FILE sorted by total_pnl descending.
    """
    import itertools

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")

    combos = list(itertools.product(
        ENTRY_TIME_STARTS,
        ENTRY_TIME_ENDS,
        ENTRY_TIME_INTERVALS,
    ))
    # Drop invalid combos where start >= end
    combos = [(s, e, i) for s, e, i in combos if s < e]

    logger.info("=" * 70)
    logger.info("MEFT v35: ENTRY TIME SWEEP MODE")
    logger.info(f"Entry starts   : {[t.strftime('%H:%M') for t in ENTRY_TIME_STARTS]}")
    logger.info(f"Entry ends     : {[t.strftime('%H:%M') for t in ENTRY_TIME_ENDS]}")
    logger.info(f"Intervals (min): {ENTRY_TIME_INTERVALS}")
    logger.info(f"Combos         : {len(combos)}")
    logger.info(f"Output         : {ENTRY_TIME_SWEEP_FILE}")
    logger.info("=" * 70)

    combo_trades: dict[tuple, list] = {c: [] for c in combos}
    # (combo, dow) -> trades, where dow: 0=Mon … 4=Fri
    combo_dow_trades: dict[tuple, list] = {}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            dow = d.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} -- running {len(combos)} entry-time combos...")

            for (es, ee, ei) in combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode="always_put" if PUT_ONLY else None,
                    entry_start=es,
                    entry_end=ee,
                    entry_interval=ei,
                )
                combo_trades[(es, ee, ei)].extend(trades)
                key = (es, ee, ei, dow)
                combo_dow_trades.setdefault(key, []).extend(trades)

    # -- Build results table --
    rows = []
    for (es, ee, ei), trades in combo_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "entry_start":   es.strftime("%H:%M"),
            "entry_end":     ee.strftime("%H:%M"),
            "entry_interval": ei,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    if not rows:
        logger.warning("Entry time sweep produced no trades.")
        return

    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    _apply_fdr_to_rows(rows)
    fieldnames = ["entry_start","entry_end","entry_interval","num_trades",
                  "win_rate_pct","total_pnl","avg_win","avg_loss",
                  "profit_factor","max_drawdown","calmar",
                  "p_value","p_fdr","sig_fdr"]
    with open(ENTRY_TIME_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nEntry time sweep complete -- {len(rows)} combos saved to {ENTRY_TIME_SWEEP_FILE}")

    sep  = "-" * 100
    sep2 = "═" * 100
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY TOTAL P&L")
    logger.info(sep2)
    hdr = f"  {'Start':>6} {'End':>6} {'Intvl':>6} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12} {'Calmar':>8}"
    logger.info(hdr)
    logger.info(sep)
    for r in rows[:20]:
        logger.info(
            f"  {r['entry_start']:>6} {r['entry_end']:>6} {r['entry_interval']:>6} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f} "
            f"{r['max_drawdown']:>12,.0f} {r['calmar']:>8.4f}"
        )

    # -- Day-of-week breakdown ------------------------------------------------
    DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}
    dow_rows = []
    for (es, ee, ei, dow), trades in combo_dow_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        dow_rows.append({
            "day_of_week":    DOW_NAMES[dow],
            "entry_start":    es.strftime("%H:%M"),
            "entry_end":      ee.strftime("%H:%M"),
            "entry_interval": ei,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   round(m["win_rate"], 1),
            "total_pnl":      round(m["total_pnl"], 2),
            "avg_win":        round(m["avg_win"], 2),
            "avg_loss":       round(m["avg_loss"], 2),
            "profit_factor":  round(pf, 3),
            "max_drawdown":   round(m["max_drawdown"], 2),
            "calmar":         round(calmar, 4) if calmar != float("inf") else 9999.0,
        })

    dow_rows.sort(key=lambda r: (r["day_of_week"], -r["total_pnl"]))

    dow_fieldnames = ["day_of_week", "entry_start", "entry_end", "entry_interval",
                      "num_trades", "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
                      "profit_factor", "max_drawdown", "calmar"]
    with open(ENTRY_TIME_DOW_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=dow_fieldnames)
        w.writeheader()
        w.writerows(dow_rows)
    logger.info(f"Day-of-week breakdown saved to {ENTRY_TIME_DOW_FILE}")

    # Print top combo per DOW
    logger.info(f"\n{sep2}")
    logger.info("  BEST ENTRY TIME PER DAY OF WEEK (by total P&L)")
    logger.info(sep2)
    hdr2 = f"  {'Day':>12} {'Start':>6} {'End':>6} {'Intvl':>6} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6}"
    logger.info(hdr2)
    logger.info(sep)
    seen_days: set = set()
    for r in sorted(dow_rows, key=lambda x: -x["total_pnl"]):
        if r["day_of_week"] not in seen_days:
            seen_days.add(r["day_of_week"])
            logger.info(
                f"  {r['day_of_week']:>12} {r['entry_start']:>6} {r['entry_end']:>6} {r['entry_interval']:>6} | "
                f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
                f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f}"
            )


# ---------------------------------------------
#  DIRECTION × TIME SWEEP
# ---------------------------------------------
async def run_direction_time_sweep():
    """For each entry time slot, compare PUT-only, CALL-only, and Both-Sides.

    Data is fetched once per day. Each (slot, mode) combo is simulated with a
    one-bar entry window (entry_start == entry_end == slot) so exactly one entry
    attempt is made per day per combo.

    Outputs:
      DIRECTION_TIME_SWEEP_FILE -- aggregate metrics per (slot, mode)
      DIRECTION_TIME_DOW_FILE   -- same broken down by day of week
    Console prints the best mode per slot and per (slot, DOW).
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")

    _DIRECTION_MODES = [
        ("PUT Only",   "always_put"),
        ("CALL Only",  "always_call"),
        ("Both Sides", "both_sides"),
    ]
    DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

    # (slot_str, mode_label) -> trades
    slot_mode_trades: dict[tuple, list] = {}
    # (slot_str, mode_label, dow_name) -> trades
    slot_mode_dow_trades: dict[tuple, list] = {}

    logger.info("=" * 70)
    logger.info("MEFT v35: DIRECTION × TIME SWEEP")
    logger.info(f"Slots  : {[s.strftime('%H:%M') for s in DIRECTION_TIME_SLOTS]}")
    logger.info(f"Modes  : {[label for label, _ in _DIRECTION_MODES]}")
    logger.info(f"Output : {DIRECTION_TIME_SWEEP_FILE}")
    logger.info("=" * 70)

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            dow_name = DOW_NAMES[d.weekday()]
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} ({dow_name})")

            for slot in DIRECTION_TIME_SLOTS:
                slot_str = slot.strftime("%H:%M")
                for label, mode in _DIRECTION_MODES:
                    trades, _ = await _simulate_day(
                        session, day_data, DAILY_SL,
                        baseline_mode=mode,
                        entry_start=slot,
                        entry_end=slot,
                        entry_interval=1,   # interval=1 ensures we never miss the slot
                    )
                    agg_key = (slot_str, label)
                    slot_mode_trades.setdefault(agg_key, []).extend(trades)
                    dow_key = (slot_str, label, dow_name)
                    slot_mode_dow_trades.setdefault(dow_key, []).extend(trades)

    # -- Build aggregate rows -------------------------------------------------
    def _metrics_row(trades, extra: dict) -> dict | None:
        if not trades:
            return None
        m = compute_metrics(trades)
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        return {
            **extra,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        }

    agg_rows = []
    for (slot_str, label), trades in slot_mode_trades.items():
        row = _metrics_row(trades, {"slot": slot_str, "mode": label})
        if row:
            agg_rows.append(row)
    _apply_fdr_to_rows(agg_rows)
    agg_rows.sort(key=lambda r: (r["slot"], -r["total_pnl"]))

    agg_fields = ["slot", "mode", "num_trades", "win_rate_pct", "total_pnl",
                  "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(DIRECTION_TIME_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=agg_fields)
        w.writeheader()
        w.writerows(agg_rows)
    logger.info(f"\nAggregate results saved to {DIRECTION_TIME_SWEEP_FILE}")

    # -- Build DOW rows -------------------------------------------------------
    dow_rows = []
    for (slot_str, label, dow_name), trades in slot_mode_dow_trades.items():
        row = _metrics_row(trades, {"slot": slot_str, "mode": label, "day_of_week": dow_name})
        if row:
            dow_rows.append(row)
    dow_rows.sort(key=lambda r: (r["day_of_week"], r["slot"], -r["total_pnl"]))
    _apply_fdr_to_rows(dow_rows)

    dow_fields = ["day_of_week", "slot", "mode", "num_trades", "win_rate_pct", "total_pnl",
                  "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(DIRECTION_TIME_DOW_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=dow_fields)
        w.writeheader()
        w.writerows(dow_rows)
    logger.info(f"Day-of-week breakdown saved to {DIRECTION_TIME_DOW_FILE}")

    # -- Console: best mode per slot (aggregate) ------------------------------
    sep  = "-" * 80
    sep2 = "═" * 80
    logger.info(f"\n{sep2}")
    logger.info("  BEST MODE PER SLOT (aggregate)")
    logger.info(sep2)
    hdr = f"  {'Slot':>6} {'Mode':>12} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6}"
    logger.info(hdr)
    logger.info(sep)
    seen_slots: set = set()
    for r in sorted(agg_rows, key=lambda x: (x["slot"], -x["total_pnl"])):
        if r["slot"] not in seen_slots:
            seen_slots.add(r["slot"])
        logger.info(
            f"  {r['slot']:>6} {r['mode']:>12} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f}"
        )

    # -- Console: best mode per slot × DOW ------------------------------------
    logger.info(f"\n{sep2}")
    logger.info("  BEST MODE PER SLOT × DAY OF WEEK")
    logger.info(sep2)
    hdr2 = f"  {'Day':>12} {'Slot':>6} {'Mode':>12} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'PF':>6}"
    logger.info(hdr2)
    logger.info(sep)
    seen_slot_dow: set = set()
    for r in sorted(dow_rows, key=lambda x: (x["day_of_week"], x["slot"], -x["total_pnl"])):
        key = (r["day_of_week"], r["slot"])
        if key not in seen_slot_dow:
            seen_slot_dow.add(key)
            logger.info(
                f"  {r['day_of_week']:>12} {r['slot']:>6} {r['mode']:>12} | "
                f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
                f"{r['profit_factor']:>6.3f}"
            )


# ---------------------------------------------
#  BIAS SWEEP
# ---------------------------------------------
async def run_bias_sweep():
    """Test daily indicators as direction routers: bullish signal -> PUT spread,
    bearish signal -> CALL spread.

    For each bias rule the whole date range is re-simulated from the shared
    quote cache.  Results are compared against three fixed baselines:
      - Always PUT, Always CALL, Intraday EMA (the main strategy).

    Output: BIAS_SWEEP_FILE sorted by total_pnl descending.
    """
    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")

    # Each rule is (label, fn(prior, today, vix) -> "always_put" | "always_call" | None)
    # Returning None means skip the day entirely for that rule.
    def gap_direction(prior, today, vix):
        g = (today or {}).get("dGapPercent")
        if g is None: return None
        return "always_put" if g >= 0 else "always_call"

    def prior_day_return(prior, today, vix):
        if prior is None: return None
        c, pc = prior.get("close"), prior.get("prev_close")
        if c is None or pc is None or pc == 0: return None
        return "always_put" if c >= pc else "always_call"

    def macd_direction(prior, today, vix):
        bull = (prior or {}).get("dMacdBull")
        if bull is None: return None
        return "always_put" if bull else "always_call"

    def rsi_direction(prior, today, vix):
        rsi = (prior or {}).get("dRsi")
        if rsi is None: return None
        return "always_put" if rsi >= 50 else "always_call"

    def sma20_direction(prior, today, vix):
        above = (prior or {}).get("above_sma5")   # sma5 used as short-term trend proxy
        if above is None: return None
        return "always_put" if above else "always_call"

    def sma200_direction(prior, today, vix):
        above = (prior or {}).get("above_sma200")
        if above is None: return None
        return "always_put" if above else "always_call"

    def vix_change_direction(prior, today, vix):
        # VIX falling -> calmer market -> bullish -> PUT; VIX rising -> fearful -> CALL
        chg = (today or {}).get("dVixChgPct")
        if chg is None: return None
        return "always_put" if chg <= 0 else "always_call"

    def momentum_direction(prior, today, vix):
        mom = (prior or {}).get("dMomentum")
        if mom is None: return None
        return "always_put" if mom >= 0 else "always_call"

    def stoch_direction(prior, today, vix):
        k = (prior or {}).get("dStoch")
        if k is None: return None
        return "always_put" if k >= 50 else "always_call"

    def var_pct_direction(prior, today, vix):
        # dVarPct: prior day's close position within its range (0=at low, 100=at high)
        # Prior day closed near high -> bullish -> trade PUT today
        v = (prior or {}).get("dVarPct")
        if v is None: return None
        return "always_put" if v >= 50 else "always_call"

    BIAS_RULES = [
        ("Gap Direction",        gap_direction),
        ("Prior Day Return",     prior_day_return),
        ("MACD Direction",       macd_direction),
        ("RSI Direction",        rsi_direction),
        ("Above SMA200",         sma200_direction),
        ("VIX Change",           vix_change_direction),
        ("Momentum",             momentum_direction),
        ("Stochastic",           stoch_direction),
        ("Close Pos in Range",   var_pct_direction),
    ]

    # Fixed baselines for comparison
    FIXED_BASELINES = [
        ("Always PUT",   "always_put"),
        ("Always CALL",  "always_call"),
        ("Intraday EMA", None),
    ]

    logger.info("=" * 70)
    logger.info("MEFT v35: BIAS SWEEP")
    logger.info(f"Rules  : {[name for name, _ in BIAS_RULES]}")
    logger.info(f"Output : {BIAS_SWEEP_FILE}")
    logger.info("=" * 70)

    rule_trades: dict[str, list] = {name: [] for name, _ in BIAS_RULES + FIXED_BASELINES}
    # Also track PUT/CALL split per rule for diagnostics
    rule_put_days:  dict[str, int] = {name: 0 for name, _ in BIAS_RULES}
    rule_call_days: dict[str, int] = {name: 0 for name, _ in BIAS_RULES}
    rule_skip_days: dict[str, int] = {name: 0 for name, _ in BIAS_RULES}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            prior   = _prior_day_ind(d_str)
            today   = _DAILY_INDICATORS.get(d_str)
            vix     = day_data.get("vix_level")

            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str}")

            # Fixed baselines
            for label, mode in FIXED_BASELINES:
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                rule_trades[label].extend(trades)

            # Bias rules
            for name, fn in BIAS_RULES:
                mode = fn(prior, today, vix)
                if mode is None:
                    rule_skip_days[name] += 1
                    continue
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                rule_trades[name].extend(trades)
                if mode == "always_put":
                    rule_put_days[name] += 1
                else:
                    rule_call_days[name] += 1

    # -- Build results --------------------------------------------------------
    rows = []
    all_names = [name for name, _ in BIAS_RULES] + [name for name, _ in FIXED_BASELINES]
    for name in all_names:
        trades = rule_trades[name]
        if not trades:
            continue
        m = compute_metrics(trades)
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        is_bias = name in dict(BIAS_RULES)
        rows.append({
            "rule":          name,
            "type":          "bias" if is_bias else "baseline",
            "put_days":      rule_put_days.get(name, "-"),
            "call_days":     rule_call_days.get(name, "-"),
            "skip_days":     rule_skip_days.get(name, "-"),
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    _apply_fdr_to_rows(rows)
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    fieldnames = ["rule", "type", "put_days", "call_days", "skip_days",
                  "num_trades", "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
                  "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(BIAS_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nBias sweep complete -- saved to {BIAS_SWEEP_FILE}")

    sep  = "-" * 110
    sep2 = "═" * 110
    logger.info(f"\n{sep2}")
    logger.info("  BIAS SWEEP RESULTS (sorted by total P&L)")
    logger.info(sep2)
    hdr = (f"  {'Rule':<22} {'Type':<9} {'PUT d':>6} {'CLL d':>6} {'Skip':>5} | "
           f"{'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12}")
    logger.info(hdr)
    logger.info(sep)
    for r in rows:
        pd_str = str(r["put_days"]) if r["put_days"] != "-" else "   -"
        cd_str = str(r["call_days"]) if r["call_days"] != "-" else "   -"
        sk_str = str(r["skip_days"]) if r["skip_days"] != "-" else "  -"
        logger.info(
            f"  {r['rule']:<22} {r['type']:<9} {pd_str:>6} {cd_str:>6} {sk_str:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f} "
            f"{r['max_drawdown']:>12,.0f}"
        )


# ---------------------------------------------
#  COMBO SWEEP RUNNER
# ---------------------------------------------
async def run_combo_sweep():
    """Cross-parameter grid search across VIX filter, spread width, stop-loss,
    and minimum credit simultaneously.

    For each trading day:
      1. Market data is fetched exactly once (seeded for EMA_SLOW bars).
      2. Every combination of (vix_max × width × daily_sl × min_credit) is
         re-simulated against the in-memory quote cache -- no extra I/O.

    After all days, results are saved to COMBO_SWEEP_FILE and automatically
    analyzed:
      • Top 20 combos by total P&L
      • Top 20 combos by Calmar ratio (total_pnl / abs(max_drawdown))
      • Parameter sensitivity: average P&L per value of each parameter
        (shows which single-parameter choices matter most regardless of others)
      • Best combo within each VIX-max bucket
    """
    import itertools

    date_list = pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")

    combos = list(itertools.product(
        COMBO_VIX_MAX,
        COMBO_WIDTHS,
        COMBO_SL_LEVELS,
        COMBO_MIN_CREDITS,
    ))

    # -- Pre-sweep data coverage check --
    # Count available option_quotes parquet files per year so we can detect
    # incomplete data before interpreting results.
    years_in_range = sorted({d.year for d in pd.date_range(_mc.PILOT_YEAR_START, _mc.PILOT_YEAR_END, freq="B")})
    coverage: dict[int, dict] = {}
    for yr in years_in_range:
        yr_trading_days = [
            d for d in pd.date_range(f"{yr}-01-01", f"{yr}-12-31", freq="B")
            if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS
        ]
        quotes_dir = os.path.join(DATA_DIR, str(yr), "option_quotes")
        available = len(os.listdir(quotes_dir)) if os.path.isdir(quotes_dir) else 0
        coverage[yr] = {"expected": len(yr_trading_days), "available": available,
                        "pct": round(available / len(yr_trading_days) * 100, 1) if yr_trading_days else 0}

    logger.info("=" * 70)
    logger.info("MEFT v35: COMBO SWEEP MODE")
    logger.info(f"VIX max   : {COMBO_VIX_MAX}")
    logger.info(f"Widths    : {COMBO_WIDTHS}")
    logger.info(f"SL levels : {COMBO_SL_LEVELS}")
    logger.info(f"Min credit: {COMBO_MIN_CREDITS}")
    logger.info(f"Combos    : {len(combos)}")
    logger.info(f"Output    : {COMBO_SWEEP_FILE}")
    logger.info("-" * 70)
    logger.info("  DATA COVERAGE (option_quotes)")
    for yr, c in coverage.items():
        status = "✓ COMPLETE" if c["pct"] >= 95 else "⚠ PARTIAL" if c["pct"] >= 50 else "✗ INCOMPLETE"
        logger.info(f"  {yr}: {c['available']:>3}/{c['expected']:>3} days ({c['pct']:>5.1f}%)  {status}")
    all_complete = all(c["pct"] >= 95 for c in coverage.values())
    if not all_complete:
        logger.warning("  WARNING: Some years have incomplete data -- results may be biased toward available periods!")
    logger.info("=" * 70)

    # key -> list of trades
    combo_trades: dict[tuple, list] = {c: [] for c in combos}

    # Coverage tracking during sweep
    days_processed: dict[int, int] = {yr: 0 for yr in years_in_range}
    days_skipped:   dict[int, int] = {yr: 0 for yr in years_in_range}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                days_skipped[d.year] = days_skipped.get(d.year, 0) + 1
                continue

            days_processed[d.year] = days_processed.get(d.year, 0) + 1
            vix_level = day_data.get("vix_level")
            logger.info(
                f"Day {i+1:>3}/{len(date_list)} | {d_str} | VIX={vix_level} -- running {len(combos)} combos..."
            )

            for (vix_max, width, sl, mc) in combos:
                # Apply VIX filter at day level -- skip simulation entirely if filtered
                if vix_max is not None and vix_level is not None and vix_level > vix_max:
                    continue
                trades, _ = await _simulate_day(
                    session, day_data, sl,
                    spread_width=float(width),
                    min_credit=mc if mc > 0 else None,
                    baseline_mode="always_put",   # PUT_ONLY=True equivalent
                )
                combo_trades[(vix_max, width, sl, mc)].extend(trades)

    # -- Build results table --
    rows = []
    for (vix_max, width, sl, mc), trades in combo_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "vix_max":       str(vix_max) if vix_max is not None else "none",
            "width":         width,
            "daily_sl":      str(sl) if sl is not None else "none",
            "min_credit":    mc,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    _apply_fdr_to_rows(rows)

    # -- Post-sweep coverage summary --
    logger.info("\n" + "=" * 70)
    logger.info("  DATA COVERAGE SUMMARY (days actually processed by sweep)")
    total_processed = sum(days_processed.values())
    total_skipped   = sum(days_skipped.values())
    for yr in years_in_range:
        p = days_processed.get(yr, 0)
        s = days_skipped.get(yr, 0)
        pct = round(p / (p + s) * 100, 1) if (p + s) > 0 else 0
        status = "✓" if pct >= 95 else "⚠ PARTIAL -- results may be skewed!"
        logger.info(f"  {yr}: {p:>3} processed, {s:>3} skipped ({pct:.1f}% coverage)  {status}")
    logger.info(f"  TOTAL: {total_processed} days processed, {total_skipped} skipped")
    if total_skipped > 0:
        logger.warning(f"  ⚠ {total_skipped} days had missing data and were excluded from ALL combos.")
        logger.warning("    Results reflect only the periods with available data -- interpret with caution!")
    logger.info("=" * 70)

    if not rows:
        logger.warning("Combo sweep produced no trades.")
        return

    # Save full results CSV -- prepend coverage metadata as comment rows
    fieldnames = ["vix_max","width","daily_sl","min_credit","num_trades",
                  "win_rate_pct","total_pnl","avg_win","avg_loss",
                  "profit_factor","max_drawdown","calmar",
                  "p_value","p_fdr","sig_fdr",
                  "days_processed","days_skipped","data_coverage_pct"]
    # Attach per-year coverage counts to every row so the CSV is self-documenting
    yr_processed_str = " | ".join(f"{yr}:{days_processed.get(yr,0)}d" for yr in years_in_range)
    yr_skipped_str   = " | ".join(f"{yr}:{days_skipped.get(yr,0)}d"   for yr in years_in_range)
    coverage_pct_str = " | ".join(f"{yr}:{coverage[yr]['pct']}%" for yr in years_in_range)
    for r in rows:
        r["days_processed"]      = yr_processed_str
        r["days_skipped"]        = yr_skipped_str
        r["data_coverage_pct"]   = coverage_pct_str
    with open(COMBO_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nCombo sweep complete -- {len(rows)} combos saved to {COMBO_SWEEP_FILE}")

    # -- Analysis --
    sep  = "-" * 100
    sep2 = "═" * 100

    # Sort helpers
    by_pnl    = sorted(rows, key=lambda r: r["total_pnl"],   reverse=True)
    by_calmar = sorted(rows, key=lambda r: r["calmar"],       reverse=True)

    # 1. Top 20 by total P&L
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY TOTAL P&L")
    logger.info(sep2)
    hdr = f"  {'VIX':>5} {'W':>4} {'SL':>7} {'MC':>5} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12} {'Calmar':>8}"
    logger.info(hdr)
    logger.info(sep)
    for r in by_pnl[:20]:
        logger.info(
            f"  {r['vix_max']:>5} {r['width']:>4} {r['daily_sl']:>7} {r['min_credit']:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6} ${r['total_pnl']:>10,.0f} "
            f"${r['avg_win']:>7,.0f} ${r['avg_loss']:>8,.0f} "
            f"{r['profit_factor']:>6.2f} ${r['max_drawdown']:>10,.0f} {r['calmar']:>8.3f}"
        )
    logger.info(sep)

    # 2. Top 20 by Calmar (risk-adjusted)
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY CALMAR RATIO  (total_pnl / abs(max_drawdown))")
    logger.info(sep2)
    logger.info(hdr)
    logger.info(sep)
    for r in by_calmar[:20]:
        logger.info(
            f"  {r['vix_max']:>5} {r['width']:>4} {r['daily_sl']:>7} {r['min_credit']:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6} ${r['total_pnl']:>10,.0f} "
            f"${r['avg_win']:>7,.0f} ${r['avg_loss']:>8,.0f} "
            f"{r['profit_factor']:>6.2f} ${r['max_drawdown']:>10,.0f} {r['calmar']:>8.3f}"
        )
    logger.info(sep)

    # 3. Parameter sensitivity -- average P&L per value of each parameter
    logger.info(f"\n{sep2}")
    logger.info("  PARAMETER SENSITIVITY  (avg total_pnl across all combos sharing that value)")
    logger.info(sep2)
    for param in ["vix_max", "width", "daily_sl", "min_credit"]:
        vals: dict[str, list] = {}
        for r in rows:
            k = str(r[param])
            vals.setdefault(k, []).append(r["total_pnl"])
        avg_by_val = sorted(
            [(k, sum(v)/len(v), len(v)) for k, v in vals.items()],
            key=lambda x: x[1], reverse=True
        )
        logger.info(f"\n  {param}:")
        logger.info(f"    {'Value':<10} {'Avg P&L':>12}  {'N combos':>9}")
        logger.info(f"    {'-'*35}")
        for val, avg, n in avg_by_val:
            logger.info(f"    {val:<10} ${avg:>10,.0f}  {n:>9}")

    # 4. Best combo per VIX bucket
    logger.info(f"\n{sep2}")
    logger.info("  BEST COMBO PER VIX-MAX BUCKET  (by total P&L)")
    logger.info(sep2)
    logger.info(hdr)
    logger.info(sep)
    by_vix: dict[str, list] = {}
    for r in rows:
        by_vix.setdefault(r["vix_max"], []).append(r)
    for vix_key in sorted(by_vix.keys(), key=lambda x: (x == "none", x)):
        best = max(by_vix[vix_key], key=lambda r: r["total_pnl"])
        logger.info(
            f"  {best['vix_max']:>5} {best['width']:>4} {best['daily_sl']:>7} {best['min_credit']:>5} | "
            f"{best['num_trades']:>7} {best['win_rate_pct']:>6} ${best['total_pnl']:>10,.0f} "
            f"${best['avg_win']:>7,.0f} ${best['avg_loss']:>8,.0f} "
            f"{best['profit_factor']:>6.2f} ${best['max_drawdown']:>10,.0f} {best['calmar']:>8.3f}"
        )
    logger.info(sep)
    logger.info(f"\n  Full results saved -> {COMBO_SWEEP_FILE}")


