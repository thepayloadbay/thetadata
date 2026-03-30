#!/usr/bin/env python3
"""Full Greeks Calculator — 1st, 2nd, and 3rd order.

Computes all Greeks from existing option_quotes + spx_ohlc parquet data
using Black-Scholes-Merton closed-form formulas.

Output: data/{year}/option_greeks_full/{YYYYMMDD}.parquet
Columns: right, strike, timestamp, bid, ask, mid, spot, iv,
         delta, gamma, theta, vega, rho,           (1st order)
         vanna, charm, vomma, veta, speed, color,   (2nd order)
         ultima, zomma                               (3rd order)

Usage:
  python compute_greeks.py                    # all dates missing greeks_full
  python compute_greeks.py --year 2024        # specific year
  python compute_greeks.py --date 20241231    # single date
  python compute_greeks.py --force            # recompute all
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.stats import norm

# ── Paths ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

RISK_FREE_RATE = 0.05  # annualized


# ── BSM IV Solver ─────────────────────────────────────────────────────────

def _bsm_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _bsm_vega_scalar(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
    return S * sqrtT * norm.pdf(d1)


def implied_vol_vectorized(prices: np.ndarray, S: np.ndarray, K: np.ndarray,
                           T: np.ndarray, r: float, is_call: np.ndarray,
                           tol: float = 1e-6, max_iter: int = 50) -> np.ndarray:
    """Vectorized Newton-Raphson IV solver. Returns array of IV (NaN where failed)."""
    n = len(prices)
    result = np.full(n, np.nan)

    # Validity mask
    intrinsic = np.where(is_call, np.maximum(S - K, 0), np.maximum(K - S, 0))
    valid = (prices > 0) & (T > 0) & (S > 0) & (K > 0) & (prices >= intrinsic)
    if not valid.any():
        return result

    # Work on valid subset
    p = prices[valid]
    s = S[valid]
    k = K[valid]
    t = T[valid]
    call = is_call[valid]
    sigma = np.full(valid.sum(), 0.25)

    converged = np.zeros(valid.sum(), dtype=bool)

    for _ in range(max_iter):
        active = ~converged
        if not active.any():
            break

        sqrtT = np.sqrt(t[active])
        d1 = (np.log(s[active] / k[active]) + (r + 0.5 * sigma[active]**2) * t[active]) / (sigma[active] * sqrtT)
        d2 = d1 - sigma[active] * sqrtT
        nd1 = norm.pdf(d1)
        Nd1 = norm.cdf(d1)
        Nd2 = norm.cdf(d2)

        # BSM price
        bsm = np.where(
            call[active],
            s[active] * Nd1 - k[active] * np.exp(-r * t[active]) * Nd2,
            k[active] * np.exp(-r * t[active]) * norm.cdf(-d2) - s[active] * norm.cdf(-d1),
        )

        # Vega
        vega = s[active] * sqrtT * nd1

        # Newton step
        low_vega = vega < 1e-12
        step = np.where(low_vega, 0.0, (bsm - p[active]) / np.where(low_vega, 1.0, vega))
        sigma_new = sigma[active] - step

        # Clamp
        sigma_new = np.clip(sigma_new, 0.001, 10.0)

        # Check convergence
        newly_converged = np.abs(sigma_new - sigma[active]) < tol
        converged[active] = newly_converged
        sigma[active] = sigma_new

    # Bisection fallback for unconverged
    unconverged = ~converged
    if unconverged.any():
        lo = np.full(unconverged.sum(), 0.01)
        hi = np.full(unconverged.sum(), 5.0)
        pu = p[unconverged]
        su = s[unconverged]
        ku = k[unconverged]
        tu = t[unconverged]
        callu = call[unconverged]
        for _ in range(80):
            mid_sig = (lo + hi) / 2
            sqrtT = np.sqrt(tu)
            d1 = (np.log(su / ku) + (r + 0.5 * mid_sig**2) * tu) / (mid_sig * sqrtT)
            d2 = d1 - mid_sig * sqrtT
            bsm = np.where(
                callu,
                su * norm.cdf(d1) - ku * np.exp(-r * tu) * norm.cdf(d2),
                ku * np.exp(-r * tu) * norm.cdf(-d2) - su * norm.cdf(-d1),
            )
            too_high = bsm > pu
            hi = np.where(too_high, mid_sig, hi)
            lo = np.where(too_high, lo, mid_sig)
        sigma[unconverged] = (lo + hi) / 2

    result[valid] = sigma
    return result


# ── Vectorized Greeks (numpy) ─────────────────────────────────────────────

def compute_all_greeks(S: np.ndarray, K: np.ndarray, T: np.ndarray,
                       r: float, sigma: np.ndarray, is_call: np.ndarray) -> dict[str, np.ndarray]:
    """Compute all 1st/2nd/3rd order Greeks vectorized.

    Parameters are all numpy arrays of the same length.
    is_call: boolean array (True=call, False=put).

    Returns dict of arrays, one per Greek.
    """
    n = len(S)
    # Pre-allocate with NaN
    result = {name: np.full(n, np.nan) for name in [
        "delta", "gamma", "theta", "vega", "rho",
        "vanna", "charm", "vomma", "veta", "speed", "color",
        "ultima", "zomma",
    ]}

    # Mask valid rows
    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0) & np.isfinite(sigma)
    if not valid.any():
        return result

    s = S[valid]
    k = K[valid]
    t = T[valid]
    sig = sigma[valid]
    call = is_call[valid]

    sqrtT = np.sqrt(t)
    d1 = (np.log(s / k) + (r + 0.5 * sig**2) * t) / (sig * sqrtT)
    d2 = d1 - sig * sqrtT

    # Standard normal PDF and CDF
    nd1 = norm.pdf(d1)
    nd2 = norm.pdf(d2)
    Nd1 = norm.cdf(d1)
    Nd2 = norm.cdf(d2)
    Nmd1 = norm.cdf(-d1)
    Nmd2 = norm.cdf(-d2)

    exp_rT = np.exp(-r * t)

    # ── 1st Order ─────────────────────────────────────────────────────
    # Delta
    delta = np.where(call, Nd1, Nd1 - 1)

    # Gamma
    gamma = nd1 / (s * sig * sqrtT)

    # Theta (per year; divide by 365 for per-day)
    theta_common = -(s * nd1 * sig) / (2 * sqrtT)
    theta_call = theta_common - r * k * exp_rT * Nd2
    theta_put = theta_common + r * k * exp_rT * Nmd2
    theta = np.where(call, theta_call, theta_put) / 365.0  # per calendar day

    # Vega (per 1% vol move = divide by 100)
    vega = s * sqrtT * nd1 / 100.0

    # Rho (per 1% rate move = divide by 100)
    rho_call = k * t * exp_rT * Nd2 / 100.0
    rho_put = -k * t * exp_rT * Nmd2 / 100.0
    rho = np.where(call, rho_call, rho_put)

    # ── 2nd Order ─────────────────────────────────────────────────────
    # Vanna = d(delta)/d(sigma) = d(vega)/d(S)
    #       = -nd1 * d2 / sigma  (= vega/S * (1 - d1/(sigma*sqrtT)))
    vanna_val = -nd1 * d2 / sig

    # Charm = d(delta)/d(T)  (delta decay)
    charm_call = -nd1 * (2 * r * t - d2 * sig * sqrtT) / (2 * t * sig * sqrtT)
    charm_put = charm_call  # same for put (charm of put = charm of call)
    charm_val = charm_call / 365.0  # per calendar day

    # Vomma (Volga) = d(vega)/d(sigma) = d²(price)/d(sigma)²
    #              = vega * d1 * d2 / sigma
    vomma_val = s * sqrtT * nd1 * d1 * d2 / sig / 100.0  # per 1% vol

    # Veta = d(vega)/d(T)
    veta_val = -s * nd1 * sqrtT * (
        r * d1 / (sig * sqrtT) - (1 + d1 * d2) / (2 * t)
    ) / 365.0 / 100.0  # per day, per 1% vol

    # Speed = d(gamma)/d(S) = d³(price)/d(S)³
    speed_val = -gamma * (1 + d1 / (sig * sqrtT)) / s

    # Color = d(gamma)/d(T) (gamma decay)
    color_val = -nd1 / (2 * s * t * sig * sqrtT) * (
        2 * r * t + 1 + d1 * (2 * r * t - d2 * sig * sqrtT) / (sig * sqrtT)
    ) / 365.0  # per calendar day

    # ── 3rd Order ─────────────────────────────────────────────────────
    # Ultima = d(vomma)/d(sigma) = d³(price)/d(sigma)³
    ultima_val = -vomma_val / sig * (d1 * d2 - d1 / d2 - d2 / d1 + 1)

    # Zomma = d(gamma)/d(sigma) = d(vanna)/d(S)
    zomma_val = gamma * (d1 * d2 - 1) / sig

    # Store results
    result["delta"][valid] = delta
    result["gamma"][valid] = gamma
    result["theta"][valid] = theta
    result["vega"][valid] = vega
    result["rho"][valid] = rho
    result["vanna"][valid] = vanna_val
    result["charm"][valid] = charm_val
    result["vomma"][valid] = vomma_val
    result["veta"][valid] = veta_val
    result["speed"][valid] = speed_val
    result["color"][valid] = color_val
    result["ultima"][valid] = ultima_val
    result["zomma"][valid] = zomma_val

    return result


# ── Per-day computation ───────────────────────────────────────────────────

def compute_day(date_str: str) -> pd.DataFrame | None:
    """Compute full Greeks for one trading day. Returns DataFrame or None."""
    year = date_str[:4]

    # Load option quotes
    quotes_path = os.path.join(DATA_DIR, year, "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(quotes_path):
        return None
    quotes = pd.read_parquet(quotes_path)

    # Load SPX OHLC for spot prices
    spx_path = os.path.join(DATA_DIR, year, "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(spx_path):
        return None
    spx = pd.read_parquet(spx_path)

    # Build spot lookup: timestamp -> close price
    spot_map = dict(zip(spx["timestamp"], spx["close"].astype(float)))
    if not spot_map:
        return None

    # Map spot to each quote row
    quotes["spot"] = quotes["timestamp"].map(spot_map)
    quotes = quotes.dropna(subset=["spot"])
    if quotes.empty:
        return None

    # Compute mid price
    if "mid" not in quotes.columns:
        quotes["mid"] = (quotes["bid"] + quotes["ask"]) / 2.0

    # Filter: need positive mid price for IV computation
    quotes = quotes[quotes["mid"] > 0].copy()
    if quotes.empty:
        return None

    # Parse time from timestamp to compute T (time to expiry in years)
    # 0DTE: expires at 16:00 ET same day
    def minutes_to_close(ts: str) -> float:
        # timestamp format: YYYY-MM-DDTHH:MM:SS.sss
        hhmm = ts[11:16]
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        return max((16 * 60) - (h * 60 + m), 1)  # min 1 minute

    quotes["T"] = quotes["timestamp"].apply(
        lambda ts: minutes_to_close(ts) / (365.25 * 24 * 60)
    )

    # Compute IV vectorized (Newton-Raphson + bisection fallback)
    S = quotes["spot"].values
    K = quotes["strike"].values.astype(float)
    T = quotes["T"].values
    mid = quotes["mid"].values
    is_call = (quotes["right"].values == "C")

    iv_arr = implied_vol_vectorized(mid, S, K, T, RISK_FREE_RATE, is_call)

    quotes["iv"] = iv_arr

    # Compute all Greeks vectorized
    greeks = compute_all_greeks(S, K, T, RISK_FREE_RATE, iv_arr, is_call)

    for name, values in greeks.items():
        quotes[name] = values

    # Select output columns
    out_cols = [
        "right", "strike", "timestamp", "bid", "ask", "mid", "spot", "iv",
        "delta", "gamma", "theta", "vega", "rho",
        "vanna", "charm", "vomma", "veta", "speed", "color",
        "ultima", "zomma",
    ]
    result = quotes[out_cols].copy()
    result["strike"] = result["strike"].astype("int32")

    return result


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Compute full Greeks (1st/2nd/3rd order)")
    parser.add_argument("--year", type=int, default=None, help="Process single year")
    parser.add_argument("--date", type=str, default=None, help="Process single date (YYYYMMDD)")
    parser.add_argument("--force", action="store_true", help="Recompute even if output exists")
    args = parser.parse_args()

    # Discover dates to process
    if args.date:
        dates = [args.date]
    else:
        dates = []
        year_pattern = str(args.year) if args.year else "20*"
        for year_dir in sorted(glob.glob(os.path.join(DATA_DIR, year_pattern))):
            quotes_dir = os.path.join(year_dir, "option_quotes")
            if not os.path.isdir(quotes_dir):
                continue
            for f in sorted(os.listdir(quotes_dir)):
                if f.endswith(".parquet"):
                    dates.append(f.replace(".parquet", ""))

    # Filter to dates that need processing
    if not args.force:
        remaining = []
        for d in dates:
            year = d[:4]
            out_path = os.path.join(DATA_DIR, year, "option_greeks_full", f"{d}.parquet")
            if not os.path.exists(out_path):
                remaining.append(d)
        dates = remaining

    print(f"Full Greeks Calculator — {len(dates)} days to process")
    if not dates:
        print("Nothing to do.")
        return

    t0 = time.time()
    success = 0
    errors = 0

    for i, date_str in enumerate(dates):
        year = date_str[:4]
        out_dir = os.path.join(DATA_DIR, year, "option_greeks_full")
        out_path = os.path.join(out_dir, f"{date_str}.parquet")

        try:
            df = compute_day(date_str)
            if df is None or df.empty:
                errors += 1
                continue

            os.makedirs(out_dir, exist_ok=True)
            df.to_parquet(out_path, index=False)
            success += 1

            if (i + 1) % 10 == 0 or (i + 1) == len(dates):
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                eta = (len(dates) - i - 1) / rate if rate > 0 else 0
                valid_pct = df["iv"].notna().mean() * 100
                print(f"  [{i+1:>4}/{len(dates)}] {date_str} | "
                      f"{len(df):>5} rows ({valid_pct:.0f}% valid IV) | "
                      f"{rate:.1f} days/s | ETA {eta/60:.0f}m")

        except Exception as e:
            print(f"  [{i+1:>4}/{len(dates)}] {date_str} ERROR: {e}")
            errors += 1

    elapsed = time.time() - t0
    print(f"\nDone — {success} days computed, {errors} errors, {elapsed:.0f}s total")


if __name__ == "__main__":
    main()
