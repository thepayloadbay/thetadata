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

# ---------------------------------------------
#  MARKET CALENDAR
# ---------------------------------------------
# Full holidays: market closed all day
MARKET_HOLIDAYS = {
    "20250101","20250120","20250217","20250418","20250526","20250704",
    "20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260703",
    "20260907","20261126","20261225",
}

# FOMC announcement days -- kept as a reference set for the calendar event sweep.
# SWEEP RESULT (2022-2026): FOMC days are NET PROFITABLE -- skipping costs $23,512 over 34 days.
# Previously hard-skipped assuming chaos; data shows this strategy handles them fine.
# No longer added to MARKET_HOLIDAYS.
FOMC_DATES = {
    # 2022
    "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
    # 2023
    "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
    # 2024
    "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
    # 2025
    "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
    # 2026
    "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
}

# Triple witching days -- kept as a reference set for the calendar event sweep.
# SWEEP RESULT (2022-2026): TW days are NET PROFITABLE -- skipping costs $12,212 over 17 days.
# Previously hard-skipped assuming pin risk/chaos; data shows this strategy trades them fine.
# No longer added to MARKET_HOLIDAYS.
TRIPLE_WITCHING_DATES = {
    # 2022
    "20220318","20220617","20220916","20221216",
    # 2023
    "20230317","20230616","20230915","20231215",
    # 2024
    "20240315","20240621","20240920","20241220",
    # 2025
    "20250321","20250620","20250919","20251219",
    # 2026
    "20260320","20260619","20260918","20261218",
}

# Early closes: market closes at 13:00 ET
EARLY_CLOSE_DAYS = {
    "20241129","20241224",
    "20251128","20251224",
    "20261127","20261224",
}

def market_close_time(date_str: str) -> time:
    """Return the market close time for a given date."""
    if date_str in EARLY_CLOSE_DAYS:
        return time(12, 59)  # last bar at 12:59 on early close days
    return time(15, 59)


# ---------------------------------------------
#  GLOBAL CONFIG
# ---------------------------------------------
import pathlib as _pathlib
PROJECT_ROOT     = str(_pathlib.Path(__file__).resolve().parent.parent)

PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"
MCP_URL          = "http://127.0.0.1:25503/mcp/sse"
USE_LOCAL_DATA   = True    # True -> read local parquet files; False -> live ThetaData MCP
DATA_DIR         = os.path.join(PROJECT_ROOT, "data")   # root of local parquet cache
QUOTE_DISK_CACHE = os.path.join(PROJECT_ROOT, "data", "quote_disk_cache.parquet")

# Unique timestamp stamped on every output file so runs never overwrite each other.
from datetime import datetime as _dt
_RUN_TS = _dt.now().strftime("%Y%m%d_%H%M%S")

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def _out(name: str) -> str:
    """Insert _RUN_TS before the extension and place in LOGS_DIR."""
    base, ext = name.rsplit(".", 1)
    return os.path.join(LOGS_DIR, f"{base}_{_RUN_TS}.{ext}")

SAVE_FILE        = _out("meft_v35_bidask_log.csv")
LOG_FILE         = _out("meft_v35_bidask.log")
RUN_HISTORY_FILE = os.path.join(LOGS_DIR, "run_history.json")   # persistent across runs

# +---------------------------------------------------------------------+
# |  STRATEGY: Multi-Entry Directional Spreads (MEDS)                   |
# |  Signal   : Prior-day VIX change -> PUT (VIX fell) / CALL (VIX rose)|
# |  Execution: SPXW 0DTE credit spreads, entry every 20min 9:35-12:45 |
# +---------------------------------------------------------------------+
# |  CONFIRMED BASELINE  (2022-01-03 -> 2026-03-25)  run 2026-03-27     |
# |  Total P&L    : $607,034   Win rate : 93.1%                         |
# |  Max drawdown : -$9,922    Sharpe   : 12.35   Calmar  : 61.2        |
# |  Trades       : 7,014      Days     : 957 / 1103                    |
# |  Key settings : WIDTH=20, QTY=2, MIN_CREDIT=0.55, MIN_OTM=30        |
# |                 DIRECTION=vix_change, ENTRY 9:35-12:45 every 20min  |
# |                 DYN_SL: VIX<13 | (13-13.5) | (25-30) -> SL=-$800    |
# |                 MTM interval: 1min on danger days, 5min otherwise    |
# |                 FOMC/TW/CPI/NFP all traded (filters removed)         |
# |                 DAILY_TP=None (TP sweep: removing $750 cap +$140k)   |
# +---------------------------------------------------------------------+
WIDTH          = 20.0
QTY            = 2
MIN_NET_CREDIT    = 0.55
MAX_NET_CREDIT    = None   # skip entry if best qualifying credit exceeds this; None = no cap
                           # SWEEP RESULT: None wins. High-credit days contribute ~$73k total.
                           # Capping at 0.60-1.00 always hurt P&L. 30pt OTM floor already
                           # handles blowup risk -- adding a credit cap is redundant.
MIN_OTM_DISTANCE  = 30.0   # minimum OTM distance (pts) for short strike at entry; None = no filter
                           # SWEEP RESULT: 30pts optimal (P&L $320k, Calmar 3.64).
                           # None/10/15/20/25 all had net-negative or near-zero P&L at those levels.
                           # 30pt floor transformed baseline from ~$62k -> $320k by eliminating
                           # close-in, low-OTM entries on dangerous days. 35-50 skip too many trades.
PUT_ONLY       = False  # legacy flag -- use DIRECTION_MODE instead
DIRECTION_MODE = "ema"  # "vix_change" | "always_put" | "always_call" | "ema"
# vix_change: VIX falling -> PUT spread (bullish); VIX rising -> CALL spread (bearish)
COMMISSION     = 0.50  # per contract per leg (TradeStation)

# -- Benchmark / risk-free rate --
ANNUAL_RISK_FREE_RATE = 0.05   # 5% -- used to adjust Sharpe/Sortino (T-bill rate)
ACCOUNT_SIZE          = 50_000.0  # notional account size ($) -- sets rf dollar cost per day

# -- Per-Position Trailing Stop (baseline) --
# Activates per position once it has profited >= POS_TRAIL_ACTIVATION% of credit.
# Closes that position if P&L then pulls back >= POS_TRAIL_PULLBACK% of credit.
# Set POS_TRAIL_ACTIVATION = None to disable.
POS_TRAIL_ACTIVATION = None   # % of credit to activate trail; None = disabled
                              # SWEEP RESULT: ALL combos worse than no-trail baseline ($320k).
                              # Best tested was Act=90, Pull=20 -> ~$120k. With 93%+ expiry win rate,
                              # trailing stops cut winning premium unnecessarily. Keep None.
POS_TRAIL_PULLBACK   = 5.0    # % of credit pullback to trigger close (only used when ACTIVATION set)
NET_DELTA_LIMIT  = 0.30   # max absolute net portfolio delta (0-1 scale per spread unit)
ENABLE_DELTA_LIMIT = False  # set True to enforce net delta limit on entries

# -- IV Rank / VIX Proxy Filter --
ENABLE_IVR_FILTER = False   # skip day if VIX below threshold (IV not elevated enough to sell premium)
MIN_VIX_LEVEL     = 16.0    # minimum VIX level to trade (proxy for IV rank)

# -- Economic Calendar Filter --
ENABLE_ECON_FILTER = False  # SWEEP RESULT: CPI days cost $29,790 and NFP days cost $36,214
                            # when skipped -- both are net-profitable days for this strategy.
                            # Dynamic SL already handles the risk on volatile release days.
                            # Previously True (blocked new entries on CPI/NFP); data shows wrong call.

# -- Calendar Event Hard Skip --
# Completely skips trading on the selected calendar event days (hard skip, like holidays).
# SWEEP RESULT (2022-2026): PCE + End-of-Month is the optimal combo --
#   skip 70 days, give up $26k P&L but cut max drawdown 35% (-$98k -> -$64k),
#   Calmar 9.34 -> 13.95, Sharpe 9.17 -> 11.03.
# Set ENABLE_CALENDAR_FILTER = False to disable and trade all days.
ENABLE_CALENDAR_FILTER = False  # PCE+EOM sweep showed dynamic SL already controls drawdown;
                                # skipping those 70 days costs $35k P&L for marginal benefit.
CALENDAR_FILTER_EVENTS = {"pce", "end_of_month"}   # keys from _build_calendar_event_dates()
ECON_DATES = {
    # CPI 2025
    "20250115","20250212","20250312","20250410",
    "20250513","20250611","20250715","20250812",
    "20250910","20251015","20251112","20251210",
    # CPI 2026
    "20260114","20260211","20260311","20260408",
    "20260513","20260610","20260714","20260811",
    "20260909","20261014","20261111","20261209",
    # NFP (Non-Farm Payroll) 2025
    "20250107","20250207","20250307","20250404",
    "20250502","20250606","20250711","20250801",
    "20250905","20251003","20251107","20251205",
    # NFP 2026
    "20260109","20260206","20260306","20260403",
    "20260508","20260605","20260710","20260807",
    "20260904","20261002","20261106","20261204",
}

