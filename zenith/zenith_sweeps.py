"""Zenith Fade CCS — Parameter Sweep Runners.

Test multiple parameter values to find optimal settings.
Uses run_backtest() with config_overrides for isolated runs.
"""
from __future__ import annotations

import itertools
from datetime import datetime

import zenith_config as _cfg
from zenith_engine import build_daily_indicators, run_backtest, logger
from zenith_reporting import compute_metrics


def _run_sweep(
    sweep_name: str,
    param_grid: list[dict],
    indicators: dict,
    sort_by: str = "total_pnl",
) -> list[dict]:
    """Generic sweep runner.

    Args:
        sweep_name: name for logging
        param_grid: list of config override dicts
        indicators: pre-built daily indicators
        sort_by: metric to sort results by (descending)

    Returns sorted list of result dicts.
    """
    logger.info(f"\n{'='*70}")
    logger.info(f"  SWEEP: {sweep_name} ({len(param_grid)} combinations)")
    logger.info(f"{'='*70}")

    results = []
    for i, overrides in enumerate(param_grid):
        trades = run_backtest(indicators=indicators, config_overrides=overrides, quiet=True)
        m = compute_metrics(trades)

        result = {**overrides, **m}
        results.append(result)

        if (i + 1) % 10 == 0 or i == 0:
            label = ", ".join(f"{k}={v}" for k, v in overrides.items())
            logger.info(f"  [{i+1}/{len(param_grid)}] {label} -> P&L ${m['total_pnl']:>9,.0f} | WR {m['win_rate']:.1f}% | Sharpe {m['sharpe']:.2f} | Trades {m['num_trades']}")

    # Sort by metric
    results.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

    # Print top 10
    sep = "-" * 90
    logger.info(f"\n{sep}")
    logger.info(f"  TOP 10 BY {sort_by.upper()}")
    logger.info(sep)

    for i, r in enumerate(results[:10]):
        overrides_str = ", ".join(f"{k}={v}" for k, v in r.items()
                                  if k not in ("num_trades", "total_pnl", "max_dd", "sharpe",
                                               "sortino", "calmar", "win_rate", "profit_factor",
                                               "avg_win", "avg_loss", "best_trade", "worst_trade",
                                               "max_win_streak", "recovery_factor", "cagr", "max_dd_pct"))
        logger.info(
            f"  #{i+1:>2} | P&L ${r['total_pnl']:>9,.0f} | DD -${r['max_dd']:>8,.0f} | "
            f"Sharpe {r['sharpe']:>5.2f} | WR {r['win_rate']:>5.1f}% | PF {r['profit_factor']:>4.2f} | "
            f"Trades {r['num_trades']:>4} | {overrides_str}"
        )

    # Also print bottom 5
    logger.info(f"\n  BOTTOM 5:")
    for r in results[-5:]:
        overrides_str = ", ".join(f"{k}={v}" for k, v in r.items()
                                  if k not in ("num_trades", "total_pnl", "max_dd", "sharpe",
                                               "sortino", "calmar", "win_rate", "profit_factor",
                                               "avg_win", "avg_loss", "best_trade", "worst_trade",
                                               "max_win_streak", "recovery_factor", "cagr", "max_dd_pct"))
        logger.info(
            f"       | P&L ${r['total_pnl']:>9,.0f} | DD -${r['max_dd']:>8,.0f} | "
            f"Sharpe {r['sharpe']:>5.2f} | WR {r['win_rate']:>5.1f}% | PF {r['profit_factor']:>4.2f} | "
            f"Trades {r['num_trades']:>4} | {overrides_str}"
        )

    logger.info(sep)
    return results


# -----------------------------------------------
#  INDIVIDUAL SWEEP FUNCTIONS
# -----------------------------------------------

def run_score_threshold_sweep(indicators: dict = None):
    """Sweep SCORE_THRESHOLD from 30 to 90 in steps of 2."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = [{"SCORE_THRESHOLD": t, "SIGNAL_MODE": "dynamic"} for t in range(30, 92, 2)]
    return _run_sweep("Score Threshold", grid, indicators)


def run_variance_threshold_sweep(indicators: dict = None):
    """Sweep VARIANCE_THRESHOLD from 30 to 85 in steps of 5."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = [{"VARIANCE_THRESHOLD": t, "SIGNAL_MODE": "classic"} for t in range(30, 90, 5)]
    return _run_sweep("Variance Threshold", grid, indicators)


def run_strike_offset_sweep(indicators: dict = None):
    """Sweep STRIKE_OFFSET from 0 to 30 in steps of 5."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = [{"STRIKE_OFFSET": s} for s in range(0, 35, 5)]
    return _run_sweep("Strike Offset", grid, indicators)


def run_spread_width_sweep(indicators: dict = None):
    """Sweep SPREAD_WIDTH from 5 to 50 in steps of 5.
    Adjusts FIXED_CREDIT proportionally (40% of width per PineScript estimate)."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = []
    for w in range(5, 55, 5):
        credit = w * 0.4  # 40% of width is a rough estimate
        grid.append({"SPREAD_WIDTH": float(w), "FIXED_CREDIT": credit})
    return _run_sweep("Spread Width", grid, indicators)


