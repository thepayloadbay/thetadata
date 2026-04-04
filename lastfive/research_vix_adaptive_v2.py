"""Research: VIX-Adaptive Strike Distance v2 — Full Bidirectional (Call+Put).

Prior sweep found Rule C (VIX-adaptive) gives $62k call-only.
Now verify with BOTH sides and test additional VIX bracket variants.

Configs tested:
1. Fixed dist=3 (current baseline)
2. Rule C: VIX<15 d=0, VIX 15-20 d=3, VIX 20-26 d=5
3. Rule C2: VIX<15 d=0, VIX 15-18 d=3, VIX 18-22 d=5, VIX 22-26 d=7
4. Rule C3: VIX<18 d=0, VIX 18-22 d=3, VIX 22-26 d=5
"""
from __future__ import annotations

import glob
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

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
def fixed_3(vix: float) -> float:
    return 3.0


def rule_c(vix: float) -> float:
    """VIX<15 -> 0; 15-20 -> 3; 20-26 -> 5."""
    if vix < 15:
        return 0.0
    elif vix < 20:
        return 3.0
    else:
        return 5.0


def rule_c2(vix: float) -> float:
    """VIX<15 -> 0; 15-18 -> 3; 18-22 -> 5; 22-26 -> 7."""
    if vix < 15:
        return 0.0
    elif vix < 18:
        return 3.0
    elif vix < 22:
        return 5.0
    else:
        return 7.0


