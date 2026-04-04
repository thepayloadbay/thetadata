"""Zenith Fade CCS — Backtest Engine.

Data loading, indicator computation, and day-by-day simulation.
Uses local ThetaData parquets (same data as MEDS project).
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
from scipy.stats import norm

import zenith_config as _cfg
from zenith_config import *

# ---------------------------------------------
#  LOGGING
# ---------------------------------------------
def setup_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("zenith")
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
#  BUILD DAILY INDICATORS
# ---------------------------------------------
_DAILY_INDICATORS: dict = {}  # date_str -> dict of indicator values


def build_daily_indicators() -> dict:
    """Aggregate 1-min SPX OHLC parquets to daily bars and compute all Zenith indicators.

    Reads all years present in DATA_DIR. Rolling windows (SMA200, ATR14, RSI14, etc.)
    are warmed up from the earliest available data before the backtest window starts.

    NOTE: SPX is an index — no native volume data. Relative volume (dRelVol) is set to 1.0.
    This is a known limitation; future enhancement can use SPY or CBOE composite volume.

    Returns dict[date_str -> indicator dict].
    """
    rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
            if df.empty or len(df) < 5:
                continue
            # Filter to valid bars (non-zero)
            valid = df[(df["high"] > 0) & (df["close"] > 0)]
            if valid.empty:
                continue
            rows.append({
                "date":   date_str,
                "open":   float(valid["open"].iloc[0]),
                "high":   float(valid["high"].max()),
                "low":    float(valid["low"].min()),
                "close":  float(valid["close"].iloc[-1]),
                "volume": float(valid["volume"].sum()) if "volume" in valid.columns else 0.0,
            })
        except Exception:
            pass

    if not rows:
        logger.error("No SPX OHLC data found")
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

    # Also load VIX from vix_history.csv for dates before parquet data
    vix_hist_path = os.path.join(DATA_DIR, "vix_history.csv")
    if os.path.exists(vix_hist_path):
        vh = pd.read_csv(vix_hist_path)
        vh["DATE"] = pd.to_datetime(vh["DATE"])
        vh["date"] = vh["DATE"].dt.strftime("%Y%m%d")
        for _, row in vh.iterrows():
            try:
                vix_rows.append({"date": row["date"], "vix_close": float(row["CLOSE"])})
            except Exception:
                pass

    if vix_rows:
        vix_df = pd.DataFrame(vix_rows).drop_duplicates(subset="date", keep="last")
        vix_df = vix_df.sort_values("date").reset_index(drop=True)
        vix_df["vix_sma20"] = vix_df["vix_close"].rolling(20, min_periods=1).mean()
        d = d.merge(vix_df[["date", "vix_close", "vix_sma20"]], on="date", how="left")

    # -- Merge VIX1D closes for term structure filter --
    vix1d_rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix1d", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix1d_close" in vdf.columns:
                vix1d_rows.append({"date": str(vdf["date"].iloc[0]), "vix1d_close": float(vdf["vix1d_close"].iloc[0])})
        except Exception:
            pass
    if vix1d_rows:
        v1d_df = pd.DataFrame(vix1d_rows).drop_duplicates(subset="date", keep="last")
        d = d.merge(v1d_df[["date", "vix1d_close"]], on="date", how="left")

    # -- Derived daily bars --
    hl = (d["high"] - d["low"]).clip(lower=0.01)

    # Variance %: ((close - low) / (high - low)) * 100
    d["dVarPct"] = (d["close"] - d["low"]) / hl * 100

    # Body size: abs(open - close)
    d["dBodySize"] = (d["open"] - d["close"]).abs()

    # Gap %: (open - prev_close) / prev_close * 100
    d["dGapPercent"] = (d["open"] - d["prev_close"]) / d["prev_close"].clip(lower=0.01) * 100

    # Daily range
    d["dRange"] = d["high"] - d["low"]

    # -- Moving averages --
    d["dSma5"]   = d["close"].rolling(5,   min_periods=1).mean()
    d["dSma20"]  = d["close"].rolling(20,  min_periods=1).mean()
    d["dSma200"] = d["close"].rolling(200, min_periods=1).mean()

    # Distance from SMA20: (close - SMA20) / SMA20
    d["distFromSma"] = (d["close"] - d["dSma20"]) / d["dSma20"].clip(lower=0.01)

    # Rising Rocket: (close - SMA5) / SMA5
    d["risingRocket"] = (d["close"] - d["dSma5"]) / d["dSma5"].clip(lower=0.01)

    # -- ATR(14): True Range = max(H-L, |H-prevC|, |L-prevC|) --
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["prev_close"]).abs(),
        (d["low"]  - d["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    d["dATR"] = tr.rolling(14, min_periods=1).mean()

    # -- RSI(14) --
    delta = d["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14, min_periods=1).mean()
    avg_loss = loss.rolling(14, min_periods=1).mean()
    rs = avg_gain / avg_loss.clip(lower=1e-10)
    d["dRsi"] = 100 - (100 / (1 + rs))

    # -- Stochastic(14) --
    lo14 = d["low"].rolling(14, min_periods=1).min()
    hi14 = d["high"].rolling(14, min_periods=1).max()
    d["dStoch"] = ((d["close"] - lo14) / (hi14 - lo14).clip(lower=0.01)) * 100

    # -- Opening Range Breakout (ORB): 9:30-10:00 range --
    if _cfg.USE_ORB_FILTER or True:  # Always compute for logging
        orb_data = []
        for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
            date_str = os.path.basename(fpath).replace(".parquet", "")
            try:
                df = pd.read_parquet(fpath)
                if df.empty:
                    continue
                df["hhmm"] = pd.to_datetime(df["timestamp"]).dt.strftime("%H:%M")
                orb_bars = df[(df["hhmm"] >= "09:30") & (df["hhmm"] < "10:00")]
                if len(orb_bars) < 10:
                    continue
                orb_high = float(orb_bars["high"].max())
                orb_low = float(orb_bars["low"].min())
                orb_width = orb_high - orb_low
                day_close = float(df[df["hhmm"] <= "15:59"]["close"].iloc[-1]) if not df[df["hhmm"] <= "15:59"].empty else None
                orb_contained = 1.0 if (day_close is not None and orb_low <= day_close <= orb_high) else 0.0
                orb_data.append({
                    "date": date_str,
                    "orb_high": orb_high, "orb_low": orb_low,
                    "orb_width": orb_width, "orb_contained": orb_contained,
                })
            except Exception:
                pass
        if orb_data:
            orb_df = pd.DataFrame(orb_data).drop_duplicates("date", keep="last")
            d = d.merge(orb_df, on="date", how="left")
            logger.info(f"  ORB computed for {len(orb_df)} days (avg width: {orb_df['orb_width'].mean():.1f} pts)")
        else:
            d["orb_high"] = np.nan
            d["orb_low"] = np.nan
            d["orb_width"] = np.nan
            d["orb_contained"] = np.nan

    # -- Williams %R (fast and slow) --
    # %R = (highest(high, n) - close) / (highest(high, n) - lowest(low, n)) * -100
    # Range: 0 to -100. Above -20 = overbought. Below -80 = oversold.
    for label, period in [("wpr_fast", _cfg.WPR_FAST_PERIOD), ("wpr_slow", _cfg.WPR_SLOW_PERIOD)]:
        hh = d["high"].rolling(period, min_periods=1).max()
        ll = d["low"].rolling(period, min_periods=1).min()
        d[label] = (hh - d["close"]) / (hh - ll).clip(lower=0.01) * -100

    # -- Relative Volume: volume / SMA(volume, 20) --
    # SPX has no native volume — default to 1.0 (passes threshold 0.25)
    if d["volume"].sum() > 0:
        vol_sma20 = d["volume"].rolling(20, min_periods=1).mean().clip(lower=1)
        d["dRelVol"] = d["volume"] / vol_sma20
    else:
        d["dRelVol"] = 1.0  # No volume data available
        logger.warning("No SPX volume data — dRelVol defaulted to 1.0")

    # -- Cluster Count: sum of days where variance <= threshold in last N days --
    d["variance_low"] = (d["dVarPct"] <= _cfg.VARIANCE_THRESHOLD).astype(int)
    d["clusterCount"] = d["variance_low"].rolling(_cfg.CLUSTER_LOOKBACK, min_periods=1).sum()

    # -- Build lookup dict keyed by date_str --
    # Use PRIOR day's indicators for signal (T-1 data for T's signal, no look-ahead)
    cols_to_keep = [
        "date", "open", "high", "low", "close", "volume",
        "dVarPct", "dBodySize", "dGapPercent", "dRange",
        "dSma5", "dSma20", "dSma200", "distFromSma", "risingRocket",
        "dATR", "dRsi", "dStoch", "dRelVol", "clusterCount",
        "wpr_fast", "wpr_slow",
    ]
    if "vix_close" in d.columns:
        cols_to_keep.extend(["vix_close", "vix_sma20"])
    if "vix1d_close" in d.columns:
        cols_to_keep.append("vix1d_close")
    if "orb_width" in d.columns:
        cols_to_keep.extend(["orb_high", "orb_low", "orb_width", "orb_contained"])

    indicators = {}
    for i, row in d.iterrows():
        indicators[row["date"]] = {col: row[col] for col in cols_to_keep if col in d.columns}

    logger.info(f"Built daily indicators: {len(indicators)} bars")
    return indicators


def prior_day_ind(date_str: str, indicators: dict) -> dict | None:
    """Get the PRIOR trading day's indicators to avoid look-ahead bias.

    For signal on date T, we use T-1's indicators (all computed from T-1 close).
    This is critical: the PineScript uses [1] indexing (yesterday's values).
    """
    sorted_dates = sorted(indicators.keys())
    try:
        idx = sorted_dates.index(date_str)
    except ValueError:
        return None
    if idx < 1:
        return None
    return indicators.get(sorted_dates[idx - 1])


# ---------------------------------------------
#  SIGNAL LOGIC
# ---------------------------------------------
def compute_classic_signal(ind: dict) -> bool:
    """Classic Filters mode: AND of all enabled filters.

    Each filter returns True if the condition is met OR the filter is disabled.
    All must be True for signal to fire.
    All thresholds read from _cfg for sweep compatibility.
    """
    var_pct     = ind.get("dVarPct", 0)
    rel_vol     = ind.get("dRelVol", 1.0)
    body_size   = ind.get("dBodySize", 0)
    atr         = ind.get("dATR", 1)
    rocket      = ind.get("risingRocket", 0)
    cluster     = ind.get("clusterCount", 0)
    close_price = ind.get("close", 0)
    sma200      = ind.get("dSma200", 0)
    rsi         = ind.get("dRsi", 50)
    dist_sma    = ind.get("distFromSma", 0)
    stoch       = ind.get("dStoch", 50)
    gap_pct     = ind.get("dGapPercent", 0)
    daily_range = ind.get("dRange", 1)
    vix_close   = ind.get("vix_close")
    vix_sma20   = ind.get("vix_sma20")

    checks = []
    if _cfg.USE_VARIANCE_FILTER:
        checks.append(var_pct >= _cfg.VARIANCE_THRESHOLD)
    if _cfg.USE_REL_VOL_FILTER:
        checks.append(rel_vol >= _cfg.REL_VOL_THRESHOLD)
    if _cfg.USE_MIN_MOVE_FILTER:
        checks.append(body_size >= atr * _cfg.BODY_SIZE_MULTIPLIER)
    if _cfg.USE_RISING_ROCKET_FILTER:
        checks.append(rocket < _cfg.RISING_ROCKET_THRESHOLD)
    if _cfg.USE_CLUSTER_FILTER:
        checks.append(cluster <= _cfg.CLUSTER_THRESHOLD)
    if _cfg.USE_TREND_SMA_FILTER:
        checks.append(close_price <= sma200 if sma200 else True)
    if _cfg.USE_BODY_RATIO_FILTER:
        checks.append(body_size > daily_range * _cfg.BODY_RATIO if daily_range else True)
    if _cfg.USE_VIX_FILTER and vix_close is not None and vix_sma20 is not None:
        checks.append(vix_close >= vix_sma20)
    if _cfg.USE_RSI_FILTER:
        checks.append(rsi >= _cfg.RSI_THRESHOLD)
    if _cfg.USE_DIST_SMA_FILTER:
        checks.append(dist_sma >= _cfg.DIST_SMA_THRESHOLD)
    if _cfg.USE_STOCH_FILTER:
        checks.append(stoch >= _cfg.STOCH_THRESHOLD)
    if _cfg.USE_GAP_FILTER:
        checks.append(gap_pct / 100 >= _cfg.GAP_THRESHOLD)

    # ORB filter: skip if opening range is too wide (volatile session)
    if _cfg.USE_ORB_FILTER:
        orb_width = ind.get("orb_width")
        if orb_width is not None:
            checks.append(orb_width <= _cfg.ORB_MAX_WIDTH)
    # ORB containment: only trade if price stayed inside ORB (range-bound day)
    if _cfg.USE_ORB_CONTAINMENT_FILTER:
        orb_contained = ind.get("orb_contained")
        if orb_contained is not None:
            checks.append(orb_contained == 1.0)

    return all(checks) if checks else False


def compute_dynamic_score(ind: dict) -> tuple[float, bool]:
    """Dynamic Score mode: weighted exhaustion score.

    score = (variancePercent * wVar) + (dRelVol * wVol) - (risingRocket * wRocket)
    Signal fires when score >= scoreThreshold.
    All weights/threshold read from _cfg for sweep compatibility.

    Returns (score, signal_bool).
    """
    var_pct = ind.get("dVarPct", 0)
    rel_vol = ind.get("dRelVol", 1.0)
    rocket  = ind.get("risingRocket", 0)

    score = (var_pct * _cfg.W_VAR) + (rel_vol * _cfg.W_VOL) - (rocket * _cfg.W_ROCKET)
    return score, score >= _cfg.SCORE_THRESHOLD


def compute_signal(ind: dict) -> tuple[bool, float]:
    """Compute signal based on current SIGNAL_MODE.

    Returns (signal_fired, exhaustion_score).
    """
    if _cfg.SIGNAL_MODE == "classic":
        fired = compute_classic_signal(ind)
        # Also compute score for logging
        var_pct = ind.get("dVarPct", 0)
        rel_vol = ind.get("dRelVol", 1.0)
        rocket  = ind.get("risingRocket", 0)
        score = (var_pct * _cfg.W_VAR) + (rel_vol * _cfg.W_VOL) - (rocket * _cfg.W_ROCKET)
        return fired, score
    else:
        score, fired = compute_dynamic_score(ind)
        return fired, score


# ---------------------------------------------
#  TRADE SIMULATION
# ---------------------------------------------
def compute_strikes(close_price: float) -> tuple[float, float]:
    """Compute short and long call strikes.

    Short strike = ceil((close + STRIKE_OFFSET) / 5) * 5
    Long strike = short + SPREAD_WIDTH
    All params from _cfg for sweep compatibility.
    """
    short = math.ceil((close_price + _cfg.STRIKE_OFFSET) / 5) * 5
    long = short + _cfg.SPREAD_WIDTH
    return float(short), float(long)


def compute_put_strikes(close_price: float) -> tuple[float, float]:
    """Compute short and long put strikes.

    Short put = floor((close - PUT_STRIKE_OFFSET) / 5) * 5
    Long put = short - PUT_SPREAD_WIDTH
    """
    short = math.floor((close_price - _cfg.PUT_STRIKE_OFFSET) / 5) * 5
    long = short - _cfg.PUT_SPREAD_WIDTH
    return float(short), float(long)


def compute_spread_value_at_exit(spx_close: float, short_strike: float, long_strike: float, spread_type: str = "call") -> float:
    """Compute intrinsic value of the spread at exit.

    Call spread: max(0, SPX - short) - max(0, SPX - long)
    Put spread:  max(0, short - SPX) - max(0, long - SPX)
    """
    if spread_type == "put":
        return max(0, short_strike - spx_close) - max(0, long_strike - spx_close)
    return max(0, spx_close - short_strike) - max(0, spx_close - long_strike)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call price.

    Args:
        S: spot price
        K: strike price
        T: time to expiry in years
        r: risk-free rate (annual)
        sigma: implied volatility (annual, e.g. 0.18 for VIX=18)
    """
    if T <= 0 or sigma <= 0:
        return max(0, S - K)
    d1 = (np.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def compute_bs_credit(
    spot: float,
    short_strike: float,
    long_strike: float,
    vix: float | None,
) -> float:
    """Compute credit received for a call spread using Black-Scholes.

    Uses VIX as a proxy for implied volatility. Options are 1DTE (T = 1/252).
    Credit = BS_call(short) - BS_call(long).

    Falls back to FIXED_CREDIT if VIX data is unavailable.
    """
    if vix is None or vix <= 0:
        return _cfg.FIXED_CREDIT

    sigma = vix / 100  # VIX is annualized vol in %
    T = 1 / 252  # 1 trading day to expiry
    r = _cfg.ANNUAL_RF_RATE

    short_price = bs_call_price(spot, short_strike, T, r, sigma)
    long_price  = bs_call_price(spot, long_strike, T, r, sigma)
    credit = short_price - long_price

    # Floor at 0.05 — if credit is negligible, don't trade
    return max(0.05, credit)


def compute_bs_put_credit(
    spot: float,
    short_strike: float,
    long_strike: float,
    vix: float | None,
) -> float:
    """Compute credit for a put spread using Black-Scholes.

    Credit = BS_put(short) - BS_put(long). Short is higher strike (closer to ATM).
    """
    if vix is None or vix <= 0:
        return _cfg.FIXED_CREDIT

    sigma = vix / 100
    T = 1 / 252
    r = _cfg.ANNUAL_RF_RATE

    def bs_put(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0:
            return max(0, K - S)
        d1 = (np.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))

    short_price = bs_put(spot, short_strike, T, r, sigma)
    long_price  = bs_put(spot, long_strike, T, r, sigma)
    credit = short_price - long_price
    return max(0.05, credit)


def compute_real_credit(
    trade_date: str,
    short_strike: float,
    long_strike: float,
) -> float | None:
    """Compute credit from actual option bid/ask quotes.

    Uses the first few bars (9:30-9:35) of the trade date as a proxy for
    T-1 close entry pricing. Credit = short_bid - long_ask (worst-case fill).

    Returns credit in dollars, or None if quotes unavailable.
    """
    year = trade_date[:4]
    fpath = os.path.join(_cfg.DATA_DIR, year, "option_quotes", f"{trade_date}.parquet")
    if not os.path.exists(fpath):
        return None
    try:
        df = pd.read_parquet(fpath)
        calls = df[df["right"] == "C"]
        # Use 9:40-9:50 window for credit pricing (settled market, realistic fills)
        ts_prefix = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        early = calls[(calls["timestamp"].astype(str) >= f"{ts_prefix}T09:40") &
                      (calls["timestamp"].astype(str) < f"{ts_prefix}T09:50")]
        if early.empty:
            # Fall back to first 10 minutes if no data in 9:40-9:50
            early = calls[calls["timestamp"].astype(str) < f"{ts_prefix}T09:40"]
        if early.empty:
            return None

        short_q = early[early["strike"] == int(short_strike)]
        long_q  = early[early["strike"] == int(long_strike)]

        if short_q.empty or long_q.empty:
            return None

        short_bid = float(short_q.iloc[0]["bid"])
        long_ask  = float(long_q.iloc[0]["ask"])

        if short_bid <= 0:
            return None  # no bid = can't sell

        credit = short_bid - long_ask
        return max(0.05, credit)  # floor at $0.05
    except Exception:
        return None


def _load_intraday_bars(trade_date: str) -> list[tuple[float, float, float]] | None:
    """Load 1-min SPX bars for the trade date.

    Returns list of (high, low, close) tuples, or None if data not available.
    """
    year = trade_date[:4]
    fpath = os.path.join(_cfg.DATA_DIR, year, "spx_ohlc", f"{trade_date}.parquet")
    if not os.path.exists(fpath):
        return None
    try:
        df = pd.read_parquet(fpath)
        valid = df[(df["high"] > 0) & (df["close"] > 0)]
        if valid.empty:
            return None
        return list(zip(valid["high"].tolist(), valid["low"].tolist(), valid["close"].tolist()))
    except Exception:
        return None


def _get_adaptive_offset(vix: float | None, spread_type: str) -> float:
    """Improvement #1: VIX-adaptive strike offset.

    Returns the offset to use based on current VIX level.
    """
    if not _cfg.ENABLE_VIX_ADAPTIVE_OFFSET or vix is None:
        return _cfg.PUT_STRIKE_OFFSET if spread_type == "put" else _cfg.STRIKE_OFFSET

    for vix_max, call_offset, put_offset in _cfg.VIX_OFFSET_TIERS:
        if vix < vix_max:
            return put_offset if spread_type == "put" else call_offset

    # VIX exceeds all tiers — use default
    return _cfg.PUT_STRIKE_OFFSET if spread_type == "put" else _cfg.STRIKE_OFFSET


def _compute_danger_score(trade: dict, trade_date: str) -> float:
    """Compute danger score for a trade. Higher = more likely to lose.

    Factors (from loss forensics):
    - Score dead zone (78-95 calls, 25-32 puts) → +2
    - VIX 16-18 (worst WR bucket) → +1
    - Wednesday (worst day) → +0.5
    - Low stochastic (<50) on call side → +1
    - High credit (>3.5, closer to ATM) → +0.5
    """
    from datetime import datetime as _dt

    score = trade.get("exhaustion_score", 0) or 0
    vix = trade.get("vix_level", 0) or 0
    stoch = trade.get("stoch", 50) or 50
    credit = trade.get("credit_received", 0) or 0
    is_call = score >= _cfg.SCORE_THRESHOLD

    try:
        dow = _dt.strptime(trade_date, "%Y%m%d").weekday()
    except Exception:
        dow = -1

    danger = 0.0

    # Dead zone
    if is_call and 78 <= score < 95:
        danger += 2
    if not is_call and 25 <= score < 32:
        danger += 2

    # VIX zone
    if 16 <= vix < 18:
        danger += 1

    # Wednesday
    if dow == 2:
        danger += 0.5

    # Low stochastic on call side
    if is_call and stoch < 50:
        danger += 1

    # High credit = closer to ATM
    if credit > 3.5:
        danger += 0.5

    return danger


def _get_danger_sizing(danger: float) -> float | None:
    """Return risk multiplier based on danger score. None = skip trade."""
    if not _cfg.ENABLE_DANGER_SIZING:
        return 1.0

    if danger > _cfg.DANGER_HIGH_THRESH:
        return None  # skip
    elif danger <= 0.5:
        return _cfg.DANGER_LOW_MULT
    else:
        return _cfg.DANGER_MED_MULT


def _get_score_sizing(score: float, spread_type: str) -> float:
    """Legacy score-proportional risk multiplier (superseded by danger sizing)."""
    if not _cfg.ENABLE_SCORE_SIZING:
        return 1.0

    if spread_type == "put":
        for score_max, mult in _cfg.PUT_SCORE_SIZING_TIERS:
            if score <= score_max:
                return mult
        return 1.0
    else:
        for score_min, mult in _cfg.SCORE_SIZING_TIERS:
            if score >= score_min:
                return mult
        return 1.0


def _get_gap_adjusted_sl(
    spx_close_signal: float,
    intraday_bars: list | None,
    spread_type: str,
) -> float:
    """Improvement #4: Overnight gap dynamic SL.

    Returns the SL cap to use based on the overnight gap direction.
    """
    if not _cfg.ENABLE_GAP_DYNAMIC_SL or intraday_bars is None or not intraday_bars:
        return _cfg.INTRADAY_SL_CAP

    spx_open = intraday_bars[0][2]  # first bar close ≈ open
    gap_pct = (spx_open - spx_close_signal) / spx_close_signal

    if spread_type == "call":
        # Call spread: gap UP is adverse (SPX moved toward short strike)
        if gap_pct > _cfg.GAP_THRESHOLD:
            return _cfg.GAP_TIGHT_SL   # Tighten SL
        elif gap_pct < -_cfg.GAP_THRESHOLD:
            return _cfg.GAP_WIDE_SL    # Widen SL (favorable gap down)
    else:
        # Put spread: gap DOWN is adverse
        if gap_pct < -_cfg.GAP_THRESHOLD:
            return _cfg.GAP_TIGHT_SL
        elif gap_pct > _cfg.GAP_THRESHOLD:
            return _cfg.GAP_WIDE_SL

    return _cfg.INTRADAY_SL_CAP  # No significant gap


def _get_atr_width(atr: float, spread_type: str) -> float:
    """Improvement #8: ATR-scaled spread width."""
    if not _cfg.ENABLE_ATR_WIDTH:
        return _cfg.PUT_SPREAD_WIDTH if spread_type == "put" else _cfg.SPREAD_WIDTH
    for atr_max, call_w, put_w in _cfg.ATR_WIDTH_TIERS:
        if atr < atr_max:
            return put_w if spread_type == "put" else call_w
    return _cfg.PUT_SPREAD_WIDTH if spread_type == "put" else _cfg.SPREAD_WIDTH


def _check_weekly_super(signal_ind: dict, indicators: dict) -> bool:
    """Improvement #9: Check if weekly exhaustion super-signal fires.

    Computes 5-day rolling variance: (last 5-day close - 5-day low) / (5-day high - 5-day low).
    """
    if not _cfg.ENABLE_WEEKLY_SUPER:
        return False
    sig_date = signal_ind.get("date", "")
    sorted_dates = sorted(indicators.keys())
    try:
        idx = sorted_dates.index(sig_date)
    except ValueError:
        return False
    if idx < 4:
        return False
    # Get last 5 days of data
    week_dates = sorted_dates[idx - 4 : idx + 1]
    highs = [indicators[d]["high"] for d in week_dates if d in indicators]
    lows  = [indicators[d]["low"]  for d in week_dates if d in indicators]
    close = signal_ind["close"]
    if not highs or not lows:
        return False
    week_high = max(highs)
    week_low  = min(lows)
    week_range = week_high - week_low
    if week_range < 0.01:
        return False
    weekly_var = (close - week_low) / week_range * 100
    return weekly_var >= _cfg.WEEKLY_VAR_THRESHOLD


def _get_compound_base_risk(cumulative_pnl: float) -> float:
    """Improvement #10: Compound growth sizing with cap."""
    if not _cfg.ENABLE_COMPOUND_GROWTH:
        return _cfg.RISK_AMOUNT
    steps = max(0, int(cumulative_pnl / _cfg.COMPOUND_GROWTH_STEP))
    base = _cfg.RISK_AMOUNT + steps * _cfg.COMPOUND_GROWTH_ADD
    return min(base, _cfg.COMPOUND_GROWTH_MAX)


def _check_intraday_confirmation(signal_date: str, spread_type: str) -> float:
    """Improvement #12: Check last 30min of signal day for fade confirmation.

    Returns sizing multiplier: CONFIRM_FADE_MULT if fade confirmed, 0 if skip, 1.0 default.
    """
    if not _cfg.ENABLE_INTRADAY_CONFIRM:
        return 1.0
    bars = _load_intraday_bars(signal_date)
    if not bars or len(bars) < _cfg.CONFIRM_LOOKBACK_BARS + 5:
        return 1.0
    # Last N bars
    recent = bars[-_cfg.CONFIRM_LOOKBACK_BARS:]
    start_price = recent[0][2]   # close of bar at -30min
    end_price = recent[-1][2]    # close of last bar
    move = end_price - start_price

    if spread_type == "call":
        # Selling calls: fade = SPX dropping at end of day (confirming exhaustion reversal)
        if move < -_cfg.CONFIRM_FADE_THRESHOLD:
            return _cfg.CONFIRM_FADE_MULT
        elif move > _cfg.CONFIRM_STRONG_TREND:
            return 0  # SPX still rallying hard → skip (false exhaustion)
    else:
        # Selling puts: fade = SPX bouncing at end of day
        if move > _cfg.CONFIRM_FADE_THRESHOLD:
            return _cfg.CONFIRM_FADE_MULT
        elif move < -_cfg.CONFIRM_STRONG_TREND:
            return 0  # SPX still dropping hard → skip

    return 1.0


def _get_entry_price(trade_date: str, exit_ind: dict) -> float:
    """Get SPX price at 9:45 AM on trade date for realistic entry pricing.

    Falls back to daily open if 1-min data unavailable.
    """
    bars = _load_intraday_bars(trade_date)
    if bars and len(bars) >= 16:
        # 1-min bars start at 9:30, so bar index 15 = 9:45
        return bars[15][2]  # close of the 9:45 bar
    return exit_ind.get("open", exit_ind["close"])


def simulate_trade(
    signal_date: str,
    trade_date: str,
    signal_ind: dict,
    exit_ind: dict,
    score: float,
    spread_type: str = "call",
    streak_count: int = 1,
    cumulative_pnl: float = 0.0,
    indicators: dict | None = None,
) -> dict | list[dict]:
    """Simulate a Zenith trade with all improvements.

    Returns a single trade dict, a list of dicts (re-entry), or None (skip).
    """
    spx_close_signal = signal_ind["close"]
    spx_close_exit   = exit_ind["close"]
    # Use exit day's 9:45 bar for strike computation (realistic: market settled, bid-ask normal)
    spx_entry_price  = _get_entry_price(trade_date, exit_ind)
    vix = signal_ind.get("vix_close")
    atr = signal_ind.get("dATR", 50)

    # --- Improvement #12: Intraday momentum confirmation ---
    confirm_mult = _check_intraday_confirmation(signal_date, spread_type)
    if confirm_mult == 0:
        return None  # Skip — strong trend continuation, not real exhaustion

    # --- Improvement #1: VIX-adaptive offset ---
    offset = _get_adaptive_offset(vix, spread_type)

    # --- Improvement #8: ATR-scaled width ---
    width = _get_atr_width(atr, spread_type)

    # --- Improvement #9: Weekly super-signal width boost ---
    is_super = _check_weekly_super(signal_ind, indicators) if indicators else False
    if is_super:
        width = width * _cfg.SUPER_SIGNAL_WIDTH_MULT

    # Round width to nearest 5
    width = round(width / 5) * 5
    if width < 5:
        width = 5.0

    if spread_type == "put":
        short_strike = math.floor((spx_entry_price - offset) / 5) * 5
        long_strike = short_strike - width
    else:
        short_strike = math.ceil((spx_entry_price + offset) / 5) * 5
        long_strike = short_strike + width

    # Compute credit
    if spread_type == "put":
        credit = compute_bs_put_credit(spx_close_signal, short_strike, long_strike, vix)
    elif _cfg.CREDIT_MODEL == "real":
        credit = compute_real_credit(trade_date, short_strike, long_strike)
        if credit is None:
            credit = compute_bs_credit(spx_close_signal, short_strike, long_strike, vix)
    elif _cfg.CREDIT_MODEL == "bs":
        credit = compute_bs_credit(spx_close_signal, short_strike, long_strike, vix)
    else:
        credit = _cfg.FIXED_CREDIT

    # --- Improvement #2: Credit floor ---
    if _cfg.ENABLE_CREDIT_FLOOR and credit < _cfg.MIN_CREDIT:
        return None  # Skip this trade

    max_risk = width - credit
    if max_risk <= 0:
        max_risk = 0.01

    # --- Improvement #3: Danger-score sizing (or legacy score sizing) ---
    if _cfg.ENABLE_DANGER_SIZING:
        # Build a lightweight trade dict for danger computation
        _partial = {
            "exhaustion_score": score,
            "vix_level": vix or 0,
            "stoch": signal_ind.get("dStoch", 50),
            "credit_received": credit,
        }
        danger = _compute_danger_score(_partial, trade_date)
        risk_mult = _get_danger_sizing(danger)
        if risk_mult is None:
            return None  # Skip high-danger trade
    else:
        risk_mult = _get_score_sizing(score, spread_type)
        danger = 0.0

    # --- Improvement #3b: Streak boost ---
    if _cfg.ENABLE_STREAK_BOOST and streak_count >= _cfg.STREAK_BOOST_DAY:
        risk_mult *= _cfg.STREAK_BOOST_MULT

    # --- Improvement #9: Super-signal sizing boost ---
    if is_super:
        risk_mult *= _cfg.SUPER_SIGNAL_SIZE_MULT

    # --- Improvement #12: Intraday confirmation boost ---
    risk_mult *= confirm_mult

    # --- Improvement #10: Compound growth base risk ---
    base_risk = _get_compound_base_risk(cumulative_pnl)
    effective_risk = base_risk * risk_mult

    # Load intraday bars (used by #4 gap SL and #5 re-entry)
    intraday = _load_intraday_bars(trade_date)

    # --- Improvement #4: Gap-adjusted SL ---
    sl_cap = _get_gap_adjusted_sl(spx_close_signal, intraday, spread_type)

    # --- Intraday stop-loss with gap-adjusted SL ---
    sl_triggered = False
    sl_bar_index = -1
    sl_trigger_price = None

    if _cfg.ENABLE_INTRADAY_SL and intraday:
        sl_spread_value = credit + abs(sl_cap) * max_risk

        if spread_type == "put":
            trigger_price = short_strike - sl_spread_value
            for idx, (bar_high, bar_low, bar_close) in enumerate(intraday):
                if bar_low <= trigger_price:
                    spx_close_exit = trigger_price
                    sl_triggered = True
                    sl_bar_index = idx
                    sl_trigger_price = trigger_price
                    break
        else:
            trigger_price = short_strike + sl_spread_value
            for idx, (bar_high, bar_low, bar_close) in enumerate(intraday):
                if bar_high >= trigger_price:
                    spx_close_exit = trigger_price
                    sl_triggered = True
                    sl_bar_index = idx
                    sl_trigger_price = trigger_price
                    break

    # --- H8: Noon profit-taking ---
    # If spread is 50%+ profitable at noon, exit early to lock in gains
    noon_exit = False
    if _cfg.ENABLE_NOON_PROFIT_TAKE and not sl_triggered and intraday:
        noon_bar = _cfg.NOON_TAKE_BAR
        if noon_bar < len(intraday):
            noon_price = intraday[noon_bar][2]  # close at noon
            sv_noon = compute_spread_value_at_exit(noon_price, short_strike, long_strike, spread_type)
            profit_at_noon = credit - sv_noon
            if credit > 0 and profit_at_noon >= credit * _cfg.NOON_TAKE_PCT:
                # Exit at noon — lock in profit
                spx_close_exit = noon_price
                noon_exit = True

    spread_value = compute_spread_value_at_exit(spx_close_exit, short_strike, long_strike, spread_type)
    raw_pnl = credit - spread_value
    raw_return = raw_pnl / max_risk if max_risk > 0 else 0
    capped_return = max(_cfg.SL_CAP, min(_cfg.TP_CAP, raw_return))
    dollar_pnl = capped_return * effective_risk
    commission = _cfg.COMMISSION * 4
    dollar_pnl -= commission

    win = 1 if dollar_pnl > 0 else 0
    loss = 1 if dollar_pnl <= 0 else 0

    main_trade = {
        "signal_date":       signal_date,
        "trade_date":        trade_date,
        "signal_mode":       _cfg.SIGNAL_MODE,
        "spx_close_signal":  round(spx_close_signal, 2),
        "spx_entry_price":   round(spx_entry_price, 2),
        "spx_close_exit":    round(spx_close_exit, 2),
        "short_strike":      short_strike,
        "long_strike":       long_strike,
        "credit_received":   credit,
        "spread_value_exit": round(spread_value, 4),
        "raw_return":        round(raw_return, 4),
        "capped_return":     round(capped_return, 4),
        "pnl_earned":        round(dollar_pnl, 2),
        "win":               win,
        "loss":              loss,
        "equity_after":      0.0,
        "vix_level":         round(signal_ind.get("vix_close", 0) or 0, 2),
        "variance_pct":      round(signal_ind.get("dVarPct", 0), 2),
        "rel_vol":           round(signal_ind.get("dRelVol", 1.0), 4),
        "rising_rocket":     round(signal_ind.get("risingRocket", 0), 6),
        "exhaustion_score":  round(score, 2),
        "body_size":         round(signal_ind.get("dBodySize", 0), 2),
        "atr":               round(signal_ind.get("dATR", 0), 2),
        "rsi":               round(signal_ind.get("dRsi", 0), 1),
        "stoch":             round(signal_ind.get("dStoch", 0), 1),
        "gap_pct":           round(signal_ind.get("dGapPercent", 0), 4),
        "cluster_count":     int(signal_ind.get("clusterCount", 0)),
        "dist_from_sma":     round(signal_ind.get("distFromSma", 0), 6),
    }

    trades = [main_trade]

    # --- Improvement #5: SL Re-Entry ---
    if _cfg.ENABLE_SL_REENTRY and sl_triggered and intraday and sl_bar_index >= 0:
        reentry_bar = sl_bar_index + _cfg.REENTRY_AFTER_MINUTES
        if reentry_bar < len(intraday):
            # Check if SPX pulled back enough
            sl_spx = intraday[sl_bar_index][2]  # SPX at SL trigger
            for re_idx in range(reentry_bar, len(intraday)):
                re_high, re_low, re_close = intraday[re_idx]
                if spread_type == "call":
                    # SPX must pull back below short_strike for re-entry
                    pullback = sl_spx - re_close
                    spike = sl_spx - spx_close_signal
                    if spike > 0 and pullback / spike >= _cfg.REENTRY_PULLBACK_PCT:
                        # Re-enter further OTM from current price
                        re_short = math.ceil((re_close + _cfg.REENTRY_OFFSET) / 5) * 5
                        re_long = re_short + width
                        # Credit for re-entry (BS, late-day 0DTE)
                        remaining_bars = len(intraday) - re_idx
                        T_remain = remaining_bars / (391 * 252)  # fraction of year
                        if vix and vix > 0:
                            re_credit = max(0.05, bs_call_price(re_close, re_short, T_remain, _cfg.ANNUAL_RF_RATE, vix/100)
                                            - bs_call_price(re_close, re_long, T_remain, _cfg.ANNUAL_RF_RATE, vix/100))
                        else:
                            re_credit = 0.10
                        re_max_risk = width - re_credit
                        if re_max_risk <= 0:
                            break
                        # Exit at day close
                        day_close = intraday[-1][2]
                        re_sv = compute_spread_value_at_exit(day_close, re_short, re_long, "call")
                        re_pnl_raw = re_credit - re_sv
                        re_return = re_pnl_raw / re_max_risk
                        re_capped = max(_cfg.SL_CAP, min(_cfg.TP_CAP, re_return))
                        re_dollar = re_capped * _cfg.RISK_AMOUNT - _cfg.COMMISSION * 4
                        trades.append({
                            **main_trade,
                            "signal_mode": "reentry",
                            "short_strike": re_short,
                            "long_strike": re_long,
                            "credit_received": re_credit,
                            "pnl_earned": round(re_dollar, 2),
                            "win": 1 if re_dollar > 0 else 0,
                            "loss": 1 if re_dollar <= 0 else 0,
                        })
                        break
                else:
                    # Put: SPX must bounce back above short_strike
                    pullback = re_close - sl_spx
                    spike = spx_close_signal - sl_spx
                    if spike > 0 and pullback / spike >= _cfg.REENTRY_PULLBACK_PCT:
                        re_short = math.floor((re_close - _cfg.REENTRY_OFFSET) / 5) * 5
                        re_long = re_short - width
                        remaining_bars = len(intraday) - re_idx
                        T_remain = remaining_bars / (391 * 252)
                        if vix and vix > 0:
                            re_credit = max(0.05, compute_bs_put_credit(re_close, re_short, re_long, vix))
                        else:
                            re_credit = 0.10
                        re_max_risk = width - re_credit
                        if re_max_risk <= 0:
                            break
                        day_close = intraday[-1][2]
                        re_sv = compute_spread_value_at_exit(day_close, re_short, re_long, "put")
                        re_pnl_raw = re_credit - re_sv
                        re_return = re_pnl_raw / re_max_risk
                        re_capped = max(_cfg.SL_CAP, min(_cfg.TP_CAP, re_return))
                        re_dollar = re_capped * _cfg.RISK_AMOUNT - _cfg.COMMISSION * 4
                        trades.append({
                            **main_trade,
                            "signal_mode": "reentry",
                            "short_strike": re_short,
                            "long_strike": re_long,
                            "credit_received": re_credit,
                            "pnl_earned": round(re_dollar, 2),
                            "win": 1 if re_dollar > 0 else 0,
                            "loss": 1 if re_dollar <= 0 else 0,
                        })
                        break

    return trades if len(trades) > 1 else trades[0]


# ---------------------------------------------
#  MAIN BACKTEST LOOP
# ---------------------------------------------
def run_backtest(
    indicators: dict | None = None,
    start: str | None = None,
    end: str | None = None,
    config_overrides: dict | None = None,
    quiet: bool = False,
) -> list[dict]:
    """Run full Zenith backtest.

    Args:
        indicators: pre-built daily indicators dict (if None, builds from scratch)
        start/end: override PILOT_YEAR_START/END (YYYY-MM-DD format)
        config_overrides: dict of config attribute overrides for sweep runs
        quiet: suppress per-day logging

    Returns list of trade dicts.
    """
    # Apply config overrides
    orig_values = {}
    if config_overrides:
        for k, v in config_overrides.items():
            if hasattr(_cfg, k):
                orig_values[k] = getattr(_cfg, k)
                setattr(_cfg, k, v)

    try:
        return _run_backtest_inner(indicators, start, end, quiet)
    finally:
        # Restore original config
        for k, v in orig_values.items():
            setattr(_cfg, k, v)


def _run_backtest_inner(
    indicators: dict | None,
    start: str | None,
    end: str | None,
    quiet: bool,
) -> list[dict]:
    if indicators is None:
        indicators = build_daily_indicators()
    if not indicators:
        logger.error("No indicators available — cannot run backtest")
        return []

    start_str = start or _cfg.PILOT_YEAR_START
    end_str   = end   or _cfg.PILOT_YEAR_END

    date_list = pd.date_range(start_str, end_str, freq="B")
    sorted_dates = sorted(indicators.keys())

    all_trades = []
    equity = _cfg.STARTING_EQUITY
    peak_equity = equity
    total_signals = 0
    total_trading_days = 0
    total_skips = 0
    _streak_count = 0   # consecutive signal days

    if not quiet:
        init_log_file()
        logger.info("=" * 60)
        logger.info("ZENITH FADE CCS BACKTEST")
        logger.info(f"Mode: {_cfg.SIGNAL_MODE}")
        logger.info(f"Period: {start_str} -> {end_str}")
        logger.info(f"Trades -> {_cfg.SAVE_FILE}")
        logger.info("=" * 60)

    # Build date pairs: for each date, we need signal_date (T) and trade_date (T+1)
    bday_list = [d.strftime("%Y%m%d") for d in date_list]
    valid_dates = [d for d in bday_list if d not in MARKET_HOLIDAYS and d in indicators]

    for i, d_str in enumerate(valid_dates):
        total_trading_days += 1

        # Skip holidays/FOMC/TW
        if _cfg.ENABLE_FOMC_SKIP and d_str in MARKET_HOLIDAYS:
            total_skips += 1
            continue
        if _cfg.ENABLE_FOMC_SKIP and d_str in FOMC_DATES:
            total_skips += 1
            continue
        if _cfg.ENABLE_TW_SKIP and d_str in TRIPLE_WITCHING_DATES:
            total_skips += 1
            continue

        # VIX max filter
        today_ind = indicators.get(d_str)
        if today_ind is None:
            continue
        vix = today_ind.get("vix_close")
        if vix is not None and vix > _cfg.VIX_MAX_FILTER:
            if not quiet:
                logger.debug(f"Skipping {d_str} — VIX {vix:.1f} > {VIX_MAX_FILTER}")
            total_skips += 1
            continue

        # Term structure filter (uses prior day's VIX1D, no look-ahead)
        if _cfg.ENABLE_TERM_STRUCTURE_FILTER:
            signal_ind_check = prior_day_ind(d_str, indicators)
            if signal_ind_check:
                vix_val = signal_ind_check.get("vix_close")
                vix1d_val = signal_ind_check.get("vix1d_close")
                if vix_val and vix1d_val and vix_val > 0:
                    ratio = vix1d_val / vix_val
                    if ratio > _cfg.TERM_STRUCTURE_MAX_RATIO:
                        total_skips += 1
                        _streak_count = 0
                        continue

        # Signal uses PRIOR day's indicators (T-1)
        signal_ind = prior_day_ind(d_str, indicators)
        if signal_ind is None:
            continue

        signal_fired, score = compute_signal(signal_ind)

        # Determine trade type: call spread (upside exhaustion) or put spread (downside exhaustion)
        trade_type = None
        if signal_fired:
            trade_type = "call"
        elif _cfg.ENABLE_PUT_SIDE and score <= _cfg.PUT_SCORE_MAX:
            trade_type = "put"

        if trade_type is None:
            total_skips += 1
            _streak_count = 0  # reset streak on no-signal day
            continue

        # ORB post-signal filter (applies regardless of signal mode)
        if _cfg.USE_ORB_FILTER:
            orb_width = signal_ind.get("orb_width")
            if orb_width is not None and orb_width > _cfg.ORB_MAX_WIDTH:
                total_skips += 1
                continue
        if _cfg.USE_ORB_CONTAINMENT_FILTER:
            orb_contained = signal_ind.get("orb_contained")
            if orb_contained is not None and orb_contained != 1.0:
                total_skips += 1
                continue

        # Track consecutive signal streak
        _streak_count += 1

        # Post-signal filters
        if trade_type == "call":
            if _cfg.POST_FILTER_STOCH_MIN is not None:
                if signal_ind.get("dStoch", 0) < _cfg.POST_FILTER_STOCH_MIN:
                    total_skips += 1
                    continue
            if _cfg.POST_FILTER_DIST_SMA_MIN is not None:
                if signal_ind.get("distFromSma", 0) < _cfg.POST_FILTER_DIST_SMA_MIN:
                    total_skips += 1
                    continue
            if _cfg.POST_FILTER_CLUSTER_MAX is not None:
                if signal_ind.get("clusterCount", 99) > _cfg.POST_FILTER_CLUSTER_MAX:
                    total_skips += 1
                    continue
            # H4: Skip call trades after big up days
            if _cfg.ENABLE_BIG_UP_DAY_SKIP:
                day_ret = (signal_ind["close"] - signal_ind["open"]) / signal_ind["open"]
                if day_ret > _cfg.BIG_UP_DAY_THRESHOLD:
                    total_skips += 1
                    continue

        # H2-WPR-1: Dual Williams %R exhaustion confirmation (uses T-1 data via signal_ind)
        if _cfg.USE_WPR_FILTER:
            wpr_f = signal_ind.get("wpr_fast", -50.0)
            wpr_s = signal_ind.get("wpr_slow", -50.0)
            if trade_type == "call":
                if not (wpr_f > _cfg.WPR_OB_LEVEL and wpr_s > _cfg.WPR_OB_LEVEL):
                    total_skips += 1
                    continue
            elif trade_type == "put":
                if not (wpr_f < _cfg.WPR_OS_LEVEL and wpr_s < _cfg.WPR_OS_LEVEL):
                    total_skips += 1
                    continue

        # Determine exit date
        if _cfg.HOLD_DAYS == 1:
            exit_ind = today_ind
            exit_date = d_str
        else:
            try:
                current_idx = sorted_dates.index(d_str)
            except ValueError:
                continue
            exit_idx = current_idx + _cfg.HOLD_DAYS - 1
            if exit_idx >= len(sorted_dates):
                continue
            exit_date = sorted_dates[exit_idx]
            exit_ind = indicators.get(exit_date)
            if exit_ind is None:
                continue

        # Execute trade
        # --- Improvement #7: Rolling WR adaptive sizing ---
        rolling_wr_mult = 1.0
        if _cfg.ENABLE_ROLLING_WR_SIZING and len(all_trades) >= _cfg.ROLLING_WR_WINDOW:
            recent = all_trades[-_cfg.ROLLING_WR_WINDOW:]
            rolling_wr = sum(1 for t in recent if t["pnl_earned"] > 0) / len(recent) * 100
            if rolling_wr < _cfg.ROLLING_WR_COLD_THRESH:
                rolling_wr_mult = 0  # skip during cold regime
            elif rolling_wr >= _cfg.ROLLING_WR_HOT_THRESH:
                rolling_wr_mult = _cfg.ROLLING_WR_HOT_MULT

        if rolling_wr_mult == 0:
            total_skips += 1
            _streak_count = 0
            continue

        cumulative_pnl = equity - _cfg.STARTING_EQUITY

        # --- Improvement #11: Iron Condor ---
        # Check if BOTH call and put qualify. If so, trade both.
        trade_types = [trade_type]
        if _cfg.ENABLE_IRON_CONDOR and signal_fired and _cfg.ENABLE_PUT_SIDE and score <= _cfg.PUT_SCORE_MAX:
            # Both signals qualify simultaneously → iron condor
            trade_types = ["call", "put"]
        elif _cfg.ENABLE_IRON_CONDOR:
            # Check if close is near 5-day midpoint (qualifies for both sides)
            sma5 = signal_ind.get("dSma5", signal_ind["close"])
            mid_dist = abs(signal_ind["close"] - sma5) / sma5 if sma5 else 1
            if mid_dist < _cfg.IRON_CONDOR_RANGE_PCT:
                trade_types = ["call", "put"]

        all_results = []
        for tt in trade_types:
            result = simulate_trade(
                signal_date=signal_ind["date"],
                trade_date=exit_date,
                signal_ind=signal_ind,
                exit_ind=exit_ind,
                score=score,
                spread_type=tt,
                streak_count=_streak_count,
                cumulative_pnl=cumulative_pnl,
                indicators=indicators,
            )

            # Apply rolling WR multiplier to P&L
            if result is not None and rolling_wr_mult != 1.0:
                if isinstance(result, list):
                    for r in result:
                        r["pnl_earned"] = round(r["pnl_earned"] * rolling_wr_mult, 2)
                else:
                    result["pnl_earned"] = round(result["pnl_earned"] * rolling_wr_mult, 2)

            if result is not None:
                if isinstance(result, list):
                    all_results.extend(result)
                else:
                    all_results.append(result)

        # Handle no trades (all skipped by credit floor / confirmation)
        if not all_results:
            total_skips += 1
            continue

        total_signals += 1

        for trade in all_results:
            equity += trade["pnl_earned"]
            trade["equity_after"] = round(equity, 2)
            peak_equity = max(peak_equity, equity)
            all_trades.append(trade)
            if not quiet:
                append_trade(trade)

        if not quiet and (total_signals % 50 == 0 or total_signals <= 5):
            w = sum(t["win"] for t in all_trades)
            l = sum(t["loss"] for t in all_trades)
            wr = w / (w + l) * 100 if (w + l) > 0 else 0
            total_pnl = sum(t["pnl_earned"] for t in all_trades)
            logger.info(
                f"Signal #{total_signals:>4} | {d_str} | {trade_type.upper():>4} | "
                f"P&L: ${all_results[0]['pnl_earned']:>7,.0f} | Total: ${total_pnl:>9,.0f} | WR: {wr:.1f}%"
            )

    if not quiet:
        logger.info(f"DONE — {len(all_trades)} trades, {total_trading_days} trading days, {total_skips} skipped")

    return all_trades
