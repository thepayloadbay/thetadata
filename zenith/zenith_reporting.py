"""Zenith Fade CCS — Reporting & Analysis.

Comprehensive MEDS-style reporting: performance metrics, VIX correlation,
put/call split, monthly P&L, intra-month drawdown, SPY comparison,
worst loss days, seasonality, econ events, and RESULTS.md generation.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

import zenith_config as _cfg
from zenith_config import *
from zenith_engine import logger

SPY_CSV = os.path.join(os.path.dirname(__file__), "..", "meds", "spy_monthly_closes.csv")


# ---------------------------------------------
#  CORE METRICS
# ---------------------------------------------
def compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {k: 0 for k in [
            "num_trades", "total_pnl", "max_dd", "max_dd_pct",
            "sharpe", "sortino", "calmar", "win_rate",
            "profit_factor", "avg_win", "avg_loss",
            "best_trade", "worst_trade", "max_win_streak",
            "recovery_factor", "cagr", "gross_premium",
            "ann_vol", "best_day", "worst_day", "pct_underwater",
        ]}

    pnls = [t["pnl_earned"] for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Gross premium
    gross_premium = sum(float(t.get("credit_received", 0) or 0) * 100 for t in trades)

    # Equity curve, drawdown, underwater
    equity = _cfg.STARTING_EQUITY
    peak = equity
    max_dd = max_dd_pct = 0
    underwater_days = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
        if dd > 0:
            underwater_days += 1

    # Daily P&L for day stats
    daily_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        daily_pnl[t.get("trade_date", "")] += t["pnl_earned"]
    day_pnls = list(daily_pnl.values())
    best_day = max(day_pnls) if day_pnls else 0
    worst_day = min(day_pnls) if day_pnls else 0

    win_rate = len(wins) / n * 100 if n > 0 else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0)
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0

    # Sharpe & Sortino
    returns = [p / _cfg.RISK_AMOUNT for p in pnls]
    daily_rf = _cfg.ANNUAL_RF_RATE / 252
    if len(returns) > 2:
        avg_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1)
        sharpe = ((avg_ret - daily_rf) / std_ret) * math.sqrt(252) if std_ret > 0 else 0
        downside = [r for r in returns if r < 0]
        if downside:
            down_std = np.std(downside, ddof=1)
            sortino = min(99.0, ((avg_ret - daily_rf) / down_std) * math.sqrt(252)) if down_std > 1e-10 else 99.0
        else:
            sortino = 99.0
        ann_vol = std_ret * math.sqrt(252) * _cfg.RISK_AMOUNT
    else:
        sharpe = sortino = 0
        ann_vol = 0

    # CAGR & Calmar
    try:
        d0 = datetime.strptime(trades[0].get("signal_date", "20220103"), "%Y%m%d")
        d1 = datetime.strptime(trades[-1].get("trade_date", "20260325"), "%Y%m%d")
        years = (d1 - d0).days / 365.25
        final_eq = _cfg.STARTING_EQUITY + total_pnl
        cagr = ((final_eq / _cfg.STARTING_EQUITY) ** (1 / years) - 1) if years > 0 and final_eq > 0 else (-1.0 if final_eq <= 0 else 0)
    except Exception:
        cagr = years = 0
    calmar = (cagr * 100) / max_dd_pct if max_dd_pct > 0 else 0

    recovery_factor = total_pnl / max_dd if max_dd > 0 else 0
    streak = max_win_streak = 0
    for p in pnls:
        if p > 0:
            streak += 1
            max_win_streak = max(max_win_streak, streak)
        else:
            streak = 0

    return {
        "num_trades": n, "total_pnl": round(total_pnl, 2),
        "max_dd": round(max_dd, 2), "max_dd_pct": round(max_dd_pct, 2),
        "sharpe": round(sharpe, 2), "sortino": round(sortino, 2), "calmar": round(calmar, 2),
        "win_rate": round(win_rate, 1), "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "best_trade": round(max(pnls), 2), "worst_trade": round(min(pnls), 2),
        "max_win_streak": max_win_streak, "recovery_factor": round(recovery_factor, 2),
        "cagr": round(cagr * 100, 2), "gross_premium": round(gross_premium, 2),
        "ann_vol": round(ann_vol, 2), "best_day": round(best_day, 2),
        "worst_day": round(worst_day, 2),
        "pct_underwater": round(underwater_days / n * 100, 1) if n else 0,
        "num_wins": len(wins), "num_losses": len(losses),
    }


# =============================================
#  CONSOLE REPORTING (to logger)
# =============================================

def print_performance_report(trades: list[dict], date_range=None):
    m = compute_metrics(trades)
    sep = "=" * 60
    sep2 = "-" * 60

    if date_range is not None and len(date_range) > 0:
        trading_days = sum(1 for d in date_range if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS)
    else:
        trading_days = 0
    trade_days = len(set(t.get("trade_date", "") for t in trades))

    logger.info(sep)
    logger.info("  ZENITH FADE CCS — BACKTEST RESULTS")
    logger.info(sep)

    # Period & Day Counts
    start = trades[0]["signal_date"] if trades else "N/A"
    end = trades[-1]["trade_date"] if trades else "N/A"
    logger.info(f"  Period          : {start} -> {end}")
    logger.info(f"  Business days   : {trading_days}")
    logger.info(f"  Days traded     : {trade_days}")
    logger.info(f"  Total trades    : {m['num_trades']}  (wins: {m['num_wins']}  losses: {m['num_losses']})")
    if trade_days:
        logger.info(f"  Avg trades/day  :    {m['num_trades']/trade_days:.1f}")

    # Returns
    logger.info(sep2)
    logger.info(f"  Total P&L       : ${m['total_pnl']:>12,.2f}  (net of commission)")
    logger.info(f"  CAGR            :  {m['cagr']:.1f}%  (acct=${_cfg.STARTING_EQUITY:,.0f})")
    logger.info(f"  Avg profit/trade: ${m['avg_win']:>8,.2f}")
    logger.info(f"  Avg loss/trade  : ${m['avg_loss']:>8,.2f}")
    expectancy = m['total_pnl'] / m['num_trades'] if m['num_trades'] else 0
    logger.info(f"  Expectancy/trade: ${expectancy:>8,.2f}")
    logger.info(f"  Profit factor   :    {m['profit_factor']:.2f}x")

    # Risk
    logger.info(sep2)
    logger.info(f"  Max drawdown    : ${m['max_dd']:>8,.2f}  ({m['max_dd_pct']:.1f}%)")
    logger.info(f"  Calmar ratio    :     {m['calmar']:.2f}  (CAGR / max DD%)")
    logger.info(f"  Recovery factor :     {m['recovery_factor']:.2f}  (net P&L / max DD)")
    logger.info(f"  Time underwater :   {m['pct_underwater']:.1f}%")

    # Ratios & Volatility
    logger.info(sep2)
    logger.info(f"  Win rate        :   {m['win_rate']:.1f}%")
    logger.info(f"  Sharpe ratio    :     {m['sharpe']:.2f}  (rf={_cfg.ANNUAL_RF_RATE*100:.1f}%)")
    logger.info(f"  Sortino ratio   :     {m['sortino']:.2f}")
    logger.info(f"  Ann. volatility : ${m['ann_vol']:>8,.2f}")

    # Day Stats
    logger.info(sep2)
    logger.info(f"  Best day        : ${m['best_day']:>8,.2f}")
    logger.info(f"  Worst day       : ${m['worst_day']:>8,.2f}")
    logger.info(f"  Best trade      : ${m['best_trade']:>8,.2f}")
    logger.info(f"  Worst trade     : ${m['worst_trade']:>8,.2f}")
    logger.info(f"  Max win streak  : {m['max_win_streak']}")

    # Key Config (only show active settings)
    logger.info(sep2)
    logger.info(f"  Score threshold : {_cfg.SCORE_THRESHOLD}")
    logger.info(f"  Strike offset   : {_cfg.STRIKE_OFFSET}pt" + (f" (VIX-adaptive)" if _cfg.ENABLE_VIX_ADAPTIVE_OFFSET else ""))
    logger.info(f"  Spread width    : {_cfg.SPREAD_WIDTH}pt" + (f" (ATR-adaptive)" if _cfg.ENABLE_ATR_WIDTH else ""))
    logger.info(f"  Credit model    : {_cfg.CREDIT_MODEL}")
    logger.info(f"  Bidirectional   : {_cfg.ENABLE_PUT_SIDE} (put score <= {_cfg.PUT_SCORE_MAX})")
    logger.info(f"  Intraday SL     : {_cfg.INTRADAY_SL_CAP}")
    logger.info(f"  Credit floor    : ${_cfg.MIN_CREDIT}" if _cfg.ENABLE_CREDIT_FLOOR else "")
    logger.info(f"  VIX max filter  : {_cfg.VIX_MAX_FILTER}")
    logger.info(f"  Term structure  : skip backwardation (ratio > {_cfg.TERM_STRUCTURE_MAX_RATIO})" if _cfg.ENABLE_TERM_STRUCTURE_FILTER else "")
    if _cfg.ENABLE_DANGER_SIZING:
        logger.info(f"  Danger sizing   : {_cfg.DANGER_LOW_MULT}x/{_cfg.DANGER_MED_MULT}x/skip")
    if _cfg.ENABLE_STREAK_BOOST:
        logger.info(f"  Streak boost    : {_cfg.STREAK_BOOST_MULT}x on day {_cfg.STREAK_BOOST_DAY}+")
    if _cfg.ENABLE_COMPOUND_GROWTH:
        logger.info(f"  Compound growth : +${_cfg.COMPOUND_GROWTH_ADD} per ${_cfg.COMPOUND_GROWTH_STEP:,.0f}, max ${_cfg.COMPOUND_GROWTH_MAX}")
    logger.info(f"  Commission      : ${_cfg.COMMISSION}/leg")
    logger.info(sep)
    return m


def print_vix_analysis(trades: list[dict]):
    VIX_BUCKETS = [("< 15", None, 15), ("15-20", 15, 20), ("20-25", 20, 25),
                   ("25-30", 25, 30), ("30-35", 30, 35), ("35+", 35, None)]
    sep = "-" * 62
    logger.info(sep)
    logger.info("  VIX CORRELATION ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'VIX Range':<10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)
    for label, lo, hi in VIX_BUCKETS:
        bucket = [t for t in trades
                  if (lo is None or (t.get("vix_level") or 0) >= lo) and
                     (hi is None or (t.get("vix_level") or 0) < hi)]
        n = len(bucket)
        if n == 0:
            continue
        wins = sum(1 for t in bucket if t["pnl_earned"] > 0)
        total = sum(t["pnl_earned"] for t in bucket)
        logger.info(f"  {label:<10} | {n:>7} | {wins/n*100:>8.1f}% | ${total/n:>9,.2f} | ${total:>11,.2f}")
    logger.info(sep)


def print_put_call_split(trades: list[dict]):
    sep = "-" * 95
    logger.info(sep)
    logger.info("  PUT vs CALL SPREAD PERFORMANCE SPLIT")
    logger.info(sep)
    logger.info(f"  {'Type':<8} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | {'Prof Fac':>9}")
    logger.info(sep)
    for label_check, label in [("call", "CALL"), ("put", "PUT")]:
        # Identify by score: score >= threshold = call, score <= put_max = put
        bucket = [t for t in trades if t.get("signal_mode") != "reentry"]
        if label_check == "call":
            bucket = [t for t in bucket if (t.get("exhaustion_score") or 0) >= _cfg.SCORE_THRESHOLD]
        else:
            bucket = [t for t in bucket if (t.get("exhaustion_score") or 0) <= _cfg.PUT_SCORE_MAX]
        if not bucket:
            continue
        m = compute_metrics(bucket)
        logger.info(
            f"  {label:<8} | {m['num_trades']:>7} | {m['win_rate']:>8.1f}% | "
            f"${m['total_pnl']/m['num_trades']:>9,.2f} | ${m['total_pnl']:>11,.2f} | "
            f"${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | {m['profit_factor']:>8.2f}x"
        )
    logger.info(sep)


def print_monthly_pnl(trades: list[dict]):
    monthly: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        d = t.get("trade_date", "")
        if len(d) >= 6:
            monthly[d[:4]][int(d[4:6])] += t["pnl_earned"]
    sep = "-" * 110
    logger.info(sep)
    logger.info("  MONTHLY P&L TABLE")
    logger.info(sep)
    header = f"  {'Year':<6}"
    for mo in range(1, 13):
        header += f" | {mo:>7}"
    header += f" | {'Total':>10}"
    logger.info(header)
    logger.info(sep)
    for year in sorted(monthly.keys()):
        row = f"  {year:<6}"
        total = 0
        for mo in range(1, 13):
            pnl = monthly[year].get(mo, 0)
            total += pnl
            row += f" | ${pnl:>6,.0f}" if pnl != 0 else f" | {'--':>7}"
        row += f" | ${total:>9,.0f}"
        logger.info(row)
    logger.info(sep)


def print_intra_month_drawdown(trades: list[dict]):
    """Intra-month max drawdown by year/month."""
    monthly_dd: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    monthly_trades: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        d = t.get("trade_date", "")
        if len(d) >= 6:
            monthly_trades[d[:4]][int(d[4:6])].append(t["pnl_earned"])

    sep = "-" * 110
    logger.info(sep)
    logger.info("  INTRA-MONTH MAX DRAWDOWN")
    logger.info(sep)
    header = f"  {'Year':<6}"
    for mo in range(1, 13):
        header += f" | {mo:>7}"
    header += f" | {'Ann DD':>10}"
    logger.info(header)
    logger.info(sep)

    for year in sorted(monthly_trades.keys()):
        row = f"  {year:<6}"
        annual_dd = 0
        for mo in range(1, 13):
            pnls = monthly_trades[year].get(mo, [])
            if not pnls:
                row += f" | {'--':>7}"
                continue
            cum = 0
            peak = 0
            dd = 0
            for p in pnls:
                cum += p
                peak = max(peak, cum)
                dd = max(dd, peak - cum)
            monthly_dd[year][mo] = dd
            annual_dd = max(annual_dd, dd)
            row += f" | ${dd:>6,.0f}"
        row += f" | ${annual_dd:>9,.0f}"
        logger.info(row)
    logger.info(sep)


def print_spy_comparison(trades: list[dict]):
    """Monthly return: Strategy vs SPY."""
    if not os.path.exists(SPY_CSV):
        logger.info("  (SPY comparison skipped — spy_monthly_closes.csv not found)")
        return

    spy = pd.read_csv(SPY_CSV, parse_dates=["date"])
    spy["year"] = spy["date"].dt.year.astype(str)
    spy["month"] = spy["date"].dt.month
    spy["spy_ret"] = spy["close"].pct_change() * 100
    spy_map = {(r["year"], r["month"]): r["spy_ret"] for _, r in spy.iterrows()}

    # Strategy monthly P&L as % of starting equity
    monthly: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        d = t.get("trade_date", "")
        if len(d) >= 6:
            monthly[d[:4]][int(d[4:6])] += t["pnl_earned"]

    sep = "-" * 120
    logger.info(sep)
    logger.info("  MONTHLY RETURN: STRATEGY vs SPY")
    logger.info(sep)
    header = f"  {'Year':<6}"
    for mo in range(1, 13):
        header += f" | {mo:>10}"
    header += f" | {'Strat':>7} | {'SPY':>7} | {'Alpha':>7}"
    logger.info(header)
    logger.info(sep)

    for year in sorted(monthly.keys()):
        row = f"  {year:<6}"
        strat_annual = 0
        spy_annual = 0
        for mo in range(1, 13):
            strat_pnl = monthly[year].get(mo, 0)
            strat_ret = strat_pnl / _cfg.STARTING_EQUITY * 100
            spy_ret = spy_map.get((year, mo), 0)
            if spy_ret and not np.isnan(spy_ret):
                strat_annual += strat_ret
                spy_annual += spy_ret
                row += f" | {strat_ret:>+4.1f}/{spy_ret:>+4.1f}"
            elif strat_pnl != 0:
                strat_annual += strat_ret
                row += f" | {strat_ret:>+4.1f}/  --"
            else:
                row += f" | {'--':>10}"
        alpha = strat_annual - spy_annual
        row += f" | {strat_annual:>+6.1f}% | {spy_annual:>+6.1f}% | {alpha:>+6.1f}%"
        logger.info(row)
    logger.info(sep)


def print_worst_days(trades: list[dict], n: int = 30):
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_trades: dict[str, int] = defaultdict(int)
    daily_vix: dict[str, float] = {}
    for t in trades:
        d = t.get("trade_date", "")
        daily_pnl[d] += t["pnl_earned"]
        daily_trades[d] += 1
        if d not in daily_vix:
            daily_vix[d] = t.get("vix_level", 0) or 0

    sorted_days = sorted(daily_pnl.items(), key=lambda x: x[1])[:n]
    sep = "-" * 70
    logger.info(sep)
    logger.info(f"  LARGEST LOSS DAYS (worst {n})")
    logger.info(sep)
    logger.info(f"  {'Date':<12} | {'VIX':>6} | {'Trades':>7} | {'Day P&L':>12} | {'Event':>10}")
    logger.info(sep)

    for date, pnl in sorted_days:
        vix = daily_vix.get(date, 0)
        tc = daily_trades[date]
        event = "FOMC" if date in FOMC_DATES else ("TW" if date in TRIPLE_WITCHING_DATES else "")
        logger.info(f"  {date:<12} | {vix:>6.1f} | {tc:>7} | ${pnl:>11,.2f} | {event:>10}")
    logger.info(sep)


def print_seasonality(trades: list[dict]):
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Day of week
    dow_data = defaultdict(list)
    for t in trades:
        try:
            d = datetime.strptime(t["trade_date"], "%Y%m%d")
            dow_data[d.weekday()].append(t["pnl_earned"])
        except Exception:
            pass

    sep = "-" * 58
    logger.info(sep)
    logger.info("  SEASONALITY — DAY OF WEEK")
    logger.info(sep)
    logger.info(f"  {'Day':<5} | {'Trades':>7} | {'WR%':>6} | {'Total P&L':>12} | {'Avg P&L':>10}")
    logger.info(sep)
    for dow in range(5):
        pnls = dow_data.get(dow, [])
        if not pnls:
            continue
        n = len(pnls)
        wr = sum(1 for p in pnls if p > 0) / n * 100
        logger.info(f"  {dow_names[dow]:<5} | {n:>7} | {wr:>5.1f}% | ${sum(pnls):>11,.2f} | ${np.mean(pnls):>9,.2f}")
    logger.info(sep)

    # Month
    month_data = defaultdict(list)
    for t in trades:
        try:
            d = datetime.strptime(t["trade_date"], "%Y%m%d")
            month_data[d.month].append(t["pnl_earned"])
        except Exception:
            pass

    logger.info(sep)
    logger.info("  SEASONALITY — MONTH")
    logger.info(sep)
    logger.info(f"  {'Month':<5} | {'Trades':>7} | {'WR%':>6} | {'Total P&L':>12} | {'Avg P&L':>10}")
    logger.info(sep)
    for mo in range(1, 13):
        pnls = month_data.get(mo, [])
        if not pnls:
            continue
        n = len(pnls)
        wr = sum(1 for p in pnls if p > 0) / n * 100
        logger.info(f"  {month_names[mo-1]:<5} | {n:>7} | {wr:>5.1f}% | ${sum(pnls):>11,.2f} | ${np.mean(pnls):>9,.2f}")
    logger.info(sep)


def print_econ_date_analysis(trades: list[dict]):
    """Calendar / econ event analysis."""
    from zenith_config import FOMC_DATES, TRIPLE_WITCHING_DATES, ECON_DATES

    event_map: dict[str, str] = {}
    for d in FOMC_DATES:
        event_map[d] = "FOMC"
    for d in TRIPLE_WITCHING_DATES:
        event_map[d] = "Triple Witch"
    for d in ECON_DATES:
        if d not in event_map:
            event_map[d] = "CPI/NFP"

    event_trades: dict[str, list] = defaultdict(list)
    for t in trades:
        d = t.get("trade_date", "")
        ev = event_map.get(d, "Normal")
        event_trades[ev].append(t)

    sep = "-" * 72
    logger.info(sep)
    logger.info("  CALENDAR / ECON EVENT ANALYSIS  (sorted by WR%)")
    logger.info(sep)
    logger.info(f"  {'Event':<14} | {'Days':>5} | {'WR%':>6} | {'Avg/Day':>10} | {'Total P&L':>12} | {'Max DD':>10}")
    logger.info(sep)

    results = []
    for ev, ev_trades in event_trades.items():
        m = compute_metrics(ev_trades)
        avg_day = m["total_pnl"] / len(set(t.get("trade_date", "") for t in ev_trades)) if ev_trades else 0
        results.append((ev, len(ev_trades), m["win_rate"], avg_day, m["total_pnl"], m["max_dd"]))

    for ev, n, wr, avg_day, total, dd in sorted(results, key=lambda x: x[2], reverse=True):
        logger.info(f"  {ev:<14} | {n:>5} | {wr:>5.1f}% | ${avg_day:>9,.2f} | ${total:>11,.2f} | ${dd:>9,.2f}")
    logger.info(sep)


# =============================================
#  RESULTS.MD GENERATION
# =============================================
def append_results_md(trades: list[dict], date_range=None):
    """Write RESULTS.md keeping only the latest 3 runs at the top."""
    m = compute_metrics(trades)
    results_path = os.path.join(os.path.dirname(__file__), "RESULTS.md")

    # Load previous results to keep last 3
    prev_runs = []
    if os.path.exists(results_path):
        try:
            with open(results_path) as f:
                content = f.read()
            # Split by run headers
            parts = content.split("# Zenith Fade CCS — Backtest Results (Run #")
            for part in parts[1:]:  # skip preamble
                prev_runs.append("# Zenith Fade CCS — Backtest Results (Run #" + part)
        except Exception:
            pass
    if date_range is not None and len(date_range) > 0:
        trading_days = sum(1 for d in date_range if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS)
    else:
        trading_days = 0
    trade_days = len(set(t.get("trade_date", "") for t in trades))
    start = trades[0]["signal_date"] if trades else "N/A"
    end = trades[-1]["trade_date"] if trades else "N/A"
    expectancy = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0

    # Load run count
    run_num = 1
    if os.path.exists(RUN_HISTORY_FILE):
        try:
            with open(RUN_HISTORY_FILE) as f:
                run_num = len(json.load(f)) + 1
        except Exception:
            pass

    L = []
    L.append(f"# Zenith Fade CCS — Backtest Results (Run #{run_num})")
    L.append(f"")
    L.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"**Period**: {start} -> {end}")
    L.append(f"")

    # Returns
    L.append(f"## Returns")
    L.append(f"")
    L.append(f"| Metric | Value |")
    L.append(f"|---|---|")
    L.append(f"| Total P&L | ${m['total_pnl']:,.2f} |")
    L.append(f"| CAGR | {m['cagr']:.1f}% |")
    L.append(f"| Expectancy/trade | ${expectancy:,.2f} |")
    L.append(f"| Profit Factor | {m['profit_factor']:.2f}x |")
    L.append(f"| Avg Win | ${m['avg_win']:,.2f} |")
    L.append(f"| Avg Loss | ${m['avg_loss']:,.2f} |")
    L.append(f"")

    # Risk
    L.append(f"## Risk")
    L.append(f"")
    L.append(f"| Metric | Value |")
    L.append(f"|---|---|")
    L.append(f"| Max Drawdown | -${m['max_dd']:,.2f} ({m['max_dd_pct']:.1f}%) |")
    L.append(f"| Recovery Factor | {m['recovery_factor']:.2f} |")
    L.append(f"| Time Underwater | {m['pct_underwater']:.1f}% |")
    L.append(f"")

    # Key Config (only active settings)
    L.append(f"## Key Config")
    L.append(f"")
    L.append(f"| Parameter | Value |")
    L.append(f"|---|---|")
    L.append(f"| Signal Mode | {_cfg.SIGNAL_MODE} (score >= {_cfg.SCORE_THRESHOLD}) |")
    L.append(f"| Strike Offset | {_cfg.STRIKE_OFFSET}pt" + (" (VIX-adaptive)" if _cfg.ENABLE_VIX_ADAPTIVE_OFFSET else "") + " |")
    L.append(f"| Spread Width | {_cfg.SPREAD_WIDTH}pt" + (" (ATR-adaptive)" if _cfg.ENABLE_ATR_WIDTH else "") + " |")
    L.append(f"| Credit Model | {_cfg.CREDIT_MODEL} |")
    L.append(f"| Bidirectional | {_cfg.ENABLE_PUT_SIDE} (put score <= {_cfg.PUT_SCORE_MAX}) |")
    L.append(f"| Intraday SL | {_cfg.INTRADAY_SL_CAP} |")
    if _cfg.ENABLE_CREDIT_FLOOR:
        L.append(f"| Credit Floor | ${_cfg.MIN_CREDIT} |")
    L.append(f"| VIX Max Filter | {_cfg.VIX_MAX_FILTER} |")
    if _cfg.ENABLE_TERM_STRUCTURE_FILTER:
        L.append(f"| Term Structure | skip ratio > {_cfg.TERM_STRUCTURE_MAX_RATIO} |")
    if _cfg.ENABLE_DANGER_SIZING:
        L.append(f"| Danger Sizing | {_cfg.DANGER_LOW_MULT}x / {_cfg.DANGER_MED_MULT}x / skip |")
    if _cfg.ENABLE_STREAK_BOOST:
        L.append(f"| Streak Boost | {_cfg.STREAK_BOOST_MULT}x on day {_cfg.STREAK_BOOST_DAY}+ |")
    if _cfg.ENABLE_COMPOUND_GROWTH:
        L.append(f"| Compound Growth | max ${_cfg.COMPOUND_GROWTH_MAX} |")
    L.append(f"| Commission | ${_cfg.COMMISSION}/leg |")
    L.append(f"")

    # Ratios & Volatility
    L.append(f"## Ratios & Volatility")
    L.append(f"")
    L.append(f"| Metric | Value |")
    L.append(f"|---|---|")
    L.append(f"| Win Rate | {m['win_rate']:.1f}% |")
    L.append(f"| Sharpe Ratio | {m['sharpe']:.2f} |")
    L.append(f"| Sortino Ratio | {m['sortino']:.2f} |")
    L.append(f"| Calmar Ratio | {m['calmar']:.2f} |")
    L.append(f"| Ann. Volatility | ${m['ann_vol']:,.2f} |")
    L.append(f"")

    # Day Stats
    L.append(f"## Day Stats")
    L.append(f"")
    L.append(f"| Metric | Value |")
    L.append(f"|---|---|")
    L.append(f"| Business Days | {trading_days} |")
    L.append(f"| Days Traded | {trade_days} |")
    L.append(f"| Total Trades | {m['num_trades']} |")
    L.append(f"| Best Day | ${m['best_day']:,.2f} |")
    L.append(f"| Worst Day | ${m['worst_day']:,.2f} |")
    L.append(f"| Max Win Streak | {m['max_win_streak']} |")
    L.append(f"")

    # VIX Correlation
    L.append(f"## VIX Correlation")
    L.append(f"")
    L.append(f"| VIX Range | Trades | WR% | Avg P&L | Total P&L |")
    L.append(f"|---|---:|---:|---:|---:|")
    for label, lo, hi in [("< 15", None, 15), ("15-20", 15, 20), ("20-25", 20, 25), ("25-30", 25, 30), ("30+", 30, None)]:
        bucket = [t for t in trades if (lo is None or (t.get("vix_level") or 0) >= lo) and (hi is None or (t.get("vix_level") or 0) < hi)]
        if not bucket:
            continue
        n = len(bucket)
        wr = sum(1 for t in bucket if t["pnl_earned"] > 0) / n * 100
        total = sum(t["pnl_earned"] for t in bucket)
        L.append(f"| {label} | {n} | {wr:.1f}% | ${total/n:,.2f} | ${total:,.2f} |")
    L.append(f"")

    # Put/Call Split
    L.append(f"## Put vs Call Split")
    L.append(f"")
    L.append(f"| Type | Trades | WR% | Total P&L | PF |")
    L.append(f"|---|---:|---:|---:|---:|")
    for side_label, side_check in [("CALL", "call"), ("PUT", "put")]:
        if side_check == "call":
            bucket = [t for t in trades if (t.get("exhaustion_score") or 0) >= _cfg.SCORE_THRESHOLD and t.get("signal_mode") != "reentry"]
        else:
            bucket = [t for t in trades if (t.get("exhaustion_score") or 0) <= _cfg.PUT_SCORE_MAX and t.get("signal_mode") != "reentry"]
        if not bucket:
            continue
        bm = compute_metrics(bucket)
        L.append(f"| {side_label} | {bm['num_trades']} | {bm['win_rate']:.1f}% | ${bm['total_pnl']:,.2f} | {bm['profit_factor']:.2f}x |")
    L.append(f"")

    # Monthly P&L
    monthly: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        d = t.get("trade_date", "")
        if len(d) >= 6:
            monthly[d[:4]][int(d[4:6])] += t["pnl_earned"]

    L.append(f"## Monthly P&L ($)")
    L.append(f"")
    header = "| Year |"
    for mo in range(1, 13):
        header += f" {mo} |"
    header += " Total |"
    L.append(header)
    L.append("|---|" + "---:|" * 13)
    for year in sorted(monthly.keys()):
        row = f"| {year} |"
        total = 0
        for mo in range(1, 13):
            pnl = monthly[year].get(mo, 0)
            total += pnl
            row += f" ${pnl:,.0f} |" if pnl != 0 else " -- |"
        row += f" **${total:,.0f}** |"
        L.append(row)
    L.append(f"")

    # Intra-month max drawdown
    L.append(f"## Intra-Month Max Drawdown ($)")
    L.append(f"")
    header = "| Year |"
    for mo in range(1, 13):
        header += f" {mo} |"
    header += " Annual DD |"
    L.append(header)
    L.append("|---|" + "---:|" * 13)
    monthly_trades_map: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        d = t.get("trade_date", "")
        if len(d) >= 6:
            monthly_trades_map[d[:4]][int(d[4:6])].append(t["pnl_earned"])
    for year in sorted(monthly_trades_map.keys()):
        row = f"| {year} |"
        annual_dd = 0
        for mo in range(1, 13):
            pnls = monthly_trades_map[year].get(mo, [])
            if not pnls:
                row += " -- |"
                continue
            cum = peak = dd = 0
            for p in pnls:
                cum += p
                peak = max(peak, cum)
                dd = max(dd, peak - cum)
            annual_dd = max(annual_dd, dd)
            row += f" ${dd:,.0f} |"
        row += f" **${annual_dd:,.0f}** |"
        L.append(row)
    L.append(f"")

    # Strategy vs SPY
    if os.path.exists(SPY_CSV):
        spy = pd.read_csv(SPY_CSV, parse_dates=["date"])
        spy["year"] = spy["date"].dt.year.astype(str)
        spy["month"] = spy["date"].dt.month
        spy["spy_ret"] = spy["close"].pct_change() * 100
        spy_map = {(r["year"], r["month"]): r["spy_ret"] for _, r in spy.iterrows()}

        L.append(f"## Monthly Return: Strategy vs SPY (strat% / SPY%)")
        L.append(f"")
        header = "| Year |"
        for mo in range(1, 13):
            header += f" {mo} |"
        header += " Strat | SPY | Alpha |"
        L.append(header)
        L.append("|---|" + ":---:|" * 12 + "---:|---:|---:|")
        for year in sorted(monthly.keys()):
            row = f"| {year} |"
            sa = sp = 0
            for mo in range(1, 13):
                sr = monthly[year].get(mo, 0) / _cfg.STARTING_EQUITY * 100
                spy_r = spy_map.get((year, mo))
                if spy_r is not None and not np.isnan(spy_r):
                    sa += sr
                    sp += spy_r
                    row += f" {sr:+.1f}/{spy_r:+.1f} |"
                elif sr != 0:
                    sa += sr
                    row += f" {sr:+.1f}/-- |"
                else:
                    row += " -- |"
            row += f" {sa:+.1f}% | {sp:+.1f}% | {sa-sp:+.1f}% |"
            L.append(row)
        L.append(f"")

    # Worst 30 loss days
    daily_pnl_map: dict[str, float] = defaultdict(float)
    daily_tc: dict[str, int] = defaultdict(int)
    daily_vix_map: dict[str, float] = {}
    for t in trades:
        d = t.get("trade_date", "")
        daily_pnl_map[d] += t["pnl_earned"]
        daily_tc[d] += 1
        if d not in daily_vix_map:
            daily_vix_map[d] = t.get("vix_level", 0) or 0
    worst = sorted(daily_pnl_map.items(), key=lambda x: x[1])[:30]

    L.append(f"## Largest Loss Days (worst 30)")
    L.append(f"")
    L.append(f"| Date | VIX | Trades | Day P&L | Event |")
    L.append(f"|---|---:|---:|---:|---|")
    for date, pnl in worst:
        vix = daily_vix_map.get(date, 0)
        ev = "FOMC" if date in FOMC_DATES else ("TW" if date in TRIPLE_WITCHING_DATES else "")
        L.append(f"| {date} | {vix:.1f} | {daily_tc[date]} | ${pnl:,.2f} | {ev} |")
    L.append(f"")

    # Seasonality
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    L.append(f"## Seasonality — Day of Week")
    L.append(f"")
    L.append(f"| Day | Trades | WR% | Total P&L | Avg P&L |")
    L.append(f"|---|---:|---:|---:|---:|")
    dow_data = defaultdict(list)
    for t in trades:
        try:
            dow_data[datetime.strptime(t["trade_date"], "%Y%m%d").weekday()].append(t["pnl_earned"])
        except Exception:
            pass
    for dow in range(5):
        pnls = dow_data.get(dow, [])
        if not pnls:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        L.append(f"| {dow_names[dow]} | {len(pnls)} | {wr:.1f}% | ${sum(pnls):,.2f} | ${np.mean(pnls):,.2f} |")
    L.append(f"")

    L.append(f"## Seasonality — Month")
    L.append(f"")
    L.append(f"| Month | Trades | WR% | Total P&L | Avg P&L |")
    L.append(f"|---|---:|---:|---:|---:|")
    mo_data = defaultdict(list)
    for t in trades:
        try:
            mo_data[datetime.strptime(t["trade_date"], "%Y%m%d").month].append(t["pnl_earned"])
        except Exception:
            pass
    for mo in range(1, 13):
        pnls = mo_data.get(mo, [])
        if not pnls:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        L.append(f"| {month_names[mo-1]} | {len(pnls)} | {wr:.1f}% | ${sum(pnls):,.2f} | ${np.mean(pnls):,.2f} |")
    L.append(f"")

    # Calendar econ events
    from zenith_config import ECON_DATES
    event_map: dict[str, str] = {}
    for d in FOMC_DATES:
        event_map[d] = "FOMC"
    for d in TRIPLE_WITCHING_DATES:
        event_map[d] = "Triple Witch"
    for d in ECON_DATES:
        if d not in event_map:
            event_map[d] = "CPI/NFP"

    L.append(f"## Calendar / Econ Event Analysis")
    L.append(f"")
    L.append(f"| Event | Trades | WR% | Avg P&L | Total P&L |")
    L.append(f"|---|---:|---:|---:|---:|")
    event_trades_map: dict[str, list] = defaultdict(list)
    for t in trades:
        ev = event_map.get(t.get("trade_date", ""), "Normal")
        event_trades_map[ev].append(t)
    for ev in ["Normal", "FOMC", "CPI/NFP", "Triple Witch"]:
        et = event_trades_map.get(ev, [])
        if not et:
            continue
        em = compute_metrics(et)
        L.append(f"| {ev} | {em['num_trades']} | {em['win_rate']:.1f}% | ${em['total_pnl']/em['num_trades']:,.2f} | ${em['total_pnl']:,.2f} |")
    L.append(f"")

    new_run = "\n".join(L) + "\n"

    # Keep latest run + last 2 previous runs (3 total)
    all_runs = [new_run] + prev_runs[:2]
    with open(results_path, "w") as f:
        f.write("\n---\n\n".join(all_runs))
    logger.info(f"Results written to {results_path} (keeping last 3 runs)")


# =============================================
#  RUN HISTORY
# =============================================
def save_run_summary(trades: list[dict]):
    m = compute_metrics(trades)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "mode": _cfg.SIGNAL_MODE,
        "trades": m["num_trades"],
        "pnl": m["total_pnl"],
        "max_dd": m["max_dd"],
        "sharpe": m["sharpe"],
        "win_rate": m["win_rate"],
        "pf": m["profit_factor"],
        "score_thresh": _cfg.SCORE_THRESHOLD,
    }
    history = []
    if os.path.exists(RUN_HISTORY_FILE):
        try:
            with open(RUN_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass
    history.append(entry)
    with open(RUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    logger.info(f"Run summary appended to {RUN_HISTORY_FILE}")


def display_run_history():
    if not os.path.exists(RUN_HISTORY_FILE):
        return
    try:
        with open(RUN_HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        return
    if not history:
        return
    logger.info("=" * 80)
    logger.info("  RECENT RUN HISTORY")
    logger.info("-" * 80)
    logger.info(f"  {'#':>3} | {'Timestamp':<22} | {'Trades':>7} | {'P&L':>12} | {'DD':>10} | {'Sharpe':>7} | {'WR':>6}")
    logger.info("-" * 80)
    for i, entry in enumerate(history[-10:], start=max(1, len(history) - 9)):
        logger.info(
            f"  {i:>3} | {entry['timestamp'][:19]:<22} | {entry['trades']:>7} | "
            f"${entry['pnl']:>10,.0f} | -${entry['max_dd']:>8,.0f} | {entry['sharpe']:>6.2f} | {entry['win_rate']:>5.1f}%"
        )
    logger.info("=" * 80)
