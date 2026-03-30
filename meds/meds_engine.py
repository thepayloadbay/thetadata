from __future__ import annotations

import asyncio
import csv
import glob
import json
import logging
import math
import os
import pathlib
import re
import sys
from contextlib import asynccontextmanager
import numpy as np
import pandas as pd
from datetime import datetime, time
from mcp import ClientSession
from mcp.client.sse import sse_client

# All config constants
from meds_config import *
import meds_config as _cfg

# Underscore names from config (not included in * import)
from meds_config import _out, _build_calendar_event_dates, _RUN_TS

# CLI-mutable vars: use _cfg.VAR in engine functions that reference these:
#   SAVE_FILE, DYNAMIC_SL_VIX_MID, SKIP_VIX_RANGE, ENABLE_KELLY_SIZING,
#   INTRADAY_ENTRY_GATE, PREMIUM_BUYBACK_EXIT, MIN_OTM_DISTANCE, MAX_NET_CREDIT

# ---------------------------------------------
#  LOGGING SETUP
# ---------------------------------------------
def setup_logger() -> logging.Logger:
    from datetime import datetime as _dt
    run_ts   = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_FILE.replace(".log", f"_{run_ts}.log")

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("meft")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console -- INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File -- DEBUG and above, unique filename per run
    fh = logging.FileHandler(log_path, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info(f"Log file  : {log_path}")
    return logger

logger = setup_logger()


# ---------------------------------------------
#  HELPERS
# ---------------------------------------------


def init_log_file():
    with open(_cfg.SAVE_FILE, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_COLS).writeheader()


def append_trade(pos: dict):
    with open(_cfg.SAVE_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore").writerow(pos)


def calculate_ema(data: list, window: int) -> float:
    if not data:
        return 0.0
    alpha = 2 / (window + 1)
    ema = data[0]
    for price in data[1:]:
        ema = price * alpha + ema * (1 - alpha)
    return ema


def mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0


def approx_spread_delta(spot: float, short_strike: int, long_strike: int) -> float:
    """
    Approximate net delta of a short credit spread using Gaussian moneyness.
    Assumes 0DTE SPX daily vol of ~0.8% of spot.
    - Call spreads return negative delta  (lose when SPX rallies)
    - Put spreads return positive delta   (lose when SPX falls)
    No extra API calls needed.
    """
    import math
    sigma = spot * 0.008  # 1-day 1-sigma move
    def n(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))
    d_short = (spot - short_strike) / sigma
    d_long  = (spot - long_strike)  / sigma
    # Unified formula: positive for put spreads, negative for call spreads
    return n(d_long) - n(d_short)


def date_to_expiry(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"


# ---------------------------------------------
#  DAILY BAR INDICATORS
# ---------------------------------------------
_DAILY_INDICATORS: dict = {}   # date_str -> dict of indicator values; built once at startup
_EOM_DATES: set = set()        # last trading day of each month (YYYYMMDD); built once at startup
_CPI_DATES: set = set()        # CPI release days
_PCE_DATES: set = set()        # PCE release days
_NFP_DATES: set = set()        # Non-Farm Payroll release days
_EOQ_DATES: set = set()        # last trading day of each quarter
_PRE_TW_DATES: set = set()     # trading day immediately before Triple Witching Friday
_POST_HOL_DATES: set = set()   # first trading day after each market holiday


def _get_baseline_mode(date_str: str) -> str | None:
    """Return the baseline_mode for _simulate_day based on DIRECTION_MODE.

    "vix_change" reads today's VIX % change from _DAILY_INDICATORS:
        VIX falling (chg <= 0) -> "always_put"  (market calmer, sell PUT spread)
        VIX rising  (chg >  0) -> "always_call" (market fearful, sell CALL spread)
    Falls back to "always_put" if VIX data is unavailable for the day.
    """
    if DIRECTION_MODE == "always_put":
        return "always_put"
    if DIRECTION_MODE == "always_call":
        return "always_call"
    if DIRECTION_MODE == "vix_change":
        today = _DAILY_INDICATORS.get(date_str)
        chg = (today or {}).get("dVixChgPct")
        if chg is None:
            return "always_put"  # safe fallback
        return "always_put" if chg <= 0 else "always_call"
    return None  # "ema" -- use intraday EMA direction


def _build_daily_indicators(compute_full: bool = False) -> dict:
    """Aggregate 1-min SPX OHLC parquets to daily bars and compute indicators.

    Reads all years present in DATA_DIR so rolling windows (SMA200, ATR14, RSI14, etc.)
    are properly warmed up before the backtest window starts.

    When compute_full=False (default), only computes the fields needed for the
    standard marathon: VIX change (direction signal), gap%, and basic OHLC.
    When compute_full=True, computes all technical indicators (SMAs, RSI, Stoch,
    ADX, CCI, CMO, MACD, StochRSI, ExpMove, IvRank, RangePct, etc.).

    Returns dict[date_str -> {open, high, low, close, dVarPct, dBodySize, dGapPercent,
                               dSma5, dSma20, dSma200, distFromSma, fallingKnife,
                               above_sma5, above_sma200, dATR, dRsi, dStoch, dStochD,
                               dAdx, dCci, dCmo, dMacd, dMacdSig, dMacdHist, dMacdBull,
                               dMomentum, dStochRsiK, dStochRsiD,
                               dExpMoveUSD, dExpMovePct, dIvRank, dRangePct}]
    """
    rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
            if df.empty or len(df) < 5:
                continue
            rows.append({
                "date":  date_str,
                "open":  float(df["open"].iloc[0]),
                "high":  float(df["high"].max()),
                "low":   float(df["low"].min()),
                "close": float(df["close"].iloc[-1]),
            })
        except Exception:
            pass

    if not rows:
        return {}

    d = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)

    # -- Merge daily VIX closes --
    vix_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix_close" in vdf.columns:
                vix_rows.append({"date": str(vdf["date"].iloc[0]), "vix_close": float(vdf["vix_close"].iloc[0])})
        except Exception:
            pass
    if vix_rows:
        vix_df = pd.DataFrame(vix_rows).sort_values("date").reset_index(drop=True)
        vix_df["prev_vix"]      = vix_df["vix_close"].shift(1)
        vix_df["dVixChgPct"]    = (vix_df["vix_close"] - vix_df["prev_vix"]) / vix_df["prev_vix"] * 100
        vix_df["dVixVelocity"]  = vix_df["vix_close"] - vix_df["prev_vix"]
        vix_df["dVixAccel"]     = vix_df["dVixVelocity"] - vix_df["dVixVelocity"].shift(1)
        # True when VIX is rising but rate of rise is slowing (weakest signal quadrant)
        vix_df["vix_rise_decel"] = (vix_df["dVixVelocity"] > 0) & (vix_df["dVixAccel"] < 0)
        d = d.merge(vix_df[["date", "vix_close", "dVixChgPct", "dVixVelocity", "dVixAccel", "vix_rise_decel"]], on="date", how="left")

    # -- Merge daily VIX1D closes --
    vix1d_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix1d", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix1d_close" in vdf.columns:
                vix1d_rows.append({"date": str(vdf["date"].iloc[0]), "vix1d_close": float(vdf["vix1d_close"].iloc[0])})
        except Exception:
            pass
    if vix1d_rows:
        v1d_df = pd.DataFrame(vix1d_rows).sort_values("date").reset_index(drop=True)
        v1d_df["prev_vix1d"]    = v1d_df["vix1d_close"].shift(1)
        v1d_df["dVix1dChgPct"]  = (v1d_df["vix1d_close"] - v1d_df["prev_vix1d"]) / v1d_df["prev_vix1d"] * 100
        d = d.merge(v1d_df[["date", "vix1d_close", "dVix1dChgPct"]], on="date", how="left")
        d.rename(columns={"vix1d_close": "dVix1d"}, inplace=True)

    # -- Merge daily VIX9D closes --
    vix9d_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix9d", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix9d_close" in vdf.columns:
                vix9d_rows.append({"date": str(vdf["date"].iloc[0]), "vix9d_close": float(vdf["vix9d_close"].iloc[0])})
        except Exception:
            pass
    if vix9d_rows:
        v9d_df = pd.DataFrame(vix9d_rows).sort_values("date").reset_index(drop=True)
        d = d.merge(v9d_df[["date", "vix9d_close"]], on="date", how="left")
        d.rename(columns={"vix9d_close": "dVix9d"}, inplace=True)

    # -- Merge daily VVIX closes --
    vvix_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vvix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vvix_close" in vdf.columns:
                vvix_rows.append({"date": str(vdf["date"].iloc[0]), "vvix_close": float(vdf["vvix_close"].iloc[0])})
        except Exception:
            pass
    if vvix_rows:
        vvix_df = pd.DataFrame(vvix_rows).sort_values("date").reset_index(drop=True)
        vvix_df["prev_vvix"]    = vvix_df["vvix_close"].shift(1)
        vvix_df["dVvixChgPct"]  = (vvix_df["vvix_close"] - vvix_df["prev_vvix"]) / vvix_df["prev_vvix"] * 100
        d = d.merge(vvix_df[["date", "vvix_close", "dVvixChgPct"]], on="date", how="left")
        d.rename(columns={"vvix_close": "dVvix"}, inplace=True)

    # -- Derived VIX term structure signals --
    if "dVix1d" in d.columns and "vix_close" in d.columns:
        # VIX1D/VIX ratio: >1 means acute 1-day fear exceeds 30-day -> danger signal
        d["dVix1dVixRatio"] = d["dVix1d"] / d["vix_close"].clip(lower=0.01)
        # Term spread: VIX - VIX1D (positive = contango = normal; negative = backwardation = stress)
        d["dVixTermSpread"] = d["vix_close"] - d["dVix1d"]
    if "dVix9d" in d.columns and "vix_close" in d.columns:
        # VIX9D/VIX ratio: short-term structure slope
        d["dVix9dVixRatio"] = d["dVix9d"] / d["vix_close"].clip(lower=0.01)

    # -- Single-bar indicators (always needed) --
    hl = (d["high"] - d["low"]).clip(lower=0.01)
    d["dVarPct"]     = (d["close"] - d["low"]) / hl * 100
    d["dBodySize"]   = (d["open"] - d["close"]).abs()
    d["dGapPercent"] = (d["open"] - d["prev_close"]) / d["prev_close"] * 100

    # -- Full technical indicators (only when day filters or sweeps are active) --
    if compute_full:
        # -- Moving averages --
        d["dSma5"]   = d["close"].rolling(5,   min_periods=1).mean()
        d["dSma10"]  = d["close"].rolling(10,  min_periods=1).mean()
        d["dSma20"]  = d["close"].rolling(20,  min_periods=1).mean()
        d["dSma30"]  = d["close"].rolling(30,  min_periods=1).mean()
        d["dSma50"]  = d["close"].rolling(50,  min_periods=1).mean()
        d["dSma100"] = d["close"].rolling(100, min_periods=1).mean()
        d["dSma200"] = d["close"].rolling(200, min_periods=1).mean()
        d["distFromSma"]   = (d["close"] - d["dSma20"]) / d["dSma20"]
        d["fallingKnife"]  = (d["close"] - d["dSma5"]).abs() / d["dSma5"]
        d["above_sma5"]    = d["close"] > d["dSma5"]
        d["above_sma10"]   = d["close"] > d["dSma10"]
        d["above_sma20"]   = d["close"] > d["dSma20"]
        d["above_sma30"]   = d["close"] > d["dSma30"]
        d["above_sma50"]   = d["close"] > d["dSma50"]
        d["above_sma100"]  = d["close"] > d["dSma100"]
        d["above_sma200"]  = d["close"] > d["dSma200"]

        # -- ATR(14): True Range = max(H-L, |H-prevC|, |L-prevC|) --
        tr = pd.concat([
            d["high"] - d["low"],
            (d["high"] - d["prev_close"]).abs(),
            (d["low"]  - d["prev_close"]).abs(),
        ], axis=1).max(axis=1)
        d["dATR"] = tr.rolling(14, min_periods=1).mean()

        # -- RSI(14) --
        delta = d["close"].diff()
        gain  = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss  = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs    = gain / loss.replace(0.0, float("nan"))
        d["dRsi"] = 100.0 - (100.0 / (1.0 + rs))

        # -- Stochastic %K(14) and %D(3) --
        low14  = d["low"].rolling(14,  min_periods=1).min()
        high14 = d["high"].rolling(14, min_periods=1).max()
        d["dStoch"]  = (d["close"] - low14) / (high14 - low14).clip(lower=0.01) * 100
        d["dStochD"] = d["dStoch"].rolling(3, min_periods=1).mean()

        # -- ADX(14) --
        prev_high  = d["high"].shift(1)
        prev_low   = d["low"].shift(1)
        up_move    = d["high"] - prev_high
        down_move  = prev_low  - d["low"]
        plus_dm    = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=d.index)
        minus_dm   = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=d.index)
        atr_s      = d["dATR"].clip(lower=0.01)
        plus_di14  = 100 * plus_dm.rolling(14, min_periods=1).mean() / atr_s
        minus_di14 = 100 * minus_dm.rolling(14, min_periods=1).mean() / atr_s
        dx         = 100 * (plus_di14 - minus_di14).abs() / (plus_di14 + minus_di14).clip(lower=0.01)
        d["dAdx"]  = dx.rolling(14, min_periods=1).mean()

        # -- CCI(20) --
        tp         = (d["high"] + d["low"] + d["close"]) / 3
        sma_tp     = tp.rolling(20, min_periods=1).mean()
        mean_dev   = tp.rolling(20, min_periods=1).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
        d["dCci"]  = (tp - sma_tp) / (0.015 * mean_dev.clip(lower=0.01))

        # -- CMO(9) --
        delta_cmo  = d["close"].diff()
        sum_up9    = delta_cmo.clip(lower=0).rolling(9, min_periods=1).sum()
        sum_dn9    = (-delta_cmo.clip(upper=0)).rolling(9, min_periods=1).sum()
        d["dCmo"]  = 100 * (sum_up9 - sum_dn9) / (sum_up9 + sum_dn9).clip(lower=0.01)

        # -- MACD(12,26,9) --
        ema12          = d["close"].ewm(span=12, adjust=False).mean()
        ema26          = d["close"].ewm(span=26, adjust=False).mean()
        d["dMacd"]     = ema12 - ema26
        d["dMacdSig"]  = d["dMacd"].ewm(span=9, adjust=False).mean()
        d["dMacdHist"] = d["dMacd"] - d["dMacdSig"]
        d["dMacdBull"] = d["dMacd"] > d["dMacdSig"]  # True = MACD above signal line

        # -- Momentum(10) --
        d["dMomentum"] = d["close"] - d["close"].shift(10)

        # -- StochRSI(14,14,3,3): stochastic of RSI(14), smoothed K(3), D(3) --
        rsi_low14       = d["dRsi"].rolling(14, min_periods=1).min()
        rsi_high14      = d["dRsi"].rolling(14, min_periods=1).max()
        stoch_rsi_raw   = (d["dRsi"] - rsi_low14) / (rsi_high14 - rsi_low14).clip(lower=0.01) * 100
        d["dStochRsiK"] = stoch_rsi_raw.rolling(3, min_periods=1).mean()
        d["dStochRsiD"] = d["dStochRsiK"].rolling(3, min_periods=1).mean()

        # -- Expected Move (VIX-implied 1-day 1σ): requires merged VIX data --
        if "vix_close" in d.columns:
            d["dExpMoveUSD"] = d["close"] * d["vix_close"] / 100.0 / math.sqrt(252)
            d["dExpMovePct"] = d["vix_close"] / 100.0 / math.sqrt(252) * 100  # same as VIX/sqrt(252)

        # -- IV Rank (52-week VIX percentile, prior close) --
        if "vix_close" in d.columns:
            vix_s          = d["vix_close"]
            vix_min365     = vix_s.rolling(365, min_periods=30).min()
            vix_max365     = vix_s.rolling(365, min_periods=30).max()
            d["dIvRank"]   = (vix_s - vix_min365) / (vix_max365 - vix_min365).clip(lower=0.01) * 100

        # -- Prior-day trading range as % of prior close --
        d["dRangePct"] = (d["high"] - d["low"]) / d["prev_close"].clip(lower=0.01) * 100

    result = {}
    for _, row in d.iterrows():
        result[row["date"]] = row.to_dict()
    return result


