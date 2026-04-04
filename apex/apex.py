#!/usr/bin/env python3
"""Apex — 0DTE SPX Credit Spread Research Engine.

Usage:
    python3 apex.py --marathon                    # Single backtest with current config
    python3 apex.py --sweep entry_time            # Sweep entry times
    python3 apex.py --sweep direction             # Sweep directions
    python3 apex.py --sweep width                 # Sweep widths
    python3 apex.py --sweep otm_distance          # Sweep OTM distances
    python3 apex.py --sweep multi_entry           # Sweep multi-entry intervals
    python3 apex.py --marathon --start 2022-01-03 --end 2024-12-31  # Custom range
"""
from __future__ import annotations

import argparse
import sys

import apex_config as _cfg
from apex_engine import build_daily_indicators, run_backtest, compute_metrics, logger
from apex_reporting import (
    print_performance_report, print_vix_analysis, print_dow_analysis,
    print_worst_days, log_iteration, format_results_table,
)


def _get_next_iteration() -> int:
    """Read iterations.csv and return next iteration number."""
    import csv
    import os
    path = os.path.join(_cfg.PROJECT_ROOT, "iterations.csv")
    if not os.path.exists(path):
        return 1
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return 1
    try:
        return max(int(r["iteration"]) for r in rows) + 1
    except (ValueError, KeyError):
        return len(rows) + 1


def run_marathon(indicators: dict):
    """Run a single backtest with current config."""
    trades = run_backtest(indicators)
    m = print_performance_report(trades)
    print_vix_analysis(trades)
    print_dow_analysis(trades)
    print_worst_days(trades)
    return trades, m