# -- Pressure Filter --
# Stops from opening more positions if any active short strike is within PRESSURE_DISTANCE_THRESHOLD pts of spot.
# VIX range gate: only activates on days where VIX_MIN <= VIX < VIX_MAX (None = no bound).
# Motivation: 10 of 15 worst loss days are in VIX 15-20 -- morning-drift / afternoon-reversal days where
# the market trends with positions during the entry window then reverses after 12:45 hitting multiple strikes.
# Threshold raised to 45 (from 27) because losses cluster in entries with dist 30-45 at entry;
# 27pt threshold only fires 3pts below MIN_OTM_DISTANCE=30, catching almost nothing.
ENABLE_PRESSURE_FILTER          = False
PRESSURE_DISTANCE_THRESHOLD     = 45.0    # block new entries if any short strike is within X pts of spot
PRESSURE_FILTER_VIX_MIN: float | None = 15.0   # only active when VIX >= this (None = no lower bound)
PRESSURE_FILTER_VIX_MAX: float | None = 20.0   # only active when VIX <  this (None = no upper bound)

# -- VIX-Range Entry Cap --
# Caps max daily entries when VIX is in a specific range (reversal-day danger zone).
# Big losses always come from entries #5-10 on those days; early entries are mostly winners.
# None = use global MAX_TRADES_DAY for all VIX ranges.
MAX_TRADES_DAY_VIX_LO_HI: int | None = None

# -- VIX-Range OTM Distance Floor --
# Raises the minimum OTM distance floor for entries within a specific VIX range.
# Analysis of worst 15 loss days showed failure mode #1 (late-day losses) clusters in entries
# with dist 29-39 at entry in the VIX 15-20 zone.
# None = use global MIN_OTM_DISTANCE for all VIX ranges.
ENABLE_OTM_DISTANCE_VIX_RANGE    = False   # True = apply raised floor only within VIX range below
MIN_OTM_DISTANCE_VIX_RANGE_LO    = 15.0   # lower bound (inclusive)
MIN_OTM_DISTANCE_VIX_RANGE_HI    = 20.0   # upper bound (exclusive)
MIN_OTM_DISTANCE_VIX_LO_HI: float | None = 40.0   # raised floor when filter is active

# -- Calendar Event Date Sets --
# Used by run_calendar_event_sweep() to test each event type independently.
# Computed once at import time from pandas date ranges + hard-coded release dates.
def _build_calendar_event_dates() -> "dict[str, set[str]]":
    """Return a dict mapping event_label -> set of YYYYMMDD trading-day strings."""
    all_bdays: set[str] = {
        d.strftime("%Y%m%d")
        for d in pd.bdate_range("2022-01-01", "2026-12-31")
        if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS
    }

    def _nth_friday(year: int, month: int, n: int) -> "str | None":
        days_in = pd.Timestamp(year, month, 1).days_in_month
        fridays = [
            d.strftime("%Y%m%d")
            for d in pd.date_range(f"{year}-{month:02d}-01",
                                   f"{year}-{month:02d}-{days_in}",
                                   freq="W-FRI")
        ]
        if len(fridays) >= n:
            d = fridays[n - 1]
            return d if d in all_bdays else None
        return None

    def _last_bday(year: int, month: int) -> "str | None":
        days_in = pd.Timestamp(year, month, 1).days_in_month
        for day in range(days_in, 0, -1):
            d_str = f"{year}{month:02d}{day:02d}"
            if d_str in all_bdays:
                return d_str
        return None

    TW_MONTHS = {3, 6, 9, 12}   # triple witching -- already in MARKET_HOLIDAYS
    monthly_opex: set[str] = set()
    first_weekly: set[str] = set()
    eom:          set[str] = set()
    eoq:          set[str] = set()

    for year in range(2022, 2027):
        for month in range(1, 13):
            if month not in TW_MONTHS:
                d = _nth_friday(year, month, 3)
                if d:
                    monthly_opex.add(d)
            d = _nth_friday(year, month, 1)
            if d:
                first_weekly.add(d)
            d = _last_bday(year, month)
            if d:
                eom.add(d)
                if month in TW_MONTHS:
                    eoq.add(d)

    # Full Moon dates -- snapped to nearest prior trading day if weekend/holiday
    _full_moon_raw = {
        "20220117","20220216","20220318","20220416","20220516","20220614",
        "20220713","20220812","20220910","20221009","20221108","20221208",
        "20230106","20230205","20230307","20230406","20230505","20230603",
        "20230703","20230801","20230830","20230929","20231028","20231127","20231226",
        "20240125","20240224","20240325","20240423","20240523","20240621",
        "20240721","20240819","20240917","20241017","20241115","20241215",
        "20250113","20250212","20250314","20250412","20250512","20250611",
        "20250710","20250809","20250907","20251007","20251105","20251204",
        "20260103","20260201","20260303","20260402","20260501","20260531",
        "20260630","20260729","20260828","20260926","20261026","20261124","20261224",
    }
    full_moon: set[str] = set()
    for d_str in _full_moon_raw:
        d = pd.Timestamp(d_str)
        while d.strftime("%Y%m%d") not in all_bdays:
            d -= pd.Timedelta(days=1)
        full_moon.add(d.strftime("%Y%m%d"))

    # PPI (Producer Price Index) release dates -- BLS, ~2nd week of each month
    ppi_dates = {
        "20220113","20220215","20220315","20220413","20220512","20220614",
        "20220714","20220811","20220914","20221012","20221115","20221209",
        "20230118","20230216","20230315","20230413","20230511","20230613",
        "20230713","20230811","20230913","20231011","20231115","20231213",
        "20240112","20240216","20240314","20240411","20240514","20240613",
        "20240712","20240813","20240912","20241011","20241114","20241212",
        "20250114","20250213","20250313","20250411","20250515","20250612",
        "20250715","20250814","20250911","20251009","20251113","20251211",
        "20260113","20260212","20260312","20260409","20260514","20260611",
        "20260714","20260813","20260910","20261008","20261112","20261210",
    }

    # PCE (Personal Consumption Expenditures) release dates -- BEA, ~last Fri of month
    pce_dates = {
        "20220128","20220225","20220331","20220429","20220527","20220630",
        "20220729","20220826","20220930","20221028","20221130","20221223",
        "20230127","20230224","20230331","20230428","20230526","20230630",
        "20230728","20230825","20230929","20231027","20231130","20231222",
        "20240126","20240229","20240329","20240426","20240531","20240628",
        "20240726","20240830","20240927","20241031","20241127","20241220",
        "20250131","20250228","20250328","20250430","20250530","20250627",
        "20250731","20250829","20250926","20251031","20251126","20251219",
        "20260130","20260227","20260327","20260430","20260529","20260626",
        "20260731","20260828","20260925","20261030","20261125","20261218",
    }

    # CPI -- full 2022-2026 range (ECON_DATES only has 2025-2026)
    cpi_dates = {
        "20220112","20220210","20220310","20220412","20220511","20220610",
        "20220713","20220810","20220913","20221013","20221110","20221213",
        "20230112","20230214","20230314","20230412","20230510","20230613",
        "20230712","20230810","20230913","20231012","20231114","20231212",
        "20240111","20240213","20240312","20240410","20240515","20240612",
        "20240711","20240814","20240911","20241010","20241113","20241211",
        "20250115","20250212","20250312","20250410","20250513","20250611",
        "20250715","20250812","20250910","20251015","20251112","20251210",
        "20260114","20260211","20260311","20260408","20260513","20260610",
        "20260714","20260811","20260909","20261014","20261111","20261209",
    }

    # NFP -- full 2022-2026 range
    nfp_dates = {
        "20220107","20220204","20220304","20220401","20220506","20220603",
        "20220708","20220805","20220902","20221007","20221104","20221202",
        "20230106","20230203","20230310","20230407","20230505","20230602",
        "20230707","20230804","20230901","20231006","20231103","20231208",
        "20240105","20240202","20240308","20240405","20240503","20240607",
        "20240705","20240802","20240906","20241004","20241101","20241206",
        "20250107","20250207","20250307","20250404","20250502","20250606",
        "20250711","20250801","20250905","20251003","20251107","20251205",
        "20260109","20260206","20260306","20260403","20260508","20260605",
        "20260710","20260807","20260904","20261002","20261106","20261204",
    }

    # FOMC announcement days (currently hard-skipped in MARKET_HOLIDAYS -- testing if justified)
    fomc_dates = {
        "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
        "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
        "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
        "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
        "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
    }

    # Triple Witching days (currently hard-skipped in MARKET_HOLIDAYS -- testing if justified)
    tw_dates = {
        "20220318","20220617","20220916","20221216",
        "20230317","20230616","20230915","20231215",
        "20240315","20240621","20240920","20241220",
        "20250321","20250620","20250919","20251219",
        "20260320","20260619","20260918","20261218",
    }

    _pce  = pce_dates & all_bdays
    _eom  = eom
    _opex = monthly_opex

    # Pre-triple-witching: the trading day immediately before each TW Friday
    pre_tw: set[str] = set()
    _all_bdays_sorted = sorted(all_bdays)
    for tw in tw_dates:
        # find the last bday strictly before tw
        idx = None
        for i, d in enumerate(_all_bdays_sorted):
            if d >= tw:
                if i > 0:
                    idx = i - 1
                break
        if idx is not None:
            pre_tw.add(_all_bdays_sorted[idx])

    # Post-major-holiday: first trading day after each MARKET_HOLIDAYS entry
    post_holiday: set[str] = set()
    for hol in MARKET_HOLIDAYS:
        # find first bday strictly after hol
        for d in _all_bdays_sorted:
            if d > hol:
                post_holiday.add(d)
                break

    return {
        "fomc":                fomc_dates,   # NOTE: needs MARKET_HOLIDAYS override in sweep
        "triple_witching":     tw_dates,     # NOTE: needs MARKET_HOLIDAYS override in sweep
        "cpi":                 cpi_dates   & all_bdays,
        "nfp":                 nfp_dates   & all_bdays,
        "ppi":                 ppi_dates   & all_bdays,
        "pce":                 _pce,
        "monthly_opex":        _opex,
        "end_of_month":        _eom,
        "end_of_quarter":      eoq,
        "first_weekly":        first_weekly,
        "full_moon":           full_moon,
        "pre_triple_witching": pre_tw,
        "post_major_holiday":  post_holiday,
        # -- Combinations --
        "pce+end_of_month": _pce | _eom,
        "pce+eom+opex":     _pce | _eom | _opex,
    }

