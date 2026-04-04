#!/usr/bin/env python3
"""Research: Dual Entry (15:55 + 15:57) — does adding a second entry improve P&L?

Hypothesis: SPX moves differently in those 2 minutes, so 15:55 and 15:57 entries
get different strikes and semi-independent outcomes. Dual entry diversifies.

Configs tested (all with Rule C3, bidirectional, width $5, min credit $0.10,
afternoon<1.0%, VIX<=26, commission $0.50/leg):

1. Single 15:55 — entry at 15:55 only, risk $2500/side
2. Single 15:57 — entry at 15:57 only, risk $2500/side
3. Dual 15:55+15:57 — both times, risk $1250/side each (same total risk budget)
4. Dual full risk — both times, risk $2500/side each (double total risk)

Rule C3: VIX<18 -> dist=0, 18-22 -> dist=3, 22-26 -> dist=5.
"""
from __future__ import annotations

import glob
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
STRIKE_STEP = 5
STARTING_EQUITY = 50_000.0
ANNUAL_RF = 0.05
COMMISSION = 0.50  # per leg per contract

MARKET_HOLIDAYS = {
    "20220117","20220221","20220415","20220530","20220620","20220704",
    "20220905","20221124","20221226",
    "20230102","20230116","20230220","20230407","20230529","20230619",
    "20230704","20230904","20231123","20231225",
    "20240101","20240115","20240219","20240329","20240527","20240619",
    "20240704","20240902","20241128","20241225",
    "20250101","20250120","20250217","20250418","20250526","20250704",
    "20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260703",
    "20260907","20261126","20261225",
}

EARLY_CLOSE_DAYS = {
    "20221125","20231124","20241129","20241224",
    "20251128","20251224","20261127","20261224",
}


# ── Data loaders ────────────────────────────────────────────────────────
def load_spx(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def load_vix(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "vix_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def load_options(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    return df


# ── Helpers ─────────────────────────────────────────────────────────────
def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return abs(end - start) / start * 100


def get_quote(quotes_df: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    mask = (
        (quotes_df["right"] == right) &
        (quotes_df["strike"] == strike) &
        (quotes_df["time_str"] == time_str)
    )
    rows = quotes_df[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


def rule_c3(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float, risk_amount: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    """Simulate a call credit spread."""
    target = spot + dist
    short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike + int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "C"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted(s for s in available if s >= spot + dist)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "C", short_strike, entry_time)
    long_q = get_quote(quotes_df, "C", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk_amount / max_loss_per)) if max_loss_per > 0 else 1

    if spx_exit <= short_strike:
        settlement = 0.0
    elif spx_exit >= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = spx_exit - short_strike

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "CALL", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "entry_time": entry_time}


def sim_put(spot: float, spx_exit: float, dist: float, risk_amount: float,
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    """Simulate a put credit spread."""
    target = spot - dist
    short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike - int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "P"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike - int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "P", short_strike, entry_time)
    long_q = get_quote(quotes_df, "P", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk_amount / max_loss_per)) if max_loss_per > 0 else 1

    if spx_exit >= short_strike:
        settlement = 0.0
    elif spx_exit <= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "PUT", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "entry_time": entry_time}


