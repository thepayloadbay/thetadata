"""MEIC reporting — performance analytics, VIX analysis, monthly P&L, etc."""
from __future__ import annotations

import csv
import math
import os
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from meic_config import *
from meic_config import _RUN_TS, _build_calendar_event_dates
import meic_config as _cfg
from meic_engine import logger, compute_metrics


# ---------------------------------------------
#  VIX ANALYSIS
# ---------------------------------------------
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-30", 25.0, 30.0),
    ("30-35", 30.0, 35.0),
    ("35+",   35.0, None),
]


def print_vix_analysis(all_trades: list) -> None:
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
        n = len(bucket)
        wins = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate = wins / n * 100 if n else 0.0
        avg_pnl = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)
        logger.info(f"  {label:<10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}")
        rows.append({"vix_range": label, "trade_count": n, "win_rate_pct": f"{win_rate:.1f}",
                      "avg_pnl": f"{avg_pnl:.2f}", "total_pnl": f"{total_pnl:.2f}"})
    logger.info(sep)

    with open(VIX_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["vix_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------
#  STRIKE DISTANCE ANALYSIS
# ---------------------------------------------
STRIKE_DISTANCE_BUCKETS = [
    ("30-40",  30, 40), ("40-50",  40, 50), ("50-60",  50, 60),
    ("60-70",  60, 70), ("70-80",  70, 80), ("80-90",  80, 90),
    ("90-100", 90, 100), ("100-120", 100, 120), ("120-150", 120, 150),
    ("150+",   150, None),
]


def print_strike_distance_analysis(all_trades: list) -> None:
    rows = []
    sep = "-" * 70
    logger.info(sep)
    logger.info("  STRIKE DISTANCE VS WIN RATE ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Distance':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for label, lo, hi in STRIKE_DISTANCE_BUCKETS:
        bucket = [t for t in all_trades
                  if t.get("strike_distance") and float(t["strike_distance"]) >= lo
                  and (hi is None or float(t["strike_distance"]) < hi)]
        n = len(bucket)
        wins = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate = wins / n * 100 if n else 0.0
        avg_pnl = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)
        logger.info(f"  {label:>10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}")
        rows.append({"distance": label, "trades": n, "wr": f"{win_rate:.1f}", "avg": f"{avg_pnl:.2f}", "total": f"{total_pnl:.2f}"})
    logger.info(sep)

    with open(STRIKE_DISTANCE_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["distance", "trades", "wr", "avg", "total"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------
#  PUT vs CALL SPLIT
# ---------------------------------------------
def print_put_call_analysis(all_trades: list) -> None:
    sep = "-" * 95
    logger.info(sep)
    logger.info("  PUT vs CALL SPREAD PERFORMANCE SPLIT")
    logger.info(sep)
    logger.info(f"  {'Type':<8} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12} | {'Prof Fac':>9} | {'Max DD':>11}")
    logger.info(sep)

    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        m = compute_metrics(bucket)
        avg_pnl = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0.0
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf"
        logger.info(
            f"  {label:<8} | {m['num_trades']:>7} | {m['win_rate']:>8.1f}% | "
            f"${avg_pnl:>9,.2f} | ${m['total_pnl']:>11,.2f} | {pf:>9} | ${m['max_drawdown']:>10,.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  PERFORMANCE REPORT
# ---------------------------------------------
def print_performance_report(all_trades: list, date_list) -> None:
    pnls = [t["pnl_earned"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    n = len(pnls)
    win_rate = len(wins) / n * 100 if n else 0

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    day_pnls: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
    best_day = max(day_pnls.values()) if day_pnls else 0
    worst_day = min(day_pnls.values()) if day_pnls else 0
    days_traded = len(day_pnls)
    avg_trades_per_day = n / days_traded if days_traded else 0

    rf_daily = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals = list(day_pnls.values())
    n_days = len(daily_vals)
    mean_daily = sum(daily_vals) / n_days if n_days else 0
    var_daily = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily = math.sqrt(var_daily)
    sharpe = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0

    downside_d = [p for p in daily_vals if p < mean_daily]
    down_var_d = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        first_d = datetime.strptime(sorted_dates[0], "%Y%m%d")
        last_d = datetime.strptime(sorted_dates[-1], "%Y%m%d")
        years = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    cagr = ((total_pnl / ACCOUNT_SIZE + 1) ** (1 / years) - 1) * 100 if years > 0 and ACCOUNT_SIZE > 0 else 0
    calmar = abs(cagr / (max_dd_pct)) if max_dd_pct != 0 else 0

    # Buying power analysis
    bp_per_trade = []
    for t in all_trades:
        w = float(t.get("width") or t.get("spread_width") or WIDTH)
        c = float(t.get("credit_received") or 0)
        q = int(t.get("qty") or QTY)
        bp_per_trade.append((w - c) * 100 * q)
    avg_bp = sum(bp_per_trade) / len(bp_per_trade) if bp_per_trade else 0

    sep = "=" * 60
    logger.info(sep)
    logger.info("  MEIC PERFORMANCE REPORT")
    logger.info(sep)
    logger.info(f"  Period         : {sorted_dates[0] if sorted_dates else 'N/A'} -> {sorted_dates[-1] if sorted_dates else 'N/A'}")
    logger.info(f"  Total Trades   : {n:,}")
    logger.info(f"  Days Traded    : {days_traded}")
    logger.info(f"  Avg Trades/Day : {avg_trades_per_day:.1f}")
    logger.info(sep)
    logger.info(f"  Total P&L      : ${total_pnl:>12,.2f}")
    logger.info(f"  Win Rate       : {win_rate:.1f}%")
    logger.info(f"  Profit Factor  : {profit_factor:.2f}")
    logger.info(f"  Avg Win        : ${avg_win:>10,.2f}")
    logger.info(f"  Avg Loss       : ${avg_loss:>10,.2f}")
    logger.info(sep)
    logger.info(f"  Max Drawdown   : ${max_dd:>10,.2f} ({max_dd_pct:.1f}%)")
    logger.info(f"  Best Day       : ${best_day:>10,.2f}")
    logger.info(f"  Worst Day      : ${worst_day:>10,.2f}")
    logger.info(sep)
    logger.info(f"  Sharpe Ratio   : {sharpe:.2f}")
    logger.info(f"  Sortino Ratio  : {sortino:.2f}")
    logger.info(f"  CAGR           : {cagr:.1f}%")
    logger.info(f"  Calmar Ratio   : {calmar:.2f}")
    logger.info(sep)
    logger.info(f"  Avg BP/Trade   : ${avg_bp:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  MONTHLY P&L TABLE
# ---------------------------------------------
def print_monthly_pnl_table(all_trades: list) -> None:
    monthly: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        key = f"{d[:4]}-{d[4:6]}"
        monthly[key] = monthly.get(key, 0.0) + t["pnl_earned"]

    years = sorted(set(k[:4] for k in monthly))
    months = [f"{m:02d}" for m in range(1, 13)]

    sep = "-" * 110
    logger.info(sep)
    logger.info("  MONTHLY P&L TABLE")
    logger.info(sep)
    header = f"  {'Year':>6}"
    for m in months:
        header += f" | {m:>7}"
    header += f" | {'TOTAL':>10}"
    logger.info(header)
    logger.info(sep)

    for year in years:
        row = f"  {year:>6}"
        year_total = 0.0
        for m in months:
            key = f"{year}-{m}"
            val = monthly.get(key, 0.0)
            year_total += val
            row += f" | ${val:>6,.0f}"
        row += f" | ${year_total:>9,.0f}"
        logger.info(row)
    logger.info(sep)
    grand = sum(monthly.values())
    logger.info(f"  {'TOTAL':>6}" + " " * (8 * 12 + 3 * 12) + f"  ${grand:>9,.0f}")
    logger.info(sep)


# ---------------------------------------------
#  ENTRY TIME ANALYSIS
# ---------------------------------------------
def print_entry_time_analysis(all_trades: list) -> None:
    time_buckets: dict[str, list] = {}
    for t in all_trades:
        et = t.get("entry_time", "")[:5]  # HH:MM
        time_buckets.setdefault(et, []).append(t)

    sep = "-" * 70
    logger.info(sep)
    logger.info("  ENTRY TIME ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Time':>6} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for et in sorted(time_buckets):
        bucket = time_buckets[et]
        n = len(bucket)
        wins = sum(1 for t in bucket if t["pnl_earned"] > 0)
        pnls = [t["pnl_earned"] for t in bucket]
        wr = wins / n * 100 if n else 0
        avg = sum(pnls) / n if n else 0
        total = sum(pnls)
        logger.info(f"  {et:>6} | {n:>7} | {wr:>8.1f}% | ${avg:>9,.2f} | ${total:>11,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  LARGE LOSS DAYS
# ---------------------------------------------
def print_large_loss_days(all_trades: list, n_worst: int = 15) -> None:
    day_pnls: dict[str, float] = {}
    day_counts: dict[str, dict] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
        if d not in day_counts:
            day_counts[d] = {"wins": 0, "losses": 0, "vix": t.get("vix_level", "")}
        if t["pnl_earned"] > 0:
            day_counts[d]["wins"] += 1
        else:
            day_counts[d]["losses"] += 1

    worst = sorted(day_pnls.items(), key=lambda x: x[1])[:n_worst]

    sep = "-" * 80
    logger.info(sep)
    logger.info(f"  WORST {n_worst} TRADING DAYS")
    logger.info(sep)
    logger.info(f"  {'Date':>10} | {'Day P&L':>10} | {'VIX':>6} | {'Wins':>5} | {'Losses':>6}")
    logger.info(sep)

    for d, pnl in worst:
        info = day_counts.get(d, {})
        vix = info.get("vix", "")
        logger.info(f"  {d:>10} | ${pnl:>9,.2f} | {vix:>6} | {info.get('wins', 0):>5} | {info.get('losses', 0):>6}")
    logger.info(sep)


# ---------------------------------------------
#  ECON DATE ANALYSIS
# ---------------------------------------------
def print_econ_date_analysis(all_trades: list) -> None:
    cal = _build_calendar_event_dates()
    events = ["fomc", "cpi", "pce", "nfp", "end_of_month", "end_of_quarter", "triple_witching"]

    sep = "-" * 80
    logger.info(sep)
    logger.info("  ECONOMIC EVENT DAY ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Event':<20} | {'Days':>5} | {'Trades':>7} | {'Win Rate':>9} | {'Total P&L':>12} | {'Avg Day P&L':>12}")
    logger.info(sep)

    for event in events:
        dates = cal.get(event, set())
        event_trades = [t for t in all_trades if t["entry_date"] in dates]
        day_pnls: dict[str, float] = {}
        for t in event_trades:
            day_pnls[t["entry_date"]] = day_pnls.get(t["entry_date"], 0.0) + t["pnl_earned"]
        n = len(event_trades)
        n_days = len(day_pnls)
        wins = sum(1 for t in event_trades if t["pnl_earned"] > 0)
        wr = wins / n * 100 if n else 0
        total = sum(t["pnl_earned"] for t in event_trades)
        avg_day = sum(day_pnls.values()) / n_days if n_days else 0
        logger.info(f"  {event:<20} | {n_days:>5} | {n:>7} | {wr:>8.1f}% | ${total:>11,.2f} | ${avg_day:>11,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  APPEND RESULTS TO RESULTS.md (MEDS-style, keep last 3 runs)
# ---------------------------------------------
def append_results_md(all_trades: list, date_list) -> None:
    import re as _re

    m = compute_metrics(all_trades)
    day_pnls: dict[str, float] = {}
    day_trade_count: dict[str, int] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
        day_trade_count[d] = day_trade_count.get(d, 0) + 1
    days_traded = len(day_pnls)
    pnls = [t["pnl_earned"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    sorted_dates = sorted(day_pnls.keys())
    first_d_str = sorted_dates[0] if sorted_dates else "N/A"
    last_d_str = sorted_dates[-1] if sorted_dates else "N/A"
    if len(sorted_dates) >= 2:
        first_d = datetime.strptime(sorted_dates[0], "%Y%m%d")
        last_d = datetime.strptime(sorted_dates[-1], "%Y%m%d")
        years = (last_d - first_d).days / 365.25
    else:
        years = 1.0

    total_pnl = m["total_pnl"]
    cagr = ((total_pnl / _cfg.ACCOUNT_SIZE + 1) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Equity curve for drawdown
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0
    calmar = abs(cagr / max_dd_pct) if max_dd_pct != 0 else 0
    recovery = abs(total_pnl / max_dd) if max_dd != 0 else 0

    # Time underwater
    eq_d = peak_d = 0.0
    uw_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]
        peak_d = max(peak_d, eq_d)
        if eq_d < peak_d:
            uw_days += 1
    uw_pct = uw_days / days_traded * 100 if days_traded else 0

    daily_vals = list(day_pnls.values())
    n_days = len(daily_vals)
    rf_daily = _cfg.ANNUAL_RISK_FREE_RATE * _cfg.ACCOUNT_SIZE / 252
    mean_d = sum(daily_vals) / n_days if n_days else 0
    var_d = sum((p - mean_d) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_d = math.sqrt(var_d)
    ann_vol = std_d * math.sqrt(252)

    best_day = max(daily_vals) if daily_vals else 0
    worst_day = min(daily_vals) if daily_vals else 0
    avg_tpd = len(all_trades) / days_traded if days_traded else 0

    gross_premium = sum(float(t.get("credit_received", 0)) * int(t.get("qty", _cfg.QTY)) * 100 for t in all_trades)
    premium_capture = (total_pnl / gross_premium * 100) if gross_premium else 0
    expectancy = total_pnl / len(all_trades) if all_trades else 0
    commission_total = sum(2 * 2 * int(t.get("qty", _cfg.QTY)) * _cfg.COMMISSION for t in all_trades)

    # BP calculations
    bp_list = []
    for t in all_trades:
        w = float(t.get("width") or t.get("spread_width") or _cfg.WIDTH)
        c = float(t.get("credit_received") or 0)
        q = int(t.get("qty") or _cfg.QTY)
        bp_list.append((w - c) * 100 * q)
    avg_bp = sum(bp_list) / len(bp_list) if bp_list else 0
    # Avg BP/day and max BP day
    day_bp: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        w = float(t.get("width") or t.get("spread_width") or _cfg.WIDTH)
        c = float(t.get("credit_received") or 0)
        q = int(t.get("qty") or _cfg.QTY)
        day_bp[d] = day_bp.get(d, 0.0) + (w - c) * 100 * q
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0
    max_bp_day = max(day_bp.values()) if day_bp else 0

    # --- VIX Correlation ---
    vix_buckets = [("< 15",None,15),("15-20",15,20),("20-25",20,25),("25-30",25,30),("30-35",30,35),("35+",35,None)]
    vix_rows = []
    for label, lo, hi in vix_buckets:
        bucket = []
        for t in all_trades:
            try:
                v = float(t.get("vix_level") or "")
            except (ValueError, TypeError):
                continue
            if (lo is None or v >= lo) and (hi is None or v < hi):
                bucket.append(t)
        n = len(bucket)
        w = sum(1 for t in bucket if t["pnl_earned"] > 0)
        wr = w / n * 100 if n else 0
        ap = sum(t["pnl_earned"] for t in bucket) / n if n else 0
        tp = sum(t["pnl_earned"] for t in bucket)
        vix_rows.append(f"| {label} | {n} | {wr:.1f}% | ${ap:,.2f} | ${tp:,.2f} |")

    # --- PUT vs CALL Split ---
    split_rows = []
    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        if not bucket:
            split_rows.append(f"| {label} | 0 | 0.0% | $0.00 | $0.00 | $0.00 | $0.00 | -- | $0.00 |")
            continue
        sm = compute_metrics(bucket)
        ap = sm["total_pnl"] / sm["num_trades"] if sm["num_trades"] else 0
        pf = f"{sm['profit_factor']:.2f}" if sm["profit_factor"] != float("inf") else "inf"
        split_rows.append(f"| {label} | {sm['num_trades']} | {sm['win_rate']:.1f}% | ${ap:,.2f} | ${sm['total_pnl']:,.2f} | ${sm['avg_win']:,.2f} | ${sm['avg_loss']:,.2f} | {pf} | ${sm['max_drawdown']:,.2f} |")

    # --- Monthly P&L ---
    monthly: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        key = f"{d[:4]}-{d[4:6]}"
        monthly[key] = monthly.get(key, 0.0) + t["pnl_earned"]
    all_years = sorted(set(k[:4] for k in monthly))
    months_list = [f"{m:02d}" for m in range(1, 13)]
    monthly_rows = []
    for year in all_years:
        cols = []
        yt = 0.0
        for mo in months_list:
            key = f"{year}-{mo}"
            val = monthly.get(key, 0.0)
            yt += val
            cols.append(f"${val:,.0f}" if val != 0 else "--")
        monthly_rows.append(f"| {year} | {' | '.join(cols)} | ${yt:,.0f} |")
    # Totals row
    tot_cols = []
    for mo in months_list:
        s = sum(monthly.get(f"{y}-{mo}", 0.0) for y in all_years)
        tot_cols.append(f"${s:,.0f}")
    monthly_rows.append(f"| **Total** | {' | '.join(tot_cols)} | **${total_pnl:,.0f}** |")

    # --- Intra-Month Max Drawdown ---
    month_dd_rows = []
    for year in all_years:
        cols = []
        # Annual DD
        year_trades = [t for t in all_trades if t["entry_date"][:4] == year]
        ye = yp = ydd = 0.0
        for t in year_trades:
            ye += t["pnl_earned"]
            yp = max(yp, ye)
            ydd = min(ydd, ye - yp)
        for mo in months_list:
            mo_trades = [t for t in all_trades if t["entry_date"][:4] == year and t["entry_date"][4:6] == mo]
            if not mo_trades:
                cols.append("--")
                continue
            me = mp = mdd = 0.0
            for t in mo_trades:
                me += t["pnl_earned"]
                mp = max(mp, me)
                mdd = min(mdd, me - mp)
            cols.append(f"${mdd:,.0f}")
        month_dd_rows.append(f"| {year} | {' | '.join(cols)} | ${ydd:,.0f} |")

    # --- Monthly Return: Strategy vs SPY ---
    spy_monthly = {
        "2022-01": -5.3, "2022-02": -3.0, "2022-03": 3.8, "2022-04": -8.8,
        "2022-05": 0.2, "2022-06": -8.2, "2022-07": 9.2, "2022-08": -4.1,
        "2022-09": -9.2, "2022-10": 8.1, "2022-11": 5.6, "2022-12": -5.8,
        "2023-01": 6.3, "2023-02": -2.5, "2023-03": 3.7, "2023-04": 1.6,
        "2023-05": 0.5, "2023-06": 6.5, "2023-07": 3.3, "2023-08": -1.6,
        "2023-09": -4.7, "2023-10": -2.2, "2023-11": 9.1, "2023-12": 4.6,
        "2024-01": 1.6, "2024-02": 5.2, "2024-03": 3.3, "2024-04": -4.0,
        "2024-05": 5.1, "2024-06": 3.5, "2024-07": 1.2, "2024-08": 2.3,
        "2024-09": 2.1, "2024-10": -0.9, "2024-11": 6.0, "2024-12": -2.4,
        "2025-01": 2.7, "2025-02": -1.3, "2025-03": -5.6, "2025-04": -0.9,
        "2025-05": 6.3, "2025-06": 5.1, "2025-07": 2.3, "2025-08": 2.1,
        "2025-09": 3.6, "2025-10": 2.4, "2025-11": 0.2, "2025-12": 0.1,
        "2026-01": 1.5, "2026-02": -0.9, "2026-03": -4.9, "2026-04": 0.7,
    }
    spy_vs_rows = []
    for year in all_years:
        cols = []
        ann_strat = 0.0
        ann_spy = 0.0
        for mo in months_list:
            key = f"{year}-{mo}"
            strat_pnl = monthly.get(key, 0.0)
            strat_pct = strat_pnl / _cfg.ACCOUNT_SIZE * 100
            spy_pct = spy_monthly.get(key)
            ann_strat += strat_pct
            if spy_pct is not None:
                ann_spy += spy_pct
                cols.append(f"{strat_pct:+.1f}% / {spy_pct:+.1f}%")
            elif strat_pnl != 0:
                cols.append(f"{strat_pct:+.1f}% / --")
            else:
                cols.append("--")
        alpha = ann_strat - ann_spy
        spy_vs_rows.append(f"| {year} | {' | '.join(cols)} | {ann_strat:+.1f}% | {ann_spy:+.1f}% | {alpha:+.1f}% |")

    # --- Largest Loss Days (worst 30) ---
    day_info: dict[str, dict] = {}
    for t in all_trades:
        d = t["entry_date"]
        if d not in day_info:
            day_info[d] = {"wins": 0, "losses": 0, "vix": t.get("vix_level", "")}
        if t["pnl_earned"] > 0:
            day_info[d]["wins"] += 1
        else:
            day_info[d]["losses"] += 1
    cal = _build_calendar_event_dates()
    _event_map = {
        "fomc": cal.get("fomc", set()), "cpi": cal.get("cpi", set()),
        "pce": cal.get("pce", set()), "nfp": cal.get("nfp", set()),
        "tw": cal.get("triple_witching", set()), "eom": cal.get("end_of_month", set()),
        "eoq": cal.get("end_of_quarter", set()), "pre_tw": cal.get("pre_triple_witching", set()),
        "post_hol": cal.get("post_major_holiday", set()),
    }
    def _event_label(d):
        labels = []
        for name, dates in _event_map.items():
            if d in dates:
                labels.append(name.upper().replace("_", "-"))
        return ", ".join(labels) if labels else ""

    worst_30 = sorted(day_pnls.items(), key=lambda x: x[1])[:30]
    loss_rows = []
    for d, pnl in worst_30:
        info = day_info.get(d, {})
        vix = info.get("vix", "")
        w, l = info.get("wins", 0), info.get("losses", 0)
        evt = _event_label(d)
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        loss_rows.append(f"| {d_fmt} | {vix} | {w+l} | {w}W/{l}L | ${pnl:,.2f} | {evt} |")

    # --- Entry Time Breakdown ---
    time_buckets: dict[str, list] = {}
    for t in all_trades:
        et = t.get("entry_time", "")[:5]
        time_buckets.setdefault(et, []).append(t)
    entry_time_rows = []
    for et in sorted(time_buckets):
        bucket = time_buckets[et]
        n = len(bucket)
        w = sum(1 for t in bucket if t["pnl_earned"] > 0)
        l = n - w
        wr = w / n * 100 if n else 0
        tp = sum(t["pnl_earned"] for t in bucket)
        # Per-slot DD
        se = sp = sdd = 0.0
        for t in sorted(bucket, key=lambda x: x["entry_date"]):
            se += t["pnl_earned"]
            sp = max(sp, se)
            sdd = min(sdd, se - sp)
        entry_time_rows.append(f"| {et} | {n} | {w}W/{l}L | {wr:.1f}% | ${tp:,.2f} | ${sdd:,.2f} |")

    # --- Seasonality DOW ---
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    dow_buckets: dict[int, list] = {i: [] for i in range(5)}
    for t in all_trades:
        try:
            d = datetime.strptime(t["entry_date"], "%Y%m%d")
            dow_buckets[d.weekday()].append(t)
        except:
            pass
    dow_rows = []
    for i in range(5):
        bucket = dow_buckets[i]
        n = len(bucket)
        w = sum(1 for t in bucket if t["pnl_earned"] > 0)
        wr = w / n * 100 if n else 0
        tp = sum(t["pnl_earned"] for t in bucket)
        ap = tp / n if n else 0
        dow_rows.append(f"| {dow_names[i]} | {n} | {wr:.1f}% | ${tp:,.2f} | ${ap:,.2f} |")

    # --- Seasonality Month ---
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    month_buckets: dict[int, list] = {i: [] for i in range(1, 13)}
    for t in all_trades:
        mo = int(t["entry_date"][4:6])
        month_buckets[mo].append(t)
    month_rows = []
    for i in range(1, 13):
        bucket = month_buckets[i]
        n = len(bucket)
        w = sum(1 for t in bucket if t["pnl_earned"] > 0)
        wr = w / n * 100 if n else 0
        tp = sum(t["pnl_earned"] for t in bucket)
        ap = tp / n if n else 0
        month_rows.append(f"| {month_names[i-1]} | {n} | {wr:.1f}% | ${tp:,.2f} | ${ap:,.2f} |")

    # --- Calendar / Econ Event Analysis ---
    events = [
        ("Normal", None), ("CPI", "cpi"), ("PCE", "pce"), ("NFP", "nfp"),
        ("FOMC", "fomc"), ("Triple Witch", "triple_witching"),
        ("EOM", "end_of_month"), ("EOQ", "end_of_quarter"),
        ("Pre-TW", "pre_triple_witching"), ("Post-Holiday", "post_major_holiday"),
    ]
    all_event_dates = set()
    for _, key in events:
        if key:
            all_event_dates |= cal.get(key, set())
    econ_rows = []
    for label, key in events:
        if key is None:
            # Normal = not in any event
            evt_trades = [t for t in all_trades if t["entry_date"] not in all_event_dates]
        else:
            evt_dates = cal.get(key, set())
            evt_trades = [t for t in all_trades if t["entry_date"] in evt_dates]
        edp: dict[str, float] = {}
        for t in evt_trades:
            edp[t["entry_date"]] = edp.get(t["entry_date"], 0.0) + t["pnl_earned"]
        nd = len(edp)
        w = sum(1 for v in edp.values() if v > 0)
        wr = w / nd * 100 if nd else 0
        tp = sum(edp.values())
        ad = tp / nd if nd else 0
        # Max DD for event
        ee = ep = edd = 0.0
        for d in sorted(edp):
            ee += edp[d]
            ep = max(ep, ee)
            edd = min(edd, ee - ep)
        econ_rows.append(f"| {label} | {nd} | {wr:.1f}% | ${ad:,.2f} | ${tp:,.2f} | ${edd:,.2f} |")

    # --- Strike Distance ---
    dist_buckets = [("30-40",30,40),("40-50",40,50),("50-60",50,60),("60-70",60,70),
                    ("70-80",70,80),("80-90",80,90),("90-100",90,100),("100+",100,None)]
    dist_rows = []
    for label, lo, hi in dist_buckets:
        bucket = [t for t in all_trades if t.get("strike_distance")
                  and float(t["strike_distance"]) >= lo
                  and (hi is None or float(t["strike_distance"]) < hi)]
        n = len(bucket)
        w = sum(1 for t in bucket if t["pnl_earned"] > 0)
        wr = w / n * 100 if n else 0
        ap = sum(t["pnl_earned"] for t in bucket) / n if n else 0
        tp = sum(t["pnl_earned"] for t in bucket)
        dist_rows.append(f"| {label} | {n} | {wr:.1f}% | ${ap:,.2f} | ${tp:,.2f} |")

    # === Build the full run entry ===
    period_str = f"{first_d_str[:4]}-{first_d_str[4:6]}-{first_d_str[6:]}" if first_d_str != "N/A" else "N/A"
    period_end = f"{last_d_str[:4]}-{last_d_str[4:6]}-{last_d_str[6:]}" if last_d_str != "N/A" else "N/A"

    entry = f"""
---

## Run: {_RUN_TS}
**Period:** {period_str} -> {period_end}  |  **Days traded:** {days_traded}  |  **Total trades:** {len(all_trades)}

### Returns
| Metric | Value |
|--------|-------|
| Total P&L (net) | ${total_pnl:,.2f} |
| Gross premium collected | ${gross_premium:,.2f} |
| Premium capture rate | {premium_capture:.1f}% |
| CAGR | {cagr:.1f}% |
| Avg profit/trade | ${m['avg_win']:,.2f} |
| Avg loss/trade | ${m['avg_loss']:,.2f} |
| Expectancy/trade | ${expectancy:,.2f} |
| Profit factor | {m['profit_factor']:.2f}x |

### Risk
| Metric | Value |
|--------|-------|
| Max drawdown | ${max_dd:,.2f} ({max_dd_pct:.1f}%) |
| Calmar ratio | {calmar:.2f} |
| Recovery factor | {recovery:.2f} |
| Time underwater | {uw_pct:.1f}% ({uw_days} of {days_traded} days) |

### Key Config
| Parameter | Value |
|-----------|-------|
| Width | {_cfg.WIDTH:.0f}pt |
| QTY | {_cfg.QTY} |
| Min credit | ${_cfg.MIN_NET_CREDIT} |
| Max credit | {"none" if _cfg.MAX_NET_CREDIT is None else f"${_cfg.MAX_NET_CREDIT}"} |
| Min OTM dist | {_cfg.MIN_OTM_DISTANCE}pt |
| Entry window | {_cfg.ENTRY_START.strftime('%H:%M')}-{_cfg.ENTRY_END.strftime('%H:%M')} every {_cfg.ENTRY_INTERVAL}min |
| Direction | {_cfg.DIRECTION_MODE} |
| Kelly sizing | {"on" if _cfg.ENABLE_KELLY_SIZING else "off"} |
| Per-pos SL | ${_cfg.PER_POS_SL_AMOUNT} |
| Afternoon SL | ${_cfg.AFTERNOON_SL_AMOUNT} @{_cfg.AFTERNOON_SL_CUTOFF.strftime('%H:%M')} |
| Daily SL | ${_cfg.DAILY_SL} |
| Daily TP | {"off" if _cfg.DAILY_TP is None else f"${_cfg.DAILY_TP}"} |
| Open MTM gate | ${_cfg.OPEN_MTM_GATE_THRESHOLD} |
| VIX max filter | {_cfg.VIX_MAX_FILTER} |
| VIX skip range | {_cfg.SKIP_VIX_RANGE if _cfg.SKIP_VIX_RANGE else "off"} |
| FOMC skip | {"on" if _cfg.ENABLE_FOMC_SKIP else "off"} |
| EMA VIX gate | {"on" if _cfg.ENABLE_EMA_VIX_GATE else "off"} ({_cfg.EMA_VIX_GATE_LO}-{_cfg.EMA_VIX_GATE_HI}) |
| Adaptive OTM time | {"on" if _cfg.ENABLE_ADAPTIVE_OTM_TIME else "off"} |
| Dynamic width | {"on" if _cfg.ENABLE_DYNAMIC_WIDTH else "off"} ({_cfg.DYNAMIC_WIDTH_HIGH_VIX}pt @VIX>{_cfg.DYNAMIC_WIDTH_VIX_THRESHOLD}) |

### Ratios & Volatility
| Metric | Value |
|--------|-------|
| Win rate | {m['win_rate']:.1f}% |
| Sharpe ratio | {m['sharpe']:.2f} |
| Sortino ratio | {m['sortino']:.2f} |
| Ann. volatility | ${ann_vol:,.2f} |

### Day Stats
| Metric | Value |
|--------|-------|
| Best day | ${best_day:,.2f} |
| Worst day | ${worst_day:,.2f} |
| Avg trades/day | {avg_tpd:.1f} |

### Cost & Capital
| Metric | Value |
|--------|-------|
| Commission total | ${commission_total:,.2f} |
| Avg BP/trade | ${avg_bp:,.2f} |
| Avg BP/day | ${avg_bp_day:,.2f} |
| Max BP day | ${max_bp_day:,.2f} |

### VIX Correlation
| VIX Range | Trades | Win Rate | Avg P&L | Total P&L |
|-----------|-------:|--------:|--------:|----------:|
{chr(10).join(vix_rows)}

### Strike Distance vs Win Rate
| Distance | Trades | Win Rate | Avg P&L | Total P&L |
|----------|-------:|--------:|--------:|----------:|
{chr(10).join(dist_rows)}

### PUT vs CALL Split
| Type | Trades | Win Rate | Avg P&L | Total P&L | Avg Win | Avg Loss | Prof Factor | Max DD |
|------|-------:|--------:|--------:|----------:|--------:|---------:|------------:|-------:|
{chr(10).join(split_rows)}

### Monthly P&L ($)
| Year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec | Total |
|------|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|
{chr(10).join(monthly_rows)}

### Intra-Month Max Drawdown ($)
| Year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec | Annual DD |
|------|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|------:|----------:|
{chr(10).join(month_dd_rows)}

### Monthly Return: Strategy vs SPY (strat% / SPY%)
| Year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec | Annual Strat | Annual SPY | Alpha |
|------|:------:|:------:|:------:|:------:|:------:|:------:|:------:|:------:|:------:|:------:|:------:|:------:|------------:|-----------:|------:|
{chr(10).join(spy_vs_rows)}

### Largest Loss Days (worst 30)
| Date | VIX | Trades | W/L | Day P&L | Event |
|------|----:|-------:|----:|--------:|-------|
{chr(10).join(loss_rows)}

### Entry Time Breakdown
| Time | Trades | W/L | WR% | Total P&L | Max DD |
|------|-------:|----:|----:|----------:|-------:|
{chr(10).join(entry_time_rows)}

### Seasonality -- Day of Week
| Day | Trades | WR% | Total P&L | Avg P&L |
|-----|-------:|----:|----------:|--------:|
{chr(10).join(dow_rows)}

### Seasonality -- Month
| Month | Trades | WR% | Total P&L | Avg P&L |
|-------|-------:|----:|----------:|--------:|
{chr(10).join(month_rows)}

### Calendar / Econ Event Analysis
| Event | Days | WR% | Avg/Day | Total P&L | Max DD |
|-------|-----:|----:|--------:|----------:|-------:|
{chr(10).join(econ_rows)}
"""

    # --- Write to RESULTS.md, keeping only last 3 runs ---
    results_path = _cfg.RESULTS_FILE
    header = "# MEIC Strategy -- Backtest Results\n"

    # Read existing content
    existing_runs = []
    if os.path.exists(results_path):
        with open(results_path, "r") as f:
            content = f.read()
        # Split by "## Run:" markers
        parts = _re.split(r'(?=\n---\n\n## Run:)', content)
        # First part is the header, rest are runs
        if len(parts) > 1:
            existing_runs = parts[1:]  # each starts with \n---\n\n## Run:

    # Prepend new entry, keep only last 3 (newest first)
    all_runs = [entry] + existing_runs
    all_runs = all_runs[:3]

    with open(results_path, "w") as f:
        f.write(header)
        for run in all_runs:
            f.write(run)

    logger.info(f"Results written to {results_path} (keeping last 3 runs)")