# -- VIX Regime Filter --
ENABLE_VIX_REGIME  = False  # reduce contract size when VIX is elevated
HIGH_VIX_THRESHOLD = 30.0   # VIX level above which we cut size
HIGH_VIX_QTY       = 1      # reduced contracts during high-VIX regime
# Low-VIX / mid-VIX half-size: reduce qty on the two worst-performing VIX zones.
# SWEEP RESULT (2026-03-27): half-size on VIX<13 and VIX 25-30 lifts win rate
# (67.5%->76.0% and 65.0%->74.3%) and improves Sharpe (12.35->12.81), but costs
# -$3,902 in total PnL ($607k->$603k) because winning trades in those zones also
# collect half credit. MaxDD unchanged at -$9,922. Not worth it -- keep False.
ENABLE_LOW_VIX_HALF_SIZE = False
LOW_VIX_THRESHOLD        = 13.0         # reduce qty when VIX < this
MID_VIX_BAND             = (25.0, 30.0) # reduce qty when VIX is in this range
LOW_VIX_QTY              = 1            # half of baseline QTY=2

# -- Kelly Zone Sizing --
# Scale contract qty by VIX zone according to Kelly criterion.
# Zones: list of (vix_lo, vix_hi, qty) -- checked in order; first match wins.
# Overrides QTY and ENABLE_LOW_VIX_HALF_SIZE when enabled.
#
# FULL MARATHON BACKTEST RESULTS (2026-03-28):
#   Scenario               P&L         MaxDD    Sharpe  Calmar
#   Baseline (qty=2 flat) $607,034   -$9,922    12.54   61.2
#   Half-Kelly            $600,495   -$9,922    13.11   60.5  <- danger zones 2->1; net -$6.5k vs baseline
#   Conservative (max=3)  $865,667  -$14,883    13.00   58.2  <- CSV estimate only
#   Full Kelly (max=4)   $1,168,607 -$19,844    13.18   58.9  <- verified full marathon
#
# WHY IT WORKS: strategy edge varies by VIX regime (64% WR danger zones vs 99%
# sweet spot). Flat qty=2 over-bets bad zones, under-bets good ones. Kelly
# corrects this -- P&L nearly doubles (+92%), Sharpe +0.64, Calmar unchanged.
# Improvement is consistent across all years (2022-2026).
#
# BUYING POWER CONSTRAINT -- LOCKED until BP reaches ~$80k (2026-03-28):
#   Full Kelly peak BP: ~$77,720  (10 positions × qty=4 × ~$1,943/contract)
#   Current account BP: ~$40,000  -> max sustainable qty = 2.07 -> qty=2
#   Baseline already uses 97% of available BP ($38,860 peak day).
#   Enable Full Kelly when account BP reaches ~$80,000+.
ENABLE_KELLY_SIZING = False
KELLY_ZONE_QTY = [           # (vix_lo_inclusive, vix_hi_exclusive, qty)
    (0.0,  13.0, 1),         # VIX <13:    Kelly 13% -- danger zone, min size
    (13.0, 15.0, 3),         # VIX 13-15:  Kelly 70% -- recovering zone
    (15.0, 20.0, 4),         # VIX 15-20:  Kelly 87% -- sweet spot
    (20.0, 25.0, 4),         # VIX 20-25:  Kelly 93% -- sweet spot
    (25.0, 30.0, 1),         # VIX 25-30:  Kelly 13% -- danger zone, min size
    (30.0, 999.0, 4),        # VIX >30:    Kelly 83% -- crisis vol, high edge
]

# -- VIX Day Filter --
# Set to a float to skip trading days outside this VIX range. None = no filter.
#
# BLACK SWAN ANALYSIS (2026-03-28):
# Historical VIX closes on major crisis days:
#   LTCM/Russia 1998        VIX close 44.28  (high 45.02)  <- would be skipped
#   Dotcom bottom 2002      VIX close 38.17  (high 38.17)  <- would be skipped
#   Lehman collapse 2008    VIX close 31.70  (high 31.87)  <- NOT skipped (damage spread over weeks)
#   GFC peak panic 2008     VIX close 69.95  (high 76.94)  <- would be skipped
#   Flash Crash 2010        VIX close 32.80  (high 40.71)  <- NOT skipped (close below 35; DAILY_SL covers this)
#   US Downgrade 2011       VIX close 48.00  (high 48.00)  <- would be skipped
#   China Black Monday 2015 VIX close 40.74  (high 53.29)  <- would be skipped
#   Volmageddon 2018        VIX close 37.32  (high 38.80)  <- would be skipped
#   COVID circuit breaker   VIX close 54.46  (high 62.12)  <- would be skipped
#   COVID peak 2020-03-16   VIX close 82.69  (high 83.56)  <- would be skipped
#   Yen carry unwind 2024   VIX close 38.57  (high 65.73)  <- would be skipped
#   Tariff shock 2025-04    VIX close 45-52  (high 57-60)  <- would be skipped
#
# VIX > 35 closes in 2022-2026 backtest: 8 days. VIX 30+ zone has 97.9% WR ($51,630 P&L) --
# skipping VIX 35+ costs a fraction of that; run marathon to quantify exact cost.
# Cases NOT caught by this filter (Flash Crash, early Lehman) are covered by DAILY_SL = -20,000.
# The two filters together provide layered black swan protection.
VIX_MIN_FILTER = None
VIX_MAX_FILTER = 35.0   # skip days where VIX > 35 -- black swan protection (see analysis above)

VIX_ANALYSIS_FILE = _out("meds_vix_analysis.csv")

# -- Daily Bar Indicator Filters --
# Applied to PRIOR day's indicators before deciding to trade today.
# Set each to None to disable. dGapPercent uses today's open vs prior close.
DAY_FILTER_VIX_MAX      = None   # today's VIX <= X (mirrors VIX_MAX_FILTER but for combined filters)
DAY_FILTER_VIX_MIN      = None   # today's VIX >= X (mirrors VIX_MIN_FILTER)
DAY_FILTER_VIX_CHG_MAX  = None   # prior day VIX change% <= X (e.g. 5 = skip if VIX jumped >5%)
DAY_FILTER_VIX_CHG_MIN  = None   # prior day VIX change% >= X (e.g. -5 = skip if VIX dropped >5%)
DAY_FILTER_VARPC_MIN    = None   # prior day close position in range >= X (0-100; 50=closed above midpoint)
DAY_FILTER_RSI_MIN      = None   # prior day RSI(14) >= X (avoid oversold; e.g. 45)
DAY_FILTER_RSI_MAX = None
DAY_FILTER_DIST_SMA_MIN = None   # prior day (close-SMA20)/SMA20 >= X (e.g. -0.03 = no more than 3% below 20d mean)
DAY_FILTER_ATR_MAX      = None   # prior day ATR(14) <= X SPX points (avoid high-vol days)
DAY_FILTER_BODY_MAX     = None   # prior day abs(open-close) <= X SPX points (avoid large directional candles)
DAY_FILTER_KNIFE_MAX    = None   # prior day abs(close-SMA5)/SMA5 <= X (avoid price far from 5d SMA)
DAY_FILTER_STOCH_MIN    = None   # prior day Stoch %K(14) >= X (avoid oversold momentum)
DAY_FILTER_GAP_MAX      = None   # today's abs(gap%) <= X (avoid large gap opens; e.g. 0.5 = 0.5%)
DAY_FILTER_ABOVE_SMA5          = None   # True = only trade when prior close > SMA5
DAY_FILTER_ABOVE_SMA200        = None   # True = only trade when prior close > SMA200
DAY_FILTER_ADX_MIN = None
DAY_FILTER_RANGE_MAX           = None   # prior day (H-L)/prevC <= X% (skip high-range days; e.g. 1.5)
DAY_FILTER_RANGE_MIN = None
DAY_FILTER_EXP_MOVE_MAX        = None   # today VIX-implied 1-day move <= X% (skip high-vol days; e.g. 1.5)
DAY_FILTER_EXP_MOVE_MIN = None
DAY_FILTER_CCI_MAX = None
DAY_FILTER_IVR_MIN = None
DAY_FILTER_SKIP_VIX_RISE_DECEL = False  # True = skip days where VIX rising but decelerating
                                        # SWEEP RESULT: improves Sharpe 10.60->13.17 but costs ~$13k P&L.
                                        # Rise+decel days still profitable (92%+ WR, $312/day avg).
                                        # Inconsistent year-to-year -- likely overfitting. Keep False.

