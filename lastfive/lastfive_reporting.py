"""LastFive — Reporting & Analysis.

Comprehensive performance reports modeled after MEDS project:
  - Returns, risk, key config, ratios & volatility
  - Day stats, VIX correlation, put/call split
  - Monthly P&L, intra-month max drawdown
  - Monthly return strategy vs SPY
  - Largest loss days (worst 30)
  - Seasonality: day of week, month
  - Calendar/econ event analysis
  - Run # tracking
"""
from __future__ import annotations

import csv
import datetime as _dt_mod
import json
import math
import os
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

import lastfive_config as _cfg
from lastfive_config import *
from lastfive_engine import logger, _DAILY_INDICATORS

# Run counter (persistent across runs)
_RUN_NUMBER_FILE = os.path.join(LOGS_DIR, "run_number.txt")


def _get_run_number() -> int:
    """Increment and return a persistent run counter."""
    n = 1
    if os.path.exists(_RUN_NUMBER_FILE):
        try:
            n = int(open(_RUN_NUMBER_FILE).read().strip()) + 1
        except Exception:
            pass
    with open(_RUN_NUMBER_FILE, "w") as f:
        f.write(str(n))
    return n


# --- Helpers ---
def _sf(val):
    try:
        return float(val) if val not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _bucket_stats(bucket):
    bp = [float(t.get("pnl_earned", 0)) for t in bucket]
    m = len(bp)
    if m == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    w = sum(1 for x in bp if x > 0)
    eq = pk = dd = 0.0
    for x in bp:
        eq += x; pk = max(pk, eq); dd = min(dd, eq - pk)
    return m, w / m * 100, sum(bp) / m, sum(bp), dd


def _max_dd_from_list(pnls):
    eq = pk = dd = 0.0
    for p in pnls:
        eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
    return dd


def _get_spy_monthly_returns(start_year, end_year):
    """Load SPY monthly returns from spy_monthly_closes.csv if available."""
    path = os.path.join(os.path.dirname(__file__), "..", "meds", "spy_monthly_closes.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        ret = {}
        for _, row in df.iterrows():
            y, m, r = int(row["year"]), int(row["month"]), float(row["return_pct"])
            if start_year <= y <= end_year:
                ret[(y, m)] = r
        return ret if ret else None
    except Exception:
        return None


# --- VIX Buckets ---
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15-18", 15.0, 18.0),
    ("18-20", 18.0, 20.0),
    ("20-22", 20.0, 22.0),
    ("22-26", 22.0, 26.0),
    ("26-30", 26.0, 30.0),
    ("30+",   30.0, None),
]


# =============================================
#  CONSOLE REPORTS
# =============================================

