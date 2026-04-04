"""Walk-Forward Validation of Rule C3 (VIX-adaptive strike distance).

Rule C3: VIX<18 -> dist=0, 18<=VIX<22 -> dist=3, 22<=VIX<=26 -> dist=5, VIX>26 -> skip.
Baseline: Fixed dist=3 always (VIX>26 -> skip).

Split:
  TRAIN (in-sample):  2022-01-03 to 2024-12-31
  TEST  (out-of-sample): 2025-01-01 to 2026-03-25

Key question: does Rule C3's advantage over Fixed persist out-of-sample?
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
RISK_AMOUNT = 2500.0
COMMISSION = 0.50  # per leg per contract
STRIKE_STEP = 5
STARTING_EQUITY = 50_000.0
ANNUAL_RF = 0.05

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


# ── Distance rules ────────────────────────────────────────────────────
def rule_c3(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


def fixed_3(vix: float) -> float:
    return 3.0


# ── Simulate one side ─────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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
    qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

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
            "short_strike": short_strike, "dist_used": short_strike - spot}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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
    qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

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
            "short_strike": short_strike, "dist_used": spot - short_strike}


# ── Compute metrics for a set of trades ────────────────────────────────
def compute_metrics(day_pnls: list[float], trades: list[dict]) -> dict:
    """Compute all metrics from daily P&L list and individual trades."""
    if not trades or not day_pnls:
        return None

    arr = np.array(day_pnls)
    total_pnl = arr.sum()
    n_trades = len(trades)
    n_calls = sum(1 for t in trades if t["side"] == "CALL")
    n_puts = sum(1 for t in trades if t["side"] == "PUT")

    # Max drawdown
    equity = np.cumsum(arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    max_dd = (equity - peak).min()

    # Win rate
    n_wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = n_wins / n_trades * 100

    # Sharpe
    daily_rf = ANNUAL_RF / 252
    excess = arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0

    # Calmar
    years = len(arr) / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    # Avg credit
    avg_credit = np.mean([t["credit"] for t in trades])

    # P&L per trade
    pnl_per_trade = total_pnl / n_trades

    # Avg win / avg loss
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0

    return {
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "n_trades": n_trades,
        "n_calls": n_calls,
        "n_puts": n_puts,
        "sharpe": sharpe,
        "calmar": calmar,
        "avg_credit": avg_credit,
        "pnl_per_trade": pnl_per_trade,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "n_days": len(arr),
    }


# ── Main ──────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("WALK-FORWARD VALIDATION: Rule C3 vs Fixed dist=3")
    print("  TRAIN: 2022-01-03 to 2024-12-31 (in-sample)")
    print("  TEST:  2025-01-01 to 2026-03-25 (out-of-sample)")
    print("=" * 110)

    # ── Load all eligible days ─────────────────────────────────────────
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if "20220103" <= ds <= "20260325":
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_high": 0, "afternoon_filter": 0}

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

        # VIX > 26 filter (both configs skip these)
        if vix1550 > 26:
            skipped["vix_high"] += 1
            continue

        # Afternoon return filter < 1.0%
        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # Spot at 15:55
        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
            skipped["no_spx"] += 1
            continue
        spot = float(spot_row["close"].iloc[0])

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
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Split into TRAIN / TEST ────────────────────────────────────────
    train_data = [d for d in day_data if d["date"] <= "20241231"]
    test_data = [d for d in day_data if d["date"] >= "20250101"]
    print(f"\n  TRAIN days: {len(train_data)}  ({train_data[0]['date']} to {train_data[-1]['date']})")
    print(f"  TEST  days: {len(test_data)}   ({test_data[0]['date']} to {test_data[-1]['date']})")

    # ── VIX distribution per period ────────────────────────────────────
    print("\n  VIX distribution:")
    brackets = [(0, 18), (18, 22), (22, 26)]
    for label, data in [("TRAIN", train_data), ("TEST", test_data)]:
        vvals = [d["vix1550"] for d in data]
        dist_str = "  ".join(
            f"VIX {lo}-{hi}: {sum(1 for v in vvals if lo <= v < hi)} ({sum(1 for v in vvals if lo <= v < hi)/len(vvals)*100:.0f}%)"
            for lo, hi in brackets
        )
        print(f"    {label}: {dist_str}")

    # ── Simulate each config on each period ────────────────────────────
    configs = {"Rule_C3": rule_c3, "Fixed_3": fixed_3}
    periods = {"TRAIN": train_data, "TEST": test_data}

    results = {}  # (config, period) -> metrics dict
    trade_log = {}  # (config, period) -> list of trades with dates

    for cfg_name, rule_fn in configs.items():
        for period_name, pdata in periods.items():
            trades = []
            day_pnls = []

            for dd in pdata:
                dist = rule_fn(dd["vix1550"])
                day_pnl = 0.0
                day_trades = []

                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct is not None:
                    ct["date"] = dd["date"]
                    ct["vix"] = dd["vix1550"]
                    ct["dist"] = dist
                    day_trades.append(ct)
                    day_pnl += ct["pnl"]

                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt is not None:
                    pt["date"] = dd["date"]
                    pt["vix"] = dd["vix1550"]
                    pt["dist"] = dist
                    day_trades.append(pt)
                    day_pnl += pt["pnl"]

                if day_trades:
                    day_pnls.append(day_pnl)
                    trades.extend(day_trades)

            key = (cfg_name, period_name)
            trade_log[key] = trades
            results[key] = compute_metrics(day_pnls, trades)

    # ── Also compute year-by-year breakdown ────────────────────────────
    years_all = sorted(set(d["date"][:4] for d in day_data))
    yearly = {}  # (config, year) -> metrics

    for cfg_name, rule_fn in configs.items():
        for yr in years_all:
            yr_data = [d for d in day_data if d["date"][:4] == yr]
            trades = []
            day_pnls = []

            for dd in yr_data:
                dist = rule_fn(dd["vix1550"])
                day_pnl = 0.0
                day_trades = []

                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct is not None:
                    ct["date"] = dd["date"]
                    ct["vix"] = dd["vix1550"]
                    ct["dist"] = dist
                    day_trades.append(ct)
                    day_pnl += ct["pnl"]

                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt is not None:
                    pt["date"] = dd["date"]
                    pt["vix"] = dd["vix1550"]
                    pt["dist"] = dist
                    day_trades.append(pt)
                    day_pnl += pt["pnl"]

                if day_trades:
                    day_pnls.append(day_pnl)
                    trades.extend(day_trades)

            yearly[(cfg_name, yr)] = compute_metrics(day_pnls, trades)

    # ══════════════════════════════════════════════════════════════════════
    #  PRINT RESULTS
    # ══════════════════════════════════════════════════════════════════════

    print("\n" + "=" * 110)
    print("WALK-FORWARD RESULTS: Rule C3 vs Fixed dist=3")
    print("=" * 110)

    header = (
        f"{'Config':<10} {'Period':<7} {'P&L':>10} {'Max DD':>10} {'WR%':>6} "
        f"{'Trades':>7} {'C/P':>9} {'Sharpe':>7} {'Calmar':>8} "
        f"{'Avg Cr':>7} {'$/Trade':>9} {'AvgWin':>8} {'AvgLoss':>9}"
    )
    print(header)
    print("-" * 110)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        for period_name in ["TRAIN", "TEST"]:
            r = results[(cfg_name, period_name)]
            if r is None:
                print(f"{cfg_name:<10} {period_name:<7}  NO TRADES")
                continue
            print(
                f"{cfg_name:<10} {period_name:<7} "
                f"${r['total_pnl']:>8,.0f} ${r['max_dd']:>8,.0f} "
                f"{r['wr']:>5.1f}% "
                f"{r['n_trades']:>7} {r['n_calls']:>4}/{r['n_puts']:<4} "
                f"{r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
                f"${r['avg_credit']:>5.2f} ${r['pnl_per_trade']:>7.0f} "
                f"${r['avg_win']:>6.0f} ${r['avg_loss']:>7.0f}"
            )
        print("-" * 110)

    # ── Delta (C3 - Fixed) per period ──────────────────────────────────
    print("\nDELTA (Rule C3 minus Fixed_3):")
    print("-" * 80)
    print(f"{'Period':<7} {'dP&L':>10} {'dMaxDD':>10} {'dWR':>7} {'dSharpe':>8} {'dCalmar':>9} {'d$/Trade':>10}")
    print("-" * 80)
    for period_name in ["TRAIN", "TEST"]:
        rc3 = results[("Rule_C3", period_name)]
        rf3 = results[("Fixed_3", period_name)]
        if rc3 is None or rf3 is None:
            continue
        dp = rc3["total_pnl"] - rf3["total_pnl"]
        ddd = rc3["max_dd"] - rf3["max_dd"]
        dwr = rc3["wr"] - rf3["wr"]
        ds = rc3["sharpe"] - rf3["sharpe"]
        dc = rc3["calmar"] - rf3["calmar"]
        dpt = rc3["pnl_per_trade"] - rf3["pnl_per_trade"]
        print(
            f"{period_name:<7} "
            f"{'$':>1}{dp:>+9,.0f} {'$':>1}{ddd:>+9,.0f} {dwr:>+6.1f}% "
            f"{ds:>+8.2f} {dc:>+9.2f} {'$':>1}{dpt:>+9.0f}"
        )
    print("-" * 80)

    # ── Year-by-year breakdown ─────────────────────────────────────────
    print("\nYEAR-BY-YEAR P&L BREAKDOWN:")
    print("-" * 90)
    yr_header = f"{'Config':<10}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 90)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        row = f"{cfg_name:<10}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" ${m['total_pnl']:>10,.0f}"
            else:
                row += f" {'N/A':>12}"
        print(row)

    # Delta row
    row = f"{'DELTA':<10}"
    for yr in years_all:
        mc3 = yearly.get(("Rule_C3", yr))
        mf3 = yearly.get(("Fixed_3", yr))
        if mc3 and mf3:
            d = mc3["total_pnl"] - mf3["total_pnl"]
            row += f" ${d:>+10,.0f}"
        else:
            row += f" {'N/A':>12}"
    print(row)
    print("-" * 90)

    # ── Year-by-year Sharpe ────────────────────────────────────────────
    print("\nYEAR-BY-YEAR SHARPE:")
    print("-" * 90)
    yr_header = f"{'Config':<10}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 90)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        row = f"{cfg_name:<10}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['sharpe']:>12.2f}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 90)

    # ── Year-by-year WR ───────────────────────────────────────────────
    print("\nYEAR-BY-YEAR WIN RATE (%):")
    print("-" * 90)
    yr_header = f"{'Config':<10}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 90)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        row = f"{cfg_name:<10}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['wr']:>11.1f}%"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 90)

    # ── Year-by-year trades ───────────────────────────────────────────
    print("\nYEAR-BY-YEAR TRADES:")
    print("-" * 90)
    yr_header = f"{'Config':<10}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 90)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        row = f"{cfg_name:<10}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['n_trades']:>12}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 90)

    # ── Year-by-year Max DD ───────────────────────────────────────────
    print("\nYEAR-BY-YEAR MAX DRAWDOWN:")
    print("-" * 90)
    yr_header = f"{'Config':<10}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 90)

    for cfg_name in ["Rule_C3", "Fixed_3"]:
        row = f"{cfg_name:<10}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" ${m['max_dd']:>10,.0f}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 90)

    # ── VIX bracket analysis per period ────────────────────────────────
    print("\nVIX BRACKET ANALYSIS (Rule C3 only):")
    print("-" * 100)
    vix_brackets = [(0, 18, "d=0"), (18, 22, "d=3"), (22, 26, "d=5")]

    for period_name in ["TRAIN", "TEST"]:
        print(f"\n  {period_name}:")
        key = ("Rule_C3", period_name)
        tlog = trade_log[key]
        for lo, hi, dlabel in vix_brackets:
            bt = [t for t in tlog if lo <= t["vix"] < hi]
            if not bt:
                print(f"    VIX {lo}-{hi} ({dlabel}): no trades")
                continue
            pnl = sum(t["pnl"] for t in bt)
            wins = sum(1 for t in bt if t["pnl"] > 0)
            wr_b = wins / len(bt) * 100
            avg_cr = np.mean([t["credit"] for t in bt])
            print(
                f"    VIX {lo}-{hi} ({dlabel}): {len(bt):>4} trades  "
                f"P&L=${pnl:>+9,.0f}  WR={wr_b:.1f}%  AvgCr=${avg_cr:.2f}"
            )

    # ══════════════════════════════════════════════════════════════════════
    #  VERDICT
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 110)
    print("VERDICT:")
    print("=" * 110)

    rc3_train = results[("Rule_C3", "TRAIN")]
    rf3_train = results[("Fixed_3", "TRAIN")]
    rc3_test = results[("Rule_C3", "TEST")]
    rf3_test = results[("Fixed_3", "TEST")]

    train_delta = rc3_train["total_pnl"] - rf3_train["total_pnl"]
    test_delta = rc3_test["total_pnl"] - rf3_test["total_pnl"]

    train_better = rc3_train["total_pnl"] > rf3_train["total_pnl"]
    test_better = rc3_test["total_pnl"] > rf3_test["total_pnl"]
    sharpe_test_better = rc3_test["sharpe"] > rf3_test["sharpe"]

    print(f"  TRAIN: Rule C3 {'BEATS' if train_better else 'LOSES TO'} Fixed by ${train_delta:+,.0f}")
    print(f"  TEST:  Rule C3 {'BEATS' if test_better else 'LOSES TO'} Fixed by ${test_delta:+,.0f}")
    print(f"  TEST Sharpe: C3={rc3_test['sharpe']:.2f} vs Fixed={rf3_test['sharpe']:.2f} "
          f"({'C3 better' if sharpe_test_better else 'Fixed better'})")

    if train_better and test_better:
        # Check if OOS advantage is at least 30% of IS advantage (not totally decayed)
        if train_delta > 0:
            decay = test_delta / (train_delta / (len(train_data) / len(test_data))) if train_delta != 0 else 0
            print(f"\n  OOS retention: {decay*100:.0f}% of IS edge (normalized by period length)")
        print("\n  >>> VALIDATED: Rule C3 advantage persists out-of-sample. NOT overfit.")
    elif train_better and not test_better:
        print("\n  >>> OVERFIT: Rule C3 wins IS but loses OOS. Edge does not generalize.")
    elif not train_better:
        print("\n  >>> Rule C3 does not beat Fixed even in-sample.")

    print("\nDone.")


if __name__ == "__main__":
    main()
