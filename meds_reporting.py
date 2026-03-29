from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

# Config constants and engine functions from meds_core
from meds_core import *
import meds_core as _mc

# Underscore names not included in * import
from meds_core import (
    _build_calendar_event_dates,
    _CPI_DATES,
    _EOM_DATES,
    _EOQ_DATES,
    _out,
    _PCE_DATES,
    _POST_HOL_DATES,
    _PRE_TW_DATES,
    _RUN_TS,
)

# ---------------------------------------------
#  VIX CORRELATION ANALYSIS
# ---------------------------------------------
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-30", 25.0, 30.0),
    ("30-35", 30.0, 35.0),
    ("35-40", 35.0, 40.0),
    ("40-45", 40.0, 45.0),
    ("45-50", 45.0, 50.0),
    ("50-55", 50.0, 55.0),
    ("55-60", 55.0, 60.0),
    ("60+",   60.0, None),
]


def print_vix_analysis(all_trades: list) -> None:
    """Bucket trades by VIX level and report win rate, avg P&L, and count.
    Prints to console and saves to VIX_ANALYSIS_FILE."""
    rows = []
    sep = "-" * 62
    logger.info(sep)
    logger.info("  VIX CORRELATION ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'VIX Range':<10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for label, lo, hi in VIX_BUCKETS:
        bucket = []
        for t in all_trades:
            try:
                v = float(t.get("vix_level") or "")
            except (ValueError, TypeError):
                continue
            if (lo is None or v >= lo) and (hi is None or v < hi):
                bucket.append(t)

        n        = len(bucket)
        wins     = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls     = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate = wins / n * 100 if n else 0.0
        avg_pnl  = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)

        logger.info(
            f"  {label:<10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}"
        )
        rows.append({
            "vix_range":    label,
            "trade_count":  n,
            "win_rate_pct": f"{win_rate:.1f}",
            "avg_pnl":      f"{avg_pnl:.2f}",
            "total_pnl":    f"{total_pnl:.2f}",
        })

    logger.info(sep)

    # -- Save to CSV --
    analysis_cols = ["vix_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"]
    with open(VIX_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=analysis_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  VIX analysis saved to {VIX_ANALYSIS_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  STRIKE DISTANCE ANALYSIS
# ---------------------------------------------
STRIKE_DISTANCE_BUCKETS = [
    ("35-40",   35,  40),
    ("40-45",   40,  45),
    ("45-50",   45,  50),
    ("50-55",   50,  55),
    ("55-60",   55,  60),
    ("60-65",   60,  65),
    ("65-70",   65,  70),
    ("70-75",   70,  75),
    ("75-80",   75,  80),
    ("80-85",   80,  85),
    ("85-90",   85,  90),
    ("90-95",   90,  95),
    ("95-100",  95,  100),
    ("100+",    100, None),
]


def print_strike_distance_analysis(all_trades: list) -> None:
    """Bucket trades by short-strike distance from spot at entry and report
    trade count, win rate, avg P&L, and total P&L per bucket.
    Prints to console and saves to STRIKE_DISTANCE_ANALYSIS_FILE."""
    rows = []
    sep = "-" * 70
    logger.info(sep)
    logger.info("  STRIKE DISTANCE VS WIN RATE ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Distance':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for label, lo, hi in STRIKE_DISTANCE_BUCKETS:
        bucket = []
        for t in all_trades:
            try:
                d = float(t.get("strike_distance") or "")
            except (ValueError, TypeError):
                continue
            if d >= lo and (hi is None or d < hi):
                bucket.append(t)

        n         = len(bucket)
        wins      = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls      = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate  = wins / n * 100 if n else 0.0
        avg_pnl   = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)

        logger.info(
            f"  {label:>10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}"
        )
        rows.append({
            "strike_distance_range": label,
            "trade_count":           n,
            "win_rate_pct":          f"{win_rate:.1f}",
            "avg_pnl":               f"{avg_pnl:.2f}",
            "total_pnl":             f"{total_pnl:.2f}",
        })

    logger.info(sep)

    analysis_cols = ["strike_distance_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"]
    with open(STRIKE_DISTANCE_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=analysis_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  Strike distance analysis saved to {STRIKE_DISTANCE_ANALYSIS_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  PUT vs CALL SPLIT ANALYSIS
# ---------------------------------------------
def print_put_call_analysis(all_trades: list) -> None:
    """Group trades by PUT vs CALL spread type and report key metrics.
    Prints to console and saves to PUT_CALL_SPLIT_FILE."""
    rows = []
    sep = "-" * 95
    logger.info(sep)
    logger.info("  PUT vs CALL SPREAD PERFORMANCE SPLIT")
    logger.info(sep)
    logger.info(
        f"  {'Type':<8} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | {'Prof Fac':>9} | {'Max DD':>11}"
    )
    logger.info(sep)

    split_cols = [
        "spread_type", "trade_count", "win_rate_pct", "avg_pnl",
        "total_pnl", "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]

    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        m = compute_metrics(bucket)
        avg_pnl = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0.0
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf"
        logger.info(
            f"  {label:<8} | {m['num_trades']:>7} | {m['win_rate']:>8.1f}% | "
            f"${avg_pnl:>9,.2f} | ${m['total_pnl']:>11,.2f} | "
            f"${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"{pf:>9} | ${m['max_drawdown']:>10,.2f}"
        )
        rows.append({
            "spread_type":   label,
            "trade_count":   m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "avg_pnl":       f"{avg_pnl:.2f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
        })

    logger.info(sep)

    with open(PUT_CALL_SPLIT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=split_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  PUT/CALL split analysis saved to {PUT_CALL_SPLIT_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  PERFORMANCE ANALYTICS
# ---------------------------------------------
def print_performance_report(all_trades: list, date_list) -> None:
    import math

    pnls        = [t["pnl_earned"] for t in all_trades]
    wins        = [p for p in pnls if p > 0]
    losses      = [p for p in pnls if p <= 0]
    total_pnl   = sum(pnls)
    n           = len(pnls)
    win_rate    = len(wins) / n * 100 if n else 0

    avg_win     = sum(wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(losses) / len(losses) if losses else 0
    expectancy  = (avg_win * len(wins) / n) + (avg_loss * len(losses) / n) if n else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Per-day P&L
    day_pnls: dict[str, float] = {}
    day_trade_count: dict[str, int] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
        day_trade_count[d] = day_trade_count.get(d, 0) + 1
    best_day  = max(day_pnls.values()) if day_pnls else 0
    worst_day = min(day_pnls.values()) if day_pnls else 0
    days_traded = len(day_pnls)
    avg_trades_per_day = n / days_traded if days_traded else 0

    # Sharpe & Sortino -- daily P&L basis, rf-adjusted, annualised with sqrt(252)
    rf_daily     = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals   = list(day_pnls.values())
    n_days       = len(daily_vals)
    mean_daily   = sum(daily_vals) / n_days if n_days else 0
    var_daily    = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily    = math.sqrt(var_daily)
    ann_vol      = std_daily * math.sqrt(252)
    sharpe       = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0

    downside_d   = [p for p in daily_vals if p < mean_daily]
    down_var_d   = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino      = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    # Max drawdown (trade-level equity curve)
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    # Time underwater -- % of trading days where cumulative equity is below its peak
    eq_d = peak_d = 0.0
    underwater_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]
        peak_d = max(peak_d, eq_d)
        if eq_d < peak_d:
            underwater_days += 1
    time_underwater_pct = underwater_days / days_traded * 100 if days_traded else 0

    # CAGR -- based on calendar days between first and last trade date
    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        from datetime import datetime as _dtt
        first_d = _dtt.strptime(sorted_dates[0],  "%Y%m%d")
        last_d  = _dtt.strptime(sorted_dates[-1], "%Y%m%d")
        years   = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    final_equity = ACCOUNT_SIZE + total_pnl
    cagr = ((final_equity / ACCOUNT_SIZE) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Calmar & Recovery factor
    calmar          = cagr / abs(max_dd / ACCOUNT_SIZE * 100) if max_dd != 0 else float("inf")
    recovery_factor = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")

    # Premium capture rate = net P&L / gross premium collected
    gross_premium    = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100
                          for t in all_trades)
    prem_capture_pct = (total_pnl / gross_premium * 100) if gross_premium > 0 else 0.0

    # Commission (pnl_earned already net of commission; add it back to compute gross)
    total_commission = sum(2 * 2 * float(t.get("qty", 1)) * COMMISSION for t in all_trades)

    # Buying power per trade = (spread_width - credit_received) * qty * 100
    bp_per_trade = [(float(t.get("spread_width", WIDTH)) - float(t.get("credit_received", 0)))
                    * float(t.get("qty", 1)) * 100 for t in all_trades]
    avg_bp_per_trade = sum(bp_per_trade) / n if n else 0
    # Max BP deployed on a single day (sum of all trades that day)
    day_bp: dict[str, float] = {}
    for t, bp in zip(all_trades, bp_per_trade):
        d = t["entry_date"]
        day_bp[d] = day_bp.get(d, 0.0) + bp
    max_bp_day = max(day_bp.values()) if day_bp else 0
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0

    # -- Print report --
    sep  = "-" * 56
    sep2 = "-" * 56
    logger.info(sep)
    logger.info("  PERFORMANCE REPORT")
    logger.info(sep)

    # Period & day counts
    logger.info(f"  Period          : {date_list[0].strftime('%Y-%m-%d')} -> {date_list[-1].strftime('%Y-%m-%d')}")
    days_in_range = len(date_list)
    days_skipped  = days_in_range - days_traded
    _holidays_in_range = sum(1 for d in date_list if d.strftime("%Y%m%d") in MARKET_HOLIDAYS)
    _skip_parts = [f"{_holidays_in_range} holidays"]
    if ENABLE_ECON_FILTER:
        _skip_parts.append("econ filter")
    if ENABLE_CALENDAR_FILTER:
        _skip_parts.append(f"calendar ({', '.join(sorted(CALENDAR_FILTER_EVENTS))})")
    _active_day_filters = [k for k, v in [
        ("vix_max", VIX_MAX_FILTER), ("vix_min", VIX_MIN_FILTER),
        ("vix_max", DAY_FILTER_VIX_MAX), ("vix_min", DAY_FILTER_VIX_MIN),
        ("adx_min", DAY_FILTER_ADX_MIN), ("range_max", DAY_FILTER_RANGE_MAX),
        ("range_min", DAY_FILTER_RANGE_MIN), ("rsi_min", DAY_FILTER_RSI_MIN),
        ("rsi_max", DAY_FILTER_RSI_MAX), ("atr_max", DAY_FILTER_ATR_MAX),
        ("exp_mv_max", DAY_FILTER_EXP_MOVE_MAX), ("exp_mv_min", DAY_FILTER_EXP_MOVE_MIN),
        ("cci_max", DAY_FILTER_CCI_MAX), ("ivr_min", DAY_FILTER_IVR_MIN),
        ("sma200", DAY_FILTER_ABOVE_SMA200),
    ] if v is not None]
    if _active_day_filters:
        _skip_parts.append(f"day filters ({', '.join(dict.fromkeys(_active_day_filters))})")
    _other = days_skipped - _holidays_in_range
    if _other > 0:
        _skip_parts.append(f"{_other} no data/no trades")
    logger.info(f"  Business days   : {days_in_range}  (skipped {days_skipped} -- {' + '.join(_skip_parts)})")
    logger.info(f"  Days traded     : {days_traded}")
    logger.info(f"  Total trades    : {n}  (wins: {len(wins)}  losses: {len(losses)})")
    logger.info(f"  Avg trades/day  : {avg_trades_per_day:>10.1f}")
    logger.info(sep)

    # Returns
    logger.info(f"  Total P&L       : ${total_pnl:>10,.2f}  (net of commission)")
    logger.info(f"  Gross premium   : ${gross_premium:>10,.2f}")
    logger.info(f"  Prem capture    : {prem_capture_pct:>9.1f}%  (net P&L / gross premium)")
    logger.info(f"  CAGR            : {cagr:>9.1f}%  (acct=${ACCOUNT_SIZE:,.0f})")
    logger.info(f"  Avg profit/trade: ${avg_win:>10,.2f}")
    logger.info(f"  Avg loss/trade  : ${avg_loss:>10,.2f}")
    logger.info(f"  Expectancy/trade: ${expectancy:>10,.2f}")
    logger.info(f"  Profit factor   : {profit_factor:>10.2f}x")
    logger.info(sep)

    # Risk
    logger.info(f"  Max drawdown    : ${max_dd:>10,.2f}  ({max_dd_pct:.1f}%)")
    logger.info(f"  Calmar ratio    : {calmar:>10.2f}  (CAGR / max DD%)")
    logger.info(f"  Recovery factor : {recovery_factor:>10.2f}  (net P&L / max DD)")
    logger.info(f"  Time underwater : {time_underwater_pct:>9.1f}%  ({underwater_days} of {days_traded} days)")
    logger.info(sep)

    # Ratios
    logger.info(f"  Win rate        : {win_rate:>9.1f}%")
    logger.info(f"  Sharpe ratio    : {sharpe:>10.2f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%)")
    logger.info(f"  Sortino ratio   : {sortino:>10.2f}")
    logger.info(f"  Ann. volatility : ${ann_vol:>10,.2f}  (daily P&L std × √252)")
    logger.info(sep)

    # Day stats
    logger.info(f"  Best day        : ${best_day:>10,.2f}")
    logger.info(f"  Worst day       : ${worst_day:>10,.2f}")
    logger.info(sep)

    # Cost & capital
    logger.info(f"  Commission total: ${total_commission:>10,.2f}  (${COMMISSION}/contract/leg)")
    logger.info(f"  Avg BP/trade    : ${avg_bp_per_trade:>10,.2f}")
    logger.info(f"  Avg BP/day      : ${avg_bp_day:>10,.2f}")
    logger.info(f"  Max BP day      : ${max_bp_day:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  RESULTS.md APPEND
# ---------------------------------------------
def append_results_md(all_trades: list, date_list) -> None:
    """Append a full performance snapshot to RESULTS.md after every run."""
    import math
    from collections import defaultdict
    from datetime import datetime as _dtt

    # -- Core metrics ----------------------------------------------------------
    pnls   = [t["pnl_earned"] for t in all_trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    total_pnl     = sum(pnls)
    win_rate      = len(wins) / n * 100 if n else 0
    avg_win       = sum(wins)   / len(wins)   if wins   else 0
    avg_loss      = sum(losses) / len(losses) if losses else 0
    expectancy    = (avg_win * len(wins) / n) + (avg_loss * len(losses) / n) if n else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    gross_premium    = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100
                          for t in all_trades)
    prem_capture_pct = (total_pnl / gross_premium * 100) if gross_premium > 0 else 0.0

    day_pnls: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
    days_traded        = len(day_pnls)
    avg_trades_per_day = n / days_traded if days_traded else 0
    best_day           = max(day_pnls.values()) if day_pnls else 0
    worst_day          = min(day_pnls.values()) if day_pnls else 0

    rf_daily   = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals = list(day_pnls.values())
    n_days     = len(daily_vals)
    mean_daily = sum(daily_vals) / n_days if n_days else 0
    var_daily  = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily  = math.sqrt(var_daily)
    ann_vol    = std_daily * math.sqrt(252)
    sharpe     = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0
    downside_d = [p for p in daily_vals if p < mean_daily]
    down_var_d = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino    = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    eq_d = peak_d = 0.0; underwater_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]; peak_d = max(peak_d, eq_d)
        if eq_d < peak_d: underwater_days += 1
    time_underwater_pct = underwater_days / days_traded * 100 if days_traded else 0

    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        first_d = _dtt.strptime(sorted_dates[0],  "%Y%m%d")
        last_d  = _dtt.strptime(sorted_dates[-1], "%Y%m%d")
        years   = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    final_equity    = ACCOUNT_SIZE + total_pnl
    cagr            = ((final_equity / ACCOUNT_SIZE) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar          = cagr / abs(max_dd / ACCOUNT_SIZE * 100) if max_dd != 0 else float("inf")
    recovery_factor = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")
    total_commission = sum(2 * 2 * float(t.get("qty", 1)) * COMMISSION for t in all_trades)
    bp_per_trade = [(float(t.get("spread_width", WIDTH)) - float(t.get("credit_received", 0)))
                    * float(t.get("qty", 1)) * 100 for t in all_trades]
    avg_bp_per_trade = sum(bp_per_trade) / n if n else 0
    day_bp: dict[str, float] = {}
    for t, bp in zip(all_trades, bp_per_trade):
        day_bp[t["entry_date"]] = day_bp.get(t["entry_date"], 0.0) + bp
    max_bp_day = max(day_bp.values()) if day_bp else 0
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0

    # -- Helpers ---------------------------------------------------------------
    def _sf(val):
        """Safe float -- returns float or None."""
        try:
            return float(val) if val not in (None, "") else None
        except (ValueError, TypeError):
            return None

    def _bucket_stats(bucket):
        bp = [float(t.get("pnl_earned", 0)) for t in bucket]
        m  = len(bp)
        if m == 0:
            return 0, 0.0, 0.0, 0.0
        w  = sum(1 for x in bp if x > 0)
        eq2 = pk2 = dd2 = 0.0
        for x in bp:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        return m, w / m * 100, sum(bp) / m, sum(bp)

    # -- VIX correlation table -------------------------------------------------
    vix_rows = []
    for label, lo, hi in VIX_BUCKETS:
        bucket = [t for t in all_trades if
                  _sf(t.get("vix_level")) is not None and
                  (lo is None or _sf(t.get("vix_level")) >= lo) and
                  (hi is None or _sf(t.get("vix_level")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        vix_rows.append((label, cnt, wr, avg_p, tot_p))

    # -- Strike distance table -------------------------------------------------
    sd_rows = []
    for label, lo, hi in STRIKE_DISTANCE_BUCKETS:
        bucket = [t for t in all_trades if
                  _sf(t.get("strike_distance")) is not None and
                  _sf(t.get("strike_distance")) >= lo and
                  (hi is None or _sf(t.get("strike_distance")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        sd_rows.append((label, cnt, wr, avg_p, tot_p))

    # -- PUT vs CALL split -----------------------------------------------------
    pc_rows = []
    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        m = compute_metrics(bucket)
        avg_p = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0.0
        pf_str = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf"
        pc_rows.append((label, m["num_trades"], m["win_rate"], avg_p,
                        m["total_pnl"], m["avg_win"], m["avg_loss"], pf_str, m["max_drawdown"]))

    # -- Dynamic SL VIX zones --------------------------------------------------
    vl = DYNAMIC_SL_VIX_LOW; ml, mh = DYNAMIC_SL_VIX_MID; hl, hh = DYNAMIC_SL_VIX_HIGH
    dyn_buckets = [
        (f"< {vl}",   None, vl,   True),
        (f"{ml}-{mh}", ml,  mh,   True),
        (f"{mh}-{hl}", mh,  hl,   False),
        (f"{hl}-{hh}", hl,  hh,   True),
        (f"> {hh}",    hh,  None, False),
    ]
    dyn_rows = []
    for label, lo, hi, sl_on in dyn_buckets:
        bucket = [t for t in all_trades if
                  _sf(t.get("vix_level")) is not None and
                  (lo is None or _sf(t.get("vix_level")) >= lo) and
                  (hi is None or _sf(t.get("vix_level")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        bp2 = [float(x.get("pnl_earned", 0)) for x in bucket]
        eq2 = pk2 = dd2 = 0.0
        for x in bp2:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        dyn_rows.append((label, "ON" if sl_on else "--", cnt, wr, avg_p, tot_p, dd2))

    # -- Monthly P&L -----------------------------------------------------------
    month_pnl: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    month_trades_map: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        y = int(t["entry_date"][:4]); m2 = int(t["entry_date"][4:6])
        month_pnl[y][m2] += t["pnl_earned"]
        month_trades_map[y][m2].append(t["pnl_earned"])

    def _month_max_dd(pl_list):
        eq2 = pk2 = dd2 = 0.0
        for x in pl_list:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        return dd2

    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    all_years = sorted(month_pnl.keys())

    # -- Large loss days -------------------------------------------------------
    loss_day_pnl, loss_day_trades, loss_day_vix, loss_day_wins, loss_day_losses = \
        _aggregate_loss_days(all_trades)
    worst_15 = sorted([(pnl, d2) for d2, pnl in loss_day_pnl.items() if pnl < 0])[:15]

    # -- Build markdown --------------------------------------------------------
    run_ts = _dtt.now().strftime("%Y-%m-%d %H:%M:%S")
    period = f"{date_list[0].strftime('%Y-%m-%d')} -> {date_list[-1].strftime('%Y-%m-%d')}"

    L = [
        "\n---\n",
        f"## Run: {run_ts}",
        f"**Period:** {period}  |  **Days traded:** {days_traded}  |  **Total trades:** {n}",
        "",
        "### Returns",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total P&L (net) | ${total_pnl:,.2f} |",
        f"| Gross premium collected | ${gross_premium:,.2f} |",
        f"| Premium capture rate | {prem_capture_pct:.1f}% |",
        f"| CAGR | {cagr:.1f}% |",
        f"| Avg profit/trade | ${avg_win:,.2f} |",
        f"| Avg loss/trade | ${avg_loss:,.2f} |",
        f"| Expectancy/trade | ${expectancy:,.2f} |",
        f"| Profit factor | {profit_factor:.2f}x |",
        "",
        "### Risk",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Max drawdown | ${max_dd:,.2f} ({max_dd_pct:.1f}%) |",
        f"| Calmar ratio | {calmar:.2f} |",
        f"| Recovery factor | {recovery_factor:.2f} |",
        f"| Time underwater | {time_underwater_pct:.1f}% ({underwater_days} of {days_traded} days) |",
        "",
        "### Key Config",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| Width | {int(WIDTH)}pt |",
        f"| QTY | {QTY} |",
        f"| Min credit | ${MIN_NET_CREDIT:.2f} |",
        f"| Min OTM dist | {MIN_OTM_DISTANCE}pt |",
        f"| Entry window | {ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min |",
        f"| Dynamic SL | {'on' if ENABLE_DYNAMIC_SL else 'off'} ${DYNAMIC_SL_AMOUNT if ENABLE_DYNAMIC_SL else ''} |",
        f"| EOM SL | {'on' if ENABLE_EOM_SL else 'off'} {'$'+str(int(EOM_SL_AMOUNT)) if ENABLE_EOM_SL else ''} |",
        f"| CPI SL | {'on $'+str(int(CPI_SL_AMOUNT)) if ENABLE_CPI_SL else 'off'} |",
        f"| PCE SL | {'on $'+str(int(PCE_SL_AMOUNT)) if ENABLE_PCE_SL else 'off'} |",
        f"| EOQ SL | {'on $'+str(int(EOQ_SL_AMOUNT)) if ENABLE_EOQ_SL else 'off'} |",
        f"| Pre-TW SL | {'on $'+str(int(PRE_TW_SL_AMOUNT)) if ENABLE_PRE_TW_SL else 'off'} |",
        f"| Post-holiday SL | {'on $'+str(int(POST_HOL_SL_AMOUNT)) if ENABLE_POST_HOL_SL else 'off'} |",
        "",
        "### Ratios & Volatility",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Win rate | {win_rate:.1f}% |",
        f"| Sharpe ratio | {sharpe:.2f} |",
        f"| Sortino ratio | {sortino:.2f} |",
        f"| Ann. volatility | ${ann_vol:,.2f} |",
        "",
        "### Day Stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Best day | ${best_day:,.2f} |",
        f"| Worst day | ${worst_day:,.2f} |",
        f"| Avg trades/day | {avg_trades_per_day:.1f} |",
        "",
        "### Cost & Capital",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Commission total | ${total_commission:,.2f} |",
        f"| Avg BP/trade | ${avg_bp_per_trade:,.2f} |",
        f"| Avg BP/day | ${avg_bp_day:,.2f} |",
        f"| Max BP day | ${max_bp_day:,.2f} |",
        "",
        "### VIX Correlation",
        "| VIX Range | Trades | Win Rate | Avg P&L | Total P&L |",
        "|-----------|-------:|--------:|--------:|----------:|",
    ]
    for label, cnt, wr, avg_p, tot_p in vix_rows:
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} |")

    L += [
        "",
        "### Dynamic SL VIX Zones",
        "| VIX Range | Dyn SL | Trades | Win Rate | Avg P&L | Total P&L | Max DD |",
        "|-----------|:------:|-------:|--------:|--------:|----------:|-------:|",
    ]
    for label, sl_flag, cnt, wr, avg_p, tot_p, mdd in dyn_rows:
        L.append(f"| {label} | {sl_flag} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} | ${mdd:,.2f} |")

    L += [
        "",
        "### Strike Distance vs Win Rate",
        "| Distance | Trades | Win Rate | Avg P&L | Total P&L |",
        "|----------|-------:|--------:|--------:|----------:|",
    ]
    for label, cnt, wr, avg_p, tot_p in sd_rows:
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} |")

    L += [
        "",
        "### PUT vs CALL Split",
        "| Type | Trades | Win Rate | Avg P&L | Total P&L | Avg Win | Avg Loss | Prof Factor | Max DD |",
        "|------|-------:|--------:|--------:|----------:|--------:|---------:|------------:|-------:|",
    ]
    for row in pc_rows:
        label, cnt, wr, avg_p, tot_p, aw, al, pf_str, mdd = row
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} | ${aw:,.2f} | ${al:,.2f} | {pf_str} | ${mdd:,.2f} |")

    # Monthly P&L table
    L += ["", "### Monthly P&L ($)"]
    L.append("| Year | " + " | ".join(mon_abbr) + " | Total |")
    L.append("|------|" + "|".join(["------:"] * 12) + "|------:|")
    yearly_totals: dict[int, float] = {}
    for y in all_years:
        ytotal = sum(month_pnl[y].get(m2, 0.0) for m2 in range(1, 13) if m2 in month_pnl[y])
        yearly_totals[y] = ytotal
        cells = []
        for m2 in range(1, 13):
            pval = month_pnl[y].get(m2)
            cells.append(f"${pval:,.0f}" if pval is not None else "--")
        L.append(f"| {y} | " + " | ".join(cells) + f" | ${ytotal:,.0f} |")
    # Grand total row
    gtotal_cells = []
    grand_total = 0.0
    for m2 in range(1, 13):
        col_sum = sum(month_pnl[y].get(m2, 0.0) for y in all_years if m2 in month_pnl[y])
        has_data = any(m2 in month_pnl[y] for y in all_years)
        gtotal_cells.append(f"${col_sum:,.0f}" if has_data else "--")
        if has_data: grand_total += col_sum
    L.append(f"| **Total** | " + " | ".join(gtotal_cells) + f" | **${grand_total:,.0f}** |")

    # Intra-month max drawdown table
    L += ["", "### Intra-Month Max Drawdown ($)"]
    L.append("| Year | " + " | ".join(mon_abbr) + " | Annual DD |")
    L.append("|------|" + "|".join(["------:"] * 12) + "|----------:|")
    for y in all_years:
        cells = []
        for m2 in range(1, 13):
            tlist = month_trades_map[y].get(m2)
            cells.append(f"${_month_max_dd(tlist):,.0f}" if tlist else "--")
        yr_dd = _month_max_dd([t["pnl_earned"] for t in sorted(all_trades, key=lambda x: x["entry_date"]) if int(t["entry_date"][:4]) == y])
        L.append(f"| {y} | " + " | ".join(cells) + f" | ${yr_dd:,.0f} |")

    # Strategy vs SPY comparison
    spy_ret_md = _get_spy_monthly_returns(all_years[0], all_years[-1])
    if spy_ret_md:
        L += ["", "### Monthly Return: Strategy vs SPY (strat% / SPY%)"]
        L.append("| Year | " + " | ".join(mon_abbr) + " | Annual Strat | Annual SPY | Alpha |")
        L.append("|------|" + "|".join([":------:"] * 12) + "|------------:|-----------:|------:|")
        for y in all_years:
            cells = []
            s_ann = sp_ann = 0.0
            for m2 in range(1, 13):
                spnl  = month_pnl[y].get(m2)
                spy_m = spy_ret_md.get((y, m2))
                if spnl is None and spy_m is None:
                    cells.append("--")
                else:
                    s_pct  = f"{spnl/ACCOUNT_SIZE*100:+.1f}%" if spnl  is not None else "--"
                    sp_pct = f"{spy_m:+.1f}%"                 if spy_m is not None else "--"
                    if spnl  is not None: s_ann  += spnl / ACCOUNT_SIZE * 100
                    if spy_m is not None: sp_ann += spy_m
                    cells.append(f"{s_pct} / {sp_pct}")
            L.append(f"| {y} | " + " | ".join(cells) +
                     f" | {s_ann:+.1f}% | {sp_ann:+.1f}% | {s_ann-sp_ann:+.1f}% |")

    # Large loss days
    _ev_md = _build_calendar_event_dates()
    L += ["", "### Largest Loss Days (worst 15)"]
    L.append("| Date | VIX | Trades | W/L | Day P&L | Event |")
    L.append("|------|----:|-------:|----:|--------:|-------|")
    for pnl, d2 in worst_15:
        date_fmt  = f"{d2[:4]}-{d2[4:6]}-{d2[6:]}"
        vix_val   = loss_day_vix.get(d2)
        vix_str   = f"{vix_val:.1f}" if vix_val is not None else "?"
        w         = loss_day_wins.get(d2, 0)
        l         = loss_day_losses.get(d2, 0)
        event_lbl = _get_day_event_labels(d2, _ev_md)
        L.append(f"| {date_fmt} | {vix_str} | {loss_day_trades[d2]} | {w}W/{l}L | ${pnl:,.2f} | {event_lbl} |")

    # Entry time breakdown
    import datetime as _dt_mod
    et_buckets: dict = {}
    for t in all_trades:
        slot = t.get("entry_time", "")[:5]
        if slot not in et_buckets:
            et_buckets[slot] = {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []}
        et_buckets[slot]["pnl"] += t["pnl_earned"]
        et_buckets[slot]["pnls"].append(t["pnl_earned"])
        et_buckets[slot]["wins" if t.get("win") else "losses"] += 1
    for b in et_buckets.values():
        eq = pk = dd = 0.0
        for p in b["pnls"]:
            eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        b["max_dd"] = dd

    L += ["", "### Entry Time Breakdown"]
    L.append("| Time | Trades | W/L | WR% | Total P&L | Max DD |")
    L.append("|------|-------:|----:|----:|----------:|-------:|")
    for slot in sorted(et_buckets.keys()):
        b = et_buckets[slot]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0.0
        L.append(f"| {slot} | {total} | {b['wins']}W/{b['losses']}L | {wr:.1f}% | ${b['pnl']:,.2f} | ${b['max_dd']:,.2f} |")

    # Seasonality
    dow_b: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(5)}
    mon_b: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(1, 13)}
    for t in all_trades:
        d3 = t.get("entry_date", "")
        if len(d3) != 8:
            continue
        try:
            dt = _dt_mod.date(int(d3[:4]), int(d3[4:6]), int(d3[6:]))
        except ValueError:
            continue
        pnl3 = t["pnl_earned"]
        win3 = bool(t.get("win"))
        dow_b[dt.weekday()]["pnl"] += pnl3
        mon_b[dt.month]["pnl"]     += pnl3
        dow_b[dt.weekday()]["wins" if win3 else "losses"] += 1
        mon_b[dt.month]["wins" if win3 else "losses"]     += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    mon_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    L += ["", "### Seasonality -- Day of Week"]
    L.append("| Day | Trades | WR% | Total P&L | Avg P&L |")
    L.append("|-----|-------:|----:|----------:|--------:|")
    for i, name in enumerate(dow_names):
        b = dow_b[i]
        total = b["wins"] + b["losses"]
        wr  = b["wins"] / total * 100 if total else 0.0
        avg = b["pnl"] / total if total else 0.0
        L.append(f"| {name} | {total} | {wr:.1f}% | ${b['pnl']:,.2f} | ${avg:,.2f} |")

    L += ["", "### Seasonality -- Month"]
    L.append("| Month | Trades | WR% | Total P&L | Avg P&L |")
    L.append("|-------|-------:|----:|----------:|--------:|")
    for i, name in enumerate(mon_names, 1):
        b = mon_b[i]
        total = b["wins"] + b["losses"]
        wr  = b["wins"] / total * 100 if total else 0.0
        avg = b["pnl"] / total if total else 0.0
        L.append(f"| {name} | {total} | {wr:.1f}% | ${b['pnl']:,.2f} | ${avg:,.2f} |")

    # Econ date analysis
    day_pnl_all: dict[str, float] = {}
    for t in all_trades:
        d4 = t["entry_date"]
        day_pnl_all[d4] = day_pnl_all.get(d4, 0.0) + t["pnl_earned"]

    def _econ_stats(date_set):
        pnls = [day_pnl_all[d] for d in day_pnl_all if d in date_set]
        if not pnls: return 0, 0.0, 0.0, 0.0, 0.0
        wins  = sum(1 for p in pnls if p > 0)
        wr    = wins / len(pnls) * 100
        avg   = sum(pnls) / len(pnls)
        total = sum(pnls)
        eq = pk = dd = 0.0
        for p in pnls:
            eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        return len(pnls), wr, avg, total, dd

    nfp_set_md   = {d for d in ECON_DATES if d not in _CPI_DATES and d not in _PCE_DATES}
    tw_set_md    = {d for d in day_pnl_all if d in TRIPLE_WITCHING_DATES}
    fomc_set_md  = {d for d in day_pnl_all if d in FOMC_DATES}
    normal_md    = {d for d in day_pnl_all
                    if d not in ECON_DATES and d not in tw_set_md and d not in fomc_set_md
                    and d not in _EOM_DATES and d not in _EOQ_DATES
                    and d not in _PRE_TW_DATES and d not in _POST_HOL_DATES}

    md_event_types = [
        ("Normal",       normal_md),
        ("CPI",          {d for d in day_pnl_all if d in _CPI_DATES}),
        ("PCE",          {d for d in day_pnl_all if d in _PCE_DATES}),
        ("NFP",          {d for d in day_pnl_all if d in nfp_set_md}),
        ("FOMC",         fomc_set_md),
        ("Triple Witch", tw_set_md),
        ("EOM",          {d for d in day_pnl_all if d in _EOM_DATES}),
        ("EOQ",          {d for d in day_pnl_all if d in _EOQ_DATES}),
        ("Pre-TW",       {d for d in day_pnl_all if d in _PRE_TW_DATES}),
        ("Post-Holiday", {d for d in day_pnl_all if d in _POST_HOL_DATES}),
    ]

    L += ["", "### Calendar / Econ Event Analysis"]
    L.append("| Event | Days | WR% | Avg/Day | Total P&L | Max DD |")
    L.append("|-------|-----:|----:|--------:|----------:|-------:|")
    for label, dset in md_event_types:
        n, wr, avg, total, dd = _econ_stats(dset)
        if n == 0:
            continue
        L.append(f"| {label} | {n} | {wr:.1f}% | ${avg:,.2f} | ${total:,.2f} | ${dd:,.2f} |")

    L.append("")

    results_path = "RESULTS.md"
    new_block = "\n".join(L)
    if os.path.exists(results_path):
        existing = open(results_path).read()
        # Strip the static header so we can re-prepend it cleanly
        header = "# MEDS Strategy -- Backtest Results\n\n"
        body = existing[len(header):] if existing.startswith(header) else existing
        with open(results_path, "w") as f:
            f.write(header + new_block + "\n" + body)
    else:
        with open(results_path, "w") as f:
            f.write("# MEDS Strategy -- Backtest Results\n\n" + new_block + "\n")

    logger.info(f"  Results prepended to {results_path}")


# ---------------------------------------------
#  SETTINGS SUMMARY
# ---------------------------------------------
def print_settings_summary() -> None:
    sep = "-" * 62
    logger.info(sep)
    logger.info("  STRATEGY SETTINGS")
    logger.info(sep)
    logger.info(f"  Period          : {PILOT_YEAR_START} -> {PILOT_YEAR_END}")
    logger.info(f"  Spread          : {int(WIDTH)}pt width  |  min credit ${MIN_NET_CREDIT:.2f}")
    logger.info(f"  Entry window    : {ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')}  every {ENTRY_INTERVAL} min")
    logger.info(f"  EMA direction   : fast={EMA_FAST}  slow={EMA_SLOW}")
    logger.info(f"  Daily SL / TP   : {DAILY_SL if DAILY_SL is not None else 'off'}  /  {DAILY_TP if DAILY_TP is not None else 'off'}")
    eom_str = f"${EOM_SL_AMOUNT:,.0f} on EOM days" if ENABLE_EOM_SL else "off"
    logger.info(f"  EOM stop loss   : {eom_str}")
    dyn_str = (f"${DYNAMIC_SL_AMOUNT:,.0f}  VIX<{DYNAMIC_SL_VIX_LOW} or "
               f"{DYNAMIC_SL_VIX_MID[0]}-{DYNAMIC_SL_VIX_MID[1]} or "
               f"{DYNAMIC_SL_VIX_HIGH[0]}-{DYNAMIC_SL_VIX_HIGH[1]}")
    logger.info(f"  Dynamic SL      : {dyn_str}")
    logger.info(f"  VIX filter      : min={VIX_MIN_FILTER or 'off'}  max={VIX_MAX_FILTER or 'off'}")
    logger.info(f"  Max BP          : {f'${MAX_BUYING_POWER:,.0f}' if MAX_BUYING_POWER is not None else 'unlimited'}")
    # Active day filters (non-None only)
    active_filters = {k: v for k, v in [
        ("vix_max",    DAY_FILTER_VIX_MAX),   ("vix_min",    DAY_FILTER_VIX_MIN),
        ("vix_chg_max",DAY_FILTER_VIX_CHG_MAX),("vix_chg_min",DAY_FILTER_VIX_CHG_MIN),
        ("rsi_min",    DAY_FILTER_RSI_MIN),    ("rsi_max",    DAY_FILTER_RSI_MAX),
        ("atr_max",    DAY_FILTER_ATR_MAX),    ("adx_min",    DAY_FILTER_ADX_MIN),
        ("range_max",  DAY_FILTER_RANGE_MAX),  ("range_min",  DAY_FILTER_RANGE_MIN),
        ("exp_mv_max", DAY_FILTER_EXP_MOVE_MAX),("exp_mv_min",DAY_FILTER_EXP_MOVE_MIN),
        ("cci_max",    DAY_FILTER_CCI_MAX),    ("ivr_min",    DAY_FILTER_IVR_MIN),
        ("gap_max",    DAY_FILTER_GAP_MAX),    ("sma200",     DAY_FILTER_ABOVE_SMA200),
        ("body_max",   DAY_FILTER_BODY_MAX),   ("knife_max",  DAY_FILTER_KNIFE_MAX),
    ] if v is not None}
    if active_filters:
        fstr = "  ".join(f"{k}={v}" for k, v in active_filters.items())
        logger.info(f"  Day filters     : {fstr}")
    else:
        logger.info(f"  Day filters     : none")
    # Calendar risk SL summary
    cal_sl_parts = []
    if ENABLE_CPI_SL:      cal_sl_parts.append(f"CPI ${CPI_SL_AMOUNT:.0f}")
    if ENABLE_PCE_SL:      cal_sl_parts.append(f"PCE ${PCE_SL_AMOUNT:.0f}")
    if ENABLE_EOQ_SL:      cal_sl_parts.append(f"EOQ ${EOQ_SL_AMOUNT:.0f}")
    if ENABLE_PRE_TW_SL:   cal_sl_parts.append(f"pre-TW ${PRE_TW_SL_AMOUNT:.0f}")
    if ENABLE_POST_HOL_SL: cal_sl_parts.append(f"post-hol ${POST_HOL_SL_AMOUNT:.0f}")
    logger.info(f"  Calendar SL     : {', '.join(cal_sl_parts) if cal_sl_parts else 'off'}")
    logger.info(sep)


# ---------------------------------------------
#  DYNAMIC SL VIX RANGE ANALYSIS
# ---------------------------------------------
def print_dynamic_sl_vix_analysis(all_trades: list) -> None:
    """Break down stats by the key VIX ranges that drive dynamic SL behaviour."""
    vl  = DYNAMIC_SL_VIX_LOW
    ml, mh = DYNAMIC_SL_VIX_MID
    hl, hh = DYNAMIC_SL_VIX_HIGH

    # Buckets: (label, lo, hi, dynamic_sl_active)
    buckets = [
        (f"< {vl}",          None, vl,   True),
        (f"{ml}-{mh}",       ml,   mh,   True),
        (f"{mh}-{hl}",       mh,   hl,   False),
        (f"{hl}-{hh}",       hl,   hh,   True),
        (f"> {hh}",          hh,   None, False),
    ]

    def bucket_stats(trades):
        pnls = [float(t.get("pnl_earned", 0)) for t in trades]
        n    = len(pnls)
        if n == 0:
            return dict(n=0, wr=0, avg=0, total=0, max_dd=0)
        wins = [p for p in pnls if p > 0]
        eq = peak = dd = 0.0
        for p in pnls:
            eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
        return dict(
            n=n,
            wr=len(wins)/n*100,
            avg=sum(pnls)/n,
            total=sum(pnls),
            max_dd=dd,
        )

    sep = "-" * 80
    logger.info(sep)
    logger.info("  VIX RANGE ANALYSIS  (dynamic SL zones)")
    logger.info(sep)
    logger.info(
        f"  {'VIX Range':<12} {'DynSL':>6} {'Trades':>7} {'WR%':>7} "
        f"{'Avg P&L':>10} {'Total P&L':>12} {'Max DD':>11}"
    )
    logger.info(sep)

    for label, lo, hi, sl_on in buckets:
        bucket = []
        for t in all_trades:
            try:
                v = float(t.get("vix_level") or "")
            except (ValueError, TypeError):
                continue
            if (lo is None or v >= lo) and (hi is None or v < hi):
                bucket.append(t)
        s = bucket_stats(bucket)
        sl_flag = "ON" if sl_on else "--"
        logger.info(
            f"  {label:<12} {sl_flag:>6} {s['n']:>7} {s['wr']:>6.1f}% "
            f"${s['avg']:>9,.2f} ${s['total']:>11,.2f} ${s['max_dd']:>10,.2f}"
        )

    logger.info(sep)


# ---------------------------------------------
#  MONTHLY P&L TABLE
# ---------------------------------------------
def print_monthly_pnl_table(all_trades: list) -> None:
    from collections import defaultdict

    # Build day_pnl map and monthly buckets
    day_pnl: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d] = day_pnl.get(d, 0.0) + t["pnl_earned"]

    # month_pnl[year][month] = total P&L
    month_pnl: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    # month_trades[year][month] = list of trade pnls in order (for intra-month DD)
    month_trades: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))

    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        y = int(t["entry_date"][:4])
        m = int(t["entry_date"][4:6])
        month_pnl[y][m] += t["pnl_earned"]
        month_trades[y][m].append(t["pnl_earned"])

    # Intra-month max drawdown
    def month_max_dd(pnls):
        eq = peak = dd = 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        return dd

    years = sorted(month_pnl.keys())
    months = list(range(1, 13))
    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    col_w = 9
    hdr = f"  {'Year':<6}" + "".join(f"{m:>{col_w}}" for m in mon_abbr) + f"{'Total':>{col_w+1}}"
    sep = "-" * len(hdr)

    logger.info(sep)
    logger.info("  MONTHLY P&L  ($)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    yearly_totals: dict[int, float] = {}
    for y in years:
        row = f"  {y:<6}"
        ytotal = 0.0
        for m in months:
            pnl = month_pnl[y].get(m)
            if pnl is None:
                row += f"{'--':>{col_w}}"
            else:
                ytotal += pnl
                row += f"{pnl:>{col_w},.0f}"
        row += f"{ytotal:>{col_w+1},.0f}"
        yearly_totals[y] = ytotal
        logger.info(row)

    # Grand total row
    logger.info(sep)
    grand = f"  {'Total':<6}"
    gtotal = 0.0
    for m in months:
        col_sum = sum(month_pnl[y].get(m, 0.0) for y in years if m in month_pnl[y])
        if col_sum == 0.0 and all(m not in month_pnl[y] for y in years):
            grand += f"{'--':>{col_w}}"
        else:
            grand += f"{col_sum:>{col_w},.0f}"
            gtotal += col_sum
    grand += f"{gtotal:>{col_w+1},.0f}"
    logger.info(grand)
    logger.info(sep)

    # Max drawdown by year table
    logger.info("  INTRA-MONTH MAX DRAWDOWN  ($)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for y in years:
        row = f"  {y:<6}"
        for m in months:
            trades_m = month_trades[y].get(m)
            if not trades_m:
                row += f"{'--':>{col_w}}"
            else:
                dd = month_max_dd(trades_m)
                row += f"{dd:>{col_w},.0f}"
        yr_dd = month_max_dd([t["pnl_earned"] for t in sorted(all_trades, key=lambda x: x["entry_date"]) if int(t["entry_date"][:4]) == y])
        row += f"{yr_dd:>{col_w+1},.0f}"
        logger.info(row)
    logger.info(sep)


# ---------------------------------------------
#  SPY BENCHMARK COMPARISON
# ---------------------------------------------
SPY_CACHE_FILE = "spy_monthly_closes.csv"   # local cache: date (YYYY-MM-DD), close

def _get_spy_monthly_returns(first_year: int, last_year: int) -> "dict[tuple,float]":
    """Return {(year, month): pct_return} for SPY.

    Reads from SPY_CACHE_FILE when present; only calls yfinance for months
    not yet in the cache. Saves updated closes back to disk.
    """
    from datetime import date as _date

    # -- Load existing cache ---------------------------------------------------
    cached: dict[str, float] = {}   # "YYYY-MM-DD" -> adjusted close
    if os.path.exists(SPY_CACHE_FILE):
        try:
            with open(SPY_CACHE_FILE, newline="") as f:
                for row in csv.DictReader(f):
                    cached[row["date"]] = float(row["close"])
        except Exception:
            cached = {}

    # -- Determine what's missing ----------------------------------------------
    # We need the month-end close for every month from (first_year-1, 12) through
    # (last_year, 12) -- the extra prior month is needed to compute the first return.
    today = _date.today()
    needed_months = []
    for y in range(first_year - 1, last_year + 1):
        for m in range(1, 13):
            # skip future months
            if y > today.year or (y == today.year and m > today.month):
                continue
            needed_months.append((y, m))

    # Determine which month-end dates are missing from cache
    def _month_end_key(y, m):
        """Return the last calendar day of the month as YYYY-MM-DD string."""
        import calendar
        last_day = calendar.monthrange(y, m)[1]
        return f"{y}-{m:02d}-{last_day:02d}"

    missing = [(y, m) for y, m in needed_months if _month_end_key(y, m) not in cached]

    # -- Fetch missing data from yfinance --------------------------------------
    if missing:
        try:
            import yfinance as yf
            fetch_start_y = min(y for y, _ in missing)
            fetch_start_m = min(m for y, m in missing if y == fetch_start_y)
            # go one month back to ensure we get a prior-month close for the first return
            if fetch_start_m == 1:
                fetch_start_y -= 1; fetch_start_m = 12
            else:
                fetch_start_m -= 1
            fetch_start = f"{fetch_start_y}-{fetch_start_m:02d}-01"
            fetch_end   = f"{last_year + 1}-01-01"
            spy = yf.download("SPY", start=fetch_start, end=fetch_end,
                               auto_adjust=True, progress=False)
            if not spy.empty:
                monthly = spy["Close"].resample("ME").last()
                if hasattr(monthly, "columns"):   # multi-index from newer yfinance
                    monthly = monthly.iloc[:, 0]
                for ts, close in monthly.items():
                    import math as _math
                    v = float(close)
                    if not _math.isnan(v):
                        cached[ts.strftime("%Y-%m-%d")] = v
                # Persist updated cache
                with open(SPY_CACHE_FILE, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["date", "close"])
                    for d_str in sorted(cached):
                        w.writerow([d_str, f"{cached[d_str]:.6f}"])
                logger.info(f"  SPY cache updated -> {SPY_CACHE_FILE} ({len(cached)} month-end rows)")
        except Exception as exc:
            logger.warning(f"  SPY fetch failed: {exc}")

    # -- Compute monthly returns from cached closes ----------------------------
    sorted_closes = sorted((d, v) for d, v in cached.items())
    returns: dict[tuple, float] = {}
    for i in range(1, len(sorted_closes)):
        d_str, close = sorted_closes[i]
        prev_close   = sorted_closes[i - 1][1]
        y, m = int(d_str[:4]), int(d_str[5:7])
        if prev_close and prev_close != 0:
            returns[(y, m)] = (close / prev_close - 1) * 100
    return returns


def print_spy_comparison(all_trades: list) -> None:
    """Year × Month table comparing strategy monthly return % vs SPY monthly return %."""
    from collections import defaultdict

    # Build strategy monthly P&L
    month_pnl: dict[tuple, float] = defaultdict(float)
    for t in all_trades:
        y, m = int(t["entry_date"][:4]), int(t["entry_date"][4:6])
        month_pnl[(y, m)] += t["pnl_earned"]

    if not month_pnl:
        return

    all_years = sorted({y for y, _ in month_pnl})
    spy_ret   = _get_spy_monthly_returns(all_years[0], all_years[-1])

    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    col_w    = 13   # wide enough for "str% / str%"
    hdr      = f"  {'Year':<6}" + "".join(f"{m:^{col_w}}" for m in mon_abbr) + f"{'Annual':^{col_w+2}}"
    sep      = "-" * len(hdr)

    logger.info(sep)
    logger.info("  MONTHLY RETURN: STRATEGY vs SPY  (strat% / SPY%  |  alpha=strat-SPY)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    for y in all_years:
        row = f"  {y:<6}"
        strat_annual = 0.0
        spy_annual   = 0.0
        for m in range(1, 13):
            spnl   = month_pnl.get((y, m))
            spy_m  = spy_ret.get((y, m))
            if spnl is None and spy_m is None:
                row += f"{'--':^{col_w}}"
            else:
                s_pct  = (spnl  / ACCOUNT_SIZE * 100) if spnl  is not None else None
                sp_pct = spy_m if spy_m is not None else None
                if s_pct is not None:  strat_annual += s_pct
                if sp_pct is not None: spy_annual   += sp_pct
                s_str  = f"{s_pct:+.1f}%" if s_pct  is not None else "   -- "
                sp_str = f"{sp_pct:+.1f}%" if sp_pct is not None else "   -- "
                cell   = f"{s_str}/{sp_str}"
                row   += f"{cell:^{col_w}}"
        # Annual column
        a_str  = f"{strat_annual:+.1f}%"
        sp_a   = f"{spy_annual:+.1f}%" if spy_annual != 0.0 else "--"
        alpha  = strat_annual - spy_annual
        row   += f"  {a_str}/{sp_a} (α{alpha:+.1f}%)"
        logger.info(row)

    # Grand totals row
    logger.info(sep)
    total_strat = sum(month_pnl.values()) / ACCOUNT_SIZE * 100
    total_spy   = sum(spy_ret.get((y, m), 0.0) for y in all_years for m in range(1, 13)
                      if (y, m) in month_pnl)
    logger.info(f"  {'Total':<6}  Cumul strategy: {total_strat:+.1f}%  |  "
                f"Cumul SPY: {total_spy:+.1f}%  |  Alpha: {total_strat-total_spy:+.1f}%")
    logger.info(sep)


# ---------------------------------------------
#  LARGE LOSS DAYS
# ---------------------------------------------
def _aggregate_loss_days(all_trades: list):
    """Return (day_pnl, day_trades, day_vix, day_wins, day_losses) dicts keyed by YYYYMMDD."""
    day_pnl: dict[str, float] = {}
    day_trades: dict[str, int] = {}
    day_vix: dict[str, float] = {}
    day_wins: dict[str, int] = {}
    day_losses: dict[str, int] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d]    = day_pnl.get(d, 0.0) + t["pnl_earned"]
        day_trades[d] = day_trades.get(d, 0) + 1
        if d not in day_vix and t.get("vix_level"):
            try:
                day_vix[d] = float(t["vix_level"])
            except (ValueError, TypeError):
                pass
        day_wins[d]   = day_wins.get(d, 0) + int(t.get("win", 0) or 0)
        day_losses[d] = day_losses.get(d, 0) + int(t.get("loss", 0) or 0)
    return day_pnl, day_trades, day_vix, day_wins, day_losses


def _get_day_event_labels(date_str: str, ev: "dict[str, set]") -> str:
    """Return '+'-joined event labels for date_str using a pre-built event dict."""
    label_map = [
        ("FOMC",     ev.get("fomc", set())),
        ("TW",       ev.get("triple_witching", set())),
        ("Pre-TW",   ev.get("pre_triple_witching", set())),
        ("CPI",      ev.get("cpi", set())),
        ("PPI",      ev.get("ppi", set())),
        ("PCE",      ev.get("pce", set())),
        ("NFP",      ev.get("nfp", set())),
        ("EOM",      ev.get("end_of_month", set())),
        ("EOQ",      ev.get("end_of_quarter", set())),
        ("OPEX",     ev.get("monthly_opex", set())),
        ("Post-Hol", ev.get("post_major_holiday", set())),
    ]
    labels = [lbl for lbl, s in label_map if date_str in s]
    return "+".join(labels) if labels else ""


def print_large_loss_days(all_trades: list, n: int = 15) -> None:
    day_pnl, day_trades, day_vix, day_wins, day_losses = _aggregate_loss_days(all_trades)
    loss_days = sorted([(pnl, d) for d, pnl in day_pnl.items() if pnl < 0])
    if not loss_days:
        return

    ev = _build_calendar_event_dates()
    top_n = loss_days[:n]
    sep = "-" * 80
    logger.info(sep)
    logger.info(f"  LARGEST LOSS DAYS  (worst {n})")
    logger.info(sep)
    logger.info(f"  {'Date':<12} {'VIX':>5}  {'Trades':>6}  {'W/L':<7} {'Day P&L':>12}  {'Event'}")
    logger.info(sep)
    for pnl, d in top_n:
        date_fmt  = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        vix_val   = day_vix.get(d)
        vix_str   = f"{vix_val:.1f}" if vix_val is not None else "?"
        w  = day_wins.get(d, 0)
        l  = day_losses.get(d, 0)
        event_lbl = _get_day_event_labels(d, ev)
        logger.info(f"  {date_fmt:<12} {vix_str:>5}  {day_trades[d]:>6}  {w}W/{l}L{'':<2} {pnl:>12,.2f}  {event_lbl}")
    logger.info(sep)


# ---------------------------------------------
#  ENTRY TIME ANALYSIS
# ---------------------------------------------
def print_entry_time_analysis(all_trades: list) -> None:
    """Break down P&L, drawdown, and W/L by entry time slot."""
    from collections import defaultdict
    buckets: dict[str, dict] = {}
    for t in all_trades:
        et = t.get("entry_time", "")[:5]  # "HH:MM"
        if et not in buckets:
            buckets[et] = {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []}
        buckets[et]["pnl"] += t["pnl_earned"]
        buckets[et]["pnls"].append(t["pnl_earned"])
        if t.get("win"):
            buckets[et]["wins"] += 1
        else:
            buckets[et]["losses"] += 1

    # Compute per-slot max drawdown (equity curve within each slot across all days)
    for slot, b in buckets.items():
        eq = pk = dd = 0.0
        for p in b["pnls"]:
            eq += p
            pk  = max(pk, eq)
            dd  = min(dd, eq - pk)
        b["max_dd"] = dd

    sorted_slots = sorted(buckets.keys())
    sep = "-" * 62
    logger.info(sep)
    logger.info("  ENTRY TIME BREAKDOWN")
    logger.info(sep)
    logger.info(f"  {'Time':<7} {'Trades':>7} {'W/L':<9} {'WR%':>5} {'Total P&L':>12} {'Max DD':>10}")
    logger.info(sep)
    for slot in sorted_slots:
        b = buckets[slot]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0.0
        wl = f"{b['wins']}W/{b['losses']}L"
        logger.info(f"  {slot:<7} {total:>7} {wl:<9} {wr:>4.1f}% {b['pnl']:>12,.2f} {b['max_dd']:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  SEASONALITY ANALYSIS
# ---------------------------------------------
def print_seasonality_analysis(all_trades: list) -> None:
    """Break down P&L by day-of-week and by month."""
    from collections import defaultdict
    import datetime as _dt_mod

    dow_buckets: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(5)}
    mon_buckets: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(1, 13)}

    for t in all_trades:
        d = t.get("entry_date", "")
        if len(d) != 8:
            continue
        try:
            dt = _dt_mod.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        except ValueError:
            continue
        dow = dt.weekday()  # 0=Mon … 4=Fri
        mon = dt.month
        pnl = t["pnl_earned"]
        win = bool(t.get("win"))
        dow_buckets[dow]["pnl"]    += pnl
        mon_buckets[mon]["pnl"]    += pnl
        dow_buckets[dow]["wins" if win else "losses"] += 1
        mon_buckets[mon]["wins" if win else "losses"] += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    mon_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    sep = "-" * 56

    logger.info(sep)
    logger.info("  SEASONALITY -- DAY OF WEEK")
    logger.info(sep)
    logger.info(f"  {'Day':<5} {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    logger.info(sep)
    for i, name in enumerate(dow_names):
        b = dow_buckets[i]
        total = b["wins"] + b["losses"]
        wr    = b["wins"] / total * 100 if total else 0.0
        avg   = b["pnl"] / total if total else 0.0
        logger.info(f"  {name:<5} {total:>7} {wr:>5.1f}% {b['pnl']:>12,.2f} {avg:>10,.2f}")
    logger.info(sep)

    logger.info(sep)
    logger.info("  SEASONALITY -- MONTH")
    logger.info(sep)
    logger.info(f"  {'Month':<5} {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    logger.info(sep)
    for i, name in enumerate(mon_names, 1):
        b = mon_buckets[i]
        total = b["wins"] + b["losses"]
        wr    = b["wins"] / total * 100 if total else 0.0
        avg   = b["pnl"] / total if total else 0.0
        logger.info(f"  {name:<5} {total:>7} {wr:>5.1f}% {b['pnl']:>12,.2f} {avg:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  ECON DATE ANALYSIS
# ---------------------------------------------
def print_econ_date_analysis(all_trades: list) -> None:
    """Break down P&L and drawdown for each calendar/econ event type vs normal days."""
    day_pnl: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d] = day_pnl.get(d, 0.0) + t["pnl_earned"]

    def _stats(date_set):
        pnls = [day_pnl[d] for d in day_pnl if d in date_set]
        if not pnls: return 0, 0.0, 0.0, 0.0, 0.0
        wins  = sum(1 for p in pnls if p > 0)
        wr    = wins / len(pnls) * 100
        avg   = sum(pnls) / len(pnls)
        total = sum(pnls)
        eq = pk = dd = 0.0
        for p in pnls: eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        return len(pnls), wr, avg, total, dd

    ev = _build_calendar_event_dates()
    all_event_days = set().union(*[v for v in ev.values() if isinstance(v, set)])
    normal_set = {d for d in day_pnl if d not in all_event_days}

    event_types = [
        ("Normal",       normal_set),
        ("CPI",          ev.get("cpi", set())),
        ("PPI",          ev.get("ppi", set())),
        ("PCE",          ev.get("pce", set())),
        ("NFP",          ev.get("nfp", set())),
        ("FOMC",         ev.get("fomc", set())),
        ("Triple Witch", ev.get("triple_witching", set())),
        ("Monthly OPEX", ev.get("monthly_opex", set())),
        ("EOM",          ev.get("end_of_month", set())),
        ("EOQ",          ev.get("end_of_quarter", set())),
        ("Pre-TW",       ev.get("pre_triple_witching", set())),
        ("Post-Holiday", ev.get("post_major_holiday", set())),
        ("Full Moon",    ev.get("full_moon", set())),
    ]

    # Compute stats, skip empty, sort event rows by WR% desc; pin Normal to bottom
    rows_data = []
    normal_row = None
    for label, date_set in event_types:
        n, wr, avg, total, dd = _stats(date_set)
        if n == 0:
            continue
        entry = (label, n, wr, avg, total, dd)
        if label == "Normal":
            normal_row = entry
        else:
            rows_data.append(entry)
    rows_data.sort(key=lambda r: r[2], reverse=True)
    if normal_row:
        rows_data.append(normal_row)

    sep = "-" * 72
    logger.info(sep)
    logger.info("  CALENDAR / ECON EVENT ANALYSIS  (sorted by WR%)")
    logger.info(sep)
    logger.info(f"  {'Event':<14} {'Days':>5} {'WR%':>6} {'Avg/Day':>10} {'Total P&L':>12} {'Max DD':>10}")
    logger.info(sep)
    for label, n, wr, avg, total, dd in rows_data:
        logger.info(f"  {label:<14} {n:>5} {wr:>5.1f}% {avg:>10,.2f} {total:>12,.2f} {dd:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  RUN HISTORY  (persist last N run summaries)
# ---------------------------------------------
def _save_run_summary(all_trades: list, date_list) -> None:
    """Append a summary of this run to RUN_HISTORY_FILE (keeps last 10 entries)."""
    import json, math, collections

    if not all_trades:
        return

    pnls = [t["pnl_earned"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate  = len(wins) / len(pnls) * 100 if pnls else 0.0

    # Max drawdown
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)

    # Daily P&L
    day_pnls: dict = {}
    for t in all_trades:
        day_pnls.setdefault(t["entry_date"], 0.0)
        day_pnls[t["entry_date"]] += t["pnl_earned"]

    # Average monthly P&L
    monthly: dict = collections.defaultdict(float)
    for d_str, pnl in day_pnls.items():
        ym = d_str[:6]   # YYYYMM
        monthly[ym] += pnl
    avg_monthly = sum(monthly.values()) / len(monthly) if monthly else 0.0

    # Sharpe
    rf_daily  = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    dv        = list(day_pnls.values())
    mean_d    = sum(dv) / len(dv) if dv else 0.0
    std_d     = math.sqrt(sum((p - mean_d) ** 2 for p in dv) / len(dv)) if dv else 0.0
    sharpe    = (mean_d - rf_daily) / std_d * math.sqrt(252) if std_d else 0.0
    calmar    = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")

    days_in_range = len(date_list)
    days_traded   = len(day_pnls)

    entry = {
        "timestamp":     _RUN_TS,
        "period":        f"{PILOT_YEAR_START} -> {PILOT_YEAR_END}",
        "days_in_range": days_in_range,
        "days_traded":   days_traded,
        # key params
        "width":         WIDTH,
        "qty":           QTY,
        "min_credit":    MIN_NET_CREDIT,
        "max_credit":    str(MAX_NET_CREDIT),
        "min_otm":       str(MIN_OTM_DISTANCE),
        "direction":     DIRECTION_MODE,
        "entry_window":  f"{ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min",
        "dyn_sl":        f"VIX<{DYNAMIC_SL_VIX_LOW} | {DYNAMIC_SL_VIX_MID} | {DYNAMIC_SL_VIX_HIGH}" if ENABLE_DYNAMIC_SL else "off",
        "skip_vix_range": str(SKIP_VIX_RANGE) if SKIP_VIX_RANGE is not None else "off",
        "entry_gate":     f"${INTRADAY_ENTRY_GATE}" if INTRADAY_ENTRY_GATE is not None else "off",
        "buyback_exit":   f"${PREMIUM_BUYBACK_EXIT}" if PREMIUM_BUYBACK_EXIT is not None else "off",
        "kelly_sizing":   f"on -- {KELLY_ZONE_QTY}" if ENABLE_KELLY_SIZING else "off",
        "cal_filter":    f"{sorted(CALENDAR_FILTER_EVENTS)}" if ENABLE_CALENDAR_FILTER else "off",
        # results
        "total_pnl":     round(total_pnl, 2),
        "win_rate_pct":  round(win_rate, 1),
        "total_trades":  len(pnls),
        "avg_monthly_pnl": round(avg_monthly, 2),
        "max_drawdown":  round(max_dd, 2),
        "sharpe":        round(sharpe, 2),
        "calmar":        round(calmar, 2) if calmar != float("inf") else "inf",
        "log_file":      LOG_FILE,
    }

    history = []
    if os.path.exists(RUN_HISTORY_FILE):
        try:
            with open(RUN_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(entry)
    history = history[-10:]   # keep last 10
    with open(RUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    logger.info(f"Run summary saved -> {RUN_HISTORY_FILE}")


def _display_run_history() -> None:
    """Print the last 3 run summaries at the top of each run."""
    import json
    if not os.path.exists(RUN_HISTORY_FILE):
        return
    try:
        with open(RUN_HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        return
    recent = history[-3:]
    if not recent:
        return

    w = 72
    sep  = "═" * w
    sep2 = "-" * w
    logger.info(sep)
    logger.info("  LAST RUN HISTORY  (most recent first)")
    logger.info(sep)
    for entry in reversed(recent):
        ts    = entry.get("timestamp", "?")
        dt    = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}  {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
        pnl   = entry.get("total_pnl", 0)
        dd    = entry.get("max_drawdown", 0)
        wr    = entry.get("win_rate_pct", 0)
        sh    = entry.get("sharpe", 0)
        cal   = entry.get("calmar", 0)
        trades= entry.get("total_trades", 0)
        days  = entry.get("days_traded", 0)
        mpnl  = entry.get("avg_monthly_pnl", 0)
        period= entry.get("period", "")
        dyn   = entry.get("dyn_sl", "?")
        win   = entry.get("entry_window", "?")
        otm   = entry.get("min_otm", "?")
        cal_f = entry.get("cal_filter", "off")
        logger.info(sep2)
        logger.info(f"  {dt}   |   {period}")
        logger.info(
            f"  P&L: ${pnl:>10,.0f}   Max DD: ${dd:>9,.0f}   "
            f"Avg Mo P&L: ${mpnl:>8,.0f}   WR: {wr:.1f}%"
        )
        logger.info(
            f"  Sharpe: {sh:>6.2f}   Calmar: {cal!s:>8}   "
            f"Trades: {trades:>5}   Days traded: {days}"
        )
        logger.info(
            f"  Params -> min_otm={otm}  dyn_sl={dyn}  "
            f"entry={win}  cal_filter={cal_f}"
        )
    logger.info(sep)
    logger.info("")


