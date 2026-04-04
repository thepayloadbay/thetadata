"""LastFive — Backtest Engine.

Data loading, strike selection, and day-by-day simulation.
Uses local ThetaData parquets (same data as MEDS/Zenith projects).

Strategy: At 15:55, sell a short call spread on SPXW 0DTE.
  - Short call: lowest strike >= spot + MIN_SHORT_DISTANCE (rounded up to STRIKE_STEP)
  - Long call: short_strike + SPREAD_WIDTH
  - Size: floor(RISK_AMOUNT / max_loss_per_contract)
  - Exit: 16:00 (expiration) — spread settles at intrinsic value
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import math
import os
from datetime import datetime, time

import numpy as np
import pandas as pd

import lastfive_config as _cfg
from lastfive_config import *

# ---------------------------------------------
#  LOGGING
# ---------------------------------------------
def setup_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("lastfive")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_FILE, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info(f"Log file  : {LOG_FILE}")
    return logger

logger = setup_logger()


# ---------------------------------------------
#  HELPERS
# ---------------------------------------------
def init_log_file():
    with open(_cfg.SAVE_FILE, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_COLS).writeheader()


def append_trade(row: dict):
    with open(_cfg.SAVE_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore").writerow(row)


# ---------------------------------------------
#  DATA LOADING
# ---------------------------------------------
_DAILY_INDICATORS: dict = {}  # date_str -> {vix_close, spx_close, ...}


def build_daily_indicators() -> dict:
    """Load SPX daily OHLC + VIX daily close for all available dates.

    Returns dict[date_str -> indicator dict].
    """
    global _DAILY_INDICATORS

    # SPX daily bars from 1-min aggregation
    rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
            if df.empty or len(df) < 5:
                continue
            valid = df[(df["high"] > 0) & (df["close"] > 0)]
            if valid.empty:
                continue
            rows.append({
                "date":   date_str,
                "open":   float(valid["open"].iloc[0]),
                "high":   float(valid["high"].max()),
                "low":    float(valid["low"].min()),
                "close":  float(valid["close"].iloc[-1]),
            })
        except Exception:
            pass

    if not rows:
        logger.error("No SPX OHLC data found")
        return {}

    d = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)
    d["day_return_pct"] = (d["close"] - d["prev_close"]) / d["prev_close"] * 100

    # VIX daily closes
    vix_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix_close" in vdf.columns:
                vix_rows.append({"date": str(vdf["date"].iloc[0]), "vix_close": float(vdf["vix_close"].iloc[0])})
        except Exception:
            pass

    # Also load VIX from vix_history.csv for older dates
    vix_hist_path = os.path.join(DATA_DIR, "vix_history.csv")
    if os.path.exists(vix_hist_path):
        vh = pd.read_csv(vix_hist_path, parse_dates=["DATE"])
        vh["date"] = vh["DATE"].dt.strftime("%Y%m%d")
        for _, row in vh.iterrows():
            vix_rows.append({"date": row["date"], "vix_close": float(row["CLOSE"])})

    if vix_rows:
        vix_df = pd.DataFrame(vix_rows).drop_duplicates("date", keep="last").sort_values("date")
        vix_df["prev_vix"] = vix_df["vix_close"].shift(1)
        vix_df["vix_change_pct"] = (vix_df["vix_close"] - vix_df["prev_vix"]) / vix_df["prev_vix"] * 100
        d = d.merge(vix_df[["date", "vix_close", "prev_vix", "vix_change_pct"]], on="date", how="left")

    # Williams VIX Fix (synthetic VIX from price only)
    d["wvf_highest_close_22"] = d["close"].rolling(22).max()
    d["wvf"] = ((d["wvf_highest_close_22"] - d["low"]) / d["wvf_highest_close_22"]) * 100
    d["wvf_sma20"] = d["wvf"].rolling(20).mean()
    d["wvf_std20"] = d["wvf"].rolling(20).std()
    d["wvf_upper_bb"] = d["wvf_sma20"] + 2.0 * d["wvf_std20"]
    d["wvf_pctrank"] = d["wvf"].rolling(50).apply(lambda x: (x.iloc[-1] > x.iloc[:-1]).mean() * 100 if len(x) > 1 else 50, raw=False)

    # VIX9D daily closes
    vix9d_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix9d", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            date_str_v = os.path.basename(fpath).replace(".parquet", "")
            col = "vix9d_close" if "vix9d_close" in vdf.columns else "close" if "close" in vdf.columns else None
            if col and not vdf.empty:
                vix9d_rows.append({"date": date_str_v, "vix9d_close": float(vdf[col].iloc[0])})
        except Exception:
            pass
    if vix9d_rows:
        v9 = pd.DataFrame(vix9d_rows).drop_duplicates("date", keep="last")
        d = d.merge(v9, on="date", how="left")
    else:
        d["vix9d_close"] = np.nan

    # VVIX daily closes
    vvix_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vvix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            date_str_v = os.path.basename(fpath).replace(".parquet", "")
            col = "vvix_close" if "vvix_close" in vdf.columns else "close" if "close" in vdf.columns else None
            if col and not vdf.empty:
                vvix_rows.append({"date": date_str_v, "vvix_close": float(vdf[col].iloc[0])})
        except Exception:
            pass
    if vvix_rows:
        vv = pd.DataFrame(vvix_rows).drop_duplicates("date", keep="last")
        d = d.merge(vv, on="date", how="left")
    else:
        d["vvix_close"] = np.nan

    # Build indicator dict
    indicators = {}
    for _, row in d.iterrows():
        indicators[row["date"]] = {
            "open":            row.get("open"),
            "high":            row.get("high"),
            "low":             row.get("low"),
            "close":           row.get("close"),
            "prev_close":      row.get("prev_close"),
            "day_return_pct":  row.get("day_return_pct"),
            "vix_close":       row.get("vix_close"),
            "prev_vix":        row.get("prev_vix"),
            "vix_change_pct":  row.get("vix_change_pct"),
            "vix9d_close":     row.get("vix9d_close"),
            "vvix_close":      row.get("vvix_close"),
            "wvf":             row.get("wvf") if pd.notna(row.get("wvf")) else None,
            "wvf_upper_bb":    row.get("wvf_upper_bb") if pd.notna(row.get("wvf_upper_bb")) else None,
            "wvf_pctrank":     row.get("wvf_pctrank") if pd.notna(row.get("wvf_pctrank")) else None,
        }

    _DAILY_INDICATORS = indicators
    return indicators


def _load_option_quotes(date_str: str) -> pd.DataFrame | None:
    """Load option_quotes parquet for a given date. Returns DataFrame or None."""
    path = os.path.join(DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    return df


def _load_spx_1min(date_str: str) -> pd.DataFrame | None:
    """Load SPX 1-min OHLC for a given date."""
    path = os.path.join(DATA_DIR, date_str[:4], "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def _load_vix_1min(date_str: str) -> pd.DataFrame | None:
    """Load VIX 1-min OHLC for a given date."""
    path = os.path.join(DATA_DIR, date_str[:4], "vix_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def _get_spx_at_time(spx_df: pd.DataFrame, time_str: str, field: str = "close") -> float | None:
    """Get SPX price at a specific time from 1-min bars.

    field: 'close' for settlement/touch, 'open' for entry strike selection.
    Strike selection MUST use bar OPEN (not CLOSE) to avoid look-ahead bias.
    Verified: bar OPEN gives 99.8% strike match with Option Alpha.
    """
    matches = spx_df[spx_df["time_str"] == time_str]
    if matches.empty:
        # Try matching just HH:MM
        hhmm = time_str[:5]
        matches = spx_df[spx_df["hhmm"] == hhmm]
    if matches.empty:
        return None
    return float(matches[field].iloc[0])


def _compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """Compute SPX return from 13:00 to 15:54 (data available before 15:55 entry)."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start_price = float(afternoon["close"].iloc[0])
    end_price = float(afternoon["close"].iloc[-1])
    if start_price <= 0:
        return None
    return (end_price - start_price) / start_price * 100


