#!/usr/bin/env python3
"""SimSearch Feature Engineering — build daily feature vectors from all data sources.

Usage:
    python simsearch_features.py                    # build all features, save cache
    python simsearch_features.py --date 20250103    # single date
    python simsearch_features.py --force            # rebuild cache from scratch
    python simsearch_features.py --tier 1           # only Tier 1 features
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from simsearch_config import (
    DATA_DIR, REFS_DIR, FEATURE_CACHE, LOGS_DIR,
    TIER1_START, TIER2_START, TIER3_START,
    MARKET_HOLIDAYS, FOMC_DATES, TRIPLE_WITCHING_DATES,
)

warnings.filterwarnings("ignore", category=FutureWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ============================================================
#  Data Loaders
# ============================================================

def load_vix_history() -> pd.DataFrame:
    """Load full VIX history (1990-2026) from CSV."""
    path = os.path.join(DATA_DIR, "vix_history.csv")
    df = pd.read_csv(path, parse_dates=["DATE"])
    df = df.rename(columns={"DATE": "date", "OPEN": "vix_open", "HIGH": "vix_high",
                            "LOW": "vix_low", "CLOSE": "vix_close"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_spx_daily() -> pd.DataFrame:
    """Build SPX daily OHLC from yfinance CSV (1993+) + 1-minute parquets (2022+).

    Uses yfinance CSV as the primary source for long history.
    Overlays 1-minute parquet data for 2022+ (more precise, has bar_count).
    """
    # --- Source 1: yfinance CSV (1993-2026) ---
    yf_path = os.path.join(DATA_DIR, "spx_daily_ohlc.csv")
    if os.path.exists(yf_path):
        yf_df = pd.read_csv(yf_path, parse_dates=["date"])
        yf_df = yf_df[["date", "spx_open", "spx_high", "spx_low", "spx_close"]].copy()
        yf_df["bar_count"] = 0  # no intraday data from yfinance
        log.info("Loaded %d days from yfinance CSV (%s to %s)",
                 len(yf_df), yf_df["date"].min().date(), yf_df["date"].max().date())
    else:
        yf_df = pd.DataFrame()
        log.warning("No yfinance SPX CSV found at %s — run download_spx_history.py", yf_path)

    # --- Source 2: 1-minute parquets (2022+, more precise) ---
    parquet_rows = []
    for year_dir in sorted(glob.glob(os.path.join(DATA_DIR, "*/spx_ohlc"))):
        for fpath in sorted(glob.glob(os.path.join(year_dir, "*.parquet"))):
            try:
                df = pd.read_parquet(fpath)
                if df.empty:
                    continue
                valid = df[(df["open"] > 0) & (df["high"] > 0) &
                           (df["low"] > 0) & (df["close"] > 0)]
                if valid.empty:
                    continue
                date_str = os.path.basename(fpath).replace(".parquet", "")
                parquet_rows.append({
                    "date": pd.Timestamp(date_str),
                    "spx_open": valid["open"].iloc[0],
                    "spx_high": valid["high"].max(),
                    "spx_low": valid["low"].min(),
                    "spx_close": valid["close"].iloc[-1],
                    "bar_count": len(valid),
                })
            except Exception:
                continue

    pq_df = pd.DataFrame(parquet_rows)
    if not pq_df.empty:
        pq_df["date"] = pd.to_datetime(pq_df["date"])
        log.info("Loaded %d days from parquets (%s to %s)",
                 len(pq_df), pq_df["date"].min().date(), pq_df["date"].max().date())

    # --- Merge: parquets override yfinance where both exist ---
    if not yf_df.empty and not pq_df.empty:
        # Use yfinance for pre-parquet dates, parquets for the rest
        pq_dates = set(pq_df["date"])
        yf_only = yf_df[~yf_df["date"].isin(pq_dates)]
        combined = pd.concat([yf_only, pq_df], ignore_index=True)
    elif not yf_df.empty:
        combined = yf_df
    elif not pq_df.empty:
        combined = pq_df
    else:
        log.error("No SPX daily data available!")
        return pd.DataFrame()

    combined = combined.sort_values("date").reset_index(drop=True)
    combined["date"] = pd.to_datetime(combined["date"])
    log.info("Combined SPX daily: %d days (%s to %s)",
             len(combined), combined["date"].min().date(), combined["date"].max().date())
    return combined


def load_parquet_scalar(year: int, subdir: str, date_str: str, col: str) -> float | None:
    """Load a single scalar value from a daily parquet."""
    path = os.path.join(DATA_DIR, str(year), subdir, f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty or col not in df.columns:
            return None
        return float(df[col].iloc[0])
    except Exception:
        return None


def load_spx_intraday(date_str: str) -> pd.DataFrame | None:
    """Load 1-minute SPX bars for a single date."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # Filter out zero/invalid bars
        df = df[(df["open"] > 0) & (df["high"] > 0) &
                (df["low"] > 0) & (df["close"] > 0)]
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df if len(df) > 0 else None
    except Exception:
        return None