DAILY_TP       = None  # SWEEP RESULT: None wins. $750 cap was cutting winners short.
                       # Full-run sweep: None=$607k, $900=$512k, $800=$487k, $750=$466k baseline.
                       # Same MaxDD (-$9,922) and better Sharpe (12.35 vs 11.40). Keep None.
DAILY_SL       = -20000   # Black swan protection. -20000 never fired in 4yr backtest (worst day -$6,118) -- zero P&L cost.
                            # -5000 DAILY_SL resulted in $365,580.00 PNL
                            # -10000 DAILY_SL resulted in $497,722.00 PNL
                            # -15000 DAILY_SL resulted in $578,172.00 PNL
                            # -20000 DAILY_SL resulted in $606,832.00 PNL (same as None -- free insurance)
                            # None   DAILY_SL resulted in $606,832.00 PNL

# -- Dynamic Stop Loss --
# Danger zones:
#   1. VIX < 13 or VIX 25-30 -> apply SL (original rule)
#   2. VIX 13-17 -> apply SL (CALL spreads blow up in low-VIX bullish drift)
# All other VIX levels use no stop loss (DAILY_SL = None).

# ENABLE_DYNAMIC_SL = False = $621,380.00 PNL, but AVG loss is -$815.15
# DYNAMIC_SL_AMOUNT = -900 = $600,804.00 PNL -$264 avg loss
# DYNAMIC_SL_AMOUNT = -800 = $607,424.00 PNL -$246 avg loss
# DYNAMIC_SL_AMOUNT = -700 = $600,556.00 PNL -$237 avg loss
# DYNAMIC_SL_AMOUNT = -600 = $598,072.00 PNL -$222 avg loss
# DYNAMIC_SL_AMOUNT = -500 = $596,788.00 PNL -$212 avg loss
# DYNAMIC_SL_AMOUNT = -400 = $585,114.00 PNL -$198 avg loss
# DYNAMIC_SL_AMOUNT = -300 = $586,256.00 PNL -$189 avg loss
# DYNAMIC_SL_AMOUNT = -200 = $570,204.00 PNL -$183 avg loss
# DYNAMIC_SL_AMOUNT = -100 = $570,204.00 PNL -$174 avg loss

ENABLE_DYNAMIC_SL    = True
DYNAMIC_SL_AMOUNT    = -800.0        # stop loss applied on danger-zone days
DYNAMIC_SL_VIX_LOW   = 13.0         # apply SL when VIX < this
DYNAMIC_SL_VIX_HIGH  = (25.0, 30.0) # apply SL when VIX is in this range (inclusive)
DYNAMIC_SL_VIX_MID   = (13.0, 13.5) # apply SL when VIX is in this range (low-VIX CALL blowup zone)
                                     # SWEEP RESULT: (13.0, 13.5) optimal -> $418,774, Sharpe 10.60.
                                     # Prior setting (13.0, 17.0) was too wide -- applying SL on VIX 15-17
                                     # days (which have 84.5% WR) cost ~$98k unnecessarily.
                                     # Tightening to just the 13-13.5 danger band unlocked that P&L.
# -- Intraday Bayesian Entry Gate --
# Stops opening new entries when current_day_pnl (live MTM on all open positions) falls
# below this threshold.
#
# TESTED 2026-03-28 -- NEGATIVE RESULT. All thresholds cost P&L vs baseline.
#
#   Threshold |  Trades | P&L        | Max DD   | Notes
#   -------   |  ------   | ----       | ------   | -----
#   Baseline  |   7,014   | $607,034   | -$9,922  | (no gate)
#   $0        |   1,314   | $95,694    | -$3,992  | fires after every 1st entry -- useless
#   -$150     |   5,925   | $493,880   | -$9,922  | -$113k vs baseline
#   -$300     |   6,313   | $527,578   | -$9,922  | -$79k vs baseline
#   -$400     |   6,469   | $543,234   | -$9,922  | -$64k vs baseline
#
# ROOT CAUSE: Each open position immediately shows ~-$29 MTM from bid-ask spread.
# With 5-6 simultaneous positions, the cumulative initial paper loss is -$150 to -$200,
# making the signal indistinguishable from a real directional loss across any tested
# threshold. The CSV pre-analysis was misleading -- it used final P&L (perfectly correlated
# with day outcome) rather than intraday MTM (which reflects B/A noise on win days too).
# The dynamic SL at -$500 already handles the true loss scenario; the gap between the
# noise floor and -$500 cannot be reliably exploited with this approach.
#
# DECISION: keep None. Do not enable unless a per-position MTM CHANGE signal (relative
# to entry mark, not absolute) is developed to isolate directional loss from B/A spread.
INTRADAY_ENTRY_GATE  = None          # keep disabled -- see above

# -- Per-Position Fixed Stop Loss --
# Closes an individual position when its own MTM loss exceeds this threshold,
# independent of total daily P&L. Targets large single-position losses (e.g. 2023-10-09
# where the 11:00 entry lost -$2,834 on its own). Different from per-position trailing
# stop (tested negative) -- this is a fixed floor, not a peak-pullback trigger.
# None = disabled (baseline).
ENABLE_PER_POS_SL  = False
PER_POS_SL_AMOUNT  = -400.0   # close position if MTM loss exceeds this (e.g. -400 = -$400)

SKIP_VIX_RANGE       = None          # (lo, hi) -> skip day entirely when VIX is in this range.
                                     # TESTED: (25.0, 30.0) -- full marathon backtest result:
                                     #   P&L: $607,034 -> $597,112  (-$9,922, -1.6%)
                                     #   Max DD: unchanged at -$9,922  (worst DD comes from elsewhere)
                                     #   Sharpe: 12.54 -> 14.37  (+1.83, cosmetic -- fewer noisy days)
                                     #   Win Rate: 93.1% -> 96.4%
                                     # DECISION: keep None. The SL already limits the zone to near
                                     # break-even. Skipping costs real P&L with no drawdown benefit.
                                     # Revisit only if live SL execution is slipping through -$500.

# -- Month + Direction Stop Loss --
# Applies a stop loss on specific calendar months where PUT or CALL spreads
# historically underperform. Keys are month numbers (1-12).
# "CALL" = apply SL only on CALL spread days, "BOTH" = apply on any direction.
ENABLE_MONTH_DIR_SL  = False  # SWEEP RESULT: costs -$49k vs baseline. Blanket monthly rules
                              # don't account for within-month VIX variation. Jun 2025 alone:
                              # +$11,604 -> -$5,808 after applying SL. Keep disabled.
MONTH_DIR_SL_AMOUNT  = -500.0
MONTH_DIR_SL_RULES   = {
    6:  "BOTH",   # Jun: both PUT (83.9%) and CALL (84.4%) weak historically
    7:  "CALL",   # Jul: CALL weak (83.1% WR historically)
    9:  "CALL",   # Sep: CALL weak (91.1%, avg only $26.54 historically)
    3:  "CALL",   # Mar: CALL weak (88.7%, avg only $36.74 historically)
}

ENTRY_START    = time(9, 35)   # SWEEP RESULT: 9:35-12:45 with 20min interval has best Calmar (3.64).
ENTRY_END      = time(12, 45)  # 5-min interval boosts P&L to ~$143k but doubles drawdown to -$66k.
ENTRY_INTERVAL = 20            # More frequent entries = more exposure on bad days. 20min is the
                               # sweet spot balancing opportunity capture vs risk concentration.
