#!/usr/bin/env python3
"""True IV Skew Analysis — TODO item #4.

Computes Black-Scholes implied volatility from bid/ask option prices,
derives a daily IV skew metric, and cross-references with backtest P&L
to evaluate as a signal.

Uses existing local parquet data:
  - option_greeks/{date}.parquet  (bid, ask, delta, strike, right, timestamp)
  - spx_ohlc/{date}.parquet      (SPX spot price)
  - trade log CSV                 (daily P&L from backtest)
"""
from __future__ import annotations

import glob
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import norm

# ── Project paths ──────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# ── BSM IV solver ──────────────────────────────────────────────────────────

def bsm_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """Black-Scholes-Merton option price. T in years."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if right == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bsm_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """BSM vega (dPrice/dSigma)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * math.sqrt(T) * norm.pdf(d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float,
                right: str, tol: float = 1e-6, max_iter: int = 50) -> float | None:
    """Newton-Raphson IV solver. Returns annualized IV or None if no convergence."""
    if price <= 0 or T <= 0:
        return None
    # Intrinsic value check
    if right == "C":
        intrinsic = max(S - K, 0)
    else:
        intrinsic = max(K - S, 0)
    if price < intrinsic:
        return None

    sigma = 0.20  # initial guess
    for _ in range(max_iter):
        p = bsm_price(S, K, T, r, sigma, right)
        v = bsm_vega(S, K, T, r, sigma)
        if v < 1e-10:
            # No vega — try bisection fallback
            return _bisect_iv(price, S, K, T, r, right)
        sigma_new = sigma - (p - price) / v
        if sigma_new <= 0:
            sigma = sigma / 2  # back off
            continue
        if abs(sigma_new - sigma) < tol:
            return sigma_new
        sigma = sigma_new
    return None


def _bisect_iv(price: float, S: float, K: float, T: float, r: float,
               right: str, lo: float = 0.01, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 80) -> float | None:
    """Bisection fallback for IV when Newton fails."""
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        p = bsm_price(S, K, T, r, mid, right)
        if abs(p - price) < tol:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ── Data loading ───────────────────────────────────────────────────────────

def load_greeks(date_str: str) -> pd.DataFrame | None:
    """Load option_greeks parquet for a date (YYYYMMDD)."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "option_greeks", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def load_spx_ohlc(date_str: str) -> pd.DataFrame | None:
    """Load SPX OHLC parquet for a date (YYYYMMDD)."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def get_spot_at_time(spx_df: pd.DataFrame, time_str: str = "09:35") -> float | None:
    """Get SPX close price at a specific time."""
    mask = spx_df["timestamp"].str.contains(f"T{time_str}")
    if mask.sum() == 0:
        return None
    return float(spx_df.loc[mask, "close"].iloc[0])


# ── Skew computation ──────────────────────────────────────────────────────

