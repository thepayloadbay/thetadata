#!/usr/bin/env python3
"""Convert TradingView CSV exports into the parquet format used by data/.

Creates per-day parquet files matching existing conventions:
  data/{year}/vix1d/{YYYYMMDD}.parquet       — columns: date, vix1d_close
  data/{year}/vix9d/{YYYYMMDD}.parquet       — columns: date, vix9d_close
  data/{year}/vvix/{YYYYMMDD}.parquet        — columns: date, vvix_close
  data/{year}/vix1d_ohlc/{YYYYMMDD}.parquet  — 1-min bars (if 1m CSV available)
  data/{year}/vix9d_ohlc/{YYYYMMDD}.parquet  — 1-min bars
  data/{year}/vvix_ohlc/{YYYYMMDD}.parquet   — 1-min bars

Usage:
  python convert_tradingview.py                          # convert all TV CSVs found
  python convert_tradingview.py --dry-run                # show what would be created
  python convert_tradingview.py --symbols vix1d vvix     # only these symbols
"""
from __future__ import annotations

import argparse
import os
import pathlib

import pandas as pd

# -- Paths -----------------------------------------------------------------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TV_DIR = PROJECT_ROOT  # TradingView CSVs sit at repo root

# -- Column mapping: TradingView column name -> (subfolder, value_col) ------
TV_COLUMNS = {
    "VIX1D · CBOE: close": ("vix1d", "vix1d_close"),
    "VIX9D · CBOE: close": ("vix9d", "vix9d_close"),
    "VVIX · CBOE: close":  ("vvix",  "vvix_close"),
    "VIX · TVC: close":    ("vix_tv", "vix_tv_close"),  # separate from ThetaData VIX
}

# -- CSV files to process --------------------------------------------------
TV_FILES = {
    "1D":  TV_DIR / "Tradingview_SP_SPX, 1D.csv",
    "1m":  TV_DIR / "Tradingview_SP_SPX, 1m.csv",
    "15m": TV_DIR / "Tradingview_SP_SPX, 15m.csv",
}


def convert_daily(csv_path: pathlib.Path, symbols: set[str] | None, dry_run: bool) -> int:
    """Convert daily CSV into per-day parquet files (vix-style: date + close)."""
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y%m%d")
    created = 0

    for tv_col, (subfolder, val_col) in TV_COLUMNS.items():
        if symbols and subfolder not in symbols:
            continue
        if tv_col not in df.columns:
            continue

        valid = df[df[tv_col].notna()].copy()
        if valid.empty:
            continue

        for _, row in valid.iterrows():
            date_str = row["date"]
            year = date_str[:4]
            out_dir = DATA_DIR / year / subfolder
            out_path = out_dir / f"{date_str}.parquet"

            if out_path.exists():
                continue

            if dry_run:
                print(f"  [dry-run] {out_path}")
                created += 1
                continue

            out_dir.mkdir(parents=True, exist_ok=True)
            out_df = pd.DataFrame({"date": [date_str], val_col: [float(row[tv_col])]})
            out_df.to_parquet(out_path, index=False)
            created += 1

    return created


def convert_intraday(csv_path: pathlib.Path, symbols: set[str] | None, dry_run: bool) -> int:
    """Convert intraday CSV into per-day OHLC parquet files (vix_ohlc-style)."""
    df = pd.read_csv(csv_path)
    df["datetime"] = pd.to_datetime(df["time"], utc=True)
    df["date_str"] = df["datetime"].dt.strftime("%Y%m%d")
    df["timestamp"] = df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S.000")
    created = 0

    for tv_col, (subfolder, val_col) in TV_COLUMNS.items():
        if symbols and subfolder not in symbols:
            continue
        if tv_col not in df.columns:
            continue

        ohlc_subfolder = f"{subfolder}_ohlc"
        valid = df[df[tv_col].notna()].copy()
        if valid.empty:
            continue

        for date_str, group in valid.groupby("date_str"):
            year = date_str[:4]
            out_dir = DATA_DIR / year / ohlc_subfolder
            out_path = out_dir / f"{date_str}.parquet"

            if out_path.exists():
                continue

            if dry_run:
                print(f"  [dry-run] {out_path} ({len(group)} bars)")
                created += 1
                continue

            out_dir.mkdir(parents=True, exist_ok=True)
            # Match vix_ohlc schema: volume, high, low, vwap, count, close, open, timestamp
            # For TV data we only have close values, so open=high=low=close
            closes = group[tv_col].astype(float)
            out_df = pd.DataFrame({
                "volume": 0,
                "high": closes.values,
                "low": closes.values,
                "vwap": 0.0,
                "count": 0,
                "close": closes.values,
                "open": closes.values,
                "timestamp": group["timestamp"].values,
            })
            out_df.to_parquet(out_path, index=False)
            created += 1

    return created


def main():
    parser = argparse.ArgumentParser(description="Convert TradingView CSVs to data/ parquet format")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created without writing")
    parser.add_argument("--symbols", nargs="+", help="Only convert these symbols (e.g., vix1d vvix)")
    args = parser.parse_args()

    symbols = set(args.symbols) if args.symbols else None
    total = 0

    # Daily CSV -> per-day close parquets
    daily_csv = TV_FILES.get("1D")
    if daily_csv and daily_csv.exists():
        print(f"Processing daily: {daily_csv.name}")
        n = convert_daily(daily_csv, symbols, args.dry_run)
        print(f"  -> {n} files {'would be created' if args.dry_run else 'created'}")
        total += n

    # Intraday CSVs -> per-day OHLC parquets
    for label in ("1m", "15m"):
        csv_path = TV_FILES.get(label)
        if csv_path and csv_path.exists():
            print(f"Processing {label}: {csv_path.name}")
            n = convert_intraday(csv_path, symbols, args.dry_run)
            print(f"  -> {n} files {'would be created' if args.dry_run else 'created'}")
            total += n

    print(f"\nTotal: {total} files {'would be created' if args.dry_run else 'created'}")


if __name__ == "__main__":
    main()