def _prior_day_ind(date_str: str) -> dict | None:
    """Return the prior trading day's daily indicator dict, or None if unavailable."""
    prev = (pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}") - pd.offsets.BDay(1))
    return _DAILY_INDICATORS.get(prev.strftime("%Y%m%d"))


def _passes_filter(filter_name: str, threshold, date_str: str, day_data: dict | None = None) -> bool:
    """Check a single named day filter. Returns True if the day passes (should trade).

    day_data is only needed for the 'vix_max' filter (uses today's VIX level).
    """
    prior = _prior_day_ind(date_str)
    today = _DAILY_INDICATORS.get(date_str)

    def safe(d, key):
        if d is None:
            return None
        v = d.get(key)
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else v

    if filter_name == "vix_max":
        if threshold is None:
            return True
        vix = day_data.get("vix_level") if day_data else None
        return vix is None or vix <= threshold
    if filter_name == "vix_min":
        vix = day_data.get("vix_level") if day_data else None
        return vix is None or vix >= threshold
    if filter_name == "dVarPct_min":
        v = safe(prior, "dVarPct");    return v is None or v >= threshold
    if filter_name == "dRsi_min":
        v = safe(prior, "dRsi");       return v is None or v >= threshold
    if filter_name == "dRsi_max":
        v = safe(prior, "dRsi");       return v is None or v <= threshold
    if filter_name == "distFromSma_min":
        v = safe(prior, "distFromSma"); return v is None or v >= threshold
    if filter_name == "dATR_max":
        v = safe(prior, "dATR");       return v is None or v <= threshold
    if filter_name == "dBodySize_max":
        v = safe(prior, "dBodySize");  return v is None or v <= threshold
    if filter_name == "fallingKnife_max":
        v = safe(prior, "fallingKnife"); return v is None or v <= threshold
    if filter_name == "dStoch_min":
        v = safe(prior, "dStoch");     return v is None or v >= threshold
    if filter_name == "dGapPercent_max":
        v = safe(today, "dGapPercent"); return v is None or abs(v) <= threshold
    if filter_name == "above_sma5":
        v = safe(prior, "above_sma5");   return v is None or bool(v)
    if filter_name == "above_sma10":
        v = safe(prior, "above_sma10");  return v is None or bool(v)
    if filter_name == "above_sma20":
        v = safe(prior, "above_sma20");  return v is None or bool(v)
    if filter_name == "above_sma30":
        v = safe(prior, "above_sma30");  return v is None or bool(v)
    if filter_name == "above_sma50":
        v = safe(prior, "above_sma50");  return v is None or bool(v)
    if filter_name == "above_sma100":
        v = safe(prior, "above_sma100"); return v is None or bool(v)
    if filter_name == "above_sma200":
        v = safe(prior, "above_sma200"); return v is None or bool(v)
    if filter_name == "dVixChgPct_max":
        v = safe(prior, "dVixChgPct"); return v is None or v <= threshold
    if filter_name == "dVixChgPct_min":
        v = safe(prior, "dVixChgPct"); return v is None or v >= threshold
    if filter_name == "dGapPercent_min":
        v = safe(today, "dGapPercent"); return v is None or v >= threshold
    if filter_name == "dAdx_min":
        v = safe(prior, "dAdx");       return v is None or v >= threshold
    if filter_name == "dCci_min":
        v = safe(prior, "dCci");       return v is None or v >= threshold
    if filter_name == "dCci_max":
        v = safe(prior, "dCci");       return v is None or v <= threshold
    if filter_name == "dCmo_min":
        v = safe(prior, "dCmo");       return v is None or v >= threshold
    if filter_name == "dMacdBull":
        v = safe(prior, "dMacdBull");  return v is None or bool(v)
    if filter_name == "dMomentum_min":
        v = safe(prior, "dMomentum"); return v is None or v >= threshold
    if filter_name == "dStochD_min":
        v = safe(prior, "dStochD");   return v is None or v >= threshold
    if filter_name == "dStochRsiK_min":
        v = safe(prior, "dStochRsiK"); return v is None or v >= threshold
    if filter_name == "dExpMoveUSD_min":
        v = safe(today, "dExpMoveUSD"); return v is None or v >= threshold
    if filter_name == "dExpMoveUSD_max":
        v = safe(today, "dExpMoveUSD"); return v is None or v <= threshold
    if filter_name == "dExpMovePct_min":
        v = safe(today, "dExpMovePct"); return v is None or v >= threshold
    if filter_name == "dExpMovePct_max":
        v = safe(today, "dExpMovePct"); return v is None or v <= threshold
    if filter_name == "dRangePct_min":
        v = safe(prior, "dRangePct"); return v is None or v >= threshold
    if filter_name == "dRangePct_max":
        v = safe(prior, "dRangePct"); return v is None or v <= threshold
    if filter_name == "dIvRank_min":
        v = safe(prior, "dIvRank");   return v is None or v >= threshold
    if filter_name == "dIvRank_max":
        v = safe(prior, "dIvRank");   return v is None or v <= threshold
    return True


