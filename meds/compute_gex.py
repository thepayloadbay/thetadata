#!/usr/bin/env python3
"""Gamma Exposure (GEX) Calculator.

Computes per-strike and aggregate GEX from:
  - option_greeks_full/{date}.parquet  (gamma, delta, spot per strike per minute)
  - option_open_interest/{date}.parquet (OI per strike, start of day)

Output: data/{year}/gex/{YYYYMMDD}.parquet
Columns:
  Per-strike at each timestamp:
    right, strike, timestamp, spot, gamma, oi, gex_strike
  Daily aggregates (one row per timestamp):
    timestamp, spot, total_gex, call_gex, put_gex, net_gex,
    gex_flip_strike, top_gamma_strike, dealer_position

GEX formula (standard):
  GEX_strike = gamma × OI × 100 × spot² × 0.01
  - Call OI: dealers are short gamma (customers buy calls) → positive GEX
  - Put OI: dealers are long gamma (customers buy puts) → negative GEX
  Net GEX = sum(call_GEX) - sum(put_GEX)

Positive net GEX → dealers hedge by selling rallies/buying dips → mean-reverting
Negative net GEX → dealers hedge by buying rallies/selling dips → trending

Usage:
  python compute_gex.py                    # all dates missing gex
  python compute_gex.py --year 2024        # specific year
  python compute_gex.py --date 20241231    # single date
  python compute_gex.py --force            # recompute all
"""
from __future__ import annotations

import argparse
import glob
import os
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

CONTRACT_MULTIPLIER = 100  # SPX options


def compute_day_gex(date_str: str) -> pd.DataFrame | None:
    """Compute GEX for one trading day."""
    year = date_str[:4]

    # Load full Greeks
    greeks_path = os.path.join(DATA_DIR, year, "option_greeks_full", f"{date_str}.parquet")
    if not os.path.exists(greeks_path):
        return None
    greeks = pd.read_parquet(greeks_path, columns=["right", "strike", "timestamp", "spot", "gamma", "delta", "iv"])

    # Load OI
    oi_path = os.path.join(DATA_DIR, year, "option_open_interest", f"{date_str}.parquet")
    if not os.path.exists(oi_path):
        return None
    oi = pd.read_parquet(oi_path, columns=["right", "strike", "open_interest"])

    # Drop rows with no gamma
    greeks = greeks.dropna(subset=["gamma"])
    if greeks.empty:
        return None

    # Merge OI onto greeks (OI is one value per strike/right for the day)
    merged = greeks.merge(oi, on=["right", "strike"], how="left")
    merged["open_interest"] = merged["open_interest"].fillna(0).astype(int)

    # Per-strike GEX: gamma × OI × multiplier × spot² × 0.01
    # Convention: call GEX positive (dealers short gamma), put GEX negative
    spot = merged["spot"]
    gex_raw = merged["gamma"] * merged["open_interest"] * CONTRACT_MULTIPLIER * spot**2 * 0.01

    # Sign: calls positive, puts negative
    merged["gex_strike"] = np.where(merged["right"] == "C", gex_raw, -gex_raw)
    merged["oi"] = merged["open_interest"]

    # ── Per-strike output ─────────────────────────────────────────────
    strike_cols = ["right", "strike", "timestamp", "spot", "gamma", "delta", "iv", "oi", "gex_strike"]
    strike_df = merged[strike_cols].copy()

    # ── Aggregate per timestamp ───────────────────────────────────────
    agg = merged.groupby("timestamp").agg(
        spot=("spot", "first"),
        call_gex=("gex_strike", lambda x: x[merged.loc[x.index, "right"] == "C"].sum()),
        put_gex=("gex_strike", lambda x: x[merged.loc[x.index, "right"] == "P"].sum()),
        total_gex=("gex_strike", "sum"),
    ).reset_index()

    agg["net_gex"] = agg["call_gex"] + agg["put_gex"]

    # GEX flip point: strike where cumulative GEX changes sign
    # Top gamma strike: strike with highest absolute GEX
    flip_strikes = []
    top_strikes = []
    for ts in agg["timestamp"]:
        ts_data = strike_df[strike_df["timestamp"] == ts].copy()
        if ts_data.empty:
            flip_strikes.append(np.nan)
            top_strikes.append(np.nan)
            continue

        # Sort by strike
        ts_data = ts_data.sort_values("strike")
        cumgex = ts_data["gex_strike"].cumsum()
        # Flip = strike where cumulative GEX crosses zero
        sign_changes = np.where(np.diff(np.sign(cumgex.values)))[0]
        if len(sign_changes) > 0:
            flip_strikes.append(int(ts_data.iloc[sign_changes[0]]["strike"]))
        else:
            flip_strikes.append(np.nan)

        # Top gamma = highest absolute GEX
        idx = ts_data["gex_strike"].abs().idxmax()
        top_strikes.append(int(ts_data.loc[idx, "strike"]))

    agg["gex_flip_strike"] = flip_strikes
    agg["top_gamma_strike"] = top_strikes

    # Dealer position: positive = long gamma (mean-reverting), negative = short gamma (trending)
    agg["dealer_position"] = np.where(agg["net_gex"] > 0, "long_gamma", "short_gamma")

    # ── Combine into single output ────────────────────────────────────
    # Store both per-strike and aggregate in one file
    # Per-strike rows have right/strike populated; aggregate rows have right=None
    agg["right"] = "AGG"
    agg["strike"] = 0
    agg["gamma"] = np.nan
    agg["delta"] = np.nan
    agg["iv"] = np.nan
    agg["oi"] = 0
    agg = agg.rename(columns={"total_gex": "gex_strike"})

    # Keep only needed cols in same order
    agg_out = agg[["right", "strike", "timestamp", "spot", "gamma", "delta", "iv", "oi", "gex_strike",
                    "call_gex", "put_gex", "net_gex", "gex_flip_strike", "top_gamma_strike", "dealer_position"]].copy()

    # For strike_df, add placeholder columns
    for col in ["call_gex", "put_gex", "net_gex", "gex_flip_strike", "top_gamma_strike", "dealer_position"]:
        strike_df[col] = np.nan if col != "dealer_position" else ""

    combined = pd.concat([strike_df, agg_out], ignore_index=True)
    combined["strike"] = combined["strike"].astype("int32")

    return combined