def _get_vix_at_1550(date_str: str) -> float | None:
    """Get VIX close at 15:50 (available before 15:55 entry)."""
    vix_df = _load_vix_1min(date_str)
    if vix_df is None:
        return None
    bar = vix_df[vix_df["hhmm"] == "15:50"]
    if bar.empty:
        return None
    val = float(bar["close"].iloc[0])
    return val if val > 0 else None


def _compute_range_budget(spx_df: pd.DataFrame, vix_open: float | None, spx_open: float | None) -> float | None:
    """Compute what fraction of VIX/16 expected daily range has been consumed by 15:54.

    range_budget_used = (high_so_far - low_so_far) / expected_daily_range
    Values > 0.9 = most of expected move used = safe for credit spreads.
    Values < 0.5 = lots of move budget remaining = risky.
    """
    if spx_df is None or spx_df.empty or vix_open is None or spx_open is None:
        return None
    if vix_open <= 0 or spx_open <= 0:
        return None

    spx_df = spx_df.copy()
    if "hhmm" not in spx_df.columns:
        spx_df["hhmm"] = pd.to_datetime(spx_df["timestamp"]).dt.strftime("%H:%M")

    bars = spx_df[spx_df["hhmm"] <= "15:54"]
    if len(bars) < 10:
        return None

    actual_range = float(bars["high"].max()) - float(bars["low"].min())
    expected_range = spx_open * (vix_open / 15.87 / 100.0)
    if expected_range <= 0:
        return None

    return actual_range / expected_range


