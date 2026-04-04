#!/usr/bin/env python3
"""
DOUBLE ENTRY RESEARCH — 15:53 entry on HIGH-CONVICTION days only
=================================================================
Base config: FINAL (C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00,
             width $5, min credit $0.10, tiered sizing, stacked put filter)

High-conviction conditions tested:
  - VIX(15:50) < 15  (very calm)
  - Afternoon return between -0.3% and +0.3%  (flat)
  - Day range < 1.0%  (low volatility day)

Configs:
  A: Single 15:55 only (baseline)
  B: Double on VIX<15 days — add 15:53 at 50% size
  C: Double on flat afternoon — add 15:53 at 50% size
  D: Double on VIX<15 AND flat afternoon — add 15:53 at 75% size
  E: Double on VIX<15 AND flat AND narrow range — add 15:53 at full size

Both entries settle independently at 16:00.
"""

import math
import os
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE = "/Users/jeerapongwongchote/Documents/thetadata"
DATA = os.path.join(BASE, "data")

HOLIDAYS = {
    "20220117","20220221","20220415","20220530","20220620","20220704",
    "20220905","20221124","20221226","20230102","20230116","20230220",
    "20230407","20230529","20230619","20230704","20230904","20231123",
    "20231225","20240101","20240115","20240219","20240329","20240527",
    "20240619","20240704","20240902","20241128","20241225","20250101",
    "20250120","20250217","20250418","20250526","20250704","20250901",
    "20251127","20251225","20260101","20260119","20260216","20260403",
    "20260525","20260703","20260907","20261126","20261225",
}
EARLY_CLOSE = {
    "20221125","20231124","20241129","20241224","20251128","20251224",
    "20261127","20261224",
}

START_DATE = "2022-01-03"
END_DATE = "2026-03-25"

COMMISSION_PER_LEG = 0.50
WIDTH = 5
MIN_CREDIT = 0.10
STARTING_EQUITY = 50_000
STRIKE_STEP = 5


# ─── helpers ───────────────────────────────────────────────────────────────────

def trading_dates(start, end):
    dates = []
    d = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    while d <= e:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in HOLIDAYS and ds not in EARLY_CLOSE:
            dates.append(ds)
        d += timedelta(days=1)
    return dates