def run_tp_sl_sweep(indicators: dict = None):
    """Sweep TP_CAP and SL_CAP combinations."""
    if indicators is None:
        indicators = build_daily_indicators()
    tp_vals = [0.3, 0.4, 0.5, 0.66, 0.8, 1.0]
    sl_vals = [-0.5, -0.75, -1.0, -1.5, -2.0]
    grid = [{"TP_CAP": tp, "SL_CAP": sl} for tp, sl in itertools.product(tp_vals, sl_vals)]
    return _run_sweep("TP/SL Cap", grid, indicators)


def run_weight_sweep(indicators: dict = None):
    """Sweep Dynamic Score weights."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = []
    for wv in [0.5, 1.0, 1.4, 2.0, 3.0]:
        for wr in [100, 200, 274, 350, 500]:
            grid.append({"W_VAR": wv, "W_ROCKET": float(wr), "SIGNAL_MODE": "dynamic"})
    return _run_sweep("Weight Optimization", grid, indicators)


def run_risk_amount_sweep(indicators: dict = None):
    """Sweep RISK_AMOUNT from $200 to $1500."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = [{"RISK_AMOUNT": float(r)} for r in range(200, 1600, 100)]
    return _run_sweep("Risk Amount", grid, indicators)


def run_rocket_threshold_sweep(indicators: dict = None):
    """Sweep RISING_ROCKET_THRESHOLD for classic mode."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = [{"RISING_ROCKET_THRESHOLD": round(r, 4), "SIGNAL_MODE": "classic"}
            for r in [i * 0.005 for i in range(4, 25)]]
    return _run_sweep("Rising Rocket Threshold", grid, indicators)


def run_filter_toggle_sweep(indicators: dict = None):
    """Test each classic filter individually (all others off)."""
    if indicators is None:
        indicators = build_daily_indicators()

    filters = [
        "USE_VARIANCE_FILTER", "USE_REL_VOL_FILTER", "USE_MIN_MOVE_FILTER",
        "USE_RISING_ROCKET_FILTER", "USE_CLUSTER_FILTER", "USE_TREND_SMA_FILTER",
        "USE_GAP_FILTER", "USE_BODY_RATIO_FILTER", "USE_VIX_FILTER",
        "USE_RSI_FILTER", "USE_DIST_SMA_FILTER", "USE_STOCH_FILTER",
    ]

    # All filters off baseline
    all_off = {f: False for f in filters}
    all_off["SIGNAL_MODE"] = "classic"

    grid = []
    # Baseline: all default filters on
    grid.append({"SIGNAL_MODE": "classic"})
    # All off
    grid.append({**all_off})
    # Each filter solo
    for filt in filters:
        overrides = {**all_off, filt: True}
        grid.append(overrides)

    return _run_sweep("Filter Toggle (Solo)", grid, indicators)


def run_vix_filter_sweep(indicators: dict = None):
    """Test VIX-based filters: VIX >= VIX_SMA20, VIX ranges, VIX max."""
    if indicators is None:
        indicators = build_daily_indicators()
    grid = []
    # VIX max filter levels
    for vmax in [20, 25, 30, 35, 40, 50, 100]:
        grid.append({"VIX_MAX_FILTER": float(vmax)})
    # VIX >= SMA20 filter
    grid.append({"USE_VIX_FILTER": True, "SIGNAL_MODE": "classic"})
    return _run_sweep("VIX Filter", grid, indicators)


def run_comprehensive_sweep(indicators: dict = None):
    """Run all core sweeps sequentially."""
    if indicators is None:
        logger.info("Building daily indicators for all sweeps...")
        indicators = build_daily_indicators()

    logger.info(f"\n{'#'*70}")
    logger.info(f"  COMPREHENSIVE SWEEP — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"{'#'*70}\n")

    run_score_threshold_sweep(indicators)
    run_variance_threshold_sweep(indicators)
    run_strike_offset_sweep(indicators)
    run_spread_width_sweep(indicators)
    run_tp_sl_sweep(indicators)
    run_filter_toggle_sweep(indicators)
    run_rocket_threshold_sweep(indicators)
    run_vix_filter_sweep(indicators)

    logger.info(f"\n{'#'*70}")
    logger.info(f"  COMPREHENSIVE SWEEP COMPLETE")
    logger.info(f"{'#'*70}")


# -----------------------------------------------
#  DISPATCH
# -----------------------------------------------
def dispatch_sweep():
    """Called from zenith.py when a sweep flag is set."""
    indicators = build_daily_indicators()

    if _cfg.RUN_SCORE_THRESHOLD_SWEEP:
        run_score_threshold_sweep(indicators)
    elif _cfg.RUN_WEIGHT_SWEEP:
        run_weight_sweep(indicators)
    elif _cfg.RUN_STRIKE_OFFSET_SWEEP:
        run_strike_offset_sweep(indicators)
    elif _cfg.RUN_WIDTH_SWEEP:
        run_spread_width_sweep(indicators)
    elif _cfg.RUN_FILTER_SWEEP:
        run_filter_toggle_sweep(indicators)
    elif _cfg.RUN_TP_SL_SWEEP:
        run_tp_sl_sweep(indicators)
    elif _cfg.RUN_RISK_AMOUNT_SWEEP:
        run_risk_amount_sweep(indicators)
    elif _cfg.RUN_VARIANCE_SWEEP:
        run_variance_threshold_sweep(indicators)
    elif _cfg.RUN_REL_VOL_SWEEP:
        pass  # Need volume data first
    elif _cfg.RUN_ROCKET_SWEEP:
        run_rocket_threshold_sweep(indicators)
    elif _cfg.RUN_VIX_FILTER_SWEEP:
        run_vix_filter_sweep(indicators)
