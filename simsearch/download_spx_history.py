#!/usr/bin/env python3
"""Download SPX daily OHLC history from Yahoo Finance.

Downloads ^GSPC (S&P 500) daily data from 1993 to present.
Saves to data/spx_daily_ohlc.csv for use by SimSearch.

This gives us ~30 years of Tier 1 feature history (vs 4 years from parquets).
"""
import os
import sys

import yfinance as yf
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def download_spx_daily():
    """Download SPX daily OHLC from Yahoo Finance."""
    print("Downloading ^GSPC daily OHLC from Yahoo Finance...")

    spx = yf.download("^GSPC", start="1993-01-01", end="2026-12-31",
                       auto_adjust=True, progress=True)

    if spx.empty:
        print("ERROR: No data returned from Yahoo Finance")
        sys.exit(1)

    # Handle multi-level columns from yfinance
    if isinstance(spx.columns, pd.MultiIndex):
        spx.columns = spx.columns.get_level_values(0)

    spx = spx.reset_index()
    spx = spx.rename(columns={
        "Date": "date",
        "Open": "spx_open",
        "High": "spx_high",
        "Low": "spx_low",
        "Close": "spx_close",
        "Volume": "spx_volume",
    })

    # Keep only OHLCV
    spx = spx[["date", "spx_open", "spx_high", "spx_low", "spx_close", "spx_volume"]]
    spx["date"] = pd.to_datetime(spx["date"])
    spx = spx.sort_values("date").reset_index(drop=True)

    # Filter out zero/invalid rows
    spx = spx[(spx["spx_open"] > 0) & (spx["spx_high"] > 0) &
              (spx["spx_low"] > 0) & (spx["spx_close"] > 0)]

    out_path = os.path.join(DATA_DIR, "spx_daily_ohlc.csv")
    spx.to_csv(out_path, index=False)

    print(f"\nSaved: {out_path}")
    print(f"Rows: {len(spx)}")
    print(f"Date range: {spx['date'].min().date()} to {spx['date'].max().date()}")
    print(f"Sample:")
    print(spx.head(3).to_string(index=False))
    print("...")
    print(spx.tail(3).to_string(index=False))

    return spx


if __name__ == "__main__":
    download_spx_daily()
