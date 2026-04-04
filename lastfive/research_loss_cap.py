"""Research: Per-Side Daily Loss Caps for LastFive strategy.

Tests whether clamping each side's P&L at a maximum loss reduces drawdown
and improves risk-adjusted returns.  Since 0DTE settles at expiration and
can't be exited early, the cap simulates having placed a protective order
or using a narrower spread: if net_pnl < -cap, set net_pnl = -cap.

Base config: Rule C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00,
width $5, min credit $0.10, commission $0.50/leg.
Call filter: afternoon >= 1.0% -> skip.
Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip.
Tiered sizing: cr<0.20 -> $1500, 0.20-0.50 -> $2500, >0.50 -> $3500.
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
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
def spx_at(df: pd.DataFrame, time_str: str) -> float | None:
    rows = df[df["time_str"] == time_str]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """Signed SPX return 13:00 -> 15:54."""
    af = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(af) < 2:
        return None
    s, e = float(af["close"].iloc[0]), float(af["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


def mom_30(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 15:25 to 15:54."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    s, e = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


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
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float | None:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5; >26 -> skip (None)."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    elif vix <= 26:
        return 5.0
    else:
        return None  # skip


# ── Tiered sizing ──────────────────────────────────────────────────────
def tiered_sizing(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    """Simulate a call credit spread with tiered sizing."""
    if dist == 0:
        short_strike = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
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

    risk_amount = tiered_sizing(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

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
            "short_strike": short_strike}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    """Simulate a put credit spread with tiered sizing."""
    if dist == 0:
        short_strike = int(math.floor(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
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

    risk_amount = tiered_sizing(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

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
            "short_strike": short_strike}


# ── Compute metrics ─────────────────────────────────────────────────────
def compute_metrics(day_pnls: list[float]) -> dict:
    arr = np.array(day_pnls)
    total = arr.sum()
    equity = np.cumsum(arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak).min()
    years = len(arr) / 252
    annual_ret = total / years if years > 0 else 0
    calmar = annual_ret / abs(dd) if dd != 0 else 0
    daily_rf = ANNUAL_RF / 252
    excess = arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0
    worst = arr.min()
    return {
        "total_pnl": total,
        "max_dd": dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "worst_day": worst,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("Per-Side Daily Loss Cap — Rule C3 Adaptive + Tiered Sizing + Put Filters")
    print("=" * 120)

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
               "no_opts": 0, "vix_skip": 0}

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

        # Rule C3 distance (returns None if VIX > 26 -> skip)
        dist = rule_c3_dist(vix1550)
        if dist is None:
            skipped["vix_skip"] += 1
            continue

        spot = spx_at(spx_df, "15:55:00")
        if spot is None:
            skipped["no_spx"] += 1
            continue

        spx_exit = spx_at(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = spx_at(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        aft_ret = afternoon_return(spx_df)
        m30 = mom_30(spx_df)

        day_data.append({
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "dist": dist,
            "opts_df": opts_df,
            "aft_ret": aft_ret,
            "mom30": m30,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Simulate all trades (uncapped) first ────────────────────────────
    # Store per-side P&L for each day so we can apply caps later
    print("\nSimulating trades...")
    daily_sides = []  # list of (date, call_pnl_or_None, put_pnl_or_None)

    for dd in day_data:
        # Call side: skip if afternoon >= 1.0%
        call_pnl = None
        skip_call = dd["aft_ret"] is not None and dd["aft_ret"] >= 1.0
        if not skip_call:
            ct = sim_call(dd["spot"], dd["spx_exit"], dd["dist"],
                          dd["opts_df"], "15:55:00")
            if ct is not None:
                call_pnl = ct["pnl"]

        # Put side: skip if afternoon < -0.75% AND mom30 < -0.10%
        put_pnl = None
        skip_put = (dd["aft_ret"] is not None and dd["aft_ret"] < -0.75 and
                    dd["mom30"] is not None and dd["mom30"] < -0.10)
        if not skip_put:
            pt = sim_put(dd["spot"], dd["spx_exit"], dd["dist"],
                         dd["opts_df"], "15:55:00")
            if pt is not None:
                put_pnl = pt["pnl"]

        if call_pnl is not None or put_pnl is not None:
            daily_sides.append((dd["date"], call_pnl, put_pnl))

    print(f"  Trading days with at least one side: {len(daily_sides)}")

    # Count trades
    n_calls = sum(1 for _, c, _ in daily_sides if c is not None)
    n_puts = sum(1 for _, _, p in daily_sides if p is not None)
    print(f"  Call trades: {n_calls}, Put trades: {n_puts}")

    # ── Define cap scenarios ────────────────────────────────────────────
    caps = {
        "A (no cap)":  None,
        "B ($2,000)":  2000.0,
        "C ($2,500)":  2500.0,
        "D ($3,000)":  3000.0,
        "E ($1,500)":  1500.0,
        "F ($1,000)":  1000.0,
    }

    # ── Apply each cap and compute metrics ──────────────────────────────
    results = {}
    for label, cap in caps.items():
        day_pnls = []
        total_trades = 0
        wins = 0

        for date, call_pnl, put_pnl in daily_sides:
            day_total = 0.0

            if call_pnl is not None:
                cp = call_pnl
                if cap is not None and cp < -cap:
                    cp = -cap
                day_total += cp
                total_trades += 1
                if cp > 0:
                    wins += 1

            if put_pnl is not None:
                pp = put_pnl
                if cap is not None and pp < -cap:
                    pp = -cap
                day_total += pp
                total_trades += 1
                if pp > 0:
                    wins += 1

            day_pnls.append(day_total)

        m = compute_metrics(day_pnls)
        m["trades"] = total_trades
        m["wr"] = wins / total_trades * 100 if total_trades > 0 else 0
        results[label] = m

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 120)
    print("COMPARISON TABLE — Per-Side Daily Loss Caps")
    print("  Base: Rule C3 adaptive + tiered sizing + put filter (aftn<-0.75% & mom30<-0.10%)")
    print("  Cap applies to each side independently: if side_pnl < -cap, clamp to -cap")
    print("=" * 120)
    print(f"{'Config':<14} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} "
          f"{'Sharpe':>8} {'Calmar':>8} {'Worst Day':>11}")
    print("-" * 120)

    baseline = results["A (no cap)"]
    for label in caps:
        r = results[label]
        marker = " <-- baseline" if "no cap" in label else ""
        print(
            f"{label:<14} {r['trades']:>7} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"${r['worst_day']:>9,.0f}{marker}"
        )
    print("=" * 120)

    # ── Delta vs baseline ───────────────────────────────────────────────
    print("\nDelta vs A (no cap) baseline:")
    print("-" * 100)
    for label in caps:
        if "no cap" in label:
            continue
        r = results[label]
        b = baseline
        dpnl = r["total_pnl"] - b["total_pnl"]
        ddd = r["max_dd"] - b["max_dd"]  # positive = less DD (improvement)
        dsharpe = r["sharpe"] - b["sharpe"]
        dcalmar = r["calmar"] - b["calmar"]
        dworst = r["worst_day"] - b["worst_day"]  # positive = less bad
        print(f"  {label:<14}: P&L {dpnl:>+10,.0f}  |  DD {ddd:>+8,.0f}  |  "
              f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}  |  "
              f"Worst Day {dworst:>+8,.0f}")

    # ── Show worst 10 days for baseline (uncapped) ──────────────────────
    print("\nWorst 10 days (uncapped baseline):")
    print("-" * 60)
    day_details = []
    for date, call_pnl, put_pnl in daily_sides:
        day_total = (call_pnl or 0.0) + (put_pnl or 0.0)
        day_details.append((date, day_total, call_pnl, put_pnl))
    day_details.sort(key=lambda x: x[1])
    print(f"  {'Date':<12} {'Day P&L':>10} {'Call P&L':>10} {'Put P&L':>10}")
    for date, total, cp, pp in day_details[:10]:
        cs = f"${cp:>8,.0f}" if cp is not None else "     skip"
        ps = f"${pp:>8,.0f}" if pp is not None else "     skip"
        print(f"  {date:<12} ${total:>8,.0f}  {cs}  {ps}")

    print("\nDone.")


if __name__ == "__main__":
    main()
