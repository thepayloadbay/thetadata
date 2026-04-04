"""Research: Adaptive Short Strike Distance Based on Volatility.

Hypothesis: On calm days we can sell closer to money for more credit.
On volatile days we need more distance for safety.

Tests several adaptive rules plus fixed baselines.
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
    """Get close price at HH:MM bar."""
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_30min_range(spx_df: pd.DataFrame) -> float | None:
    """Max high - min low from 15:25 to 15:54 bars."""
    window = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["hhmm"] <= "15:54")]
    if len(window) < 5:
        return None
    return float(window["high"].max() - window["low"].min())


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 13:00 to 15:54."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return abs(end - start) / start * 100


def find_short_strike(spot: float, dist: float) -> int:
    """Lowest strike >= spot + dist, rounded up to STRIKE_STEP."""
    target = spot + dist
    return int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)


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


# ── Adaptive distance rules ────────────────────────────────────────────
def rule_a(range30: float, vix1550: float, spot: float) -> float:
    """If 30-min range < $5: dist=0; elif range > $10: dist=10; else dist=5."""
    if range30 < 5:
        return 0.0
    elif range30 > 10:
        return 10.0
    else:
        return 5.0


def rule_b(range30: float, vix1550: float, spot: float) -> float:
    """dist = max(3, round(30min_range * 1.0 / 5) * 5) — scale to recent vol."""
    return max(3.0, round(range30 / 5.0) * 5.0)


def rule_c(range30: float, vix1550: float, spot: float) -> float:
    """VIX-based: <15 -> dist=0; 15-20 -> dist=3; 20-26 -> dist=5."""
    if vix1550 < 15:
        return 0.0
    elif vix1550 < 20:
        return 3.0
    else:
        return 5.0


def rule_d(range30: float, vix1550: float, spot: float) -> float:
    """1-sigma expected move for 5 minutes, rounded to nearest 5."""
    # sigma_5min = spot * (VIX/100) * sqrt(5 / (252*390))
    sigma = spot * (vix1550 / 100.0) * math.sqrt(5.0 / (252.0 * 390.0))
    return max(0.0, round(sigma / 5.0) * 5.0)


def fixed_dist(d: float):
    """Factory for fixed-distance rules."""
    def _rule(range30: float, vix1550: float, spot: float) -> float:
        return d
    _rule.__name__ = f"Fixed_{int(d)}"
    return _rule


RULES = {
    "Fixed_0":  fixed_dist(0),
    "Fixed_3":  fixed_dist(3),
    "Fixed_5":  fixed_dist(5),
    "Rule_A":   rule_a,
    "Rule_B":   rule_b,
    "Rule_C":   rule_c,
    "Rule_D":   rule_d,
}


# ── Simulate one trade ─────────────────────────────────────────────────
def sim_trade(spot: float, spx_exit: float, dist: float,
              quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    """Simulate a single trade given distance. Returns trade dict or None if skip."""
    short_strike = find_short_strike(spot, dist)
    long_strike = short_strike + int(SPREAD_WIDTH)

    available = set(quotes_df["strike"].unique())
    if short_strike not in available or long_strike not in available:
        # Try next available
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

    return {
        "credit": credit,
        "pnl": net,
        "qty": qty,
        "short_strike": short_strike,
        "dist_used": short_strike - spot,
    }


# ── Main loop ──────────────────────────────────────────────────────────
def main():
    print("Loading trading dates...")
    # Collect all dates with SPX data
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
    day_data = []  # list of dicts with all needed info
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_filter": 0, "afternoon_filter": 0, "no_range": 0}

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

        # Filter: VIX at 15:50 <= 26
        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        # Filter: afternoon return < 1.0%
        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # 30-min range
        range30 = compute_30min_range(spx_df)
        if range30 is None:
            skipped["no_range"] += 1
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
            "range30": range30,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")
    print()

    # ── Run each rule ───────────────────────────────────────────────────
    results = {}
    for rule_name, rule_fn in RULES.items():
        trades_pnl = []
        trades_credit = []
        n_trades = 0
        n_wins = 0
        n_skip = 0
        dists_used = []

        for dd in day_data:
            dist = rule_fn(dd["range30"], dd["vix1550"], dd["spot"])
            trade = sim_trade(
                dd["spot"], dd["spx_exit"], dist,
                dd["opts_df"], "15:55:00"
            )
            if trade is None:
                n_skip += 1
                continue
            n_trades += 1
            trades_pnl.append(trade["pnl"])
            trades_credit.append(trade["credit"])
            dists_used.append(trade["dist_used"])
            if trade["pnl"] > 0:
                n_wins += 1

        if n_trades == 0:
            results[rule_name] = None
            continue

        pnl_arr = np.array(trades_pnl)
        total_pnl = pnl_arr.sum()
        avg_pnl = pnl_arr.mean()
        avg_credit = np.mean(trades_credit)
        wr = n_wins / n_trades * 100

        # Max drawdown
        equity = np.cumsum(pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = equity - peak
        max_dd = dd_arr.min()

        # Sharpe (daily)
        daily_rf = ANNUAL_RF / 252
        sharpe = (pnl_arr.mean() - daily_rf * STARTING_EQUITY / n_trades) / pnl_arr.std() * np.sqrt(252) if pnl_arr.std() > 0 else 0

        # Calmar
        calmar = total_pnl / abs(max_dd) if max_dd != 0 else 0

        avg_dist = np.mean(dists_used)

        results[rule_name] = {
            "trades": n_trades,
            "skipped": n_skip,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "avg_credit": avg_credit,
            "avg_pnl": avg_pnl,
            "avg_dist": avg_dist,
        }

    # ── Print comparison table ──────────────────────────────────────────
    print("=" * 120)
    print(f"{'Rule':<12} {'Trades':>7} {'Skip':>5} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} "
          f"{'Sharpe':>8} {'Calmar':>8} {'Avg Credit':>11} {'Avg P&L':>9} {'Avg Dist':>9}")
    print("-" * 120)

    for rule_name in RULES:
        r = results.get(rule_name)
        if r is None:
            print(f"{rule_name:<12}  {'NO TRADES':>7}")
            continue
        print(
            f"{rule_name:<12} {r['trades']:>7} {r['skipped']:>5} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"${r['avg_credit']:>9.2f} ${r['avg_pnl']:>7.0f} "
            f"${r['avg_dist']:>7.1f}"
        )
    print("=" * 120)

    # ── Distribution of distances for adaptive rules ────────────────────
    print("\nDistance distribution for adaptive rules:")
    print("-" * 70)
    for rule_name, rule_fn in RULES.items():
        if rule_name.startswith("Fixed"):
            continue
        dist_counts = {}
        for dd in day_data:
            d = rule_fn(dd["range30"], dd["vix1550"], dd["spot"])
            d_rounded = round(d, 1)
            dist_counts[d_rounded] = dist_counts.get(d_rounded, 0) + 1
        sorted_dists = sorted(dist_counts.items())
        dist_str = ", ".join(f"${d:.0f}: {c}" for d, c in sorted_dists)
        print(f"  {rule_name}: {dist_str}")

    print("\nDone.")


if __name__ == "__main__":
    main()