def load_gex(date_str: str) -> pd.DataFrame | None:
    """Load GEX data for a single date (aggregate rows only)."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "gex", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        # Aggregate rows have right == "AGG"
        agg = df[df["right"] == "AGG"].copy()
        if agg.empty:
            # Fallback: rows with net_gex populated
            agg = df[df["net_gex"].notna()].copy()
        if agg.empty:
            agg = df.copy()
        return agg
    except Exception:
        return None


def load_greeks_full(date_str: str) -> pd.DataFrame | None:
    """Load full Greeks for a single date."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "option_greeks_full", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return None


def load_oi(date_str: str) -> pd.DataFrame | None:
    """Load open interest for a single date."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "option_open_interest", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        return df
    except Exception:
        return None


# ============================================================
#  Tier 1: Price & Volatility Features (1990+)
# ============================================================

def compute_tier1(spx_daily: pd.DataFrame, vix_daily: pd.DataFrame) -> pd.DataFrame:
    """Compute Tier 1 features from daily SPX + VIX data.

    CRITICAL: All features use PRIOR-DAY data only (shifted by 1).
    Today's features describe the state entering the day, NOT what happened today.
    Today's gap is the only same-day feature (known at open).
    """
    log.info("Computing Tier 1 features (%d SPX days, %d VIX days)...",
             len(spx_daily), len(vix_daily))

    df = spx_daily.copy()
    df = df.merge(vix_daily[["date", "vix_open", "vix_high", "vix_low", "vix_close"]],
                  on="date", how="left")

    # ---- PRIOR-DAY returns (what happened yesterday, 2 days ago, etc.) ----
    # shift(1) means yesterday's value assigned to today
    prev_close = df["spx_close"].shift(1)
    df["spx_prev_return_1d"] = prev_close.pct_change(1)    # T-1 return
    df["spx_prev_return_2d"] = prev_close.pct_change(2)    # T-1 to T-3 return
    df["spx_prev_return_3d"] = prev_close.pct_change(3)
    df["spx_prev_return_5d"] = prev_close.pct_change(5)
    df["spx_prev_return_10d"] = prev_close.pct_change(10)
    df["spx_prev_return_20d"] = prev_close.pct_change(20)

    # Today's gap (known at open — only same-day feature)
    df["spx_gap_pct"] = (df["spx_open"] - df["spx_close"].shift(1)) / df["spx_close"].shift(1)

    # ---- PRIOR-DAY candle structure ----
    prev_range = df["spx_high"].shift(1) - df["spx_low"].shift(1)
    prev_open = df["spx_open"].shift(1)
    df["spx_prev_range_pct"] = prev_range / prev_open
    df["spx_prev_body_pct"] = (df["spx_close"].shift(1) - prev_open) / prev_open
    df["spx_prev_upper_wick_pct"] = np.where(
        prev_range > 0,
        (df["spx_high"].shift(1) - df[["spx_open", "spx_close"]].shift(1).max(axis=1)) / prev_range,
        0
    )
    df["spx_prev_lower_wick_pct"] = np.where(
        prev_range > 0,
        (df[["spx_open", "spx_close"]].shift(1).min(axis=1) - df["spx_low"].shift(1)) / prev_range,
        0
    )

    # ---- PRIOR-DAY VIX (yesterday's VIX close, known before today opens) ----
    df["vix_prev_close"] = df["vix_close"].shift(1)
    df["vix_prev_change_1d"] = df["vix_close"].shift(1).pct_change(1)
    df["vix_prev_change_5d"] = df["vix_close"].shift(1).pct_change(5)
    df["vix_prev_range_pct"] = ((df["vix_high"].shift(1) - df["vix_low"].shift(1))
                                 / df["vix_open"].shift(1).replace(0, np.nan))

    # VIX percentile and z-score (using yesterday's close and prior 20 days)
    vix_prev = df["vix_close"].shift(1)
    df["vix_percentile_20d"] = vix_prev.rolling(20).apply(
        lambda x: (x.iloc[-1] >= x).sum() / len(x) if len(x) == 20 else np.nan
    )
    vix_mean_20 = vix_prev.rolling(20).mean()
    vix_std_20 = vix_prev.rolling(20).std()
    df["vix_zscore_20d"] = (vix_prev - vix_mean_20) / vix_std_20

    # ---- Trend indicators (using yesterday's close) ----
    spx_prev = df["spx_close"].shift(1)
    sma_20 = spx_prev.rolling(20).mean()
    sma_50 = spx_prev.rolling(50).mean()
    df["spx_above_sma20"] = (spx_prev > sma_20).astype(float)
    df["spx_above_sma50"] = (spx_prev > sma_50).astype(float)

    # RSI 14 (using yesterday's close)
    delta = spx_prev.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["spx_rsi_14"] = 100 - (100 / (1 + rs))

    # Distance from 20-day high/low (using yesterday)
    high_20d = df["spx_high"].shift(1).rolling(20).max()
    low_20d = df["spx_low"].shift(1).rolling(20).min()
    df["spx_dist_from_20d_high"] = (spx_prev - high_20d) / high_20d
    df["spx_dist_from_20d_low"] = (spx_prev - low_20d) / low_20d

    # Realized vol (20-day, using yesterday's returns)
    prev_daily_ret = spx_prev.pct_change(1)
    df["realized_vol_20d"] = prev_daily_ret.rolling(20).std() * np.sqrt(252)

    # Calendar features (known before open)
    df["dow"] = df["date"].dt.dayofweek
    df["month"] = df["date"].dt.month
    df["is_month_end"] = df["date"].dt.is_month_end.astype(float)
    df["is_quarter_end"] = (df["month"].isin([3, 6, 9, 12]) & df["is_month_end"].astype(bool)).astype(float)

    date_strs = df["date"].dt.strftime("%Y%m%d")
    df["is_fomc"] = date_strs.isin(FOMC_DATES).astype(float)
    df["is_triple_witching"] = date_strs.isin(TRIPLE_WITCHING_DATES).astype(float)

    fomc_dates_sorted = sorted(pd.Timestamp(d) for d in FOMC_DATES)
    def days_to_fomc(dt):
        for fd in fomc_dates_sorted:
            if fd >= dt:
                return (fd - dt).days
        return 999
    df["days_to_next_fomc"] = df["date"].apply(days_to_fomc)

    # VIX-SPX correlation (20-day rolling, using prior-day data)
    vix_chg = df["vix_close"].shift(1).pct_change(1)
    spx_ret = df["spx_close"].shift(1).pct_change(1)
    df["vix_spx_corr_20d"] = vix_chg.rolling(20).corr(spx_ret)

    # ---- Cross-asset features (prior-day, available from yfinance CSVs) ----
    for asset, name in [("tlt", "tlt"), ("gld", "gld"), ("uup", "uup"), ("iwm", "iwm")]:
        csv_path = os.path.join(DATA_DIR, f"{name}_daily_ohlc.csv")
        if not os.path.exists(csv_path):
            continue
        adf = pd.read_csv(csv_path, parse_dates=["date"])
        close_col = f"{name}_close"
        if close_col not in adf.columns:
            continue
        # Prior-day return
        adf[f"{name}_prev_return_1d"] = adf[close_col].shift(1).pct_change(1)
        adf[f"{name}_prev_return_5d"] = adf[close_col].shift(1).pct_change(5)
        # Prior-day range
        high_col = f"{name}_high"
        low_col = f"{name}_low"
        open_col = f"{name}_open"
        if all(c in adf.columns for c in [high_col, low_col, open_col]):
            adf[f"{name}_prev_range_pct"] = (
                (adf[high_col].shift(1) - adf[low_col].shift(1)) /
                adf[open_col].shift(1).replace(0, np.nan)
            )
        df = df.merge(adf[["date"] + [c for c in adf.columns if c.startswith(f"{name}_prev_")]],
                       on="date", how="left")
        log.info("Added %s cross-asset features", name)

    # ---- Multi-day context (T-2 through T-5 lagged features) ----
    # These capture the recent trajectory, not just T-1 state
    spx_prev = df["spx_close"].shift(1)
    vix_prev = df["vix_close"].shift(1)

    # Was T-2 also a big range day? (volatility persistence)
    df["spx_prev2_range_pct"] = df["spx_prev_range_pct"].shift(1)
    df["spx_prev3_range_pct"] = df["spx_prev_range_pct"].shift(2)

    # VIX momentum over last 3 days
    df["vix_momentum_3d"] = (vix_prev - vix_prev.shift(3)) / vix_prev.shift(3).replace(0, np.nan)

    # Streak: consecutive up/down days
    daily_dir = np.sign(df["spx_prev_body_pct"])
    streak = daily_dir.copy()
    for i in range(1, len(streak)):
        if pd.notna(streak.iloc[i]) and pd.notna(streak.iloc[i-1]):
            if streak.iloc[i] == streak.iloc[i-1] and streak.iloc[i] != 0:
                streak.iloc[i] = streak.iloc[i-1] + np.sign(streak.iloc[i])
    df["spx_streak"] = streak

    # Range expansion/contraction: is range increasing or decreasing?
    df["range_change_2d"] = df["spx_prev_range_pct"] - df["spx_prev_range_pct"].shift(2)

    log.info("Tier 1: %d features for %d days", len([c for c in df.columns if c != "date"]), len(df))
    return df


# ============================================================
#  Tier 2: Intraday Shape Features (2013+)
# ============================================================

def compute_tier2_for_date(date_str: str, prev_date_str: str | None = None) -> dict | None:
    """Compute Tier 2 features: YESTERDAY's intraday shape (no look-ahead).

    All features describe the PRIOR day's intraday action, which is known
    before today's open. This tells us "what kind of day just happened"
    as context for predicting today.
    """
    # Use previous day's intraday data
    use_date = prev_date_str if prev_date_str else date_str
    bars = load_spx_intraday(use_date)
    if bars is None or len(bars) < 60:
        return None

    feats = {}
    bars["time"] = bars["timestamp"].dt.time

    from datetime import time as t
    morning = bars[(bars["time"] >= t(9, 30)) & (bars["time"] < t(10, 30))]
    midday = bars[(bars["time"] >= t(10, 30)) & (bars["time"] < t(13, 0))]
    afternoon = bars[(bars["time"] >= t(13, 0)) & (bars["time"] <= t(16, 0))]

    day_open = bars["open"].iloc[0]
    day_close = bars["close"].iloc[-1]
    day_high = bars["high"].max()
    day_low = bars["low"].min()

    # Prior day morning features
    if len(morning) > 0:
        feats["prev_morning_range_pct"] = (morning["high"].max() - morning["low"].min()) / day_open
        feats["prev_morning_direction"] = (morning["close"].iloc[-1] - morning["open"].iloc[0]) / day_open
    else:
        feats["prev_morning_range_pct"] = np.nan
        feats["prev_morning_direction"] = np.nan

    # Prior day midday features
    if len(midday) > 0:
        feats["prev_midday_range_pct"] = (midday["high"].max() - midday["low"].min()) / day_open
        feats["prev_midday_direction"] = (midday["close"].iloc[-1] - midday["open"].iloc[0]) / day_open
    else:
        feats["prev_midday_range_pct"] = np.nan
        feats["prev_midday_direction"] = np.nan

    # Prior day afternoon features
    if len(afternoon) > 0:
        feats["prev_afternoon_range_pct"] = (afternoon["high"].max() - afternoon["low"].min()) / day_open
        feats["prev_afternoon_direction"] = (afternoon["close"].iloc[-1] - afternoon["open"].iloc[0]) / day_open
    else:
        feats["prev_afternoon_range_pct"] = np.nan
        feats["prev_afternoon_direction"] = np.nan

    # High/low time buckets (0=morning, 1=midday, 2=afternoon)
    high_idx = bars["high"].idxmax()
    low_idx = bars["low"].idxmin()

    def time_bucket(tm):
        if tm < t(10, 30):
            return 0
        elif tm < t(13, 0):
            return 1
        else:
            return 2

    feats["prev_high_time_bucket"] = time_bucket(bars.loc[high_idx, "time"])
    feats["prev_low_time_bucket"] = time_bucket(bars.loc[low_idx, "time"])

    # Reversal magnitude
    if day_high > day_low:
        feats["prev_reversal_from_high"] = (day_high - day_close) / (day_high - day_low)
        feats["prev_reversal_from_low"] = (day_close - day_low) / (day_high - day_low)
    else:
        feats["prev_reversal_from_high"] = 0.5
        feats["prev_reversal_from_low"] = 0.5

    # Trend consistency
    day_dir = 1 if day_close > day_open else -1
    bars_5m = bars.iloc[::5].copy()
    if len(bars_5m) > 1:
        bar_dirs = np.sign(bars_5m["close"].diff().dropna())
        feats["prev_trend_consistency"] = (bar_dirs == day_dir).mean()
    else:
        feats["prev_trend_consistency"] = 0.5

    # Max 1-minute move
    bars["bar_return"] = bars["close"].pct_change().abs()
    feats["prev_max_1min_move_pct"] = bars["bar_return"].max()

    # Intraday vol ratio: first half vs second half
    half = len(bars) // 2
    if half > 10:
        ret = bars["close"].pct_change()
        feats["prev_vol_ratio_first_second_half"] = (
            ret.iloc[:half].std() / max(ret.iloc[half:].std(), 1e-10)
        )
    else:
        feats["prev_vol_ratio_first_second_half"] = 1.0

    # Close location in range
    if day_high > day_low:
        feats["prev_close_location_in_range"] = (day_close - day_low) / (day_high - day_low)
    else:
        feats["prev_close_location_in_range"] = 0.5

    return feats


# ============================================================
#  Tier 3: Options & Positioning Features (2022+)
# ============================================================

def compute_tier3_for_date(date_str: str, prev_date_str: str | None = None) -> dict | None:
    """Compute Tier 3 features for a single date from options/GEX data.

    CRITICAL: Uses PRIOR-DAY VIX term structure and GEX (known before open).
    IV at open (first timestamp) is the one same-day feature — available at 9:31.
    """
    year = int(date_str[:4])
    feats = {}

    # --- VIX term structure (PRIOR DAY — known before open) ---
    prev_ds = prev_date_str or date_str
    prev_year = int(prev_ds[:4])
    vix1d = load_parquet_scalar(prev_year, "vix1d", prev_ds, "vix1d_close")
    vix9d = load_parquet_scalar(prev_year, "vix9d", prev_ds, "vix9d_close")
    vvix = load_parquet_scalar(prev_year, "vvix", prev_ds, "vvix_close")
    vix = load_parquet_scalar(prev_year, "vix", prev_ds, "vix_close")

    if vix1d is not None and vix is not None and vix > 0:
        feats["vix1d_vix_ratio"] = vix1d / vix
    else:
        feats["vix1d_vix_ratio"] = np.nan

    if vix9d is not None and vix is not None and vix > 0:
        feats["vix9d_vix_ratio"] = vix9d / vix
    else:
        feats["vix9d_vix_ratio"] = np.nan

    feats["vvix_level"] = vvix if vvix is not None else np.nan

    # --- GEX regime (PRIOR DAY — dealer positioning known before open) ---
    gex_df = load_gex(prev_ds)
    if gex_df is not None and len(gex_df) > 0:
        # Get first timestamp's aggregate data
        first_ts = gex_df["timestamp"].min()
        first_agg = gex_df[gex_df["timestamp"] == first_ts]

        for col in ["net_gex", "call_gex", "put_gex", "net_vex", "gex_plus",
                     "call_wall", "put_wall", "gex_flip_strike", "top_gamma_strike"]:
            if col in first_agg.columns:
                vals = first_agg[col].dropna()
                feats[col] = float(vals.iloc[0]) if len(vals) > 0 else np.nan
            else:
                feats[col] = np.nan

        if "gex_plus_regime" in first_agg.columns:
            regimes = first_agg["gex_plus_regime"].dropna()
            regimes = regimes[regimes != ""]
            regime = str(regimes.iloc[0]) if len(regimes) > 0 else ""
            regime_map = {"stable": 3, "gex_cushion": 2, "vex_cushion": 1, "fragile": 0}
            feats["gex_regime_encoded"] = regime_map.get(regime, np.nan)

        if "spot" in first_agg.columns:
            spots = first_agg["spot"].dropna()
            spot = float(spots.iloc[0]) if len(spots) > 0 else 0
            if pd.notna(feats.get("call_wall")) and spot > 0:
                feats["call_wall_distance_pct"] = (feats["call_wall"] - spot) / spot
            else:
                feats["call_wall_distance_pct"] = np.nan
            if pd.notna(feats.get("put_wall")) and spot > 0:
                feats["put_wall_distance_pct"] = (spot - feats["put_wall"]) / spot
            else:
                feats["put_wall_distance_pct"] = np.nan
    else:
        for col in ["net_gex", "call_gex", "put_gex", "net_vex", "gex_plus",
                     "gex_regime_encoded", "call_wall_distance_pct", "put_wall_distance_pct"]:
            feats[col] = np.nan

    # --- IV features from Greeks ---
    greeks = load_greeks_full(date_str)
    if greeks is not None and len(greeks) > 0:
        # Get open snapshot (first timestamp)
        first_ts = greeks["timestamp"].min()
        open_greeks = greeks[greeks["timestamp"] == first_ts].copy()

        if len(open_greeks) > 0 and "spot" in open_greeks.columns:
            spot = open_greeks["spot"].iloc[0]

            # ATM IV at open
            atm_puts = open_greeks[(open_greeks["right"] == "P") &
                                   ((open_greeks["strike"] - spot).abs() <= 10)]
            atm_calls = open_greeks[(open_greeks["right"] == "C") &
                                    ((open_greeks["strike"] - spot).abs() <= 10)]

            if len(atm_puts) > 0 and "iv" in atm_puts.columns:
                feats["iv_atm_put_open"] = atm_puts["iv"].mean()
            else:
                feats["iv_atm_put_open"] = np.nan

            if len(atm_calls) > 0 and "iv" in atm_calls.columns:
                feats["iv_atm_call_open"] = atm_calls["iv"].mean()
            else:
                feats["iv_atm_call_open"] = np.nan

            feats["iv_atm_open"] = np.nanmean([feats.get("iv_atm_put_open", np.nan),
                                                feats.get("iv_atm_call_open", np.nan)])

            # IV skew: 25-delta equivalent (OTM put IV - OTM call IV)
            otm_puts = open_greeks[(open_greeks["right"] == "P") &
                                    (open_greeks["strike"] < spot - 20) &
                                    (open_greeks["strike"] > spot - 60)]
            otm_calls = open_greeks[(open_greeks["right"] == "C") &
                                     (open_greeks["strike"] > spot + 20) &
                                     (open_greeks["strike"] < spot + 60)]

            if len(otm_puts) > 0 and len(otm_calls) > 0:
                feats["iv_skew_25d"] = otm_puts["iv"].mean() - otm_calls["iv"].mean()
            else:
                feats["iv_skew_25d"] = np.nan

            # Gamma concentration: what % of total gamma is within 20 pts of spot
            if "gamma" in open_greeks.columns:
                total_gamma = open_greeks["gamma"].abs().sum()
                near_gamma = open_greeks[
                    (open_greeks["strike"] - spot).abs() <= 20
                ]["gamma"].abs().sum()
                feats["gamma_concentration"] = near_gamma / max(total_gamma, 1e-10)
            else:
                feats["gamma_concentration"] = np.nan
        else:
            for col in ["iv_atm_put_open", "iv_atm_call_open", "iv_atm_open",
                         "iv_skew_25d", "gamma_concentration"]:
                feats[col] = np.nan
    else:
        for col in ["iv_atm_put_open", "iv_atm_call_open", "iv_atm_open",
                     "iv_skew_25d", "gamma_concentration"]:
            feats[col] = np.nan

    # --- Open Interest (PRIOR DAY — OI measured at yesterday's close) ---
    oi_df = load_oi(prev_ds)
    if oi_df is not None and len(oi_df) > 0:
        call_oi = oi_df[oi_df["right"] == "C"]["open_interest"].sum()
        put_oi = oi_df[oi_df["right"] == "P"]["open_interest"].sum()
        feats["put_call_oi_ratio"] = put_oi / max(call_oi, 1) if call_oi > 0 else np.nan
        feats["total_oi"] = call_oi + put_oi
    else:
        feats["put_call_oi_ratio"] = np.nan
        feats["total_oi"] = np.nan

    return feats


# ============================================================
#  Outcome Features (what happened on this day — for validation)
# ============================================================

def compute_outcomes(spx_daily: pd.DataFrame) -> pd.DataFrame:
    """Compute outcome variables for each day (used as prediction targets)."""
    df = spx_daily[["date", "spx_open", "spx_high", "spx_low", "spx_close"]].copy()
    df["outcome_return"] = (df["spx_close"] - df["spx_open"]) / df["spx_open"]
    df["outcome_direction"] = np.sign(df["spx_close"] - df["spx_open"])
    df["outcome_range_pct"] = (df["spx_high"] - df["spx_low"]) / df["spx_open"]
    df["outcome_close_to_close"] = df["spx_close"].pct_change(1)

    # Next-day features (for prediction validation)
    df["outcome_next_return"] = df["outcome_return"].shift(-1)
    df["outcome_next_direction"] = df["outcome_direction"].shift(-1)
    df["outcome_next_range_pct"] = df["outcome_range_pct"].shift(-1)

    return df[["date", "outcome_return", "outcome_direction", "outcome_range_pct",
               "outcome_close_to_close", "outcome_next_return",
               "outcome_next_direction", "outcome_next_range_pct"]]


# ============================================================
#  Main: Build Feature Cache
# ============================================================

def build_all_features(max_tier: int = 3, force: bool = False,
                       single_date: str | None = None) -> pd.DataFrame:
    """Build feature vectors for all available dates."""

    # Load base data
    log.info("Loading SPX daily data from parquets...")
    spx_daily = load_spx_daily()
    log.info("Loaded %d SPX daily rows (%s to %s)",
             len(spx_daily), spx_daily["date"].min(), spx_daily["date"].max())

    log.info("Loading VIX history...")
    vix_daily = load_vix_history()
    log.info("Loaded %d VIX daily rows", len(vix_daily))

    # Tier 1
    features = compute_tier1(spx_daily, vix_daily)

    # Outcomes
    outcomes = compute_outcomes(spx_daily)
    features = features.merge(outcomes, on="date", how="left")

    # Build date->prev_date mapping (used by Tier 2 and Tier 3)
    all_dates = features["date"].dt.strftime("%Y%m%d").tolist()
    date_to_prev = {}
    for i in range(1, len(all_dates)):
        date_to_prev[all_dates[i]] = all_dates[i - 1]

    if max_tier >= 2:
        log.info("Computing Tier 2 features (prior-day intraday shape)...")
        tier2_eligible = features[features["date"] >= TIER2_START].copy()
        tier2_dates = tier2_eligible["date"].dt.strftime("%Y%m%d").tolist()
        if single_date:
            tier2_dates = [single_date] if single_date in tier2_dates else []

        tier2_rows = []
        for i, ds in enumerate(tier2_dates):
            if i % 100 == 0 and i > 0:
                log.info("  Tier 2 progress: %d/%d dates", i, len(tier2_dates))
            prev_ds = date_to_prev.get(ds)
            if prev_ds is None:
                continue
            feats = compute_tier2_for_date(ds, prev_date_str=prev_ds)
            if feats:
                feats["date"] = pd.Timestamp(ds)
                tier2_rows.append(feats)

        if tier2_rows:
            tier2_df = pd.DataFrame(tier2_rows)
            features = features.merge(tier2_df, on="date", how="left")
            log.info("Tier 2: added %d features for %d dates",
                     len(tier2_df.columns) - 1, len(tier2_df))

    if max_tier >= 3:
        log.info("Computing Tier 3 features (prior-day options & positioning)...")
        tier3_dates = features[features["date"] >= TIER3_START]["date"].dt.strftime("%Y%m%d").tolist()
        if single_date:
            tier3_dates = [single_date] if single_date in tier3_dates else []

        tier3_rows = []
        for i, ds in enumerate(tier3_dates):
            if i % 50 == 0 and i > 0:
                log.info("  Tier 3 progress: %d/%d dates", i, len(tier3_dates))
            prev_ds = date_to_prev.get(ds)
            feats = compute_tier3_for_date(ds, prev_date_str=prev_ds)
            if feats:
                feats["date"] = pd.Timestamp(ds)
                tier3_rows.append(feats)

        if tier3_rows:
            tier3_df = pd.DataFrame(tier3_rows)
            features = features.merge(tier3_df, on="date", how="left")
            log.info("Tier 3: added %d features for %d dates",
                     len(tier3_df.columns) - 1, len(tier3_df))

    # Sort and save
    features = features.sort_values("date").reset_index(drop=True)
    features.to_parquet(FEATURE_CACHE, index=False)
    log.info("Saved feature cache: %s (%d days, %d columns)",
             FEATURE_CACHE, len(features), len(features.columns))

    return features


def load_features() -> pd.DataFrame:
    """Load cached features, or build if missing."""
    if os.path.exists(FEATURE_CACHE):
        return pd.read_parquet(FEATURE_CACHE)
    return build_all_features()


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="SimSearch Feature Engineering")
    parser.add_argument("--date", type=str, help="Single date to compute (YYYYMMDD)")
    parser.add_argument("--tier", type=int, default=3, help="Max tier (1, 2, or 3)")
    parser.add_argument("--force", action="store_true", help="Rebuild from scratch")
    args = parser.parse_args()

    if args.force and os.path.exists(FEATURE_CACHE):
        os.remove(FEATURE_CACHE)

    df = build_all_features(max_tier=args.tier, single_date=args.date)

    # Summary
    print(f"\n{'='*60}")
    print(f"SimSearch Feature Cache Built")
    print(f"{'='*60}")
    print(f"Total days:     {len(df)}")
    print(f"Total features: {len(df.columns)}")
    print(f"Date range:     {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"Cache saved:    {FEATURE_CACHE}")

    # Feature completeness by tier
    tier1_cols = [c for c in df.columns if c.startswith(("spx_", "vix_", "realized_",
                  "dow", "month", "is_", "days_to"))]
    tier2_cols = [c for c in df.columns if c.startswith(("morning_", "midday_", "afternoon_",
                  "high_time", "low_time", "reversal_", "trend_", "max_1min",
                  "ema_14", "vol_ratio", "close_location"))]
    tier3_cols = [c for c in df.columns if c.startswith(("vix1d_", "vix9d_", "vvix_",
                  "net_gex", "call_gex", "put_gex", "net_vex", "gex_",
                  "iv_", "gamma_", "put_call_oi", "total_oi",
                  "call_wall", "put_wall"))]

    print(f"\nTier 1 features: {len(tier1_cols)} (price & vol, 1993+)")
    print(f"Tier 2 features: {len(tier2_cols)} (intraday shape, 2013+)")
    print(f"Tier 3 features: {len(tier3_cols)} (options & positioning, 2022+)")

    # NaN report
    nan_pcts = df[tier1_cols + tier2_cols + tier3_cols].isna().mean()
    high_nan = nan_pcts[nan_pcts > 0.5].sort_values(ascending=False)
    if len(high_nan) > 0:
        print(f"\nFeatures with >50% NaN:")
        for col, pct in high_nan.items():
            print(f"  {col}: {pct:.1%} missing")


if __name__ == "__main__":
    main()