def print_performance_report(all_trades: list, date_range: pd.DatetimeIndex) -> dict:
    """Comprehensive performance report to console."""
    run_num = _get_run_number()
    pnls = [float(t["pnl_earned"]) for t in all_trades]
    total = sum(pnls)
    n = len(pnls)
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    wins = len(win_pnls)
    losses_n = len(loss_pnls)
    wr = wins / n * 100 if n else 0
    avg_win = np.mean(win_pnls) if win_pnls else 0
    avg_loss = np.mean(loss_pnls) if loss_pnls else 0
    expectancy = (avg_win * wins / n) + (avg_loss * losses_n / n) if n else 0
    pf = abs(sum(win_pnls) / sum(loss_pnls)) if loss_pnls and sum(loss_pnls) != 0 else float("inf")

    # Gross premium
    gross_premium = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100 for t in all_trades)
    prem_capture = (total / gross_premium * 100) if gross_premium > 0 else 0

    # Per-day P&L
    day_pnls = defaultdict(float)
    day_trade_count = defaultdict(int)
    for t in all_trades:
        day_pnls[t["date"]] += float(t["pnl_earned"])
        day_trade_count[t["date"]] += 1
    days_traded = len(day_pnls)
    avg_trades_day = n / days_traded if days_traded else 0
    best_day = max(day_pnls.values()) if day_pnls else 0
    worst_day = min(day_pnls.values()) if day_pnls else 0

    # Sharpe & Sortino (daily P&L basis)
    rf_daily = _cfg.ANNUAL_RF_RATE * _cfg.STARTING_EQUITY / 252
    daily_vals = list(day_pnls.values())
    n_days = len(daily_vals)
    mean_daily = sum(daily_vals) / n_days if n_days else 0
    var_daily = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily = math.sqrt(var_daily)
    ann_vol = std_daily * math.sqrt(252)
    sharpe = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0

    downside_d = [p for p in daily_vals if p < mean_daily]
    down_var_d = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    # Max drawdown
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    # Time underwater
    eq_d = peak_d = 0.0; underwater_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]; peak_d = max(peak_d, eq_d)
        if eq_d < peak_d: underwater_days += 1
    time_uw_pct = underwater_days / days_traded * 100 if days_traded else 0

    # CAGR, Calmar, Recovery
    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        first_d = datetime.strptime(sorted_dates[0], "%Y%m%d")
        last_d = datetime.strptime(sorted_dates[-1], "%Y%m%d")
        years = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    final_eq = _cfg.STARTING_EQUITY + total
    cagr = ((final_eq / _cfg.STARTING_EQUITY) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = cagr / abs(max_dd / _cfg.STARTING_EQUITY * 100) if max_dd != 0 else float("inf")
    recovery = total / abs(max_dd) if max_dd != 0 else float("inf")

    # Commission
    total_commission = sum(4 * float(t.get("qty", 1)) * _cfg.COMMISSION for t in all_trades)

    # BP
    bp_per_trade = [(_cfg.SPREAD_WIDTH - float(t.get("credit_received", 0))) * float(t.get("qty", 1)) * 100 for t in all_trades]
    avg_bp = sum(bp_per_trade) / n if n else 0
    day_bp = defaultdict(float)
    for t, bp in zip(all_trades, bp_per_trade):
        day_bp[t["date"]] += bp
    max_bp_day = max(day_bp.values()) if day_bp else 0
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0

    # Max consec losses
    max_consec = consec = 0
    for p in pnls:
        if p <= 0:
            consec += 1; max_consec = max(max_consec, consec)
        else:
            consec = 0

    sep = "=" * 60
    sep2 = "-" * 60
    logger.info(sep)
    logger.info(f"  LASTFIVE PERFORMANCE REPORT  (Run #{run_num})")
    logger.info(sep)

    # Period
    logger.info(f"  Period          : {_cfg.PILOT_YEAR_START} -> {_cfg.PILOT_YEAR_END}")
    logger.info(f"  Days traded     : {days_traded}")
    logger.info(f"  Total trades    : {n}  (wins: {wins}  losses: {losses_n})")
    logger.info(f"  Avg trades/day  : {avg_trades_day:.1f}")
    logger.info(sep2)

    # Returns
    logger.info(f"  Total P&L       : ${total:>12,.2f}  (net of commission)")
    logger.info(f"  Gross premium   : ${gross_premium:>12,.2f}")
    logger.info(f"  Prem capture    : {prem_capture:>11.1f}%")
    logger.info(f"  CAGR            : {cagr:>11.1f}%  (acct=${_cfg.STARTING_EQUITY:,.0f})")
    logger.info(f"  Avg win/trade   : ${avg_win:>12,.2f}")
    logger.info(f"  Avg loss/trade  : ${avg_loss:>12,.2f}")
    logger.info(f"  Expectancy      : ${expectancy:>12,.2f}")
    logger.info(f"  Profit factor   : {pf:>12.2f}x")
    logger.info(sep2)

    # Risk
    logger.info(f"  Max drawdown    : ${max_dd:>12,.2f}  ({max_dd_pct:.1f}%)")
    logger.info(f"  Calmar ratio    : {calmar:>12.2f}")
    logger.info(f"  Recovery factor : {recovery:>12.2f}")
    logger.info(f"  Time underwater : {time_uw_pct:>11.1f}%  ({underwater_days} of {days_traded} days)")
    logger.info(f"  Max consec loss : {max_consec}")
    logger.info(sep2)

    # Key Config
    logger.info(f"  VIX adaptive    : {'ON' if _cfg.ENABLE_VIX_ADAPTIVE else 'OFF'} (ATM<{_cfg.VIX_ATM_CUTOFF}, d{_cfg.DIST_MID:.0f}<{_cfg.VIX_MID_CUTOFF}, d{_cfg.DIST_WIDE:.0f}<{_cfg.VIX_WIDE_CUTOFF})")
    logger.info(f"  Tiered sizing   : {'ON' if _cfg.ENABLE_TIERED_SIZING else 'OFF'} (${_cfg.RISK_LOW:.0f}/${_cfg.RISK_MID:.0f}/${_cfg.RISK_HIGH:.0f})")
    logger.info(f"  Width / MinCr   : ${_cfg.SPREAD_WIDTH:.0f} / ${_cfg.MIN_NET_CREDIT:.2f}")
    logger.info(f"  Afternoon filter: < {_cfg.AFTERNOON_RETURN_MAX}%")
    logger.info(f"  Put filter      : aftn<{_cfg.PUT_AFTERNOON_MIN}% + mom30<{_cfg.PUT_MOM30_MIN}%")
    logger.info(f"  Commission      : ${_cfg.COMMISSION}/leg")
    logger.info(sep2)

    # Ratios & Volatility
    logger.info(f"  Win rate        : {wr:>11.1f}%")
    logger.info(f"  Sharpe ratio    : {sharpe:>12.2f}  (rf={_cfg.ANNUAL_RF_RATE*100:.1f}%)")
    logger.info(f"  Sortino ratio   : {sortino:>12.2f}")
    logger.info(f"  Ann. volatility : ${ann_vol:>12,.2f}")
    logger.info(sep2)

    # Day Stats
    logger.info(f"  Best day        : ${best_day:>12,.2f}")
    logger.info(f"  Worst day       : ${worst_day:>12,.2f}")
    logger.info(f"  Best trade      : ${max(pnls):>12,.2f}")
    logger.info(f"  Worst trade     : ${min(pnls):>12,.2f}")
    logger.info(sep2)

    # Cost & Capital
    logger.info(f"  Commission total: ${total_commission:>12,.2f}")
    logger.info(f"  Avg BP/trade    : ${avg_bp:>12,.2f}")
    logger.info(f"  Avg BP/day      : ${avg_bp_day:>12,.2f}")
    logger.info(f"  Max BP day      : ${max_bp_day:>12,.2f}")
    logger.info(sep)

    return {
        "run_num": run_num, "total_pnl": total, "max_dd": max_dd, "sharpe": sharpe,
        "sortino": sortino, "calmar": calmar, "win_rate": wr, "trades": n, "pf": pf,
        "cagr": cagr, "avg_win": avg_win, "avg_loss": avg_loss, "worst_trade": min(pnls),
        "best_trade": max(pnls), "best_day": best_day, "worst_day": worst_day,
        "ann_vol": ann_vol, "recovery": recovery, "time_uw_pct": time_uw_pct,
        "gross_premium": gross_premium, "prem_capture": prem_capture,
    }


# =============================================
#  VIX CORRELATION
# =============================================
def print_vix_analysis(all_trades: list) -> None:
    sep = "-" * 80
    logger.info(sep)
    logger.info("  VIX CORRELATION ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'VIX Range':<10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12} | {'Max DD':>10}")
    logger.info(sep)

    for label, lo, hi in VIX_BUCKETS:
        bucket = [t for t in all_trades if _sf(t.get("vix_level")) is not None
                  and (lo is None or _sf(t["vix_level"]) >= lo)
                  and (hi is None or _sf(t["vix_level"]) < hi)]
        cnt, wr, avg_p, tot_p, dd = _bucket_stats(bucket)
        logger.info(f"  {label:<10} | {cnt:>7} | {wr:>8.1f}% | ${avg_p:>9,.2f} | ${tot_p:>11,.2f} | ${dd:>9,.2f}")
    logger.info(sep)


# =============================================
#  PUT vs CALL SPLIT
# =============================================
def print_put_call_split(all_trades: list) -> None:
    sep = "-" * 95
    logger.info(sep)
    logger.info("  PUT vs CALL SPLIT")
    logger.info(sep)
    logger.info(f"  {'Side':<6} | {'Trades':>7} | {'WR':>7} | {'Avg P&L':>10} | {'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | {'PF':>6} | {'Max DD':>10}")
    logger.info(sep)

    for side in ("CALL", "PUT"):
        bucket = [t for t in all_trades if t.get("side") == side]
        pnls = [float(t["pnl_earned"]) for t in bucket]
        cnt = len(pnls)
        if cnt == 0:
            continue
        w = sum(1 for p in pnls if p > 0)
        wr = w / cnt * 100
        wp = [p for p in pnls if p > 0]
        lp = [p for p in pnls if p <= 0]
        avg_w = np.mean(wp) if wp else 0
        avg_l = np.mean(lp) if lp else 0
        pf_v = abs(sum(wp) / sum(lp)) if lp and sum(lp) != 0 else float("inf")
        dd = _max_dd_from_list(pnls)
        logger.info(
            f"  {side:<6} | {cnt:>7} | {wr:>6.1f}% | ${np.mean(pnls):>9,.2f} | ${sum(pnls):>11,.2f} | "
            f"${avg_w:>8,.2f} | ${avg_l:>8,.2f} | {pf_v:>5.2f} | ${dd:>9,.2f}"
        )
    logger.info(sep)


# =============================================
#  YEARLY + MONTHLY BREAKDOWN (grouped)
# =============================================
def print_monthly_pnl(all_trades: list) -> None:
    """Print year→month grouped breakdown with P&L, Trades, WR, Avg RoR, Avg P&L, Avg Risk, Best Day, Worst Day, DD."""

    # Organize trades by date, year, month
    day_pnls = defaultdict(float)       # date_str -> total P&L
    day_risk = defaultdict(float)       # date_str -> total risk deployed
    trades_by_ym = defaultdict(list)    # (year, month) -> [trade dicts]
    trades_by_y = defaultdict(list)     # year -> [trade dicts]

    for t in all_trades:
        d = str(t["date"]).replace(".0", "")
        y, m = int(d[:4]), int(d[4:6])
        pnl = float(t["pnl_earned"])
        day_pnls[d] += pnl
        # Risk deployed = (width - credit) * qty * 100
        cr = float(t.get("credit_received", 0))
        q = float(t.get("qty", 1))
        day_risk[d] += (_cfg.SPREAD_WIDTH - cr) * q * 100
        trades_by_ym[(y, m)].append(t)
        trades_by_y[y].append(t)

    def _period_stats(trade_list):
        """Compute stats for a list of trades in a period."""
        if not trade_list:
            return None
        pnls = [float(t["pnl_earned"]) for t in trade_list]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100

        # Avg P&L
        avg_pnl = sum(pnls) / n

        # Avg risk per trade
        risks = [(_cfg.SPREAD_WIDTH - float(t.get("credit_received", 0))) * float(t.get("qty", 1)) * 100
                 for t in trade_list]
        avg_risk = sum(risks) / n if n else 0

        # Avg RoR = avg P&L / avg risk
        avg_ror = (avg_pnl / avg_risk * 100) if avg_risk > 0 else 0

        # Daily P&L for best/worst day
        daily = defaultdict(float)
        for t in trade_list:
            daily[str(t["date"]).replace(".0", "")] += float(t["pnl_earned"])
        best_day = max(daily.values()) if daily else 0
        worst_day = min(daily.values()) if daily else 0

        # Max DD
        dd = _max_dd_from_list(pnls)

        return {
            "pnl": sum(pnls), "trades": n, "wr": wr,
            "avg_ror": avg_ror, "avg_pnl": avg_pnl, "avg_risk": avg_risk,
            "best_day": best_day, "worst_day": worst_day, "dd": dd,
        }

    mon_names = ["January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    all_years = sorted(trades_by_y.keys())

    sep = "-" * 140
    header = (f"  {'Period':<14} | {'P&L':>10} | {'Trades':>6} | {'WR':>6} | {'Avg RoR':>8} | "
              f"{'Avg P&L':>9} | {'Avg Risk':>9} | {'Best Day':>10} | {'Worst Day':>10} | {'Drawdown':>10}")

    logger.info(sep)
    logger.info("  YEARLY + MONTHLY BREAKDOWN")
    logger.info(sep)
    logger.info(header)
    logger.info(sep)

    for y in all_years:
        # Year summary row
        ys = _period_stats(trades_by_y[y])
        if ys:
            logger.info(
                f"  {y:<14} | ${ys['pnl']:>9,.0f} | {ys['trades']:>6} | {ys['wr']:>5.1f}% | "
                f"{ys['avg_ror']:>7.1f}% | ${ys['avg_pnl']:>8,.0f} | ${ys['avg_risk']:>8,.0f} | "
                f"${ys['best_day']:>9,.0f} | ${ys['worst_day']:>9,.0f} | ${ys['dd']:>9,.0f}"
            )

        # Month rows under the year
        for m in range(1, 13):
            ms = _period_stats(trades_by_ym.get((y, m), []))
            if ms is None:
                continue
            logger.info(
                f"    {mon_names[m-1]:<12} | ${ms['pnl']:>9,.0f} | {ms['trades']:>6} | {ms['wr']:>5.1f}% | "
                f"{ms['avg_ror']:>7.1f}% | ${ms['avg_pnl']:>8,.0f} | ${ms['avg_risk']:>8,.0f} | "
                f"${ms['best_day']:>9,.0f} | ${ms['worst_day']:>9,.0f} | ${ms['dd']:>9,.0f}"
            )

        logger.info(sep)

    # Grand total
    gs = _period_stats(all_trades)
    if gs:
        logger.info(
            f"  {'TOTAL':<14} | ${gs['pnl']:>9,.0f} | {gs['trades']:>6} | {gs['wr']:>5.1f}% | "
            f"{gs['avg_ror']:>7.1f}% | ${gs['avg_pnl']:>8,.0f} | ${gs['avg_risk']:>8,.0f} | "
            f"${gs['best_day']:>9,.0f} | ${gs['worst_day']:>9,.0f} | ${gs['dd']:>9,.0f}"
        )
    logger.info(sep)


# =============================================
#  MONTHLY RETURN: STRATEGY vs SPY
# =============================================
def print_monthly_vs_spy(all_trades: list) -> None:
    month_pnl = defaultdict(lambda: defaultdict(float))
    for t in all_trades:
        d = str(t["date"])
        y, m = int(d[:4]), int(d[4:6])
        month_pnl[y][m] += float(t["pnl_earned"])

    all_years = sorted(month_pnl.keys())
    spy_ret = _get_spy_monthly_returns(all_years[0], all_years[-1])
    if not spy_ret:
        logger.info("  (SPY monthly data not available — skipping strategy vs SPY)")
        return

    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    sep = "-" * 130
    logger.info(sep)
    logger.info("  MONTHLY RETURN: STRATEGY vs SPY  (strat% / SPY%)")
    logger.info(sep)

    for y in all_years:
        cells = []
        s_ann = sp_ann = 0.0
        for m in range(1, 13):
            spnl = month_pnl[y].get(m)
            spy_m = spy_ret.get((y, m))
            if spnl is None and spy_m is None:
                cells.append("--")
            else:
                s_pct = f"{spnl / _cfg.STARTING_EQUITY * 100:+.1f}%" if spnl is not None else "--"
                sp_pct = f"{spy_m:+.1f}%" if spy_m is not None else "--"
                cells.append(f"{s_pct}/{sp_pct}")
                if spnl is not None: s_ann += spnl / _cfg.STARTING_EQUITY * 100
                if spy_m is not None: sp_ann += spy_m
        logger.info(f"  {y}: " + " | ".join(cells) + f" || Strat={s_ann:+.1f}% SPY={sp_ann:+.1f}% Alpha={s_ann-sp_ann:+.1f}%")
    logger.info(sep)


# =============================================
#  WORST DAYS (30)
# =============================================
def print_worst_days(all_trades: list, n: int = 30) -> None:
    daily = defaultdict(float)
    daily_sides = defaultdict(lambda: {"call_pnl": 0, "put_pnl": 0, "call_n": 0, "put_n": 0})
    for t in all_trades:
        d = str(t["date"])
        daily[d] += float(t["pnl_earned"])
        side = t.get("side", "CALL")
        daily_sides[d][f"{side.lower()}_pnl"] += float(t["pnl_earned"])
        daily_sides[d][f"{side.lower()}_n"] += 1

    worst = sorted(daily.items(), key=lambda x: x[1])[:n]

    sep = "-" * 90
    logger.info(sep)
    logger.info(f"  WORST {n} DAYS")
    logger.info(sep)
    logger.info(f"  {'Date':<12} | {'Day P&L':>10} | {'VIX':>6} | {'Call P&L':>10} | {'Put P&L':>10} | {'Trades':>6}")
    logger.info(sep)

    for date_str, pnl in worst:
        ind = _DAILY_INDICATORS.get(date_str, {})
        vix = ind.get("vix_close", "")
        vix_str = f"{vix:.1f}" if isinstance(vix, (int, float)) and vix else "N/A"
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        ds = daily_sides[date_str]
        total_n = ds["call_n"] + ds["put_n"]
        logger.info(
            f"  {d:<12} | ${pnl:>9,.2f} | {vix_str:>6} | "
            f"${ds['call_pnl']:>9,.2f} | ${ds['put_pnl']:>9,.2f} | {total_n:>6}"
        )
    logger.info(sep)


# =============================================
#  SEASONALITY — DAY OF WEEK
# =============================================
def print_seasonality_dow(all_trades: list) -> None:
    dow_b = {i: {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []} for i in range(5)}
    for t in all_trades:
        d = str(t["date"])
        try:
            dt = _dt_mod.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        except ValueError:
            continue
        pnl = float(t["pnl_earned"])
        w = pnl > 0
        dow_b[dt.weekday()]["pnl"] += pnl
        dow_b[dt.weekday()]["pnls"].append(pnl)
        dow_b[dt.weekday()]["wins" if w else "losses"] += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    sep = "-" * 70
    logger.info(sep)
    logger.info("  SEASONALITY — DAY OF WEEK")
    logger.info(sep)
    logger.info(f"  {'Day':<5} | {'Trades':>7} | {'WR':>7} | {'Total P&L':>12} | {'Avg P&L':>10} | {'Max DD':>10}")
    logger.info(sep)

    for i, name in enumerate(dow_names):
        b = dow_b[i]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0
        avg = b["pnl"] / total if total else 0
        dd = _max_dd_from_list(b["pnls"]) if b["pnls"] else 0
        logger.info(f"  {name:<5} | {total:>7} | {wr:>6.1f}% | ${b['pnl']:>11,.2f} | ${avg:>9,.2f} | ${dd:>9,.2f}")
    logger.info(sep)


# =============================================
#  SEASONALITY — MONTH
# =============================================
def print_seasonality_month(all_trades: list) -> None:
    mon_b = {i: {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []} for i in range(1, 13)}
    for t in all_trades:
        d = str(t["date"])
        m = int(d[4:6])
        pnl = float(t["pnl_earned"])
        w = pnl > 0
        mon_b[m]["pnl"] += pnl
        mon_b[m]["pnls"].append(pnl)
        mon_b[m]["wins" if w else "losses"] += 1

    mon_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    sep = "-" * 70
    logger.info(sep)
    logger.info("  SEASONALITY — MONTH")
    logger.info(sep)
    logger.info(f"  {'Month':<5} | {'Trades':>7} | {'WR':>7} | {'Total P&L':>12} | {'Avg P&L':>10} | {'Max DD':>10}")
    logger.info(sep)

    for i, name in enumerate(mon_names, 1):
        b = mon_b[i]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0
        avg = b["pnl"] / total if total else 0
        dd = _max_dd_from_list(b["pnls"]) if b["pnls"] else 0
        logger.info(f"  {name:<5} | {total:>7} | {wr:>6.1f}% | ${b['pnl']:>11,.2f} | ${avg:>9,.2f} | ${dd:>9,.2f}")
    logger.info(sep)


# =============================================
#  CALENDAR / ECON EVENT ANALYSIS
# =============================================
def print_econ_event_analysis(all_trades: list) -> None:
    """Analyze performance on FOMC, Triple Witching, CPI, and month-end days."""
    event_sets = {
        "FOMC": FOMC_DATES,
        "Triple Witching": TRIPLE_WITCHING_DATES,
    }
    # Add CPI/NFP if defined
    if hasattr(_cfg, "ECON_DATES"):
        # Split into CPI and NFP by checking the sets in meds config pattern
        pass  # LastFive doesn't have separate CPI/NFP sets yet

    # End-of-month: last business day of each month
    eom_dates = set()
    for t in all_trades:
        d = str(t["date"])
        y, m = int(d[:4]), int(d[4:6])
        key = f"{y}{m:02d}"
        eom_dates.discard(key)  # we'll compute below

    # Compute EOM from trade dates
    trade_dates_by_month = defaultdict(list)
    for t in all_trades:
        d = str(t["date"])
        ym = d[:6]
        trade_dates_by_month[ym].append(d)
    eom_set = set()
    for ym, dates in trade_dates_by_month.items():
        eom_set.add(max(dates))
    event_sets["End of Month"] = eom_set

    # Monday / Friday
    event_sets["Monday"] = set()
    event_sets["Friday"] = set()
    for t in all_trades:
        d = str(t["date"])
        try:
            dt = _dt_mod.date(int(d[:4]), int(d[4:6]), int(d[6:]))
            if dt.weekday() == 0:
                event_sets["Monday"].add(d)
            elif dt.weekday() == 4:
                event_sets["Friday"].add(d)
        except ValueError:
            pass

    sep = "-" * 80
    logger.info(sep)
    logger.info("  CALENDAR / ECON EVENT ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Event':<18} | {'Trades':>7} | {'WR':>7} | {'Total P&L':>12} | {'Avg P&L':>10} | {'Max DD':>10}")
    logger.info(sep)

    for event_name, date_set in event_sets.items():
        bucket = [t for t in all_trades if str(t["date"]) in date_set]
        if not bucket:
            continue
        pnls = [float(t["pnl_earned"]) for t in bucket]
        cnt = len(pnls)
        w = sum(1 for p in pnls if p > 0)
        wr = w / cnt * 100
        dd = _max_dd_from_list(pnls)
        logger.info(
            f"  {event_name:<18} | {cnt:>7} | {wr:>6.1f}% | ${sum(pnls):>11,.2f} | ${np.mean(pnls):>9,.2f} | ${dd:>9,.2f}"
        )

    # Non-event (normal days)
    all_event_dates = set()
    for ds in event_sets.values():
        all_event_dates |= ds
    normal = [t for t in all_trades if str(t["date"]) not in all_event_dates]
    if normal:
        pnls_n = [float(t["pnl_earned"]) for t in normal]
        w_n = sum(1 for p in pnls_n if p > 0)
        dd_n = _max_dd_from_list(pnls_n)
        logger.info(
            f"  {'Normal days':<18} | {len(pnls_n):>7} | {w_n/len(pnls_n)*100:>6.1f}% | "
            f"${sum(pnls_n):>11,.2f} | ${np.mean(pnls_n):>9,.2f} | ${dd_n:>9,.2f}"
        )
    logger.info(sep)


# =============================================
#  RESULTS.md APPEND (comprehensive)
# =============================================
def append_results_md(all_trades: list, date_range: pd.DatetimeIndex) -> None:
    """Write full performance report to RESULTS.md, keeping only latest 3 runs (newest first)."""
    run_num = int(open(_RUN_NUMBER_FILE).read().strip()) if os.path.exists(_RUN_NUMBER_FILE) else 0
    pnls = [float(t["pnl_earned"]) for t in all_trades]
    total = sum(pnls)
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses_n = n - wins
    wr = wins / n * 100 if n else 0
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    avg_win = np.mean(win_pnls) if win_pnls else 0
    avg_loss = np.mean(loss_pnls) if loss_pnls else 0
    expectancy = (avg_win * wins / n) + (avg_loss * losses_n / n) if n else 0
    pf = abs(sum(win_pnls) / sum(loss_pnls)) if loss_pnls and sum(loss_pnls) != 0 else float("inf")
    gross_premium = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100 for t in all_trades)
    prem_capture = (total / gross_premium * 100) if gross_premium > 0 else 0

    eq = [_cfg.STARTING_EQUITY]
    for p in pnls:
        eq.append(eq[-1] + p)
    eq_arr = np.array(eq)
    peak_arr = np.maximum.accumulate(eq_arr)
    max_dd = float((eq_arr - peak_arr).min())
    max_dd_pct = (max_dd / max(peak_arr) * 100) if max(peak_arr) > 0 else 0

    # Daily P&L
    day_pnls = defaultdict(float)
    for t in all_trades:
        day_pnls[t["date"]] += float(t["pnl_earned"])
    days_traded = len(day_pnls)
    daily_vals = list(day_pnls.values())
    best_day = max(daily_vals) if daily_vals else 0
    worst_day = min(daily_vals) if daily_vals else 0

    # Sharpe / Sortino
    daily_returns = [day_pnls.get(d.strftime("%Y%m%d"), 0.0) for d in date_range]
    rf_daily = _cfg.STARTING_EQUITY * _cfg.ANNUAL_RF_RATE / 252
    excess = [r - rf_daily for r in daily_returns]
    sharpe = (np.mean(excess) / np.std(excess) * math.sqrt(252)) if np.std(excess) > 0 else 0
    mean_d = sum(daily_vals) / len(daily_vals) if daily_vals else 0
    down_d = [p for p in daily_vals if p < mean_d]
    down_var = sum((p - mean_d) ** 2 for p in down_d) / len(down_d) if down_d else 0
    sortino = ((mean_d - rf_daily) / math.sqrt(down_var) * math.sqrt(252)) if down_var > 0 else 0
    std_daily = np.std(daily_vals) if daily_vals else 0
    ann_vol = std_daily * math.sqrt(252)

    n_days = len(date_range)
    years = n_days / 252
    cagr = ((eq_arr[-1] / eq_arr[0]) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = cagr / abs(max_dd / _cfg.STARTING_EQUITY * 100) if max_dd != 0 else 0
    recovery = total / abs(max_dd) if max_dd != 0 else 0

    # Time underwater
    eq_d = peak_d = 0.0; uw_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]; peak_d = max(peak_d, eq_d)
        if eq_d < peak_d: uw_days += 1
    time_uw = uw_days / days_traded * 100 if days_traded else 0

    # Max consec losses
    max_consec = consec = 0
    for p in pnls:
        if p <= 0: consec += 1; max_consec = max(max_consec, consec)
        else: consec = 0

    # Side breakdown
    call_trades = [t for t in all_trades if t.get("side") == "CALL"]
    put_trades = [t for t in all_trades if t.get("side") == "PUT"]
    call_pnl = sum(float(t["pnl_earned"]) for t in call_trades)
    put_pnl = sum(float(t["pnl_earned"]) for t in put_trades)
    call_wr = sum(1 for t in call_trades if float(t["pnl_earned"]) > 0) / len(call_trades) * 100 if call_trades else 0
    put_wr = sum(1 for t in put_trades if float(t["pnl_earned"]) > 0) / len(put_trades) * 100 if put_trades else 0

    # Commission
    total_commission = sum(4 * float(t.get("qty", 1)) * _cfg.COMMISSION for t in all_trades)

    # VIX correlation
    vix_rows = []
    for label, lo, hi in VIX_BUCKETS:
        bucket = [t for t in all_trades if _sf(t.get("vix_level")) is not None
                  and (lo is None or _sf(t["vix_level"]) >= lo)
                  and (hi is None or _sf(t["vix_level"]) < hi)]
        cnt, bwr, avg_p, tot_p, dd = _bucket_stats(bucket)
        vix_rows.append((label, cnt, bwr, avg_p, tot_p, dd))

    # Monthly P&L
    month_pnl = defaultdict(lambda: defaultdict(float))
    month_trades_list = defaultdict(lambda: defaultdict(list))
    for t in all_trades:
        d = str(t["date"]); y, m = int(d[:4]), int(d[4:6])
        month_pnl[y][m] += float(t["pnl_earned"])
        month_trades_list[y][m].append(float(t["pnl_earned"]))
    all_years = sorted(month_pnl.keys())
    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    # Worst 30 days
    daily_sides = defaultdict(lambda: {"call_pnl": 0.0, "put_pnl": 0.0, "n": 0})
    for t in all_trades:
        d = str(t["date"]); side = t.get("side", "CALL").lower()
        daily_sides[d][f"{side}_pnl"] += float(t["pnl_earned"])
        daily_sides[d]["n"] += 1
    worst_30 = sorted(day_pnls.items(), key=lambda x: x[1])[:30]

    # Seasonality DOW
    dow_b = {i: {"pnl": 0.0, "w": 0, "l": 0} for i in range(5)}
    for t in all_trades:
        d = str(t["date"])
        try:
            dt = _dt_mod.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        except ValueError:
            continue
        p = float(t["pnl_earned"])
        dow_b[dt.weekday()]["pnl"] += p
        dow_b[dt.weekday()]["w" if p > 0 else "l"] += 1
    dow_names = ["Mon","Tue","Wed","Thu","Fri"]

    # Seasonality Month
    mon_b = {i: {"pnl": 0.0, "w": 0, "l": 0} for i in range(1, 13)}
    for t in all_trades:
        m = int(str(t["date"])[4:6]); p = float(t["pnl_earned"])
        mon_b[m]["pnl"] += p
        mon_b[m]["w" if p > 0 else "l"] += 1

    # Calendar events
    event_sets = {"FOMC": FOMC_DATES, "Triple Witching": TRIPLE_WITCHING_DATES}

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- Build full markdown ---
    L = [
        f"## Run #{run_num} — {ts}",
        f"**Period:** {_cfg.PILOT_YEAR_START} -> {_cfg.PILOT_YEAR_END}  |  **Days traded:** {days_traded}  |  **Total trades:** {n}",
        "",
        "### Returns",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total P&L (net) | ${total:,.0f} |",
        f"| Gross premium | ${gross_premium:,.0f} |",
        f"| Premium capture | {prem_capture:.1f}% |",
        f"| CAGR | {cagr:.1f}% |",
        f"| Avg win | ${avg_win:,.0f} |",
        f"| Avg loss | ${avg_loss:,.0f} |",
        f"| Expectancy/trade | ${expectancy:,.0f} |",
        f"| Profit factor | {pf:.2f}x |",
        "",
        "### Risk",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Max drawdown | ${max_dd:,.0f} ({max_dd_pct:.1f}%) |",
        f"| Calmar | {calmar:.2f} |",
        f"| Recovery factor | {recovery:.2f} |",
        f"| Time underwater | {time_uw:.1f}% ({uw_days}/{days_traded} days) |",
        f"| Max consec losses | {max_consec} |",
        "",
        "### Key Config",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| VIX adaptive | {'ON' if _cfg.ENABLE_VIX_ADAPTIVE else 'OFF'} (ATM<{_cfg.VIX_ATM_CUTOFF}, d{_cfg.DIST_MID:.0f}<{_cfg.VIX_MID_CUTOFF}, d{_cfg.DIST_WIDE:.0f}<{_cfg.VIX_WIDE_CUTOFF}) |",
        f"| Tiered sizing | {'ON' if _cfg.ENABLE_TIERED_SIZING else 'OFF'} (${_cfg.RISK_LOW:.0f}/${_cfg.RISK_MID:.0f}/${_cfg.RISK_HIGH:.0f}) |",
        f"| Width / Min credit | ${_cfg.SPREAD_WIDTH:.0f} / ${_cfg.MIN_NET_CREDIT:.2f} |",
        f"| Afternoon filter | < {_cfg.AFTERNOON_RETURN_MAX}% |",
        f"| Put filter | {'ON' if _cfg.ENABLE_PUT_MOMENTUM_FILTER else 'OFF'} (aftn<{_cfg.PUT_AFTERNOON_MIN}% + mom30<{_cfg.PUT_MOM30_MIN}%) |",
        f"| Commission | ${_cfg.COMMISSION}/leg |",
        "",
        "### Ratios & Volatility",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Win rate | {wr:.1f}% |",
        f"| Sharpe | {sharpe:.2f} |",
        f"| Sortino | {sortino:.2f} |",
        f"| Ann. volatility | ${ann_vol:,.0f} |",
        "",
        "### Day Stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Best day | ${best_day:,.0f} |",
        f"| Worst day | ${worst_day:,.0f} |",
        f"| Best trade | ${max(pnls):,.0f} |",
        f"| Worst trade | ${min(pnls):,.0f} |",
        f"| Commission total | ${total_commission:,.0f} |",
        "",
        "### VIX Correlation",
        "| VIX Range | Trades | WR | Avg P&L | Total P&L | Max DD |",
        "|-----------|-------:|---:|--------:|----------:|-------:|",
    ]
    for label, cnt, bwr, avg_p, tot_p, dd in vix_rows:
        if cnt > 0:
            L.append(f"| {label} | {cnt} | {bwr:.1f}% | ${avg_p:,.0f} | ${tot_p:,.0f} | ${dd:,.0f} |")

    L += [
        "",
        "### Put vs Call Split",
        "| Side | Trades | WR | Total P&L |",
        "|------|-------:|---:|----------:|",
        f"| CALL | {len(call_trades)} | {call_wr:.1f}% | ${call_pnl:,.0f} |",
        f"| PUT | {len(put_trades)} | {put_wr:.1f}% | ${put_pnl:,.0f} |",
    ]

    # Yearly + Monthly Breakdown (grouped)
    mon_names_full = ["January","February","March","April","May","June",
                      "July","August","September","October","November","December"]

    def _md_period_stats(trade_list):
        if not trade_list:
            return None
        pnls_p = [float(t["pnl_earned"]) for t in trade_list]
        n_p = len(pnls_p)
        wins_p = sum(1 for p in pnls_p if p > 0)
        wr_p = wins_p / n_p * 100
        avg_pnl_p = sum(pnls_p) / n_p
        risks_p = [(_cfg.SPREAD_WIDTH - float(t.get("credit_received", 0))) * float(t.get("qty", 1)) * 100 for t in trade_list]
        avg_risk_p = sum(risks_p) / n_p if n_p else 0
        avg_ror_p = (avg_pnl_p / avg_risk_p * 100) if avg_risk_p > 0 else 0
        daily_p = defaultdict(float)
        for t in trade_list:
            daily_p[str(t["date"])] += float(t["pnl_earned"])
        best_d = max(daily_p.values()) if daily_p else 0
        worst_d = min(daily_p.values()) if daily_p else 0
        dd_p = _max_dd_from_list(pnls_p)
        return {"pnl": sum(pnls_p), "trades": n_p, "wr": wr_p, "avg_ror": avg_ror_p,
                "avg_pnl": avg_pnl_p, "avg_risk": avg_risk_p,
                "best_day": best_d, "worst_day": worst_d, "dd": dd_p}

    trades_by_ym = defaultdict(list)
    trades_by_y = defaultdict(list)
    for t in all_trades:
        d = str(t["date"]).replace(".0", "")
        y_t, m_t = int(d[:4]), int(d[4:6])
        trades_by_ym[(y_t, m_t)].append(t)
        trades_by_y[y_t].append(t)

    L += ["", "### Yearly + Monthly Breakdown"]
    L.append("| Period | P&L | Trades | WR | Avg RoR | Avg P&L | Avg Risk | Best Day | Worst Day | Drawdown |")
    L.append("|--------|----:|-------:|---:|--------:|--------:|---------:|---------:|----------:|---------:|")

    for y in all_years:
        ys = _md_period_stats(trades_by_y.get(y, []))
        if ys:
            L.append(f"| **{y}** | ${ys['pnl']:,.0f} | {ys['trades']} | {ys['wr']:.1f}% | {ys['avg_ror']:.1f}% | ${ys['avg_pnl']:,.0f} | ${ys['avg_risk']:,.0f} | ${ys['best_day']:,.0f} | ${ys['worst_day']:,.0f} | ${ys['dd']:,.0f} |")
        for m_i in range(1, 13):
            ms = _md_period_stats(trades_by_ym.get((y, m_i), []))
            if ms:
                L.append(f"| &nbsp;&nbsp;{mon_names_full[m_i-1]} | ${ms['pnl']:,.0f} | {ms['trades']} | {ms['wr']:.1f}% | {ms['avg_ror']:.1f}% | ${ms['avg_pnl']:,.0f} | ${ms['avg_risk']:,.0f} | ${ms['best_day']:,.0f} | ${ms['worst_day']:,.0f} | ${ms['dd']:,.0f} |")

    # Worst 30 days
    L += ["", "### Largest Loss Days (worst 30)"]
    L.append("| Date | Day P&L | Call P&L | Put P&L | Trades |")
    L.append("|------|--------:|---------:|--------:|-------:|")
    for date_str, dpnl in worst_30:
        ds = daily_sides[date_str]
        d_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        L.append(f"| {d_fmt} | ${dpnl:,.0f} | ${ds['call_pnl']:,.0f} | ${ds['put_pnl']:,.0f} | {ds['n']} |")

    # Seasonality DOW
    L += ["", "### Seasonality — Day of Week"]
    L.append("| Day | Trades | WR | Total P&L | Avg P&L |")
    L.append("|-----|-------:|---:|----------:|--------:|")
    for i, name in enumerate(dow_names):
        b = dow_b[i]; tot = b["w"] + b["l"]
        wr2 = b["w"] / tot * 100 if tot else 0; avg2 = b["pnl"] / tot if tot else 0
        L.append(f"| {name} | {tot} | {wr2:.1f}% | ${b['pnl']:,.0f} | ${avg2:,.0f} |")

    # Seasonality Month
    L += ["", "### Seasonality — Month"]
    L.append("| Month | Trades | WR | Total P&L | Avg P&L |")
    L.append("|-------|-------:|---:|----------:|--------:|")
    for i, name in enumerate(mon_abbr, 1):
        b = mon_b[i]; tot = b["w"] + b["l"]
        wr2 = b["w"] / tot * 100 if tot else 0; avg2 = b["pnl"] / tot if tot else 0
        L.append(f"| {name} | {tot} | {wr2:.1f}% | ${b['pnl']:,.0f} | ${avg2:,.0f} |")

    # Calendar events
    L += ["", "### Calendar / Econ Event Analysis"]
    L.append("| Event | Trades | WR | Total P&L | Avg P&L |")
    L.append("|-------|-------:|---:|----------:|--------:|")
    for ename, eset in event_sets.items():
        bucket = [t for t in all_trades if str(t["date"]) in eset]
        if not bucket: continue
        bp = [float(t["pnl_earned"]) for t in bucket]
        ew = sum(1 for p in bp if p > 0)
        L.append(f"| {ename} | {len(bp)} | {ew/len(bp)*100:.1f}% | ${sum(bp):,.0f} | ${np.mean(bp):,.0f} |")

    new_entry = "\n".join(L)

    # --- Read existing RESULTS.md, parse runs, keep latest 2 + this new one = 3 ---
    results_path = os.path.join(os.path.dirname(__file__), "RESULTS.md")
    header = "# RESULTS — LastFive Strategy\n"

    existing_runs = []
    if os.path.exists(results_path):
        content = open(results_path).read()
        # Split on "## Run" markers
        import re
        parts = re.split(r'(?=^## Run )', content, flags=re.MULTILINE)
        for part in parts:
            if part.strip().startswith("## Run"):
                existing_runs.append(part.strip())

    # Keep latest 2 existing + new one on top = 3 total
    existing_runs = existing_runs[-2:]
    all_runs = [new_entry] + existing_runs

    with open(results_path, "w") as f:
        f.write(header + "\n")
        for run_text in all_runs:
            f.write("\n---\n\n" + run_text + "\n")

    logger.info(f"Results written to {results_path} (latest 3 runs, newest first)")


# =============================================
#  RUN HISTORY
# =============================================
def save_run_summary(all_trades: list) -> None:
    run_num = int(open(_RUN_NUMBER_FILE).read().strip()) if os.path.exists(_RUN_NUMBER_FILE) else 0
    pnls = [float(t["pnl_earned"]) for t in all_trades]
    total = sum(pnls)
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)

    summary = {
        "run_number": run_num,
        "timestamp": datetime.now().isoformat(),
        "trades": n,
        "total_pnl": round(total, 2),
        "win_rate": round(wins / n * 100, 1) if n else 0,
        "config": {
            "vix_adaptive": _cfg.ENABLE_VIX_ADAPTIVE,
            "vix_atm_cutoff": _cfg.VIX_ATM_CUTOFF,
            "tiered_sizing": _cfg.ENABLE_TIERED_SIZING,
            "spread_width": _cfg.SPREAD_WIDTH,
            "min_credit": _cfg.MIN_NET_CREDIT,
            "put_filter": _cfg.ENABLE_PUT_MOMENTUM_FILTER,
        },
    }

    history = []
    if os.path.exists(_cfg.RUN_HISTORY_FILE):
        try:
            with open(_cfg.RUN_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass

    history.append(summary)
    with open(_cfg.RUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def display_run_history() -> None:
    if not os.path.exists(_cfg.RUN_HISTORY_FILE):
        return
    try:
        with open(_cfg.RUN_HISTORY_FILE) as f:
            history = json.load(f)
        if not history:
            return
        logger.info("=" * 60)
        logger.info("  RECENT RUN HISTORY")
        logger.info("-" * 60)
        for run in history[-5:]:
            ts = run.get("timestamp", "?")[:16]
            pnl = run.get("total_pnl", 0)
            wr = run.get("win_rate", 0)
            trades = run.get("trades", 0)
            rn = run.get("run_number", "?")
            logger.info(f"  #{rn} {ts} | P&L=${pnl:>10,.0f} | WR={wr:.1f}% | {trades} trades")
        logger.info("=" * 60)
    except Exception:
        pass
