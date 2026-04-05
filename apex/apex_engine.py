"""Apex — Backtest Engine.

Data loading, spread simulation, trade logging.
Adapted from lastfive_engine.py and meic_engine.py patterns.
Key differences:
  - No stop-loss reliance (filter-based risk management)
  - $30k BP constraint enforced
  - Flexible entry time/interval for research sweeps
  - Bar OPEN for strike selection (no look-ahead)
"""
from __future__ import annotations

import csv
import glob
import logging
import math
import os
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

import apex_config as _cfg

# ——————————————————————————————————————
#  LOGGING
# ——————————————————————————————————————
def setup_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    lg = logging.getLogger("apex")
    lg.setLevel(logging.DEBUG)
    lg.handlers.clear()
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    fh = logging.FileHandler(_cfg.LOG_FILE, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.info(f"Log file: {_cfg.LOG_FILE}")
    return lg

logger = setup_logger()

# ——————————————————————————————————————
#  TRADE LOG I/O
# ——————————————————————————————————————
def init_log_file():
    with open(_cfg.SAVE_FILE, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=_cfg.LOG_COLS).writeheader()

def append_trade(row: dict):
    with open(_cfg.SAVE_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=_cfg.LOG_COLS).writerow(row)

# ——————————————————————————————————————
#  DAILY INDICATORS (built once at startup)
# ——————————————————————————————————————
_DAILY_INDICATORS: dict = {}

def build_daily_indicators() -> dict:
    """Load SPX daily OHLC + VIX/VIX1D/VIX9D/VVIX daily values."""
    global _DAILY_INDICATORS

    # --- SPX daily from 1-min parquets ---
    rows = []
    for fpath in sorted(glob.glob(os.path.join(_cfg.DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
        except Exception:
            continue
        if df.empty or len(df) < 5:
            continue
        valid = df[(df["high"] > 0) & (df["close"] > 0)]
        if valid.empty:
            continue
        rows.append({
            "date": date_str,
            "open": float(valid["open"].iloc[0]),
            "high": float(valid["high"].max()),
            "low": float(valid["low"].min()),
            "close": float(valid["close"].iloc[-1]),
        })

    d = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)
    d["day_return_pct"] = (d["close"] - d["prev_close"]) / d["prev_close"] * 100
    d["day_range_pct"] = (d["high"] - d["low"]) / d["close"] * 100

    # --- SMA / EMA / RSI / ATR ---
    d["sma5"] = d["close"].rolling(5).mean()
    d["sma20"] = d["close"].rolling(20).mean()
    d["sma200"] = d["close"].rolling(200).mean()

    # EMA
    d["ema13"] = d["close"].ewm(span=13, adjust=False).mean()
    d["ema48"] = d["close"].ewm(span=48, adjust=False).mean()

    # RSI 14
    delta = d["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["rsi14"] = 100.0 - (100.0 / (1.0 + rs))

    # ATR 14
    d["tr"] = np.maximum(
        d["high"] - d["low"],
        np.maximum(abs(d["high"] - d["prev_close"]), abs(d["low"] - d["prev_close"]))
    )
    d["atr14"] = d["tr"].rolling(14).mean()

    # Williams VIX Fix (synthetic VIX from price only)
    # wvf = ((highest(close, 22) - low) / highest(close, 22)) * 100
    d["wvf_highest_close_22"] = d["close"].rolling(22).max()
    d["wvf"] = ((d["wvf_highest_close_22"] - d["low"]) / d["wvf_highest_close_22"]) * 100
    # Bollinger Band on WVF: upper = SMA(20) + 2*stdev(20)
    d["wvf_sma20"] = d["wvf"].rolling(20).mean()
    d["wvf_std20"] = d["wvf"].rolling(20).std()
    d["wvf_upper_bb"] = d["wvf_sma20"] + 2.0 * d["wvf_std20"]
    # WVF percentile rank over 50 bars
    d["wvf_pctrank"] = d["wvf"].rolling(50).apply(lambda x: (x.iloc[-1] > x.iloc[:-1]).mean() * 100 if len(x) > 1 else 50, raw=False)
    # Boolean: is WVF above upper BB?
    d["wvf_above_bb"] = (d["wvf"] >= d["wvf_upper_bb"]).astype(float)

    # --- VIX daily ---
    vix_rows = []
    for fpath in sorted(glob.glob(os.path.join(_cfg.DATA_DIR, "*", "vix", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            vdf = pd.read_parquet(fpath)
        except Exception:
            continue
        if not vdf.empty and "vix_close" in vdf.columns:
            vix_rows.append({"date": date_str, "vix_close": float(vdf["vix_close"].iloc[0])})

    # Also try vix_history.csv
    vix_hist = os.path.join(_cfg.DATA_DIR, "vix_history.csv")
    if os.path.exists(vix_hist):
        vh = pd.read_csv(vix_hist, parse_dates=["DATE"])
        vh["date"] = vh["DATE"].dt.strftime("%Y%m%d")
        for _, row in vh.iterrows():
            if pd.notna(row.get("CLOSE")):
                vix_rows.append({"date": row["date"], "vix_close": float(row["CLOSE"])})

    if vix_rows:
        vdf = pd.DataFrame(vix_rows).drop_duplicates("date", keep="last").sort_values("date")
        d = d.merge(vdf[["date", "vix_close"]], on="date", how="left")
    else:
        d["vix_close"] = np.nan

    # --- VIX1D daily ---
    vix1d_rows = []
    for fpath in sorted(glob.glob(os.path.join(_cfg.DATA_DIR, "*", "vix1d", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            vdf = pd.read_parquet(fpath)
        except Exception:
            continue
        if not vdf.empty:
            col = "vix1d_close" if "vix1d_close" in vdf.columns else "close" if "close" in vdf.columns else None
            if col:
                vix1d_rows.append({"date": date_str, "vix1d_close": float(vdf[col].iloc[0])})
    if vix1d_rows:
        v1d = pd.DataFrame(vix1d_rows).drop_duplicates("date", keep="last")
        d = d.merge(v1d, on="date", how="left")
    else:
        d["vix1d_close"] = np.nan

    # --- VIX9D daily ---
    vix9d_rows = []
    for fpath in sorted(glob.glob(os.path.join(_cfg.DATA_DIR, "*", "vix9d", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            vdf = pd.read_parquet(fpath)
        except Exception:
            continue
        if not vdf.empty:
            col = "vix9d_close" if "vix9d_close" in vdf.columns else "close" if "close" in vdf.columns else None
            if col:
                vix9d_rows.append({"date": date_str, "vix9d_close": float(vdf[col].iloc[0])})
    if vix9d_rows:
        v9d = pd.DataFrame(vix9d_rows).drop_duplicates("date", keep="last")
        d = d.merge(v9d, on="date", how="left")
    else:
        d["vix9d_close"] = np.nan

    # --- VVIX daily ---
    vvix_rows = []
    for fpath in sorted(glob.glob(os.path.join(_cfg.DATA_DIR, "*", "vvix", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            vdf = pd.read_parquet(fpath)
        except Exception:
            continue
        if not vdf.empty:
            col = "vvix_close" if "vvix_close" in vdf.columns else "close" if "close" in vdf.columns else None
            if col:
                vvix_rows.append({"date": date_str, "vvix_close": float(vdf[col].iloc[0])})
    if vvix_rows:
        vv = pd.DataFrame(vvix_rows).drop_duplicates("date", keep="last")
        d = d.merge(vv, on="date", how="left")
    else:
        d["vvix_close"] = np.nan

    # --- SqueezeMetrics GEX + DIX (multi-expiry, all strikes) ---
    sqz_path = os.path.join(os.path.dirname(_cfg.DATA_DIR), "squeezemetrics.csv")
    if os.path.exists(sqz_path):
        sqz = pd.read_csv(sqz_path)
        sqz["date"] = sqz["date"].str.replace("-", "")
        sqz = sqz.sort_values("date").reset_index(drop=True)
        sqz["prev_gex"] = sqz["gex"].shift(1)
        sqz["prev_dix"] = sqz["dix"].shift(1)
        # Compute fixed quintile boundaries from full history
        gex_vals = sqz.dropna(subset=["prev_gex"])["prev_gex"]
        _gex_q_bounds = [gex_vals.quantile(q) for q in [0.0, 0.2, 0.4, 0.6, 0.8]]
        def _gex_quintile(val):
            if pd.isna(val):
                return None
            for i in range(4, -1, -1):
                if val >= _gex_q_bounds[i]:
                    return i + 1
            return 1
        sqz["gex_quintile"] = sqz["prev_gex"].apply(_gex_quintile)
        sqz_cols = sqz[["date", "prev_gex", "prev_dix", "gex_quintile"]].rename(
            columns={"prev_gex": "sqz_gex", "prev_dix": "sqz_dix", "gex_quintile": "sqz_gex_quintile"})
        d = d.merge(sqz_cols, on="date", how="left")
        logger.info(f"  SQZ GEX loaded: {sqz_cols.dropna().shape[0]} dates")
    else:
        d["sqz_gex"] = np.nan
        d["sqz_dix"] = np.nan
        d["sqz_gex_quintile"] = np.nan
        logger.warning(f"  squeezemetrics.csv not found at {sqz_path} — GEX filter disabled")

    # --- Build dict ---
    indicators = {}
    cols_to_grab = [
        "open", "high", "low", "close", "prev_close",
        "day_return_pct", "day_range_pct",
        "sma5", "sma20", "sma200", "ema13", "ema48",
        "rsi14", "atr14",
        "wvf", "wvf_upper_bb", "wvf_pctrank", "wvf_above_bb",
        "vix_close", "vix1d_close", "vix9d_close", "vvix_close",
        "sqz_gex", "sqz_dix", "sqz_gex_quintile",
    ]
    for _, row in d.iterrows():
        rec = {}
        for c in cols_to_grab:
            val = row.get(c)
            rec[c] = float(val) if pd.notna(val) else None
        indicators[row["date"]] = rec

    _DAILY_INDICATORS = indicators
    logger.info(f"Built daily indicators: {len(indicators)} dates "
                f"({min(indicators.keys()) if indicators else '?'} → {max(indicators.keys()) if indicators else '?'})")
    return indicators


def prior_day_ind(date_str: str) -> dict | None:
    """Return indicators for the prior trading day (prevents look-ahead)."""
    all_dates = sorted(_DAILY_INDICATORS.keys())
    try:
        idx = all_dates.index(date_str)
    except ValueError:
        return None
    if idx == 0:
        return None
    return _DAILY_INDICATORS.get(all_dates[idx - 1])


# ——————————————————————————————————————
#  DATA LOADING
# ——————————————————————————————————————
def _load_option_quotes(date_str: str) -> pd.DataFrame | None:
    """Load option quotes parquet for a date, with indexed lookup."""
    path = os.path.join(_cfg.DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    # Multi-index for O(1) lookups
    df = df.set_index(["right", "strike", "time_str"]).sort_index()
    return df


def _get_quote(quotes_df: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    """O(1) indexed quote lookup."""
    try:
        row = quotes_df.loc[(right, int(strike), time_str)]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return {"bid": float(row["bid"]), "ask": float(row["ask"]), "mid": float(row["mid"])}
    except KeyError:
        return None


def _load_spx_1min(date_str: str) -> pd.DataFrame | None:
    """Load SPX 1-min OHLC bars."""
    path = os.path.join(_cfg.DATA_DIR, date_str[:4], "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df.empty or len(df) < 5:
        return None
    df["time_str"] = df["timestamp"].str[11:19] if "timestamp" in df.columns else None
    return df


def _get_spx_at_time(spx_df: pd.DataFrame, time_str: str) -> float | None:
    """Get SPX OPEN price at a given time (bar open = no look-ahead)."""
    if spx_df is None or "time_str" not in spx_df.columns:
        return None
    mask = spx_df["time_str"] == time_str
    rows = spx_df[mask]
    if rows.empty:
        return None
    return float(rows.iloc[0]["open"])


def _get_spx_close_at_time(spx_df: pd.DataFrame, time_str: str) -> float | None:
    """Get SPX close at a given time (for settlement only)."""
    if spx_df is None or "time_str" not in spx_df.columns:
        return None
    mask = spx_df["time_str"] == time_str
    rows = spx_df[mask]
    if rows.empty:
        return None
    return float(rows.iloc[0]["close"])


def _get_morning_return(spx_df: pd.DataFrame, entry_time_str: str) -> float | None:
    """Compute return from 9:30 open to entry time open."""
    open_930 = _get_spx_at_time(spx_df, "09:30:00")
    entry_price = _get_spx_at_time(spx_df, entry_time_str)
    if open_930 and entry_price and open_930 > 0:
        return (entry_price - open_930) / open_930 * 100.0
    return None


def _load_vix_intraday(date_str: str, time_str: str) -> float | None:
    """Load VIX value at a specific intraday time."""
    for subdir in ["vix_ohlc", "vix_tv_ohlc"]:
        path = os.path.join(_cfg.DATA_DIR, date_str[:4], subdir, f"{date_str}.parquet")
        if os.path.exists(path):
            try:
                df = pd.read_parquet(path)
                if df.empty:
                    continue
                df["time_str"] = df["timestamp"].str[11:19]
                mask = df["time_str"] == time_str
                rows = df[mask]
                if not rows.empty:
                    col = "close" if "close" in df.columns else "open"
                    return float(rows.iloc[0][col])
            except Exception:
                continue
    return None


# ——————————————————————————————————————
#  OPEN INTEREST
# ——————————————————————————————————————
def _load_oi(date_str: str) -> pd.DataFrame | None:
    """Load option open interest for a date."""
    path = os.path.join(_cfg.DATA_DIR, date_str[:4], "option_open_interest", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        return df if not df.empty else None
    except Exception:
        return None


def _get_nearby_oi(oi_df: pd.DataFrame, right: str, strike: int, spread: int = 10) -> int:
    """Get total open interest within `spread` points of a strike."""
    if oi_df is None:
        return -1  # Unknown
    side = oi_df[oi_df["right"] == right]
    nearby = side[(side["strike"] >= strike - spread) & (side["strike"] <= strike + spread)]
    if nearby.empty:
        return 0
    col = "open_interest" if "open_interest" in nearby.columns else "oi"
    return int(nearby[col].sum()) if col in nearby.columns else 0


# ——————————————————————————————————————
#  STRIKE SELECTION
# ——————————————————————————————————————
def _find_put_strikes(spot: float, otm_distance: float, width: float,
                      available_strikes: set) -> tuple | None:
    """Find put spread strikes: short below spot, long further below."""
    target = spot - otm_distance
    short = int(math.floor(target / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)
    long = short - int(width)

    if short in available_strikes and long in available_strikes:
        return short, long

    # Fallback: search available strikes
    candidates = sorted((s for s in available_strikes if s <= spot - otm_distance), reverse=True)
    for s in candidates:
        l = s - int(width)
        if l in available_strikes:
            return s, l
    return None


def _find_call_strikes(spot: float, otm_distance: float, width: float,
                       available_strikes: set) -> tuple | None:
    """Find call spread strikes: short above spot, long further above."""
    target = spot + otm_distance
    short = int(math.ceil(target / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)
    long = short + int(width)

    if short in available_strikes and long in available_strikes:
        return short, long

    candidates = sorted(s for s in available_strikes if s >= spot + otm_distance)
    for s in candidates:
        l = s + int(width)
        if l in available_strikes:
            return s, l
    return None


# ——————————————————————————————————————
#  SPREAD PRICING
# ——————————————————————————————————————
def _price_spread(quotes_df, right: str, short_strike: int, long_strike: int,
                  time_str: str) -> dict | None:
    """Get entry credit and full quote details for a spread."""
    short_q = _get_quote(quotes_df, right, short_strike, time_str)
    long_q = _get_quote(quotes_df, right, long_strike, time_str)
    if not short_q or not long_q:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < _cfg.MIN_NET_CREDIT:
        return None

    return {
        "credit": credit,
        "short_bid": short_q["bid"],
        "short_ask": short_q["ask"],
        "long_bid": long_q["bid"],
        "long_ask": long_q["ask"],
    }


# ——————————————————————————————————————
#  SETTLEMENT
# ——————————————————————————————————————
def _settle_spread(option_type: str, short_strike: int, long_strike: int,
                   spx_exit: float, width: float) -> float:
    """Calculate settlement value of a spread at expiration."""
    if option_type == "PUT":
        if spx_exit >= short_strike:
            return 0.0  # Max profit (OTM)
        elif spx_exit <= long_strike:
            return width  # Max loss (deep ITM)
        else:
            return short_strike - spx_exit  # Partial loss
    else:  # CALL
        if spx_exit <= short_strike:
            return 0.0
        elif spx_exit >= long_strike:
            return width
        else:
            return spx_exit - short_strike


# ——————————————————————————————————————
#  MAIN BACKTEST
# ——————————————————————————————————————
def run_backtest(indicators: dict, **overrides) -> list:
    """Run full backtest simulation. Returns list of trade dicts.

    overrides: any _cfg attribute can be overridden for sweep purposes.
    """
    # Apply overrides
    orig_values = {}
    for k, v in overrides.items():
        if hasattr(_cfg, k):
            orig_values[k] = getattr(_cfg, k)
            setattr(_cfg, k, v)

    try:
        return _run_backtest_inner(indicators)
    finally:
        # Restore originals
        for k, v in orig_values.items():
            setattr(_cfg, k, v)


def _run_backtest_inner(indicators: dict) -> list:
    init_log_file()

    start = _cfg.PILOT_YEAR_START.replace("-", "")
    end = _cfg.PILOT_YEAR_END.replace("-", "")
    all_dates = sorted(indicators.keys())
    trade_dates = [d for d in all_dates if start <= d <= end]

    all_trades = []
    equity = _cfg.STARTING_EQUITY
    peak_equity = equity

    skip_reasons = {
        "holiday": 0, "early_close": 0, "no_data": 0, "no_quotes": 0,
        "no_spx": 0, "vix_filter": 0, "vix_term": 0, "vvix_filter": 0, "wvf_filter": 0,
        "prior_return": 0, "morning_mom": 0, "fomc": 0, "cpi": 0,
        "nfp": 0, "opex": 0, "tw": 0, "dow_filter": 0, "bp_limit": 0,
    }

    for date_str in trade_dates:
        # --- Calendar skips ---
        if date_str in _cfg.MARKET_HOLIDAYS:
            skip_reasons["holiday"] += 1
            continue
        if date_str in _cfg.EARLY_CLOSE_DAYS:
            skip_reasons["early_close"] += 1
            continue

        # --- Calendar event skips ---
        if _cfg.ENABLE_FOMC_SKIP and date_str in _cfg.FOMC_DATES:
            skip_reasons["fomc"] += 1
            continue
        if _cfg.ENABLE_CPI_SKIP and date_str in _cfg.CPI_DATES:
            skip_reasons["cpi"] += 1
            continue
        if _cfg.ENABLE_NFP_SKIP and date_str in _cfg.NFP_DATES:
            skip_reasons["nfp"] += 1
            continue
        if _cfg.ENABLE_TW_SKIP and date_str in _cfg.TRIPLE_WITCHING_DATES:
            skip_reasons["tw"] += 1
            continue

        # --- Prior-day indicator filters ---
        prev = prior_day_ind(date_str)
        if prev is None:
            continue

        vix = prev.get("vix_close")
        if _cfg.ENABLE_VIX_MAX_FILTER and vix is not None and vix > _cfg.VIX_MAX:
            skip_reasons["vix_filter"] += 1
            continue
        if _cfg.ENABLE_VIX_MIN_FILTER and vix is not None and vix < _cfg.VIX_MIN:
            skip_reasons["vix_filter"] += 1
            continue

        # VIX term structure
        if _cfg.ENABLE_VIX_TERM_FILTER:
            vix9d = prev.get("vix9d_close")
            if vix is not None and vix9d is not None and vix9d > 0:
                ratio = vix / vix9d
                if ratio > _cfg.VIX_TERM_MAX_RATIO:
                    skip_reasons["vix_term"] += 1
                    continue

        # VVIX
        if _cfg.ENABLE_VVIX_FILTER:
            vvix = prev.get("vvix_close")
            if vvix is not None and vvix > _cfg.VVIX_MAX:
                skip_reasons["vvix_filter"] += 1
                continue

        # Williams VIX Fix filter (price-derived fear gauge)
        if _cfg.ENABLE_WVF_FILTER:
            wvf_val = prev.get("wvf")
            wvf_bb = prev.get("wvf_upper_bb")
            wvf_pct = prev.get("wvf_pctrank")
            if _cfg.WVF_MODE == "bb" and wvf_val is not None and wvf_bb is not None:
                if wvf_val >= wvf_bb:
                    skip_reasons["wvf_filter"] += 1
                    continue
            elif _cfg.WVF_MODE == "percentile" and wvf_pct is not None:
                if wvf_pct >= _cfg.WVF_PERCENTILE_MAX:
                    skip_reasons["wvf_filter"] += 1
                    continue
            elif _cfg.WVF_MODE == "absolute" and wvf_val is not None:
                if wvf_val >= _cfg.WVF_ABSOLUTE_MAX:
                    skip_reasons["wvf_filter"] += 1
                    continue

        # Prior-day return
        if _cfg.ENABLE_PRIOR_DAY_RETURN_FILTER:
            pdr = prev.get("day_return_pct")
            if pdr is not None and abs(pdr) > _cfg.PRIOR_DAY_RETURN_MAX:
                skip_reasons["prior_return"] += 1
                continue

        # GEX regime filter (SqueezeMetrics multi-expiry GEX)
        if _cfg.ENABLE_GEX_FILTER:
            gex_q = prev.get("sqz_gex_quintile")
            if gex_q is not None and gex_q < _cfg.GEX_MIN_QUINTILE:
                skip_reasons.setdefault("gex_filter", 0)
                skip_reasons["gex_filter"] += 1
                continue

        # Gap filter (today's open vs prior close)
        if _cfg.ENABLE_GAP_FILTER:
            curr = indicators.get(date_str, {})
            today_open = curr.get("open")
            prev_close = prev.get("close")
            if today_open and prev_close and prev_close > 0:
                gap_pct = abs((today_open - prev_close) / prev_close * 100.0)
                if gap_pct > _cfg.GAP_MAX:
                    skip_reasons.setdefault("gap_filter", 0)
                    skip_reasons["gap_filter"] += 1
                    continue

        # Consecutive calm days filter
        if _cfg.ENABLE_CONSECUTIVE_CALM_FILTER:
            all_sorted = sorted(_DAILY_INDICATORS.keys())
            try:
                idx = all_sorted.index(date_str)
                calm_count = 0
                for i in range(1, _cfg.CONSECUTIVE_CALM_DAYS + 1):
                    if idx - i < 0:
                        break
                    past = _DAILY_INDICATORS.get(all_sorted[idx - i], {})
                    past_ret = past.get("day_return_pct")
                    if past_ret is not None and abs(past_ret) < _cfg.CONSECUTIVE_CALM_THRESHOLD:
                        calm_count += 1
                    else:
                        break
                if calm_count < _cfg.CONSECUTIVE_CALM_DAYS:
                    skip_reasons.setdefault("consecutive_calm", 0)
                    skip_reasons["consecutive_calm"] += 1
                    continue
            except ValueError:
                pass

        # Prior-day range
        if _cfg.ENABLE_PRIOR_DAY_RANGE_FILTER:
            pdr_range = prev.get("day_range_pct")
            if pdr_range is not None and pdr_range > _cfg.PRIOR_DAY_RANGE_MAX:
                skip_reasons.setdefault("prior_range", 0)
                skip_reasons["prior_range"] += 1
                continue

        # Day of week
        if _cfg.ENABLE_DOW_FILTER:
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                if dt.weekday() in _cfg.SKIP_DAYS_OF_WEEK:
                    skip_reasons["dow_filter"] += 1
                    continue
            except ValueError:
                pass

        # --- Load data ---
        spx_df = _load_spx_1min(date_str)
        if spx_df is None:
            skip_reasons["no_data"] += 1
            continue

        quotes_df = _load_option_quotes(date_str)
        if quotes_df is None:
            skip_reasons["no_quotes"] += 1
            continue

        # Load OI data (optional, for OI filter)
        oi_df = _load_oi(date_str) if _cfg.ENABLE_OI_FILTER else None

        # Straddle filter: check ATM straddle at 15:50
        if _cfg.ENABLE_STRADDLE_FILTER:
            straddle_spot = _get_spx_at_time(spx_df, _cfg.STRADDLE_TIME)
            if straddle_spot is not None:
                atm_strike = int(round(straddle_spot / _cfg.STRIKE_STEP) * _cfg.STRIKE_STEP)
                call_q = _get_quote(quotes_df, "C", atm_strike, _cfg.STRADDLE_TIME)
                put_q = _get_quote(quotes_df, "P", atm_strike, _cfg.STRADDLE_TIME)
                if call_q and put_q:
                    straddle_price = call_q["mid"] + put_q["mid"]
                    if straddle_price > _cfg.STRADDLE_MAX:
                        skip_reasons.setdefault("straddle_filter", 0)
                        skip_reasons["straddle_filter"] += 1
                        continue

        # Vol consumed filter: (range by 15:00) / (VIX/16 expected daily range)
        if _cfg.ENABLE_VOL_CONSUMED_FILTER and vix is not None:
            open_930_vc = _get_spx_at_time(spx_df, "09:30:00")
            if open_930_vc and open_930_vc > 0:
                bars_to_1500 = spx_df[spx_df["time_str"] <= "15:00:00"]
                if not bars_to_1500.empty:
                    range_to_1500 = float(bars_to_1500["high"].max()) - float(bars_to_1500["low"].min())
                    vix16_daily_pts = open_930_vc * (vix / 16) / 100
                    if vix16_daily_pts > 0:
                        vol_consumed = range_to_1500 / vix16_daily_pts
                        if vol_consumed > _cfg.VOL_CONSUMED_MAX:
                            skip_reasons.setdefault("vol_consumed", 0)
                            skip_reasons["vol_consumed"] += 1
                            continue

        # Vol surprise filter: range by 15:00 / VIX prediction (no look-ahead)
        if _cfg.ENABLE_VOL_SURPRISE_FILTER and vix is not None:
            open_930_vs = _get_spx_at_time(spx_df, "09:30:00")
            if open_930_vs and open_930_vs > 0 and "time_str" in spx_df.columns:
                bars_to_1500 = spx_df[spx_df["time_str"] <= "15:00:00"]
                if not bars_to_1500.empty:
                    range_to_1500 = float(bars_to_1500["high"].max()) - float(bars_to_1500["low"].min())
                    vix16_daily = open_930_vs * (vix / 16) / 100
                    if vix16_daily > 0:
                        vol_surprise = range_to_1500 / vix16_daily
                        if vol_surprise > _cfg.VOL_SURPRISE_MAX:
                            skip_reasons.setdefault("vol_surprise", 0)
                            skip_reasons["vol_surprise"] += 1
                            continue

        # VIX1D/VIX ratio filter (backwardation = risky)
        if _cfg.ENABLE_VIX1D_RATIO_FILTER:
            vix1d = prev.get("vix1d_close")
            if vix is not None and vix1d is not None and vix > 0:
                ratio_1d = vix1d / vix
                if ratio_1d > _cfg.VIX1D_RATIO_MAX:
                    skip_reasons.setdefault("vix1d_ratio", 0)
                    skip_reasons["vix1d_ratio"] += 1
                    continue

        # Safety ratio filter: d / (VIX@15:50/16 implied 5-min move)
        if _cfg.ENABLE_SAFETY_RATIO_FILTER:
            vix_1550 = _load_vix_intraday(date_str, "15:50:00")
            spot_1550 = _get_spx_at_time(spx_df, "15:50:00")
            if vix_1550 and spot_1550 and spot_1550 > 0:
                import math as _math
                expected_5min = spot_1550 * (vix_1550 / 16) / 100 * _math.sqrt(5 / 390)
                if expected_5min > 0:
                    safety = _cfg.MIN_OTM_DISTANCE / expected_5min
                    if safety < _cfg.SAFETY_RATIO_MIN:
                        skip_reasons.setdefault("safety_ratio", 0)
                        skip_reasons["safety_ratio"] += 1
                        continue

        # Determine available strikes (from index level 1)
        try:
            available_strikes = set(quotes_df.index.get_level_values("strike").unique())
        except Exception:
            skip_reasons["no_quotes"] += 1
            continue

        # Intraday range filter (check range by 15:00)
        if _cfg.ENABLE_INTRADAY_RANGE_FILTER:
            open_930 = _get_spx_at_time(spx_df, "09:30:00")
            at_1500_high = None
            at_1500_low = None
            if "time_str" in spx_df.columns:
                bars_to_1500 = spx_df[spx_df["time_str"] <= "15:00:00"]
                if not bars_to_1500.empty and open_930 and open_930 > 0:
                    at_1500_high = float(bars_to_1500["high"].max())
                    at_1500_low = float(bars_to_1500["low"].min())
                    intraday_range = (at_1500_high - at_1500_low) / open_930 * 100.0
                    if intraday_range > _cfg.INTRADAY_RANGE_MAX:
                        skip_reasons.setdefault("intraday_range", 0)
                        skip_reasons["intraday_range"] += 1
                        continue

        # Adaptive OTM distance based on VIX
        effective_otm = _cfg.MIN_OTM_DISTANCE
        if _cfg.ENABLE_ADAPTIVE_OTM and vix is not None:
            for vix_lo, vix_hi, otm_dist in _cfg.ADAPTIVE_OTM_RULES:
                if vix_lo <= vix < vix_hi:
                    effective_otm = otm_dist
                    break

        # --- Determine entry times ---
        if _cfg.ENABLE_MULTI_ENTRY:
            entry_times = []
            t = datetime.combine(datetime.min, _cfg.ENTRY_START)
            t_end = datetime.combine(datetime.min, _cfg.ENTRY_END)
            while t <= t_end:
                entry_times.append(t.strftime("%H:%M:%S"))
                t += timedelta(minutes=_cfg.ENTRY_INTERVAL)
        else:
            entry_times = [_cfg.ENTRY_TIME]

        # --- Day simulation ---
        day_positions = []
        day_pnl = 0.0
        day_open_risk = 0.0
        stopped_today = False

        for etime in entry_times:
            if stopped_today:
                break

            # Morning momentum filter
            if _cfg.ENABLE_MORNING_MOMENTUM_FILTER:
                mom = _get_morning_return(spx_df, etime)
                if mom is not None and abs(mom) > _cfg.MORNING_MOMENTUM_MAX:
                    skip_reasons["morning_mom"] += 1
                    break  # Skip rest of day

            spot = _get_spx_at_time(spx_df, etime)
            if spot is None:
                continue

            # Determine directions to trade
            directions = []
            if _cfg.DIRECTION_MODE == "always_put":
                directions = ["PUT"]
            elif _cfg.DIRECTION_MODE == "always_call":
                directions = ["CALL"]
            elif _cfg.DIRECTION_MODE == "both_sides":
                directions = ["PUT", "CALL"]
            elif _cfg.DIRECTION_MODE == "ema":
                if prev.get("ema13") is not None and prev.get("ema48") is not None:
                    if prev["ema13"] > prev["ema48"]:
                        directions = ["PUT"]  # Bullish → sell puts
                    else:
                        directions = ["CALL"]  # Bearish → sell calls
                else:
                    directions = ["PUT"]  # Default

            for opt_type in directions:
                right = "P" if opt_type == "PUT" else "C"

                # Find strikes (use adaptive OTM if enabled)
                otm_dist = effective_otm
                if opt_type == "PUT":
                    strikes = _find_put_strikes(spot, otm_dist, _cfg.WIDTH, available_strikes)
                else:
                    strikes = _find_call_strikes(spot, otm_dist, _cfg.WIDTH, available_strikes)

                if strikes is None:
                    continue

                short_strike, long_strike = strikes
                otm_dist = abs(spot - short_strike)

                # OI filter (check nearby open interest at short strike)
                if _cfg.ENABLE_OI_FILTER:
                    if oi_df is not None:
                        nearby_oi = _get_nearby_oi(oi_df, right, short_strike, _cfg.OI_NEARBY_RANGE)
                        if nearby_oi >= 0 and nearby_oi < _cfg.OI_MIN_NEARBY:
                            skip_reasons.setdefault("oi_filter", 0)
                            skip_reasons["oi_filter"] += 1
                            continue

                # Price the spread
                pricing = _price_spread(quotes_df, right, short_strike, long_strike, etime)
                if pricing is None:
                    continue

                credit = pricing["credit"]
                qty = _cfg.QTY

                # BP check
                max_loss_per = (_cfg.WIDTH - credit) * 100.0
                position_risk = max_loss_per * qty
                if day_open_risk + position_risk > _cfg.ACCOUNT_BP:
                    # Try reducing qty
                    if max_loss_per > 0:
                        max_qty = int((_cfg.ACCOUNT_BP - day_open_risk) / max_loss_per)
                        if max_qty < 1:
                            skip_reasons["bp_limit"] += 1
                            continue
                        qty = max_qty
                        position_risk = max_loss_per * qty

                day_open_risk += position_risk

                day_positions.append({
                    "entry_date": date_str,
                    "entry_time": etime,
                    "option_type": opt_type,
                    "right": right,
                    "short_strike": short_strike,
                    "long_strike": long_strike,
                    "width": _cfg.WIDTH,
                    "credit": credit,
                    "short_bid": pricing["short_bid"],
                    "short_ask": pricing["short_ask"],
                    "long_bid": pricing["long_bid"],
                    "long_ask": pricing["long_ask"],
                    "qty": qty,
                    "vix_level": vix,
                    "otm_distance": otm_dist,
                    "spx_at_entry": spot,
                })

        # --- Settle all positions at EOD (0DTE expiration) ---
        # Get SPX at 16:00 (or last available close)
        spx_exit = None
        for exit_time in ["16:00:00", "15:59:00", "15:58:00"]:
            spx_exit = _get_spx_close_at_time(spx_df, exit_time)
            if spx_exit is not None:
                break
        if spx_exit is None:
            # Use last bar close
            spx_exit = float(spx_df["close"].iloc[-1])

        exit_time_str = "16:00:00"

        for pos in day_positions:
            settlement = _settle_spread(
                pos["option_type"], pos["short_strike"], pos["long_strike"],
                spx_exit, pos["width"]
            )
            commission = 4 * pos["qty"] * _cfg.COMMISSION  # 2 legs × open + close
            net_pnl = (pos["credit"] - settlement) * pos["qty"] * 100.0 - commission

            equity += net_pnl
            day_pnl += net_pnl
            peak_equity = max(peak_equity, equity)
            is_win = net_pnl > 0

            trade = {
                "entry_date": date_str,
                "entry_time": pos["entry_time"],
                "option_type": pos["option_type"],
                "short_strike": pos["short_strike"],
                "long_strike": pos["long_strike"],
                "width": pos["width"],
                "credit_received": round(pos["credit"], 4),
                "short_bid": round(pos["short_bid"], 4),
                "short_ask": round(pos["short_ask"], 4),
                "long_bid": round(pos["long_bid"], 4),
                "long_ask": round(pos["long_ask"], 4),
                "qty": pos["qty"],
                "vix_level": round(pos["vix_level"], 2) if pos["vix_level"] else "",
                "otm_distance": round(pos["otm_distance"], 1),
                "exit_value": round(settlement, 4),
                "pnl": round(net_pnl, 2),
                "outcome": "WIN" if is_win else "LOSS",
                "close_time": exit_time_str,
                "spx_at_entry": round(pos["spx_at_entry"], 2),
                "spx_at_exit": round(spx_exit, 2),
                "equity_after": round(equity, 2),
            }
            append_trade(trade)
            all_trades.append(trade)

        if day_positions:
            logger.debug(f"[{date_str}] {len(day_positions)} trades, day P&L=${day_pnl:+.0f}, equity=${equity:,.0f}")

    # --- Summary ---
    logger.info("=" * 60)
    logger.info("SKIP REASONS:")
    for reason, count in sorted(skip_reasons.items()):
        if count > 0:
            logger.info(f"  {reason:<20}: {count}")
    logger.info(f"  {'TOTAL TRADES':<20}: {len(all_trades)}")
    logger.info("=" * 60)

    return all_trades


# ——————————————————————————————————————
#  METRICS
# ——————————————————————————————————————
def compute_metrics(trades: list) -> dict:
    """Compute performance metrics from trade list."""
    if not trades:
        return {
            "num_trades": 0, "win_rate": 0, "total_pnl": 0, "max_drawdown": 0,
            "sharpe": 0, "calmar": 0, "profit_factor": 0, "avg_win": 0,
            "avg_loss": 0, "max_single_loss": 0,
        }

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    num_trades = len(pnls)
    win_rate = len(wins) / num_trades * 100 if num_trades > 0 else 0

    # Max drawdown
    equity_curve = []
    eq = _cfg.STARTING_EQUITY
    for p in pnls:
        eq += p
        equity_curve.append(eq)
    peak = _cfg.STARTING_EQUITY
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = eq - peak
        max_dd = min(max_dd, dd)

    # Daily P&L for Sharpe
    daily_pnl = {}
    for t in trades:
        d = t["entry_date"]
        daily_pnl[d] = daily_pnl.get(d, 0.0) + t["pnl"]
    daily_vals = list(daily_pnl.values())

    if len(daily_vals) > 1:
        mean_daily = np.mean(daily_vals)
        std_daily = np.std(daily_vals, ddof=1)
        sharpe = (mean_daily / std_daily) * math.sqrt(252) if std_daily > 0 else 0
    else:
        sharpe = 0

    calmar = total_pnl / abs(max_dd) if max_dd != 0 else 0

    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    max_single_loss = min(pnls) if pnls else 0

    return {
        "num_trades": num_trades,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_single_loss": round(max_single_loss, 2),
    }