def _compute_kaufman_er(spx_df: pd.DataFrame, n: int = 10) -> float | None:
    """Compute Kaufman Efficiency Ratio on 1-min SPX bars ending at 15:54.

    ER = abs(close[now] - close[now-n]) / sum(abs(close[i] - close[i-1]))
    ER near 0 = choppy (safe for credit spreads)
    ER near 1 = trending (dangerous)
    """
    if spx_df is None or spx_df.empty:
        return None

    spx_df = spx_df.copy()
    if "hhmm" not in spx_df.columns:
        spx_df["hhmm"] = pd.to_datetime(spx_df["timestamp"]).dt.strftime("%H:%M")

    # Get bars up to 15:54
    bars = spx_df[spx_df["hhmm"] <= "15:54"].tail(n + 1)
    if len(bars) < n + 1:
        return None

    closes = bars["close"].values.astype(float)
    direction = abs(closes[-1] - closes[0])
    volatility = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))

    if volatility == 0:
        return 0.0
    return direction / volatility


def _compute_parkinson_vol(spx_df: pd.DataFrame, window_start: str = "15:25", window_end: str = "15:54") -> float | None:
    """Compute Parkinson volatility from intraday OHLC bars.

    Parkinson (1980): sigma = sqrt(1/(4*n*ln2) * sum(ln(H/L)^2))
    5x more efficient than close-to-close volatility. Uses only high/low.
    Returns annualized vol as a percentage, or None if insufficient data.
    """
    if spx_df is None or spx_df.empty:
        return None

    spx_df = spx_df.copy()
    if "hhmm" not in spx_df.columns:
        spx_df["hhmm"] = pd.to_datetime(spx_df["timestamp"]).dt.strftime("%H:%M")

    window = spx_df[(spx_df["hhmm"] >= window_start) & (spx_df["hhmm"] <= window_end)]
    if len(window) < 10:
        return None

    import math
    ln_hl_sq = [(math.log(float(r["high"]) / float(r["low"]))) ** 2
                for _, r in window.iterrows() if float(r["low"]) > 0 and float(r["high"]) > 0]
    if not ln_hl_sq:
        return None

    n = len(ln_hl_sq)
    parkinson_var = sum(ln_hl_sq) / (4 * n * math.log(2))
    # Return as annualized percentage (scale from 1-min to annual)
    # 1-min -> daily: sqrt(390), daily -> annual: sqrt(252)
    parkinson_vol = math.sqrt(parkinson_var) * math.sqrt(390) * math.sqrt(252) * 100
    return parkinson_vol


def _compute_spread_compression(spx_df: pd.DataFrame) -> dict | None:
    """Compute bar spread (range) compression metrics for 15:45-15:54.

    Inspired by VSA effort-vs-result: narrow bars = compressed = institutional absorption.
    Returns dict with:
      - avg_spread: average bar range (high-low) in last 10 min before entry
      - spread_pctile: percentile of avg_spread vs full-day bars
      - max_bar_range: largest single bar range in the window
      - is_compressed: True if avg_spread is below 30th percentile of day
    """
    if spx_df is None or spx_df.empty:
        return None

    spx_df = spx_df.copy()
    if "hhmm" not in spx_df.columns:
        spx_df["hhmm"] = pd.to_datetime(spx_df["timestamp"]).dt.strftime("%H:%M")

    # Full day bar ranges (for percentile context)
    spx_df["bar_range"] = spx_df["high"] - spx_df["low"]
    day_ranges = spx_df["bar_range"].dropna()
    if len(day_ranges) < 20:
        return None

    # Window: 15:45-15:54 (10 bars before entry)
    window = spx_df[(spx_df["hhmm"] >= "15:45") & (spx_df["hhmm"] <= "15:54")]
    if len(window) < 5:
        return None

    avg_spread = float(window["bar_range"].mean())
    max_bar = float(window["bar_range"].max())

    # Percentile of window avg vs all day bars
    pctile = float((day_ranges < avg_spread).mean() * 100)

    return {
        "avg_spread": avg_spread,
        "spread_pctile": pctile,
        "max_bar_range": max_bar,
        "is_compressed": pctile < 30.0,
    }