def run_sweep(indicators: dict, sweep_type: str):
    """Run a parameter sweep."""
    results = []
    it = _get_next_iteration()

    if sweep_type == "entry_time":
        times = [
            "09:35:00", "10:00:00", "10:30:00", "11:00:00", "11:30:00",
            "12:00:00", "12:30:00", "13:00:00", "13:30:00", "14:00:00",
            "14:30:00", "15:00:00", "15:30:00", "15:55:00",
        ]
        for t in times:
            logger.info(f"\n>>> SWEEP: entry_time={t}")
            trades = run_backtest(indicators, ENTRY_TIME=t, ENABLE_MULTI_ENTRY=False)
            m = compute_metrics(trades)
            log_iteration(it, f"entry_time_sweep_{t}", m, "SWEEP",
                          entry_time=t)
            results.append({"param": t, **m})
            it += 1

    elif sweep_type == "direction":
        for d in ["always_put", "always_call", "both_sides", "ema"]:
            logger.info(f"\n>>> SWEEP: direction={d}")
            trades = run_backtest(indicators, DIRECTION_MODE=d)
            m = compute_metrics(trades)
            log_iteration(it, f"direction_sweep_{d}", m, "SWEEP",
                          direction=d)
            results.append({"param": d, **m})
            it += 1

    elif sweep_type == "width":
        for w in [5, 10, 15, 20, 25, 30, 50]:
            logger.info(f"\n>>> SWEEP: width={w}")
            trades = run_backtest(indicators, WIDTH=float(w))
            m = compute_metrics(trades)
            log_iteration(it, f"width_sweep_{w}", m, "SWEEP",
                          width=w)
            results.append({"param": w, **m})
            it += 1

    elif sweep_type == "otm_distance":
        for d in [5, 10, 15, 20, 25, 30, 40, 50]:
            logger.info(f"\n>>> SWEEP: otm_distance={d}")
            trades = run_backtest(indicators, MIN_OTM_DISTANCE=float(d))
            m = compute_metrics(trades)
            log_iteration(it, f"otm_distance_sweep_{d}", m, "SWEEP",
                          otm_distance=d)
            results.append({"param": d, **m})
            it += 1

    elif sweep_type == "multi_entry":
        # Single entry (baseline)
        logger.info(f"\n>>> SWEEP: single_entry")
        trades = run_backtest(indicators, ENABLE_MULTI_ENTRY=False)
        m = compute_metrics(trades)
        log_iteration(it, "multi_entry_sweep_single", m, "SWEEP", interval="single")
        results.append({"param": "single", **m})
        it += 1

        # Multi-entry at various intervals
        for interval in [5, 10, 15, 20, 30]:
            logger.info(f"\n>>> SWEEP: multi_entry interval={interval}min")
            trades = run_backtest(indicators,
                                 ENABLE_MULTI_ENTRY=True,
                                 ENTRY_INTERVAL=interval)
            m = compute_metrics(trades)
            log_iteration(it, f"multi_entry_sweep_{interval}min", m, "SWEEP",
                          interval=interval)
            results.append({"param": f"{interval}min", **m})
            it += 1

    elif sweep_type == "qty":
        for q in [1, 2, 3, 4, 5]:
            logger.info(f"\n>>> SWEEP: qty={q}")
            trades = run_backtest(indicators, QTY=q)
            m = compute_metrics(trades)
            log_iteration(it, f"qty_sweep_{q}", m, "SWEEP", qty=q)
            results.append({"param": q, **m})
            it += 1

    else:
        logger.error(f"Unknown sweep type: {sweep_type}")
        return []

    # Print sweep summary
    logger.info(f"\n{'='*80}")
    logger.info(f"SWEEP SUMMARY: {sweep_type}")
    logger.info(f"{'='*80}")
    logger.info(f"{'Param':<15} {'Trades':>7} {'WR%':>6} {'P&L':>12} {'DD':>10} {'Sharpe':>7} {'PF':>6} {'MaxLoss':>10}")
    logger.info("-" * 80)
    for r in results:
        logger.info(f"{str(r['param']):<15} {r['num_trades']:>7} {r['win_rate']:>5.1f}% "
                    f"${r['total_pnl']:>10,.0f} ${r['max_drawdown']:>8,.0f} "
                    f"{r['sharpe']:>6.2f} {r['profit_factor']:>5.2f} ${r['max_single_loss']:>8,.0f}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Apex 0DTE SPX Research")
    parser.add_argument("--marathon", action="store_true", help="Run single backtest")
    parser.add_argument("--sweep", type=str, help="Run parameter sweep (entry_time, direction, width, otm_distance, multi_entry, qty)")
    parser.add_argument("--start", type=str, help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="Override end date (YYYY-MM-DD)")

    # Config overrides
    parser.add_argument("--direction", type=str)
    parser.add_argument("--entry-time", type=str)
    parser.add_argument("--width", type=float)
    parser.add_argument("--otm", type=float)
    parser.add_argument("--qty", type=int)
    parser.add_argument("--min-credit", type=float)
    parser.add_argument("--vix-max", type=float)
    parser.add_argument("--skip-fomc", action="store_true")
    parser.add_argument("--skip-cpi", action="store_true")
    parser.add_argument("--skip-days", type=str, help="Comma-separated DOW numbers to skip (0=Mon..4=Fri)")
    parser.add_argument("--prior-ret-max", type=float, help="Max prior-day return pct")
    parser.add_argument("--prior-range-max", type=float, help="Max prior-day range pct")
    parser.add_argument("--gap-max", type=float, help="Max gap pct")
    parser.add_argument("--oi-min", type=int, help="Min nearby OI at short strike")
    parser.add_argument("--straddle-max", type=float, help="Max 15:50 ATM straddle price")
    parser.add_argument("--vix-term-max", type=float, help="Max VIX/VIX9D ratio (backwardation filter)")
    parser.add_argument("--vvix-max", type=float, help="Max VVIX level (crash kill switch)")
    parser.add_argument("--wvf-bb", action="store_true", help="Enable WVF Bollinger Band filter")
    parser.add_argument("--wvf-pct", type=float, help="Enable WVF percentile filter (max percentile)")
    parser.add_argument("--wvf-max", type=float, help="Enable WVF absolute max filter")
    parser.add_argument("--vol-consumed-max", type=float, help="Max vol consumed by 15:00 (range/VIX16)")
    parser.add_argument("--vol-surprise-max", type=float, help="Max vol surprise (daily range/VIX16)")
    parser.add_argument("--vix1d-ratio-max", type=float, help="Max VIX1D/VIX ratio")
    parser.add_argument("--safety-ratio-min", type=float, help="Min safety ratio (d / expected 5min move)")

    args = parser.parse_args()

    # Apply overrides
    if args.start:
        _cfg.PILOT_YEAR_START = args.start
    if args.end:
        _cfg.PILOT_YEAR_END = args.end
    if args.direction:
        _cfg.DIRECTION_MODE = args.direction
    if args.entry_time:
        _cfg.ENTRY_TIME = args.entry_time
    if args.width:
        _cfg.WIDTH = args.width
    if args.otm:
        _cfg.MIN_OTM_DISTANCE = args.otm
    if args.qty:
        _cfg.QTY = args.qty
    if args.min_credit:
        _cfg.MIN_NET_CREDIT = args.min_credit
    if args.vix_max:
        _cfg.ENABLE_VIX_MAX_FILTER = True
        _cfg.VIX_MAX = args.vix_max
    if args.skip_fomc:
        _cfg.ENABLE_FOMC_SKIP = True
    if args.skip_cpi:
        _cfg.ENABLE_CPI_SKIP = True
    if args.skip_days:
        _cfg.ENABLE_DOW_FILTER = True
        _cfg.SKIP_DAYS_OF_WEEK = {int(d) for d in args.skip_days.split(",")}
    if args.prior_ret_max:
        _cfg.ENABLE_PRIOR_DAY_RETURN_FILTER = True
        _cfg.PRIOR_DAY_RETURN_MAX = args.prior_ret_max
    if args.prior_range_max:
        _cfg.ENABLE_PRIOR_DAY_RANGE_FILTER = True
        _cfg.PRIOR_DAY_RANGE_MAX = args.prior_range_max
    if args.gap_max:
        _cfg.ENABLE_GAP_FILTER = True
        _cfg.GAP_MAX = args.gap_max
    if args.oi_min:
        _cfg.ENABLE_OI_FILTER = True
        _cfg.OI_MIN_NEARBY = args.oi_min
    if args.straddle_max:
        _cfg.ENABLE_STRADDLE_FILTER = True
        _cfg.STRADDLE_MAX = args.straddle_max
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
    if args.wvf_max:
        _cfg.ENABLE_WVF_FILTER = True
        _cfg.WVF_MODE = "absolute"
        _cfg.WVF_ABSOLUTE_MAX = args.wvf_max

    if args.vol_consumed_max:
        _cfg.ENABLE_VOL_CONSUMED_FILTER = True
        _cfg.VOL_CONSUMED_MAX = args.vol_consumed_max
    if args.vol_surprise_max:
        _cfg.ENABLE_VOL_SURPRISE_FILTER = True
        _cfg.VOL_SURPRISE_MAX = args.vol_surprise_max
    if args.vix1d_ratio_max:
        _cfg.ENABLE_VIX1D_RATIO_FILTER = True
        _cfg.VIX1D_RATIO_MAX = args.vix1d_ratio_max
    if args.safety_ratio_min:
        _cfg.ENABLE_SAFETY_RATIO_FILTER = True
        _cfg.SAFETY_RATIO_MIN = args.safety_ratio_min

    # Build indicators
    logger.info("Building daily indicators...")
    indicators = build_daily_indicators()

    if args.marathon:
        run_marathon(indicators)
    elif args.sweep:
        run_sweep(indicators, args.sweep)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