MAX_TRADES_DAY = 10
PNL_SAMPLE_INTERVAL = 5          # fetch MTM quotes every N minutes on normal days
DANGER_PNL_SAMPLE_INTERVAL = 1  # tighter MTM check interval on dynamic SL days (catches -$500 threshold faster)

EMA_FAST       = 10
EMA_SLOW       = 30

# -- Premium Buyback Exit --
# Close a position early when its remaining close cost (buy-back price) falls to or below
# this threshold. Frees up buying power to potentially open additional entries within the
# entry window. e.g. 0.10 = close when the spread can be bought back for $0.10 or less
# (i.e. you've captured ~87%+ of a $0.75 credit).
# Set to None to disable (hold all positions to EOD, SL, or TP as normal).
#
# TESTED 2026-03-28 -- NEGATIVE RESULT.
#   Threshold |  Trades | P&L        | Max DD   | Buyback closes
#   --------- |  ------   | ----       | ------   | -----
#   Baseline  |   7,014   | $607,034   | -$9,922  | 0
#   $0.10     |   7,013   | $543,158   | -$9,922  | 6,397  (-$64k)
#   $0.05     |   7,014   | $604,054   | -$9,922  | 6,349  (-$3k, breakeven)
#
# ROOT CAUSE: The buyback exit frees up BP but almost never enables a new entry --
# trades count is 7,013 vs 7,014 baseline. Most positions reach $0.10 AFTER the
# entry window closes (12:45), so the freed BP has nowhere to deploy. The early close
# just forfeits the remaining premium plus commission. The "recycle BP" thesis fails
# for 0DTE because there is no next-day deployment; capital resets at EOD regardless.
# The $0.05 level is nearly free (-$3k) but equally useless.
PREMIUM_BUYBACK_EXIT = None   # keep disabled -- see above

# -- Touch Exit --
# Close a position when the underlying price is within TOUCH_EXIT_DOLLARS (or TOUCH_EXIT_PCT%)
# of the short strike. Positive = OTM buffer (exit before ITM); 0 = exit at-the-money;
# Negative = allow going ITM by that amount before exiting.
# For PUT: triggers when curr_price <= short_strike + touch_dollars
#           (or curr_price <= short_strike * (1 + touch_pct/100))
# For CALL: triggers when curr_price >= short_strike - touch_dollars
#           (or curr_price >= short_strike * (1 - touch_pct/100))
# Set both to None to disable. Only one should be active at a time.
TOUCH_EXIT_DOLLARS = None   # e.g. 5 = exit when underlying is $5 above short put
TOUCH_EXIT_PCT     = None   # e.g. 0.5 = exit when 0.5% OTM; -1.0 = exit when 1% ITM
RUN_TOUCH_SWEEP    = False
SWEEP_TOUCH_DOLLARS = [-20, -10, -5, -2, 0, 2, 5, 10, 20, 30]  # None added programmatically as baseline
SWEEP_TOUCH_PCT     = [-2.0, -1.0, -0.5, -0.25, 0, 0.25, 0.5, 1.0, 1.5, 2.0]
TOUCH_SWEEP_FILE    = _out("meds_touch_sweep.csv")

# -- Stop Loss Sweep --
# Set RUN_SL_SWEEP = True to run a parameter sweep instead of the normal single backtest.
RUN_SL_SWEEP    = False
SWEEP_SL_LEVELS = [-250, -300, -500, -750, -900, -1200, -1500, -1800, -2000, None]  # None = no stop loss
SWEEP_SAVE_FILE = _out("meft_v35_bidask_sl_sweep.csv")

# -- Daily Profit Target Sweep --
# Sweeps the intraday profit target (closes all positions when day P&L >= level).
# None = no daily profit target (let positions run to expiration).
# NOTE: Pool-based sweep is unreliable for TP (shows wrong sign on all results due to
# EMA seeding issue). Use run_tp_sweep.sh for sequential full-run testing instead.
# SWEEP RESULT (2026-03-27, full marathon runs via run_tp_sweep.sh):
#   None=$607k, $900=$512k, $800=$487k, $750=$466k, $700=$442k, $650=$416k, $600=$393k
#   Higher TP always wins. None best -- same MaxDD (-$9,922), Sharpe 12.35 vs 11.40.
RUN_DAILY_TP_SWEEP    = False
SWEEP_DAILY_TP_LEVELS = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0, 800.0, 900.0, 1100.0, None]  # None = no daily TP
DAILY_TP_SWEEP_FILE   = _out("meds_daily_tp_sweep.csv")

# -- PnL Sample Interval Sweep --
# Sweeps how frequently (in minutes) the MTM check runs to evaluate open positions.
# Lower = more responsive (catches moves faster); higher = fewer checks per day.
RUN_PNL_SAMPLE_SWEEP    = False
SWEEP_PNL_SAMPLE_INTERVALS = [1, 2, 3, 5, 10, 15]  # minutes between MTM checks
PNL_SAMPLE_SWEEP_FILE   = _out("meds_pnl_sample_sweep.csv")

# -- Max Buying Power Sweep --
# Caps total committed buying power across all open positions at once.
# At entry, qty is dynamically reduced so that (committed_bp + new_bp) stays within limit.
# None = no limit. BP per contract = (spread_width - credit) × 100.
MAX_BUYING_POWER     = None   # active single-run setting (None = no cap)
RUN_MAX_BP_SWEEP     = False
SWEEP_MAX_BP_LEVELS  = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 40_000, None]
MAX_BP_SWEEP_FILE    = _out("meds_max_bp_sweep.csv")

# -- Daily Bar Indicator Filter Sweep --
# Tests each indicator independently across threshold levels.
# Each row = one (indicator, threshold) combination with full backtest metrics.
RUN_DAY_FILTER_SWEEP  = False
SWEEP_DAY_FILTERS = {
    "vix_max":          [12, 13, 14, 14.5, 15, 16, 18, 20, None],  # today's VIX <= X (None=no filter)
    "vix_min":          [12, 13, 14, 14.5, 15, 16, 18, 20],        # today's VIX >= X (None=no filter)
    "dVixChgPct_max":   [2, 3, 5, 8, 10, 15],                      # prior day VIX chg% <= X (filter big spikes)
    "dVixChgPct_min":   [-15, -10, -8, -5, -3, -2],                # prior day VIX chg% >= X (filter big drops)
    "priceChgPct_max":  [0.5, 1.0, 1.5, 2.0, 3.0],                # intraday price chg from prior close <= X% at entry
    "priceChgPct_min":  [-3.0, -2.0, -1.5, -1.0, -0.5],           # intraday price chg from prior close >= X% at entry
    "priceChgSD_max":   [0.25, 0.5, 0.75, 1.0, 1.5],              # price chg in VIX-implied σ <= X at entry (skip big up moves)
    "priceChgSD_min":   [-1.5, -1.0, -0.75, -0.5, -0.25],         # price chg in VIX-implied σ >= X at entry (skip big down moves)
    "dVarPct_min":      [20, 30, 40, 50, 60],            # prior day close in range >=X%
    "dRsi_min":         [25, 35, 45, 55, 65],            # prior day RSI >= X
    "dRsi_max":         [60, 65, 70, 75, 80],            # prior day RSI <= X
    "distFromSma_min":  [-0.05, -0.03, -0.01, 0.01],    # (close-sma20)/sma20 >= X
    "dATR_max":         [20, 30, 40, 50, 70, 100],       # prior day ATR(14) <= X pts
    "dBodySize_max":    [10, 20, 30, 40, 60],            # prior day body <= X pts
    "fallingKnife_max": [0.005, 0.01, 0.02, 0.03],      # abs(close-sma5)/sma5 <= X
    "dStoch_min":       [20, 30, 40, 50],                # prior day Stoch %K >= X
    "dGapPercent_max":  [0.25, 0.5, 0.75, 1.0, 1.5],    # today abs(gap%) <= X%
    "dGapPercent_min":  [-1.5, -1.0, -0.75, -0.5, -0.25],  # today gap% >= X (filter big gap-downs)
    "above_sma5":       [True],                          # close > 5-day SMA
    "above_sma10":      [True],                          # close > 10-day SMA
    "above_sma20":      [True],                          # close > 20-day SMA
    "above_sma30":      [True],                          # close > 30-day SMA
    "above_sma50":      [True],                          # close > 50-day SMA
    "above_sma100":     [True],                          # close > 100-day SMA
    "above_sma200":     [True],                          # close > 200-day SMA
    # -- Momentum oscillators --
    "dAdx_min":         [15, 20, 25, 30, 40],           # prior day ADX(14) >= X (trending)
    "dCci_min":         [-100, -50, 0, 50, 100],        # prior day CCI(20) >= X
    "dCci_max":         [0, 50, 100, 150, 200],         # prior day CCI(20) <= X
    "dCmo_min":         [-50, -25, 0, 25, 50],          # prior day CMO(9) >= X
    "dMacdBull":        [True],                         # prior day MACD > Signal (bullish cross)
    "dMomentum_min":    [-50, -20, 0, 20, 50],          # prior day Momentum(10) >= X pts
    "dStochD_min":      [20, 30, 40, 50],               # prior day Stoch %D(3) >= X
    "dStochRsiK_min":   [20, 30, 40, 50],               # prior day StochRSI K(3) >= X
    # -- Intraday open-to-entry change --
    "openChgPct_max":   [0.25, 0.5, 1.0, 1.5, 2.0],    # (entry_price - day_open) / day_open * 100 <= X%
    "openChgPct_min":   [-2.0, -1.5, -1.0, -0.5, -0.25],  # (entry_price - day_open) / day_open * 100 >= X%
    # -- Expected move (VIX-implied 1-day 1σ) --
    "dExpMoveUSD_min":  [10, 15, 20, 25, 30],           # today expected move >= $X
    "dExpMoveUSD_max":  [30, 40, 50, 60, 80],           # today expected move <= $X
    "dExpMovePct_min":  [0.5, 0.75, 1.0, 1.25, 1.5],   # today expected move >= X%
    "dExpMovePct_max":  [1.0, 1.5, 2.0, 2.5, 3.0],     # today expected move <= X%
    # -- Prior-day trading range --
    "dRangePct_min":    [0.25, 0.5, 0.75, 1.0, 1.5],   # prior day (H-L)/prevC >= X%
    "dRangePct_max":    [0.75, 1.0, 1.5, 2.0, 3.0],    # prior day (H-L)/prevC <= X%
    # -- IV Rank (52-week VIX rank, prior close) --
    "dIvRank_min":      [20, 30, 40, 50, 60],           # prior day IV Rank >= X (avoid low-IV)
    "dIvRank_max":      [40, 50, 60, 70, 80],           # prior day IV Rank <= X (avoid high-IV)
}
DAY_FILTER_SWEEP_FILE = _out("meds_day_filter_sweep.csv")