def _passes_active_day_filters(date_str: str, vix_level: float | None = None) -> bool:
    """Check all globally-configured DAY_FILTER_* settings. Returns True if day should trade."""
    _vix_day_data = {"vix_level": vix_level}
    checks = [
        ("vix_max",          DAY_FILTER_VIX_MAX),
        ("vix_min",          DAY_FILTER_VIX_MIN),
        ("dVixChgPct_max",   DAY_FILTER_VIX_CHG_MAX),
        ("dVixChgPct_min",   DAY_FILTER_VIX_CHG_MIN),
        ("dVarPct_min",      DAY_FILTER_VARPC_MIN),
        ("dRsi_min",         DAY_FILTER_RSI_MIN),
        ("dRsi_max",         DAY_FILTER_RSI_MAX),
        ("distFromSma_min",  DAY_FILTER_DIST_SMA_MIN),
        ("dATR_max",         DAY_FILTER_ATR_MAX),
        ("dBodySize_max",    DAY_FILTER_BODY_MAX),
        ("fallingKnife_max", DAY_FILTER_KNIFE_MAX),
        ("dStoch_min",       DAY_FILTER_STOCH_MIN),
        ("dGapPercent_max",  DAY_FILTER_GAP_MAX),
        ("above_sma5",       DAY_FILTER_ABOVE_SMA5),
        ("above_sma200",     DAY_FILTER_ABOVE_SMA200),
        ("dAdx_min",         DAY_FILTER_ADX_MIN),
        ("dRangePct_max",    DAY_FILTER_RANGE_MAX),
        ("dRangePct_min",    DAY_FILTER_RANGE_MIN),
        ("dExpMovePct_max",  DAY_FILTER_EXP_MOVE_MAX),
        ("dExpMovePct_min",  DAY_FILTER_EXP_MOVE_MIN),
        ("dCci_max",         DAY_FILTER_CCI_MAX),
        ("dIvRank_min",      DAY_FILTER_IVR_MIN),
    ]
    for fname, threshold in checks:
        if threshold is None:
            continue
        if not _passes_filter(fname, threshold, date_str, _vix_day_data):
            return False
    if DAY_FILTER_SKIP_VIX_RISE_DECEL:
        ind = _DAILY_INDICATORS.get(date_str, {})
        if ind.get("vix_rise_decel", False):
            return False
    return True


def parse_single_quote(raw: str) -> dict | None:
    # Try full JSON parse first
    try:
        sanitized = re.sub(r'\b(NaN|-?Infinity)\b', 'null', raw)
        parsed = json.loads(sanitized)
        response = parsed.get("response", [])
        if not response:
            return None
        data = response[-1].get("data", [])
        if not data:
            return None
        last = data[-1]
        bid = float(last.get("bid") or 0.0)
        ask = float(last.get("ask") or 0.0)
        return {"bid": bid, "ask": ask, "mid": mid(bid, ask)}
    except Exception:
        pass

    # Fallback: regex extraction for truncated responses
    bids = re.findall(r'"bid":([\d\.]+)', raw)
    asks = re.findall(r'"ask":([\d\.]+)', raw)
    if bids and asks:
        bid = float(bids[-1])
        ask = float(asks[-1])
        return {"bid": bid, "ask": ask, "mid": mid(bid, ask)}

    return None



# ---------------------------------------------
# ---------------------------------------------
#  SESSION FACTORY
# ---------------------------------------------