def rule_c3(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


RULES = {
    "Fixed_3":  fixed_3,
    "Rule_C":   rule_c,
    "Rule_C2":  rule_c2,
    "Rule_C3":  rule_c3,
}


# ── Simulate one side ─────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
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


# ── Main loop ──────────────────────────────────────────────────────────
def main():
    print("=" * 100)
    print("VIX-Adaptive Strike Distance v2 — Bidirectional (Call + Put)")
    print("=" * 100)

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

        # VIX > 26 filter (all configs skip these)
        if vix1550 > 26:
            skipped["vix_filter"] += 1
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

    # ── VIX distribution ───────────────────────────────────────────────
    vix_vals = [dd["vix1550"] for dd in day_data]
    print(f"\n  VIX(15:50) distribution across eligible days:")
    brackets = [(0, 15), (15, 18), (18, 20), (20, 22), (22, 26)]
    for lo, hi in brackets:
        cnt = sum(1 for v in vix_vals if lo <= v < hi)
        print(f"    VIX {lo}-{hi}: {cnt} days ({cnt/len(vix_vals)*100:.1f}%)")

    # ── Run each rule ───────────────────────────────────────────────────
    results = {}
    for rule_name, rule_fn in RULES.items():
        call_trades = []
        put_trades = []
        all_day_pnls = []  # combined daily P&L for Sharpe

        for dd in day_data:
            dist = rule_fn(dd["vix1550"])
            day_pnl = 0.0

            # Call side
            ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if ct is not None:
                call_trades.append(ct)
                day_pnl += ct["pnl"]

            # Put side
            pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if pt is not None:
                put_trades.append(pt)
                day_pnl += pt["pnl"]

            if ct is not None or pt is not None:
                all_day_pnls.append(day_pnl)

        all_trades = call_trades + put_trades
        n_trades = len(all_trades)
        n_calls = len(call_trades)
        n_puts = len(put_trades)

        if n_trades == 0:
            results[rule_name] = None
            continue

        # Combined P&L using daily aggregation for equity curve
        day_pnl_arr = np.array(all_day_pnls)
        total_pnl = day_pnl_arr.sum()

        # Max drawdown on daily equity curve
        equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = equity - peak
        max_dd = dd_arr.min()

        # Win rate (per trade)
        n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
        wr = n_wins / n_trades * 100

        # Call/Put WR
        call_wins = sum(1 for t in call_trades if t["pnl"] > 0)
        put_wins = sum(1 for t in put_trades if t["pnl"] > 0)
        call_wr = call_wins / n_calls * 100 if n_calls > 0 else 0
        put_wr = put_wins / n_puts * 100 if n_puts > 0 else 0

        # Sharpe (daily)
        daily_rf = ANNUAL_RF / 252
        excess = day_pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = excess.mean() / day_pnl_arr.std() * np.sqrt(252) if day_pnl_arr.std() > 0 else 0

        # Calmar
        years = len(day_pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Avg credit
        avg_credit = np.mean([t["credit"] for t in all_trades])
        avg_call_credit = np.mean([t["credit"] for t in call_trades]) if call_trades else 0
        avg_put_credit = np.mean([t["credit"] for t in put_trades]) if put_trades else 0

        # Call/Put P&L
        call_pnl = sum(t["pnl"] for t in call_trades)
        put_pnl = sum(t["pnl"] for t in put_trades)

        results[rule_name] = {
            "trades": n_trades,
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_pnl": total_pnl,
            "call_pnl": call_pnl,
            "put_pnl": put_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "call_wr": call_wr,
            "put_wr": put_wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "avg_credit": avg_credit,
            "avg_call_credit": avg_call_credit,
            "avg_put_credit": avg_put_credit,
        }

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 140)
    print("COMPARISON TABLE — VIX-Adaptive Strike Distance (Bidirectional Call+Put)")
    print("=" * 140)
    print(f"{'Config':<12} {'Trades':>7} {'C/P':>9} {'Total P&L':>12} {'Call P&L':>10} "
          f"{'Put P&L':>10} {'Max DD':>10} {'WR%':>6} {'C_WR':>5} {'P_WR':>5} "
          f"{'Sharpe':>7} {'Calmar':>7} {'Avg Cr':>7}")
    print("-" * 140)

    for rule_name in RULES:
        r = results.get(rule_name)
        if r is None:
            print(f"{rule_name:<12}  NO TRADES")
            continue
        print(
            f"{rule_name:<12} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['call_pnl']:>8,.0f} "
            f"${r['put_pnl']:>8,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>5.1f}% {r['call_wr']:>4.1f} {r['put_wr']:>4.1f} "
            f"{r['sharpe']:>7.2f} {r['calmar']:>7.2f} "
            f"${r['avg_credit']:>5.2f}"
        )
    print("=" * 140)

    # ── Rule descriptions ─────────────────────────────────────────────
    print("\nRule definitions:")
    print("  Fixed_3 : dist=3 always (current baseline)")
    print("  Rule_C  : VIX<15 d=0, VIX 15-20 d=3, VIX 20-26 d=5")
    print("  Rule_C2 : VIX<15 d=0, VIX 15-18 d=3, VIX 18-22 d=5, VIX 22-26 d=7")
    print("  Rule_C3 : VIX<18 d=0, VIX 18-22 d=3, VIX 22-26 d=5")

    # ── Distance distribution per rule ────────────────────────────────
    print("\nDistance distribution per rule:")
    print("-" * 70)
    for rule_name, rule_fn in RULES.items():
        if rule_name == "Fixed_3":
            continue
        dist_counts = {}
        for dd in day_data:
            d = rule_fn(dd["vix1550"])
            d_rounded = round(d, 1)
            dist_counts[d_rounded] = dist_counts.get(d_rounded, 0) + 1
        sorted_dists = sorted(dist_counts.items())
        dist_str = ", ".join(f"d={d:.0f}: {c} days" for d, c in sorted_dists)
        print(f"  {rule_name}: {dist_str}")

    # ── Year-by-year breakdown ─────────────────────────────────────────
    print("\nYear-by-year P&L breakdown:")
    print("-" * 90)
    years = sorted(set(dd["date"][:4] for dd in day_data))
    header = f"{'Config':<12}"
    for yr in years:
        header += f" {yr:>10}"
    print(header)
    print("-" * 90)

    for rule_name, rule_fn in RULES.items():
        yr_pnl = {yr: 0.0 for yr in years}
        for dd in day_data:
            dist = rule_fn(dd["vix1550"])
            day_pnl = 0.0
            ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if ct:
                day_pnl += ct["pnl"]
            pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if pt:
                day_pnl += pt["pnl"]
            yr_pnl[dd["date"][:4]] += day_pnl

        row = f"{rule_name:<12}"
        for yr in years:
            row += f" ${yr_pnl[yr]:>8,.0f}"
        print(row)
    print("-" * 90)

    # ── Delta vs baseline ─────────────────────────────────────────────
    baseline = results.get("Fixed_3")
    if baseline:
        print("\nDelta vs Fixed_3 baseline:")
        print("-" * 70)
        for rule_name in RULES:
            if rule_name == "Fixed_3":
                continue
            r = results.get(rule_name)
            if r is None:
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dsharpe = r["sharpe"] - baseline["sharpe"]
            dcalmar = r["calmar"] - baseline["calmar"]
            print(f"  {rule_name:<10}: P&L {'+' if dpnl>=0 else ''}{dpnl:>+,.0f}  |  "
                  f"DD {'+' if ddd>=0 else ''}{ddd:>+,.0f}  |  "
                  f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