# -- EMA Parameter Sweep --
# Set RUN_EMA_SWEEP = True to sweep all (fast, slow) EMA combinations.
# Data is fetched once per day; the simulation is re-run for each combo from cache.
RUN_EMA_SWEEP       = False
SWEEP_EMA_FAST      = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
SWEEP_EMA_SLOW      = [18, 20, 22, 24, 26, 40, 45, 50, 55]
EMA_SWEEP_SAVE_FILE = _out("meds_ema_sweep.csv")

# -- Trailing Stop Sweep --
# When enabled, runs the backtest for each trailing stop level.
# A trailing stop closes all positions when portfolio P&L falls back by the given
# dollar amount from its intraday peak. If peak P&L never goes positive, the regular
# daily stop loss still applies. None = no trailing stop.
RUN_TRAILING_STOP_SWEEP  = False
SWEEP_TS_LEVELS          = [50, 100, 150, 200, 300, 400, 500, 600, None]
TRAILING_STOP_SWEEP_FILE = _out("meds_trailing_stop_sweep.csv")

# -- Per-Position Trailing Stop Sweep --
# When enabled, sweeps all (activation%, pullback%) combos for a per-position
# trailing stop. A position's trail activates once it has profited >= activation%
# of credit received; it then closes if P&L pulls back >= pullback% of credit
# from that position's peak. Both values are % of credit received (0-100 scale).
RUN_POS_TRAIL_SWEEP        = False
SWEEP_POS_TRAIL_ACTIVATION = [50, 60, 70, 80, 90]   # % of credit to activate trail
SWEEP_POS_TRAIL_PULLBACK   = [5, 10, 15, 20, 25]    # % of credit pullback to close
POS_TRAIL_SWEEP_FILE       = _out("meds_pos_trail_sweep.csv")

# -- Minimum Net Credit Sweep --
# When enabled, sweeps the minimum credit required to enter a spread.
# Higher values = fewer but higher-quality entries. Lower values = more trades
# but smaller credits. Results are sorted by total_pnl descending.
RUN_MIN_CREDIT_SWEEP    = False
SWEEP_MIN_CREDIT_LEVELS = [0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 1.00]
MIN_CREDIT_SWEEP_FILE   = _out("meds_min_credit_sweep.csv")

# -- Max Net Credit Sweep --
# Sweeps an upper bound on credit collected. If the best qualifying spread gives
# credit above this cap, the entry is skipped (too close to money / too risky).
# None means no cap (baseline behaviour).
RUN_MAX_CREDIT_SWEEP    = False
SWEEP_MAX_CREDIT_LEVELS = [None, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 1.00]
MAX_CREDIT_SWEEP_FILE   = _out("meds_max_credit_sweep.csv")

# -- Min OTM Distance Sweep --
# Sweeps the minimum OTM distance (pts) the short strike must be from spot at entry.
# Trades where the nearest qualifying spread is closer than this are skipped.
RUN_MIN_OTM_SWEEP    = False
SWEEP_MIN_OTM_LEVELS = [None, 10, 15, 20, 25, 30, 35, 40, 50]
MIN_OTM_SWEEP_FILE   = _out("meds_min_otm_sweep.csv")

# -- Spread Width Sweep --
# When enabled, runs the full backtest across each spread width (distance between
# short and long strike). Market data is fetched once per day and re-used for every
# width -- no extra API calls. Results are sorted by total_pnl descending.
RUN_SPREAD_WIDTH_SWEEP  = False
SWEEP_SPREAD_WIDTHS     = [10, 15, 20, 30, 40, 50]
SPREAD_WIDTH_SWEEP_FILE = _out("meds_spread_width_sweep.csv")

# -- Naive Baseline Comparison --
# When True, re-runs the same date range after the main backtest with three naive baselines
# and prints/saves a side-by-side summary. Ignored when RUN_SL_SWEEP = True.
RUN_BASELINE_COMPARISON  = False
BASELINE_COMPARISON_FILE = _out("meds_baseline_comparison.csv")

# -- Strike Distance Analysis --
# When True, buckets trades by short-strike distance from spot at entry and reports
# trade count, win rate, avg P&L, and total P&L per bucket.
RUN_STRIKE_DISTANCE_ANALYSIS  = True
STRIKE_DISTANCE_ANALYSIS_FILE = _out("meds_strike_distance_analysis.csv")

# -- PUT vs CALL Split Analysis --
# When True, groups trades by spread type and reports key metrics for each.
RUN_PUT_CALL_ANALYSIS  = True
PUT_CALL_SPLIT_FILE    = _out("meds_put_call_split.csv")

# -- Combo Sweep --
# Set RUN_COMBO_SWEEP = True to run a full cross-parameter grid search.
# All combinations of (vix_max × width × daily_sl × min_credit) are tested.
# Data is fetched once per day; every combo is re-simulated from the cache.
# Results are saved to COMBO_SWEEP_FILE and auto-analyzed (top combos by P&L
# and Calmar, parameter sensitivity rankings, and per-VIX-bucket best combos).
RUN_COMBO_SWEEP      = False
# Sweep 13: entry time window at best settings (width=200, mc=0.56)
# Note: RUN_ENTRY_TIME_SWEEP handles this -- keep combo sweep off until done
COMBO_VIX_MAX        = [None]
COMBO_WIDTHS         = [200]
COMBO_SL_LEVELS      = [None]
COMBO_MIN_CREDITS    = [0.56]
COMBO_TS_LEVELS      = [None]   # trailing stop levels for mega combo sweep
COMBO_TP_LEVELS      = [None]   # daily TP levels for mega combo sweep
COMBO_SWEEP_FILE     = _out("meds_combo_sweep.csv")

# -- Entry Time Sweep --
# Sweeps all combinations of entry_start × entry_interval (entry_end is fixed).
# Data is fetched once per day; every combo is re-simulated from the cache.
# Results are saved to ENTRY_TIME_SWEEP_FILE.
RUN_ENTRY_TIME_SWEEP   = False
ENTRY_TIME_STARTS      = [time(9, 30), time(9, 45), time(10, 0), time(10, 30), time(11, 0)]
ENTRY_TIME_ENDS        = [time(12, 45), time(13, 30), time(14, 0)]
ENTRY_TIME_INTERVALS   = [5, 10, 15, 20, 30]            # minutes between entry attempts
ENTRY_TIME_SWEEP_FILE  = _out("meds_entry_time_sweep.csv")
ENTRY_TIME_DOW_FILE    = _out("meds_entry_time_dow.csv")

# -- Direction × Time Sweep --
# Compares PUT-only, CALL-only, and Both-Sides at each entry time slot to find
# which direction mode is most profitable at each time of day (and per DOW).
RUN_DIRECTION_TIME_SWEEP = False
DIRECTION_TIME_SLOTS = [                              # individual slots to test
    time(9, 35), time(9, 55), time(10, 15), time(10, 35), time(10, 55),
    time(11, 15), time(11, 35), time(11, 55), time(12, 15), time(12, 35),
]
DIRECTION_TIME_SWEEP_FILE = _out("meds_direction_time_sweep.csv")
DIRECTION_TIME_DOW_FILE   = _out("meds_direction_time_dow.csv")