def _compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """Compute SPX 30-min momentum (15:25 to 15:54) — available before 15:55 entry."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    start_price = float(bars["close"].iloc[0])
    end_price = float(bars["close"].iloc[-1])
    if start_price <= 0:
        return None
    return (end_price - start_price) / start_price * 100


def _get_adaptive_distance(vix_1550: float | None, spot: float | None = None) -> float:
    """Return strike distance based on VIX level.

    Two modes:
    - "buckets" (default): discrete VIX buckets (Rule C3)
    - "vix16": continuous formula: dist = mult * SPX * (VIX/15.87/100) * sqrt(5/390)
    """
    if not _cfg.ENABLE_VIX_ADAPTIVE or vix_1550 is None:
        return _cfg.MIN_SHORT_DISTANCE

    # VIX/16 continuous mode
    if _cfg.VIX_ADAPTIVE_MODE == "vix16" and spot is not None and spot > 0:
        import math as _math
        expected_move = spot * (vix_1550 / 100.0) / 15.87 * _math.sqrt(5.0 / 390.0)
        dist = _math.ceil(_cfg.VIX16_MULTIPLIER * expected_move / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP
        dist = max(dist, _cfg.VIX16_MIN_DIST)
        return float(dist)

    # Hybrid mode: use buckets but enforce VIX/16 floor
    if _cfg.VIX_ADAPTIVE_MODE == "hybrid" and spot is not None and spot > 0:
        import math as _math
        expected_move = spot * (vix_1550 / 100.0) / 15.87 * _math.sqrt(5.0 / 390.0)
        vix16_floor = _math.ceil(_cfg.VIX16_MULTIPLIER * expected_move / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP
        vix16_floor = max(vix16_floor, _cfg.VIX16_MIN_DIST)
        # Use bucket distance but never go below VIX/16 floor
        if vix_1550 < _cfg.VIX_ATM_CUTOFF:
            bucket_dist = _cfg.DIST_ATM
        elif vix_1550 < _cfg.VIX_MID_CUTOFF:
            bucket_dist = _cfg.DIST_MID
        else:
            bucket_dist = _cfg.DIST_WIDE
        return float(max(bucket_dist, vix16_floor))

    # Default: discrete buckets (Rule C3)
    if vix_1550 < _cfg.VIX_ATM_CUTOFF:
        return _cfg.DIST_ATM
    elif vix_1550 < _cfg.VIX_MID_CUTOFF:
        return _cfg.DIST_MID
    else:
        return _cfg.DIST_WIDE


def _get_tiered_risk(credit: float) -> float:
    """Return risk amount based on credit level (tiered sizing)."""
    if not _cfg.ENABLE_TIERED_SIZING:
        return _cfg.RISK_AMOUNT
    if credit < _cfg.TIER_LOW_CREDIT:
        return _cfg.RISK_LOW
    elif credit > _cfg.TIER_HIGH_CREDIT:
        return _cfg.RISK_HIGH
    else:
        return _cfg.RISK_MID


def _find_short_strike(spot: float) -> int:
    """Find the lowest strike >= spot + MIN_SHORT_DISTANCE, rounded up to STRIKE_STEP."""
    target = spot + _cfg.MIN_SHORT_DISTANCE
    return int(math.ceil(target / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)


def _get_quote(quotes_df: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    """Look up bid/ask/mid for a specific option at a specific time."""
    mask = (
        (quotes_df["right"] == right) &
        (quotes_df["strike"] == strike) &
        (quotes_df["time_str"] == time_str)
    )
    rows = quotes_df[mask]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {"bid": float(row["bid"]), "ask": float(row["ask"]), "mid": float(row["mid"])}


# ---------------------------------------------
#  SIMULATION
# ---------------------------------------------
def run_backtest(indicators: dict) -> list:
    """Run the full backtest over the date range.

    For each trading day:
    1. Check filters (holidays, VIX, indicator)
    2. At entry_time (15:55), get SPX price
    3. Find short call strike >= spot + MIN_SHORT_DISTANCE
    4. Long call = short + SPREAD_WIDTH
    5. Get real bid/ask quotes for both legs
    6. Credit = short_bid - long_ask (selling short, buying long)
    7. Qty = floor(RISK_AMOUNT / (SPREAD_WIDTH * 100 - credit * 100))
    8. At 16:00, spread settles at intrinsic:
       - If SPX < short_strike: spread worth $0 (max profit = credit)
       - If short_strike <= SPX < long_strike: spread worth (SPX - short_strike)
       - If SPX >= long_strike: spread worth SPREAD_WIDTH (max loss)
    9. P&L = (credit - settlement) * qty * 100 - commissions

    Returns list of trade dicts.
    """
    init_log_file()

    start = _cfg.PILOT_YEAR_START.replace("-", "")
    end   = _cfg.PILOT_YEAR_END.replace("-", "")

    # Get all trading dates with option data
    all_dates = sorted(indicators.keys())
    trade_dates = [d for d in all_dates if start <= d <= end]

    logger.info(f"Trading dates: {len(trade_dates)} (from {trade_dates[0]} to {trade_dates[-1]})")

    all_trades = []
    equity = _cfg.STARTING_EQUITY
    skip_reasons = {"holiday": 0, "early_close": 0, "no_data": 0, "no_spx": 0,
                    "no_quotes": 0, "no_strikes": 0, "zero_credit": 0,
                    "vix_filter": 0, "vix_term": 0, "vvix_filter": 0, "wvf_filter": 0,
                    "er_filter": 0, "parkinson_filter": 0, "range_budget": 0, "spread_compression": 0,
                    "fomc_skip": 0, "tw_skip": 0,
                    "vix_intraday": 0, "afternoon_filter": 0,
                    "put_momentum": 0}

    for date_str in trade_dates:
        # Skip holidays
        if date_str in MARKET_HOLIDAYS:
            skip_reasons["holiday"] += 1
            continue

        # Skip early close days (entry at 12:55 — only 4 min of data, unreliable)
        if date_str in EARLY_CLOSE_DAYS:
            skip_reasons["early_close"] += 1
            continue

        # FOMC skip
        if _cfg.ENABLE_FOMC_SKIP and date_str in FOMC_DATES:
            skip_reasons["fomc_skip"] += 1
            continue

        # Triple witching skip
        if _cfg.ENABLE_TW_SKIP and date_str in TRIPLE_WITCHING_DATES:
            skip_reasons["tw_skip"] += 1
            continue

        # VIX daily filter (prior-day close)
        ind = indicators.get(date_str, {})
        vix = ind.get("vix_close")
        if vix is not None:
            if _cfg.VIX_MAX_FILTER is not None and vix > _cfg.VIX_MAX_FILTER:
                skip_reasons["vix_filter"] += 1
                continue
            if _cfg.VIX_MIN_FILTER is not None and vix < _cfg.VIX_MIN_FILTER:
                skip_reasons["vix_filter"] += 1
                continue

        # VIX term structure filter (VIX/VIX9D ratio — backwardation detection)
        if _cfg.ENABLE_VIX_TERM_FILTER:
            vix9d = ind.get("vix9d_close")
            if vix is not None and vix9d is not None and vix9d > 0:
                ratio = vix / vix9d
                if ratio > _cfg.VIX_TERM_MAX_RATIO:
                    skip_reasons["vix_term"] += 1
                    continue

        # VVIX filter (vol-of-vol crash detector)
        if _cfg.ENABLE_VVIX_FILTER:
            vvix = ind.get("vvix_close")
            if vvix is not None and vvix > _cfg.VVIX_MAX:
                skip_reasons["vvix_filter"] += 1
                continue

        # Williams VIX Fix filter (price-derived fear gauge)
        if _cfg.ENABLE_WVF_FILTER:
            wvf_val = ind.get("wvf")
            wvf_bb = ind.get("wvf_upper_bb")
            wvf_pct = ind.get("wvf_pctrank")
            if _cfg.WVF_MODE == "bb" and wvf_val is not None and wvf_bb is not None:
                if wvf_val >= wvf_bb:
                    skip_reasons["wvf_filter"] += 1
                    continue
            elif _cfg.WVF_MODE == "percentile" and wvf_pct is not None:
                if wvf_pct >= _cfg.WVF_PERCENTILE_MAX:
                    skip_reasons["wvf_filter"] += 1
                    continue

        # Load SPX 1-min data
        spx_df = _load_spx_1min(date_str)
        if spx_df is None:
            skip_reasons["no_data"] += 1
            continue

        # --- Intraday indicator filters (computed before entry, no look-ahead) ---

        # VIX at 15:50 filter
        vix_1550 = None
        if _cfg.ENABLE_VIX_INTRADAY_FILTER:
            vix_1550 = _get_vix_at_1550(date_str)
            if vix_1550 is not None and vix_1550 > _cfg.VIX_INTRADAY_MAX:
                skip_reasons["vix_intraday"] += 1
                logger.debug(f"[{date_str}] SKIP: VIX(15:50)={vix_1550:.1f} > {_cfg.VIX_INTRADAY_MAX}")
                continue

        # Afternoon return filter (13:00 -> 15:54)
        afternoon_ret = None
        if _cfg.ENABLE_AFTERNOON_FILTER:
            afternoon_ret = _compute_afternoon_return(spx_df)
            if afternoon_ret is not None and afternoon_ret >= _cfg.AFTERNOON_RETURN_MAX:
                skip_reasons["afternoon_filter"] += 1
                logger.debug(f"[{date_str}] SKIP: Afternoon return={afternoon_ret:.3f}% >= {_cfg.AFTERNOON_RETURN_MAX}%")
                continue

        # Kaufman Efficiency Ratio filter (trend/chop classifier)
        if _cfg.ENABLE_ER_FILTER:
            er_val = _compute_kaufman_er(spx_df, n=_cfg.ER_LOOKBACK)
            if er_val is not None and er_val > _cfg.ER_MAX:
                skip_reasons["er_filter"] += 1
                continue

        # Parkinson volatility filter (realized micro-vol from OHLC)
        if _cfg.ENABLE_PARKINSON_FILTER:
            park_vol = _compute_parkinson_vol(spx_df)
            if park_vol is not None and park_vol > _cfg.PARKINSON_MAX:
                skip_reasons["parkinson_filter"] += 1
                continue

        # Range budget filter (H2-V16-1: fraction of VIX/16 expected range consumed)
        if _cfg.ENABLE_RANGE_BUDGET_FILTER:
            # Use prior-day VIX close as proxy for today's VIX open, and today's SPX open
            vix_for_budget = ind.get("vix_close")  # prior-day VIX
            spx_open_for_budget = float(spx_df["open"].iloc[0]) if not spx_df.empty else None
            rb = _compute_range_budget(spx_df, vix_for_budget, spx_open_for_budget)
            if rb is not None and rb < _cfg.RANGE_BUDGET_MIN:
                skip_reasons["range_budget"] += 1
                continue

        # Spread compression filter (VSA-inspired: narrow bars = safe for credit spreads)
        if _cfg.ENABLE_SPREAD_COMPRESSION_FILTER:
            sc = _compute_spread_compression(spx_df)
            if sc is not None and not sc["is_compressed"]:
                # Bars are wide (not compressed) — elevated movement risk
                if sc["spread_pctile"] >= _cfg.SPREAD_COMPRESSION_SKIP_PCTILE:
                    skip_reasons["spread_compression"] += 1
                    continue

        # Compute 30-min momentum for put filter
        mom30 = _compute_mom30(spx_df)

        # Load option quotes
        quotes_df = _load_option_quotes(date_str)
        if quotes_df is None:
            skip_reasons["no_quotes"] += 1
            continue

        # Get SPX price at entry time (15:55) — use bar OPEN for strike selection
        # Bar OPEN = price at the start of the entry bar, before any intrabar movement.
        # This avoids look-ahead bias and matches OA's backtester (99.8% strike match).
        etime = entry_time(date_str)
        spot = _get_spx_at_time(spx_df, etime, field="open")
        if spot is None:
            skip_reasons["no_spx"] += 1
            continue

        # Get SPX at 16:00 (close / settlement)
        exit_time_str = _cfg.EXIT_TIME
        spx_exit = _get_spx_at_time(spx_df, exit_time_str)
        if spx_exit is None:
            spx_exit = _get_spx_at_time(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        # VIX-adaptive strike distance
        dist = _get_adaptive_distance(vix_1550, spot=spot)

        # Range budget tightening: on quiet days (<50% of VIX-implied range consumed),
        # tighten distance by $2 to collect more credit.
        if _cfg.ENABLE_RANGE_BUDGET_TIGHTEN and vix_1550 and vix_1550 > 0 and spot > 0:
            expected_range = spot * vix_1550 / 100 / math.sqrt(252) * 1.6
            pre_entry = spx_df[spx_df["hhmm"] <= "15:50"]
            if len(pre_entry) >= 5 and expected_range > 0:
                actual_range = float(pre_entry["high"].max()) - float(pre_entry["low"].min())
                range_consumed = actual_range / expected_range
                if range_consumed < _cfg.RANGE_BUDGET_QUIET_THRESHOLD:
                    dist = max(0, dist - _cfg.RANGE_BUDGET_TIGHTEN_AMOUNT)

        available_strikes = set(quotes_df["strike"].unique())
        day_traded = False

        # Collect open positions for protective buy-back check
        open_positions = []  # list of {side, short_strike, long_strike, qty, credit, right}

        # --- CALL SIDE ENTRY ---
        if _cfg.ENABLE_CALL_SIDE:
            call_short = int(math.ceil((spot + dist) / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)
            call_long = call_short + int(_cfg.SPREAD_WIDTH)

            if call_short not in available_strikes or call_long not in available_strikes:
                candidates = sorted(s for s in available_strikes if s >= spot + dist)
                if candidates:
                    call_short = int(candidates[0])
                    call_long = call_short + int(_cfg.SPREAD_WIDTH)

            if call_short in available_strikes and call_long in available_strikes:
                short_q = _get_quote(quotes_df, "C", call_short, etime)
                long_q = _get_quote(quotes_df, "C", call_long, etime)

                if short_q and long_q:
                    credit = short_q["bid"] - long_q["ask"]
                    if credit >= _cfg.MIN_NET_CREDIT:
                        risk = _get_tiered_risk(credit)
                        max_loss_per = (_cfg.SPREAD_WIDTH - credit) * 100.0
                        qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1
                        qty = min(qty, _cfg.MAX_QTY_PER_SIDE)
                        open_positions.append({
                            "side": "CALL", "right": "C",
                            "short_strike": call_short, "long_strike": call_long,
                            "qty": qty, "credit": credit,
                        })

        # --- PUT SIDE ENTRY ---
        if _cfg.ENABLE_PUT_SIDE:
            skip_put = False
            if _cfg.ENABLE_PUT_MOMENTUM_FILTER:
                if (afternoon_ret is not None and afternoon_ret < _cfg.PUT_AFTERNOON_MIN
                        and mom30 is not None and mom30 < _cfg.PUT_MOM30_MIN):
                    skip_put = True
                    skip_reasons["put_momentum"] += 1

            if not skip_put:
                put_short = int(math.floor((spot - dist) / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)
                put_long = put_short - int(_cfg.SPREAD_WIDTH)

                if put_short not in available_strikes or put_long not in available_strikes:
                    candidates = sorted((s for s in available_strikes if s <= spot - dist), reverse=True)
                    if candidates:
                        put_short = int(candidates[0])
                        put_long = put_short - int(_cfg.SPREAD_WIDTH)

                if put_short in available_strikes and put_long in available_strikes:
                    short_q = _get_quote(quotes_df, "P", put_short, etime)
                    long_q = _get_quote(quotes_df, "P", put_long, etime)

                    if short_q and long_q:
                        credit = short_q["bid"] - long_q["ask"]
                        if credit >= _cfg.MIN_NET_CREDIT:
                            risk = _get_tiered_risk(credit)
                            max_loss_per = (_cfg.SPREAD_WIDTH - credit) * 100.0
                            qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1
                            qty = min(qty, _cfg.MAX_QTY_PER_SIDE)
                            open_positions.append({
                                "side": "PUT", "right": "P",
                                "short_strike": put_short, "long_strike": put_long,
                                "qty": qty, "credit": credit,
                            })

        # --- CONTINUOUS TOUCH EXIT (OA-style) ---
        # Check each 1-min bar CLOSE from entry+1 through 15:59.
        # Uses bar CLOSE (not LOW/HIGH) — matches OA's touch detection method.
        stopped_positions = set()
        if _cfg.ENABLE_TOUCH_EXIT and open_positions:
            entry_hhmm = etime[:5]  # e.g. "15:55"
            entry_h, entry_m = int(entry_hhmm[:2]), int(entry_hhmm[3:5])
            entry_min = entry_h * 60 + entry_m
            for cm in range(entry_min + 1, 960):  # up to 16:00
                h, m = cm // 60, cm % 60
                check_hhmm = f"{h}:{m:02d}"
                check_time = f"{check_hhmm}:00"
                spx_check = _get_spx_at_time(spx_df, check_time)
                if spx_check is None:
                    continue
                for idx, pos in enumerate(open_positions):
                    if idx in stopped_positions:
                        continue
                    if pos["side"] == "CALL":
                        triggered = spx_check >= pos["short_strike"] - _cfg.CALL_TOUCH_VALUE
                    else:  # PUT
                        triggered = spx_check <= pos["short_strike"] + _cfg.PUT_TOUCH_VALUE
                    if triggered:
                        close_short = _get_quote(quotes_df, pos["right"], pos["short_strike"], check_time)
                        close_long = _get_quote(quotes_df, pos["right"], pos["long_strike"], check_time)
                        if close_short and close_long:
                            close_cost = close_short["ask"] - close_long["bid"]
                            net_pnl = (pos["credit"] - close_cost) * pos["qty"] * 100.0 - 4 * pos["qty"] * _cfg.COMMISSION
                            equity += net_pnl
                            is_win = net_pnl > 0
                            day_traded = True
                            stopped_positions.add(idx)
                            touch_label = f"TOUCH@{check_hhmm}"
                            trade = {
                                "date": date_str, "side": pos["side"],
                                "entry_time": etime, "exit_time": check_time,
                                "spx_at_entry": round(spot, 2), "spx_at_exit": round(spx_check, 2),
                                "short_strike": pos["short_strike"], "long_strike": pos["long_strike"],
                                "qty": pos["qty"], "credit_received": round(pos["credit"], 4),
                                "spread_value_exit": round(close_cost, 4),
                                "pnl_earned": round(net_pnl, 2),
                                "win": 1 if is_win else 0, "loss": 0 if is_win else 1,
                                "equity_after": round(equity, 2),
                                "vix_level": round(vix, 2) if vix else "",
                                "indicator_value": f"vix1550={vix_1550:.1f},dist={dist:.0f},{touch_label}" if vix_1550 else touch_label,
                            }
                            append_trade(trade)
                            all_trades.append(trade)
                            logger.debug(
                                f"[{date_str}] {touch_label} {pos['side']} SPX={spx_check:.0f} | "
                                f"{pos['right']} {pos['short_strike']}/{pos['long_strike']} x{pos['qty']} | "
                                f"cr=${pos['credit']:.2f} close=${close_cost:.2f} | P&L=${net_pnl:+.0f}"
                            )

        # --- LEGACY PROTECTIVE BUY-BACK at 15:56, 15:57 (disabled by default) ---
        if _cfg.ENABLE_PROTECTIVE_BUYBACK and open_positions:
            for check_time in _cfg.STOP_CHECK_TIMES:
                spx_check = _get_spx_at_time(spx_df, check_time)
                if spx_check is None:
                    continue
                for idx, pos in enumerate(open_positions):
                    if idx in stopped_positions:
                        continue
                    if pos["side"] == "CALL":
                        move_toward = spx_check - spot
                    else:
                        move_toward = spot - spx_check
                    if move_toward >= _cfg.STOP_THRESHOLD:
                        close_short = _get_quote(quotes_df, pos["right"], pos["short_strike"], check_time)
                        close_long = _get_quote(quotes_df, pos["right"], pos["long_strike"], check_time)
                        if close_short and close_long:
                            close_cost = close_short["ask"] - close_long["bid"]
                            net_pnl = (pos["credit"] - close_cost) * pos["qty"] * 100.0 - 4 * pos["qty"] * _cfg.COMMISSION
                            equity += net_pnl
                            is_win = net_pnl > 0
                            day_traded = True
                            stopped_positions.add(idx)
                            trade = {
                                "date": date_str, "side": pos["side"],
                                "entry_time": etime, "exit_time": check_time,
                                "spx_at_entry": round(spot, 2), "spx_at_exit": round(spx_check, 2),
                                "short_strike": pos["short_strike"], "long_strike": pos["long_strike"],
                                "qty": pos["qty"], "credit_received": round(pos["credit"], 4),
                                "spread_value_exit": round(close_cost, 4),
                                "pnl_earned": round(net_pnl, 2),
                                "win": 1 if is_win else 0, "loss": 0 if is_win else 1,
                                "equity_after": round(equity, 2),
                                "vix_level": round(vix, 2) if vix else "",
                                "indicator_value": f"vix1550={vix_1550:.1f},dist={dist:.0f},STOP@{check_time[:5]}" if vix_1550 else f"STOP@{check_time[:5]}",
                            }
                            append_trade(trade)
                            all_trades.append(trade)
                            logger.debug(
                                f"[{date_str}] STOP {pos['side']} @{check_time[:5]} SPX={spx_check:.0f} (moved ${move_toward:+.1f}) | "
                                f"{pos['right']} {pos['short_strike']}/{pos['long_strike']} x{pos['qty']} | "
                                f"cr=${pos['credit']:.2f} close=${close_cost:.2f} | P&L=${net_pnl:+.0f}"
                            )

        # --- SETTLE remaining positions at 16:00 ---
        for idx, pos in enumerate(open_positions):
            if idx in stopped_positions:
                continue

            if pos["side"] == "CALL":
                if spx_exit <= pos["short_strike"]:
                    settlement = 0.0
                elif spx_exit >= pos["long_strike"]:
                    settlement = _cfg.SPREAD_WIDTH
                else:
                    settlement = spx_exit - pos["short_strike"]
            else:  # PUT
                if spx_exit >= pos["short_strike"]:
                    settlement = 0.0
                elif spx_exit <= pos["long_strike"]:
                    settlement = _cfg.SPREAD_WIDTH
                else:
                    settlement = pos["short_strike"] - spx_exit

            net_pnl = (pos["credit"] - settlement) * pos["qty"] * 100.0 - 4 * pos["qty"] * _cfg.COMMISSION
            equity += net_pnl
            is_win = net_pnl > 0
            day_traded = True

            trade = {
                "date": date_str, "side": pos["side"],
                "entry_time": etime, "exit_time": exit_time_str,
                "spx_at_entry": round(spot, 2), "spx_at_exit": round(spx_exit, 2),
                "short_strike": pos["short_strike"], "long_strike": pos["long_strike"],
                "qty": pos["qty"], "credit_received": round(pos["credit"], 4),
                "spread_value_exit": round(settlement, 4),
                "pnl_earned": round(net_pnl, 2),
                "win": 1 if is_win else 0, "loss": 0 if is_win else 1,
                "equity_after": round(equity, 2),
                "vix_level": round(vix, 2) if vix else "",
                "indicator_value": f"vix1550={vix_1550:.1f},dist={dist:.0f}" if vix_1550 else "",
            }
            append_trade(trade)
            all_trades.append(trade)
            logger.debug(
                f"[{date_str}] {pos['side']} SPX={spot:.0f}->{spx_exit:.0f} | "
                f"{pos['right']} {pos['short_strike']}/{pos['long_strike']} x{pos['qty']} | "
                f"cr=${pos['credit']:.2f} settle=${settlement:.2f} | "
                f"P&L=${net_pnl:+.0f} | eq=${equity:,.0f} | dist={dist:.0f}"
            )

        if not day_traded:
            skip_reasons["no_quotes"] += 1

    # Summary of skips
    logger.info("=" * 60)
    logger.info("SKIP REASONS:")
    for reason, count in sorted(skip_reasons.items()):
        if count > 0:
            logger.info(f"  {reason:<20}: {count}")
    logger.info(f"  {'TOTAL TRADES':<20}: {len(all_trades)}")
    logger.info("=" * 60)

    return all_trades