def compute_iv_skew(date_str: str, otm_distance: int = 30,
                    entry_time: str = "09:35", r: float = 0.05) -> dict | None:
    """Compute true IV skew for one day.

    Returns dict with put_iv, call_iv, skew_ratio, skew_diff, or None if data missing.
    """
    greeks_df = load_greeks(date_str)
    spx_df = load_spx_ohlc(date_str)
    if greeks_df is None or spx_df is None:
        return None

    spot = get_spot_at_time(spx_df, entry_time)
    if spot is None:
        return None

    # Filter to entry time
    mask = greeks_df["timestamp"].str.contains(f"T{entry_time}")
    bar = greeks_df[mask].copy()
    if bar.empty:
        return None

    # Target strikes at fixed OTM distance
    put_strike = int(round((spot - otm_distance) / 5.0) * 5)
    call_strike = int(round((spot + otm_distance) / 5.0) * 5)

    # Time to expiry: minutes from entry to 4:00 PM close, in years
    h, m = map(int, entry_time.split(":"))
    minutes_to_close = (16 * 60) - (h * 60 + m)
    T = minutes_to_close / (365.25 * 24 * 60)  # in years

    # Get put IV
    put_row = bar[(bar["right"] == "P") & (bar["strike"] == put_strike)]
    call_row = bar[(bar["right"] == "C") & (bar["strike"] == call_strike)]

    put_iv = None
    call_iv = None

    if not put_row.empty:
        mid = (float(put_row.iloc[0]["bid"]) + float(put_row.iloc[0]["ask"])) / 2
        if mid > 0:
            put_iv = implied_vol(mid, spot, put_strike, T, r, "P")

    if not call_row.empty:
        mid = (float(call_row.iloc[0]["bid"]) + float(call_row.iloc[0]["ask"])) / 2
        if mid > 0:
            call_iv = implied_vol(mid, spot, call_strike, T, r, "C")

    if put_iv is None or call_iv is None or call_iv <= 0:
        return None

    return {
        "date": date_str,
        "spot": spot,
        "put_strike": put_strike,
        "call_strike": call_strike,
        "put_iv": round(put_iv, 6),
        "call_iv": round(call_iv, 6),
        "skew_ratio": round(put_iv / call_iv, 4),
        "skew_diff": round(put_iv - call_iv, 6),
        "T_minutes": minutes_to_close,
    }


# ── Load backtest P&L ────────────────────────────────────────────────────

def load_daily_pnl(trade_csv: str | None = None) -> dict[str, float]:
    """Load daily P&L from the most recent trade log CSV.

    Returns {date_str: day_pnl}.
    """
    if trade_csv is None:
        # Find latest trade log
        logs_dir = os.path.join(os.path.dirname(__file__), "logs")
        csvs = sorted(glob.glob(os.path.join(logs_dir, "meft_v35_bidask_log_*.csv")))
        if not csvs:
            print("No trade CSV found. Run a marathon first.")
            sys.exit(1)
        trade_csv = csvs[-1]

    df = pd.read_csv(trade_csv)
    day_pnl = df.groupby("entry_date")["pnl_earned"].sum().to_dict()
    # Convert keys to YYYYMMDD format if needed
    result = {}
    for k, v in day_pnl.items():
        key = str(k).replace("-", "")
        result[key] = v
    return result


# ── Main analysis ─────────────────────────────────────────────────────────