# ── Run one config ─────────────────────────────────────────────────────
def run_config(day_data: list, entry_times: list[str], risk_per_side: float,
               label: str) -> dict:
    """Run a config with one or more entry times.

    entry_times: list of time strings, e.g. ["15:55:00"] or ["15:55:00","15:57:00"]
    risk_per_side: risk budget per side per entry time
    """
    all_trades = []
    day_pnls = []

    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        day_pnl = 0.0
        day_traded = False

        for etime in entry_times:
            spot = dd.get(f"spot_{etime}")
            if spot is None:
                continue

            # Call side
            ct = sim_call(spot, dd["spx_exit"], dist, risk_per_side,
                          dd["opts_df"], etime)
            if ct is not None:
                all_trades.append(ct)
                day_pnl += ct["pnl"]
                day_traded = True

            # Put side
            pt = sim_put(spot, dd["spx_exit"], dist, risk_per_side,
                         dd["opts_df"], etime)
            if pt is not None:
                all_trades.append(pt)
                day_pnl += pt["pnl"]
                day_traded = True

        if day_traded:
            day_pnls.append(day_pnl)

    if not all_trades:
        return {"label": label, "trades": 0}

    day_pnl_arr = np.array(day_pnls)
    total_pnl = day_pnl_arr.sum()

    # Max drawdown on daily equity curve
    equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd_arr = equity - peak
    max_dd = dd_arr.min()

    # Trade stats
    n_trades = len(all_trades)
    n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
    wr = n_wins / n_trades * 100

    # Per-side stats
    call_trades = [t for t in all_trades if t["side"] == "CALL"]
    put_trades = [t for t in all_trades if t["side"] == "PUT"]
    n_calls = len(call_trades)
    n_puts = len(put_trades)

    # Per entry-time stats
    t55 = [t for t in all_trades if t["entry_time"] == "15:55:00"]
    t57 = [t for t in all_trades if t["entry_time"] == "15:57:00"]

    # Sharpe (daily)
    daily_rf = ANNUAL_RF / 252
    excess = day_pnl_arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / day_pnl_arr.std() * np.sqrt(252) if day_pnl_arr.std() > 0 else 0

    # Calmar
    n_days = len(day_pnl_arr)
    years = n_days / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    # Avg credit
    avg_credit = np.mean([t["credit"] for t in all_trades])

    # Profit factor
    gross_wins = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gross_losses = abs(sum(t["pnl"] for t in all_trades if t["pnl"] <= 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Strike independence check — how many days do 15:55 and 15:57 get different strikes?
    diff_strike_days = 0
    same_strike_days = 0
    if len(entry_times) > 1:
        for dd in day_data:
            s55 = dd.get("spot_15:55:00")
            s57 = dd.get("spot_15:57:00")
            if s55 is not None and s57 is not None:
                dist = rule_c3(dd["vix1550"])
                cs55 = int(math.ceil((s55 + dist) / STRIKE_STEP) * STRIKE_STEP)
                cs57 = int(math.ceil((s57 + dist) / STRIKE_STEP) * STRIKE_STEP)
                if cs55 != cs57:
                    diff_strike_days += 1
                else:
                    same_strike_days += 1

    return {
        "label": label,
        "trades": n_trades,
        "n_calls": n_calls,
        "n_puts": n_puts,
        "n_t55": len(t55),
        "n_t57": len(t57),
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "sharpe": sharpe,
        "calmar": calmar,
        "avg_credit": avg_credit,
        "pf": pf,
        "n_days": n_days,
        "diff_strike_days": diff_strike_days,
        "same_strike_days": same_strike_days,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("DUAL ENTRY RESEARCH — 15:55 + 15:57 (Rule C3 VIX-Adaptive, Bidirectional)")
    print("=" * 110)

    # Collect all dates
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    # Pre-collect day data
    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_filter": 0, "afternoon_filter": 0}

    for i, ds in enumerate(all_dates):
        if i % 200 == 0:
            print(f"  Processing {i}/{len(all_dates)}...")

        if ds in MARKET_HOLIDAYS:
            skipped["holiday"] += 1
            continue
        if ds in EARLY_CLOSE_DAYS:
            skipped["early_close"] += 1
            continue

        spx_df = load_spx(ds)
        if spx_df is None:
            skipped["no_spx"] += 1
            continue

        vix_df = load_vix(ds)
        if vix_df is None:
            skipped["no_vix"] += 1
            continue

        vix1550 = get_price_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0:
            skipped["no_vix"] += 1
            continue

        # VIX > 26 filter
        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        # Afternoon return filter < 1.0%
        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # Spot at both entry times
        spot_1555 = get_price_at(spx_df, "15:55")
        spot_1557 = get_price_at(spx_df, "15:57")

        # Need at least one entry spot
        if spot_1555 is None and spot_1557 is None:
            skipped["no_spx"] += 1
            continue

        # SPX at 16:00
        exit_row = spx_df[spx_df["hhmm"] == "16:00"]
        if exit_row.empty:
            exit_row = spx_df[spx_df["hhmm"] == "15:59"]
        if exit_row.empty:
            spx_exit = float(spx_df["close"].iloc[-1])
        else:
            spx_exit = float(exit_row["close"].iloc[0])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        day_data.append({
            "date": ds,
            "spot_15:55:00": spot_1555,
            "spot_15:57:00": spot_1557,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # Check how many days have both spots available
    both_spots = sum(1 for dd in day_data
                     if dd["spot_15:55:00"] is not None and dd["spot_15:57:00"] is not None)
    only_55 = sum(1 for dd in day_data
                  if dd["spot_15:55:00"] is not None and dd["spot_15:57:00"] is None)
    only_57 = sum(1 for dd in day_data
                  if dd["spot_15:55:00"] is None and dd["spot_15:57:00"] is not None)
    print(f"  Days with both 15:55+15:57 bars: {both_spots}")
    print(f"  Days with only 15:55: {only_55}")
    print(f"  Days with only 15:57: {only_57}")

    # SPX movement between 15:55 and 15:57
    diffs = []
    for dd in day_data:
        s55 = dd["spot_15:55:00"]
        s57 = dd["spot_15:57:00"]
        if s55 is not None and s57 is not None:
            diffs.append(s57 - s55)
    diffs_arr = np.array(diffs) if diffs else np.array([0])
    print(f"\n  SPX change 15:55 -> 15:57 across {len(diffs)} days:")
    print(f"    Mean: {diffs_arr.mean():+.2f}  Median: {np.median(diffs_arr):+.2f}")
    print(f"    Std:  {diffs_arr.std():.2f}   Min: {diffs_arr.min():+.2f}  Max: {diffs_arr.max():+.2f}")
    pct_moved = sum(1 for d in diffs if abs(d) >= 5) / len(diffs) * 100 if diffs else 0
    print(f"    Days |move| >= $5: {sum(1 for d in diffs if abs(d) >= 5)} ({pct_moved:.1f}%)")

    # ── Run configs ────────────────────────────────────────────────────
    configs = [
        ("1. Single 15:55",         ["15:55:00"],               2500.0),
        ("2. Single 15:57",         ["15:57:00"],               2500.0),
        ("3. Dual half-risk",       ["15:55:00", "15:57:00"],   1250.0),
        ("4. Dual full-risk",       ["15:55:00", "15:57:00"],   2500.0),
    ]

    results = []
    for label, etimes, risk in configs:
        print(f"\nRunning: {label} (entry={','.join(t[:5] for t in etimes)}, risk=${risk:.0f}/side)...")
        r = run_config(day_data, etimes, risk, label)
        results.append(r)

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 130)
    print("COMPARISON TABLE — Dual Entry Research (Rule C3, Bidirectional)")
    print("=" * 130)
    print(f"{'Config':<24} {'Trades':>7} {'C/P':>9} {'P&L':>12} {'Max DD':>10} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'PF':>6} {'Avg Cr':>7} {'Days':>5}")
    print("-" * 130)

    for r in results:
        if r["trades"] == 0:
            print(f"{r['label']:<24}  NO TRADES")
            continue
        print(
            f"{r['label']:<24} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>5.1f}% "
            f"{r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} "
            f"${r['avg_credit']:>5.2f} {r['n_days']:>5}"
        )
    print("=" * 130)

    # ── Trade count by entry time ──────────────────────────────────────
    print("\nTrade count by entry time:")
    print("-" * 70)
    for r in results:
        if r["trades"] == 0:
            continue
        t55 = r.get("n_t55", 0)
        t57 = r.get("n_t57", 0)
        print(f"  {r['label']:<24} | 15:55={t55:>4}  15:57={t57:>4}  total={r['trades']:>4}")

    # ── Strike independence ────────────────────────────────────────────
    print("\nStrike independence (dual configs only):")
    print("-" * 70)
    for r in results:
        ds = r.get("diff_strike_days", 0)
        ss = r.get("same_strike_days", 0)
        total = ds + ss
        if total > 0:
            pct = ds / total * 100
            print(f"  {r['label']:<24} | Different strikes: {ds}/{total} days ({pct:.1f}%)")

    # ── Delta vs Single 15:55 ──────────────────────────────────────────
    base = results[0]
    if base["trades"] > 0:
        print("\nDelta vs Single 15:55 baseline:")
        print("-" * 100)
        print(f"{'Config':<24} {'dP&L':>12} {'dMax DD':>10} {'dSharpe':>8} {'dCalmar':>9} {'dTrades':>8}")
        print("-" * 100)
        for r in results[1:]:
            if r["trades"] == 0:
                continue
            dpnl = r["total_pnl"] - base["total_pnl"]
            ddd = r["max_dd"] - base["max_dd"]
            dsh = r["sharpe"] - base["sharpe"]
            dcal = r["calmar"] - base["calmar"]
            dtr = r["trades"] - base["trades"]
            print(
                f"  {r['label']:<22} "
                f"${dpnl:>+10,.0f} ${ddd:>+8,.0f} "
                f"{dsh:>+8.2f} {dcal:>+9.2f} {dtr:>+8}"
            )

    # ── Year-by-year P&L ──────────────────────────────────────────────
    print("\nYear-by-year P&L:")
    years = sorted(set(dd["date"][:4] for dd in day_data))
    print("-" * (30 + 12 * len(years)))
    header = f"{'Config':<28}"
    for yr in years:
        header += f" {yr:>10}"
    print(header)
    print("-" * (30 + 12 * len(years)))

    for label, etimes, risk in configs:
        yr_pnl = {yr: 0.0 for yr in years}
        for dd in day_data:
            dist = rule_c3(dd["vix1550"])
            for etime in etimes:
                spot = dd.get(f"spot_{etime}")
                if spot is None:
                    continue
                ct = sim_call(spot, dd["spx_exit"], dist, risk, dd["opts_df"], etime)
                if ct:
                    yr_pnl[dd["date"][:4]] += ct["pnl"]
                pt = sim_put(spot, dd["spx_exit"], dist, risk, dd["opts_df"], etime)
                if pt:
                    yr_pnl[dd["date"][:4]] += pt["pnl"]

        row = f"{label:<28}"
        for yr in years:
            row += f" ${yr_pnl[yr]:>8,.0f}"
        print(row)
    print("-" * (30 + 12 * len(years)))

    # ── Correlation of 15:55 vs 15:57 daily P&L ──────────────────────
    print("\nCorrelation of 15:55 vs 15:57 daily P&L:")
    pnl_55_by_date = {}
    pnl_57_by_date = {}
    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        ds = dd["date"]

        s55 = dd.get("spot_15:55:00")
        if s55 is not None:
            day_pnl = 0.0
            ct = sim_call(s55, dd["spx_exit"], dist, 2500.0, dd["opts_df"], "15:55:00")
            if ct:
                day_pnl += ct["pnl"]
            pt = sim_put(s55, dd["spx_exit"], dist, 2500.0, dd["opts_df"], "15:55:00")
            if pt:
                day_pnl += pt["pnl"]
            if ct or pt:
                pnl_55_by_date[ds] = day_pnl

        s57 = dd.get("spot_15:57:00")
        if s57 is not None:
            day_pnl = 0.0
            ct = sim_call(s57, dd["spx_exit"], dist, 2500.0, dd["opts_df"], "15:57:00")
            if ct:
                day_pnl += ct["pnl"]
            pt = sim_put(s57, dd["spx_exit"], dist, 2500.0, dd["opts_df"], "15:57:00")
            if pt:
                day_pnl += pt["pnl"]
            if ct or pt:
                pnl_57_by_date[ds] = day_pnl

    common_dates = sorted(set(pnl_55_by_date.keys()) & set(pnl_57_by_date.keys()))
    if len(common_dates) > 10:
        arr_55 = np.array([pnl_55_by_date[d] for d in common_dates])
        arr_57 = np.array([pnl_57_by_date[d] for d in common_dates])
        corr = np.corrcoef(arr_55, arr_57)[0, 1]
        print(f"  Common trading days: {len(common_dates)}")
        print(f"  Correlation: {corr:.4f}")

        # How often do they disagree (one wins, other loses)?
        both_win = sum(1 for a, b in zip(arr_55, arr_57) if a > 0 and b > 0)
        both_lose = sum(1 for a, b in zip(arr_55, arr_57) if a <= 0 and b <= 0)
        only_55_wins = sum(1 for a, b in zip(arr_55, arr_57) if a > 0 and b <= 0)
        only_57_wins = sum(1 for a, b in zip(arr_55, arr_57) if a <= 0 and b > 0)
        n = len(common_dates)
        print(f"  Both win:  {both_win:>4} ({both_win/n*100:.1f}%)")
        print(f"  Both lose: {both_lose:>4} ({both_lose/n*100:.1f}%)")
        print(f"  Only 15:55 wins: {only_55_wins:>4} ({only_55_wins/n*100:.1f}%)")
        print(f"  Only 15:57 wins: {only_57_wins:>4} ({only_57_wins/n*100:.1f}%)")

    print("\nDone.")


if __name__ == "__main__":
    main()