def load_parquet(year, subdir, date_str):
    path = os.path.join(DATA, str(year), subdir, f"{date_str}.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)
    return None


def get_bar(df, hhmm):
    mask = df["timestamp"].str.contains(f"T{hhmm}:")
    rows = df[mask]
    if len(rows) == 0:
        return None
    return rows.iloc[0]


def get_spot_at(spx_df, hhmm):
    bar = get_bar(spx_df, hhmm)
    if bar is None:
        return None
    v = float(bar["close"])
    return v if v > 0 else None


def get_vix_at(vix_df, hhmm):
    bar = get_bar(vix_df, hhmm)
    if bar is None:
        return None
    v = float(bar["close"])
    return v if v > 0 else None


def compute_afternoon_return(spx_df):
    """SPX return from 13:00 to 15:54 (signed, not absolute)."""
    hhmm_series = spx_df["timestamp"].str[11:16]
    afternoon = spx_df[(hhmm_series >= "13:00") & (hhmm_series <= "15:54")]
    if len(afternoon) < 2:
        return None
    first_close = float(afternoon.iloc[0]["close"])
    last_close = float(afternoon.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def compute_mom_30(spx_df):
    """SPX 30-min momentum (15:25 to 15:54)."""
    hhmm_series = spx_df["timestamp"].str[11:16]
    bars = spx_df[(hhmm_series >= "15:25") & (hhmm_series <= "15:54")]
    if len(bars) < 2:
        return None
    first_close = float(bars.iloc[0]["close"])
    last_close = float(bars.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def compute_day_range_pct(spx_df):
    """Intraday range as % of open."""
    valid = spx_df[(spx_df["high"] > 0) & (spx_df["close"] > 0)]
    if len(valid) < 2:
        return None
    day_high = float(valid["high"].max())
    day_low = float(valid["low"].min())
    day_open = float(valid.iloc[0]["open"])
    if day_open == 0 or day_low == 0:
        return None
    return (day_high - day_low) / day_open * 100


def get_quote(quotes_df, right, strike, hhmm):
    mask = (quotes_df["right"] == right) & \
           (quotes_df["strike"] == strike) & \
           (quotes_df["timestamp"].str.contains(f"T{hhmm}:"))
    rows = quotes_df[mask]
    if len(rows) == 0:
        return None
    row = rows.iloc[0]
    return {"bid": float(row["bid"]), "ask": float(row["ask"])}


def get_distance(vix):
    """Rule C3: VIX<18 -> 0, 18-22 -> 3, 22-26 -> 5, >26 -> None (skip)."""
    if vix is None:
        return None
    if vix < 18:
        return 0
    elif vix < 22:
        return 3
    elif vix <= 26:
        return 5
    else:
        return None


def get_tiered_risk(credit):
    """Tiered sizing: cr<0.20 -> $1500, 0.20-0.50 -> $2500, >0.50 -> $3500."""
    if credit < 0.20:
        return 1500
    elif credit <= 0.50:
        return 2500
    else:
        return 3500


def sim_spread(quotes_df, spx_exit, right, short_strike, long_strike, hhmm, risk_amount):
    """Simulate one credit spread. Returns trade dict or None."""
    short_q = get_quote(quotes_df, right, short_strike, hhmm)
    long_q = get_quote(quotes_df, right, long_strike, hhmm)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    # Tiered sizing with risk_amount scaling
    base_risk = get_tiered_risk(credit)
    scaled_risk = base_risk * risk_amount  # risk_amount is a multiplier (1.0 = full)
    max_loss_per = (WIDTH - credit) * 100
    if max_loss_per > 0:
        num_contracts = max(1, int(scaled_risk / max_loss_per))
    else:
        num_contracts = 1

    # Settlement
    if right == "C":
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = WIDTH
        else:
            settlement = spx_exit - short_strike
    else:  # PUT
        if spx_exit >= short_strike:
            settlement = 0.0
        elif spx_exit <= long_strike:
            settlement = WIDTH
        else:
            settlement = short_strike - spx_exit

    pnl = (credit - settlement) * 100 * num_contracts - 2 * COMMISSION_PER_LEG * num_contracts
    return {
        "credit": credit,
        "pnl": pnl,
        "contracts": num_contracts,
        "short_strike": short_strike,
        "long_strike": long_strike,
        "side": right,
        "entry_hhmm": hhmm,
        "win": 1 if pnl > 0 else 0,
    }


def sim_entry(spx_df, quotes_df, spx_exit, vix_1550, afternoon_ret, mom_30,
              entry_hhmm, risk_multiplier):
    """Simulate both call and put sides at a given entry time.

    risk_multiplier: 1.0 = full tiered size, 0.5 = half, 0.75 = three-quarter, etc.

    Returns list of trade dicts.
    """
    spot = get_spot_at(spx_df, entry_hhmm)
    if spot is None:
        return []

    dist = get_distance(vix_1550)
    if dist is None:
        return []

    trades = []

    # ── CALL SIDE ──
    # Call filter: skip if afternoon >= 1.0%
    skip_call = afternoon_ret is not None and afternoon_ret >= 1.0
    if not skip_call:
        call_short = math.ceil((spot + dist) / STRIKE_STEP) * STRIKE_STEP
        call_long = call_short + WIDTH

        # Check available strikes
        avail = set(quotes_df[quotes_df["right"] == "C"]["strike"].unique())
        if call_short not in avail or call_long not in avail:
            candidates = sorted(s for s in avail if s >= spot + dist)
            if candidates:
                call_short = int(candidates[0])
                call_long = call_short + WIDTH

        if call_short in avail and call_long in avail:
            t = sim_spread(quotes_df, spx_exit, "C", call_short, call_long,
                           entry_hhmm, risk_multiplier)
            if t is not None:
                trades.append(t)

    # ── PUT SIDE ──
    # Put filter: skip if afternoon < -0.75% AND mom30 < -0.10%
    skip_put = False
    if afternoon_ret is not None and mom_30 is not None:
        if afternoon_ret < -0.75 and mom_30 < -0.10:
            skip_put = True

    if not skip_put:
        put_short = math.floor((spot - dist) / STRIKE_STEP) * STRIKE_STEP
        put_long = put_short - WIDTH

        avail = set(quotes_df[quotes_df["right"] == "P"]["strike"].unique())
        if put_short not in avail or put_long not in avail:
            candidates = sorted((s for s in avail if s <= spot - dist), reverse=True)
            if candidates:
                put_short = int(candidates[0])
                put_long = put_short - WIDTH

        if put_short in avail and put_long in avail:
            t = sim_spread(quotes_df, spx_exit, "P", put_short, put_long,
                           entry_hhmm, risk_multiplier)
            if t is not None:
                trades.append(t)

    return trades


# ─── main backtest for a config ──────────────────────────────────────────────

def run_config(day_data_list, config_name, should_double_fn, extra_risk_mult):
    """
    Run a config over pre-loaded day data.

    should_double_fn: function(day_info) -> bool — whether to add the 15:53 entry
    extra_risk_mult: risk multiplier for the 15:53 entry (0.5, 0.75, 1.0)

    Always enters at 15:55 at full risk (1.0).
    """
    all_trades = []
    day_pnls = []

    n_double_days = 0
    trades_1555 = []
    trades_1553 = []

    for day in day_data_list:
        day_trades = []

        # Always enter at 15:55 (full risk)
        t55 = sim_entry(
            day["spx_df"], day["quotes_df"], day["spx_exit"],
            day["vix_1550"], day["afternoon_ret"], day["mom_30"],
            "15:55", 1.0
        )
        day_trades.extend(t55)
        trades_1555.extend(t55)

        # Conditionally enter at 15:53
        if should_double_fn(day):
            n_double_days += 1
            t53 = sim_entry(
                day["spx_df"], day["quotes_df"], day["spx_exit"],
                day["vix_1550"], day["afternoon_ret"], day["mom_30"],
                "15:53", extra_risk_mult
            )
            day_trades.extend(t53)
            trades_1553.extend(t53)

        if day_trades:
            day_pnl = sum(t["pnl"] for t in day_trades)
            day_pnls.append(day_pnl)
            all_trades.extend(day_trades)

    if not all_trades:
        return {"label": config_name, "trades": 0}

    day_pnl_arr = np.array(day_pnls)
    total_pnl = day_pnl_arr.sum()

    # Max drawdown on equity curve
    equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd_arr = equity - peak
    max_dd = dd_arr.min()
    max_dd_pct = abs(max_dd) / np.maximum.accumulate(equity + abs(dd_arr)).max() * 100 if max_dd < 0 else 0

    # Recalculate max_dd_pct properly
    max_dd_pct = abs(max_dd) / peak[np.argmin(dd_arr)] * 100 if max_dd < 0 else 0

    n_trades = len(all_trades)
    n_wins = sum(t["win"] for t in all_trades)
    wr = n_wins / n_trades * 100

    n_calls = sum(1 for t in all_trades if t["side"] == "C")
    n_puts = sum(1 for t in all_trades if t["side"] == "P")

    # Sharpe (daily)
    sharpe = day_pnl_arr.mean() / day_pnl_arr.std() * np.sqrt(252) if day_pnl_arr.std() > 0 else 0

    # Profit factor
    gross_wins = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gross_losses = abs(sum(t["pnl"] for t in all_trades if t["pnl"] <= 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Avg credit
    avg_credit = np.mean([t["credit"] for t in all_trades])

    # CAGR
    final_eq = equity[-1]
    n_years = len(day_pnl_arr) / 252
    cagr = ((final_eq / STARTING_EQUITY) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    # 15:53 stats
    n_t53 = len(trades_1553)
    pnl_t53 = sum(t["pnl"] for t in trades_1553) if trades_1553 else 0
    wr_t53 = sum(t["win"] for t in trades_1553) / n_t53 * 100 if n_t53 > 0 else 0

    n_t55 = len(trades_1555)
    pnl_t55 = sum(t["pnl"] for t in trades_1555) if trades_1555 else 0
    wr_t55 = sum(t["win"] for t in trades_1555) / n_t55 * 100 if n_t55 > 0 else 0

    return {
        "label": config_name,
        "trades": n_trades,
        "n_calls": n_calls,
        "n_puts": n_puts,
        "pnl": total_pnl,
        "max_dd": max_dd,
        "max_dd_pct": max_dd_pct,
        "wr": wr,
        "sharpe": sharpe,
        "pf": pf,
        "avg_credit": avg_credit,
        "cagr": cagr,
        "n_days": len(day_pnl_arr),
        "final_eq": final_eq,
        "n_double_days": n_double_days,
        "n_t55": n_t55,
        "pnl_t55": pnl_t55,
        "wr_t55": wr_t55,
        "n_t53": n_t53,
        "pnl_t53": pnl_t53,
        "wr_t53": wr_t53,
    }


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 120)
    print("DOUBLE ENTRY RESEARCH — 15:53 on HIGH-CONVICTION days (Rule C3, FINAL config)")
    print("=" * 120)

    all_dates = trading_dates(START_DATE, END_DATE)
    print(f"Total candidate dates: {len(all_dates)}")

    # ── Pre-load all day data ──
    print("\nLoading day data...")
    day_data_list = []
    skip_counts = {"no_spx": 0, "no_vix": 0, "no_opts": 0, "vix_skip": 0}

    for i, ds in enumerate(all_dates):
        if i % 200 == 0:
            print(f"  {i}/{len(all_dates)}...")

        year = ds[:4]
        spx_df = load_parquet(year, "spx_ohlc", ds)
        if spx_df is None:
            skip_counts["no_spx"] += 1
            continue

        vix_df = load_parquet(year, "vix_ohlc", ds)
        if vix_df is None:
            skip_counts["no_vix"] += 1
            continue

        quotes_df = load_parquet(year, "option_quotes", ds)
        if quotes_df is None:
            skip_counts["no_opts"] += 1
            continue

        vix_1550 = get_vix_at(vix_df, "15:50")
        if vix_1550 is None:
            skip_counts["no_vix"] += 1
            continue

        # VIX > 26 skip (part of baseline)
        if vix_1550 > 26:
            skip_counts["vix_skip"] += 1
            continue

        # Compute features
        afternoon_ret = compute_afternoon_return(spx_df)
        mom_30 = compute_mom_30(spx_df)
        day_range = compute_day_range_pct(spx_df)

        # SPX exit at 16:00
        spx_exit = get_spot_at(spx_df, "16:00")
        if spx_exit is None:
            spx_exit = get_spot_at(spx_df, "15:59")
        if spx_exit is None:
            # Last bar
            valid = spx_df[spx_df["close"] > 0]
            if len(valid) == 0:
                continue
            spx_exit = float(valid.iloc[-1]["close"])

        day_data_list.append({
            "date": ds,
            "spx_df": spx_df,
            "quotes_df": quotes_df,
            "spx_exit": spx_exit,
            "vix_1550": vix_1550,
            "afternoon_ret": afternoon_ret,
            "mom_30": mom_30,
            "day_range": day_range,
        })

    print(f"\n  Eligible trading days: {len(day_data_list)}")
    print(f"  Skipped: {skip_counts}")

    # ── Analyze high-conviction condition frequencies ──
    n_vix_lt15 = sum(1 for d in day_data_list if d["vix_1550"] < 15)
    n_flat_aft = sum(1 for d in day_data_list
                     if d["afternoon_ret"] is not None and -0.3 < d["afternoon_ret"] < 0.3)
    n_narrow = sum(1 for d in day_data_list
                   if d["day_range"] is not None and d["day_range"] < 1.0)
    n_vix_flat = sum(1 for d in day_data_list
                     if d["vix_1550"] < 15
                     and d["afternoon_ret"] is not None and -0.3 < d["afternoon_ret"] < 0.3)
    n_all3 = sum(1 for d in day_data_list
                 if d["vix_1550"] < 15
                 and d["afternoon_ret"] is not None and -0.3 < d["afternoon_ret"] < 0.3
                 and d["day_range"] is not None and d["day_range"] < 1.0)

    total = len(day_data_list)
    print(f"\n  HIGH-CONVICTION CONDITION FREQUENCIES:")
    print(f"    VIX < 15:                     {n_vix_lt15:>4} / {total} ({n_vix_lt15/total*100:.1f}%)")
    print(f"    -0.3% < afternoon < 0.3%:     {n_flat_aft:>4} / {total} ({n_flat_aft/total*100:.1f}%)")
    print(f"    Day range < 1.0%:             {n_narrow:>4} / {total} ({n_narrow/total*100:.1f}%)")
    print(f"    VIX<15 AND flat afternoon:    {n_vix_flat:>4} / {total} ({n_vix_flat/total*100:.1f}%)")
    print(f"    All three:                    {n_all3:>4} / {total} ({n_all3/total*100:.1f}%)")

    # ── Define configs ──
    def never_double(day):
        return False

    def double_vix_lt15(day):
        return day["vix_1550"] < 15

    def double_flat_afternoon(day):
        return (day["afternoon_ret"] is not None
                and -0.3 < day["afternoon_ret"] < 0.3)

    def double_vix_and_flat(day):
        return (day["vix_1550"] < 15
                and day["afternoon_ret"] is not None
                and -0.3 < day["afternoon_ret"] < 0.3)

    def double_all_three(day):
        return (day["vix_1550"] < 15
                and day["afternoon_ret"] is not None
                and -0.3 < day["afternoon_ret"] < 0.3
                and day["day_range"] is not None
                and day["day_range"] < 1.0)

    configs = [
        ("A: Single 15:55 (baseline)", never_double, 0.0),
        ("B: +15:53 on VIX<15 (50%)", double_vix_lt15, 0.5),
        ("C: +15:53 on flat aft (50%)", double_flat_afternoon, 0.5),
        ("D: +15:53 VIX<15+flat (75%)", double_vix_and_flat, 0.75),
        ("E: +15:53 all 3 cond (100%)", double_all_three, 1.0),
    ]

    # ── Run all configs ──
    results = []
    for label, fn, risk_mult in configs:
        print(f"\nRunning: {label}...")
        r = run_config(day_data_list, label, fn, risk_mult)
        results.append(r)

    # ── Print comparison table ──
    print("\n" + "=" * 140)
    print("COMPARISON TABLE — Double Entry Research (15:53 on high-conviction days)")
    print("=" * 140)
    print(f"  {'Config':<30} {'Trades':>7} {'C/P':>9} {'P&L':>12} {'MaxDD':>10} {'DD%':>6} "
          f"{'WR%':>6} {'Sharpe':>7} {'PF':>6} {'AvgCr':>7} {'CAGR':>7} {'Days':>5} {'Dbl':>4}")
    print("  " + "-" * 136)

    for r in results:
        if r["trades"] == 0:
            print(f"  {r['label']:<30} NO TRADES")
            continue
        print(
            f"  {r['label']:<30} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['max_dd_pct']:>5.2f}% "
            f"{r['wr']:>5.1f}% "
            f"{r['sharpe']:>7.2f} {r['pf']:>6.2f} "
            f"${r['avg_credit']:>5.2f} "
            f"{r['cagr']:>6.1f}% "
            f"{r['n_days']:>5} {r['n_double_days']:>4}"
        )

    # ── Trade breakdown by entry time ──
    print("\n" + "=" * 120)
    print("TRADE BREAKDOWN BY ENTRY TIME")
    print("=" * 120)
    print(f"  {'Config':<30} {'15:55 #':>8} {'15:55 P&L':>12} {'15:55 WR':>9} "
          f"{'15:53 #':>8} {'15:53 P&L':>12} {'15:53 WR':>9} {'Dbl Days':>9}")
    print("  " + "-" * 116)

    for r in results:
        if r["trades"] == 0:
            continue
        print(
            f"  {r['label']:<30} "
            f"{r['n_t55']:>8} ${r['pnl_t55']:>10,.0f} {r['wr_t55']:>8.1f}% "
            f"{r['n_t53']:>8} ${r['pnl_t53']:>10,.0f} {r['wr_t53']:>8.1f}% "
            f"{r['n_double_days']:>9}"
        )

    # ── Delta vs baseline ──
    base = results[0]
    if base["trades"] > 0:
        print("\n" + "=" * 120)
        print("DELTA vs BASELINE (A)")
        print("=" * 120)
        print(f"  {'Config':<30} {'dP&L':>12} {'dMaxDD':>10} {'dSharpe':>8} {'dPF':>7} {'dTrades':>8} {'Extra$':>10}")
        print("  " + "-" * 116)

        for r in results[1:]:
            if r["trades"] == 0:
                continue
            dpnl = r["pnl"] - base["pnl"]
            ddd = r["max_dd"] - base["max_dd"]
            dsh = r["sharpe"] - base["sharpe"]
            dpf = r["pf"] - base["pf"]
            dtr = r["trades"] - base["trades"]
            # Extra $ from the 15:53 trades
            extra = r["pnl_t53"]
            print(
                f"  {r['label']:<30} "
                f"${dpnl:>+10,.0f} ${ddd:>+8,.0f} "
                f"{dsh:>+8.2f} {dpf:>+7.2f} {dtr:>+8} "
                f"${extra:>+9,.0f}"
            )

    # ── Year-by-year for each config ──
    print("\n" + "=" * 120)
    print("YEAR-BY-YEAR P&L")
    print("=" * 120)
    years = sorted(set(d["date"][:4] for d in day_data_list))
    header = f"  {'Config':<30}"
    for yr in years:
        header += f" {yr:>10}"
    header += f" {'Total':>12}"
    print(header)
    print("  " + "-" * (28 + 12 * len(years) + 14))

    for label, fn, risk_mult in configs:
        yr_pnls = {yr: 0.0 for yr in years}
        for day in day_data_list:
            yr = day["date"][:4]

            # 15:55 entry
            t55 = sim_entry(
                day["spx_df"], day["quotes_df"], day["spx_exit"],
                day["vix_1550"], day["afternoon_ret"], day["mom_30"],
                "15:55", 1.0
            )
            for t in t55:
                yr_pnls[yr] += t["pnl"]

            # 15:53 entry if condition met
            if fn(day):
                t53 = sim_entry(
                    day["spx_df"], day["quotes_df"], day["spx_exit"],
                    day["vix_1550"], day["afternoon_ret"], day["mom_30"],
                    "15:53", risk_mult
                )
                for t in t53:
                    yr_pnls[yr] += t["pnl"]

        row = f"  {label:<30}"
        total = 0
        for yr in years:
            row += f" ${yr_pnls[yr]:>8,.0f}"
            total += yr_pnls[yr]
        row += f" ${total:>10,.0f}"
        print(row)

    # ── Win rate of 15:53 entries by condition ──
    print("\n" + "=" * 120)
    print("15:53 ENTRY ANALYSIS — win rate and P&L on double-entry days")
    print("=" * 120)

    for label, fn, risk_mult in configs[1:]:  # skip baseline
        double_days = [d for d in day_data_list if fn(d)]
        if not double_days:
            print(f"  {label}: 0 qualifying days")
            continue

        # Simulate just the 15:53 trades on qualifying days
        t53_list = []
        for day in double_days:
            t53 = sim_entry(
                day["spx_df"], day["quotes_df"], day["spx_exit"],
                day["vix_1550"], day["afternoon_ret"], day["mom_30"],
                "15:53", risk_mult
            )
            t53_list.extend(t53)

        if not t53_list:
            print(f"  {label}: {len(double_days)} qualifying days, 0 executable 15:53 trades")
            continue

        n53 = len(t53_list)
        w53 = sum(t["win"] for t in t53_list)
        pnl53 = sum(t["pnl"] for t in t53_list)
        avg_pnl = pnl53 / n53
        avg_cr = np.mean([t["credit"] for t in t53_list])

        # Also check the 15:55 win rate on the same qualifying days
        t55_on_double = []
        for day in double_days:
            t55 = sim_entry(
                day["spx_df"], day["quotes_df"], day["spx_exit"],
                day["vix_1550"], day["afternoon_ret"], day["mom_30"],
                "15:55", 1.0
            )
            t55_on_double.extend(t55)

        n55d = len(t55_on_double)
        w55d = sum(t["win"] for t in t55_on_double)
        pnl55d = sum(t["pnl"] for t in t55_on_double)

        print(f"  {label}:")
        print(f"    Qualifying days: {len(double_days)}")
        print(f"    15:53 trades: {n53}  WR: {w53/n53*100:.1f}%  P&L: ${pnl53:>+,.0f}  Avg: ${avg_pnl:>+,.0f}  AvgCr: ${avg_cr:.3f}")
        if n55d > 0:
            print(f"    15:55 trades (same days): {n55d}  WR: {w55d/n55d*100:.1f}%  P&L: ${pnl55d:>+,.0f}")
        print()

    print("=" * 120)
    print("DONE")
    print("=" * 120)


if __name__ == "__main__":
    main()