# -- Master Sweep --
# Set RUN_MASTER_SWEEP = True to run ALL sweeps (SL, EMA, trailing stop, spread
# width, baseline comparison) in a single pass -- data is fetched once per day
# and every sweep variant is re-simulated from the cache.  All per-sweep flags
# above are ignored when this mode is active.
RUN_MASTER_SWEEP = False

# -- Calendar Event Filter Sweep --
# Tests the P&L impact of skipping each calendar event type independently.
# Events: CPI, NFP, PPI, PCE, monthly OPEX, end-of-month, end-of-quarter,
#         first weekly expiry, and full moon.
# For each event, all days that fall on that event are removed from the pool
# and the full backtest is re-run. The delta vs. baseline shows whether
# skipping those days would have hurt or improved P&L.
RUN_CALENDAR_SWEEP  = False
CALENDAR_SWEEP_FILE = _out("meds_calendar_sweep.csv")

# -- CALL-Side SL Sweep --
# Tests a dedicated stop-loss applied only on CALL spread days (VIX rose -> sell calls).
# PUT days continue to use _get_effective_sl() (dynamic VIX-based SL) unchanged.
# Motivation: all max drawdown in the baseline comes from CALL spreads (-$9,922 CALL vs -$5,982 PUT).
RUN_CALL_SL_SWEEP   = False
CALL_SL_SWEEP_FILE  = _out("meds_call_sl_sweep.csv")
CALL_SL_SWEEP_LEVELS = [-100, -200, -300, -400, -500, -600, -700, -800, -1000, -1500, None]

# -- VIX Magnitude Filter Sweep --
# Skips days where |dVixChgPct| is below a threshold (signal too weak to be directional).
# Analysis shows 0-1% VIX change days have only 83.5% win rate vs 93%+ for larger moves.
RUN_VIX_MAG_SWEEP   = False
VIX_MAG_SWEEP_FILE  = _out("meds_vix_mag_sweep.csv")
VIX_MAG_THRESHOLDS  = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]  # skip if |dVixChgPct| < threshold

# -- Gap-Down CALL SL Sweep --
# Gap-down + CALL days are the weakest combo: $369/day avg vs $530+ for others.
# Theory: intraday gap-fill rallies can threaten CALL strikes on gap-down days.
# Tests adding a tighter SL exclusively on days where gap < 0 AND direction is CALL.
RUN_GAP_CALL_SL_SWEEP   = False
GAP_CALL_SL_SWEEP_FILE  = _out("meds_gap_call_sl_sweep.csv")
GAP_CALL_SL_LEVELS      = [-100, -200, -300, -400, -500, -600, -700, -800, -1000, None]

# -- Gap-Down CALL Day SL (live) --
# Apply a tighter SL on days where market gaps down AND direction is CALL.
# Analysis: gap-down + CALL days avg only $369/day vs $530+ for all other combos.
ENABLE_GAP_CALL_SL  = False
GAP_CALL_SL_AMOUNT  = -300.0  # tighter SL applied on gap-down CALL days

# -- VIX Sub-13 Tighter SL Sweep --
# Tests applying a tighter SL exclusively on days where VIX < threshold.
# Motivation: Jun 2024 (VIX 11.95-13.41) had 44.7% WR and drove -$4,660 monthly loss.
# The existing DYNAMIC_SL_VIX_LOW=13 already applies -$500 SL on VIX<13 days.
# SWEEP RESULT (2026-03-27, pool-based): Only 4 days had VIX < 12 in full backtest --
# too few to matter. Widened to VIX < 13 (65 days): pool sweep unreliable (2,797 trades
# vs 6,954 full run), all levels flat. June 2024 is a regime anomaly, not a structural
# SL sizing problem. No change warranted -- keep existing -$500 dynamic SL.
RUN_VIX_SUB12_SL_SWEEP   = False
VIX_SUB12_SL_SWEEP_FILE  = _out("meds_vix_sub12_sl_sweep.csv")
VIX_SUB12_SL_LEVELS      = [-100, -150, -200, -250, -300, -350, -400, -500, None]
VIX_SUB12_THRESHOLD      = 13.0   # apply tighter SL only when VIX < this

# -- VIX Sub-13 Tighter SL (live) --
ENABLE_VIX_SUB12_SL  = False
VIX_SUB12_SL_AMOUNT  = -300.0  # tighter SL on VIX < threshold days

# -- VIX 15-20 Loose SL (live) --
# The "safe zone" (VIX 15-20) has no dynamic SL, but Oct 9 2023 (-$6,118, VIX 17.7)
# is the single largest loss day and defines max DD. A loose SL here caps tail losses
# without strangling the 97.8% WR normal days in this zone.
ENABLE_VIX_MID_SAFE_SL   = False
VIX_MID_SAFE_SL_AMOUNT   = -1500.0  # loose SL for VIX 15-20 zone
VIX_MID_SAFE_SL_RANGE    = (15.0, 20.0)

# -- Backwardation SL (VIX term spread) --
# When VIX term structure inverts (VIX1D > VIX, i.e. term spread < 0),
# acute same-day fear exceeds 30-day — apply tighter daily SL.
# SWEEP RESULT (day-level approx): -$500 SL on spread<0 showed +$13,382 in approximation,
# but FULL MARATHON REJECTED: costs -$91,642 P&L, Sharpe 14.55 -> 11.80. Same failure
# mode as all other SLs: fires early in day, cuts off recovery + blocks re-entry.
ENABLE_BACKWARDATION_SL      = False
BACKWARDATION_SL_AMOUNT      = -500.0   # tighter SL when term structure is inverted
BACKWARDATION_SPREAD_THRESHOLD = 0.0    # apply SL when (VIX - VIX1D) < this value

# -- EOM SL Sweep --
# Tests applying a tighter daily SL exclusively on the last trading day of each month.
# EOM days have 74% WR and $105/day avg vs 93% WR and $618/day for normal days.
# Top 4 single-day losses (-$7k, -$7k, -$4.3k, -$2.5k) all fell on EOM dates.
RUN_EOM_SL_SWEEP    = False
EOM_SL_SWEEP_FILE   = _out("meds_eom_sl_sweep.csv")
EOM_SL_SWEEP_LEVELS = [-200, -300, -400, -500, -600, None]

# -- Per-Position Fixed SL Sweep --
RUN_PER_POS_SL_SWEEP    = False
PER_POS_SL_SWEEP_FILE   = _out("meds_per_pos_sl_sweep.csv")
PER_POS_SL_SWEEP_LEVELS = [None, -200, -300, -400, -500, -600]  # None = baseline (no per-pos SL)

# -- VIX-Range Entry Cutoff Sweep --
# Tests stopping new entries earlier on VIX LO-HI days to avoid late-day reversal losses.
# None = use global ENTRY_END for all VIX ranges (baseline).
RUN_VIX_ENTRY_CUTOFF_SWEEP    = False
VIX_ENTRY_CUTOFF_SWEEP_FILE   = _out("meds_vix_entry_cutoff_sweep.csv")
VIX_ENTRY_CUTOFF_SWEEP_TIMES  = [time(10, 45), time(11, 5), time(11, 25), time(11, 45),
                                   time(12, 5), time(12, 25), None]  # None = baseline (12:45)
VIX_ENTRY_CUTOFF_VIX_LO       = 15.0   # apply cutoff when VIX >= this
VIX_ENTRY_CUTOFF_VIX_HI       = 20.0   # apply cutoff when VIX <  this

# -- EOM SL (live) --
ENABLE_EOM_SL         = True
EOM_SL_AMOUNT         = -200.0   # tighter SL on EOM days (normal VIX)
EOM_SL_AMOUNT_DANGER  = -150.0   # even tighter SL on EOM days when VIX is in danger zone

# -- SL Gap Re-entry --
# After the batch STOP_LOSS fires, instead of blocking all further entries for the day
# (current behaviour), wait SL_GAP_MINUTES then allow new entries to resume.
# Hypothesis: volatility clusters dissipate after ~60 min; re-entering lets us
# capture any intraday recovery. Only applies to STOP_LOSS (not PROFIT_TARGET or
# TRAILING_STOP -- those keep stopped_today=True). Affects 49 of 89 batch-SL days
# (SL fires before 11:40, leaving at least one entry slot after the gap).
# NOTE: untested -- needs full marathon run to verify.
#  |    Gap (min)    | Trades |  WR%  |   P&L    |  Delta  | Sharpe |
#  +-----------------+--------+-------+----------+---------+--------+
#  | None (baseline) | 6,929  | 93.1% | $607,554 | --       | 13.90  |
#  +-----------------+--------+-------+----------+---------+--------+
#  | 30 min          | 7,114  | 92.4% | $611,914 | +$4,360 | 14.07  |
#  +-----------------+--------+-------+----------+---------+--------+
#  | 60 min          | 7,042  | 92.7% | $612,012 | +$4,458 | 14.15  |
#  +-----------------+--------+-------+----------+---------+--------+
#  | 90 min          | 6,995  | 92.9% | $610,150 | +$2,596 | 14.04  |
#  +-----------------+--------+-------+----------+---------+--------+
#  | 120 min         | 6,957  | 93.0% | $607,802 | +$248   | 13.89  |