@asynccontextmanager
async def _get_session():
    """Yield an MCP session (USE_LOCAL_DATA=False) or None (USE_LOCAL_DATA=True)."""
    if USE_LOCAL_DATA:
        yield None
    else:
        async with sse_client(MCP_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session



#  THETA DATA API CALLS
# ---------------------------------------------
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds, doubles each attempt

async def call_with_retry(session, tool: str, params: dict):
    """Call a ThetaData MCP tool with exponential backoff retry."""
    delay = RETRY_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        result = await session.call_tool(tool, params)
        if not result.isError:
            return result
        if attempt < MAX_RETRIES:
            logger.warning(f"{tool} failed (attempt {attempt}/{MAX_RETRIES}), retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay *= 2
        else:
            logger.error(f"{tool} failed after {MAX_RETRIES} attempts: {result.content[0].text[:100]}")
    return result  # return last failed result so callers can check isError


async def get_strikes(session, expiry: str) -> list:
    date_str = expiry.replace("-", "")
    if USE_LOCAL_DATA:
        path = pathlib.Path(DATA_DIR) / date_str[:4] / "option_quotes" / f"{date_str}.parquet"
        if not path.exists():
            logger.warning(f"No option_quotes parquet for {date_str}")
            return []
        df = pd.read_parquet(path, columns=["strike"])
        strikes = sorted(int(s) for s in df["strike"].unique())
        logger.info(f"Got {len(strikes)} strikes. Range: {strikes[0]} - {strikes[-1]}")
        return strikes
    logger.info(f"Fetching strikes for expiry={expiry}...")
    result = await call_with_retry(session, "option_list_strikes", {
        "symbol":     ["SPXW"],
        "expiration": expiry,
    })
    if result.isError:
        return []
    raw = result.content[0].text
    strikes = [int(round(float(s))) for s in re.findall(r'[\d]+\.[\d]+', raw)]
    strikes = sorted(set(strikes))
    logger.info(f"Got {len(strikes)} strikes. Range: {strikes[0]} - {strikes[-1]}")
    return strikes


# --- Local option_quotes cache (loaded once per trading day) ---
_local_quotes_df: pd.DataFrame | None = None
_local_quotes_date: str | None = None


def _load_local_quotes(date_str: str) -> None:
    """Load option_quotes parquet for date_str into module-level cache."""
    global _local_quotes_df, _local_quotes_date
    if _local_quotes_date == date_str:
        return
    path = pathlib.Path(DATA_DIR) / date_str[:4] / "option_quotes" / f"{date_str}.parquet"
    if not path.exists():
        _local_quotes_df = pd.DataFrame()
        _local_quotes_date = date_str
        return
    df = pd.read_parquet(path).copy()
    df["time_str"] = df["timestamp"].str[11:19]  # extract "HH:MM:SS"
    _local_quotes_df = df.set_index(["right", "strike", "time_str"]).sort_index()
    _local_quotes_date = date_str
    logger.debug(f"Loaded local quotes for {date_str}: {len(df)} rows")


async def fetch_quote_at_time(
    session, date_str, expiry, right, strike, bar_time_str
) -> dict | None:
    if USE_LOCAL_DATA:
        _load_local_quotes(date_str)
        if _local_quotes_df is None or _local_quotes_df.empty:
            return None
        try:
            row = _local_quotes_df.loc[(right, int(strike), bar_time_str)]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            return {"bid": float(row["bid"]), "ask": float(row["ask"]), "mid": float(row["mid"])}
        except KeyError:
            return None
    result = await call_with_retry(session, "option_at_time_quote", {
        "symbol":      "SPXW",
        "start_date":  date_str,
        "end_date":    date_str,
        "expiration":  expiry,
        "right":       right,
        "strike":      str(float(strike)),
        "time_of_day": bar_time_str,
    })
    if result.isError:
        return None
    return parse_single_quote(result.content[0].text)


async def fetch_quotes_for_strikes(
    session, date_str, expiry, right, strikes, bar_time_str
) -> dict:
    async def fetch_one(s):
        q = await fetch_quote_at_time(session, date_str, expiry, right, s, bar_time_str)
        return s, q
    results = await asyncio.gather(*[fetch_one(s) for s in strikes])
    return {s: q for s, q in results if q is not None}


# ---------------------------------------------
#  QUOTE CACHE  (lazy -- populated during normal fetches)
# ---------------------------------------------
_quote_cache: dict = {}  # (date_str, right, strike, bar_time_str) -> dict | None


def clear_day_cache():
    """Call once at the start of each day to discard stale quotes."""
    global _quote_cache
    _quote_cache.clear()


def load_quote_disk_cache() -> None:
    """Load the persistent quote cache from disk into _quote_cache at startup.

    Skips parquet index lookups for all previously seen (date, right, strike, bar_time)
    tuples -- eliminates redundant I/O on repeat runs and sweeps.
    """
    global _quote_cache
    path = pathlib.Path(QUOTE_DISK_CACHE)
    if not path.exists():
        return
    try:
        df = pd.read_parquet(path)
        loaded = 0
        for row in df.itertuples(index=False):
            key = (row.date_str, row.right, row.strike, row.bar_time_str)
            if key not in _quote_cache:
                _quote_cache[key] = {"bid": row.bid, "ask": row.ask, "mid": row.mid} if not pd.isna(row.bid) else None
                loaded += 1
        logger.info(f"[quote cache] Loaded {loaded:,} entries from {path} ({len(_quote_cache):,} total in cache)")
    except Exception as e:
        logger.warning(f"[quote cache] Failed to load disk cache: {e}")


def save_quote_disk_cache() -> None:
    """Persist _quote_cache to disk after a run so the next run can skip re-fetching.

    Only writes entries where the quote is not None (missing quotes are re-fetched naturally).
    Merges with any existing cache file so entries accumulate across runs.
    """
    if not _quote_cache:
        return
    path = pathlib.Path(QUOTE_DISK_CACHE)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for (date_str, right, strike, bar_time_str), q in _quote_cache.items():
        if q is not None:
            rows.append((date_str, right, strike, bar_time_str, q["bid"], q["ask"], q["mid"]))
    if not rows:
        return
    new_df = pd.DataFrame(rows, columns=["date_str", "right", "strike", "bar_time_str", "bid", "ask", "mid"])
    if path.exists():
        try:
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["date_str", "right", "strike", "bar_time_str"], keep="last")
            combined.to_parquet(path, index=False)
            added = len(combined) - len(existing)
            logger.info(f"[quote cache] Saved {len(combined):,} entries to {path} (+{added} new)")
        except Exception as e:
            logger.warning(f"[quote cache] Failed to merge with existing cache: {e} -- writing fresh")
            new_df.to_parquet(path, index=False)
    else:
        new_df.to_parquet(path, index=False)
        logger.info(f"[quote cache] Created {path} with {len(new_df):,} entries")


async def fetch_quote_cached(
    session, date_str, expiry, right, strike, bar_time_str
) -> dict | None:
    """fetch_quote_at_time with a cache layer -- never fetches the same quote twice.

    date_str is included in the key so sweep runners (which simulate multiple days
    from a pre-fetched pool without calling clear_day_cache between days) never
    receive a cached quote from a different trading day.
    """
    key = (date_str, right, strike, bar_time_str)
    if key in _quote_cache:
        return _quote_cache[key]
    q = await fetch_quote_at_time(session, date_str, expiry, right, strike, bar_time_str)
    _quote_cache[key] = q
    return q


async def fetch_quotes_for_strikes_cached(
    session, date_str, expiry, right, strikes, bar_time_str
) -> dict:
    """Concurrent fetch for multiple strikes, served from cache where possible."""
    async def fetch_one(s):
        q = await fetch_quote_cached(session, date_str, expiry, right, s, bar_time_str)
        return s, q
    results = await asyncio.gather(*[fetch_one(s) for s in strikes])
    return {s: q for s, q in results if q is not None}


async def fetch_vix_level(session, date_str: str) -> float | None:
    """
    Fetch VIX close for a given date as an IV proxy.
    Returns None if unavailable (filters that depend on it will be skipped gracefully).
    """
    if USE_LOCAL_DATA:
        path = pathlib.Path(DATA_DIR) / date_str[:4] / "vix_ohlc" / f"{date_str}.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["close"])
        closes = df["close"].dropna()
        return float(closes.iloc[-1]) if len(closes) > 0 else None
    result = await call_with_retry(session, "index_history_eod", {
        "symbol": "VIX",
        "start_date": date_str,
        "end_date": date_str,
    })
    if result.isError:
        return None
    closes = re.findall(r'"close":\s*([\d\.]+)', result.content[0].text)
    return float(closes[-1]) if closes else None


def select_short_strike(quotes, spot, opt_type, elapsed_minutes):
    buf = max(25.0, 100.0 - (elapsed_minutes / 180.0 * 75.0))
    candidates = []
    if opt_type == "PUT":
        for strike, q in quotes.items():
            if strike < (spot - buf) and q["bid"] >= MIN_NET_CREDIT:
                candidates.append(strike)
        return min(candidates) if candidates else None
    else:
        for strike, q in quotes.items():
            if strike > (spot + buf) and q["bid"] >= MIN_NET_CREDIT:
                candidates.append(strike)
        return max(candidates) if candidates else None


# ---------------------------------------------
#  PER-DAY PROCESSOR  (split into fetch + simulate for SL sweep reuse)
# ---------------------------------------------

def _sl_label(sl) -> str:
    """Human-readable label for a stop-loss level (None -> 'none')."""
    return "none" if sl is None else str(int(sl))


async def _fetch_day_data(session, date_str: str, seed_bars: int | None = None) -> dict | None:
    """Fetch all market data for a trading day into the quote cache.

    Args:
        session:   Active MCP ClientSession.
        date_str:  Date in YYYYMMDD format.
        seed_bars: Number of prior-day bars to use for EMA seeding.
                   Defaults to EMA_SLOW. Pass max(SWEEP_EMA_SLOW) for EMA sweeps.

    Returns a dict consumed by _simulate_day, or None if the day should be skipped.
    The global _quote_cache is populated as a side-effect.
    """
    if seed_bars is None:
        seed_bars = EMA_SLOW
    logger.info(f"{'='*50}")
    logger.info(f"Processing {date_str}")
    logger.info(f"{'='*50}")

    # -- 1. Fetch OHLC (today + prior day), VIX, and strikes all in parallel --
    prev_date = (pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}") - pd.offsets.BDay(1))
    prev_str  = prev_date.strftime("%Y%m%d")
    expiry    = date_to_expiry(date_str)

    if USE_LOCAL_DATA:
        logger.info("Loading OHLC, VIX, and strikes from local parquet...")
        ohlc_path = pathlib.Path(DATA_DIR) / date_str[:4] / "spx_ohlc" / f"{date_str}.parquet"
        if not ohlc_path.exists():
            logger.error(f"No SPX OHLC parquet for {date_str}")
            return None
        df_today = pd.read_parquet(ohlc_path)
        closes   = df_today["close"].tolist()
        times    = [ts[:19] for ts in df_today["timestamp"].tolist()]  # strip ms
        day_open = float(df_today["open"].iloc[0]) if not df_today.empty else None
        logger.info(f"Got {len(closes)} 1-min bars. First close: {closes[0] if closes else 'N/A'}")
        if len(closes) < 5:
            logger.warning("Not enough bars -- skipping day.")
            return None
        prev_path = pathlib.Path(DATA_DIR) / prev_str[:4] / "spx_ohlc" / f"{prev_str}.parquet"
        seed_closes = []
        if prev_path.exists():
            df_prev     = pd.read_parquet(prev_path)
            seed_closes = df_prev["close"].tolist()[-seed_bars:]
            logger.info(f"Seeding EMA with {len(seed_closes)} bars from {prev_str}")
        else:
            logger.warning("Could not find prior day -- EMA will warm up during the day")
        vix_level, all_strikes = await asyncio.gather(
            fetch_vix_level(session, date_str),
            get_strikes(session, expiry),
        )
    else:
        logger.info(f"Fetching OHLC, VIX, and strikes in parallel...")
        hist, prev_hist, vix_level, all_strikes = await asyncio.gather(
            call_with_retry(session, "index_history_ohlc", {
                "symbol": "SPX", "start_date": date_str, "end_date": date_str, "interval": "1m",
            }),
            call_with_retry(session, "index_history_ohlc", {
                "symbol": "SPX", "start_date": prev_str, "end_date": prev_str, "interval": "1m",
            }),
            fetch_vix_level(session, date_str),
            get_strikes(session, expiry),
        )
        if hist.isError:
            logger.error(f"OHLC fetch failed: {hist.content[0].text[:100]}")
            return None
        raw    = hist.content[0].text
        closes = [float(f) for f in re.findall(r'"close":\s*([\d\.]+)', raw)]
        opens  = [float(f) for f in re.findall(r'"open":\s*([\d\.]+)', raw)]
        times  = re.findall(r'"timestamp":"([^"]+)"', raw)
        day_open = opens[0] if opens else (closes[0] if closes else None)
        logger.info(f"Got {len(closes)} 1-min bars. First close: {closes[0] if closes else 'N/A'}")
        if len(closes) < 5:
            logger.warning("Not enough bars -- skipping day.")
            return None
        seed_closes = []
        if not prev_hist.isError:
            prev_closes = [float(f) for f in re.findall(r'"close":\s*([\d\.]+)', prev_hist.content[0].text)]
            seed_closes = prev_closes[-seed_bars:]
            logger.info(f"Seeding EMA with {len(seed_closes)} bars from {prev_str}")
        else:
            logger.warning("Could not fetch prior day -- EMA will warm up during the day")
    all_closes = seed_closes + closes
    seed_len   = len(seed_closes)

    # -- Process VIX --
    if vix_level is not None:
        logger.info(f"VIX level: {vix_level:.2f}")
    else:
        logger.warning("Could not fetch VIX -- VIX-based filters disabled for today")

    # -- IV Rank filter: skip entire day if VIX is too low --
    if ENABLE_IVR_FILTER and vix_level is not None and vix_level < MIN_VIX_LEVEL:
        logger.info(f"VIX={vix_level:.2f} < {MIN_VIX_LEVEL} (MIN_VIX_LEVEL) -- skipping day (IVR filter)")
        return None

    # -- VIX day range filter --
    if VIX_MIN_FILTER is not None and vix_level is not None and vix_level < VIX_MIN_FILTER:
        logger.info(f"VIX={vix_level:.2f} < {VIX_MIN_FILTER} (VIX_MIN_FILTER) -- skipping day")
        return None
    if VIX_MAX_FILTER is not None and vix_level is not None and vix_level > VIX_MAX_FILTER:
        logger.info(f"VIX={vix_level:.2f} > {VIX_MAX_FILTER} (VIX_MAX_FILTER) -- skipping day")
        return None
    if _cfg.SKIP_VIX_RANGE is not None and vix_level is not None:
        lo, hi = _cfg.SKIP_VIX_RANGE
        if lo <= vix_level <= hi:
            logger.info(f"VIX={vix_level:.2f} in _cfg.SKIP_VIX_RANGE {_cfg.SKIP_VIX_RANGE} -- skipping day")
            return None

    # -- Calendar event skip (full day) --
    for _skip_flag, _skip_dates, _skip_label in [
        (ENABLE_CPI_SKIP,      _CPI_DATES,      "CPI"),
        (ENABLE_FOMC_SKIP,     FOMC_DATES,      "FOMC"),
        (ENABLE_PCE_SKIP,      _PCE_DATES,      "PCE"),
        (ENABLE_EOQ_SKIP,      _EOQ_DATES,      "EOQ"),
        (ENABLE_PRE_TW_SKIP,   _PRE_TW_DATES,   "pre-TW"),
        (ENABLE_POST_HOL_SKIP, _POST_HOL_DATES, "post-holiday"),
    ]:
        if _skip_flag and date_str in _skip_dates:
            logger.info(f"{_skip_label} day {date_str} -- skipping (ENABLE_{_skip_label.upper().replace('-','_')}_SKIP)")
            return None

    # -- VIX-conditional econ skip (event+VIX combos that are net negative) --
    if _cfg.ENABLE_ECON_VIX_SKIP and vix_level is not None:
        _event_date_map = {
            "fomc": FOMC_DATES,
            "cpi": _CPI_DATES,
            "pce": _PCE_DATES,
            "nfp": _NFP_DATES,
        }
        for _evt_type, _vix_lo, _vix_hi in _cfg.ECON_VIX_SKIP_RULES:
            _evt_dates = _event_date_map.get(_evt_type, set())
            if date_str in _evt_dates and _vix_lo <= vix_level < _vix_hi:
                logger.info(f"Econ+VIX skip: {_evt_type} day {date_str} VIX={vix_level:.1f} in [{_vix_lo},{_vix_hi}) -- skipping")
                return None

    # -- Daily indicator filters --
    if _DAILY_INDICATORS and not _passes_active_day_filters(date_str, vix_level):
        logger.info(f"Day filter blocked {date_str} -- skipping")
        return None

    # -- Economic calendar: block new entries on high-impact event days --
    econ_skip_entries = ENABLE_ECON_FILTER and date_str in ECON_DATES
    if econ_skip_entries:
        logger.info(f"Economic event date {date_str} -- new entries disabled today (econ filter)")

    # -- VIX regime: determine effective contract qty --
    in_high_vix_regime = ENABLE_VIX_REGIME and vix_level is not None and vix_level >= HIGH_VIX_THRESHOLD
    in_low_vix_half = (
        ENABLE_LOW_VIX_HALF_SIZE and vix_level is not None and
        (vix_level < LOW_VIX_THRESHOLD or MID_VIX_BAND[0] <= vix_level <= MID_VIX_BAND[1])
    )
    if _cfg.ENABLE_KELLY_SIZING and vix_level is not None:
        kelly_qty = next(
            (qty for lo, hi, qty in KELLY_ZONE_QTY if lo <= vix_level < hi),
            QTY
        )
        trade_qty = kelly_qty
        logger.info(f"VIX={vix_level:.2f} -- Kelly sizing -> qty={kelly_qty}")
    elif in_high_vix_regime:
        trade_qty = HIGH_VIX_QTY
        logger.info(f"VIX={vix_level:.2f} >= {HIGH_VIX_THRESHOLD} (HIGH_VIX_THRESHOLD) -- reducing size to {HIGH_VIX_QTY} contracts")
    elif in_low_vix_half:
        trade_qty = LOW_VIX_QTY
        logger.info(f"VIX={vix_level:.2f} in bad zone (<{LOW_VIX_THRESHOLD} or {MID_VIX_BAND}) -- reducing size to {LOW_VIX_QTY} contracts")
    else:
        trade_qty = QTY

    # -- Process strikes --
    if not all_strikes:
        logger.warning("No strikes found -- skipping day.")
        return None

    # -- 2b. Clear quote cache for this day --
    clear_day_cache()

    prior_close = seed_closes[-1] if seed_closes else None

    return {
        "date_str":          date_str,
        "closes":            closes,
        "times":             times,
        "all_closes":        all_closes,
        "seed_len":          seed_len,
        "all_strikes":       all_strikes,
        "vix_level":         vix_level,
        "trade_qty":         trade_qty,
        "prior_close":       prior_close,
        "day_open":          day_open,
        "expiry":            expiry,
        "econ_skip_entries": econ_skip_entries,
    }


async def _simulate_day(
    session,
    day_data: dict,
    daily_sl,
    baseline_mode=None,
    ema_fast: int | None = None,
    ema_slow: int | None = None,
    trailing_stop: float | None = None,
    spread_width: float | None = None,
    pos_trail_activation: float | None = None,
    pos_trail_pullback: float | None = None,
    min_credit: float | None = None,
    max_credit: float | None = None,
    min_otm_distance: float | None = None,
    entry_start: time | None = None,
    entry_end: time | None = None,
    entry_interval: int | None = None,
    daily_tp: float | None = "USE_GLOBAL",
    pnl_sample_interval: int | None = None,
    max_buying_power: float | None = None,
    price_chg_pct_min: float | None = None,
    price_chg_pct_max: float | None = None,
    price_chg_sd_min: float | None = None,
    price_chg_sd_max: float | None = None,
    open_chg_pct_min: float | None = None,
    open_chg_pct_max: float | None = None,
    touch_exit_dollars: float | None = "USE_GLOBAL",
    touch_exit_pct: float | None = "USE_GLOBAL",
    pressure_vix_min: float | None = "USE_GLOBAL",
    pressure_vix_max: float | None = "USE_GLOBAL",
    enable_pressure_filter: bool | None = None,
    per_pos_sl: float | None = "USE_GLOBAL",
    sl_gap_minutes: int | None = "USE_GLOBAL",
) -> tuple:
    """Run the intraday simulation using the pre-populated quote cache.

    Args:
        day_data:      dict returned by _fetch_day_data.
        daily_sl:      stop-loss threshold (float) or None for no stop loss.
        trailing_stop: dollar amount to trail from peak portfolio P&L (positive float),
                       or None to disable. Triggers only after peak P&L has gone positive.
        baseline_mode: None           -> normal EMA direction (main strategy)
                       "always_put"   -> always trade PUT spreads
                       "always_call"  -> always trade CALL spreads
                       "no_ema_filter"-> enter every bar using EMA direction (control baseline)
                       "both_sides"   -> enter PUT + CALL spread on every bar (iron condor style)
        ema_fast:      Fast EMA period override (defaults to global EMA_FAST).
        ema_slow:      Slow EMA period override (defaults to global EMA_SLOW).

    Returns:
        (trades, day_pnl) -- trades are NOT written to any file.
    """
    if ema_fast is None:
        ema_fast = EMA_FAST
    if ema_slow is None:
        ema_slow = EMA_SLOW
    if spread_width is None:
        spread_width = WIDTH
    if daily_tp == "USE_GLOBAL":
        daily_tp = DAILY_TP
    if touch_exit_dollars == "USE_GLOBAL":
        touch_exit_dollars = TOUCH_EXIT_DOLLARS
    if touch_exit_pct == "USE_GLOBAL":
        touch_exit_pct = TOUCH_EXIT_PCT
    if pnl_sample_interval is None:
        pnl_sample_interval = PNL_SAMPLE_INTERVAL
    date_str          = day_data["date_str"]
    closes            = day_data["closes"]
    times             = day_data["times"]
    all_closes        = day_data["all_closes"]
    all_strikes       = day_data["all_strikes"]
    vix_level         = day_data["vix_level"]
    trade_qty         = day_data["trade_qty"]
    econ_skip_entries = day_data["econ_skip_entries"]
    expiry            = day_data["expiry"]
    prior_close       = day_data.get("prior_close")
    day_open          = day_data.get("day_open")

    day_trades_log    = []
    active_positions  = []
    stopped_today     = False
    sl_gap_resume_time = None  # set when SL gap re-entry is active; None = no gap pending
    daily_trades      = 0
    current_day_pnl   = 0.0
    peak_day_pnl      = 0.0  # highest portfolio P&L seen this day (for trailing stop)
    # Opening skew -- computed once at the first entry bar, stamped on all trades
    _skew_put   = None  # credit for PUT spread at OPENING_SKEW_OTM distance
    _skew_call  = None  # credit for CALL spread at OPENING_SKEW_OTM distance
    _skew_ratio = None  # put_credit / call_credit
    # NOTE: `offset` is intentionally named to match the original variable so that
    # the inner strike-search loop `for offset in range(200, 0, -5)` replicates the
    # original shadowing behaviour exactly.
    offset = day_data["seed_len"]

    for i in range(len(closes)):
        dt         = datetime.fromisoformat(times[i].replace('Z', ''))
        curr_time  = dt.time()
        curr_price = closes[i]
        bar_time   = curr_time.strftime("%H:%M:%S")
        bar_label  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {bar_time}"
        ai         = offset + i  # index into all_closes (seed + today)

        is_eod        = (curr_time == market_close_time(date_str))
        is_sample_bar = (curr_time.minute % pnl_sample_interval == 0)
        should_mtm    = is_sample_bar or is_eod

        # -- 3. Mark-to-market --
        if active_positions and should_mtm:
            put_legs  = list(set(
                s for p in active_positions if p["option_type"] == "PUT"
                for s in [p["short_strike"], p["long_strike"]]
            ))
            call_legs = list(set(
                s for p in active_positions if p["option_type"] == "CALL"
                for s in [p["short_strike"], p["long_strike"]]
            ))
            put_quotes, call_quotes = await asyncio.gather(
                fetch_quotes_for_strikes_cached(session, date_str, expiry, "P", put_legs,  bar_time),
                fetch_quotes_for_strikes_cached(session, date_str, expiry, "C", call_legs, bar_time),
            )
            live = {"P": put_quotes, "C": call_quotes}

            current_day_pnl = 0.0
            for pos in active_positions:
                right   = "P" if pos["option_type"] == "PUT" else "C"
                short_q = live[right].get(pos["short_strike"])
                long_q  = live[right].get(pos["long_strike"])
                if short_q and long_q:
                    # Exit: buy back short at ask, sell long at bid
                    pos["last_short_ask"] = short_q["ask"]
                    pos["last_long_bid"]  = long_q["bid"]
                else:
                    pos["stale_bars"] += 1
                    logger.debug(f"[{bar_label}] Stale quote for {pos['option_type']} {pos['short_strike']}/{pos['long_strike']} (stale_bars={pos['stale_bars']})")
                # Always recalculate using best available prices (fresh or last known)
                close_cost        = pos["last_short_ask"] - pos["last_long_bid"]
                pos["pnl_earned"] = (pos["credit_received"] - close_cost) * pos["qty"] * 100.0
                current_day_pnl += pos["pnl_earned"]

            # Update per-position peak P&L and portfolio-level peak for trailing stop
            for pos in active_positions:
                pos["peak_pnl"] = max(pos.get("peak_pnl", 0.0), pos["pnl_earned"])
            peak_day_pnl = max(peak_day_pnl, current_day_pnl)

            # -- Per-position trailing stop --
            if pos_trail_activation is not None and pos_trail_pullback is not None and active_positions:
                pos_to_close = []
                for pos in active_positions:
                    credit_dollars     = pos["credit_received"] * pos["qty"] * 100.0
                    activation_dollars = credit_dollars * pos_trail_activation / 100.0
                    pullback_dollars   = credit_dollars * pos_trail_pullback   / 100.0
                    if (pos["peak_pnl"] >= activation_dollars
                            and pos["pnl_earned"] <= pos["peak_pnl"] - pullback_dollars):
                        pos_to_close.append(pos)
                for pos in pos_to_close:
                    active_positions.remove(pos)
                    commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                    pos["pnl_earned"] -= commission_per_pos
                    pos.update({
                        "outcome": "POS_TRAIL_STOP",
                        "profit_price": curr_price,
                        "win":  1 if pos["pnl_earned"] > 0 else 0,
                        "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                        "close_date": date_str, "close_time": bar_time,
                        "profit_date_time": f"{date_str} {bar_time}",
                    })
                    day_trades_log.append(pos)
                    logger.debug(
                        f"[{bar_label}] POS_TRAIL_STOP: {pos['option_type']} "
                        f"{pos['short_strike']}/{pos['long_strike']} | P&L=${pos['pnl_earned']:.2f}"
                    )
                if pos_to_close:
                    current_day_pnl = sum(p["pnl_earned"] for p in active_positions)

            # -- Per-Position Fixed Stop Loss --
            _per_pos_sl = (PER_POS_SL_AMOUNT if ENABLE_PER_POS_SL else None) if per_pos_sl == "USE_GLOBAL" else per_pos_sl
            if _per_pos_sl is not None and active_positions:
                pos_sl_to_close = [p for p in active_positions if p["pnl_earned"] <= _per_pos_sl]
                for pos in pos_sl_to_close:
                    active_positions.remove(pos)
                    commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                    pos["pnl_earned"] -= commission_per_pos
                    pos.update({
                        "outcome": "PER_POS_SL",
                        "profit_price": curr_price,
                        "win":  1 if pos["pnl_earned"] > 0 else 0,
                        "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                        "close_date": date_str, "close_time": bar_time,
                        "profit_date_time": f"{date_str} {bar_time}",
                    })
                    day_trades_log.append(pos)
                    logger.debug(
                        f"[{bar_label}] PER_POS_SL: {pos['option_type']} "
                        f"{pos['short_strike']}/{pos['long_strike']} | P&L=${pos['pnl_earned']:.2f}"
                    )
                if pos_sl_to_close:
                    current_day_pnl = sum(p["pnl_earned"] for p in active_positions)

            # -- Per-position Touch Exit --
            # Closes individual positions when the underlying crosses within X dollars
            # (or X%) of the short strike. Positive = OTM buffer; 0 = ATM; negative = ITM.
            if (touch_exit_dollars is not None or touch_exit_pct is not None) and active_positions:
                touch_to_close = []
                for pos in active_positions:
                    short_s = pos["short_strike"]
                    if pos["option_type"] == "PUT":
                        if touch_exit_dollars is not None:
                            triggered = curr_price <= short_s + touch_exit_dollars
                        else:
                            triggered = curr_price <= short_s * (1.0 + touch_exit_pct / 100.0)
                    else:  # CALL
                        if touch_exit_dollars is not None:
                            triggered = curr_price >= short_s - touch_exit_dollars
                        else:
                            triggered = curr_price >= short_s * (1.0 - touch_exit_pct / 100.0)
                    if triggered:
                        touch_to_close.append(pos)
                for pos in touch_to_close:
                    active_positions.remove(pos)
                    commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                    pos["pnl_earned"] -= commission_per_pos
                    pos.update({
                        "outcome": "TOUCH_EXIT",
                        "profit_price": curr_price,
                        "win":  1 if pos["pnl_earned"] > 0 else 0,
                        "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                        "close_date": date_str, "close_time": bar_time,
                        "profit_date_time": f"{date_str} {bar_time}",
                    })
                    day_trades_log.append(pos)
                    logger.debug(
                        f"[{bar_label}] TOUCH_EXIT: {pos['option_type']} "
                        f"{pos['short_strike']}/{pos['long_strike']} | spot={curr_price:.2f} | "
                        f"P&L=${pos['pnl_earned']:.2f}"
                    )
                if touch_to_close:
                    current_day_pnl = sum(p["pnl_earned"] for p in active_positions)

            # -- Per-position Premium Buyback Exit --
            # Close when remaining close cost <= PREMIUM_BUYBACK_EXIT threshold.
            if _cfg.PREMIUM_BUYBACK_EXIT is not None and active_positions:
                buyback_to_close = []
                for pos in active_positions:
                    close_cost = pos["last_short_ask"] - pos["last_long_bid"]
                    if close_cost <= _cfg.PREMIUM_BUYBACK_EXIT:
                        buyback_to_close.append(pos)
                for pos in buyback_to_close:
                    active_positions.remove(pos)
                    commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                    pos["pnl_earned"] -= commission_per_pos
                    pos.update({
                        "outcome": "BUYBACK_EXIT",
                        "profit_price": curr_price,
                        "win":  1 if pos["pnl_earned"] > 0 else 0,
                        "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                        "close_date": date_str, "close_time": bar_time,
                        "profit_date_time": f"{date_str} {bar_time}",
                    })
                    day_trades_log.append(pos)
                    logger.debug(
                        f"[{bar_label}] BUYBACK_EXIT: {pos['option_type']} "
                        f"{pos['short_strike']}/{pos['long_strike']} | "
                        f"close_cost={close_cost:.3f} | P&L=${pos['pnl_earned']:.2f}"
                    )
                if buyback_to_close:
                    current_day_pnl = sum(p["pnl_earned"] for p in active_positions)

            dd = current_day_pnl - peak_day_pnl
            logger.debug(f"[{bar_label}] MTM: {len(active_positions)} open positions | day P&L=${current_day_pnl:.2f} | peak=${peak_day_pnl:.2f} | dd=${dd:.2f}")

        # -- 4. Settlement --
        outcome = ""
        if active_positions:
            if daily_tp is not None and current_day_pnl >= daily_tp:
                outcome = "PROFIT_TARGET"
            elif (trailing_stop is not None
                  and peak_day_pnl > 0
                  and current_day_pnl <= peak_day_pnl - trailing_stop):
                outcome = "TRAILING_STOP"
            elif daily_sl is not None and current_day_pnl <= daily_sl:
                outcome = "STOP_LOSS"
            elif is_eod:
                outcome = "EXPIRATION"

        if outcome:
            logger.info(f"[{bar_label}] CLOSING all positions -> {outcome} | P&L=${current_day_pnl:.2f} (before commission)")
            for pos in active_positions:
                # 2 legs × 2 (open + close) × position qty × commission rate
                commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                if pos["stale_bars"] > 0:
                    logger.warning(f"  Stale data: {pos['option_type']} {pos['short_strike']}/{pos['long_strike']} had {pos['stale_bars']} bars with missing quotes -- P&L may be inaccurate")
                pos["pnl_earned"] -= commission_per_pos
                pos.update({
                    "outcome": outcome, "profit_price": curr_price,
                    "win": 1 if pos["pnl_earned"] > 0 else 0,
                    "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                    "close_date": date_str, "close_time": bar_time,
                    "profit_date_time": f"{date_str} {bar_time}",
                })
                day_trades_log.append(pos)
            active_positions = []
            _gap_mins = (SL_GAP_MINUTES if ENABLE_SL_GAP_REENTRY else None) if sl_gap_minutes == "USE_GLOBAL" else sl_gap_minutes
            if outcome == "STOP_LOSS" and _gap_mins is not None:
                # Pause for _gap_mins then allow re-entry; don't set stopped_today
                _bar_dt = datetime.strptime(f"{date_str} {bar_time}", "%Y%m%d %H:%M:%S")
                sl_gap_resume_time = (_bar_dt + pd.Timedelta(minutes=_gap_mins)).time()
            elif outcome != "EXPIRATION":
                stopped_today = True

        # -- 5. Entry --
        _entry_start    = entry_start    if entry_start    is not None else ENTRY_START
        _entry_end      = entry_end      if entry_end      is not None else ENTRY_END
        _entry_interval = entry_interval if entry_interval is not None else ENTRY_INTERVAL
        in_window   = _entry_start <= curr_time <= _entry_end
        on_interval = (dt.minute % _entry_interval == 0)
        bayesian_gate_ok = (_cfg.INTRADAY_ENTRY_GATE is None or current_day_pnl >= _cfg.INTRADAY_ENTRY_GATE)

        # PRESSURE FILTER ---
        is_under_pressure = False
        _pressure_on = ENABLE_PRESSURE_FILTER if enable_pressure_filter is None else enable_pressure_filter
        if _pressure_on:
            _pvix_min = PRESSURE_FILTER_VIX_MIN if pressure_vix_min == "USE_GLOBAL" else pressure_vix_min
            _pvix_max = PRESSURE_FILTER_VIX_MAX if pressure_vix_max == "USE_GLOBAL" else pressure_vix_max
            _vix_gate_ok = (
                (_pvix_min is None or (vix_level is not None and vix_level >= _pvix_min)) and
                (_pvix_max is None or (vix_level is not None and vix_level <  _pvix_max))
            )
            if _vix_gate_ok:
                for pos in active_positions:
                    s_strike = pos['short_strike']
                    dist = (curr_price - s_strike) if pos['option_type'] == 'PUT' else (s_strike - curr_price)
                    if dist < PRESSURE_DISTANCE_THRESHOLD:
                        is_under_pressure = True
                        break

        _max_trades = MAX_TRADES_DAY
        if (MAX_TRADES_DAY_VIX_LO_HI is not None and vix_level is not None
                and 15.0 <= vix_level < 20.0):
            _max_trades = MAX_TRADES_DAY_VIX_LO_HI
        sl_gap_ok = (sl_gap_resume_time is None or curr_time >= sl_gap_resume_time)
        can_enter = in_window and on_interval and not stopped_today and sl_gap_ok and daily_trades < _max_trades and not econ_skip_entries and bayesian_gate_ok and not is_under_pressure

        if not can_enter:
            continue

        e13 = calculate_ema(all_closes[max(0, ai - ema_fast):ai + 1], ema_fast)
        e48 = calculate_ema(all_closes[max(0, ai - ema_slow):ai + 1], ema_slow)

        if baseline_mode == "always_put":
            sides_to_enter = [("PUT", "P")]
        elif baseline_mode == "always_call":
            sides_to_enter = [("CALL", "C")]
        elif baseline_mode == "both_sides":
            sides_to_enter = [("PUT", "P"), ("CALL", "C")]
        else:
            # Normal EMA direction (also used for "no_ema_filter" control baseline)
            direction = 1 if e13 > e48 else -1
            opt_type  = "PUT" if direction == 1 else "CALL"
            right     = "P"   if direction == 1 else "C"
            sides_to_enter = [(opt_type, right)]
        elapsed   = (dt.hour - 9) * 60 + (dt.minute - 45)

        # -- Opening skew: compute once at first entry bar --
        if COMPUTE_OPENING_SKEW and _skew_put is None and daily_trades == 0:
            try:
                otm = OPENING_SKEW_OTM
                sw  = int(WIDTH)
                p_short = int(round((curr_price - otm) / 5.0) * 5)
                p_long  = p_short - sw
                c_short = int(round((curr_price + otm) / 5.0) * 5)
                c_long  = c_short + sw
                p_quotes, c_quotes = await asyncio.gather(
                    fetch_quotes_for_strikes_cached(session, date_str, expiry, "P", [p_short, p_long], bar_time),
                    fetch_quotes_for_strikes_cached(session, date_str, expiry, "C", [c_short, c_long], bar_time),
                )
                psq = p_quotes.get(p_short); plq = p_quotes.get(p_long)
                csq = c_quotes.get(c_short); clq = c_quotes.get(c_long)
                if psq and plq:
                    _skew_put = round(psq["bid"] - plq["ask"], 4)
                if csq and clq:
                    _skew_call = round(csq["bid"] - clq["ask"], 4)
                if _skew_put is not None and _skew_call is not None and _skew_call > 0:
                    _skew_ratio = round(_skew_put / _skew_call, 4)
                logger.debug(f"[{bar_label}] Opening skew @ {otm}pt OTM -- PUT={_skew_put} CALL={_skew_call} ratio={_skew_ratio}")
            except Exception as e:
                logger.debug(f"[{bar_label}] Opening skew compute failed: {e}")

        for opt_type, right in sides_to_enter:

            # Fetch strikes from 10 OTM out to (200 + spread_width) OTM so the long leg
            # is always in the chain regardless of spread width.
            otm_min, otm_max = 10, 200 + int(spread_width)
            if opt_type == "PUT":
                lo, hi = curr_price - otm_max, curr_price - otm_min
            else:
                lo, hi = curr_price + otm_min, curr_price + otm_max
            candidate_strikes = [s for s in all_strikes if lo <= s <= hi]

            chain = await fetch_quotes_for_strikes_cached(session, date_str, expiry, right, candidate_strikes, bar_time)

            # Loop from 200 OTM down by 5 until we find a spread with credit >= min_credit threshold
            credit_threshold = min_credit if min_credit is not None else MIN_NET_CREDIT
            credit_cap      = max_credit if max_credit is not None else _cfg.MAX_NET_CREDIT
            otm_floor = min_otm_distance if min_otm_distance is not None else _cfg.MIN_OTM_DISTANCE
            if (ENABLE_OTM_DISTANCE_VIX_RANGE and MIN_OTM_DISTANCE_VIX_LO_HI is not None
                    and vix_level is not None
                    and MIN_OTM_DISTANCE_VIX_RANGE_LO <= vix_level < MIN_OTM_DISTANCE_VIX_RANGE_HI):
                otm_floor = max(otm_floor or 0, MIN_OTM_DISTANCE_VIX_LO_HI)
            short_strike = long_strike = short_q = long_q = credit = None
            for offset in range(200, 0, -5):
                if otm_floor is not None and offset < otm_floor:
                    break  # don't enter closer than min OTM distance
                if opt_type == "PUT":
                    s = int(round((curr_price - offset) / 5.0) * 5)
                    l = s - int(spread_width)
                else:
                    s = int(round((curr_price + offset) / 5.0) * 5)
                    l = s + int(spread_width)
                sq = chain.get(s)
                lq = chain.get(l)
                if not sq or not lq:
                    continue
                c = sq["bid"] - lq["ask"]  # real entry: sell short at bid, buy long at ask
                if c >= credit_threshold:
                    if credit_cap is not None and c > credit_cap:
                        logger.debug(f"[{bar_label}] Skipping offset={offset}: credit={c:.3f} exceeds cap={credit_cap}")
                        break  # closest qualifying strike already exceeds cap -- skip entry
                    short_strike, long_strike, short_q, long_q, credit = s, l, sq, lq, c
                    break

            if short_strike is None:
                logger.warning(f"[{bar_label}] No spread found with credit >= {credit_threshold} -- skipping.")
                continue

            # -- Net delta check --
            current_net_delta = sum(
                approx_spread_delta(curr_price, p["short_strike"], p["long_strike"])
                for p in active_positions
            )
            new_delta = approx_spread_delta(curr_price, short_strike, long_strike)
            projected_delta = current_net_delta + new_delta
            logger.debug(f"[{bar_label}] Net delta: current={current_net_delta:.3f} new={new_delta:.3f} projected={projected_delta:.3f} limit=±{NET_DELTA_LIMIT}")
            if ENABLE_DELTA_LIMIT and abs(projected_delta) > NET_DELTA_LIMIT:
                logger.warning(f"[{bar_label}] Net delta limit breached ({projected_delta:.3f}) -- skipping {opt_type} spread.")
                continue

            # -- Buying power cap: dynamically reduce qty to stay within budget --
            entry_qty = trade_qty
            if max_buying_power is not None:
                committed_bp  = sum((p["spread_width"] - p["credit_received"]) * p["qty"] * 100 for p in active_positions)
                available_bp  = max_buying_power - committed_bp
                bp_per_contract = (spread_width - credit) * 100
                max_qty = int(available_bp // bp_per_contract) if bp_per_contract > 0 else 0
                entry_qty = min(trade_qty, max_qty)
                if entry_qty <= 0:
                    logger.warning(f"[{bar_label}] Buying power limit (${max_buying_power:,.0f}) reached -- skipping entry.")
                    continue

            # -- Price change % from prior close filter --
            if prior_close and (price_chg_pct_min is not None or price_chg_pct_max is not None):
                chg_pct = (curr_price - prior_close) / prior_close * 100
                if price_chg_pct_min is not None and chg_pct < price_chg_pct_min:
                    logger.warning(f"[{bar_label}] Price chg {chg_pct:.2f}% < min {price_chg_pct_min}% -- skipping entry.")
                    continue
                if price_chg_pct_max is not None and chg_pct > price_chg_pct_max:
                    logger.warning(f"[{bar_label}] Price chg {chg_pct:.2f}% > max {price_chg_pct_max}% -- skipping entry.")
                    continue

            # -- Price change in standard deviations from prior close --
            # daily_sigma = prior_close × (VIX/100) / √252 (VIX-implied 1-day 1σ move)
            if prior_close and vix_level and (price_chg_sd_min is not None or price_chg_sd_max is not None):
                daily_sigma = prior_close * (vix_level / 100.0) / math.sqrt(252)
                if daily_sigma > 0:
                    chg_sd = (curr_price - prior_close) / daily_sigma
                    if price_chg_sd_min is not None and chg_sd < price_chg_sd_min:
                        logger.warning(f"[{bar_label}] Price chg {chg_sd:.2f}σ < min {price_chg_sd_min}σ -- skipping entry.")
                        continue
                    if price_chg_sd_max is not None and chg_sd > price_chg_sd_max:
                        logger.warning(f"[{bar_label}] Price chg {chg_sd:.2f}σ > max {price_chg_sd_max}σ -- skipping entry.")
                        continue

            # -- Price change % from day open to entry bar --
            if day_open and (open_chg_pct_min is not None or open_chg_pct_max is not None):
                open_chg = (curr_price - day_open) / day_open * 100
                if open_chg_pct_min is not None and open_chg < open_chg_pct_min:
                    logger.warning(f"[{bar_label}] Open chg {open_chg:.2f}% < min {open_chg_pct_min}% -- skipping entry.")
                    continue
                if open_chg_pct_max is not None and open_chg > open_chg_pct_max:
                    logger.warning(f"[{bar_label}] Open chg {open_chg:.2f}% > max {open_chg_pct_max}% -- skipping entry.")
                    continue

            strike_dist = round(abs(short_strike - curr_price))
            logger.info(f"[{bar_label}] {opt_type.lower()} spread spot={curr_price:.2f} | {short_strike}/{long_strike} credit=${round(credit*entry_qty*100)} | dist={strike_dist}pts")
            active_positions.append({
                "entry_date": date_str, "entry_time": bar_time,
                "option_type": opt_type,
                "short_strike": short_strike, "long_strike": long_strike,
                "width": spread_width, "spread_width": spread_width, "credit_received": credit,
                "entry_short_bid": short_q["bid"], "entry_short_ask": short_q["ask"], "entry_short_mid": short_q["mid"],
                "entry_long_bid":  long_q["bid"],  "entry_long_ask":  long_q["ask"],  "entry_long_mid":  long_q["mid"],
                "profit_target": DAILY_TP, "stop_loss": daily_sl if daily_sl is not None else "none",
                "ema13": round(e13, 2), "ema48": round(e48, 2),
                "qty": entry_qty, "vix_level": round(vix_level, 2) if vix_level is not None else "",
                "strike_distance": strike_dist,
                "opening_put_credit":  _skew_put   if _skew_put  is not None else "",
                "opening_call_credit": _skew_call  if _skew_call is not None else "",
                "pc_skew_ratio":       _skew_ratio if _skew_ratio is not None else "",
                "pnl_earned": 0.0, "peak_pnl": 0.0, "last_short_ask": short_q["ask"], "last_long_bid": long_q["bid"],
                "stale_bars": 0,
                "outcome": "", "profit_price": None,
                "win": 0, "loss": 0, "close_date": "", "close_time": "", "profit_date_time": "",
            })
            daily_trades += 1

    day_pnl = sum(t["pnl_earned"] for t in day_trades_log)
    logger.info(f"Day complete: {len(day_trades_log)} trades | day P&L=${day_pnl:.2f}")
    return day_trades_log, day_pnl


def _get_effective_sl(day_data: dict, date_str: str) -> "float | None":
    """Return the correct stop-loss for a day, applying dynamic SL and month/dir rules.

    Shared by process_day() and sweep runners so all paths use identical SL logic.
    """
    vix = day_data.get("vix_level")
    # in_danger is computed regardless of ENABLE_DYNAMIC_SL so calendar event
    # dynamic SL variants (EOM/FOMC/PCE/Pre-TW) can also use it.
    in_danger = vix is not None and (
        (vix < DYNAMIC_SL_VIX_LOW) or
        (DYNAMIC_SL_VIX_HIGH[0] <= vix <= DYNAMIC_SL_VIX_HIGH[1]) or
        (_cfg.DYNAMIC_SL_VIX_MID[0] <= vix <= _cfg.DYNAMIC_SL_VIX_MID[1])
    )

    if ENABLE_DYNAMIC_SL and vix is not None:
        effective_sl = DYNAMIC_SL_AMOUNT if in_danger else DAILY_SL
    else:
        effective_sl = DAILY_SL

    if ENABLE_MONTH_DIR_SL and effective_sl is None:
        month = int(date_str[4:6])
        rule = MONTH_DIR_SL_RULES.get(month)
        if rule:
            direction = _get_baseline_mode(date_str)
            day_is_call = direction == "always_call"
            apply = (rule == "BOTH") or (rule == "CALL" and day_is_call)
            if apply:
                effective_sl = MONTH_DIR_SL_AMOUNT

    if ENABLE_GAP_CALL_SL and effective_sl is None:
        direction = _get_baseline_mode(date_str)
        if direction == "always_call":
            ind = _DAILY_INDICATORS.get(date_str) or {}
            gap_pct = ind.get("dGapPercent")
            if gap_pct is not None and gap_pct < 0:
                effective_sl = GAP_CALL_SL_AMOUNT

    if ENABLE_VIX_MID_SAFE_SL and vix is not None:
        if VIX_MID_SAFE_SL_RANGE[0] <= vix < VIX_MID_SAFE_SL_RANGE[1]:
            candidate = VIX_MID_SAFE_SL_AMOUNT
            if effective_sl is None:
                effective_sl = candidate
            else:
                effective_sl = max(effective_sl, candidate)

    if ENABLE_VIX_SUB12_SL and day_data.get("vix_level") is not None:
        vix = day_data["vix_level"]
        if vix < VIX_SUB12_THRESHOLD:
            candidate = VIX_SUB12_SL_AMOUNT
            if effective_sl is None:
                effective_sl = candidate
            else:
                effective_sl = max(effective_sl, candidate)  # use tighter (less negative)

    if ENABLE_EOM_SL and date_str in _EOM_DATES:
        candidate = EOM_SL_AMOUNT_DANGER if in_danger else EOM_SL_AMOUNT
        if effective_sl is None:
            effective_sl = candidate
        else:
            effective_sl = max(effective_sl, candidate)  # use tighter (less negative)

    # (flag, date_set, normal_amt, danger_amt)
    # danger_amt == normal_amt for types without a VIX-tiered variant
    for _flag, _dates, _amt, _amt_danger in [
        (ENABLE_CPI_SL,      _CPI_DATES,      CPI_SL_AMOUNT,      CPI_SL_AMOUNT),
        (ENABLE_FOMC_SL,     FOMC_DATES,      FOMC_SL_AMOUNT,     FOMC_SL_AMOUNT_DANGER),
        (ENABLE_PCE_SL,      _PCE_DATES,      PCE_SL_AMOUNT,      PCE_SL_AMOUNT_DANGER),
        (ENABLE_EOQ_SL,      _EOQ_DATES,      EOQ_SL_AMOUNT,      EOQ_SL_AMOUNT),
        (ENABLE_PRE_TW_SL,   _PRE_TW_DATES,   PRE_TW_SL_AMOUNT,   PRE_TW_SL_AMOUNT_DANGER),
        (ENABLE_POST_HOL_SL, _POST_HOL_DATES, POST_HOL_SL_AMOUNT, POST_HOL_SL_AMOUNT),
    ]:
        if _flag and date_str in _dates:
            effective_amt = _amt_danger if in_danger else _amt
            if effective_sl is None:
                effective_sl = effective_amt
            else:
                effective_sl = max(effective_sl, effective_amt)  # tighter wins

    # FOMC + VIX 15-20 targeted SL (Finding 7: 5 of 9 worst unprotected days)
    if _cfg.ENABLE_FOMC_VIX1520_SL and date_str in FOMC_DATES and vix is not None:
        lo, hi = _cfg.FOMC_VIX1520_RANGE
        if lo <= vix < hi:
            candidate = _cfg.FOMC_VIX1520_SL_AMOUNT
            if effective_sl is None:
                effective_sl = candidate
            else:
                effective_sl = max(effective_sl, candidate)  # tighter wins

    return effective_sl


async def process_day(session, date_str: str) -> tuple:
    """Fetch data for one trading day and run the simulation with the configured DAILY_SL.

    Writes each closed trade to _cfg.SAVE_FILE (existing behaviour preserved).
    """
    day_data = await _fetch_day_data(session, date_str)
    if day_data is None:
        return [], 0.0

    effective_sl = _get_effective_sl(day_data, date_str)
    in_danger = False
    if ENABLE_DYNAMIC_SL and day_data.get("vix_level") is not None:
        vix = day_data["vix_level"]
        in_danger = (
            (vix < DYNAMIC_SL_VIX_LOW) or
            (DYNAMIC_SL_VIX_HIGH[0] <= vix <= DYNAMIC_SL_VIX_HIGH[1]) or
            (_cfg.DYNAMIC_SL_VIX_MID[0] <= vix <= _cfg.DYNAMIC_SL_VIX_MID[1])
        )
        if in_danger:
            logger.info(f"Dynamic SL active: VIX={vix:.2f} in danger zone -> SL=${DYNAMIC_SL_AMOUNT:.0f}, MTM interval={DANGER_PNL_SAMPLE_INTERVAL}min")
    if ENABLE_MONTH_DIR_SL and effective_sl == MONTH_DIR_SL_AMOUNT:
        month = int(date_str[4:6])
        rule  = MONTH_DIR_SL_RULES.get(month, "")
        direction = _get_baseline_mode(date_str)
        logger.info(f"Month/Dir SL active: month={month} rule={rule} direction={'CALL' if direction == 'always_call' else 'PUT'} -> SL=${MONTH_DIR_SL_AMOUNT:.0f}")

    sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
    baseline_mode = _get_baseline_mode(date_str)
    trades, day_pnl = await _simulate_day(
        session, day_data, effective_sl,
        pos_trail_activation=POS_TRAIL_ACTIVATION,
        pos_trail_pullback=POS_TRAIL_PULLBACK,
        baseline_mode=baseline_mode,
        min_otm_distance=_cfg.MIN_OTM_DISTANCE,
        max_credit=_cfg.MAX_NET_CREDIT,
        pnl_sample_interval=sample_interval,
    )
    for pos in trades:
        append_trade(pos)
    return trades, day_pnl


# ---------------------------------------------
#  METRICS HELPER  (shared by normal run + sweep)
# ---------------------------------------------
def compute_metrics(all_trades: list) -> dict:
    """Compute summary performance metrics for a list of closed trades."""
    import math
    pnls   = [t["pnl_earned"] for t in all_trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    total_pnl     = sum(pnls)
    win_rate      = len(wins) / n * 100 if n else 0.0
    avg_win       = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss      = sum(losses) / len(losses) if losses else 0.0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)

    # Sharpe & Sortino -- daily P&L basis, rf-adjusted, annualised with sqrt(252)
    day_pnls_d: dict = {}
    for t in all_trades:
        day_pnls_d.setdefault(t["entry_date"], 0.0)
        day_pnls_d[t["entry_date"]] += t["pnl_earned"]
    daily_vals  = list(day_pnls_d.values())
    n_days      = len(daily_vals)
    rf_daily    = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    mean_d      = sum(daily_vals) / n_days if n_days else 0.0
    var_d       = sum((p - mean_d) ** 2 for p in daily_vals) / n_days if n_days else 0.0
    std_d       = math.sqrt(var_d)
    sharpe      = ((mean_d - rf_daily) / std_d * math.sqrt(252)) if std_d else 0.0
    down_d      = [p for p in daily_vals if p < mean_d]
    down_var_d  = sum((p - mean_d) ** 2 for p in down_d) / len(down_d) if down_d else 0.0
    sortino     = ((mean_d - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0.0

    # p-value for H0: mean daily P&L = 0, H1: mean > 0 (one-sided, normal approx)
    p_value = _compute_pvalue(all_trades)

    return {
        "num_trades":    n,
        "num_wins":      len(wins),
        "num_losses":    len(losses),
        "total_pnl":     total_pnl,
        "win_rate":      win_rate,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown":  max_dd,
        "sharpe":        sharpe,
        "sortino":       sortino,
        "p_value":       p_value,
    }


# ---------------------------------------------
#  STATISTICAL HELPERS  (p-value, FDR correction)
# ---------------------------------------------
def _compute_pvalue(trades: list) -> float:
    """One-sided p-value: H0 = mean daily P&L is zero, H1 = mean > 0.

    Uses a one-sample t-test with normal approximation (valid for n >= 30
    trading days; returns 1.0 for fewer than 5 days).
    """
    day_pnls: dict = {}
    for t in trades:
        day_pnls.setdefault(t["entry_date"], 0.0)
        day_pnls[t["entry_date"]] += t["pnl_earned"]
    vals = list(day_pnls.values())
    n = len(vals)
    if n < 5:
        return 1.0
    mean = sum(vals) / n
    # sample variance (Bessel's correction)
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var == 0:
        return 0.0 if mean > 0 else 1.0
    t_stat = mean / math.sqrt(var / n)
    # one-sided p-value via standard-normal approximation: P(Z > t_stat)
    p = 0.5 * math.erfc(t_stat / math.sqrt(2))
    return float(min(max(p, 0.0), 1.0))


def _fdr_adjust(pvalues: list) -> list:
    """Benjamini-Hochberg FDR correction (step-down).

    Returns BH-adjusted p-values in the same order as the input list.
    Each adjusted value is min(p * N / rank, previous_adjusted).
    """
    n = len(pvalues)
    if n == 0:
        return []
    indexed  = sorted(enumerate(pvalues), key=lambda x: x[1])
    adjusted = [1.0] * n
    min_seen = 1.0
    for rank, (orig_idx, p) in reversed(list(enumerate(indexed, start=1))):
        bh = min(p * n / rank, min_seen)
        adjusted[orig_idx] = bh
        min_seen = bh
    return adjusted


def _apply_fdr_to_rows(rows: list, p_col: str = "p_value") -> list:
    """Add 'p_fdr' and 'sig_fdr' columns to a list of row dicts in-place.

    sig_fdr = True  -> survives Benjamini-Hochberg correction at alpha = 0.05.
    sig_fdr = False -> result is not statistically significant after correction.
    """
    if not rows:
        return rows
    pvals    = [r.get(p_col, 1.0) for r in rows]
    adjusted = _fdr_adjust(pvals)
    for r, adj in zip(rows, adjusted):
        r["p_fdr"]   = round(adj, 4)
        r["sig_fdr"] = adj < 0.05
    return rows