def run_analysis(otm_distance: int = 30, entry_time: str = "09:35"):
    """Compute IV skew for all available days, quintile by P&L."""
    print(f"IV Skew Analysis — OTM distance={otm_distance}pt, entry={entry_time}")
    print("=" * 70)

    # Discover all dates with option_greeks data
    all_dates = set()
    for year_dir in sorted(glob.glob(os.path.join(DATA_DIR, "20*"))):
        greeks_dir = os.path.join(year_dir, "option_greeks")
        if os.path.isdir(greeks_dir):
            for f in os.listdir(greeks_dir):
                if f.endswith(".parquet"):
                    all_dates.add(f.replace(".parquet", ""))

    print(f"Found {len(all_dates)} days with option_greeks data")

    # Load daily P&L
    daily_pnl = load_daily_pnl()
    print(f"Loaded P&L for {len(daily_pnl)} trading days")

    # Compute IV skew for each day
    results = []
    errors = 0
    for i, date_str in enumerate(sorted(all_dates)):
        if date_str not in daily_pnl:
            continue  # skip days not in backtest
        skew = compute_iv_skew(date_str, otm_distance, entry_time)
        if skew is None:
            errors += 1
            continue
        skew["day_pnl"] = daily_pnl[date_str]
        results.append(skew)
        if (i + 1) % 100 == 0:
            print(f"  Processed {i+1}/{len(all_dates)}... ({len(results)} valid)")

    print(f"\nComputed IV skew for {len(results)} days ({errors} errors)")

    if not results:
        print("No results to analyze.")
        return

    df = pd.DataFrame(results)

    # ── Summary stats ─────────────────────────────────────────────────
    print(f"\nIV Skew Summary:")
    print(f"  Put IV:  mean={df['put_iv'].mean():.4f}, std={df['put_iv'].std():.4f}")
    print(f"  Call IV: mean={df['call_iv'].mean():.4f}, std={df['call_iv'].std():.4f}")
    print(f"  Ratio:   mean={df['skew_ratio'].mean():.4f}, std={df['skew_ratio'].std():.4f}")
    print(f"  Diff:    mean={df['skew_diff'].mean():.6f}, std={df['skew_diff'].std():.6f}")

    # ── Quintile analysis ─────────────────────────────────────────────
    df["quintile"] = pd.qcut(df["skew_ratio"], 5, labels=["Q1","Q2","Q3","Q4","Q5"])

    print(f"\n{'='*70}")
    print(f"QUINTILE ANALYSIS — IV Skew Ratio (put_iv / call_iv)")
    print(f"{'='*70}")
    print(f"{'Quintile':<10} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12} {'Avg Skew':>10}")
    print("-" * 60)

    for q in ["Q1","Q2","Q3","Q4","Q5"]:
        subset = df[df["quintile"] == q]
        n = len(subset)
        wr = (subset["day_pnl"] > 0).mean() * 100
        avg = subset["day_pnl"].mean()
        total = subset["day_pnl"].sum()
        avg_skew = subset["skew_ratio"].mean()
        print(f"{q:<10} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f} {avg_skew:>10.4f}")

    total_all = df["day_pnl"].sum()
    print("-" * 60)
    print(f"{'Total':<10} {len(df):>5} {(df['day_pnl']>0).mean()*100:>5.1f}% ${df['day_pnl'].mean():>9.2f} ${total_all:>11.2f}")

    # ── Gradient strength ─────────────────────────────────────────────
    q1_avg = df[df["quintile"]=="Q1"]["day_pnl"].mean()
    q5_avg = df[df["quintile"]=="Q5"]["day_pnl"].mean()
    gradient = q5_avg - q1_avg
    print(f"\nQ1→Q5 gradient: ${gradient:.2f}/day")
    print(f"(Credit proxy gradient was $274/day)")

    # ── Also try skew_diff (absolute IV difference) ───────────────────
    df["quintile_diff"] = pd.qcut(df["skew_diff"], 5, labels=["Q1","Q2","Q3","Q4","Q5"])

    print(f"\n{'='*70}")
    print(f"QUINTILE ANALYSIS — IV Skew Diff (put_iv - call_iv)")
    print(f"{'='*70}")
    print(f"{'Quintile':<10} {'Days':>5} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12} {'Avg Diff':>10}")
    print("-" * 60)

    for q in ["Q1","Q2","Q3","Q4","Q5"]:
        subset = df[df["quintile_diff"] == q]
        n = len(subset)
        wr = (subset["day_pnl"] > 0).mean() * 100
        avg = subset["day_pnl"].mean()
        total = subset["day_pnl"].sum()
        avg_diff = subset["skew_diff"].mean()
        print(f"{q:<10} {n:>5} {wr:>5.1f}% ${avg:>9.2f} ${total:>11.2f} {avg_diff:>10.6f}")

    # ── Save results ──────────────────────────────────────────────────
    out_path = os.path.join(os.path.dirname(__file__), "logs", "iv_skew_analysis.csv")
    df.to_csv(out_path, index=False)
    print(f"\nDetailed results saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="True IV Skew Analysis")
    parser.add_argument("--otm", type=int, default=30, help="OTM distance in points (default: 30)")
    parser.add_argument("--time", type=str, default="09:35", help="Entry time (default: 09:35)")
    parser.add_argument("--csv", type=str, default=None, help="Path to trade log CSV")
    args = parser.parse_args()
    run_analysis(otm_distance=args.otm, entry_time=args.time)