ENABLE_SL_GAP_REENTRY = True    # tested: +$4,458 P&L, Sharpe 14.15 (best at 60 min); DD unchanged at -$6,356
SL_GAP_MINUTES        = 60

# -- Hard Time Exit --
# Close all open positions at a fixed time rather than letting them expire at 15:59.
# Motivation: 40 trades expire at max loss (avg -$1,047 each, total -$41,880).
# Hour-15 losses are 8x worse than noon losses. Closing early may cut these losses
# while only giving up a small amount of remaining premium on winning positions.
ENABLE_HARD_TIME_EXIT = False   # TODO: test with marathon, then enable if positive
HARD_TIME_EXIT_TIME   = time(15, 15)  # close all positions at this time

# -- Hard Time Exit Sweep --
RUN_HARD_TIME_EXIT_SWEEP      = False
HARD_TIME_EXIT_SWEEP_FILE     = _out("meds_hard_time_exit_sweep.csv")
HARD_TIME_EXIT_SWEEP_TIMES    = [None, time(13, 0), time(13, 30), time(14, 0), time(14, 30),
                                  time(14, 45), time(15, 0), time(15, 15), time(15, 30), time(15, 45)]

# -- SL Gap Re-entry Sweep --
RUN_SL_GAP_SWEEP       = False
SL_GAP_SWEEP_FILE      = _out("meds_sl_gap_sweep.csv")
SL_GAP_SWEEP_LEVELS    = [None, 30, 60, 90, 120]  # None = baseline (no re-entry; stopped_today forever)

# -- Pressure Filter VIX Sweep --
# Tests conditional pressure filter: only activates when VIX >= threshold.
# Baseline row (filter disabled entirely) establishes the current P&L floor.
# The globally-rejected flat filter (-$50k) corresponds to threshold=None.
# Goal: find if gating to high-VIX days (25+) recovers P&L while cutting DD.
RUN_PRESSURE_VIX_SWEEP      = False
PRESSURE_VIX_SWEEP_FILE     = _out("meds_pressure_vix_sweep.csv")
PRESSURE_VIX_SWEEP_THRESHOLDS = [None, 20.0, 22.0, 25.0, 27.0, 28.0, 30.0]  # None = active at all VIX levels

# -- Calendar Risk SL Sweep --
# Tests each recurring-date SL category independently to identify which ones
# are net positive (save more than they cost on winning days).
RUN_CALENDAR_RISK_SL_SWEEP   = False
CALENDAR_RISK_SL_SWEEP_FILE  = _out("meds_calendar_risk_sl_sweep.csv")
CALENDAR_RISK_SL_SWEEP_LEVELS = [-100, -200, -300, -400, -500, None]

# -- Calendar Risk SL --
# Tighter daily SL applied on recurring high-risk calendar dates identified from
# large single-day loss analysis:
#   CPI days:              2 of top 15 losses (May 11 '22, Aug 10 '23)
#   PCE days:              3 of top 15 losses (Jun 27 '24, Aug 29 '24, Sep 30 '24)
#   End of quarter:        3 related losses   (Jun 27 '24, Sep 30 '24, Oct 1 '25)
#   Day before TW:         2 losses           (Jun 15 '23, Sep 15 '22)
#   Post-major-holiday:    2 losses           (Sep 2 '25 Labor Day, Jan 6 '25 New Year)

# ENABLE_CPI_SL = $571,200.00 PNL
# ENABLE_PCE_SL = $585,056.00 PNL
# ENABLE_EOQ_SL = $596,788.00 PNL
# ENABLE_PRE_TW_SL = $590,974.00 PNL
# ENABLE_POST_HOL_SL = $593,580.00 PNL

# Potential: EOQ, PRE_TW, and POST_HOL

ENABLE_CPI_SL        = False
CPI_SL_AMOUNT        = -300.0   # tighter SL on CPI release days
ENABLE_CPI_SKIP      = False    # skip all entries on CPI days entirely

ENABLE_FOMC_SL          = False
FOMC_SL_AMOUNT          = -300.0   # tighter SL on FOMC days (normal VIX)
FOMC_SL_AMOUNT_DANGER   = -200.0   # even tighter SL on FOMC days when VIX in danger zone
ENABLE_FOMC_SKIP        = False    # skip all entries on FOMC days entirely
ENABLE_FOMC_VIX1520_SL  = False    # FOMC SL only when VIX is in 15-20 range (Finding 7)
FOMC_VIX1520_SL_AMOUNT  = -800.0   # SL amount for FOMC days in VIX 15-20
FOMC_VIX1520_RANGE      = (15.0, 20.0)  # VIX range where FOMC SL applies

ENABLE_PCE_SL           = False
PCE_SL_AMOUNT           = -300.0   # tighter SL on PCE release days (normal VIX)
PCE_SL_AMOUNT_DANGER    = -200.0   # even tighter SL on PCE days when VIX in danger zone
ENABLE_PCE_SKIP         = False    # skip all entries on PCE days entirely

ENABLE_EOQ_SL        = False
EOQ_SL_AMOUNT        = -300.0   # tighter SL on last trading day of each quarter
ENABLE_EOQ_SKIP      = True    # skip all entries on EOQ days entirely - new baseline

ENABLE_PRE_TW_SL        = False
PRE_TW_SL_AMOUNT        = -300.0   # tighter SL on the trading day before Triple Witching (normal VIX)
PRE_TW_SL_AMOUNT_DANGER = -200.0   # even tighter SL on pre-TW days when VIX in danger zone
ENABLE_PRE_TW_SKIP      = False    # skip all entries on pre-Triple-Witching days entirely

ENABLE_POST_HOL_SL   = False
POST_HOL_SL_AMOUNT   = -300.0   # tighter SL on first trading day after each market holiday
ENABLE_POST_HOL_SKIP = False    # skip all entries on post-holiday days entirely

# -- VIX-conditional econ skips (Finding 7 + econ_vix_analysis) --
# Skip specific event+VIX combos that are net negative in backtest.
# Unlike blanket event skips which cost P&L, these target only unprofitable pockets.
ENABLE_ECON_VIX_SKIP = False
ECON_VIX_SKIP_RULES_ALL: list[tuple[str, float, float]] = [
    # (event_type, vix_lo, vix_hi) — skip day if event matches AND VIX in range
    # FOMC 15-20: 13 days, 30.8% WR, -$3,372 total
    ("fomc", 15.0, 20.0),
    # FOMC 25-30: 5 days, 20.0% WR, -$2,840 total (already partially covered by dynamic SL)
    ("fomc", 25.0, 30.0),
    # PCE <15: 8 days, 50.0% WR, -$204 total
    ("pce", 0.0, 15.0),
    # NFP 25-30: 3 days, 33.3% WR, -$486 total
    ("nfp", 25.0, 30.0),
]
# Active rule set — change to test subsets
ECON_VIX_SKIP_RULES: list[tuple[str, float, float]] = ECON_VIX_SKIP_RULES_ALL

# -- Bias Sweep --
# Tests each daily indicator as a direction router: bullish signal -> PUT spread,
# bearish signal -> CALL spread.  Compares against always-PUT, always-CALL, and
# intraday-EMA baselines.
RUN_BIAS_SWEEP      = False
BIAS_SWEEP_FILE     = _out("meds_bias_sweep.csv")

# -- Opening Skew --
# At the first entry bar (9:35) each day, fetch PUT and CALL credits at a fixed
# OTM distance to compute a same-day vol skew ratio (put_credit / call_credit).
# Ratio > 1 = market pricing more downside risk (put skew); < 1 = call skew.
# Used as a GEX-proxy to study whether same-day skew improves direction selection.
COMPUTE_OPENING_SKEW = False  # RESULT: skew ratio not a useful trading filter -- see analysis 2026-03-27
OPENING_SKEW_OTM     = 30   # OTM distance (pts) to measure opening credits

LOG_COLS = [
    "entry_date", "entry_time", "option_type",
    "short_strike", "long_strike", "width", "spread_width", "credit_received",
    "entry_short_bid", "entry_short_ask", "entry_short_mid",
    "entry_long_bid",  "entry_long_ask",  "entry_long_mid",
    "profit_target", "stop_loss", "ema13", "ema48",
    "qty", "vix_level", "strike_distance",
    "opening_put_credit", "opening_call_credit", "pc_skew_ratio",
    "profit_date_time", "profit_price",
    "win", "loss", "outcome", "pnl_earned",
    "close_date", "close_time",
]