def main():
    parser = argparse.ArgumentParser(description="Compute Gamma Exposure (GEX)")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    # Discover dates
    if args.date:
        dates = [args.date]
    else:
        dates = []
        year_pattern = str(args.year) if args.year else "20*"
        for year_dir in sorted(glob.glob(os.path.join(DATA_DIR, year_pattern))):
            greeks_dir = os.path.join(year_dir, "option_greeks_full")
            if not os.path.isdir(greeks_dir):
                continue
            for f in sorted(os.listdir(greeks_dir)):
                if f.endswith(".parquet"):
                    dates.append(f.replace(".parquet", ""))

    # Filter
    if not args.force:
        remaining = []
        for d in dates:
            out = os.path.join(DATA_DIR, d[:4], "gex", f"{d}.parquet")
            if not os.path.exists(out):
                remaining.append(d)
        dates = remaining

    print(f"GEX Calculator — {len(dates)} days to process")
    if not dates:
        print("Nothing to do.")
        return

    t0 = time.time()
    success = 0
    errors = 0

    for i, date_str in enumerate(dates):
        year = date_str[:4]
        out_dir = os.path.join(DATA_DIR, year, "gex")
        out_path = os.path.join(out_dir, f"{date_str}.parquet")

        try:
            df = compute_day_gex(date_str)
            if df is None or df.empty:
                errors += 1
                continue

            os.makedirs(out_dir, exist_ok=True)
            df.to_parquet(out_path, index=False)
            success += 1

            if (i + 1) % 50 == 0 or (i + 1) == len(dates):
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                eta = (len(dates) - i - 1) / rate if rate > 0 else 0

                # Show summary for this day
                agg = df[df["right"] == "AGG"]
                if not agg.empty:
                    first = agg.iloc[0]
                    print(f"  [{i+1:>4}/{len(dates)}] {date_str} | "
                          f"net_gex={first['net_gex']:>12,.0f} | "
                          f"dealer={first['dealer_position']} | "
                          f"{rate:.1f} days/s | ETA {eta/60:.0f}m")

        except Exception as e:
            print(f"  [{i+1:>4}/{len(dates)}] {date_str} ERROR: {e}")
            errors += 1

    elapsed = time.time() - t0
    print(f"\nDone — {success} days, {errors} errors, {elapsed:.0f}s")


if __name__ == "__main__":
    main()
