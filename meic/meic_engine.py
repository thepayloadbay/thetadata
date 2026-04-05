"""MEIC engine — afternoon SPX credit spreads, both PUT + CALL independently.

Adapted from MEDS engine. Key differences:
  - Afternoon entry window (default 12:00-15:30)
  - Both PUT and CALL spreads opened independently each bar (full qty each side)
  - 50pt wide spreads, $1.20 min credit, 30pt min OTM
  - 1-min MTM (less time to recover in afternoon)
"""
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

from meic_config import *
import meic_config as _cfg

from meic_config import _out, _build_calendar_event_dates, _RUN_TS


# ---------------------------------------------
#  LOGGING
# ---------------------------------------------
def setup_logger() -> logging.Logger:
    from datetime import datetime as _dt
    run_ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_FILE.replace(".log", f"_{run_ts}.log")

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    lgr = logging.getLogger("meic")
    lgr.setLevel(logging.DEBUG)
    lgr.handlers.clear()

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lgr.addHandler(ch)

    fh = logging.FileHandler(log_path, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lgr.addHandler(fh)

    lgr.info(f"Log file  : {log_path}")
    return lgr

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


def date_to_expiry(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"


def print_settings_summary():
    logger.info("--- MEIC Settings ---")
    logger.info(f"  WIDTH={WIDTH}  QTY={QTY}  MIN_CREDIT={MIN_NET_CREDIT}  MIN_OTM={MIN_OTM_DISTANCE}")
    logger.info(f"  DIRECTION={DIRECTION_MODE}")
    logger.info(f"  ENTRY: {ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min")
    logger.info(f"  PER_POS_SL: PUT={ASYM_PUT_SL_AMOUNT} CALL={ASYM_CALL_SL_AMOUNT}")
    logger.info(f"  DAILY_SL={DAILY_SL}  AFTN_SL={AFTERNOON_SL_AMOUNT} after {AFTERNOON_SL_CUTOFF.strftime('%H:%M')}")
    logger.info(f"  VIX_MAX={VIX_MAX_FILTER}  OPEN_MTM_GATE={OPEN_MTM_GATE_THRESHOLD}")
    if _cfg.ENABLE_ORB_FILTER:
        logger.info(f"  ORB_FILTER=ON (skip if SPX breaks 9:30-10:00 range)")
    if _cfg.ENABLE_PRIOR_DAY_DIRECTION_FILTER:
        logger.info(f"  PRIOR_DAY_DIRECTION_FILTER=ON (skip bearish prior days)")
    if _cfg.ENABLE_ER_DIRECTION:
        logger.info(f"  ER_DIRECTION=ON (Kaufman ER>{_cfg.ER_TREND_THRESHOLD} + downtrend = skip)")
    if _cfg.ENABLE_ORB_BREAKOUT_TIME_FILTER:
        logger.info(f"  ORB_BREAKOUT_TIME_FILTER=ON (breakout before {_cfg.ORB_LATE_BREAKOUT_HOUR}:00 = skip)")
    logger.info("---------------------")


# ---------------------------------------------
#  DAILY INDICATORS
# ---------------------------------------------
_DAILY_INDICATORS: dict = {}
_EOM_DATES: set = set()
_CPI_DATES: set = set()
_PCE_DATES: set = set()
_NFP_DATES: set = set()
_EOQ_DATES: set = set()
_PRE_TW_DATES: set = set()
_POST_HOL_DATES: set = set()


def _build_daily_indicators(compute_full: bool = False) -> dict:
    """Aggregate 1-min SPX OHLC to daily bars. Minimal version for MEIC."""
    rows = []
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            df = pd.read_parquet(fpath)
            if df.empty or len(df) < 5:
                continue
            rows.append({
                "date": date_str,
                "open": float(df["open"].iloc[0]),
                "high": float(df["high"].max()),
                "low": float(df["low"].min()),
                "close": float(df["close"].iloc[-1]),
            })
        except Exception:
            pass
    if not rows:
        return {}
    d = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)
    d["dGapPercent"] = (d["open"] - d["prev_close"]) / d["prev_close"] * 100
    result = {}
    for _, row in d.iterrows():
        result[row["date"]] = row.to_dict()
    return result


# ---------------------------------------------
#  QUOTE CACHE & DATA ACCESS
# ---------------------------------------------
_quote_cache: dict = {}
_local_quotes_df: pd.DataFrame | None = None
_local_quotes_date: str | None = None


def clear_day_cache():
    global _quote_cache
    _quote_cache.clear()


def _load_local_quotes(date_str: str) -> None:
    global _local_quotes_df, _local_quotes_date
    if _local_quotes_date == date_str:
        return
    path = pathlib.Path(DATA_DIR) / date_str[:4] / "option_quotes" / f"{date_str}.parquet"
    if not path.exists():
        _local_quotes_df = pd.DataFrame()
        _local_quotes_date = date_str
        return
    df = pd.read_parquet(path).copy()
    df["time_str"] = df["timestamp"].str[11:19]
    _local_quotes_df = df.set_index(["right", "strike", "time_str"]).sort_index()
    _local_quotes_date = date_str
    logger.debug(f"Loaded local quotes for {date_str}: {len(df)} rows")


def load_quote_disk_cache() -> None:
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
        logger.info(f"[quote cache] Loaded {loaded:,} entries ({len(_quote_cache):,} total)")
    except Exception as e:
        logger.warning(f"[quote cache] Failed to load: {e}")


def save_quote_disk_cache() -> None:
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
            logger.info(f"[quote cache] Saved {len(combined):,} entries (+{added} new)")
        except Exception:
            new_df.to_parquet(path, index=False)
    else:
        new_df.to_parquet(path, index=False)
        logger.info(f"[quote cache] Created with {len(new_df):,} entries")


async def fetch_quote_at_time(session, date_str, expiry, right, strike, bar_time_str) -> dict | None:
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


async def fetch_quote_cached(session, date_str, expiry, right, strike, bar_time_str) -> dict | None:
    key = (date_str, right, strike, bar_time_str)
    if key in _quote_cache:
        return _quote_cache[key]
    q = await fetch_quote_at_time(session, date_str, expiry, right, strike, bar_time_str)
    _quote_cache[key] = q
    return q


async def fetch_quotes_for_strikes_cached(session, date_str, expiry, right, strikes, bar_time_str) -> dict:
    async def fetch_one(s):
        q = await fetch_quote_cached(session, date_str, expiry, right, s, bar_time_str)
        return s, q
    results = await asyncio.gather(*[fetch_one(s) for s in strikes])
    return {s: q for s, q in results if q is not None}


async def get_strikes(session, expiry: str) -> list:
    date_str = expiry.replace("-", "")
    path = pathlib.Path(DATA_DIR) / date_str[:4] / "option_quotes" / f"{date_str}.parquet"
    if not path.exists():
        logger.warning(f"No option_quotes parquet for {date_str}")
        return []
    df = pd.read_parquet(path, columns=["strike"])
    strikes = sorted(int(s) for s in df["strike"].unique())
    logger.info(f"Got {len(strikes)} strikes. Range: {strikes[0]} - {strikes[-1]}")
    return strikes


async def fetch_vix_level(session, date_str: str) -> float | None:
    path = pathlib.Path(DATA_DIR) / date_str[:4] / "vix_ohlc" / f"{date_str}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["close"])
    closes = df["close"].dropna()
    return float(closes.iloc[-1]) if len(closes) > 0 else None


# ---------------------------------------------
#  SESSION FACTORY
# ---------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def _get_session():
    yield None  # local data only


# ---------------------------------------------
#  PER-DAY PROCESSOR
# ---------------------------------------------
async def _fetch_day_data(session, date_str: str, seed_bars: int | None = None) -> dict | None:
    if seed_bars is None:
        seed_bars = EMA_SLOW
    logger.info(f"{'='*50}")
    logger.info(f"Processing {date_str}")
    logger.info(f"{'='*50}")

    prev_date = (pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}") - pd.offsets.BDay(1))
    prev_str = prev_date.strftime("%Y%m%d")
    expiry = date_to_expiry(date_str)

    ohlc_path = pathlib.Path(DATA_DIR) / date_str[:4] / "spx_ohlc" / f"{date_str}.parquet"
    if not ohlc_path.exists():
        logger.error(f"No SPX OHLC parquet for {date_str}")
        return None
    df_today = pd.read_parquet(ohlc_path)
    closes = df_today["close"].tolist()
    times = [ts[:19] for ts in df_today["timestamp"].tolist()]
    day_open = float(df_today["open"].iloc[0]) if not df_today.empty else None
    if len(closes) < 5:
        logger.warning("Not enough bars -- skipping day.")
        return None

    prev_path = pathlib.Path(DATA_DIR) / prev_str[:4] / "spx_ohlc" / f"{prev_str}.parquet"
    seed_closes = []
    if prev_path.exists():
        df_prev = pd.read_parquet(prev_path)
        seed_closes = df_prev["close"].tolist()[-seed_bars:]
        logger.info(f"Seeding EMA with {len(seed_closes)} bars from {prev_str}")

    vix_level, all_strikes = await asyncio.gather(
        fetch_vix_level(session, date_str),
        get_strikes(session, expiry),
    )

    all_closes = seed_closes + closes
    seed_len = len(seed_closes)

    if vix_level is not None:
        logger.info(f"VIX level: {vix_level:.2f}")

    # VIX filters
    if VIX_MIN_FILTER is not None and vix_level is not None and vix_level < VIX_MIN_FILTER:
        logger.info(f"VIX={vix_level:.2f} < {VIX_MIN_FILTER} -- skipping day")
        return None
    if VIX_MAX_FILTER is not None and vix_level is not None and vix_level > VIX_MAX_FILTER:
        logger.info(f"VIX={vix_level:.2f} > {VIX_MAX_FILTER} -- skipping day")
        return None
    if _cfg.SKIP_VIX_RANGE is not None and vix_level is not None:
        lo, hi = _cfg.SKIP_VIX_RANGE
        if lo <= vix_level <= hi:
            logger.info(f"VIX={vix_level:.2f} in SKIP_VIX_RANGE ({lo},{hi}) -- skipping day")
            return None

    # Calendar event skips
    for _skip_flag, _skip_dates, _skip_label in [
        (_cfg.ENABLE_CPI_SKIP,      _CPI_DATES,      "CPI"),
        (_cfg.ENABLE_FOMC_SKIP,     FOMC_DATES,       "FOMC"),
        (_cfg.ENABLE_PCE_SKIP,      _PCE_DATES,       "PCE"),
        (_cfg.ENABLE_EOQ_SKIP,      _EOQ_DATES,       "EOQ"),
        (_cfg.ENABLE_PRE_TW_SKIP,   _PRE_TW_DATES,    "pre-TW"),
        (_cfg.ENABLE_POST_HOL_SKIP, _POST_HOL_DATES,  "post-holiday"),
        (_cfg.ENABLE_EOM_SKIP,      _EOM_DATES,       "EOM"),
        (_cfg.ENABLE_TW_SKIP,       TRIPLE_WITCHING_DATES, "TW"),
    ]:
        if _skip_flag and date_str in _skip_dates:
            logger.info(f"{_skip_label} day {date_str} -- skipping")
            return None

    # Econ filter
    econ_skip_entries = _cfg.ENABLE_ECON_FILTER and date_str in ECON_DATES

    # Determine qty (Kelly zone sizing)
    if _cfg.ENABLE_KELLY_SIZING and vix_level is not None:
        trade_qty = QTY  # default
        for lo, hi, qty in _cfg.KELLY_ZONE_QTY_MEIC:
            if lo <= vix_level < hi:
                trade_qty = qty
                break
        logger.info(f"Kelly sizing: VIX={vix_level:.2f} → qty={trade_qty}")
    else:
        trade_qty = QTY

    if not all_strikes:
        logger.warning("No strikes found -- skipping day.")
        return None

    clear_day_cache()
    prior_close = seed_closes[-1] if seed_closes else None

    # -- ORB (Opening Range Breakout) computation: 9:30-10:00 high/low from 1-min bars --
    orb_high = None
    orb_low = None
    if _cfg.ENABLE_ORB_FILTER:
        orb_highs = []
        orb_lows = []
        for ci in range(len(df_today)):
            t = datetime.fromisoformat(str(df_today["timestamp"].iloc[ci])[:19].replace('Z', '')).time()
            if time(9, 30) <= t < time(10, 0):
                orb_highs.append(float(df_today["high"].iloc[ci]))
                orb_lows.append(float(df_today["low"].iloc[ci]))
        if orb_highs:
            orb_high = max(orb_highs)
            orb_low = min(orb_lows)
            logger.info(f"ORB computed: high={orb_high:.2f} low={orb_low:.2f} (from {len(orb_highs)} bars)")
        else:
            logger.warning(f"ORB filter enabled but no 9:30-10:00 bars found -- disabling for today")

    # -- Prior-day candle direction (T-1 open/close) --
    prior_day_bullish = None
    if _cfg.ENABLE_PRIOR_DAY_DIRECTION_FILTER:
        if prev_path.exists():
            try:
                df_prev_full = pd.read_parquet(prev_path)
                if not df_prev_full.empty:
                    prev_open = float(df_prev_full["open"].iloc[0])
                    prev_close_price = float(df_prev_full["close"].iloc[-1])
                    prior_day_bullish = prev_close_price > prev_open
                    logger.info(f"Prior day {prev_str}: open={prev_open:.2f} close={prev_close_price:.2f} → {'bullish' if prior_day_bullish else 'bearish'}")
            except Exception as e:
                logger.warning(f"Could not load prior-day candle: {e}")
        if prior_day_bullish is not None and not prior_day_bullish:
            logger.info(f"Prior day bearish -- skipping day (prior-day direction filter)")
            return None

    # L4: Morning range gate — skip if morning SPX range is too large
    if _cfg.ENABLE_MORNING_RANGE_GATE:
        morning_closes = []
        for ci, c in enumerate(closes):
            t = datetime.fromisoformat(times[ci].replace('Z', '')).time()
            if t <= time(14, 0):
                morning_closes.append(c)
        if morning_closes and len(morning_closes) > 10:
            morning_high = max(morning_closes)
            morning_low = min(morning_closes)
            morning_range_pct = (morning_high - morning_low) / max(morning_low, 0.01) * 100
            if morning_range_pct > _cfg.MORNING_RANGE_MAX_PCT:
                logger.info(f"Morning range gate: {morning_range_pct:.2f}% > {_cfg.MORNING_RANGE_MAX_PCT}% -- skipping day")
                return None

    # -- H2-KER-1: Kaufman Efficiency Ratio direction signal --
    if _cfg.ENABLE_ER_DIRECTION:
        # Build 5-min bars from 12:00-14:00 using 1-min close data
        five_min_closes = []
        bucket_closes = []
        for ci in range(len(df_today)):
            t = datetime.fromisoformat(str(df_today["timestamp"].iloc[ci])[:19].replace('Z', '')).time()
            if time(12, 0) <= t < time(14, 0):
                bucket_closes.append(float(df_today["close"].iloc[ci]))
                # Every 5 bars = one 5-min bar (use last close in bucket)
                if len(bucket_closes) == 5:
                    five_min_closes.append(bucket_closes[-1])
                    bucket_closes = []
        # Flush remaining partial bucket
        if bucket_closes:
            five_min_closes.append(bucket_closes[-1])

        n_bars = len(five_min_closes)
        if n_bars >= 2:
            lookback = min(_cfg.ER_LOOKBACK, n_bars)
            window = five_min_closes[-lookback:]
            net_change = abs(window[-1] - window[0])
            sum_abs_changes = sum(abs(window[j] - window[j-1]) for j in range(1, len(window)))
            er = net_change / sum_abs_changes if sum_abs_changes > 0 else 0.0
            trending_down = window[-1] < window[0]
            logger.info(f"Kaufman ER: {er:.3f} (n={len(window)} 5-min bars) | direction={'DOWN' if trending_down else 'UP'}")
            if er > _cfg.ER_TREND_THRESHOLD and trending_down:
                logger.info(f"ER direction filter: ER={er:.3f} > {_cfg.ER_TREND_THRESHOLD} AND trending DOWN -- skipping day")
                return None
        else:
            logger.warning(f"ER direction: not enough 5-min bars ({n_bars}) -- skipping filter")

    # -- H2-ORB-2: ORB breakout TIME filter --
    if _cfg.ENABLE_ORB_BREAKOUT_TIME_FILTER:
        # We need ORB high/low (9:30-10:00). Compute if not already done.
        _orb_h = orb_high
        _orb_l = orb_low
        if _orb_h is None or _orb_l is None:
            _orb_highs = []
            _orb_lows = []
            for ci in range(len(df_today)):
                t = datetime.fromisoformat(str(df_today["timestamp"].iloc[ci])[:19].replace('Z', '')).time()
                if time(9, 30) <= t < time(10, 0):
                    _orb_highs.append(float(df_today["high"].iloc[ci]))
                    _orb_lows.append(float(df_today["low"].iloc[ci]))
            if _orb_highs:
                _orb_h = max(_orb_highs)
                _orb_l = min(_orb_lows)

        if _orb_h is not None and _orb_l is not None:
            # Scan 1-min bars from 10:00 onward to find first breakout time
            breakout_time = None
            for ci in range(len(df_today)):
                t = datetime.fromisoformat(str(df_today["timestamp"].iloc[ci])[:19].replace('Z', '')).time()
                if t < time(10, 0):
                    continue
                bar_high = float(df_today["high"].iloc[ci])
                bar_low = float(df_today["low"].iloc[ci])
                if bar_high > _orb_h or bar_low < _orb_l:
                    breakout_time = t
                    break

            if breakout_time is not None:
                logger.info(f"ORB breakout time: {breakout_time.strftime('%H:%M')} (ORB=[{_orb_l:.2f}, {_orb_h:.2f}])")
                cutoff = time(_cfg.ORB_LATE_BREAKOUT_HOUR, 0)
                if breakout_time < cutoff:
                    logger.info(f"ORB breakout time filter: breakout at {breakout_time.strftime('%H:%M')} < {cutoff.strftime('%H:%M')} -- skipping day (trending)")
                    return None
            else:
                logger.info(f"ORB breakout time: NEVER (range-bound day, safe for MEIC)")
        else:
            logger.warning(f"ORB breakout time filter: no ORB data available -- skipping filter")

    return {
        "date_str": date_str,
        "closes": closes,
        "times": times,
        "all_closes": all_closes,
        "seed_len": seed_len,
        "all_strikes": all_strikes,
        "vix_level": vix_level,
        "trade_qty": trade_qty,
        "prior_close": prior_close,
        "day_open": day_open,
        "expiry": expiry,
        "econ_skip_entries": econ_skip_entries,
        "orb_high": orb_high,
        "orb_low": orb_low,
    }


async def _simulate_day(
    session,
    day_data: dict,
    daily_sl,
    baseline_mode=None,
    ema_fast: int | None = None,
    ema_slow: int | None = None,
    spread_width: float | None = None,
    min_credit: float | None = None,
    max_credit: float | None = None,
    min_otm_distance: float | None = None,
    entry_start: time | None = None,
    entry_end: time | None = None,
    entry_interval: int | None = None,
    daily_tp: float | None = "USE_GLOBAL",
    per_pos_sl: float | None = "USE_GLOBAL",
) -> tuple:
    """Run intraday simulation for one day."""
    if ema_fast is None:
        ema_fast = EMA_FAST
    if ema_slow is None:
        ema_slow = EMA_SLOW
    if spread_width is None:
        spread_width = WIDTH
    if daily_tp == "USE_GLOBAL":
        daily_tp = DAILY_TP

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
    orb_high          = day_data.get("orb_high")
    orb_low           = day_data.get("orb_low")

    day_trades_log    = []
    active_positions  = []
    stopped_today     = False
    daily_trades      = 0
    per_pos_sl_count  = 0
    realized_day_pnl  = 0.0
    current_day_pnl   = 0.0
    peak_day_pnl      = 0.0
    offset            = day_data["seed_len"]
    # L2: Canary state
    canary_active     = _cfg.ENABLE_CANARY_ENTRY  # True until canary resolves
    canary_resolved   = False   # set True once canary check is done
    canary_passed     = True    # set False if canary loses → stop day
    canary_bars_seen  = 0       # MTM bars since canary entry
    # Skip reason counters
    skip_orb_count       = 0
    skip_momentum_count  = 0
    skip_mtm_gate_count  = 0
    skip_ema_vix_count   = 0

    for i in range(len(closes)):
        dt         = datetime.fromisoformat(times[i].replace('Z', ''))
        curr_time  = dt.time()
        curr_price = closes[i]
        bar_time   = curr_time.strftime("%H:%M:%S")
        bar_label  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {bar_time}"
        ai         = offset + i

        is_eod        = (curr_time == market_close_time(date_str))
        is_sample_bar = (curr_time.minute % PNL_SAMPLE_INTERVAL == 0)
        should_mtm    = is_sample_bar or is_eod

        # -- Mark-to-market --
        if active_positions and should_mtm:
            put_legs = list(set(
                s for p in active_positions if p["option_type"] == "PUT"
                for s in [p["short_strike"], p["long_strike"]]
            ))
            call_legs = list(set(
                s for p in active_positions if p["option_type"] == "CALL"
                for s in [p["short_strike"], p["long_strike"]]
            ))
            put_quotes, call_quotes = await asyncio.gather(
                fetch_quotes_for_strikes_cached(session, date_str, expiry, "P", put_legs, bar_time),
                fetch_quotes_for_strikes_cached(session, date_str, expiry, "C", call_legs, bar_time),
            )
            live = {"P": put_quotes, "C": call_quotes}

            current_day_pnl = 0.0
            for pos in active_positions:
                right = "P" if pos["option_type"] == "PUT" else "C"
                short_q = live[right].get(pos["short_strike"])
                long_q = live[right].get(pos["long_strike"])
                if short_q and long_q:
                    pos["last_short_ask"] = short_q["ask"]
                    pos["last_long_bid"] = long_q["bid"]
                else:
                    pos["stale_bars"] += 1
                close_cost = pos["last_short_ask"] - pos["last_long_bid"]
                pos["pnl_earned"] = (pos["credit_received"] - close_cost) * pos["qty"] * 100.0
                current_day_pnl += pos["pnl_earned"]

            for pos in active_positions:
                pos["peak_pnl"] = max(pos.get("peak_pnl", 0.0), pos["pnl_earned"])
            peak_day_pnl = max(peak_day_pnl, current_day_pnl)

            # -- Per-Position Fixed Stop Loss (asymmetric) --
            _per_pos_sl = (PER_POS_SL_AMOUNT if ENABLE_PER_POS_SL else None) if per_pos_sl == "USE_GLOBAL" else per_pos_sl
            _afternoon_sl_active = _cfg.ENABLE_AFTERNOON_SL_TIGHTEN and curr_time >= _cfg.AFTERNOON_SL_CUTOFF

            if _per_pos_sl is not None and active_positions:
                if _cfg.ENABLE_ASYMMETRIC_SL:
                    pos_sl_to_close = []
                    for p in active_positions:
                        sl = _cfg.ASYM_PUT_SL_AMOUNT if p["option_type"] == "PUT" else _cfg.ASYM_CALL_SL_AMOUNT
                        if _afternoon_sl_active:
                            sl = max(sl, _cfg.AFTERNOON_SL_AMOUNT)
                        if p["pnl_earned"] <= sl:
                            pos_sl_to_close.append(p)
                else:
                    pos_sl_to_close = []
                    for p in active_positions:
                        sl = _per_pos_sl
                        if _afternoon_sl_active:
                            sl = max(sl, _cfg.AFTERNOON_SL_AMOUNT)
                        if p["pnl_earned"] <= sl:
                            pos_sl_to_close.append(p)

                for pos in pos_sl_to_close:
                    active_positions.remove(pos)
                    commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                    pos["pnl_earned"] -= commission_per_pos
                    pos.update({
                        "outcome": "PER_POS_SL",
                        "profit_price": curr_price,
                        "win": 1 if pos["pnl_earned"] > 0 else 0,
                        "loss": 1 if pos["pnl_earned"] <= 0 else 0,
                        "close_date": date_str, "close_time": bar_time,
                        "profit_date_time": f"{date_str} {bar_time}",
                    })
                    day_trades_log.append(pos)
                    logger.debug(f"[{bar_label}] PER_POS_SL: {pos['option_type']} {pos['short_strike']}/{pos['long_strike']} | P&L=${pos['pnl_earned']:.2f}")

                if pos_sl_to_close:
                    per_pos_sl_count += len(pos_sl_to_close)
                    realized_day_pnl += sum(p["pnl_earned"] for p in pos_sl_to_close)
                    current_day_pnl = sum(p["pnl_earned"] for p in active_positions)

            # L2: Canary check — after N MTM bars, check if canary entry is losing
            if canary_active and not canary_resolved and daily_trades >= 1:
                canary_bars_seen += 1
                if canary_bars_seen >= _cfg.CANARY_CHECK_BARS:
                    canary_resolved = True
                    if current_day_pnl < 0:
                        canary_passed = False
                        logger.info(f"[{bar_label}] Canary FAILED: P&L=${current_day_pnl:.2f} < 0 after {canary_bars_seen} bars -- stopping entries for day")
                    else:
                        canary_passed = True
                        logger.debug(f"[{bar_label}] Canary PASSED: P&L=${current_day_pnl:.2f} >= 0")

            dd = current_day_pnl - peak_day_pnl
            logger.debug(f"[{bar_label}] MTM: {len(active_positions)} open | day P&L=${current_day_pnl:.2f} | peak=${peak_day_pnl:.2f}")

        # -- Settlement --
        outcome = ""
        if active_positions:
            if daily_tp is not None and current_day_pnl >= daily_tp:
                outcome = "PROFIT_TARGET"
            elif _cfg.DAILY_LOSS_CAP is not None and (realized_day_pnl + current_day_pnl) <= _cfg.DAILY_LOSS_CAP:
                outcome = "DAILY_LOSS_CAP"
            elif daily_sl is not None and (realized_day_pnl + current_day_pnl) <= daily_sl:
                outcome = "STOP_LOSS"
            elif is_eod:
                outcome = "EXPIRATION"

        if outcome:
            logger.info(f"[{bar_label}] CLOSING all -> {outcome} | P&L=${current_day_pnl:.2f}")
            for pos in active_positions:
                commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
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
            if outcome != "EXPIRATION":
                stopped_today = True

        # -- Entry Logic --
        _entry_start    = entry_start    if entry_start    is not None else ENTRY_START
        _entry_end      = entry_end      if entry_end      is not None else ENTRY_END
        _entry_interval = entry_interval if entry_interval is not None else ENTRY_INTERVAL
        in_window   = _entry_start <= curr_time <= _entry_end
        on_interval = (dt.minute % _entry_interval == 0)
        can_enter   = in_window and on_interval and not stopped_today and daily_trades < MAX_TRADES_DAY and not econ_skip_entries

        # L2: Canary — block entries if canary failed
        if canary_active and canary_resolved and not canary_passed:
            can_enter = False

        if not can_enter:
            continue

        # -- Momentum filter: skip if SPX dropped fast (PUT spread into falling market) --
        if _cfg.ENABLE_MOMENTUM_FILTER and i >= _cfg.MOMENTUM_FILTER_BARS:
            lookback = min(i, _cfg.MOMENTUM_FILTER_BARS)
            spx_move = closes[i] - closes[i - lookback]
            if spx_move <= _cfg.MOMENTUM_FILTER_PTS:
                logger.debug(f"[{bar_label}] Momentum filter: SPX moved {spx_move:.1f}pts in {lookback}bars <= {_cfg.MOMENTUM_FILTER_PTS} -- skipping.")
                skip_momentum_count += 1
                continue

        # -- ORB containment filter: skip if SPX broke out of opening range --
        if _cfg.ENABLE_ORB_FILTER and orb_high is not None and orb_low is not None:
            if curr_price > orb_high or curr_price < orb_low:
                logger.debug(f"[{bar_label}] ORB filter: SPX={curr_price:.2f} outside ORB [{orb_low:.2f}, {orb_high:.2f}] -- skipping.")
                skip_orb_count += 1
                continue

        # -- Open MTM gate --
        if _cfg.ENABLE_OPEN_MTM_GATE and active_positions and current_day_pnl <= _cfg.OPEN_MTM_GATE_THRESHOLD:
            logger.debug(f"[{bar_label}] Open MTM gate: ${current_day_pnl:.0f} <= ${_cfg.OPEN_MTM_GATE_THRESHOLD:.0f} -- skipping.")
            skip_mtm_gate_count += 1
            continue

        # Compute EMA for direction (used when not both_sides)
        e13 = calculate_ema(all_closes[max(0, ai - ema_fast):ai + 1], ema_fast)
        e48 = calculate_ema(all_closes[max(0, ai - ema_slow):ai + 1], ema_slow)

        # -- R6: EMA direction gate for VIX 20-25 zone --
        if _cfg.ENABLE_EMA_VIX_GATE and vix_level is not None:
            if _cfg.EMA_VIX_GATE_LO <= vix_level < _cfg.EMA_VIX_GATE_HI:
                ema_bullish = e13 > e48
                if not ema_bullish:
                    logger.debug(f"[{bar_label}] EMA VIX gate: VIX={vix_level:.1f} in [{_cfg.EMA_VIX_GATE_LO},{_cfg.EMA_VIX_GATE_HI}), EMA bearish -- skipping.")
                    skip_ema_vix_count += 1
                    continue

        # Determine which sides to enter
        _mode = baseline_mode if baseline_mode is not None else _cfg.DIRECTION_MODE
        if _mode == "both_sides":
            sides_to_enter = [("PUT", "P"), ("CALL", "C")]
        elif _mode == "always_put":
            sides_to_enter = [("PUT", "P")]
        elif _mode == "always_call":
            sides_to_enter = [("CALL", "C")]
        else:
            # EMA direction
            direction = 1 if e13 > e48 else -1
            opt_type = "PUT" if direction == 1 else "CALL"
            right = "P" if direction == 1 else "C"
            sides_to_enter = [(opt_type, right)]

        elapsed = (dt.hour - 9) * 60 + (dt.minute - 45)

        for opt_type, right in sides_to_enter:
            # Fetch strike candidates
            otm_min, otm_max = 10, 200 + int(spread_width)
            if opt_type == "PUT":
                lo, hi = curr_price - otm_max, curr_price - otm_min
            else:
                lo, hi = curr_price + otm_min, curr_price + otm_max
            candidate_strikes = [s for s in all_strikes if lo <= s <= hi]

            chain = await fetch_quotes_for_strikes_cached(session, date_str, expiry, right, candidate_strikes, bar_time)

            credit_threshold = min_credit if min_credit is not None else _cfg.MIN_NET_CREDIT
            # R10 ALT: Late credit floor
            if _cfg.ENABLE_LATE_CREDIT_FLOOR and curr_time >= _cfg.LATE_CREDIT_CUTOFF:
                credit_threshold = max(credit_threshold, _cfg.LATE_CREDIT_MIN)
            credit_cap = max_credit if max_credit is not None else _cfg.MAX_NET_CREDIT

            # -- R9: Adaptive OTM by entry time --
            otm_floor = min_otm_distance if min_otm_distance is not None else _cfg.MIN_OTM_DISTANCE
            if _cfg.ENABLE_ADAPTIVE_OTM_TIME:
                for rule_time, rule_otm in reversed(_cfg.OTM_TIME_RULES):
                    if curr_time >= rule_time:
                        otm_floor = max(otm_floor or 0, rule_otm)
                        break

            short_strike = long_strike = short_q = long_q = credit = None
            for off in range(200, 0, -5):
                if otm_floor is not None and off < otm_floor:
                    break
                if opt_type == "PUT":
                    s = int(round((curr_price - off) / 5.0) * 5)
                    l = s - int(spread_width)
                else:
                    s = int(round((curr_price + off) / 5.0) * 5)
                    l = s + int(spread_width)
                sq = chain.get(s)
                lq = chain.get(l)
                if not sq or not lq:
                    continue
                c = sq["bid"] - lq["ask"]
                if c >= credit_threshold:
                    if credit_cap is not None and c > credit_cap:
                        break
                    short_strike, long_strike, short_q, long_q, credit = s, l, sq, lq, c
                    break

            if short_strike is None:
                logger.debug(f"[{bar_label}] No {opt_type} spread with credit >= {credit_threshold} -- skipping.")
                continue

            # Max OTM distance cap
            strike_dist = abs(curr_price - short_strike)
            if MAX_OTM_DISTANCE is not None and strike_dist > MAX_OTM_DISTANCE:
                continue

            # -- Determine entry qty with all sizing rules --
            entry_qty = trade_qty

            # L2: Canary entry — first entry uses reduced qty
            if canary_active and not canary_resolved and daily_trades == 0:
                entry_qty = _cfg.CANARY_QTY

            # R7: Time-decay qty reduction
            if _cfg.ENABLE_LATE_QTY_REDUCTION and curr_time >= _cfg.LATE_QTY_CUTOFF:
                entry_qty = min(entry_qty, _cfg.LATE_QTY)

            # R8: Economic event day upsizing
            if _cfg.ENABLE_ECON_UPSIZING:
                _econ_dates_map = {
                    "cpi": _CPI_DATES,
                    "nfp": _NFP_DATES,
                }
                for evt in _cfg.ECON_UPSIZE_EVENTS:
                    if date_str in _econ_dates_map.get(evt, set()):
                        entry_qty = max(entry_qty, _cfg.ECON_UPSIZE_QTY)
                        break

            # R10: Winning streak acceleration
            if _cfg.ENABLE_STREAK_ACCEL and len(day_trades_log) >= _cfg.STREAK_ACCEL_COUNT:
                recent = day_trades_log[-_cfg.STREAK_ACCEL_COUNT:]
                if all(t["pnl_earned"] > 0 for t in recent):
                    entry_qty += _cfg.STREAK_ACCEL_QTY_BONUS

            strike_dist = round(abs(short_strike - curr_price))
            logger.info(f"[{bar_label}] {opt_type.lower()} spread spot={curr_price:.2f} | {short_strike}/{long_strike} credit=${round(credit*entry_qty*100)} | dist={strike_dist}pts qty={entry_qty}")
            active_positions.append({
                "entry_date": date_str, "entry_time": bar_time,
                "option_type": opt_type,
                "short_strike": short_strike, "long_strike": long_strike,
                "width": spread_width, "spread_width": spread_width, "credit_received": credit,
                "entry_short_bid": short_q["bid"], "entry_short_ask": short_q["ask"], "entry_short_mid": short_q["mid"],
                "entry_long_bid": long_q["bid"], "entry_long_ask": long_q["ask"], "entry_long_mid": long_q["mid"],
                "profit_target": DAILY_TP, "stop_loss": daily_sl if daily_sl is not None else "none",
                "ema13": round(e13, 2), "ema48": round(e48, 2),
                "qty": entry_qty, "vix_level": round(vix_level, 2) if vix_level is not None else "",
                "strike_distance": strike_dist,
                "opening_put_credit": "", "opening_call_credit": "", "pc_skew_ratio": "",
                "pnl_earned": 0.0, "peak_pnl": 0.0,
                "last_short_ask": short_q["ask"], "last_long_bid": long_q["bid"],
                "stale_bars": 0,
                "outcome": "", "profit_price": None,
                "win": 0, "loss": 0, "close_date": "", "close_time": "", "profit_date_time": "",
            })
            daily_trades += 1

    day_pnl = sum(t["pnl_earned"] for t in day_trades_log)
    # Log skip reason counters if any fired
    skip_parts = []
    if skip_orb_count:
        skip_parts.append(f"ORB={skip_orb_count}")
    if skip_momentum_count:
        skip_parts.append(f"momentum={skip_momentum_count}")
    if skip_mtm_gate_count:
        skip_parts.append(f"MTM_gate={skip_mtm_gate_count}")
    if skip_ema_vix_count:
        skip_parts.append(f"EMA_VIX={skip_ema_vix_count}")
    skip_summary = f" | skips: {', '.join(skip_parts)}" if skip_parts else ""
    logger.info(f"Day complete: {len(day_trades_log)} trades | day P&L=${day_pnl:.2f}{skip_summary}")
    return day_trades_log, day_pnl


def _get_effective_sl(day_data: dict, date_str: str) -> float | None:
    """Return the correct stop-loss for a day."""
    effective_sl = DAILY_SL

    if ENABLE_EOM_SL and date_str in _EOM_DATES:
        candidate = EOM_SL_AMOUNT
        if effective_sl is None:
            effective_sl = candidate
        else:
            effective_sl = max(effective_sl, candidate)

    return effective_sl


async def process_day(session, date_str: str) -> tuple:
    day_data = await _fetch_day_data(session, date_str)
    if day_data is None:
        return [], 0.0

    effective_sl = _get_effective_sl(day_data, date_str)

    # Dynamic width: widen spread when VIX is elevated
    sim_width = _cfg.WIDTH
    if _cfg.ENABLE_DYNAMIC_WIDTH and day_data.get("vix_level") is not None:
        if day_data["vix_level"] >= _cfg.DYNAMIC_WIDTH_VIX_THRESHOLD:
            sim_width = _cfg.DYNAMIC_WIDTH_HIGH_VIX
            logger.info(f"Dynamic width: VIX={day_data['vix_level']:.2f} >= {_cfg.DYNAMIC_WIDTH_VIX_THRESHOLD} → width={sim_width}")

    trades, day_pnl = await _simulate_day(
        session, day_data, effective_sl,
        min_otm_distance=_cfg.MIN_OTM_DISTANCE,
        max_credit=_cfg.MAX_NET_CREDIT,
        entry_start=_cfg.ENTRY_START,
        entry_end=_cfg.ENTRY_END,
        entry_interval=_cfg.ENTRY_INTERVAL,
        spread_width=sim_width,
        min_credit=_cfg.MIN_NET_CREDIT,
        per_pos_sl=_cfg.PER_POS_SL_AMOUNT if _cfg.ENABLE_PER_POS_SL else None,
        daily_tp=_cfg.DAILY_TP,
    )
    for pos in trades:
        append_trade(pos)
    return trades, day_pnl


# ---------------------------------------------
#  METRICS
# ---------------------------------------------
def compute_metrics(all_trades: list) -> dict:
    pnls = [t["pnl_earned"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    total_pnl = sum(pnls)
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    day_pnls_d: dict = {}
    for t in all_trades:
        day_pnls_d.setdefault(t["entry_date"], 0.0)
        day_pnls_d[t["entry_date"]] += t["pnl_earned"]
    daily_vals = list(day_pnls_d.values())
    n_days = len(daily_vals)
    rf_daily = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    mean_d = sum(daily_vals) / n_days if n_days else 0.0
    var_d = sum((p - mean_d) ** 2 for p in daily_vals) / n_days if n_days else 0.0
    std_d = math.sqrt(var_d)
    sharpe = ((mean_d - rf_daily) / std_d * math.sqrt(252)) if std_d else 0.0
    down_d = [p for p in daily_vals if p < mean_d]
    down_var_d = sum((p - mean_d) ** 2 for p in down_d) / len(down_d) if down_d else 0.0
    sortino = ((mean_d - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0.0

    return {
        "num_trades": n, "num_wins": len(wins), "num_losses": len(losses),
        "total_pnl": total_pnl, "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": profit_factor, "max_drawdown": max_dd,
        "sharpe": sharpe, "sortino": sortino,
    }
