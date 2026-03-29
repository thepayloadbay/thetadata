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
PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"
MCP_URL          = "http://127.0.0.1:25503/mcp/sse"
USE_LOCAL_DATA   = True    # True -> read local parquet files; False -> live ThetaData MCP
DATA_DIR         = "data"  # root of local parquet cache (used when USE_LOCAL_DATA=True)
QUOTE_DISK_CACHE = "data/quote_disk_cache.parquet"  # persistent cross-run quote cache
                                                     # populated at end of run, loaded at start
                                                     # eliminates parquet index lookups on repeat runs

# Unique timestamp stamped on every output file so runs never overwrite each other.
from datetime import datetime as _dt
_RUN_TS = _dt.now().strftime("%Y%m%d_%H%M%S")

LOGS_DIR = "logs"
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
DIRECTION_MODE = "vix_change"  # "vix_change" | "always_put" | "always_call" | "ema"
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

ENABLE_PCE_SL           = False
PCE_SL_AMOUNT           = -300.0   # tighter SL on PCE release days (normal VIX)
PCE_SL_AMOUNT_DANGER    = -200.0   # even tighter SL on PCE days when VIX in danger zone
ENABLE_PCE_SKIP         = False    # skip all entries on PCE days entirely

ENABLE_EOQ_SL        = False
EOQ_SL_AMOUNT        = -300.0   # tighter SL on last trading day of each quarter
ENABLE_EOQ_SKIP      = True    # skip all entries on EOQ days entirely

ENABLE_PRE_TW_SL        = False
PRE_TW_SL_AMOUNT        = -300.0   # tighter SL on the trading day before Triple Witching (normal VIX)
PRE_TW_SL_AMOUNT_DANGER = -200.0   # even tighter SL on pre-TW days when VIX in danger zone
ENABLE_PRE_TW_SKIP      = False    # skip all entries on pre-Triple-Witching days entirely

ENABLE_POST_HOL_SL   = False
POST_HOL_SL_AMOUNT   = -300.0   # tighter SL on first trading day after each market holiday
ENABLE_POST_HOL_SKIP = False    # skip all entries on post-holiday days entirely

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
    with open(SAVE_FILE, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_COLS).writeheader()


def append_trade(pos: dict):
    with open(SAVE_FILE, "a", newline="") as f:
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
    if SKIP_VIX_RANGE is not None and vix_level is not None:
        lo, hi = SKIP_VIX_RANGE
        if lo <= vix_level <= hi:
            logger.info(f"VIX={vix_level:.2f} in SKIP_VIX_RANGE {SKIP_VIX_RANGE} -- skipping day")
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
    if ENABLE_KELLY_SIZING and vix_level is not None:
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
            if PREMIUM_BUYBACK_EXIT is not None and active_positions:
                buyback_to_close = []
                for pos in active_positions:
                    close_cost = pos["last_short_ask"] - pos["last_long_bid"]
                    if close_cost <= PREMIUM_BUYBACK_EXIT:
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
        bayesian_gate_ok = (INTRADAY_ENTRY_GATE is None or current_day_pnl >= INTRADAY_ENTRY_GATE)

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
            credit_cap      = max_credit if max_credit is not None else MAX_NET_CREDIT
            otm_floor = min_otm_distance if min_otm_distance is not None else MIN_OTM_DISTANCE
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
        (DYNAMIC_SL_VIX_MID[0] <= vix <= DYNAMIC_SL_VIX_MID[1])
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

    return effective_sl


async def process_day(session, date_str: str) -> tuple:
    """Fetch data for one trading day and run the simulation with the configured DAILY_SL.

    Writes each closed trade to SAVE_FILE (existing behaviour preserved).
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
            (DYNAMIC_SL_VIX_MID[0] <= vix <= DYNAMIC_SL_VIX_MID[1])
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
        min_otm_distance=MIN_OTM_DISTANCE,
        max_credit=MAX_NET_CREDIT,
        pnl_sample_interval=sample_interval,
    )
    for pos in trades:
        append_trade(pos)
    return trades, day_pnl


# ---------------------------------------------
#  VIX CORRELATION ANALYSIS
# ---------------------------------------------
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-30", 25.0, 30.0),
    ("30-35", 30.0, 35.0),
    ("35-40", 35.0, 40.0),
    ("40-45", 40.0, 45.0),
    ("45-50", 45.0, 50.0),
    ("50-55", 50.0, 55.0),
    ("55-60", 55.0, 60.0),
    ("60+",   60.0, None),
]


def print_vix_analysis(all_trades: list) -> None:
    """Bucket trades by VIX level and report win rate, avg P&L, and count.
    Prints to console and saves to VIX_ANALYSIS_FILE."""
    rows = []
    sep = "-" * 62
    logger.info(sep)
    logger.info("  VIX CORRELATION ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'VIX Range':<10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for label, lo, hi in VIX_BUCKETS:
        bucket = []
        for t in all_trades:
            try:
                v = float(t.get("vix_level") or "")
            except (ValueError, TypeError):
                continue
            if (lo is None or v >= lo) and (hi is None or v < hi):
                bucket.append(t)

        n        = len(bucket)
        wins     = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls     = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate = wins / n * 100 if n else 0.0
        avg_pnl  = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)

        logger.info(
            f"  {label:<10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}"
        )
        rows.append({
            "vix_range":    label,
            "trade_count":  n,
            "win_rate_pct": f"{win_rate:.1f}",
            "avg_pnl":      f"{avg_pnl:.2f}",
            "total_pnl":    f"{total_pnl:.2f}",
        })

    logger.info(sep)

    # -- Save to CSV --
    analysis_cols = ["vix_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"]
    with open(VIX_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=analysis_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  VIX analysis saved to {VIX_ANALYSIS_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  STRIKE DISTANCE ANALYSIS
# ---------------------------------------------
STRIKE_DISTANCE_BUCKETS = [
    ("35-40",   35,  40),
    ("40-45",   40,  45),
    ("45-50",   45,  50),
    ("50-55",   50,  55),
    ("55-60",   55,  60),
    ("60-65",   60,  65),
    ("65-70",   65,  70),
    ("70-75",   70,  75),
    ("75-80",   75,  80),
    ("80-85",   80,  85),
    ("85-90",   85,  90),
    ("90-95",   90,  95),
    ("95-100",  95,  100),
    ("100+",    100, None),
]


def print_strike_distance_analysis(all_trades: list) -> None:
    """Bucket trades by short-strike distance from spot at entry and report
    trade count, win rate, avg P&L, and total P&L per bucket.
    Prints to console and saves to STRIKE_DISTANCE_ANALYSIS_FILE."""
    rows = []
    sep = "-" * 70
    logger.info(sep)
    logger.info("  STRIKE DISTANCE VS WIN RATE ANALYSIS")
    logger.info(sep)
    logger.info(f"  {'Distance':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | {'Total P&L':>12}")
    logger.info(sep)

    for label, lo, hi in STRIKE_DISTANCE_BUCKETS:
        bucket = []
        for t in all_trades:
            try:
                d = float(t.get("strike_distance") or "")
            except (ValueError, TypeError):
                continue
            if d >= lo and (hi is None or d < hi):
                bucket.append(t)

        n         = len(bucket)
        wins      = sum(1 for t in bucket if float(t.get("pnl_earned", 0)) > 0)
        pnls      = [float(t.get("pnl_earned", 0)) for t in bucket]
        win_rate  = wins / n * 100 if n else 0.0
        avg_pnl   = sum(pnls) / n if n else 0.0
        total_pnl = sum(pnls)

        logger.info(
            f"  {label:>10} | {n:>7} | {win_rate:>8.1f}% | ${avg_pnl:>9,.2f} | ${total_pnl:>11,.2f}"
        )
        rows.append({
            "strike_distance_range": label,
            "trade_count":           n,
            "win_rate_pct":          f"{win_rate:.1f}",
            "avg_pnl":               f"{avg_pnl:.2f}",
            "total_pnl":             f"{total_pnl:.2f}",
        })

    logger.info(sep)

    analysis_cols = ["strike_distance_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"]
    with open(STRIKE_DISTANCE_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=analysis_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  Strike distance analysis saved to {STRIKE_DISTANCE_ANALYSIS_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  PUT vs CALL SPLIT ANALYSIS
# ---------------------------------------------
def print_put_call_analysis(all_trades: list) -> None:
    """Group trades by PUT vs CALL spread type and report key metrics.
    Prints to console and saves to PUT_CALL_SPLIT_FILE."""
    rows = []
    sep = "-" * 95
    logger.info(sep)
    logger.info("  PUT vs CALL SPREAD PERFORMANCE SPLIT")
    logger.info(sep)
    logger.info(
        f"  {'Type':<8} | {'Trades':>7} | {'Win Rate':>9} | {'Avg P&L':>10} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | {'Prof Fac':>9} | {'Max DD':>11}"
    )
    logger.info(sep)

    split_cols = [
        "spread_type", "trade_count", "win_rate_pct", "avg_pnl",
        "total_pnl", "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]

    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        m = compute_metrics(bucket)
        avg_pnl = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0.0
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf"
        logger.info(
            f"  {label:<8} | {m['num_trades']:>7} | {m['win_rate']:>8.1f}% | "
            f"${avg_pnl:>9,.2f} | ${m['total_pnl']:>11,.2f} | "
            f"${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"{pf:>9} | ${m['max_drawdown']:>10,.2f}"
        )
        rows.append({
            "spread_type":   label,
            "trade_count":   m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "avg_pnl":       f"{avg_pnl:.2f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
        })

    logger.info(sep)

    with open(PUT_CALL_SPLIT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=split_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  PUT/CALL split analysis saved to {PUT_CALL_SPLIT_FILE}")
    logger.info(sep)


# ---------------------------------------------
#  PERFORMANCE ANALYTICS
# ---------------------------------------------
def print_performance_report(all_trades: list, date_list) -> None:
    import math

    pnls        = [t["pnl_earned"] for t in all_trades]
    wins        = [p for p in pnls if p > 0]
    losses      = [p for p in pnls if p <= 0]
    total_pnl   = sum(pnls)
    n           = len(pnls)
    win_rate    = len(wins) / n * 100 if n else 0

    avg_win     = sum(wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(losses) / len(losses) if losses else 0
    expectancy  = (avg_win * len(wins) / n) + (avg_loss * len(losses) / n) if n else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Per-day P&L
    day_pnls: dict[str, float] = {}
    day_trade_count: dict[str, int] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
        day_trade_count[d] = day_trade_count.get(d, 0) + 1
    best_day  = max(day_pnls.values()) if day_pnls else 0
    worst_day = min(day_pnls.values()) if day_pnls else 0
    days_traded = len(day_pnls)
    avg_trades_per_day = n / days_traded if days_traded else 0

    # Sharpe & Sortino -- daily P&L basis, rf-adjusted, annualised with sqrt(252)
    rf_daily     = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals   = list(day_pnls.values())
    n_days       = len(daily_vals)
    mean_daily   = sum(daily_vals) / n_days if n_days else 0
    var_daily    = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily    = math.sqrt(var_daily)
    ann_vol      = std_daily * math.sqrt(252)
    sharpe       = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0

    downside_d   = [p for p in daily_vals if p < mean_daily]
    down_var_d   = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino      = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    # Max drawdown (trade-level equity curve)
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    # Time underwater -- % of trading days where cumulative equity is below its peak
    eq_d = peak_d = 0.0
    underwater_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]
        peak_d = max(peak_d, eq_d)
        if eq_d < peak_d:
            underwater_days += 1
    time_underwater_pct = underwater_days / days_traded * 100 if days_traded else 0

    # CAGR -- based on calendar days between first and last trade date
    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        from datetime import datetime as _dtt
        first_d = _dtt.strptime(sorted_dates[0],  "%Y%m%d")
        last_d  = _dtt.strptime(sorted_dates[-1], "%Y%m%d")
        years   = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    final_equity = ACCOUNT_SIZE + total_pnl
    cagr = ((final_equity / ACCOUNT_SIZE) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Calmar & Recovery factor
    calmar          = cagr / abs(max_dd / ACCOUNT_SIZE * 100) if max_dd != 0 else float("inf")
    recovery_factor = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")

    # Premium capture rate = net P&L / gross premium collected
    gross_premium    = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100
                          for t in all_trades)
    prem_capture_pct = (total_pnl / gross_premium * 100) if gross_premium > 0 else 0.0

    # Commission (pnl_earned already net of commission; add it back to compute gross)
    total_commission = sum(2 * 2 * float(t.get("qty", 1)) * COMMISSION for t in all_trades)

    # Buying power per trade = (spread_width - credit_received) * qty * 100
    bp_per_trade = [(float(t.get("spread_width", WIDTH)) - float(t.get("credit_received", 0)))
                    * float(t.get("qty", 1)) * 100 for t in all_trades]
    avg_bp_per_trade = sum(bp_per_trade) / n if n else 0
    # Max BP deployed on a single day (sum of all trades that day)
    day_bp: dict[str, float] = {}
    for t, bp in zip(all_trades, bp_per_trade):
        d = t["entry_date"]
        day_bp[d] = day_bp.get(d, 0.0) + bp
    max_bp_day = max(day_bp.values()) if day_bp else 0
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0

    # -- Print report --
    sep  = "-" * 56
    sep2 = "-" * 56
    logger.info(sep)
    logger.info("  PERFORMANCE REPORT")
    logger.info(sep)

    # Period & day counts
    logger.info(f"  Period          : {date_list[0].strftime('%Y-%m-%d')} -> {date_list[-1].strftime('%Y-%m-%d')}")
    days_in_range = len(date_list)
    days_skipped  = days_in_range - days_traded
    _holidays_in_range = sum(1 for d in date_list if d.strftime("%Y%m%d") in MARKET_HOLIDAYS)
    _skip_parts = [f"{_holidays_in_range} holidays"]
    if ENABLE_ECON_FILTER:
        _skip_parts.append("econ filter")
    if ENABLE_CALENDAR_FILTER:
        _skip_parts.append(f"calendar ({', '.join(sorted(CALENDAR_FILTER_EVENTS))})")
    _active_day_filters = [k for k, v in [
        ("vix_max", VIX_MAX_FILTER), ("vix_min", VIX_MIN_FILTER),
        ("vix_max", DAY_FILTER_VIX_MAX), ("vix_min", DAY_FILTER_VIX_MIN),
        ("adx_min", DAY_FILTER_ADX_MIN), ("range_max", DAY_FILTER_RANGE_MAX),
        ("range_min", DAY_FILTER_RANGE_MIN), ("rsi_min", DAY_FILTER_RSI_MIN),
        ("rsi_max", DAY_FILTER_RSI_MAX), ("atr_max", DAY_FILTER_ATR_MAX),
        ("exp_mv_max", DAY_FILTER_EXP_MOVE_MAX), ("exp_mv_min", DAY_FILTER_EXP_MOVE_MIN),
        ("cci_max", DAY_FILTER_CCI_MAX), ("ivr_min", DAY_FILTER_IVR_MIN),
        ("sma200", DAY_FILTER_ABOVE_SMA200),
    ] if v is not None]
    if _active_day_filters:
        _skip_parts.append(f"day filters ({', '.join(dict.fromkeys(_active_day_filters))})")
    _other = days_skipped - _holidays_in_range
    if _other > 0:
        _skip_parts.append(f"{_other} no data/no trades")
    logger.info(f"  Business days   : {days_in_range}  (skipped {days_skipped} -- {' + '.join(_skip_parts)})")
    logger.info(f"  Days traded     : {days_traded}")
    logger.info(f"  Total trades    : {n}  (wins: {len(wins)}  losses: {len(losses)})")
    logger.info(f"  Avg trades/day  : {avg_trades_per_day:>10.1f}")
    logger.info(sep)

    # Returns
    logger.info(f"  Total P&L       : ${total_pnl:>10,.2f}  (net of commission)")
    logger.info(f"  Gross premium   : ${gross_premium:>10,.2f}")
    logger.info(f"  Prem capture    : {prem_capture_pct:>9.1f}%  (net P&L / gross premium)")
    logger.info(f"  CAGR            : {cagr:>9.1f}%  (acct=${ACCOUNT_SIZE:,.0f})")
    logger.info(f"  Avg profit/trade: ${avg_win:>10,.2f}")
    logger.info(f"  Avg loss/trade  : ${avg_loss:>10,.2f}")
    logger.info(f"  Expectancy/trade: ${expectancy:>10,.2f}")
    logger.info(f"  Profit factor   : {profit_factor:>10.2f}x")
    logger.info(sep)

    # Risk
    logger.info(f"  Max drawdown    : ${max_dd:>10,.2f}  ({max_dd_pct:.1f}%)")
    logger.info(f"  Calmar ratio    : {calmar:>10.2f}  (CAGR / max DD%)")
    logger.info(f"  Recovery factor : {recovery_factor:>10.2f}  (net P&L / max DD)")
    logger.info(f"  Time underwater : {time_underwater_pct:>9.1f}%  ({underwater_days} of {days_traded} days)")
    logger.info(sep)

    # Ratios
    logger.info(f"  Win rate        : {win_rate:>9.1f}%")
    logger.info(f"  Sharpe ratio    : {sharpe:>10.2f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%)")
    logger.info(f"  Sortino ratio   : {sortino:>10.2f}")
    logger.info(f"  Ann. volatility : ${ann_vol:>10,.2f}  (daily P&L std × √252)")
    logger.info(sep)

    # Day stats
    logger.info(f"  Best day        : ${best_day:>10,.2f}")
    logger.info(f"  Worst day       : ${worst_day:>10,.2f}")
    logger.info(sep)

    # Cost & capital
    logger.info(f"  Commission total: ${total_commission:>10,.2f}  (${COMMISSION}/contract/leg)")
    logger.info(f"  Avg BP/trade    : ${avg_bp_per_trade:>10,.2f}")
    logger.info(f"  Avg BP/day      : ${avg_bp_day:>10,.2f}")
    logger.info(f"  Max BP day      : ${max_bp_day:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  RESULTS.md APPEND
# ---------------------------------------------
def append_results_md(all_trades: list, date_list) -> None:
    """Append a full performance snapshot to RESULTS.md after every run."""
    import math
    from collections import defaultdict
    from datetime import datetime as _dtt

    # -- Core metrics ----------------------------------------------------------
    pnls   = [t["pnl_earned"] for t in all_trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    total_pnl     = sum(pnls)
    win_rate      = len(wins) / n * 100 if n else 0
    avg_win       = sum(wins)   / len(wins)   if wins   else 0
    avg_loss      = sum(losses) / len(losses) if losses else 0
    expectancy    = (avg_win * len(wins) / n) + (avg_loss * len(losses) / n) if n else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    gross_premium    = sum(float(t.get("credit_received", 0)) * float(t.get("qty", 1)) * 100
                          for t in all_trades)
    prem_capture_pct = (total_pnl / gross_premium * 100) if gross_premium > 0 else 0.0

    day_pnls: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnls[d] = day_pnls.get(d, 0.0) + t["pnl_earned"]
    days_traded        = len(day_pnls)
    avg_trades_per_day = n / days_traded if days_traded else 0
    best_day           = max(day_pnls.values()) if day_pnls else 0
    worst_day          = min(day_pnls.values()) if day_pnls else 0

    rf_daily   = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals = list(day_pnls.values())
    n_days     = len(daily_vals)
    mean_daily = sum(daily_vals) / n_days if n_days else 0
    var_daily  = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily  = math.sqrt(var_daily)
    ann_vol    = std_daily * math.sqrt(252)
    sharpe     = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0
    downside_d = [p for p in daily_vals if p < mean_daily]
    down_var_d = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino    = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    eq_d = peak_d = 0.0; underwater_days = 0
    for d in sorted(day_pnls):
        eq_d += day_pnls[d]; peak_d = max(peak_d, eq_d)
        if eq_d < peak_d: underwater_days += 1
    time_underwater_pct = underwater_days / days_traded * 100 if days_traded else 0

    sorted_dates = sorted(day_pnls.keys())
    if len(sorted_dates) >= 2:
        first_d = _dtt.strptime(sorted_dates[0],  "%Y%m%d")
        last_d  = _dtt.strptime(sorted_dates[-1], "%Y%m%d")
        years   = (last_d - first_d).days / 365.25
    else:
        years = 1.0
    final_equity    = ACCOUNT_SIZE + total_pnl
    cagr            = ((final_equity / ACCOUNT_SIZE) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar          = cagr / abs(max_dd / ACCOUNT_SIZE * 100) if max_dd != 0 else float("inf")
    recovery_factor = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")
    total_commission = sum(2 * 2 * float(t.get("qty", 1)) * COMMISSION for t in all_trades)
    bp_per_trade = [(float(t.get("spread_width", WIDTH)) - float(t.get("credit_received", 0)))
                    * float(t.get("qty", 1)) * 100 for t in all_trades]
    avg_bp_per_trade = sum(bp_per_trade) / n if n else 0
    day_bp: dict[str, float] = {}
    for t, bp in zip(all_trades, bp_per_trade):
        day_bp[t["entry_date"]] = day_bp.get(t["entry_date"], 0.0) + bp
    max_bp_day = max(day_bp.values()) if day_bp else 0
    avg_bp_day = sum(day_bp.values()) / len(day_bp) if day_bp else 0

    # -- Helpers ---------------------------------------------------------------
    def _sf(val):
        """Safe float -- returns float or None."""
        try:
            return float(val) if val not in (None, "") else None
        except (ValueError, TypeError):
            return None

    def _bucket_stats(bucket):
        bp = [float(t.get("pnl_earned", 0)) for t in bucket]
        m  = len(bp)
        if m == 0:
            return 0, 0.0, 0.0, 0.0
        w  = sum(1 for x in bp if x > 0)
        eq2 = pk2 = dd2 = 0.0
        for x in bp:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        return m, w / m * 100, sum(bp) / m, sum(bp)

    # -- VIX correlation table -------------------------------------------------
    vix_rows = []
    for label, lo, hi in VIX_BUCKETS:
        bucket = [t for t in all_trades if
                  _sf(t.get("vix_level")) is not None and
                  (lo is None or _sf(t.get("vix_level")) >= lo) and
                  (hi is None or _sf(t.get("vix_level")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        vix_rows.append((label, cnt, wr, avg_p, tot_p))

    # -- Strike distance table -------------------------------------------------
    sd_rows = []
    for label, lo, hi in STRIKE_DISTANCE_BUCKETS:
        bucket = [t for t in all_trades if
                  _sf(t.get("strike_distance")) is not None and
                  _sf(t.get("strike_distance")) >= lo and
                  (hi is None or _sf(t.get("strike_distance")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        sd_rows.append((label, cnt, wr, avg_p, tot_p))

    # -- PUT vs CALL split -----------------------------------------------------
    pc_rows = []
    for label in ("PUT", "CALL"):
        bucket = [t for t in all_trades if t.get("option_type") == label]
        m = compute_metrics(bucket)
        avg_p = m["total_pnl"] / m["num_trades"] if m["num_trades"] else 0.0
        pf_str = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf"
        pc_rows.append((label, m["num_trades"], m["win_rate"], avg_p,
                        m["total_pnl"], m["avg_win"], m["avg_loss"], pf_str, m["max_drawdown"]))

    # -- Dynamic SL VIX zones --------------------------------------------------
    vl = DYNAMIC_SL_VIX_LOW; ml, mh = DYNAMIC_SL_VIX_MID; hl, hh = DYNAMIC_SL_VIX_HIGH
    dyn_buckets = [
        (f"< {vl}",   None, vl,   True),
        (f"{ml}-{mh}", ml,  mh,   True),
        (f"{mh}-{hl}", mh,  hl,   False),
        (f"{hl}-{hh}", hl,  hh,   True),
        (f"> {hh}",    hh,  None, False),
    ]
    dyn_rows = []
    for label, lo, hi, sl_on in dyn_buckets:
        bucket = [t for t in all_trades if
                  _sf(t.get("vix_level")) is not None and
                  (lo is None or _sf(t.get("vix_level")) >= lo) and
                  (hi is None or _sf(t.get("vix_level")) < hi)]
        cnt, wr, avg_p, tot_p = _bucket_stats(bucket)
        bp2 = [float(x.get("pnl_earned", 0)) for x in bucket]
        eq2 = pk2 = dd2 = 0.0
        for x in bp2:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        dyn_rows.append((label, "ON" if sl_on else "--", cnt, wr, avg_p, tot_p, dd2))

    # -- Monthly P&L -----------------------------------------------------------
    month_pnl: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    month_trades_map: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        y = int(t["entry_date"][:4]); m2 = int(t["entry_date"][4:6])
        month_pnl[y][m2] += t["pnl_earned"]
        month_trades_map[y][m2].append(t["pnl_earned"])

    def _month_max_dd(pl_list):
        eq2 = pk2 = dd2 = 0.0
        for x in pl_list:
            eq2 += x; pk2 = max(pk2, eq2); dd2 = min(dd2, eq2 - pk2)
        return dd2

    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    all_years = sorted(month_pnl.keys())

    # -- Large loss days -------------------------------------------------------
    loss_day_pnl, loss_day_trades, loss_day_vix, loss_day_wins, loss_day_losses = \
        _aggregate_loss_days(all_trades)
    worst_15 = sorted([(pnl, d2) for d2, pnl in loss_day_pnl.items() if pnl < 0])[:15]

    # -- Build markdown --------------------------------------------------------
    run_ts = _dtt.now().strftime("%Y-%m-%d %H:%M:%S")
    period = f"{date_list[0].strftime('%Y-%m-%d')} -> {date_list[-1].strftime('%Y-%m-%d')}"

    L = [
        "\n---\n",
        f"## Run: {run_ts}",
        f"**Period:** {period}  |  **Days traded:** {days_traded}  |  **Total trades:** {n}",
        "",
        "### Returns",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total P&L (net) | ${total_pnl:,.2f} |",
        f"| Gross premium collected | ${gross_premium:,.2f} |",
        f"| Premium capture rate | {prem_capture_pct:.1f}% |",
        f"| CAGR | {cagr:.1f}% |",
        f"| Avg profit/trade | ${avg_win:,.2f} |",
        f"| Avg loss/trade | ${avg_loss:,.2f} |",
        f"| Expectancy/trade | ${expectancy:,.2f} |",
        f"| Profit factor | {profit_factor:.2f}x |",
        "",
        "### Risk",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Max drawdown | ${max_dd:,.2f} ({max_dd_pct:.1f}%) |",
        f"| Calmar ratio | {calmar:.2f} |",
        f"| Recovery factor | {recovery_factor:.2f} |",
        f"| Time underwater | {time_underwater_pct:.1f}% ({underwater_days} of {days_traded} days) |",
        "",
        "### Key Config",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| Width | {int(WIDTH)}pt |",
        f"| QTY | {QTY} |",
        f"| Min credit | ${MIN_NET_CREDIT:.2f} |",
        f"| Min OTM dist | {MIN_OTM_DISTANCE}pt |",
        f"| Entry window | {ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min |",
        f"| Dynamic SL | {'on' if ENABLE_DYNAMIC_SL else 'off'} ${DYNAMIC_SL_AMOUNT if ENABLE_DYNAMIC_SL else ''} |",
        f"| EOM SL | {'on' if ENABLE_EOM_SL else 'off'} {'$'+str(int(EOM_SL_AMOUNT)) if ENABLE_EOM_SL else ''} |",
        f"| CPI SL | {'on $'+str(int(CPI_SL_AMOUNT)) if ENABLE_CPI_SL else 'off'} |",
        f"| PCE SL | {'on $'+str(int(PCE_SL_AMOUNT)) if ENABLE_PCE_SL else 'off'} |",
        f"| EOQ SL | {'on $'+str(int(EOQ_SL_AMOUNT)) if ENABLE_EOQ_SL else 'off'} |",
        f"| Pre-TW SL | {'on $'+str(int(PRE_TW_SL_AMOUNT)) if ENABLE_PRE_TW_SL else 'off'} |",
        f"| Post-holiday SL | {'on $'+str(int(POST_HOL_SL_AMOUNT)) if ENABLE_POST_HOL_SL else 'off'} |",
        "",
        "### Ratios & Volatility",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Win rate | {win_rate:.1f}% |",
        f"| Sharpe ratio | {sharpe:.2f} |",
        f"| Sortino ratio | {sortino:.2f} |",
        f"| Ann. volatility | ${ann_vol:,.2f} |",
        "",
        "### Day Stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Best day | ${best_day:,.2f} |",
        f"| Worst day | ${worst_day:,.2f} |",
        f"| Avg trades/day | {avg_trades_per_day:.1f} |",
        "",
        "### Cost & Capital",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Commission total | ${total_commission:,.2f} |",
        f"| Avg BP/trade | ${avg_bp_per_trade:,.2f} |",
        f"| Avg BP/day | ${avg_bp_day:,.2f} |",
        f"| Max BP day | ${max_bp_day:,.2f} |",
        "",
        "### VIX Correlation",
        "| VIX Range | Trades | Win Rate | Avg P&L | Total P&L |",
        "|-----------|-------:|--------:|--------:|----------:|",
    ]
    for label, cnt, wr, avg_p, tot_p in vix_rows:
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} |")

    L += [
        "",
        "### Dynamic SL VIX Zones",
        "| VIX Range | Dyn SL | Trades | Win Rate | Avg P&L | Total P&L | Max DD |",
        "|-----------|:------:|-------:|--------:|--------:|----------:|-------:|",
    ]
    for label, sl_flag, cnt, wr, avg_p, tot_p, mdd in dyn_rows:
        L.append(f"| {label} | {sl_flag} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} | ${mdd:,.2f} |")

    L += [
        "",
        "### Strike Distance vs Win Rate",
        "| Distance | Trades | Win Rate | Avg P&L | Total P&L |",
        "|----------|-------:|--------:|--------:|----------:|",
    ]
    for label, cnt, wr, avg_p, tot_p in sd_rows:
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} |")

    L += [
        "",
        "### PUT vs CALL Split",
        "| Type | Trades | Win Rate | Avg P&L | Total P&L | Avg Win | Avg Loss | Prof Factor | Max DD |",
        "|------|-------:|--------:|--------:|----------:|--------:|---------:|------------:|-------:|",
    ]
    for row in pc_rows:
        label, cnt, wr, avg_p, tot_p, aw, al, pf_str, mdd = row
        L.append(f"| {label} | {cnt} | {wr:.1f}% | ${avg_p:,.2f} | ${tot_p:,.2f} | ${aw:,.2f} | ${al:,.2f} | {pf_str} | ${mdd:,.2f} |")

    # Monthly P&L table
    L += ["", "### Monthly P&L ($)"]
    L.append("| Year | " + " | ".join(mon_abbr) + " | Total |")
    L.append("|------|" + "|".join(["------:"] * 12) + "|------:|")
    yearly_totals: dict[int, float] = {}
    for y in all_years:
        ytotal = sum(month_pnl[y].get(m2, 0.0) for m2 in range(1, 13) if m2 in month_pnl[y])
        yearly_totals[y] = ytotal
        cells = []
        for m2 in range(1, 13):
            pval = month_pnl[y].get(m2)
            cells.append(f"${pval:,.0f}" if pval is not None else "--")
        L.append(f"| {y} | " + " | ".join(cells) + f" | ${ytotal:,.0f} |")
    # Grand total row
    gtotal_cells = []
    grand_total = 0.0
    for m2 in range(1, 13):
        col_sum = sum(month_pnl[y].get(m2, 0.0) for y in all_years if m2 in month_pnl[y])
        has_data = any(m2 in month_pnl[y] for y in all_years)
        gtotal_cells.append(f"${col_sum:,.0f}" if has_data else "--")
        if has_data: grand_total += col_sum
    L.append(f"| **Total** | " + " | ".join(gtotal_cells) + f" | **${grand_total:,.0f}** |")

    # Intra-month max drawdown table
    L += ["", "### Intra-Month Max Drawdown ($)"]
    L.append("| Year | " + " | ".join(mon_abbr) + " | Annual DD |")
    L.append("|------|" + "|".join(["------:"] * 12) + "|----------:|")
    for y in all_years:
        cells = []
        for m2 in range(1, 13):
            tlist = month_trades_map[y].get(m2)
            cells.append(f"${_month_max_dd(tlist):,.0f}" if tlist else "--")
        yr_dd = _month_max_dd([t["pnl_earned"] for t in sorted(all_trades, key=lambda x: x["entry_date"]) if int(t["entry_date"][:4]) == y])
        L.append(f"| {y} | " + " | ".join(cells) + f" | ${yr_dd:,.0f} |")

    # Strategy vs SPY comparison
    spy_ret_md = _get_spy_monthly_returns(all_years[0], all_years[-1])
    if spy_ret_md:
        L += ["", "### Monthly Return: Strategy vs SPY (strat% / SPY%)"]
        L.append("| Year | " + " | ".join(mon_abbr) + " | Annual Strat | Annual SPY | Alpha |")
        L.append("|------|" + "|".join([":------:"] * 12) + "|------------:|-----------:|------:|")
        for y in all_years:
            cells = []
            s_ann = sp_ann = 0.0
            for m2 in range(1, 13):
                spnl  = month_pnl[y].get(m2)
                spy_m = spy_ret_md.get((y, m2))
                if spnl is None and spy_m is None:
                    cells.append("--")
                else:
                    s_pct  = f"{spnl/ACCOUNT_SIZE*100:+.1f}%" if spnl  is not None else "--"
                    sp_pct = f"{spy_m:+.1f}%"                 if spy_m is not None else "--"
                    if spnl  is not None: s_ann  += spnl / ACCOUNT_SIZE * 100
                    if spy_m is not None: sp_ann += spy_m
                    cells.append(f"{s_pct} / {sp_pct}")
            L.append(f"| {y} | " + " | ".join(cells) +
                     f" | {s_ann:+.1f}% | {sp_ann:+.1f}% | {s_ann-sp_ann:+.1f}% |")

    # Large loss days
    _ev_md = _build_calendar_event_dates()
    L += ["", "### Largest Loss Days (worst 15)"]
    L.append("| Date | VIX | Trades | W/L | Day P&L | Event |")
    L.append("|------|----:|-------:|----:|--------:|-------|")
    for pnl, d2 in worst_15:
        date_fmt  = f"{d2[:4]}-{d2[4:6]}-{d2[6:]}"
        vix_val   = loss_day_vix.get(d2)
        vix_str   = f"{vix_val:.1f}" if vix_val is not None else "?"
        w         = loss_day_wins.get(d2, 0)
        l         = loss_day_losses.get(d2, 0)
        event_lbl = _get_day_event_labels(d2, _ev_md)
        L.append(f"| {date_fmt} | {vix_str} | {loss_day_trades[d2]} | {w}W/{l}L | ${pnl:,.2f} | {event_lbl} |")

    # Entry time breakdown
    import datetime as _dt_mod
    et_buckets: dict = {}
    for t in all_trades:
        slot = t.get("entry_time", "")[:5]
        if slot not in et_buckets:
            et_buckets[slot] = {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []}
        et_buckets[slot]["pnl"] += t["pnl_earned"]
        et_buckets[slot]["pnls"].append(t["pnl_earned"])
        et_buckets[slot]["wins" if t.get("win") else "losses"] += 1
    for b in et_buckets.values():
        eq = pk = dd = 0.0
        for p in b["pnls"]:
            eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        b["max_dd"] = dd

    L += ["", "### Entry Time Breakdown"]
    L.append("| Time | Trades | W/L | WR% | Total P&L | Max DD |")
    L.append("|------|-------:|----:|----:|----------:|-------:|")
    for slot in sorted(et_buckets.keys()):
        b = et_buckets[slot]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0.0
        L.append(f"| {slot} | {total} | {b['wins']}W/{b['losses']}L | {wr:.1f}% | ${b['pnl']:,.2f} | ${b['max_dd']:,.2f} |")

    # Seasonality
    dow_b: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(5)}
    mon_b: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(1, 13)}
    for t in all_trades:
        d3 = t.get("entry_date", "")
        if len(d3) != 8:
            continue
        try:
            dt = _dt_mod.date(int(d3[:4]), int(d3[4:6]), int(d3[6:]))
        except ValueError:
            continue
        pnl3 = t["pnl_earned"]
        win3 = bool(t.get("win"))
        dow_b[dt.weekday()]["pnl"] += pnl3
        mon_b[dt.month]["pnl"]     += pnl3
        dow_b[dt.weekday()]["wins" if win3 else "losses"] += 1
        mon_b[dt.month]["wins" if win3 else "losses"]     += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    mon_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    L += ["", "### Seasonality -- Day of Week"]
    L.append("| Day | Trades | WR% | Total P&L | Avg P&L |")
    L.append("|-----|-------:|----:|----------:|--------:|")
    for i, name in enumerate(dow_names):
        b = dow_b[i]
        total = b["wins"] + b["losses"]
        wr  = b["wins"] / total * 100 if total else 0.0
        avg = b["pnl"] / total if total else 0.0
        L.append(f"| {name} | {total} | {wr:.1f}% | ${b['pnl']:,.2f} | ${avg:,.2f} |")

    L += ["", "### Seasonality -- Month"]
    L.append("| Month | Trades | WR% | Total P&L | Avg P&L |")
    L.append("|-------|-------:|----:|----------:|--------:|")
    for i, name in enumerate(mon_names, 1):
        b = mon_b[i]
        total = b["wins"] + b["losses"]
        wr  = b["wins"] / total * 100 if total else 0.0
        avg = b["pnl"] / total if total else 0.0
        L.append(f"| {name} | {total} | {wr:.1f}% | ${b['pnl']:,.2f} | ${avg:,.2f} |")

    # Econ date analysis
    day_pnl_all: dict[str, float] = {}
    for t in all_trades:
        d4 = t["entry_date"]
        day_pnl_all[d4] = day_pnl_all.get(d4, 0.0) + t["pnl_earned"]

    def _econ_stats(date_set):
        pnls = [day_pnl_all[d] for d in day_pnl_all if d in date_set]
        if not pnls: return 0, 0.0, 0.0, 0.0, 0.0
        wins  = sum(1 for p in pnls if p > 0)
        wr    = wins / len(pnls) * 100
        avg   = sum(pnls) / len(pnls)
        total = sum(pnls)
        eq = pk = dd = 0.0
        for p in pnls:
            eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        return len(pnls), wr, avg, total, dd

    nfp_set_md   = {d for d in ECON_DATES if d not in _CPI_DATES and d not in _PCE_DATES}
    tw_set_md    = {d for d in day_pnl_all if d in TRIPLE_WITCHING_DATES}
    fomc_set_md  = {d for d in day_pnl_all if d in FOMC_DATES}
    normal_md    = {d for d in day_pnl_all
                    if d not in ECON_DATES and d not in tw_set_md and d not in fomc_set_md
                    and d not in _EOM_DATES and d not in _EOQ_DATES
                    and d not in _PRE_TW_DATES and d not in _POST_HOL_DATES}

    md_event_types = [
        ("Normal",       normal_md),
        ("CPI",          {d for d in day_pnl_all if d in _CPI_DATES}),
        ("PCE",          {d for d in day_pnl_all if d in _PCE_DATES}),
        ("NFP",          {d for d in day_pnl_all if d in nfp_set_md}),
        ("FOMC",         fomc_set_md),
        ("Triple Witch", tw_set_md),
        ("EOM",          {d for d in day_pnl_all if d in _EOM_DATES}),
        ("EOQ",          {d for d in day_pnl_all if d in _EOQ_DATES}),
        ("Pre-TW",       {d for d in day_pnl_all if d in _PRE_TW_DATES}),
        ("Post-Holiday", {d for d in day_pnl_all if d in _POST_HOL_DATES}),
    ]

    L += ["", "### Calendar / Econ Event Analysis"]
    L.append("| Event | Days | WR% | Avg/Day | Total P&L | Max DD |")
    L.append("|-------|-----:|----:|--------:|----------:|-------:|")
    for label, dset in md_event_types:
        n, wr, avg, total, dd = _econ_stats(dset)
        if n == 0:
            continue
        L.append(f"| {label} | {n} | {wr:.1f}% | ${avg:,.2f} | ${total:,.2f} | ${dd:,.2f} |")

    L.append("")

    results_path = "RESULTS.md"
    new_block = "\n".join(L)
    if os.path.exists(results_path):
        existing = open(results_path).read()
        # Strip the static header so we can re-prepend it cleanly
        header = "# MEDS Strategy -- Backtest Results\n\n"
        body = existing[len(header):] if existing.startswith(header) else existing
        with open(results_path, "w") as f:
            f.write(header + new_block + "\n" + body)
    else:
        with open(results_path, "w") as f:
            f.write("# MEDS Strategy -- Backtest Results\n\n" + new_block + "\n")

    logger.info(f"  Results prepended to {results_path}")


# ---------------------------------------------
#  SETTINGS SUMMARY
# ---------------------------------------------
def print_settings_summary() -> None:
    sep = "-" * 62
    logger.info(sep)
    logger.info("  STRATEGY SETTINGS")
    logger.info(sep)
    logger.info(f"  Period          : {PILOT_YEAR_START} -> {PILOT_YEAR_END}")
    logger.info(f"  Spread          : {int(WIDTH)}pt width  |  min credit ${MIN_NET_CREDIT:.2f}")
    logger.info(f"  Entry window    : {ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')}  every {ENTRY_INTERVAL} min")
    logger.info(f"  EMA direction   : fast={EMA_FAST}  slow={EMA_SLOW}")
    logger.info(f"  Daily SL / TP   : {DAILY_SL if DAILY_SL is not None else 'off'}  /  {DAILY_TP if DAILY_TP is not None else 'off'}")
    eom_str = f"${EOM_SL_AMOUNT:,.0f} on EOM days" if ENABLE_EOM_SL else "off"
    logger.info(f"  EOM stop loss   : {eom_str}")
    dyn_str = (f"${DYNAMIC_SL_AMOUNT:,.0f}  VIX<{DYNAMIC_SL_VIX_LOW} or "
               f"{DYNAMIC_SL_VIX_MID[0]}-{DYNAMIC_SL_VIX_MID[1]} or "
               f"{DYNAMIC_SL_VIX_HIGH[0]}-{DYNAMIC_SL_VIX_HIGH[1]}")
    logger.info(f"  Dynamic SL      : {dyn_str}")
    logger.info(f"  VIX filter      : min={VIX_MIN_FILTER or 'off'}  max={VIX_MAX_FILTER or 'off'}")
    logger.info(f"  Max BP          : {f'${MAX_BUYING_POWER:,.0f}' if MAX_BUYING_POWER is not None else 'unlimited'}")
    # Active day filters (non-None only)
    active_filters = {k: v for k, v in [
        ("vix_max",    DAY_FILTER_VIX_MAX),   ("vix_min",    DAY_FILTER_VIX_MIN),
        ("vix_chg_max",DAY_FILTER_VIX_CHG_MAX),("vix_chg_min",DAY_FILTER_VIX_CHG_MIN),
        ("rsi_min",    DAY_FILTER_RSI_MIN),    ("rsi_max",    DAY_FILTER_RSI_MAX),
        ("atr_max",    DAY_FILTER_ATR_MAX),    ("adx_min",    DAY_FILTER_ADX_MIN),
        ("range_max",  DAY_FILTER_RANGE_MAX),  ("range_min",  DAY_FILTER_RANGE_MIN),
        ("exp_mv_max", DAY_FILTER_EXP_MOVE_MAX),("exp_mv_min",DAY_FILTER_EXP_MOVE_MIN),
        ("cci_max",    DAY_FILTER_CCI_MAX),    ("ivr_min",    DAY_FILTER_IVR_MIN),
        ("gap_max",    DAY_FILTER_GAP_MAX),    ("sma200",     DAY_FILTER_ABOVE_SMA200),
        ("body_max",   DAY_FILTER_BODY_MAX),   ("knife_max",  DAY_FILTER_KNIFE_MAX),
    ] if v is not None}
    if active_filters:
        fstr = "  ".join(f"{k}={v}" for k, v in active_filters.items())
        logger.info(f"  Day filters     : {fstr}")
    else:
        logger.info(f"  Day filters     : none")
    # Calendar risk SL summary
    cal_sl_parts = []
    if ENABLE_CPI_SL:      cal_sl_parts.append(f"CPI ${CPI_SL_AMOUNT:.0f}")
    if ENABLE_PCE_SL:      cal_sl_parts.append(f"PCE ${PCE_SL_AMOUNT:.0f}")
    if ENABLE_EOQ_SL:      cal_sl_parts.append(f"EOQ ${EOQ_SL_AMOUNT:.0f}")
    if ENABLE_PRE_TW_SL:   cal_sl_parts.append(f"pre-TW ${PRE_TW_SL_AMOUNT:.0f}")
    if ENABLE_POST_HOL_SL: cal_sl_parts.append(f"post-hol ${POST_HOL_SL_AMOUNT:.0f}")
    logger.info(f"  Calendar SL     : {', '.join(cal_sl_parts) if cal_sl_parts else 'off'}")
    logger.info(sep)


# ---------------------------------------------
#  DYNAMIC SL VIX RANGE ANALYSIS
# ---------------------------------------------
def print_dynamic_sl_vix_analysis(all_trades: list) -> None:
    """Break down stats by the key VIX ranges that drive dynamic SL behaviour."""
    vl  = DYNAMIC_SL_VIX_LOW
    ml, mh = DYNAMIC_SL_VIX_MID
    hl, hh = DYNAMIC_SL_VIX_HIGH

    # Buckets: (label, lo, hi, dynamic_sl_active)
    buckets = [
        (f"< {vl}",          None, vl,   True),
        (f"{ml}-{mh}",       ml,   mh,   True),
        (f"{mh}-{hl}",       mh,   hl,   False),
        (f"{hl}-{hh}",       hl,   hh,   True),
        (f"> {hh}",          hh,   None, False),
    ]

    def bucket_stats(trades):
        pnls = [float(t.get("pnl_earned", 0)) for t in trades]
        n    = len(pnls)
        if n == 0:
            return dict(n=0, wr=0, avg=0, total=0, max_dd=0)
        wins = [p for p in pnls if p > 0]
        eq = peak = dd = 0.0
        for p in pnls:
            eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
        return dict(
            n=n,
            wr=len(wins)/n*100,
            avg=sum(pnls)/n,
            total=sum(pnls),
            max_dd=dd,
        )

    sep = "-" * 80
    logger.info(sep)
    logger.info("  VIX RANGE ANALYSIS  (dynamic SL zones)")
    logger.info(sep)
    logger.info(
        f"  {'VIX Range':<12} {'DynSL':>6} {'Trades':>7} {'WR%':>7} "
        f"{'Avg P&L':>10} {'Total P&L':>12} {'Max DD':>11}"
    )
    logger.info(sep)

    for label, lo, hi, sl_on in buckets:
        bucket = []
        for t in all_trades:
            try:
                v = float(t.get("vix_level") or "")
            except (ValueError, TypeError):
                continue
            if (lo is None or v >= lo) and (hi is None or v < hi):
                bucket.append(t)
        s = bucket_stats(bucket)
        sl_flag = "ON" if sl_on else "--"
        logger.info(
            f"  {label:<12} {sl_flag:>6} {s['n']:>7} {s['wr']:>6.1f}% "
            f"${s['avg']:>9,.2f} ${s['total']:>11,.2f} ${s['max_dd']:>10,.2f}"
        )

    logger.info(sep)


# ---------------------------------------------
#  MONTHLY P&L TABLE
# ---------------------------------------------
def print_monthly_pnl_table(all_trades: list) -> None:
    from collections import defaultdict

    # Build day_pnl map and monthly buckets
    day_pnl: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d] = day_pnl.get(d, 0.0) + t["pnl_earned"]

    # month_pnl[year][month] = total P&L
    month_pnl: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    # month_trades[year][month] = list of trade pnls in order (for intra-month DD)
    month_trades: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))

    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        y = int(t["entry_date"][:4])
        m = int(t["entry_date"][4:6])
        month_pnl[y][m] += t["pnl_earned"]
        month_trades[y][m].append(t["pnl_earned"])

    # Intra-month max drawdown
    def month_max_dd(pnls):
        eq = peak = dd = 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        return dd

    years = sorted(month_pnl.keys())
    months = list(range(1, 13))
    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    col_w = 9
    hdr = f"  {'Year':<6}" + "".join(f"{m:>{col_w}}" for m in mon_abbr) + f"{'Total':>{col_w+1}}"
    sep = "-" * len(hdr)

    logger.info(sep)
    logger.info("  MONTHLY P&L  ($)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    yearly_totals: dict[int, float] = {}
    for y in years:
        row = f"  {y:<6}"
        ytotal = 0.0
        for m in months:
            pnl = month_pnl[y].get(m)
            if pnl is None:
                row += f"{'--':>{col_w}}"
            else:
                ytotal += pnl
                row += f"{pnl:>{col_w},.0f}"
        row += f"{ytotal:>{col_w+1},.0f}"
        yearly_totals[y] = ytotal
        logger.info(row)

    # Grand total row
    logger.info(sep)
    grand = f"  {'Total':<6}"
    gtotal = 0.0
    for m in months:
        col_sum = sum(month_pnl[y].get(m, 0.0) for y in years if m in month_pnl[y])
        if col_sum == 0.0 and all(m not in month_pnl[y] for y in years):
            grand += f"{'--':>{col_w}}"
        else:
            grand += f"{col_sum:>{col_w},.0f}"
            gtotal += col_sum
    grand += f"{gtotal:>{col_w+1},.0f}"
    logger.info(grand)
    logger.info(sep)

    # Max drawdown by year table
    logger.info("  INTRA-MONTH MAX DRAWDOWN  ($)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for y in years:
        row = f"  {y:<6}"
        for m in months:
            trades_m = month_trades[y].get(m)
            if not trades_m:
                row += f"{'--':>{col_w}}"
            else:
                dd = month_max_dd(trades_m)
                row += f"{dd:>{col_w},.0f}"
        yr_dd = month_max_dd([t["pnl_earned"] for t in sorted(all_trades, key=lambda x: x["entry_date"]) if int(t["entry_date"][:4]) == y])
        row += f"{yr_dd:>{col_w+1},.0f}"
        logger.info(row)
    logger.info(sep)


# ---------------------------------------------
#  SPY BENCHMARK COMPARISON
# ---------------------------------------------
SPY_CACHE_FILE = "spy_monthly_closes.csv"   # local cache: date (YYYY-MM-DD), close

def _get_spy_monthly_returns(first_year: int, last_year: int) -> "dict[tuple,float]":
    """Return {(year, month): pct_return} for SPY.

    Reads from SPY_CACHE_FILE when present; only calls yfinance for months
    not yet in the cache. Saves updated closes back to disk.
    """
    from datetime import date as _date

    # -- Load existing cache ---------------------------------------------------
    cached: dict[str, float] = {}   # "YYYY-MM-DD" -> adjusted close
    if os.path.exists(SPY_CACHE_FILE):
        try:
            with open(SPY_CACHE_FILE, newline="") as f:
                for row in csv.DictReader(f):
                    cached[row["date"]] = float(row["close"])
        except Exception:
            cached = {}

    # -- Determine what's missing ----------------------------------------------
    # We need the month-end close for every month from (first_year-1, 12) through
    # (last_year, 12) -- the extra prior month is needed to compute the first return.
    today = _date.today()
    needed_months = []
    for y in range(first_year - 1, last_year + 1):
        for m in range(1, 13):
            # skip future months
            if y > today.year or (y == today.year and m > today.month):
                continue
            needed_months.append((y, m))

    # Determine which month-end dates are missing from cache
    def _month_end_key(y, m):
        """Return the last calendar day of the month as YYYY-MM-DD string."""
        import calendar
        last_day = calendar.monthrange(y, m)[1]
        return f"{y}-{m:02d}-{last_day:02d}"

    missing = [(y, m) for y, m in needed_months if _month_end_key(y, m) not in cached]

    # -- Fetch missing data from yfinance --------------------------------------
    if missing:
        try:
            import yfinance as yf
            fetch_start_y = min(y for y, _ in missing)
            fetch_start_m = min(m for y, m in missing if y == fetch_start_y)
            # go one month back to ensure we get a prior-month close for the first return
            if fetch_start_m == 1:
                fetch_start_y -= 1; fetch_start_m = 12
            else:
                fetch_start_m -= 1
            fetch_start = f"{fetch_start_y}-{fetch_start_m:02d}-01"
            fetch_end   = f"{last_year + 1}-01-01"
            spy = yf.download("SPY", start=fetch_start, end=fetch_end,
                               auto_adjust=True, progress=False)
            if not spy.empty:
                monthly = spy["Close"].resample("ME").last()
                if hasattr(monthly, "columns"):   # multi-index from newer yfinance
                    monthly = monthly.iloc[:, 0]
                for ts, close in monthly.items():
                    import math as _math
                    v = float(close)
                    if not _math.isnan(v):
                        cached[ts.strftime("%Y-%m-%d")] = v
                # Persist updated cache
                with open(SPY_CACHE_FILE, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["date", "close"])
                    for d_str in sorted(cached):
                        w.writerow([d_str, f"{cached[d_str]:.6f}"])
                logger.info(f"  SPY cache updated -> {SPY_CACHE_FILE} ({len(cached)} month-end rows)")
        except Exception as exc:
            logger.warning(f"  SPY fetch failed: {exc}")

    # -- Compute monthly returns from cached closes ----------------------------
    sorted_closes = sorted((d, v) for d, v in cached.items())
    returns: dict[tuple, float] = {}
    for i in range(1, len(sorted_closes)):
        d_str, close = sorted_closes[i]
        prev_close   = sorted_closes[i - 1][1]
        y, m = int(d_str[:4]), int(d_str[5:7])
        if prev_close and prev_close != 0:
            returns[(y, m)] = (close / prev_close - 1) * 100
    return returns


def print_spy_comparison(all_trades: list) -> None:
    """Year × Month table comparing strategy monthly return % vs SPY monthly return %."""
    from collections import defaultdict

    # Build strategy monthly P&L
    month_pnl: dict[tuple, float] = defaultdict(float)
    for t in all_trades:
        y, m = int(t["entry_date"][:4]), int(t["entry_date"][4:6])
        month_pnl[(y, m)] += t["pnl_earned"]

    if not month_pnl:
        return

    all_years = sorted({y for y, _ in month_pnl})
    spy_ret   = _get_spy_monthly_returns(all_years[0], all_years[-1])

    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    col_w    = 13   # wide enough for "str% / str%"
    hdr      = f"  {'Year':<6}" + "".join(f"{m:^{col_w}}" for m in mon_abbr) + f"{'Annual':^{col_w+2}}"
    sep      = "-" * len(hdr)

    logger.info(sep)
    logger.info("  MONTHLY RETURN: STRATEGY vs SPY  (strat% / SPY%  |  alpha=strat-SPY)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    for y in all_years:
        row = f"  {y:<6}"
        strat_annual = 0.0
        spy_annual   = 0.0
        for m in range(1, 13):
            spnl   = month_pnl.get((y, m))
            spy_m  = spy_ret.get((y, m))
            if spnl is None and spy_m is None:
                row += f"{'--':^{col_w}}"
            else:
                s_pct  = (spnl  / ACCOUNT_SIZE * 100) if spnl  is not None else None
                sp_pct = spy_m if spy_m is not None else None
                if s_pct is not None:  strat_annual += s_pct
                if sp_pct is not None: spy_annual   += sp_pct
                s_str  = f"{s_pct:+.1f}%" if s_pct  is not None else "   -- "
                sp_str = f"{sp_pct:+.1f}%" if sp_pct is not None else "   -- "
                cell   = f"{s_str}/{sp_str}"
                row   += f"{cell:^{col_w}}"
        # Annual column
        a_str  = f"{strat_annual:+.1f}%"
        sp_a   = f"{spy_annual:+.1f}%" if spy_annual != 0.0 else "--"
        alpha  = strat_annual - spy_annual
        row   += f"  {a_str}/{sp_a} (α{alpha:+.1f}%)"
        logger.info(row)

    # Grand totals row
    logger.info(sep)
    total_strat = sum(month_pnl.values()) / ACCOUNT_SIZE * 100
    total_spy   = sum(spy_ret.get((y, m), 0.0) for y in all_years for m in range(1, 13)
                      if (y, m) in month_pnl)
    logger.info(f"  {'Total':<6}  Cumul strategy: {total_strat:+.1f}%  |  "
                f"Cumul SPY: {total_spy:+.1f}%  |  Alpha: {total_strat-total_spy:+.1f}%")
    logger.info(sep)


# ---------------------------------------------
#  LARGE LOSS DAYS
# ---------------------------------------------
def _aggregate_loss_days(all_trades: list):
    """Return (day_pnl, day_trades, day_vix, day_wins, day_losses) dicts keyed by YYYYMMDD."""
    day_pnl: dict[str, float] = {}
    day_trades: dict[str, int] = {}
    day_vix: dict[str, float] = {}
    day_wins: dict[str, int] = {}
    day_losses: dict[str, int] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d]    = day_pnl.get(d, 0.0) + t["pnl_earned"]
        day_trades[d] = day_trades.get(d, 0) + 1
        if d not in day_vix and t.get("vix_level"):
            try:
                day_vix[d] = float(t["vix_level"])
            except (ValueError, TypeError):
                pass
        day_wins[d]   = day_wins.get(d, 0) + int(t.get("win", 0) or 0)
        day_losses[d] = day_losses.get(d, 0) + int(t.get("loss", 0) or 0)
    return day_pnl, day_trades, day_vix, day_wins, day_losses


def _get_day_event_labels(date_str: str, ev: "dict[str, set]") -> str:
    """Return '+'-joined event labels for date_str using a pre-built event dict."""
    label_map = [
        ("FOMC",     ev.get("fomc", set())),
        ("TW",       ev.get("triple_witching", set())),
        ("Pre-TW",   ev.get("pre_triple_witching", set())),
        ("CPI",      ev.get("cpi", set())),
        ("PPI",      ev.get("ppi", set())),
        ("PCE",      ev.get("pce", set())),
        ("NFP",      ev.get("nfp", set())),
        ("EOM",      ev.get("end_of_month", set())),
        ("EOQ",      ev.get("end_of_quarter", set())),
        ("OPEX",     ev.get("monthly_opex", set())),
        ("Post-Hol", ev.get("post_major_holiday", set())),
    ]
    labels = [lbl for lbl, s in label_map if date_str in s]
    return "+".join(labels) if labels else ""


def print_large_loss_days(all_trades: list, n: int = 15) -> None:
    day_pnl, day_trades, day_vix, day_wins, day_losses = _aggregate_loss_days(all_trades)
    loss_days = sorted([(pnl, d) for d, pnl in day_pnl.items() if pnl < 0])
    if not loss_days:
        return

    ev = _build_calendar_event_dates()
    top_n = loss_days[:n]
    sep = "-" * 80
    logger.info(sep)
    logger.info(f"  LARGEST LOSS DAYS  (worst {n})")
    logger.info(sep)
    logger.info(f"  {'Date':<12} {'VIX':>5}  {'Trades':>6}  {'W/L':<7} {'Day P&L':>12}  {'Event'}")
    logger.info(sep)
    for pnl, d in top_n:
        date_fmt  = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        vix_val   = day_vix.get(d)
        vix_str   = f"{vix_val:.1f}" if vix_val is not None else "?"
        w  = day_wins.get(d, 0)
        l  = day_losses.get(d, 0)
        event_lbl = _get_day_event_labels(d, ev)
        logger.info(f"  {date_fmt:<12} {vix_str:>5}  {day_trades[d]:>6}  {w}W/{l}L{'':<2} {pnl:>12,.2f}  {event_lbl}")
    logger.info(sep)


# ---------------------------------------------
#  ENTRY TIME ANALYSIS
# ---------------------------------------------
def print_entry_time_analysis(all_trades: list) -> None:
    """Break down P&L, drawdown, and W/L by entry time slot."""
    from collections import defaultdict
    buckets: dict[str, dict] = {}
    for t in all_trades:
        et = t.get("entry_time", "")[:5]  # "HH:MM"
        if et not in buckets:
            buckets[et] = {"pnl": 0.0, "wins": 0, "losses": 0, "pnls": []}
        buckets[et]["pnl"] += t["pnl_earned"]
        buckets[et]["pnls"].append(t["pnl_earned"])
        if t.get("win"):
            buckets[et]["wins"] += 1
        else:
            buckets[et]["losses"] += 1

    # Compute per-slot max drawdown (equity curve within each slot across all days)
    for slot, b in buckets.items():
        eq = pk = dd = 0.0
        for p in b["pnls"]:
            eq += p
            pk  = max(pk, eq)
            dd  = min(dd, eq - pk)
        b["max_dd"] = dd

    sorted_slots = sorted(buckets.keys())
    sep = "-" * 62
    logger.info(sep)
    logger.info("  ENTRY TIME BREAKDOWN")
    logger.info(sep)
    logger.info(f"  {'Time':<7} {'Trades':>7} {'W/L':<9} {'WR%':>5} {'Total P&L':>12} {'Max DD':>10}")
    logger.info(sep)
    for slot in sorted_slots:
        b = buckets[slot]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total else 0.0
        wl = f"{b['wins']}W/{b['losses']}L"
        logger.info(f"  {slot:<7} {total:>7} {wl:<9} {wr:>4.1f}% {b['pnl']:>12,.2f} {b['max_dd']:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  SEASONALITY ANALYSIS
# ---------------------------------------------
def print_seasonality_analysis(all_trades: list) -> None:
    """Break down P&L by day-of-week and by month."""
    from collections import defaultdict
    import datetime as _dt_mod

    dow_buckets: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(5)}
    mon_buckets: dict[int, dict] = {i: {"pnl": 0.0, "wins": 0, "losses": 0} for i in range(1, 13)}

    for t in all_trades:
        d = t.get("entry_date", "")
        if len(d) != 8:
            continue
        try:
            dt = _dt_mod.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        except ValueError:
            continue
        dow = dt.weekday()  # 0=Mon … 4=Fri
        mon = dt.month
        pnl = t["pnl_earned"]
        win = bool(t.get("win"))
        dow_buckets[dow]["pnl"]    += pnl
        mon_buckets[mon]["pnl"]    += pnl
        dow_buckets[dow]["wins" if win else "losses"] += 1
        mon_buckets[mon]["wins" if win else "losses"] += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    mon_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    sep = "-" * 56

    logger.info(sep)
    logger.info("  SEASONALITY -- DAY OF WEEK")
    logger.info(sep)
    logger.info(f"  {'Day':<5} {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    logger.info(sep)
    for i, name in enumerate(dow_names):
        b = dow_buckets[i]
        total = b["wins"] + b["losses"]
        wr    = b["wins"] / total * 100 if total else 0.0
        avg   = b["pnl"] / total if total else 0.0
        logger.info(f"  {name:<5} {total:>7} {wr:>5.1f}% {b['pnl']:>12,.2f} {avg:>10,.2f}")
    logger.info(sep)

    logger.info(sep)
    logger.info("  SEASONALITY -- MONTH")
    logger.info(sep)
    logger.info(f"  {'Month':<5} {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    logger.info(sep)
    for i, name in enumerate(mon_names, 1):
        b = mon_buckets[i]
        total = b["wins"] + b["losses"]
        wr    = b["wins"] / total * 100 if total else 0.0
        avg   = b["pnl"] / total if total else 0.0
        logger.info(f"  {name:<5} {total:>7} {wr:>5.1f}% {b['pnl']:>12,.2f} {avg:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  ECON DATE ANALYSIS
# ---------------------------------------------
def print_econ_date_analysis(all_trades: list) -> None:
    """Break down P&L and drawdown for each calendar/econ event type vs normal days."""
    day_pnl: dict[str, float] = {}
    for t in all_trades:
        d = t["entry_date"]
        day_pnl[d] = day_pnl.get(d, 0.0) + t["pnl_earned"]

    def _stats(date_set):
        pnls = [day_pnl[d] for d in day_pnl if d in date_set]
        if not pnls: return 0, 0.0, 0.0, 0.0, 0.0
        wins  = sum(1 for p in pnls if p > 0)
        wr    = wins / len(pnls) * 100
        avg   = sum(pnls) / len(pnls)
        total = sum(pnls)
        eq = pk = dd = 0.0
        for p in pnls: eq += p; pk = max(pk, eq); dd = min(dd, eq - pk)
        return len(pnls), wr, avg, total, dd

    ev = _build_calendar_event_dates()
    all_event_days = set().union(*[v for v in ev.values() if isinstance(v, set)])
    normal_set = {d for d in day_pnl if d not in all_event_days}

    event_types = [
        ("Normal",       normal_set),
        ("CPI",          ev.get("cpi", set())),
        ("PPI",          ev.get("ppi", set())),
        ("PCE",          ev.get("pce", set())),
        ("NFP",          ev.get("nfp", set())),
        ("FOMC",         ev.get("fomc", set())),
        ("Triple Witch", ev.get("triple_witching", set())),
        ("Monthly OPEX", ev.get("monthly_opex", set())),
        ("EOM",          ev.get("end_of_month", set())),
        ("EOQ",          ev.get("end_of_quarter", set())),
        ("Pre-TW",       ev.get("pre_triple_witching", set())),
        ("Post-Holiday", ev.get("post_major_holiday", set())),
        ("Full Moon",    ev.get("full_moon", set())),
    ]

    # Compute stats, skip empty, sort event rows by WR% desc; pin Normal to bottom
    rows_data = []
    normal_row = None
    for label, date_set in event_types:
        n, wr, avg, total, dd = _stats(date_set)
        if n == 0:
            continue
        entry = (label, n, wr, avg, total, dd)
        if label == "Normal":
            normal_row = entry
        else:
            rows_data.append(entry)
    rows_data.sort(key=lambda r: r[2], reverse=True)
    if normal_row:
        rows_data.append(normal_row)

    sep = "-" * 72
    logger.info(sep)
    logger.info("  CALENDAR / ECON EVENT ANALYSIS  (sorted by WR%)")
    logger.info(sep)
    logger.info(f"  {'Event':<14} {'Days':>5} {'WR%':>6} {'Avg/Day':>10} {'Total P&L':>12} {'Max DD':>10}")
    logger.info(sep)
    for label, n, wr, avg, total, dd in rows_data:
        logger.info(f"  {label:<14} {n:>5} {wr:>5.1f}% {avg:>10,.2f} {total:>12,.2f} {dd:>10,.2f}")
    logger.info(sep)


# ---------------------------------------------
#  RUN HISTORY  (persist last N run summaries)
# ---------------------------------------------
def _save_run_summary(all_trades: list, date_list) -> None:
    """Append a summary of this run to RUN_HISTORY_FILE (keeps last 10 entries)."""
    import json, math, collections

    if not all_trades:
        return

    pnls = [t["pnl_earned"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate  = len(wins) / len(pnls) * 100 if pnls else 0.0

    # Max drawdown
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)

    # Daily P&L
    day_pnls: dict = {}
    for t in all_trades:
        day_pnls.setdefault(t["entry_date"], 0.0)
        day_pnls[t["entry_date"]] += t["pnl_earned"]

    # Average monthly P&L
    monthly: dict = collections.defaultdict(float)
    for d_str, pnl in day_pnls.items():
        ym = d_str[:6]   # YYYYMM
        monthly[ym] += pnl
    avg_monthly = sum(monthly.values()) / len(monthly) if monthly else 0.0

    # Sharpe
    rf_daily  = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    dv        = list(day_pnls.values())
    mean_d    = sum(dv) / len(dv) if dv else 0.0
    std_d     = math.sqrt(sum((p - mean_d) ** 2 for p in dv) / len(dv)) if dv else 0.0
    sharpe    = (mean_d - rf_daily) / std_d * math.sqrt(252) if std_d else 0.0
    calmar    = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")

    days_in_range = len(date_list)
    days_traded   = len(day_pnls)

    entry = {
        "timestamp":     _RUN_TS,
        "period":        f"{PILOT_YEAR_START} -> {PILOT_YEAR_END}",
        "days_in_range": days_in_range,
        "days_traded":   days_traded,
        # key params
        "width":         WIDTH,
        "qty":           QTY,
        "min_credit":    MIN_NET_CREDIT,
        "max_credit":    str(MAX_NET_CREDIT),
        "min_otm":       str(MIN_OTM_DISTANCE),
        "direction":     DIRECTION_MODE,
        "entry_window":  f"{ENTRY_START.strftime('%H:%M')}-{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min",
        "dyn_sl":        f"VIX<{DYNAMIC_SL_VIX_LOW} | {DYNAMIC_SL_VIX_MID} | {DYNAMIC_SL_VIX_HIGH}" if ENABLE_DYNAMIC_SL else "off",
        "skip_vix_range": str(SKIP_VIX_RANGE) if SKIP_VIX_RANGE is not None else "off",
        "entry_gate":     f"${INTRADAY_ENTRY_GATE}" if INTRADAY_ENTRY_GATE is not None else "off",
        "buyback_exit":   f"${PREMIUM_BUYBACK_EXIT}" if PREMIUM_BUYBACK_EXIT is not None else "off",
        "kelly_sizing":   f"on -- {KELLY_ZONE_QTY}" if ENABLE_KELLY_SIZING else "off",
        "cal_filter":    f"{sorted(CALENDAR_FILTER_EVENTS)}" if ENABLE_CALENDAR_FILTER else "off",
        # results
        "total_pnl":     round(total_pnl, 2),
        "win_rate_pct":  round(win_rate, 1),
        "total_trades":  len(pnls),
        "avg_monthly_pnl": round(avg_monthly, 2),
        "max_drawdown":  round(max_dd, 2),
        "sharpe":        round(sharpe, 2),
        "calmar":        round(calmar, 2) if calmar != float("inf") else "inf",
        "log_file":      LOG_FILE,
    }

    history = []
    if os.path.exists(RUN_HISTORY_FILE):
        try:
            with open(RUN_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(entry)
    history = history[-10:]   # keep last 10
    with open(RUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    logger.info(f"Run summary saved -> {RUN_HISTORY_FILE}")


def _display_run_history() -> None:
    """Print the last 3 run summaries at the top of each run."""
    import json
    if not os.path.exists(RUN_HISTORY_FILE):
        return
    try:
        with open(RUN_HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        return
    recent = history[-3:]
    if not recent:
        return

    w = 72
    sep  = "═" * w
    sep2 = "-" * w
    logger.info(sep)
    logger.info("  LAST RUN HISTORY  (most recent first)")
    logger.info(sep)
    for entry in reversed(recent):
        ts    = entry.get("timestamp", "?")
        dt    = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}  {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
        pnl   = entry.get("total_pnl", 0)
        dd    = entry.get("max_drawdown", 0)
        wr    = entry.get("win_rate_pct", 0)
        sh    = entry.get("sharpe", 0)
        cal   = entry.get("calmar", 0)
        trades= entry.get("total_trades", 0)
        days  = entry.get("days_traded", 0)
        mpnl  = entry.get("avg_monthly_pnl", 0)
        period= entry.get("period", "")
        dyn   = entry.get("dyn_sl", "?")
        win   = entry.get("entry_window", "?")
        otm   = entry.get("min_otm", "?")
        cal_f = entry.get("cal_filter", "off")
        logger.info(sep2)
        logger.info(f"  {dt}   |   {period}")
        logger.info(
            f"  P&L: ${pnl:>10,.0f}   Max DD: ${dd:>9,.0f}   "
            f"Avg Mo P&L: ${mpnl:>8,.0f}   WR: {wr:.1f}%"
        )
        logger.info(
            f"  Sharpe: {sh:>6.2f}   Calmar: {cal!s:>8}   "
            f"Trades: {trades:>5}   Days traded: {days}"
        )
        logger.info(
            f"  Params -> min_otm={otm}  dyn_sl={dyn}  "
            f"entry={win}  cal_filter={cal_f}"
        )
    logger.info(sep)
    logger.info("")


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


# ---------------------------------------------
#  STOP-LOSS SWEEP RUNNER
# ---------------------------------------------
async def run_sweep():
    """Run a stop-loss parameter sweep.

    For each date the market data is fetched exactly once.
    The simulation is then re-run independently for every level in SWEEP_SL_LEVELS
    using the in-memory quote cache, so no extra API calls are needed.
    Results are written to SWEEP_SAVE_FILE and printed as a summary table.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: STOP-LOSS SWEEP MODE")
    logger.info(f"SL levels : {[_sl_label(sl) for sl in SWEEP_SL_LEVELS]}")
    logger.info(f"Output    : {SWEEP_SAVE_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per SL level
    sweep_trades: dict[str, list] = {_sl_label(sl): [] for sl in SWEEP_SL_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each SL level using the cached quotes
            for sl in SWEEP_SL_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, sl)
                label = _sl_label(sl)
                sweep_trades[label].extend(trades)
                logger.debug(f"  SL={label:>6} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Write summary CSV --
    sweep_cols = [
        "stop_loss", "num_trades", "num_wins", "num_losses",
        "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    with open(SWEEP_SAVE_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sweep_cols)
        w.writeheader()
        for sl in SWEEP_SL_LEVELS:
            label = _sl_label(sl)
            m     = compute_metrics(sweep_trades[label])
            pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
            w.writerow({
                "stop_loss":     label,
                "num_trades":    m["num_trades"],
                "num_wins":      m["num_wins"],
                "num_losses":    m["num_losses"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{m['total_pnl']:.2f}",
                "avg_win":       f"{m['avg_win']:.2f}",
                "avg_loss":      f"{m['avg_loss']:.2f}",
                "profit_factor": pf,
                "max_drawdown":  f"{m['max_drawdown']:.2f}",
            })

    logger.info(f"\nSweep complete -- summary saved to {SWEEP_SAVE_FILE}")

    # -- Print comparison table --
    col_w = 12
    hdr   = (f"{'SL':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "-" * len(hdr)
    logger.info(sep)
    logger.info("  STOP-LOSS SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for sl in SWEEP_SL_LEVELS:
        label = _sl_label(sl)
        m     = compute_metrics(sweep_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"{label:>{col_w}} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"${m['max_drawdown']:>9,.2f} | {pf:>6}"
        )
    logger.info(sep)

    # -- VIX analysis across all sweep trades (combined) --
    all_sweep_trades = []
    for trades in sweep_trades.values():
        all_sweep_trades.extend(trades)
    # Deduplicate by (entry_date, entry_time, short_strike) to avoid counting each trade N times
    seen = set()
    unique_sweep_trades = []
    for t in all_sweep_trades:
        key = (t.get("entry_date"), t.get("entry_time"), t.get("short_strike"))
        if key not in seen:
            seen.add(key)
            unique_sweep_trades.append(t)
    print_vix_analysis(unique_sweep_trades)
    if RUN_STRIKE_DISTANCE_ANALYSIS:
        print_strike_distance_analysis(unique_sweep_trades)
    if RUN_PUT_CALL_ANALYSIS:
        print_put_call_analysis(unique_sweep_trades)


# ---------------------------------------------
#  EMA PARAMETER SWEEP RUNNER
# ---------------------------------------------
async def run_ema_sweep():
    """Run an EMA fast/slow parameter sweep.

    For each date the market data is fetched exactly once (seeded with enough
    bars for the largest slow EMA period).  The simulation is then re-run for
    every (fast, slow) combination against the in-memory quote cache -- no extra
    API calls required.

    Results are written to EMA_SWEEP_SAVE_FILE sorted by total_pnl descending.
    """
    date_list  = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')
    combos     = [(f, s) for f in SWEEP_EMA_FAST for s in SWEEP_EMA_SLOW]
    max_seed   = max(SWEEP_EMA_SLOW)

    logger.info("=" * 70)
    logger.info("MEFT v35: EMA PARAMETER SWEEP MODE")
    logger.info(f"Fast EMA  : {SWEEP_EMA_FAST}")
    logger.info(f"Slow EMA  : {SWEEP_EMA_SLOW}")
    logger.info(f"Combos    : {len(combos)}")
    logger.info(f"Output    : {EMA_SWEEP_SAVE_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per (fast, slow) combo
    ema_trades: dict[tuple, list] = {(f, s): [] for f, s in combos}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once, seeding with enough bars for the largest slow EMA
            day_data = await _fetch_day_data(session, d_str, seed_bars=max_seed)
            if day_data is None:
                continue

            # Re-simulate for each (fast, slow) combo using the cached quotes
            for fast, slow in combos:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, ema_fast=fast, ema_slow=slow)
                ema_trades[(fast, slow)].extend(trades)
                logger.debug(
                    f"  EMA({fast},{slow}) | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows sorted by total_pnl descending --
    ema_sweep_cols = [
        "ema_fast", "ema_slow",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
        "p_value", "p_fdr", "sig_fdr",
    ]

    rows = []
    for fast, slow in combos:
        m  = compute_metrics(ema_trades[(fast, slow)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "ema_fast":      fast,
            "ema_slow":      slow,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
            "p_value":       round(m["p_value"], 4),
            "_total_pnl_num": m["total_pnl"],
        })

    _apply_fdr_to_rows(rows)
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV (drop sort key) --
    with open(EMA_SWEEP_SAVE_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ema_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in ema_sweep_cols})

    logger.info(f"\nEMA sweep complete -- summary saved to {EMA_SWEEP_SAVE_FILE}")

    # -- Print comparison table --
    hdr = (
        f"  {'Fast':>4} | {'Slow':>4} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  EMA PARAMETER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"  {row['ema_fast']:>4} | {row['ema_slow']:>4} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{pf:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MINIMUM NET CREDIT SWEEP RUNNER
# ---------------------------------------------
async def run_min_credit_sweep():
    """Sweep minimum net credit threshold for spread entry.

    Market data is fetched once per day; the simulation is re-run for each
    credit level from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MINIMUM NET CREDIT SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MIN_CREDIT_LEVELS}")
    logger.info(f"Output : {MIN_CREDIT_SWEEP_FILE}")
    logger.info("=" * 70)

    mc_trades: dict[float, list] = {mc: [] for mc in SWEEP_MIN_CREDIT_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for mc in SWEEP_MIN_CREDIT_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, min_credit=mc)
                mc_trades[mc].extend(trades)
                logger.debug(f"  min_credit={mc} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    mc_cols = [
        "min_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    rows = []
    for mc in SWEEP_MIN_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[mc])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in mc_trades[mc]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        rows.append({
            "min_credit":          mc,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MIN_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=mc_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in mc_cols})

    logger.info(f"\nMin credit sweep complete -- saved to {MIN_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MinCredit':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['min_credit']:>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MINIMUM OTM DISTANCE SWEEP RUNNER
# ---------------------------------------------
async def run_min_otm_sweep():
    """Sweep minimum OTM distance (pts) required for spread entry.

    Market data is fetched once per day; the simulation is re-run for each
    OTM floor from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MINIMUM OTM DISTANCE SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MIN_OTM_LEVELS}")
    logger.info(f"Output : {MIN_OTM_SWEEP_FILE}")
    logger.info("=" * 70)

    otm_trades: dict = {str(otm): [] for otm in SWEEP_MIN_OTM_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for otm in SWEEP_MIN_OTM_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    min_otm_distance=otm,
                )
                otm_trades[str(otm)].extend(trades)
                logger.debug(f"  min_otm={otm} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    otm_cols = [
        "min_otm_distance", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for otm in SWEEP_MIN_OTM_LEVELS:
        m  = compute_metrics(otm_trades[str(otm)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "min_otm_distance": "None" if otm is None else otm,
            "num_trades":       m["num_trades"],
            "win_rate_pct":     f"{m['win_rate']:.1f}",
            "total_pnl":        f"{m['total_pnl']:.2f}",
            "avg_win":          f"{m['avg_win']:.2f}",
            "avg_loss":         f"{m['avg_loss']:.2f}",
            "profit_factor":    pf,
            "max_drawdown":     f"{m['max_drawdown']:.2f}",
            "calmar":           f"{calmar:.2f}",
            "_total_pnl_num":   m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MIN_OTM_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=otm_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in otm_cols})

    logger.info(f"\nMin OTM sweep complete -- saved to {MIN_OTM_SWEEP_FILE}")

    hdr = (
        f"  {'MinOTM':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN OTM DISTANCE SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['min_otm_distance']):>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MAX NET CREDIT SWEEP RUNNER
# ---------------------------------------------
async def run_max_credit_sweep():
    """Sweep maximum net credit cap for spread entry.

    Entries where the only qualifying spread exceeds the cap are skipped.
    Market data is fetched once per day and re-used for each level.
    Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MAX NET CREDIT SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MAX_CREDIT_LEVELS}")
    logger.info(f"Output : {MAX_CREDIT_SWEEP_FILE}")
    logger.info("=" * 70)

    mc_trades: dict = {str(mc): [] for mc in SWEEP_MAX_CREDIT_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for mc in SWEEP_MAX_CREDIT_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    max_credit=mc,
                )
                mc_trades[str(mc)].extend(trades)
                logger.debug(f"  max_credit={mc} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "max_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for mc in SWEEP_MAX_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[str(mc)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "max_credit":     "None" if mc is None else mc,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "calmar":         f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MAX_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nMax credit sweep complete -- saved to {MAX_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MaxCredit':>10} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MAX CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['max_credit']):>10} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  DAILY PROFIT TARGET SWEEP RUNNER
# ---------------------------------------------
async def run_daily_tp_sweep():
    """Sweep the intraday daily profit target level.

    Market data is fetched once per day; the simulation is re-run for each
    TP level from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: DAILY PROFIT TARGET SWEEP MODE")
    logger.info(f"Levels : {SWEEP_DAILY_TP_LEVELS}")
    logger.info(f"Output : {DAILY_TP_SWEEP_FILE}")
    logger.info("=" * 70)

    tp_trades: dict = {str(tp): [] for tp in SWEEP_DAILY_TP_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for tp in SWEEP_DAILY_TP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    daily_tp=tp,
                )
                tp_trades[str(tp)].extend(trades)
                logger.debug(f"  daily_tp={tp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    tp_cols = [
        "daily_tp", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for tp in SWEEP_DAILY_TP_LEVELS:
        m  = compute_metrics(tp_trades[str(tp)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "daily_tp":     "None" if tp is None else tp,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(DAILY_TP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tp_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in tp_cols})

    logger.info(f"\nDaily TP sweep complete -- saved to {DAILY_TP_SWEEP_FILE}")

    hdr = (
        f"  {'DailyTP':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  DAILY TP SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['daily_tp']):>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  PNL SAMPLE INTERVAL SWEEP RUNNER
# ---------------------------------------------
async def run_pnl_sample_sweep():
    """Sweep how frequently (in minutes) MTM checks run to evaluate open positions.

    Market data is fetched once per day; the simulation is re-run for each
    interval from the cache. Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: PNL SAMPLE INTERVAL SWEEP MODE")
    logger.info(f"Intervals : {SWEEP_PNL_SAMPLE_INTERVALS} minutes")
    logger.info(f"Output    : {PNL_SAMPLE_SWEEP_FILE}")
    logger.info("=" * 70)

    interval_trades: dict[int, list] = {iv: [] for iv in SWEEP_PNL_SAMPLE_INTERVALS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for iv in SWEEP_PNL_SAMPLE_INTERVALS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    pnl_sample_interval=iv,
                )
                interval_trades[iv].extend(trades)
                logger.debug(f"  pnl_sample={iv}m | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "pnl_sample_interval_min", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for iv in SWEEP_PNL_SAMPLE_INTERVALS:
        m  = compute_metrics(interval_trades[iv])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "pnl_sample_interval_min": iv,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{calmar:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(PNL_SAMPLE_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nPnL sample interval sweep complete -- saved to {PNL_SAMPLE_SWEEP_FILE}")

    hdr = (
        f"  {'Interval':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PNL SAMPLE INTERVAL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['pnl_sample_interval_min']) + 'min':>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  MAX BUYING POWER SWEEP RUNNER
# ---------------------------------------------
async def run_max_bp_sweep():
    """Sweep max total committed buying power across open positions.

    At each entry, qty is reduced so (committed_bp + new_bp) stays within limit.
    BP per contract = (spread_width - credit) × 100.
    Data is fetched once per day; each BP level is re-simulated from cache.
    Results sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: MAX BUYING POWER SWEEP MODE")
    logger.info(f"Levels : {SWEEP_MAX_BP_LEVELS}")
    logger.info(f"Output : {MAX_BP_SWEEP_FILE}")
    logger.info("=" * 70)

    bp_trades: dict = {str(bp): [] for bp in SWEEP_MAX_BP_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for bp in SWEEP_MAX_BP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    max_buying_power=bp,
                )
                bp_trades[str(bp)].extend(trades)
                logger.debug(f"  max_bp={bp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build result rows --
    cols = [
        "max_bp", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
        "avg_qty",
    ]
    rows = []
    for bp in SWEEP_MAX_BP_LEVELS:
        trades = bp_trades[str(bp)]
        m   = compute_metrics(trades)
        pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
        avg_qty = sum(float(t.get("qty", 0)) for t in trades) / len(trades) if trades else 0.0
        rows.append({
            "max_bp":       "None" if bp is None else bp,
            "num_trades":   m["num_trades"],
            "win_rate_pct": f"{m['win_rate']:.1f}",
            "total_pnl":    f"{m['total_pnl']:.2f}",
            "avg_win":      f"{m['avg_win']:.2f}",
            "avg_loss":     f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown": f"{m['max_drawdown']:.2f}",
            "calmar":       f"{cal:.2f}",
            "avg_qty":      f"{avg_qty:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    with open(MAX_BP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nMax BP sweep complete -- saved to {MAX_BP_SWEEP_FILE}")

    hdr = (
        f"  {'Max BP':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8} | {'Avg Qty':>7}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MAX BUYING POWER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {str(row['max_bp']):>8} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8} | {float(row['avg_qty']):>7.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  TOUCH EXIT SWEEP RUNNER
# ---------------------------------------------
async def run_touch_sweep():
    """Sweep Touch Exit thresholds -- both dollar and % variants.

    Dollar touch: close PUT when curr_price <= short_strike + touch_dollars
                  close CALL when curr_price >= short_strike - touch_dollars
    Percent touch: close PUT when curr_price <= short_strike * (1 + touch_pct/100)
                   close CALL when curr_price >= short_strike * (1 - touch_pct/100)

    Positive values = OTM buffer (exit before ITM).
    Zero = exit at-the-money.
    Negative = allow going ITM by that amount before exiting.
    None (baseline) = no touch exit, hold to daily_tp / expiration.

    Data is fetched once per day; each threshold is re-simulated from cache.
    Results sorted by Calmar descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    # Build the full list: dollar levels + pct levels + None baseline
    dollar_keys = [("$", v) for v in SWEEP_TOUCH_DOLLARS]
    pct_keys    = [("%", v) for v in SWEEP_TOUCH_PCT]
    all_keys    = [("none", None)] + dollar_keys + pct_keys  # "none" = baseline

    trade_map: dict = {str(k): [] for k in all_keys}

    logger.info("=" * 70)
    logger.info("MEFT v35: TOUCH EXIT SWEEP MODE")
    logger.info(f"Dollar levels : {SWEEP_TOUCH_DOLLARS}")
    logger.info(f"Pct    levels : {SWEEP_TOUCH_PCT}")
    logger.info(f"Output        : {TOUCH_SWEEP_FILE}")
    logger.info("=" * 70)

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for kind, val in all_keys:
                if kind == "none":
                    t_dollars, t_pct = None, None
                elif kind == "$":
                    t_dollars, t_pct = val, None
                else:
                    t_dollars, t_pct = None, val

                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode=DIRECTION_MODE,
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    touch_exit_dollars=t_dollars,
                    touch_exit_pct=t_pct,
                )
                trade_map[str((kind, val))].extend(trades)

    # -- Build result rows --
    cols = [
        "touch_type", "touch_value",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]
    rows = []
    for kind, val in all_keys:
        trades = trade_map[str((kind, val))]
        m   = compute_metrics(trades)
        pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
        rows.append({
            "touch_type":    kind,
            "touch_value":   "None" if val is None else val,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
            "calmar":        f"{cal:.2f}",
            "_calmar_num":   cal,
        })
    rows.sort(key=lambda r: r["_calmar_num"], reverse=True)

    with open(TOUCH_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nTouch exit sweep complete -- saved to {TOUCH_SWEEP_FILE}")

    hdr = (
        f"  {'Type':>6} | {'Value':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TOUCH EXIT SWEEP RESULTS  (sorted by Calmar)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['touch_type']:>6} | {str(row['touch_value']):>8} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>5}% | "
            f"${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  DAILY BAR INDICATOR FILTER SWEEP RUNNER
# ---------------------------------------------
_DAY_FILTER_CHECKPOINT = os.path.join(LOGS_DIR, "meds_day_filter_sweep_checkpoint.csv")


async def run_day_filter_sweep():
    """Test each daily indicator as an independent day-selection filter.

    Data is fetched once per day (respecting VIX and other global filters).
    For each (indicator, threshold) combo the same day pool is re-filtered and
    re-simulated. Results sorted by Calmar descending within each indicator group.
    """
    # -- Suppress DEBUG during sweep to avoid log bloat and I/O pressure --
    _saved_log_level = logger.level
    logger.setLevel(logging.INFO)

    global _DAILY_INDICATORS
    if not _DAILY_INDICATORS:
        logger.info("Building daily indicator table from local parquets (full mode for sweep)...")
        _DAILY_INDICATORS = _build_daily_indicators(compute_full=True)
        logger.info(f"  -> {len(_DAILY_INDICATORS)} daily bars loaded")

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: DAILY BAR INDICATOR FILTER SWEEP MODE")
    logger.info(f"Filters   : {list(SWEEP_DAY_FILTERS.keys())}")
    logger.info(f"Output    : {DAY_FILTER_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Resume: load already-completed combos from checkpoint --
    _completed: set[tuple] = set()
    _checkpoint_rows: list[dict] = []
    if os.path.exists(_DAY_FILTER_CHECKPOINT):
        with open(_DAY_FILTER_CHECKPOINT, newline="") as _f:
            for _row in csv.DictReader(_f):
                _completed.add((_row["filter_name"], _row["threshold"]))
                _checkpoint_rows.append(_row)
        logger.info(f"Resuming -- loaded {len(_checkpoint_rows)} completed combos from checkpoint.")

    # -- Step 1: Pre-fetch all days with VIX filter OFF so vix_max can be swept --
    # We temporarily disable the global VIX filters so every trading day enters
    # the pool. The vix_max sweep filter then applies VIX thresholds in the loop.
    global VIX_MAX_FILTER, VIX_MIN_FILTER
    _saved_vix_max = VIX_MAX_FILTER
    _saved_vix_min = VIX_MIN_FILTER
    VIX_MAX_FILTER = None
    VIX_MIN_FILTER = None

    day_pool: dict[str, dict] = {}   # date_str -> day_data
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data

    VIX_MAX_FILTER = _saved_vix_max
    VIX_MIN_FILTER = _saved_vix_min
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days (VIX filter suspended during fetch).")

    async with _get_session() as session:
        # -- Step 2: Baseline (no extra filter) --
        baseline_trades = []
        for d_str, day_data in day_pool.items():
            trades, _ = await _simulate_day(
                session, day_data, DAILY_SL,
                baseline_mode=DIRECTION_MODE,
                spread_width=WIDTH,
                min_credit=MIN_NET_CREDIT,
                entry_start=ENTRY_START,
                entry_end=ENTRY_END,
                entry_interval=ENTRY_INTERVAL,
            )
            baseline_trades.extend(trades)

        # -- Step 3: For each filter × threshold, re-filter the day pool and simulate --
        cols = [
            "filter_name", "threshold", "days_traded", "days_filtered_out",
            "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
        ]
        rows = []

        # Open checkpoint file for appending (write header only if starting fresh)
        _ckpt_is_new = not os.path.exists(_DAY_FILTER_CHECKPOINT)
        _ckpt_f = open(_DAY_FILTER_CHECKPOINT, "a", newline="")
        _ckpt_w = csv.DictWriter(_ckpt_f, fieldnames=cols)
        if _ckpt_is_new:
            _ckpt_w.writeheader()

        # Baseline row (skip if already in checkpoint)
        if ("BASELINE (no filter)", "--") not in _completed:
            bm = compute_metrics(baseline_trades)
            bpf = f"{bm['profit_factor']:.2f}" if bm['profit_factor'] != float("inf") else "inf"
            bcalmar = bm["total_pnl"] / abs(bm["max_drawdown"]) if bm["max_drawdown"] != 0 else float("inf")
            _baseline_row = {
                "filter_name":       "BASELINE (no filter)",
                "threshold":         "--",
                "days_traded":       len(day_pool),
                "days_filtered_out": 0,
                "num_trades":        bm["num_trades"],
                "win_rate_pct":      f"{bm['win_rate']:.1f}",
                "total_pnl":         f"{bm['total_pnl']:.2f}",
                "avg_win":           f"{bm['avg_win']:.2f}",
                "avg_loss":          f"{bm['avg_loss']:.2f}",
                "profit_factor":     bpf,
                "max_drawdown":      f"{bm['max_drawdown']:.2f}",
                "calmar":            f"{bcalmar:.2f}",
                "_calmar_num":       bcalmar,
                "_filter_sort":      "000_BASELINE",
            }
            rows.append(_baseline_row)
            _ckpt_w.writerow({k: _baseline_row[k] for k in cols})
            _ckpt_f.flush()

        for filter_name, thresholds in SWEEP_DAY_FILTERS.items():
            for threshold in thresholds:
                if (filter_name, str(threshold)) in _completed:
                    logger.info(
                        f"  {filter_name:<22} threshold={str(threshold):<8} "
                        f"[skipped -- already in checkpoint]"
                    )
                    continue

                filtered_trades = []
                days_in = 0
                days_out = 0
                _is_intraday = filter_name in (
                    "priceChgPct_max", "priceChgPct_min",
                    "priceChgSD_max",  "priceChgSD_min",
                    "openChgPct_max",  "openChgPct_min",
                )
                for d_str, day_data in day_pool.items():
                    if not _is_intraday and not _passes_filter(filter_name, threshold, d_str, day_data):
                        days_out += 1
                        continue
                    days_in += 1
                    trades, _ = await _simulate_day(
                        session, day_data, DAILY_SL,
                        baseline_mode=DIRECTION_MODE,
                        spread_width=WIDTH,
                        min_credit=MIN_NET_CREDIT,
                        entry_start=ENTRY_START,
                        entry_end=ENTRY_END,
                        entry_interval=ENTRY_INTERVAL,
                        price_chg_pct_max=threshold if filter_name == "priceChgPct_max" else None,
                        price_chg_pct_min=threshold if filter_name == "priceChgPct_min" else None,
                        price_chg_sd_max=threshold if filter_name == "priceChgSD_max" else None,
                        price_chg_sd_min=threshold if filter_name == "priceChgSD_min" else None,
                        open_chg_pct_max=threshold if filter_name == "openChgPct_max" else None,
                        open_chg_pct_min=threshold if filter_name == "openChgPct_min" else None,
                    )
                    filtered_trades.extend(trades)

                m   = compute_metrics(filtered_trades)
                pf  = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
                cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] != 0 else float("inf")
                _row = {
                    "filter_name":       filter_name,
                    "threshold":         str(threshold),
                    "days_traded":       days_in,
                    "days_filtered_out": days_out,
                    "num_trades":        m["num_trades"],
                    "win_rate_pct":      f"{m['win_rate']:.1f}",
                    "total_pnl":         f"{m['total_pnl']:.2f}",
                    "avg_win":           f"{m['avg_win']:.2f}",
                    "avg_loss":          f"{m['avg_loss']:.2f}",
                    "profit_factor":     pf,
                    "max_drawdown":      f"{m['max_drawdown']:.2f}",
                    "calmar":            f"{cal:.2f}",
                    "_calmar_num":       cal,
                    "_filter_sort":      filter_name,
                }
                rows.append(_row)
                _ckpt_w.writerow({k: _row[k] for k in cols})
                _ckpt_f.flush()
                logger.info(
                    f"  {filter_name:<22} threshold={str(threshold):<8} "
                    f"days={days_in:>3} | trades={m['num_trades']:>5} | "
                    f"pnl=${m['total_pnl']:>10,.2f} | calmar={cal:>7.2f}"
                )

        _ckpt_f.close()

    # -- Merge checkpoint rows (from prior runs) with newly computed rows --
    # Rebuild _calmar_num / _filter_sort for checkpoint rows so the summary works.
    for _cr in _checkpoint_rows:
        try:
            _cn = float(_cr["calmar"]) if _cr["calmar"] not in ("inf", "-inf") else float(_cr["calmar"].replace("inf", "inf"))
        except (ValueError, KeyError):
            _cn = 0.0
        _cr["_calmar_num"] = _cn
        _cr["_filter_sort"] = _cr["filter_name"] if _cr["filter_name"] != "BASELINE (no filter)" else "000_BASELINE"
    all_rows = _checkpoint_rows + rows

    cols = [
        "filter_name", "threshold", "days_traded", "days_filtered_out",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
    ]

    # -- Save final CSV --
    with open(DAY_FILTER_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in all_rows:
            w.writerow({k: row[k] for k in cols})

    # -- Delete checkpoint -- clean finish --
    if os.path.exists(_DAY_FILTER_CHECKPOINT):
        os.remove(_DAY_FILTER_CHECKPOINT)

    logger.info(f"\nDay filter sweep complete -- saved to {DAY_FILTER_SWEEP_FILE}")
    logger.setLevel(_saved_log_level)

    # -- Print grouped summary (best threshold per indicator by Calmar) --
    hdr = (
        f"  {'Filter':<22} | {'Threshold':>10} | {'Days':>5} | {'Trades':>7} | "
        f"{'WR%':>6} | {'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  DAY FILTER SWEEP -- BEST THRESHOLD PER INDICATOR (by Calmar)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    # Group by filter, pick best Calmar per group
    best: dict[str, dict] = {}
    for row in all_rows:
        fn = row["_filter_sort"]
        if fn not in best or row["_calmar_num"] > best[fn]["_calmar_num"]:
            best[fn] = row
    for row in sorted(best.values(), key=lambda r: r["_calmar_num"], reverse=True):
        logger.info(
            f"  {row['filter_name']:<22} | {row['threshold']:>10} | {row['days_traded']:>5} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>5}% | "
            f"${float(row['total_pnl']):>11,.2f} | ${float(row['max_drawdown']):>9,.2f} | "
            f"{row['calmar']:>8}"
        )
    logger.info(sep)


# ---------------------------------------------
#  CALENDAR EVENT FILTER SWEEP RUNNER
# ---------------------------------------------
async def run_calendar_event_sweep():
    """Test P&L impact of skipping each calendar event type independently.

    Pre-fetches all trading days (ECON_FILTER suspended so the full pool is
    available). Baseline = trade every day. For each event type the days in
    that set are removed from the pool and the full backtest is re-simulated.
    A positive pnl_delta means skipping those days *improves* P&L.
    """
    global ENABLE_ECON_FILTER

    event_sets = _build_calendar_event_dates()

    logger.info("=" * 70)
    logger.info("MEFT v35: CALENDAR EVENT FILTER SWEEP MODE")
    logger.info(f"Events  : {list(event_sets.keys())}")
    logger.info(f"Output  : {CALENDAR_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Step 1: Pre-fetch all days --
    # Suspend ECON_FILTER and temporarily remove FOMC + TW from MARKET_HOLIDAYS
    # so those dates enter the pool and can be tested as event filters.
    global MARKET_HOLIDAYS, ENABLE_ECON_FILTER
    _saved_econ      = ENABLE_ECON_FILTER
    _saved_holidays  = MARKET_HOLIDAYS
    ENABLE_ECON_FILTER = False
    # Strip out FOMC and TW so we can fetch and test them
    _fomc_set = {
        "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
        "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
        "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
        "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
        "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
    }
    _tw_set = {
        "20220318","20220617","20220916","20221216",
        "20230317","20230616","20230915","20231215",
        "20240315","20240621","20240920","20241220",
        "20250321","20250620","20250919","20251219",
        "20260320","20260619","20260918","20261218",
    }
    MARKET_HOLIDAYS = _saved_holidays - _fomc_set - _tw_set

    date_list  = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data

    ENABLE_ECON_FILTER = _saved_econ
    MARKET_HOLIDAYS    = _saved_holidays
    logger.info(
        f"Pre-fetched {len(day_pool)} qualifying days "
        f"(ECON_FILTER + FOMC + TW suspended during fetch)."
    )

    cols = [
        "event_type", "days_in_pool", "days_skipped", "days_traded",
        "num_trades", "win_rate_pct", "total_pnl", "pnl_delta",
        "max_drawdown", "calmar", "sharpe", "sortino",
    ]
    rows = []

    async def _sim(session, d_str: str, day_data: dict):
        """Simulate one day with full current settings (dynamic SL, all params)."""
        effective_sl = _get_effective_sl(day_data, d_str)
        return await _simulate_day(
            session, day_data, effective_sl,
            baseline_mode=_get_baseline_mode(d_str),
            pos_trail_activation=POS_TRAIL_ACTIVATION,
            pos_trail_pullback=POS_TRAIL_PULLBACK,
            min_otm_distance=MIN_OTM_DISTANCE,
            max_credit=MAX_NET_CREDIT,
        )

    async with _get_session() as session:
        # -- Step 2: Baseline (no calendar filter, full settings) --
        baseline_trades: list = []
        for d_str, day_data in day_pool.items():
            trades, _ = await _sim(session, d_str, day_data)
            baseline_trades.extend(trades)
        bm       = compute_metrics(baseline_trades)
        base_pnl = bm["total_pnl"]
        base_dd  = bm["max_drawdown"]
        base_cal = base_pnl / abs(base_dd) if base_dd != 0 else float("inf")

        rows.append({
            "event_type":    "BASELINE (no filter)",
            "days_in_pool":  len(day_pool),
            "days_skipped":  0,
            "days_traded":   len(day_pool),
            "num_trades":    bm["num_trades"],
            "win_rate_pct":  f"{bm['win_rate']:.1f}",
            "total_pnl":     f"{base_pnl:.2f}",
            "pnl_delta":     "--",
            "max_drawdown":  f"{base_dd:.2f}",
            "calmar":        f"{base_cal:.2f}",
            "sharpe":        f"{bm['sharpe']:.2f}",
            "sortino":       f"{bm['sortino']:.2f}",
            "_pnl_num":      base_pnl,
            "_cal_num":      base_cal,
        })
        logger.info(
            f"  {'BASELINE':<20} | skipped={0:>3} | days={len(day_pool):>4} | "
            f"trades={bm['num_trades']:>5} | pnl=${base_pnl:>10,.2f} | calmar={base_cal:.2f}"
        )

        # -- Step 3: For each event type, remove those dates and re-simulate --
        for event_name, event_dates in event_sets.items():
            in_pool       = {d for d in event_dates if d in day_pool}
            skip_count    = len(in_pool)
            filtered_pool = {d: v for d, v in day_pool.items() if d not in in_pool}

            filtered_trades: list = []
            for d_str, day_data in filtered_pool.items():
                trades, _ = await _sim(session, d_str, day_data)
                filtered_trades.extend(trades)

            m     = compute_metrics(filtered_trades)
            pnl   = m["total_pnl"]
            dd    = m["max_drawdown"]
            cal   = pnl / abs(dd) if dd != 0 else float("inf")
            delta = pnl - base_pnl

            rows.append({
                "event_type":   event_name,
                "days_in_pool": len(day_pool),
                "days_skipped": skip_count,
                "days_traded":  len(filtered_pool),
                "num_trades":   m["num_trades"],
                "win_rate_pct": f"{m['win_rate']:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    f"{delta:+.2f}",
                "max_drawdown": f"{dd:.2f}",
                "calmar":       f"{cal:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
                "sortino":      f"{m['sortino']:.2f}",
                "_pnl_num":     pnl,
                "_cal_num":     cal,
            })
            sign = "▲" if delta >= 0 else "▼"
            logger.info(
                f"  {event_name:<20} | skipped={skip_count:>3} | days={len(filtered_pool):>4} | "
                f"trades={m['num_trades']:>5} | pnl=${pnl:>10,.2f} | "
                f"delta={sign}${abs(delta):>8,.2f} | calmar={cal:.2f}"
            )

    # -- Save CSV --
    with open(CALENDAR_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in cols})
    logger.info(f"\nCalendar event sweep saved -> {CALENDAR_SWEEP_FILE}")

    # -- Print summary table sorted by P&L delta --
    data_rows = [r for r in rows if r["pnl_delta"] != "--"]
    data_rows.sort(key=lambda r: float(r["pnl_delta"]), reverse=True)

    hdr = (
        f"  {'Event Type':<20} | {'Skipped':>7} | {'Traded':>6} | "
        f"{'Trades':>7} | {'WR%':>5} | {'Total P&L':>12} | "
        f"{'Delta':>11} | {'Max DD':>10} | {'Calmar':>8} | {'Sharpe':>7}"
    )
    sep = "-" * len(hdr)
    logger.info("")
    logger.info(sep)
    logger.info("  CALENDAR EVENT SWEEP -- sorted by P&L delta (positive = skipping helps)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    # Print baseline first
    br = rows[0]
    logger.info(
        f"  {br['event_type']:<20} | {'--':>7} | {br['days_traded']:>6} | "
        f"{br['num_trades']:>7} | {br['win_rate_pct']:>4}% | "
        f"${float(br['total_pnl']):>11,.2f} | {'--':>11} | "
        f"${float(br['max_drawdown']):>9,.2f} | {br['calmar']:>8} | {br['sharpe']:>7}"
    )
    logger.info(sep)
    for row in data_rows:
        delta_val = float(row["pnl_delta"])
        sign = "▲ +" if delta_val >= 0 else "▼ "
        logger.info(
            f"  {row['event_type']:<20} | {row['days_skipped']:>7} | {row['days_traded']:>6} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {sign}${abs(delta_val):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info(sep)


# ---------------------------------------------
#  VIX MAGNITUDE FILTER SWEEP RUNNER
# ---------------------------------------------
async def run_vix_mag_sweep():
    """Sweep minimum |dVixChgPct| required to trade a day.

    Days where the overnight VIX change is smaller than the threshold are skipped
    entirely -- the directional signal is too weak. Baseline = threshold 0.0 (trade all days).
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX MAGNITUDE FILTER SWEEP")
    logger.info(f"Thresholds : {VIX_MAG_THRESHOLDS}")
    logger.info(f"Output     : {VIX_MAG_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all qualifying days --
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["min_vix_chg_pct", "days_traded", "days_skipped", "num_trades",
            "win_rate_pct", "total_pnl", "pnl_delta", "max_drawdown",
            "dd_delta", "calmar", "sharpe", "sortino"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for threshold in VIX_MAG_THRESHOLDS:
            all_trades: list = []
            skipped = 0
            for d_str, day_data in day_pool.items():
                ind = _DAILY_INDICATORS.get(d_str) or {}
                chg = ind.get("dVixChgPct")
                if chg is not None and abs(chg) < threshold:
                    skipped += 1
                    continue
                effective_sl = _get_effective_sl(day_data, d_str)
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if effective_sl is not None else PNL_SAMPLE_INTERVAL
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=_get_baseline_mode(d_str),
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "min_vix_chg_pct": f"{threshold:.2f}",
                "days_traded":     len(day_pool) - skipped,
                "days_skipped":    skipped,
                "num_trades":      m["num_trades"],
                "win_rate_pct":    f"{m['win_rate']:.1f}",
                "total_pnl":       f"{pnl:.2f}",
                "pnl_delta":       pnl_delta,
                "max_drawdown":    f"{dd:.2f}",
                "dd_delta":        dd_delta,
                "calmar":          f"{calmar:.2f}",
                "sharpe":          f"{m['sharpe']:.2f}",
                "sortino":         f"{m['sortino']:.2f}",
            })
            logger.info(
                f"  threshold={threshold:.2f}% | skipped={skipped:>3} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    # Save CSV
    with open(VIX_MAG_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Print summary table
    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  VIX MAGNITUDE FILTER SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'MIN|dVIX%|':>10} | {'TRADED':>6} | {'SKIPPED':>7} | {'TRADES':>7} | "
                f"{'WIN%':>5} | {'TOTAL_PNL':>12} | {'PNL_DELTA':>10} | "
                f"{'MAX_DD':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['min_vix_chg_pct']:>10} | {row['days_traded']:>6} | {row['days_skipped']:>7} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {VIX_MAG_SWEEP_FILE}")


# ---------------------------------------------
#  GAP-DOWN CALL SL SWEEP RUNNER
# ---------------------------------------------
async def run_gap_call_sl_sweep():
    """Sweep SL levels applied only on gap-down CALL days.

    Gap-down + CALL days are the weakest combo ($369/day, 89.7% WR vs 93%+ baseline).
    Theory: intraday gap-fill rallies threaten CALL strikes on bearish opens.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: GAP-DOWN CALL SL SWEEP")
    logger.info(f"Levels  : {GAP_CALL_SL_LEVELS}")
    logger.info(f"Output  : {GAP_CALL_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days --
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    # Count gap-down CALL days for reference
    gap_down_call_days = sum(
        1 for d_str in day_pool
        if (_DAILY_INDICATORS.get(d_str) or {}).get("dGapPercent", 0) < 0
        and _get_baseline_mode(d_str) == "always_call"
    )
    logger.info(f"Gap-down CALL days in pool: {gap_down_call_days}")

    cols = ["gap_call_sl", "gap_down_call_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in GAP_CALL_SL_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                ind       = _DAILY_INDICATORS.get(d_str) or {}
                gap_pct   = ind.get("dGapPercent", 0) or 0
                direction = _get_baseline_mode(d_str)
                is_gap_down_call = (gap_pct < 0) and (direction == "always_call")

                if is_gap_down_call and sl_level is not None:
                    # Use test SL; still respect existing dynamic SL if it's tighter
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    effective_sl = sl_level if dyn_sl is None else max(sl_level, dyn_sl)
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "gap_call_sl":        label,
                "gap_down_call_days": gap_down_call_days,
                "num_trades":         m["num_trades"],
                "win_rate_pct":       f"{m['win_rate']:.1f}",
                "total_pnl":          f"{pnl:.2f}",
                "pnl_delta":          pnl_delta,
                "max_drawdown":       f"{dd:.2f}",
                "dd_delta":           dd_delta,
                "calmar":             f"{calmar:.2f}",
                "sharpe":             f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  gap_call_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(GAP_CALL_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  GAP-DOWN CALL SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['gap_call_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {GAP_CALL_SL_SWEEP_FILE}")


# ---------------------------------------------
#  VIX SUB-12 TIGHTER SL SWEEP RUNNER
# ---------------------------------------------
async def run_vix_sub12_sl_sweep():
    """Sweep tighter SL levels applied exclusively on days where VIX < 12.

    Jun 2024 (VIX 11.95-12.03) saw 4 straight 0% win-rate days driving most of
    the month's -$4,660 loss. The existing dyn SL (-$500) kicked in but wasn't
    tight enough. Tests whether a tighter per-trade SL on extreme low-VIX days
    improves risk-adjusted returns without hurting the wider backtest.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX SUB-12 TIGHTER SL SWEEP")
    logger.info(f"VIX threshold : VIX < {VIX_SUB12_THRESHOLD}")
    logger.info(f"Levels        : {VIX_SUB12_SL_LEVELS}")
    logger.info(f"Output        : {VIX_SUB12_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days --
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    vix_sub12_days = sum(
        1 for d_str, dd in day_pool.items()
        if (dd.get("vix_level") or 99) < VIX_SUB12_THRESHOLD
    )
    logger.info(f"VIX < {VIX_SUB12_THRESHOLD} days in pool: {vix_sub12_days}")

    cols = ["vix_sub12_sl", "vix_sub12_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in VIX_SUB12_SL_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                vix = day_data.get("vix_level") or 99
                is_sub12 = vix < VIX_SUB12_THRESHOLD

                if is_sub12 and sl_level is not None:
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    # use whichever is tighter (less negative = closer to zero = triggers sooner)
                    effective_sl = max(sl_level, dyn_sl) if dyn_sl is not None else sl_level
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "vix_sub12_sl":   label,
                "vix_sub12_days": vix_sub12_days,
                "num_trades":     m["num_trades"],
                "win_rate_pct":   f"{m['win_rate']:.1f}",
                "total_pnl":      f"{pnl:.2f}",
                "pnl_delta":      pnl_delta,
                "max_drawdown":   f"{dd:.2f}",
                "dd_delta":       dd_delta,
                "calmar":         f"{calmar:.2f}",
                "sharpe":         f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  vix_sub12_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(VIX_SUB12_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  VIX SUB-12 SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['vix_sub12_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {VIX_SUB12_SL_SWEEP_FILE}")


# ---------------------------------------------
#  EOM SL SWEEP RUNNER
# ---------------------------------------------
async def run_eom_sl_sweep():
    """Sweep tighter SL levels applied exclusively on the last trading day of each month.

    EOM days have 74% WR and $105/day avg vs 93%+ WR and $618/day for normal days.
    The 4 largest single-day losses all fell on EOM dates. Tests whether a tighter
    daily SL on EOM days improves risk-adjusted returns without hurting normal days.
    All other days use standard _get_effective_sl() unchanged.
    """
    logger.info("=" * 70)
    logger.info("MEDS: EOM SL SWEEP")
    logger.info(f"EOM days      : {len(_EOM_DATES)}")
    logger.info(f"Levels        : {EOM_SL_SWEEP_LEVELS}")
    logger.info(f"Output        : {EOM_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    eom_in_pool = sum(1 for d_str in day_pool if d_str in _EOM_DATES)
    logger.info(f"EOM days in pool: {eom_in_pool}")

    cols = ["eom_sl", "eom_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in EOM_SL_SWEEP_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                is_eom = d_str in _EOM_DATES

                if is_eom and sl_level is not None:
                    dyn_sl = _get_effective_sl(day_data, d_str)
                    effective_sl = max(sl_level, dyn_sl) if dyn_sl is not None else sl_level
                else:
                    effective_sl = _get_effective_sl(day_data, d_str)

                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "eom_sl":       label,
                "eom_days":     eom_in_pool,
                "num_trades":   m["num_trades"],
                "win_rate_pct": f"{m['win_rate']:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  eom_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(EOM_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 105
    logger.info("")
    logger.info("═" * 105)
    logger.info("  EOM SL SWEEP RESULTS")
    logger.info("═" * 105)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['eom_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 105)
    logger.info(f"  Full results: {EOM_SL_SWEEP_FILE}")


async def run_per_pos_sl_sweep():
    """Sweep per-position fixed SL thresholds to test Option 3d.

    Closes an individual position when its own MTM loss exceeds the threshold,
    independent of total daily P&L. Tests whether capping single-position losses
    (e.g. the -$2,834 position on 2023-10-09) improves overall metrics.
    None = baseline (no per-position SL).
    """
    logger.info("=" * 70)
    logger.info("MEDS: PER-POSITION FIXED SL SWEEP")
    logger.info(f"Levels        : {PER_POS_SL_SWEEP_LEVELS}")
    logger.info(f"Output        : {PER_POS_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["per_pos_sl", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for sl_level in PER_POS_SL_SWEEP_LEVELS:
            label = "none" if sl_level is None else str(sl_level)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    per_pos_sl=sl_level,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "per_pos_sl":   label,
                "num_trades":   len(all_trades),
                "win_rate_pct": f"{wr:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(f"  SL={label:>6}  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(PER_POS_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info(f"  {'SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['per_pos_sl']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {PER_POS_SL_SWEEP_FILE}")


# ---------------------------------------------
#  SL GAP RE-ENTRY SWEEP RUNNER
# ---------------------------------------------
async def run_sl_gap_sweep():
    """Sweep SL gap re-entry cooldown periods: None (baseline), 30, 60, 90, 120 minutes.

    None = current behaviour (stopped_today forever after batch SL).
    30/60/90/120 = wait that many minutes after SL fires, then allow re-entry.
    """
    logger.info("=" * 70)
    logger.info("MEDS: SL GAP RE-ENTRY SWEEP")
    logger.info(f"Levels (min)  : {SL_GAP_SWEEP_LEVELS}  (None=baseline/no-reentry)")
    logger.info(f"Output        : {SL_GAP_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["gap_minutes", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for gap in SL_GAP_SWEEP_LEVELS:
            label = "none" if gap is None else str(gap)
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    sl_gap_minutes=gap,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "gap_minutes":  label,
                "num_trades":   len(all_trades),
                "win_rate_pct": f"{wr:.1f}",
                "total_pnl":    f"{pnl:.2f}",
                "pnl_delta":    pnl_delta,
                "max_drawdown": f"{dd:.2f}",
                "dd_delta":     dd_delta,
                "calmar":       f"{calmar:.2f}",
                "sharpe":       f"{m['sharpe']:.2f}",
            })
            logger.info(f"  gap={label:>4} min  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(SL_GAP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info(f"  {'GAP(min)':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['gap_minutes']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {SL_GAP_SWEEP_FILE}")


# ---------------------------------------------
#  VIX-RANGE ENTRY CUTOFF SWEEP RUNNER
# ---------------------------------------------
async def run_vix_entry_cutoff_sweep():
    """Sweep earlier entry window cutoffs applied only on VIX LO-HI days (Option 3b).

    On VIX 15-20 days the market often drifts with positions during the morning entry
    window then reverses in the afternoon. Tests whether stopping new entries earlier
    on those days reduces late-day losses without hurting the rest of the backtest.
    Non-VIX-15-20 days always use the global ENTRY_END (12:45).
    None = use global ENTRY_END for VIX 15-20 days too (baseline).
    """
    logger.info("=" * 70)
    logger.info("MEDS: VIX-RANGE ENTRY CUTOFF SWEEP")
    logger.info(f"VIX range     : {VIX_ENTRY_CUTOFF_VIX_LO}-{VIX_ENTRY_CUTOFF_VIX_HI}")
    logger.info(f"Cutoff times  : {[t.strftime('%H:%M') if t else 'None' for t in VIX_ENTRY_CUTOFF_SWEEP_TIMES]}")
    logger.info(f"Output        : {VIX_ENTRY_CUTOFF_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["cutoff_time", "vix_days_affected", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for cutoff in VIX_ENTRY_CUTOFF_SWEEP_TIMES:
            label = cutoff.strftime("%H:%M") if cutoff else "none"
            all_trades: list = []
            vix_days_affected = 0

            for d_str, day_data in day_pool.items():
                vix_level = day_data.get("vix_level")
                in_vix_range = (
                    vix_level is not None
                    and VIX_ENTRY_CUTOFF_VIX_LO <= vix_level < VIX_ENTRY_CUTOFF_VIX_HI
                )
                effective_entry_end = cutoff if (cutoff is not None and in_vix_range) else None
                if in_vix_range and cutoff is not None:
                    vix_days_affected += 1

                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger    = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction    = _get_baseline_mode(d_str)
                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pnl_sample_interval=sample_interval,
                    entry_end=effective_entry_end,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")
            wr     = m["win_rate"]

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "cutoff_time":       label,
                "vix_days_affected": vix_days_affected,
                "num_trades":        len(all_trades),
                "win_rate_pct":      f"{wr:.1f}",
                "total_pnl":         f"{pnl:.2f}",
                "pnl_delta":         pnl_delta,
                "max_drawdown":      f"{dd:.2f}",
                "dd_delta":          dd_delta,
                "calmar":            f"{calmar:.2f}",
                "sharpe":            f"{m['sharpe']:.2f}",
            })
            logger.info(f"  Cutoff={label}  vix_days={vix_days_affected}  trades={len(all_trades):>5}  P&L=${pnl:>10,.2f}  DD=${dd:>9,.2f}  Calmar={calmar:.2f}  Sharpe={m['sharpe']:.2f}")

    with open(VIX_ENTRY_CUTOFF_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 108
    logger.info("")
    logger.info("═" * 108)
    logger.info(f"  {'CUTOFF':>7} | {'VIX DAYS':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['cutoff_time']:>7} | {row['vix_days_affected']:>8} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>4}% | ${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 108)
    logger.info(f"  Full results: {VIX_ENTRY_CUTOFF_SWEEP_FILE}")


# ---------------------------------------------
#  CALENDAR RISK SL SWEEP RUNNER
# ---------------------------------------------
async def run_calendar_risk_sl_sweep():
    """Test each recurring-date SL category independently across SL levels.

    For each category (CPI, PCE, EOQ, pre-TW, post-holiday), runs every level in
    CALENDAR_RISK_SL_SWEEP_LEVELS with all OTHER calendar risk SLs disabled, so
    the P&L delta shows the isolated impact of that one category.
    Finishes with a combined row using the best level found for each category.
    """
    global ENABLE_CPI_SL, CPI_SL_AMOUNT
    global ENABLE_PCE_SL, PCE_SL_AMOUNT
    global ENABLE_EOQ_SL, EOQ_SL_AMOUNT
    global ENABLE_PRE_TW_SL, PRE_TW_SL_AMOUNT
    global ENABLE_POST_HOL_SL, POST_HOL_SL_AMOUNT

    logger.info("=" * 110)
    logger.info("MEDS: CALENDAR RISK SL SWEEP")
    logger.info(f"Levels: {CALENDAR_RISK_SL_SWEEP_LEVELS}")
    logger.info(f"Output: {CALENDAR_RISK_SL_SWEEP_FILE}")
    logger.info("=" * 110)

    # Disable all calendar risk SLs during sweep -- each test enables only one
    ENABLE_CPI_SL = ENABLE_PCE_SL = ENABLE_EOQ_SL = False
    ENABLE_PRE_TW_SL = ENABLE_POST_HOL_SL = False

    # Pre-fetch day pool
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    # Category definitions: (label, date_set, enable_setter, amount_setter)
    categories = [
        ("CPI",       _CPI_DATES,      "_cpi"),
        ("PCE",       _PCE_DATES,      "_pce"),
        ("EOQ",       _EOQ_DATES,      "_eoq"),
        ("pre-TW",    _PRE_TW_DATES,   "_pretw"),
        ("post-hol",  _POST_HOL_DATES, "_posthol"),
    ]

    cols = ["category", "sl_level", "cat_days", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []

    async with _get_session() as session:

        # -- Baseline: all calendar risk SLs off ------------------------------
        base_trades: list = []
        for d_str, day_data in day_pool.items():
            effective_sl = _get_effective_sl(day_data, d_str)
            direction    = _get_baseline_mode(d_str)
            trades, _    = await _simulate_day(
                session, day_data, effective_sl,
                baseline_mode=direction,
                pos_trail_activation=POS_TRAIL_ACTIVATION,
                pos_trail_pullback=POS_TRAIL_PULLBACK,
                min_otm_distance=MIN_OTM_DISTANCE,
                max_credit=MAX_NET_CREDIT,
                pnl_sample_interval=PNL_SAMPLE_INTERVAL,
            )
            base_trades.extend(trades)
        bm       = compute_metrics(base_trades)
        base_pnl = bm["total_pnl"]
        base_dd  = bm["max_drawdown"]
        base_cal = base_pnl / abs(base_dd) if base_dd != 0 else float("inf")
        rows.append({
            "category": "BASELINE", "sl_level": "--", "cat_days": "--",
            "num_trades": bm["num_trades"], "win_rate_pct": f"{bm['win_rate']:.1f}",
            "total_pnl": f"{base_pnl:.2f}", "pnl_delta": "--",
            "max_drawdown": f"{base_dd:.2f}", "dd_delta": "--",
            "calmar": f"{base_cal:.2f}", "sharpe": f"{bm['sharpe']:.2f}",
        })
        logger.info(f"  {'BASELINE':>10} | sl=    -- | pnl=${base_pnl:>10,.2f} |  -- |"
                    f" dd=${base_dd:>9,.2f} |  -- | calmar={base_cal:.2f} | sharpe={bm['sharpe']:.2f}")

        # -- Per-category sweep ------------------------------------------------
        best_level: dict[str, "float|None"] = {}

        for cat_label, cat_dates, cat_key in categories:
            cat_days = sum(1 for d in day_pool if d in cat_dates)
            best_pnl = base_pnl
            best_lv  = None
            logger.info(f"  --- {cat_label} ({cat_days} days) ---")

            for sl_level in CALENDAR_RISK_SL_SWEEP_LEVELS:
                # Enable only this category at this level
                ENABLE_CPI_SL      = (cat_key == "_cpi"    and sl_level is not None)
                ENABLE_PCE_SL      = (cat_key == "_pce"    and sl_level is not None)
                ENABLE_EOQ_SL      = (cat_key == "_eoq"    and sl_level is not None)
                ENABLE_PRE_TW_SL   = (cat_key == "_pretw"  and sl_level is not None)
                ENABLE_POST_HOL_SL = (cat_key == "_posthol" and sl_level is not None)
                if cat_key == "_cpi":      CPI_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_pce":    PCE_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_eoq":    EOQ_SL_AMOUNT      = sl_level or -300.0
                elif cat_key == "_pretw":  PRE_TW_SL_AMOUNT   = sl_level or -300.0
                elif cat_key == "_posthol":POST_HOL_SL_AMOUNT = sl_level or -300.0

                all_trades: list = []
                for d_str, day_data in day_pool.items():
                    effective_sl = _get_effective_sl(day_data, d_str)
                    in_danger    = effective_sl is not None
                    sample_int   = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                    direction    = _get_baseline_mode(d_str)
                    trades, _    = await _simulate_day(
                        session, day_data, effective_sl,
                        baseline_mode=direction,
                        pos_trail_activation=POS_TRAIL_ACTIVATION,
                        pos_trail_pullback=POS_TRAIL_PULLBACK,
                        min_otm_distance=MIN_OTM_DISTANCE,
                        max_credit=MAX_NET_CREDIT,
                        pnl_sample_interval=sample_int,
                    )
                    all_trades.extend(trades)

                m      = compute_metrics(all_trades)
                pnl    = m["total_pnl"]
                dd     = m["max_drawdown"]
                calmar = pnl / abs(dd) if dd != 0 else float("inf")
                label  = "none" if sl_level is None else str(sl_level)
                rows.append({
                    "category":    cat_label, "sl_level": label, "cat_days": cat_days,
                    "num_trades":  m["num_trades"], "win_rate_pct": f"{m['win_rate']:.1f}",
                    "total_pnl":   f"{pnl:.2f}",  "pnl_delta":  f"{pnl - base_pnl:+.2f}",
                    "max_drawdown": f"{dd:.2f}",   "dd_delta":   f"{dd - base_dd:+.2f}",
                    "calmar":      f"{calmar:.2f}", "sharpe":     f"{m['sharpe']:.2f}",
                })
                logger.info(
                    f"  {cat_label:>10} | sl={label:>6} | pnl=${pnl:>10,.2f} | delta={pnl-base_pnl:>+9,.2f} |"
                    f" dd=${dd:>9,.2f} | dd_delta={dd-base_dd:>+8,.2f} | calmar={calmar:.2f}"
                )
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_lv  = sl_level

            best_level[cat_key] = best_lv

        # -- Combined: best level per category --------------------------------
        ENABLE_CPI_SL      = best_level["_cpi"]     is not None
        ENABLE_PCE_SL      = best_level["_pce"]     is not None
        ENABLE_EOQ_SL      = best_level["_eoq"]     is not None
        ENABLE_PRE_TW_SL   = best_level["_pretw"]   is not None
        ENABLE_POST_HOL_SL = best_level["_posthol"] is not None
        if best_level["_cpi"]:      CPI_SL_AMOUNT      = best_level["_cpi"]
        if best_level["_pce"]:      PCE_SL_AMOUNT      = best_level["_pce"]
        if best_level["_eoq"]:      EOQ_SL_AMOUNT      = best_level["_eoq"]
        if best_level["_pretw"]:    PRE_TW_SL_AMOUNT   = best_level["_pretw"]
        if best_level["_posthol"]:  POST_HOL_SL_AMOUNT = best_level["_posthol"]

        combo_trades: list = []
        for d_str, day_data in day_pool.items():
            effective_sl = _get_effective_sl(day_data, d_str)
            in_danger    = effective_sl is not None
            sample_int   = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
            direction    = _get_baseline_mode(d_str)
            trades, _    = await _simulate_day(
                session, day_data, effective_sl,
                baseline_mode=direction,
                pos_trail_activation=POS_TRAIL_ACTIVATION,
                pos_trail_pullback=POS_TRAIL_PULLBACK,
                min_otm_distance=MIN_OTM_DISTANCE,
                max_credit=MAX_NET_CREDIT,
                pnl_sample_interval=sample_int,
            )
            combo_trades.extend(trades)

        cm     = compute_metrics(combo_trades)
        cpnl   = cm["total_pnl"]
        cdd    = cm["max_drawdown"]
        ccal   = cpnl / abs(cdd) if cdd != 0 else float("inf")
        combo_label = " + ".join(
            f"{k}={v}" for k, v in [
                ("CPI", best_level["_cpi"]), ("PCE", best_level["_pce"]),
                ("EOQ", best_level["_eoq"]), ("preTW", best_level["_pretw"]),
                ("postHol", best_level["_posthol"]),
            ] if v is not None
        ) or "none"
        rows.append({
            "category": "COMBINED", "sl_level": combo_label, "cat_days": "--",
            "num_trades": cm["num_trades"], "win_rate_pct": f"{cm['win_rate']:.1f}",
            "total_pnl": f"{cpnl:.2f}", "pnl_delta": f"{cpnl - base_pnl:+.2f}",
            "max_drawdown": f"{cdd:.2f}", "dd_delta": f"{cdd - base_dd:+.2f}",
            "calmar": f"{ccal:.2f}", "sharpe": f"{cm['sharpe']:.2f}",
        })
        logger.info(f"  {'COMBINED':>10} | {combo_label} |"
                    f" pnl=${cpnl:>10,.2f} | delta={cpnl-base_pnl:>+9,.2f} |"
                    f" dd=${cdd:>9,.2f} | calmar={ccal:.2f}")

    # Save CSV
    with open(CALENDAR_RISK_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    W = 115
    logger.info("")
    logger.info("═" * W)
    logger.info("  CALENDAR RISK SL SWEEP RESULTS")
    logger.info("═" * W)
    logger.info(f"  {'CATEGORY':>10} | {'SL':>7} | {'DAYS':>5} | {'TRADES':>7} | {'WIN%':>5} | "
                f"{'TOTAL_PNL':>12} | {'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>9} | "
                f"{'CALMAR':>8} | {'SHARPE':>7}")
    logger.info("-" * W)
    for row in rows:
        logger.info(
            f"  {row['category']:>10} | {row['sl_level']:>7} | {str(row['cat_days']):>5} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>9} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * W)
    logger.info(f"  Full results: {CALENDAR_RISK_SL_SWEEP_FILE}")


# ---------------------------------------------
#  CALL-SIDE SL SWEEP RUNNER
# ---------------------------------------------
async def run_call_sl_sweep():
    """Sweep stop-loss levels applied exclusively on CALL spread days.

    PUT days continue to use _get_effective_sl() (dynamic VIX-based SL) unchanged.
    For each CALL_SL level, the full date range is re-simulated from pre-fetched data.
    A tighter CALL SL reduces drawdown but may also cut profitable CALL days early.
    """
    logger.info("=" * 70)
    logger.info("MEDS: CALL-SIDE SL SWEEP")
    logger.info(f"Levels  : {CALL_SL_SWEEP_LEVELS}")
    logger.info(f"Output  : {CALL_SL_SWEEP_FILE}")
    logger.info("=" * 70)

    # -- Pre-fetch all days once --
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["call_sl_level", "num_trades", "win_rate_pct", "total_pnl",
            "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe", "sortino"]
    rows = []
    base_pnl = None
    base_dd  = None

    async with _get_session() as session:
        for call_sl in CALL_SL_SWEEP_LEVELS:
            label = "none" if call_sl is None else str(call_sl)

            all_trades: list = []
            for d_str, day_data in day_pool.items():
                direction = _get_baseline_mode(d_str)
                if direction == "always_call":
                    # Apply the test CALL SL level directly on CALL days
                    effective_sl = call_sl
                else:
                    # PUT days: use standard dynamic SL logic unchanged
                    effective_sl = _get_effective_sl(day_data, d_str)

                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                )
                all_trades.extend(trades)

            m        = compute_metrics(all_trades)
            pnl      = m["total_pnl"]
            dd       = m["max_drawdown"]
            calmar   = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl = pnl
                base_dd  = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "call_sl_level": label,
                "num_trades":    m["num_trades"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{pnl:.2f}",
                "pnl_delta":     pnl_delta,
                "max_drawdown":  f"{dd:.2f}",
                "dd_delta":      dd_delta,
                "calmar":        f"{calmar:.2f}",
                "sharpe":        f"{m['sharpe']:.2f}",
                "sortino":       f"{m['sortino']:.2f}",
            })
            logger.info(
                f"  call_sl={label:>6} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    # Save CSV
    with open(CALL_SL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Print summary table
    sep = "-" * 100
    logger.info("")
    logger.info("═" * 100)
    logger.info("  CALL-SIDE SL SWEEP RESULTS")
    logger.info("═" * 100)
    logger.info(f"  {'CALL_SL':>8} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['call_sl_level']:>8} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 100)
    logger.info(f"  Full results: {CALL_SL_SWEEP_FILE}")


# ---------------------------------------------
#  PRESSURE FILTER VIX SWEEP RUNNER
# ---------------------------------------------
async def run_pressure_vix_sweep():
    """Sweep pressure filter with VIX-conditional gating.

    Row 0 (baseline): pressure filter disabled -- current confirmed config.
    Row 1 (threshold=None): filter active at all VIX levels -- previously -$50k globally.
    Rows 2-N: filter active only when VIX >= threshold (20, 22, 25, 27, 28, 30).

    Goal: find if restricting the filter to high-VIX days recovers P&L while
    still cutting drawdown in the dangerous VIX 25-30 zone.
    """
    logger.info("=" * 70)
    logger.info("MEDS: PRESSURE FILTER VIX SWEEP")
    logger.info(f"Thresholds    : {PRESSURE_VIX_SWEEP_THRESHOLDS}  (None = all VIX levels)")
    logger.info(f"Distance      : {PRESSURE_DISTANCE_THRESHOLD} pts")
    logger.info(f"Output        : {PRESSURE_VIX_SWEEP_FILE}")
    logger.info("=" * 70)

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")
    day_pool: dict[str, dict] = {}
    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue
            if ENABLE_ECON_FILTER and d_str in ECON_DATES:
                continue
            day_data = await _fetch_day_data(session, d_str)
            if day_data is not None:
                day_pool[d_str] = day_data
    logger.info(f"Pre-fetched {len(day_pool)} qualifying days.")

    cols = ["label", "pressure_enabled", "vix_min", "num_trades", "win_rate_pct",
            "total_pnl", "pnl_delta", "max_drawdown", "dd_delta", "calmar", "sharpe"]
    rows = []
    base_pnl = None
    base_dd  = None

    # Build sweep list: (label, enable_filter, vix_min)
    sweep = [("baseline (off)", False, None)] + [
        (f"vix>={t}" if t is not None else "all VIX", True, t)
        for t in PRESSURE_VIX_SWEEP_THRESHOLDS
    ]

    async with _get_session() as session:
        for label, enable_filter, vix_min in sweep:
            all_trades: list = []

            for d_str, day_data in day_pool.items():
                effective_sl = _get_effective_sl(day_data, d_str)
                in_danger = effective_sl is not None
                sample_interval = DANGER_PNL_SAMPLE_INTERVAL if in_danger else PNL_SAMPLE_INTERVAL
                direction = _get_baseline_mode(d_str)

                trades, _ = await _simulate_day(
                    session, day_data, effective_sl,
                    baseline_mode=direction,
                    pos_trail_activation=POS_TRAIL_ACTIVATION,
                    pos_trail_pullback=POS_TRAIL_PULLBACK,
                    min_otm_distance=MIN_OTM_DISTANCE,
                    max_credit=MAX_NET_CREDIT,
                    pnl_sample_interval=sample_interval,
                    enable_pressure_filter=enable_filter,
                    pressure_vix_min=vix_min,
                    pressure_vix_max=None,
                )
                all_trades.extend(trades)

            m      = compute_metrics(all_trades)
            pnl    = m["total_pnl"]
            dd     = m["max_drawdown"]
            calmar = pnl / abs(dd) if dd != 0 else float("inf")

            if base_pnl is None:
                base_pnl  = pnl
                base_dd   = dd
                pnl_delta = "--"
                dd_delta  = "--"
            else:
                pnl_delta = f"{pnl - base_pnl:+.2f}"
                dd_delta  = f"{dd - base_dd:+.2f}"

            rows.append({
                "label":         label,
                "pressure_enabled": str(enable_filter),
                "vix_min":       str(vix_min) if vix_min is not None else "all" if enable_filter else "off",
                "num_trades":    m["num_trades"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{pnl:.2f}",
                "pnl_delta":     pnl_delta,
                "max_drawdown":  f"{dd:.2f}",
                "dd_delta":      dd_delta,
                "calmar":        f"{calmar:.2f}",
                "sharpe":        f"{m['sharpe']:.2f}",
            })
            logger.info(
                f"  {label:<18} | trades={m['num_trades']:>5} | "
                f"pnl=${pnl:>10,.2f} ({pnl_delta}) | dd=${dd:>9,.2f} ({dd_delta}) | "
                f"calmar={calmar:.2f} | sharpe={m['sharpe']:.2f}"
            )

    with open(PRESSURE_VIX_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    sep = "-" * 110
    logger.info("")
    logger.info("═" * 110)
    logger.info("  PRESSURE FILTER VIX SWEEP RESULTS")
    logger.info("═" * 110)
    logger.info(f"  {'LABEL':<18} | {'TRADES':>7} | {'WIN%':>5} | {'TOTAL_PNL':>12} | "
                f"{'PNL_DELTA':>10} | {'MAX_DD':>10} | {'DD_DELTA':>10} | {'CALMAR':>8} | {'SHARPE':>7}")
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['label']:<18} | {row['num_trades']:>7} | {row['win_rate_pct']:>4}% | "
            f"${float(row['total_pnl']):>11,.2f} | {row['pnl_delta']:>10} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['dd_delta']:>10} | "
            f"{row['calmar']:>8} | {row['sharpe']:>7}"
        )
    logger.info("═" * 110)
    logger.info(f"  Full results: {PRESSURE_VIX_SWEEP_FILE}")


# ---------------------------------------------
#  TRAILING STOP SWEEP RUNNER
# ---------------------------------------------
def _ts_label(ts) -> str:
    """Human-readable label for a trailing stop level (None -> 'none')."""
    return "none" if ts is None else str(int(ts))


async def run_trailing_stop_sweep():
    """Run a trailing stop parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every level in SWEEP_TS_LEVELS using the
    in-memory quote cache -- no extra API calls are needed.

    The regular DAILY_SL remains active alongside the trailing stop so that
    trades that never go positive are still protected by the hard stop.

    Results are printed to console and saved to TRAILING_STOP_SWEEP_FILE.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: TRAILING STOP SWEEP MODE")
    logger.info(f"TS levels : {[_ts_label(ts) for ts in SWEEP_TS_LEVELS]}")
    logger.info(f"Daily SL  : {DAILY_SL}  (applied alongside trailing stop)")
    logger.info(f"Output    : {TRAILING_STOP_SWEEP_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per trailing stop level
    ts_trades: dict[str, list] = {_ts_label(ts): [] for ts in SWEEP_TS_LEVELS}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each trailing stop level using the cached quotes
            for ts in SWEEP_TS_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, trailing_stop=ts)
                label = _ts_label(ts)
                ts_trades[label].extend(trades)
                logger.debug(
                    f"  TS={label:>6} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Write summary CSV --
    ts_sweep_cols = [
        "trailing_stop", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    rows = []
    for ts in SWEEP_TS_LEVELS:
        label = _ts_label(ts)
        m     = compute_metrics(ts_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "trailing_stop":  label,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
        })

    with open(TRAILING_STOP_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ts_sweep_cols)
        w.writeheader()
        w.writerows(rows)

    logger.info(f"\nTrailing stop sweep complete -- summary saved to {TRAILING_STOP_SWEEP_FILE}")

    # -- Print comparison table --
    col_w = 10
    hdr   = (f"{'Trail $':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TRAILING STOP SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"{row['trailing_stop']:>{col_w}} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {pf:>6}"
        )
    logger.info(sep)


# ---------------------------------------------
#  PER-POSITION TRAILING STOP SWEEP RUNNER
# ---------------------------------------------
async def run_pos_trail_sweep():
    """Sweep all (activation%, pullback%) combos for the per-position trailing stop.

    A position's trail activates once it has profited >= activation% of credit;
    it then closes if P&L pulls back >= pullback% of credit from the position's
    peak.  Market data is fetched once per day; every combo re-simulates from
    the cache.  Results are sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')
    combos    = [(a, p) for a in SWEEP_POS_TRAIL_ACTIVATION for p in SWEEP_POS_TRAIL_PULLBACK]

    logger.info("=" * 70)
    logger.info("MEFT v35: PER-POSITION TRAILING STOP SWEEP")
    logger.info(f"Activation : {SWEEP_POS_TRAIL_ACTIVATION}% of credit")
    logger.info(f"Pullback   : {SWEEP_POS_TRAIL_PULLBACK}% of credit")
    logger.info(f"Combos     : {len(combos)}")
    logger.info(f"Output     : {POS_TRAIL_SWEEP_FILE}")
    logger.info("=" * 70)

    pos_trail_trades: dict[tuple, list] = {(a, p): [] for a, p in combos}

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for act, pull in combos:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    pos_trail_activation=act,
                    pos_trail_pullback=pull,
                )
                pos_trail_trades[(act, pull)].extend(trades)
                logger.debug(
                    f"  act={act}% pull={pull}% | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows --
    pt_cols = [
        "activation_pct", "pullback_pct",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    rows = []
    for act, pull in combos:
        m  = compute_metrics(pos_trail_trades[(act, pull)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "activation_pct":  act,
            "pullback_pct":    pull,
            "num_trades":      m["num_trades"],
            "win_rate_pct":    f"{m['win_rate']:.1f}",
            "total_pnl":       f"{m['total_pnl']:.2f}",
            "avg_win":         f"{m['avg_win']:.2f}",
            "avg_loss":        f"{m['avg_loss']:.2f}",
            "profit_factor":   pf,
            "max_drawdown":    f"{m['max_drawdown']:.2f}",
            "_total_pnl_num":  m["total_pnl"],
        })
    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV --
    with open(POS_TRAIL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pt_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in pt_cols})

    logger.info(f"\nPos trail sweep complete -- saved to {POS_TRAIL_SWEEP_FILE}")

    # -- Print table --
    hdr = (
        f"  {'Act%':>5} | {'Pull%':>5} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PER-POSITION TRAIL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        logger.info(
            f"  {row['activation_pct']:>5} | {row['pullback_pct']:>5} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)


# ---------------------------------------------
#  SPREAD WIDTH SWEEP RUNNER
# ---------------------------------------------
async def run_spread_width_sweep():
    """Run a spread width parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every width in SWEEP_SPREAD_WIDTHS using the
    in-memory quote cache -- no extra API calls are needed.

    Results are printed to console and saved to SPREAD_WIDTH_SWEEP_FILE,
    sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: SPREAD WIDTH SWEEP MODE")
    logger.info(f"Widths    : {SWEEP_SPREAD_WIDTHS}")
    logger.info(f"Output    : {SPREAD_WIDTH_SWEEP_FILE}")
    logger.info("=" * 70)

    # Accumulate trades per width
    width_trades: dict[int, list] = {w: [] for w in SWEEP_SPREAD_WIDTHS}

    init_log_file()  # reset meft_v35_bidask_log.csv for the default WIDTH run

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # Fetch market data once (populates _quote_cache)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Re-simulate for each width using the cached quotes
            for w in SWEEP_SPREAD_WIDTHS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, spread_width=float(w))
                width_trades[w].extend(trades)
                # Write the default-width trades to the main trade log
                if w == WIDTH:
                    for pos in trades:
                        append_trade(pos)
                logger.debug(
                    f"  Width={w:>3} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}"
                )

    # -- Build result rows --
    sw_sweep_cols = [
        "spread_width", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]

    rows = []
    for w in SWEEP_SPREAD_WIDTHS:
        trades = width_trades[w]
        m      = compute_metrics(trades)
        pf     = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in trades]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        rows.append({
            "spread_width":        w,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            # numeric key for sorting
            "_total_pnl_num":      m["total_pnl"],
        })

    rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)

    # -- Write CSV (drop sort key) --
    with open(SPREAD_WIDTH_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sw_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in sw_sweep_cols})

    logger.info(f"\nSpread width sweep complete -- summary saved to {SPREAD_WIDTH_SWEEP_FILE}")

    # -- Print comparison table --
    hdr = (
        f"  {'Width':>6} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  SPREAD WIDTH SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in rows:
        pf = row["profit_factor"]
        logger.info(
            f"  {row['spread_width']:>6} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{pf:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)

    # -- Full performance report for the default WIDTH --
    default_trades = width_trades.get(WIDTH, [])
    if default_trades:
        logger.info(f"\n  Full performance report for default width={WIDTH}:")
        print_performance_report(default_trades, date_list)


# ---------------------------------------------
#  NAIVE BASELINE COMPARISON
# ---------------------------------------------
_BASELINES = [
    ("EMA Strategy",   None),
    ("Always PUT",     "always_put"),
    ("Always CALL",    "always_call"),
    ("No EMA Filter",  "no_ema_filter"),
    ("Both Sides",     "both_sides"),
]


async def run_baseline_comparison():
    """Re-run the full date range for the main strategy and three naive baselines.

    Market data is fetched once per day; each baseline re-simulates from the cache.
    Results are printed to console and saved to BASELINE_COMPARISON_FILE.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: NAIVE BASELINE COMPARISON")
    logger.info(f"Baselines : {[name for name, _ in _BASELINES]}")
    logger.info(f"Output    : {BASELINE_COMPARISON_FILE}")
    logger.info("=" * 70)

    baseline_trades: dict[str, list] = {name: [] for name, _ in _BASELINES}
    spx_daily: list[dict] = []   # {date, close} for SPX benchmark

    async with _get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            # Fetch market data once (re-populates _quote_cache for this day)
            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            # Collect SPX close for benchmark
            ind = _DAILY_INDICATORS.get(d_str)
            if ind and ind.get("close"):
                spx_daily.append({"date": d_str, "close": ind["close"]})

            # Simulate once per baseline using the shared cache
            for name, mode in _BASELINES:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                baseline_trades[name].extend(trades)
                logger.debug(f"  [{name}] {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # -- Build comparison rows --
    comp_cols = [
        "strategy", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
        "p_value", "p_fdr", "sig_fdr",
    ]
    rows = []
    for name, _ in _BASELINES:
        m = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            "strategy":       name,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "p_value":        round(m["p_value"], 4),
        })
    _apply_fdr_to_rows(rows)

    # -- SPX buy-and-hold benchmark --
    spx_row = None
    if len(spx_daily) >= 2:
        import math as _math
        spx_closes  = [r["close"] for r in sorted(spx_daily, key=lambda x: x["date"])]
        spx_returns = [(spx_closes[i] - spx_closes[i-1]) / spx_closes[i-1]
                       for i in range(1, len(spx_closes))]
        rf_daily_r  = ANNUAL_RISK_FREE_RATE / 252
        n_spx       = len(spx_returns)
        mean_r      = sum(spx_returns) / n_spx
        var_r       = sum((r - mean_r) ** 2 for r in spx_returns) / n_spx
        spx_sharpe  = ((mean_r - rf_daily_r) / _math.sqrt(var_r) * _math.sqrt(252)) if var_r else 0
        down_r      = [r for r in spx_returns if r < mean_r]
        down_var_r  = sum((r - mean_r) ** 2 for r in down_r) / len(down_r) if down_r else 0
        spx_sortino = ((mean_r - rf_daily_r) / _math.sqrt(down_var_r) * _math.sqrt(252)) if down_var_r else 0
        spx_total   = (spx_closes[-1] / spx_closes[0] - 1) * 100
        n_years     = n_spx / 252
        spx_ann     = ((spx_closes[-1] / spx_closes[0]) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0
        # Max drawdown on SPX price curve
        spx_peak = spx_dd = 0.0
        for c in spx_closes:
            spx_peak = max(spx_peak, c)
            spx_dd   = min(spx_dd, (c - spx_peak) / spx_peak * 100)
        spx_row = {
            "total_return_pct": round(spx_total, 2),
            "ann_return_pct":   round(spx_ann, 2),
            "sharpe":           round(spx_sharpe, 3),
            "sortino":          round(spx_sortino, 3),
            "max_drawdown_pct": round(spx_dd, 2),
            "start":            spx_daily[0]["date"],
            "end":              spx_daily[-1]["date"],
            "start_price":      round(spx_closes[0], 2),
            "end_price":        round(spx_closes[-1], 2),
        }

    # -- Save CSV --
    with open(BASELINE_COMPARISON_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=comp_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"Baseline comparison saved to {BASELINE_COMPARISON_FILE}")

    # -- Print comparison table --
    sep = "-" * 90
    hdr = (f"  {'Strategy':<18} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Profit Factor':>14} | {'Max DD':>10}")
    logger.info(sep)
    logger.info(f"  NAIVE BASELINE COMPARISON  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, acct=${ACCOUNT_SIZE:,.0f})")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for name, _ in _BASELINES:
        m  = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"  {name:<18} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"{pf:>14} | ${m['max_drawdown']:>9,.2f}"
        )
    logger.info(sep)

    # -- Print SPX benchmark --
    if spx_row:
        logger.info("")
        logger.info(f"  SPX BUY-AND-HOLD BENCHMARK  "
                    f"({spx_row['start']} -> {spx_row['end']} | "
                    f"{spx_row['start_price']:,.0f} -> {spx_row['end_price']:,.0f})")
        logger.info(sep)
        logger.info(f"  Total return    : {spx_row['total_return_pct']:>8.1f}%")
        logger.info(f"  Ann. return     : {spx_row['ann_return_pct']:>8.1f}%  (CAGR)")
        logger.info(f"  Sharpe ratio    : {spx_row['sharpe']:>8.3f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, daily returns)")
        logger.info(f"  Sortino ratio   : {spx_row['sortino']:>8.3f}")
        logger.info(f"  Max drawdown    : {spx_row['max_drawdown_pct']:>8.1f}%  (price-based)")
        logger.info(sep)


# ---------------------------------------------
#  MASTER SWEEP RUNNER
# ---------------------------------------------
async def run_master_sweep():
    """Run ALL sweep variants in a single pass.

    For each trading day:
      1. Market data is fetched exactly once (seeded for the largest EMA period).
      2. Every sweep variant is re-simulated against the in-memory quote cache --
         no extra API calls.

    Sweep variants:
      • Stop-loss sweep              (SWEEP_SL_LEVELS)
      • EMA parameter sweep          (SWEEP_EMA_FAST × SWEEP_EMA_SLOW)
      • Trailing stop sweep          (SWEEP_TS_LEVELS)
      • Spread width sweep           (SWEEP_SPREAD_WIDTHS)
      • Per-position trailing stop   (SWEEP_POS_TRAIL_ACTIVATION × SWEEP_POS_TRAIL_PULLBACK)
      • Naive baseline comparison    (_BASELINES)

    Default-width trades are written to meft_v35_bidask_log.csv as usual.
    """
    date_list   = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')
    ema_combos  = [(f, s) for f in SWEEP_EMA_FAST for s in SWEEP_EMA_SLOW]
    pt_combos   = [(a, p) for a in SWEEP_POS_TRAIL_ACTIVATION for p in SWEEP_POS_TRAIL_PULLBACK]
    max_seed    = max(SWEEP_EMA_SLOW)

    logger.info("=" * 70)
    logger.info("MEFT v35: MASTER SWEEP MODE")
    logger.info(f"SL levels : {[_sl_label(sl) for sl in SWEEP_SL_LEVELS]}")
    logger.info(f"EMA combos: {len(ema_combos)}  ({SWEEP_EMA_FAST} × {SWEEP_EMA_SLOW})")
    logger.info(f"TS levels : {[_ts_label(ts) for ts in SWEEP_TS_LEVELS]}")
    logger.info(f"Widths    : {SWEEP_SPREAD_WIDTHS}")
    logger.info(f"Pos trail : {len(pt_combos)}  (act={SWEEP_POS_TRAIL_ACTIVATION} × pull={SWEEP_POS_TRAIL_PULLBACK})")
    logger.info(f"Min credit: {SWEEP_MIN_CREDIT_LEVELS}")
    logger.info(f"Baselines : {[name for name, _ in _BASELINES]}")
    logger.info("=" * 70)

    # -- Accumulators --
    sl_trades:       dict[str,   list] = {_sl_label(sl):   [] for sl in SWEEP_SL_LEVELS}
    ema_trades:      dict[tuple, list] = {(f, s):           [] for f, s in ema_combos}
    ts_trades:       dict[str,   list] = {_ts_label(ts):    [] for ts in SWEEP_TS_LEVELS}
    width_trades:    dict[float, list] = {float(w):         [] for w in SWEEP_SPREAD_WIDTHS}
    pt_trades:       dict[tuple, list] = {(a, p):           [] for a, p in pt_combos}
    mc_trades:       dict[float, list] = {mc:               [] for mc in SWEEP_MIN_CREDIT_LEVELS}
    baseline_trades: dict[str,   list] = {name:             [] for name, _ in _BASELINES}

    init_log_file()  # reset meft_v35_bidask_log.csv

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue

            # -- Fetch market data once per day --
            clear_day_cache()
            day_data = await _fetch_day_data(session, d_str, seed_bars=max_seed)
            if day_data is None:
                continue

            logger.info(
                f"Day {i+1:>3}/{len(date_list)} | {d_str} -- running all sweep variants..."
            )

            # SL sweep
            for sl in SWEEP_SL_LEVELS:
                trades, _ = await _simulate_day(session, day_data, sl)
                sl_trades[_sl_label(sl)].extend(trades)

            # EMA sweep
            for fast, slow in ema_combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, ema_fast=fast, ema_slow=slow
                )
                ema_trades[(fast, slow)].extend(trades)

            # Trailing stop sweep
            for ts in SWEEP_TS_LEVELS:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, trailing_stop=ts
                )
                ts_trades[_ts_label(ts)].extend(trades)

            # Spread width sweep (write default-width trades to main log)
            for w in SWEEP_SPREAD_WIDTHS:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, spread_width=float(w)
                )
                width_trades[float(w)].extend(trades)
                if float(w) == float(WIDTH):
                    for pos in trades:
                        append_trade(pos)

            # Per-position trailing stop sweep
            for act, pull in pt_combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    pos_trail_activation=act, pos_trail_pullback=pull,
                )
                pt_trades[(act, pull)].extend(trades)

            # Min credit sweep
            for mc in SWEEP_MIN_CREDIT_LEVELS:
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, min_credit=mc)
                mc_trades[mc].extend(trades)

            # Baseline comparison
            for name, mode in _BASELINES:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL, baseline_mode=mode
                )
                baseline_trades[name].extend(trades)

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- SL SWEEP
    # ════════════════════════════════════════════════════════════
    sweep_cols = [
        "stop_loss", "num_trades", "num_wins", "num_losses",
        "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    with open(SWEEP_SAVE_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=sweep_cols)
        w_csv.writeheader()
        for sl in SWEEP_SL_LEVELS:
            label = _sl_label(sl)
            m     = compute_metrics(sl_trades[label])
            pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
            w_csv.writerow({
                "stop_loss":     label,
                "num_trades":    m["num_trades"],
                "num_wins":      m["num_wins"],
                "num_losses":    m["num_losses"],
                "win_rate_pct":  f"{m['win_rate']:.1f}",
                "total_pnl":     f"{m['total_pnl']:.2f}",
                "avg_win":       f"{m['avg_win']:.2f}",
                "avg_loss":      f"{m['avg_loss']:.2f}",
                "profit_factor": pf,
                "max_drawdown":  f"{m['max_drawdown']:.2f}",
            })
    col_w = 12
    hdr = (f"{'SL':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Max DD':>10} | {'PF':>6}")
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  STOP-LOSS SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for sl in SWEEP_SL_LEVELS:
        label = _sl_label(sl)
        m     = compute_metrics(sl_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "   inf"
        logger.info(
            f"{label:>{col_w}} | {m['num_trades']:>7} | {m['win_rate']:>5.1f}% | "
            f"${m['total_pnl']:>11,.2f} | ${m['avg_win']:>8,.2f} | ${m['avg_loss']:>8,.2f} | "
            f"${m['max_drawdown']:>9,.2f} | {pf:>6}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- EMA SWEEP
    # ════════════════════════════════════════════════════════════
    ema_sweep_cols = [
        "ema_fast", "ema_slow",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    ema_rows = []
    for fast, slow in ema_combos:
        m  = compute_metrics(ema_trades[(fast, slow)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        ema_rows.append({
            "ema_fast":        fast,
            "ema_slow":        slow,
            "num_trades":      m["num_trades"],
            "win_rate_pct":    f"{m['win_rate']:.1f}",
            "total_pnl":       f"{m['total_pnl']:.2f}",
            "avg_win":         f"{m['avg_win']:.2f}",
            "avg_loss":        f"{m['avg_loss']:.2f}",
            "profit_factor":   pf,
            "max_drawdown":    f"{m['max_drawdown']:.2f}",
            "_total_pnl_num":  m["total_pnl"],
        })
    ema_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(EMA_SWEEP_SAVE_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=ema_sweep_cols)
        w_csv.writeheader()
        for row in ema_rows:
            w_csv.writerow({k: row[k] for k in ema_sweep_cols})
    hdr = (
        f"  {'Fast':>4} | {'Slow':>4} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  EMA PARAMETER SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in ema_rows:
        logger.info(
            f"  {row['ema_fast']:>4} | {row['ema_slow']:>4} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {EMA_SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- TRAILING STOP SWEEP
    # ════════════════════════════════════════════════════════════
    ts_sweep_cols = [
        "trailing_stop", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    ts_rows = []
    for ts in SWEEP_TS_LEVELS:
        label = _ts_label(ts)
        m     = compute_metrics(ts_trades[label])
        pf    = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        ts_rows.append({
            "trailing_stop":  label,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
        })
    with open(TRAILING_STOP_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=ts_sweep_cols)
        w_csv.writeheader()
        w_csv.writerows(ts_rows)
    col_w = 10
    hdr = (f"{'Trail $':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Max DD':>10} | {'PF':>6}")
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  TRAILING STOP SWEEP RESULTS")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in ts_rows:
        logger.info(
            f"{row['trailing_stop']:>{col_w}} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['profit_factor']:>6}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {TRAILING_STOP_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- SPREAD WIDTH SWEEP
    # ════════════════════════════════════════════════════════════
    sw_sweep_cols = [
        "spread_width", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    sw_rows = []
    for w in SWEEP_SPREAD_WIDTHS:
        m      = compute_metrics(width_trades[float(w)])
        pf     = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in width_trades[float(w)]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        sw_rows.append({
            "spread_width":        w,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    sw_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(SPREAD_WIDTH_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=sw_sweep_cols)
        w_csv.writeheader()
        for row in sw_rows:
            w_csv.writerow({k: row[k] for k in sw_sweep_cols})
    hdr = (
        f"  {'Width':>6} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  SPREAD WIDTH SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in sw_rows:
        logger.info(
            f"  {row['spread_width']:>6} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {SPREAD_WIDTH_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- PER-POSITION TRAILING STOP SWEEP
    # ════════════════════════════════════════════════════════════
    pt_cols = [
        "activation_pct", "pullback_pct",
        "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown",
    ]
    pt_rows = []
    for act, pull in pt_combos:
        m  = compute_metrics(pt_trades[(act, pull)])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        pt_rows.append({
            "activation_pct": act,
            "pullback_pct":   pull,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   f"{m['win_rate']:.1f}",
            "total_pnl":      f"{m['total_pnl']:.2f}",
            "avg_win":        f"{m['avg_win']:.2f}",
            "avg_loss":       f"{m['avg_loss']:.2f}",
            "profit_factor":  pf,
            "max_drawdown":   f"{m['max_drawdown']:.2f}",
            "_total_pnl_num": m["total_pnl"],
        })
    pt_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(POS_TRAIL_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=pt_cols)
        w_csv.writeheader()
        for row in pt_rows:
            w_csv.writerow({k: row[k] for k in pt_cols})
    hdr = (
        f"  {'Act%':>5} | {'Pull%':>5} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  PER-POSITION TRAIL SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in pt_rows:
        logger.info(
            f"  {row['activation_pct']:>5} | {row['pullback_pct']:>5} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {POS_TRAIL_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- MIN CREDIT SWEEP
    # ════════════════════════════════════════════════════════════
    mc_cols = [
        "min_credit", "num_trades", "win_rate_pct", "total_pnl",
        "avg_win", "avg_loss", "profit_factor", "max_drawdown", "avg_credit_received",
    ]
    mc_rows = []
    for mc in SWEEP_MIN_CREDIT_LEVELS:
        m  = compute_metrics(mc_trades[mc])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        credits = [float(t.get("credit_received", 0)) for t in mc_trades[mc]]
        avg_credit = sum(credits) / len(credits) if credits else 0.0
        mc_rows.append({
            "min_credit":          mc,
            "num_trades":          m["num_trades"],
            "win_rate_pct":        f"{m['win_rate']:.1f}",
            "total_pnl":           f"{m['total_pnl']:.2f}",
            "avg_win":             f"{m['avg_win']:.2f}",
            "avg_loss":            f"{m['avg_loss']:.2f}",
            "profit_factor":       pf,
            "max_drawdown":        f"{m['max_drawdown']:.2f}",
            "avg_credit_received": f"{avg_credit:.4f}",
            "_total_pnl_num":      m["total_pnl"],
        })
    mc_rows.sort(key=lambda r: r["_total_pnl_num"], reverse=True)
    with open(MIN_CREDIT_SWEEP_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=mc_cols)
        w_csv.writeheader()
        for row in mc_rows:
            w_csv.writerow({k: row[k] for k in mc_cols})
    hdr = (
        f"  {'MinCredit':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "-" * len(hdr)
    logger.info(sep)
    logger.info("  MIN CREDIT SWEEP RESULTS  (sorted by Total P&L)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in mc_rows:
        logger.info(
            f"  {row['min_credit']:>9} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>9} | ${float(row['max_drawdown']):>9,.2f} | "
            f"  {float(row['avg_credit_received']):>9.4f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {MIN_CREDIT_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT -- BASELINE COMPARISON
    # ════════════════════════════════════════════════════════════
    comp_cols = [
        "strategy", "num_trades", "win_rate_pct",
        "total_pnl", "avg_win", "avg_loss",
        "profit_factor", "max_drawdown",
    ]
    base_rows = []
    for name, _ in _BASELINES:
        m  = compute_metrics(baseline_trades[name])
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float("inf") else "inf"
        base_rows.append({
            "strategy":      name,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
        })
    with open(BASELINE_COMPARISON_FILE, "w", newline="") as f:
        w_csv = csv.DictWriter(f, fieldnames=comp_cols)
        w_csv.writeheader()
        w_csv.writerows(base_rows)
    sep = "-" * 90
    hdr = (f"  {'Strategy':<18} | {'Trades':>7} | {'WR%':>6} | "
           f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
           f"{'Profit Factor':>14} | {'Max DD':>10}")
    logger.info(sep)
    logger.info("  NAIVE BASELINE COMPARISON")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    for row in base_rows:
        logger.info(
            f"  {row['strategy']:<18} | {row['num_trades']:>7} | "
            f"{row['win_rate_pct']:>5}% | ${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['avg_win']):>8,.2f} | ${float(row['avg_loss']):>8,.2f} | "
            f"{row['profit_factor']:>14} | ${float(row['max_drawdown']):>9,.2f}"
        )
    logger.info(sep)
    logger.info(f"  Saved -> {BASELINE_COMPARISON_FILE}")

    # ════════════════════════════════════════════════════════════
    #  FULL PERFORMANCE REPORT -- default config (SL=DAILY_SL, WIDTH=WIDTH)
    # ════════════════════════════════════════════════════════════
    default_trades = width_trades.get(float(WIDTH), [])
    if default_trades:
        logger.info(f"\n  Full performance report (default: SL={DAILY_SL}, width={WIDTH}):")
        print_performance_report(default_trades, date_list)
        print_vix_analysis(default_trades)
        if RUN_STRIKE_DISTANCE_ANALYSIS:
            print_strike_distance_analysis(default_trades)
        if RUN_PUT_CALL_ANALYSIS:
            print_put_call_analysis(default_trades)

    logger.info("\nMaster sweep complete.")


# ---------------------------------------------
#  ENTRY TIME SWEEP RUNNER
# ---------------------------------------------
async def run_entry_time_sweep():
    """Sweep all combinations of entry_start × entry_end × entry_interval.

    For each trading day:
      1. Market data is fetched exactly once.
      2. Every (start × end × interval) combo is re-simulated from the cache.

    Results saved to ENTRY_TIME_SWEEP_FILE sorted by total_pnl descending.
    """
    import itertools

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")

    combos = list(itertools.product(
        ENTRY_TIME_STARTS,
        ENTRY_TIME_ENDS,
        ENTRY_TIME_INTERVALS,
    ))
    # Drop invalid combos where start >= end
    combos = [(s, e, i) for s, e, i in combos if s < e]

    logger.info("=" * 70)
    logger.info("MEFT v35: ENTRY TIME SWEEP MODE")
    logger.info(f"Entry starts   : {[t.strftime('%H:%M') for t in ENTRY_TIME_STARTS]}")
    logger.info(f"Entry ends     : {[t.strftime('%H:%M') for t in ENTRY_TIME_ENDS]}")
    logger.info(f"Intervals (min): {ENTRY_TIME_INTERVALS}")
    logger.info(f"Combos         : {len(combos)}")
    logger.info(f"Output         : {ENTRY_TIME_SWEEP_FILE}")
    logger.info("=" * 70)

    combo_trades: dict[tuple, list] = {c: [] for c in combos}
    # (combo, dow) -> trades, where dow: 0=Mon … 4=Fri
    combo_dow_trades: dict[tuple, list] = {}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            dow = d.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} -- running {len(combos)} entry-time combos...")

            for (es, ee, ei) in combos:
                trades, _ = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode="always_put" if PUT_ONLY else None,
                    entry_start=es,
                    entry_end=ee,
                    entry_interval=ei,
                )
                combo_trades[(es, ee, ei)].extend(trades)
                key = (es, ee, ei, dow)
                combo_dow_trades.setdefault(key, []).extend(trades)

    # -- Build results table --
    rows = []
    for (es, ee, ei), trades in combo_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "entry_start":   es.strftime("%H:%M"),
            "entry_end":     ee.strftime("%H:%M"),
            "entry_interval": ei,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    if not rows:
        logger.warning("Entry time sweep produced no trades.")
        return

    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    _apply_fdr_to_rows(rows)
    fieldnames = ["entry_start","entry_end","entry_interval","num_trades",
                  "win_rate_pct","total_pnl","avg_win","avg_loss",
                  "profit_factor","max_drawdown","calmar",
                  "p_value","p_fdr","sig_fdr"]
    with open(ENTRY_TIME_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nEntry time sweep complete -- {len(rows)} combos saved to {ENTRY_TIME_SWEEP_FILE}")

    sep  = "-" * 100
    sep2 = "═" * 100
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY TOTAL P&L")
    logger.info(sep2)
    hdr = f"  {'Start':>6} {'End':>6} {'Intvl':>6} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12} {'Calmar':>8}"
    logger.info(hdr)
    logger.info(sep)
    for r in rows[:20]:
        logger.info(
            f"  {r['entry_start']:>6} {r['entry_end']:>6} {r['entry_interval']:>6} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f} "
            f"{r['max_drawdown']:>12,.0f} {r['calmar']:>8.4f}"
        )

    # -- Day-of-week breakdown ------------------------------------------------
    DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}
    dow_rows = []
    for (es, ee, ei, dow), trades in combo_dow_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        dow_rows.append({
            "day_of_week":    DOW_NAMES[dow],
            "entry_start":    es.strftime("%H:%M"),
            "entry_end":      ee.strftime("%H:%M"),
            "entry_interval": ei,
            "num_trades":     m["num_trades"],
            "win_rate_pct":   round(m["win_rate"], 1),
            "total_pnl":      round(m["total_pnl"], 2),
            "avg_win":        round(m["avg_win"], 2),
            "avg_loss":       round(m["avg_loss"], 2),
            "profit_factor":  round(pf, 3),
            "max_drawdown":   round(m["max_drawdown"], 2),
            "calmar":         round(calmar, 4) if calmar != float("inf") else 9999.0,
        })

    dow_rows.sort(key=lambda r: (r["day_of_week"], -r["total_pnl"]))

    dow_fieldnames = ["day_of_week", "entry_start", "entry_end", "entry_interval",
                      "num_trades", "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
                      "profit_factor", "max_drawdown", "calmar"]
    with open(ENTRY_TIME_DOW_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=dow_fieldnames)
        w.writeheader()
        w.writerows(dow_rows)
    logger.info(f"Day-of-week breakdown saved to {ENTRY_TIME_DOW_FILE}")

    # Print top combo per DOW
    logger.info(f"\n{sep2}")
    logger.info("  BEST ENTRY TIME PER DAY OF WEEK (by total P&L)")
    logger.info(sep2)
    hdr2 = f"  {'Day':>12} {'Start':>6} {'End':>6} {'Intvl':>6} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6}"
    logger.info(hdr2)
    logger.info(sep)
    seen_days: set = set()
    for r in sorted(dow_rows, key=lambda x: -x["total_pnl"]):
        if r["day_of_week"] not in seen_days:
            seen_days.add(r["day_of_week"])
            logger.info(
                f"  {r['day_of_week']:>12} {r['entry_start']:>6} {r['entry_end']:>6} {r['entry_interval']:>6} | "
                f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
                f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f}"
            )


# ---------------------------------------------
#  DIRECTION × TIME SWEEP
# ---------------------------------------------
async def run_direction_time_sweep():
    """For each entry time slot, compare PUT-only, CALL-only, and Both-Sides.

    Data is fetched once per day. Each (slot, mode) combo is simulated with a
    one-bar entry window (entry_start == entry_end == slot) so exactly one entry
    attempt is made per day per combo.

    Outputs:
      DIRECTION_TIME_SWEEP_FILE -- aggregate metrics per (slot, mode)
      DIRECTION_TIME_DOW_FILE   -- same broken down by day of week
    Console prints the best mode per slot and per (slot, DOW).
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")

    _DIRECTION_MODES = [
        ("PUT Only",   "always_put"),
        ("CALL Only",  "always_call"),
        ("Both Sides", "both_sides"),
    ]
    DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

    # (slot_str, mode_label) -> trades
    slot_mode_trades: dict[tuple, list] = {}
    # (slot_str, mode_label, dow_name) -> trades
    slot_mode_dow_trades: dict[tuple, list] = {}

    logger.info("=" * 70)
    logger.info("MEFT v35: DIRECTION × TIME SWEEP")
    logger.info(f"Slots  : {[s.strftime('%H:%M') for s in DIRECTION_TIME_SLOTS]}")
    logger.info(f"Modes  : {[label for label, _ in _DIRECTION_MODES]}")
    logger.info(f"Output : {DIRECTION_TIME_SWEEP_FILE}")
    logger.info("=" * 70)

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            dow_name = DOW_NAMES[d.weekday()]
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} ({dow_name})")

            for slot in DIRECTION_TIME_SLOTS:
                slot_str = slot.strftime("%H:%M")
                for label, mode in _DIRECTION_MODES:
                    trades, _ = await _simulate_day(
                        session, day_data, DAILY_SL,
                        baseline_mode=mode,
                        entry_start=slot,
                        entry_end=slot,
                        entry_interval=1,   # interval=1 ensures we never miss the slot
                    )
                    agg_key = (slot_str, label)
                    slot_mode_trades.setdefault(agg_key, []).extend(trades)
                    dow_key = (slot_str, label, dow_name)
                    slot_mode_dow_trades.setdefault(dow_key, []).extend(trades)

    # -- Build aggregate rows -------------------------------------------------
    def _metrics_row(trades, extra: dict) -> dict | None:
        if not trades:
            return None
        m = compute_metrics(trades)
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        return {
            **extra,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        }

    agg_rows = []
    for (slot_str, label), trades in slot_mode_trades.items():
        row = _metrics_row(trades, {"slot": slot_str, "mode": label})
        if row:
            agg_rows.append(row)
    _apply_fdr_to_rows(agg_rows)
    agg_rows.sort(key=lambda r: (r["slot"], -r["total_pnl"]))

    agg_fields = ["slot", "mode", "num_trades", "win_rate_pct", "total_pnl",
                  "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(DIRECTION_TIME_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=agg_fields)
        w.writeheader()
        w.writerows(agg_rows)
    logger.info(f"\nAggregate results saved to {DIRECTION_TIME_SWEEP_FILE}")

    # -- Build DOW rows -------------------------------------------------------
    dow_rows = []
    for (slot_str, label, dow_name), trades in slot_mode_dow_trades.items():
        row = _metrics_row(trades, {"slot": slot_str, "mode": label, "day_of_week": dow_name})
        if row:
            dow_rows.append(row)
    dow_rows.sort(key=lambda r: (r["day_of_week"], r["slot"], -r["total_pnl"]))
    _apply_fdr_to_rows(dow_rows)

    dow_fields = ["day_of_week", "slot", "mode", "num_trades", "win_rate_pct", "total_pnl",
                  "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(DIRECTION_TIME_DOW_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=dow_fields)
        w.writeheader()
        w.writerows(dow_rows)
    logger.info(f"Day-of-week breakdown saved to {DIRECTION_TIME_DOW_FILE}")

    # -- Console: best mode per slot (aggregate) ------------------------------
    sep  = "-" * 80
    sep2 = "═" * 80
    logger.info(f"\n{sep2}")
    logger.info("  BEST MODE PER SLOT (aggregate)")
    logger.info(sep2)
    hdr = f"  {'Slot':>6} {'Mode':>12} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6}"
    logger.info(hdr)
    logger.info(sep)
    seen_slots: set = set()
    for r in sorted(agg_rows, key=lambda x: (x["slot"], -x["total_pnl"])):
        if r["slot"] not in seen_slots:
            seen_slots.add(r["slot"])
        logger.info(
            f"  {r['slot']:>6} {r['mode']:>12} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f}"
        )

    # -- Console: best mode per slot × DOW ------------------------------------
    logger.info(f"\n{sep2}")
    logger.info("  BEST MODE PER SLOT × DAY OF WEEK")
    logger.info(sep2)
    hdr2 = f"  {'Day':>12} {'Slot':>6} {'Mode':>12} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'PF':>6}"
    logger.info(hdr2)
    logger.info(sep)
    seen_slot_dow: set = set()
    for r in sorted(dow_rows, key=lambda x: (x["day_of_week"], x["slot"], -x["total_pnl"])):
        key = (r["day_of_week"], r["slot"])
        if key not in seen_slot_dow:
            seen_slot_dow.add(key)
            logger.info(
                f"  {r['day_of_week']:>12} {r['slot']:>6} {r['mode']:>12} | "
                f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
                f"{r['profit_factor']:>6.3f}"
            )


# ---------------------------------------------
#  BIAS SWEEP
# ---------------------------------------------
async def run_bias_sweep():
    """Test daily indicators as direction routers: bullish signal -> PUT spread,
    bearish signal -> CALL spread.

    For each bias rule the whole date range is re-simulated from the shared
    quote cache.  Results are compared against three fixed baselines:
      - Always PUT, Always CALL, Intraday EMA (the main strategy).

    Output: BIAS_SWEEP_FILE sorted by total_pnl descending.
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")

    # Each rule is (label, fn(prior, today, vix) -> "always_put" | "always_call" | None)
    # Returning None means skip the day entirely for that rule.
    def gap_direction(prior, today, vix):
        g = (today or {}).get("dGapPercent")
        if g is None: return None
        return "always_put" if g >= 0 else "always_call"

    def prior_day_return(prior, today, vix):
        if prior is None: return None
        c, pc = prior.get("close"), prior.get("prev_close")
        if c is None or pc is None or pc == 0: return None
        return "always_put" if c >= pc else "always_call"

    def macd_direction(prior, today, vix):
        bull = (prior or {}).get("dMacdBull")
        if bull is None: return None
        return "always_put" if bull else "always_call"

    def rsi_direction(prior, today, vix):
        rsi = (prior or {}).get("dRsi")
        if rsi is None: return None
        return "always_put" if rsi >= 50 else "always_call"

    def sma20_direction(prior, today, vix):
        above = (prior or {}).get("above_sma5")   # sma5 used as short-term trend proxy
        if above is None: return None
        return "always_put" if above else "always_call"

    def sma200_direction(prior, today, vix):
        above = (prior or {}).get("above_sma200")
        if above is None: return None
        return "always_put" if above else "always_call"

    def vix_change_direction(prior, today, vix):
        # VIX falling -> calmer market -> bullish -> PUT; VIX rising -> fearful -> CALL
        chg = (today or {}).get("dVixChgPct")
        if chg is None: return None
        return "always_put" if chg <= 0 else "always_call"

    def momentum_direction(prior, today, vix):
        mom = (prior or {}).get("dMomentum")
        if mom is None: return None
        return "always_put" if mom >= 0 else "always_call"

    def stoch_direction(prior, today, vix):
        k = (prior or {}).get("dStoch")
        if k is None: return None
        return "always_put" if k >= 50 else "always_call"

    def var_pct_direction(prior, today, vix):
        # dVarPct: prior day's close position within its range (0=at low, 100=at high)
        # Prior day closed near high -> bullish -> trade PUT today
        v = (prior or {}).get("dVarPct")
        if v is None: return None
        return "always_put" if v >= 50 else "always_call"

    BIAS_RULES = [
        ("Gap Direction",        gap_direction),
        ("Prior Day Return",     prior_day_return),
        ("MACD Direction",       macd_direction),
        ("RSI Direction",        rsi_direction),
        ("Above SMA200",         sma200_direction),
        ("VIX Change",           vix_change_direction),
        ("Momentum",             momentum_direction),
        ("Stochastic",           stoch_direction),
        ("Close Pos in Range",   var_pct_direction),
    ]

    # Fixed baselines for comparison
    FIXED_BASELINES = [
        ("Always PUT",   "always_put"),
        ("Always CALL",  "always_call"),
        ("Intraday EMA", None),
    ]

    logger.info("=" * 70)
    logger.info("MEFT v35: BIAS SWEEP")
    logger.info(f"Rules  : {[name for name, _ in BIAS_RULES]}")
    logger.info(f"Output : {BIAS_SWEEP_FILE}")
    logger.info("=" * 70)

    rule_trades: dict[str, list] = {name: [] for name, _ in BIAS_RULES + FIXED_BASELINES}
    # Also track PUT/CALL split per rule for diagnostics
    rule_put_days:  dict[str, int] = {name: 0 for name, _ in BIAS_RULES}
    rule_call_days: dict[str, int] = {name: 0 for name, _ in BIAS_RULES}
    rule_skip_days: dict[str, int] = {name: 0 for name, _ in BIAS_RULES}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                continue

            prior   = _prior_day_ind(d_str)
            today   = _DAILY_INDICATORS.get(d_str)
            vix     = day_data.get("vix_level")

            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str}")

            # Fixed baselines
            for label, mode in FIXED_BASELINES:
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                rule_trades[label].extend(trades)

            # Bias rules
            for name, fn in BIAS_RULES:
                mode = fn(prior, today, vix)
                if mode is None:
                    rule_skip_days[name] += 1
                    continue
                trades, _ = await _simulate_day(session, day_data, DAILY_SL, baseline_mode=mode)
                rule_trades[name].extend(trades)
                if mode == "always_put":
                    rule_put_days[name] += 1
                else:
                    rule_call_days[name] += 1

    # -- Build results --------------------------------------------------------
    rows = []
    all_names = [name for name, _ in BIAS_RULES] + [name for name, _ in FIXED_BASELINES]
    for name in all_names:
        trades = rule_trades[name]
        if not trades:
            continue
        m = compute_metrics(trades)
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        is_bias = name in dict(BIAS_RULES)
        rows.append({
            "rule":          name,
            "type":          "bias" if is_bias else "baseline",
            "put_days":      rule_put_days.get(name, "-"),
            "call_days":     rule_call_days.get(name, "-"),
            "skip_days":     rule_skip_days.get(name, "-"),
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    _apply_fdr_to_rows(rows)
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    fieldnames = ["rule", "type", "put_days", "call_days", "skip_days",
                  "num_trades", "win_rate_pct", "total_pnl", "avg_win", "avg_loss",
                  "profit_factor", "max_drawdown", "calmar",
                  "p_value", "p_fdr", "sig_fdr"]
    with open(BIAS_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nBias sweep complete -- saved to {BIAS_SWEEP_FILE}")

    sep  = "-" * 110
    sep2 = "═" * 110
    logger.info(f"\n{sep2}")
    logger.info("  BIAS SWEEP RESULTS (sorted by total P&L)")
    logger.info(sep2)
    hdr = (f"  {'Rule':<22} {'Type':<9} {'PUT d':>6} {'CLL d':>6} {'Skip':>5} | "
           f"{'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12}")
    logger.info(hdr)
    logger.info(sep)
    for r in rows:
        pd_str = str(r["put_days"]) if r["put_days"] != "-" else "   -"
        cd_str = str(r["call_days"]) if r["call_days"] != "-" else "   -"
        sk_str = str(r["skip_days"]) if r["skip_days"] != "-" else "  -"
        logger.info(
            f"  {r['rule']:<22} {r['type']:<9} {pd_str:>6} {cd_str:>6} {sk_str:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6.1f} {r['total_pnl']:>12,.0f} "
            f"{r['avg_win']:>9.2f} {r['avg_loss']:>10.2f} {r['profit_factor']:>6.3f} "
            f"{r['max_drawdown']:>12,.0f}"
        )


# ---------------------------------------------
#  COMBO SWEEP RUNNER
# ---------------------------------------------
async def run_combo_sweep():
    """Cross-parameter grid search across VIX filter, spread width, stop-loss,
    and minimum credit simultaneously.

    For each trading day:
      1. Market data is fetched exactly once (seeded for EMA_SLOW bars).
      2. Every combination of (vix_max × width × daily_sl × min_credit) is
         re-simulated against the in-memory quote cache -- no extra I/O.

    After all days, results are saved to COMBO_SWEEP_FILE and automatically
    analyzed:
      • Top 20 combos by total P&L
      • Top 20 combos by Calmar ratio (total_pnl / abs(max_drawdown))
      • Parameter sensitivity: average P&L per value of each parameter
        (shows which single-parameter choices matter most regardless of others)
      • Best combo within each VIX-max bucket
    """
    import itertools

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")

    combos = list(itertools.product(
        COMBO_VIX_MAX,
        COMBO_WIDTHS,
        COMBO_SL_LEVELS,
        COMBO_MIN_CREDITS,
    ))

    # -- Pre-sweep data coverage check --
    # Count available option_quotes parquet files per year so we can detect
    # incomplete data before interpreting results.
    years_in_range = sorted({d.year for d in pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")})
    coverage: dict[int, dict] = {}
    for yr in years_in_range:
        yr_trading_days = [
            d for d in pd.date_range(f"{yr}-01-01", f"{yr}-12-31", freq="B")
            if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS
        ]
        quotes_dir = os.path.join(DATA_DIR, str(yr), "option_quotes")
        available = len(os.listdir(quotes_dir)) if os.path.isdir(quotes_dir) else 0
        coverage[yr] = {"expected": len(yr_trading_days), "available": available,
                        "pct": round(available / len(yr_trading_days) * 100, 1) if yr_trading_days else 0}

    logger.info("=" * 70)
    logger.info("MEFT v35: COMBO SWEEP MODE")
    logger.info(f"VIX max   : {COMBO_VIX_MAX}")
    logger.info(f"Widths    : {COMBO_WIDTHS}")
    logger.info(f"SL levels : {COMBO_SL_LEVELS}")
    logger.info(f"Min credit: {COMBO_MIN_CREDITS}")
    logger.info(f"Combos    : {len(combos)}")
    logger.info(f"Output    : {COMBO_SWEEP_FILE}")
    logger.info("-" * 70)
    logger.info("  DATA COVERAGE (option_quotes)")
    for yr, c in coverage.items():
        status = "✓ COMPLETE" if c["pct"] >= 95 else "⚠ PARTIAL" if c["pct"] >= 50 else "✗ INCOMPLETE"
        logger.info(f"  {yr}: {c['available']:>3}/{c['expected']:>3} days ({c['pct']:>5.1f}%)  {status}")
    all_complete = all(c["pct"] >= 95 for c in coverage.values())
    if not all_complete:
        logger.warning("  WARNING: Some years have incomplete data -- results may be biased toward available periods!")
    logger.info("=" * 70)

    # key -> list of trades
    combo_trades: dict[tuple, list] = {c: [] for c in combos}

    # Coverage tracking during sweep
    days_processed: dict[int, int] = {yr: 0 for yr in years_in_range}
    days_skipped:   dict[int, int] = {yr: 0 for yr in years_in_range}

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                continue

            day_data = await _fetch_day_data(session, d_str, seed_bars=EMA_SLOW)
            if day_data is None:
                days_skipped[d.year] = days_skipped.get(d.year, 0) + 1
                continue

            days_processed[d.year] = days_processed.get(d.year, 0) + 1
            vix_level = day_data.get("vix_level")
            logger.info(
                f"Day {i+1:>3}/{len(date_list)} | {d_str} | VIX={vix_level} -- running {len(combos)} combos..."
            )

            for (vix_max, width, sl, mc) in combos:
                # Apply VIX filter at day level -- skip simulation entirely if filtered
                if vix_max is not None and vix_level is not None and vix_level > vix_max:
                    continue
                trades, _ = await _simulate_day(
                    session, day_data, sl,
                    spread_width=float(width),
                    min_credit=mc if mc > 0 else None,
                    baseline_mode="always_put",   # PUT_ONLY=True equivalent
                )
                combo_trades[(vix_max, width, sl, mc)].extend(trades)

    # -- Build results table --
    rows = []
    for (vix_max, width, sl, mc), trades in combo_trades.items():
        if not trades:
            continue
        m = compute_metrics(trades)
        calmar = (m["total_pnl"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "vix_max":       str(vix_max) if vix_max is not None else "none",
            "width":         width,
            "daily_sl":      str(sl) if sl is not None else "none",
            "min_credit":    mc,
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(calmar, 4) if calmar != float("inf") else 9999.0,
            "p_value":       round(m["p_value"], 4),
        })

    _apply_fdr_to_rows(rows)

    # -- Post-sweep coverage summary --
    logger.info("\n" + "=" * 70)
    logger.info("  DATA COVERAGE SUMMARY (days actually processed by sweep)")
    total_processed = sum(days_processed.values())
    total_skipped   = sum(days_skipped.values())
    for yr in years_in_range:
        p = days_processed.get(yr, 0)
        s = days_skipped.get(yr, 0)
        pct = round(p / (p + s) * 100, 1) if (p + s) > 0 else 0
        status = "✓" if pct >= 95 else "⚠ PARTIAL -- results may be skewed!"
        logger.info(f"  {yr}: {p:>3} processed, {s:>3} skipped ({pct:.1f}% coverage)  {status}")
    logger.info(f"  TOTAL: {total_processed} days processed, {total_skipped} skipped")
    if total_skipped > 0:
        logger.warning(f"  ⚠ {total_skipped} days had missing data and were excluded from ALL combos.")
        logger.warning("    Results reflect only the periods with available data -- interpret with caution!")
    logger.info("=" * 70)

    if not rows:
        logger.warning("Combo sweep produced no trades.")
        return

    # Save full results CSV -- prepend coverage metadata as comment rows
    fieldnames = ["vix_max","width","daily_sl","min_credit","num_trades",
                  "win_rate_pct","total_pnl","avg_win","avg_loss",
                  "profit_factor","max_drawdown","calmar",
                  "p_value","p_fdr","sig_fdr",
                  "days_processed","days_skipped","data_coverage_pct"]
    # Attach per-year coverage counts to every row so the CSV is self-documenting
    yr_processed_str = " | ".join(f"{yr}:{days_processed.get(yr,0)}d" for yr in years_in_range)
    yr_skipped_str   = " | ".join(f"{yr}:{days_skipped.get(yr,0)}d"   for yr in years_in_range)
    coverage_pct_str = " | ".join(f"{yr}:{coverage[yr]['pct']}%" for yr in years_in_range)
    for r in rows:
        r["days_processed"]      = yr_processed_str
        r["days_skipped"]        = yr_skipped_str
        r["data_coverage_pct"]   = coverage_pct_str
    with open(COMBO_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"\nCombo sweep complete -- {len(rows)} combos saved to {COMBO_SWEEP_FILE}")

    # -- Analysis --
    sep  = "-" * 100
    sep2 = "═" * 100

    # Sort helpers
    by_pnl    = sorted(rows, key=lambda r: r["total_pnl"],   reverse=True)
    by_calmar = sorted(rows, key=lambda r: r["calmar"],       reverse=True)

    # 1. Top 20 by total P&L
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY TOTAL P&L")
    logger.info(sep2)
    hdr = f"  {'VIX':>5} {'W':>4} {'SL':>7} {'MC':>5} | {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg Win':>9} {'Avg Loss':>10} {'PF':>6} {'MaxDD':>12} {'Calmar':>8}"
    logger.info(hdr)
    logger.info(sep)
    for r in by_pnl[:20]:
        logger.info(
            f"  {r['vix_max']:>5} {r['width']:>4} {r['daily_sl']:>7} {r['min_credit']:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6} ${r['total_pnl']:>10,.0f} "
            f"${r['avg_win']:>7,.0f} ${r['avg_loss']:>8,.0f} "
            f"{r['profit_factor']:>6.2f} ${r['max_drawdown']:>10,.0f} {r['calmar']:>8.3f}"
        )
    logger.info(sep)

    # 2. Top 20 by Calmar (risk-adjusted)
    logger.info(f"\n{sep2}")
    logger.info("  TOP 20 COMBOS BY CALMAR RATIO  (total_pnl / abs(max_drawdown))")
    logger.info(sep2)
    logger.info(hdr)
    logger.info(sep)
    for r in by_calmar[:20]:
        logger.info(
            f"  {r['vix_max']:>5} {r['width']:>4} {r['daily_sl']:>7} {r['min_credit']:>5} | "
            f"{r['num_trades']:>7} {r['win_rate_pct']:>6} ${r['total_pnl']:>10,.0f} "
            f"${r['avg_win']:>7,.0f} ${r['avg_loss']:>8,.0f} "
            f"{r['profit_factor']:>6.2f} ${r['max_drawdown']:>10,.0f} {r['calmar']:>8.3f}"
        )
    logger.info(sep)

    # 3. Parameter sensitivity -- average P&L per value of each parameter
    logger.info(f"\n{sep2}")
    logger.info("  PARAMETER SENSITIVITY  (avg total_pnl across all combos sharing that value)")
    logger.info(sep2)
    for param in ["vix_max", "width", "daily_sl", "min_credit"]:
        vals: dict[str, list] = {}
        for r in rows:
            k = str(r[param])
            vals.setdefault(k, []).append(r["total_pnl"])
        avg_by_val = sorted(
            [(k, sum(v)/len(v), len(v)) for k, v in vals.items()],
            key=lambda x: x[1], reverse=True
        )
        logger.info(f"\n  {param}:")
        logger.info(f"    {'Value':<10} {'Avg P&L':>12}  {'N combos':>9}")
        logger.info(f"    {'-'*35}")
        for val, avg, n in avg_by_val:
            logger.info(f"    {val:<10} ${avg:>10,.0f}  {n:>9}")

    # 4. Best combo per VIX bucket
    logger.info(f"\n{sep2}")
    logger.info("  BEST COMBO PER VIX-MAX BUCKET  (by total P&L)")
    logger.info(sep2)
    logger.info(hdr)
    logger.info(sep)
    by_vix: dict[str, list] = {}
    for r in rows:
        by_vix.setdefault(r["vix_max"], []).append(r)
    for vix_key in sorted(by_vix.keys(), key=lambda x: (x == "none", x)):
        best = max(by_vix[vix_key], key=lambda r: r["total_pnl"])
        logger.info(
            f"  {best['vix_max']:>5} {best['width']:>4} {best['daily_sl']:>7} {best['min_credit']:>5} | "
            f"{best['num_trades']:>7} {best['win_rate_pct']:>6} ${best['total_pnl']:>10,.0f} "
            f"${best['avg_win']:>7,.0f} ${best['avg_loss']:>8,.0f} "
            f"{best['profit_factor']:>6.2f} ${best['max_drawdown']:>10,.0f} {best['calmar']:>8.3f}"
        )
    logger.info(sep)
    logger.info(f"\n  Full results saved -> {COMBO_SWEEP_FILE}")


# ---------------------------------------------
#  MAIN RUNNER
# ---------------------------------------------
def load_existing_trades() -> tuple[list, str | None]:
    """Read SAVE_FILE and return (trades, last_processed_date_str).
    last_processed_date_str is the max entry_date found (YYYYMMDD), or None."""
    trades = []
    try:
        with open(SAVE_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                row["pnl_earned"] = float(row["pnl_earned"]) if row.get("pnl_earned") else 0.0
                row["win"]        = int(row["win"])  if row.get("win")  else 0
                row["loss"]       = int(row["loss"]) if row.get("loss") else 0
                trades.append(row)
    except FileNotFoundError:
        return [], None
    dates = [t["entry_date"] for t in trades if t.get("entry_date")]
    last_date = max(dates) if dates else None
    return trades, last_date

def is_portfolio_under_pressure(active_positions, current_spot, threshold_pct=0.50):
    """
    Checks if any open position has an unrealized loss exceeding threshold_pct 
    of the credit received.
    """
    if not active_positions:
        return False
        
    for pos in active_positions:
        # Calculate approximate current value of the spread
        # Note: In backtest, we approximate mid-price or use the current spot 
        # to see if the short strike is being challenged.
        
        # Simple Logic: If it's a PUT and spot is approaching short strike,
        # or if it's a CALL and spot is approaching short strike.
        
        # A more accurate 'Pressure' check for your script: 
        # Check if the current price is within X points of the short strike.
        short_strike = pos['short_strike']
        side = pos['option_type']
        
        distance = current_spot - short_strike if side == 'PUT' else short_strike - current_spot
        
        # If distance is less than 15 points, the spread is likely down > 50% in 0DTE
        if distance < 15.0:
            return True
            
    return False

async def run():
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    # -- Resume or new run? --
    resume_from = None
    if os.path.exists(SAVE_FILE):
        print(f"\nFound existing log: {SAVE_FILE}")
        if sys.stdin.isatty():
            choice = input("Resume where you left off? [y/n]: ").strip().lower()
        else:
            choice = "n"  # non-interactive (background/sweep) -- always start fresh
            print("Non-interactive mode -- starting fresh.")
        if choice == "y":
            existing_trades, resume_from = load_existing_trades()
            print(f"Resuming -- {len(existing_trades)} existing trades loaded"
                  + (f", skipping up to {resume_from}." if resume_from else "."))
        else:
            existing_trades = []
            init_log_file()
            print("Starting fresh -- existing log overwritten.")
    else:
        existing_trades = []
        init_log_file()

    all_trades = list(existing_trades)
    total_pnl  = sum(t["pnl_earned"] for t in all_trades)
    total_w    = sum(t["win"]  for t in all_trades)
    total_l    = sum(t["loss"] for t in all_trades)

    _display_run_history()

    logger.info("=" * 60)
    logger.info("MEFT v35: BID/ASK DRIVEN MARATHON")
    logger.info(f"Trades  -> {SAVE_FILE}")
    logger.info(f"Log     -> {LOG_FILE}")
    logger.info("=" * 60)
    print_settings_summary()

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} -- market holiday")
                continue
            if d_str in _CALENDAR_SKIP_DATES:
                logger.info(f"Skipping {d_str} -- calendar filter ({', '.join(sorted(CALENDAR_FILTER_EVENTS))})")
                continue
            if resume_from and d_str <= resume_from:
                logger.info(f"Skipping {d_str} -- already processed (resume)")
                continue
            trades, pnl = await process_day(session, d_str)

            if trades:
                all_trades.extend(trades)
                total_pnl += pnl
                total_w   += sum(t["win"]  for t in trades)
                total_l   += sum(t["loss"] for t in trades)

            total_trades = total_w + total_l
            wr = (total_w / total_trades * 100) if total_trades > 0 else 0.0
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} | Day P&L: ${pnl:>7,.0f} | Total: ${total_pnl:>9,.0f} | WR: {wr:.1f}%")

    if not all_trades:
        logger.warning("No trades recorded.")
        return

    logger.info(f"DONE -- {len(all_trades)} trades logged to {SAVE_FILE}")
    _snapshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_meds.csv")
    import shutil
    shutil.copy2(SAVE_FILE, _snapshot)
    logger.info(f"Trade log snapshot -> {_snapshot}")
    print_performance_report(all_trades, date_list)
    append_results_md(all_trades, date_list)
    print_dynamic_sl_vix_analysis(all_trades)
    print_monthly_pnl_table(all_trades)
    print_spy_comparison(all_trades)
    print_large_loss_days(all_trades)
    print_entry_time_analysis(all_trades)
    print_seasonality_analysis(all_trades)
    print_econ_date_analysis(all_trades)
    _save_run_summary(all_trades, date_list)
    print_vix_analysis(all_trades)
    if RUN_STRIKE_DISTANCE_ANALYSIS:
        print_strike_distance_analysis(all_trades)
    if RUN_PUT_CALL_ANALYSIS:
        print_put_call_analysis(all_trades)


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="MEFT v35 backtest")
    _parser.add_argument("--start",           default=None, help="Override PILOT_YEAR_START (YYYY-MM-DD)")
    _parser.add_argument("--end",             default=None, help="Override PILOT_YEAR_END   (YYYY-MM-DD)")
    _parser.add_argument("--out",             default=None, help="Override output trade CSV path")
    _parser.add_argument("--sl-vix-mid-low",   default=None, type=float, help="Override DYNAMIC_SL_VIX_MID lower bound")
    _parser.add_argument("--sl-vix-mid-high",  default=None, type=float, help="Override DYNAMIC_SL_VIX_MID upper bound")
    _parser.add_argument("--skip-vix-lo",      default=None, type=float, help="Skip day entirely when VIX >= this value (lower bound of SKIP_VIX_RANGE)")
    _parser.add_argument("--skip-vix-hi",      default=None, type=float, help="Skip day entirely when VIX <= this value (upper bound of SKIP_VIX_RANGE)")
    _parser.add_argument("--entry-gate",        default=None, type=float, help="Set INTRADAY_ENTRY_GATE threshold (e.g. 0 = stop new entries when open positions show any loss)")
    _parser.add_argument("--buyback-exit",      default=None, type=float, help="Close position early when remaining premium <= this value (e.g. 0.10)")
    _parser.add_argument("--marathon",         action="store_true",      help="Force a single marathon backtest run, skipping all RUN_* sweeps")
    _parser.add_argument("--kelly",            action="store_true",      help="Enable Kelly zone sizing (ENABLE_KELLY_SIZING=True)")
    _parser.add_argument("--min-otm-distance", default=None, type=float, help="Override MIN_OTM_DISTANCE (pts)")
    _parser.add_argument("--max-credit",       default=None, type=float, help="Override MAX_NET_CREDIT cap")
    _args = _parser.parse_args()
    if _args.start:           PILOT_YEAR_START    = _args.start
    if _args.end:             PILOT_YEAR_END      = _args.end
    if _args.out:             SAVE_FILE           = _args.out
    if _args.sl_vix_mid_low is not None and _args.sl_vix_mid_high is not None:
        DYNAMIC_SL_VIX_MID = (_args.sl_vix_mid_low, _args.sl_vix_mid_high)
    if _args.skip_vix_lo is not None and _args.skip_vix_hi is not None:
        SKIP_VIX_RANGE = (_args.skip_vix_lo, _args.skip_vix_hi)
    if _args.kelly:
        ENABLE_KELLY_SIZING = True
    if _args.entry_gate is not None:
        INTRADAY_ENTRY_GATE = _args.entry_gate
    if _args.buyback_exit is not None:
        PREMIUM_BUYBACK_EXIT = _args.buyback_exit
    if _args.min_otm_distance is not None:
        MIN_OTM_DISTANCE = _args.min_otm_distance
    if _args.max_credit is not None:
        MAX_NET_CREDIT = _args.max_credit

    # Build EOM date set (used by EOM SL and EOM SL sweep)
    _cal_event_sets_startup = _build_calendar_event_dates()
    _EOM_DATES.update(_cal_event_sets_startup.get("end_of_month", set()))
    _CPI_DATES.update(_cal_event_sets_startup.get("cpi", set()))
    _PCE_DATES.update(_cal_event_sets_startup.get("pce", set()))
    _EOQ_DATES.update(_cal_event_sets_startup.get("end_of_quarter", set()))
    _PRE_TW_DATES.update(_cal_event_sets_startup.get("pre_triple_witching", set()))
    _POST_HOL_DATES.update(_cal_event_sets_startup.get("post_major_holiday", set()))

    # Build calendar skip set (PCE + End-of-Month hard skip)
    _CALENDAR_SKIP_DATES: set[str] = set()
    if ENABLE_CALENDAR_FILTER and CALENDAR_FILTER_EVENTS:
        for _ev in CALENDAR_FILTER_EVENTS:
            _CALENDAR_SKIP_DATES |= _cal_event_sets_startup.get(_ev, set())
        logger.info(
            f"Calendar filter active: {sorted(CALENDAR_FILTER_EVENTS)} -- "
            f"{len(_CALENDAR_SKIP_DATES)} dates will be hard-skipped"
        )

    # Build daily indicators: always load basic (VIX change, gap%) for direction signal;
    # only compute full technical indicators (RSI, ADX, MACD, etc.) when day filters are active.
    _needs_full_indicators = any([
        DAY_FILTER_VARPC_MIN, DAY_FILTER_RSI_MIN, DAY_FILTER_RSI_MAX,
        DAY_FILTER_DIST_SMA_MIN, DAY_FILTER_ATR_MAX, DAY_FILTER_BODY_MAX,
        DAY_FILTER_KNIFE_MAX, DAY_FILTER_STOCH_MIN, DAY_FILTER_GAP_MAX,
        DAY_FILTER_ABOVE_SMA5, DAY_FILTER_ABOVE_SMA200,
        DAY_FILTER_ADX_MIN, DAY_FILTER_RANGE_MAX, DAY_FILTER_RANGE_MIN,
        DAY_FILTER_EXP_MOVE_MAX, DAY_FILTER_EXP_MOVE_MIN,
        DAY_FILTER_CCI_MAX, DAY_FILTER_IVR_MIN,
        DAY_FILTER_SKIP_VIX_RISE_DECEL,
        RUN_DAY_FILTER_SWEEP,
    ])
    _needs_indicators = DIRECTION_MODE == "vix_change" or _needs_full_indicators
    if _needs_indicators:
        _mode = "full" if _needs_full_indicators else "basic"
        logger.info(f"Building daily indicators ({_mode} mode)...")
        _DAILY_INDICATORS.update(_build_daily_indicators(compute_full=_needs_full_indicators))
        logger.info(f"  -> {len(_DAILY_INDICATORS)} daily bars loaded")

    load_quote_disk_cache()

    if _args.marathon:
        asyncio.run(run())
    elif RUN_MAX_BP_SWEEP:
        asyncio.run(run_max_bp_sweep())
    elif RUN_TOUCH_SWEEP:
        asyncio.run(run_touch_sweep())
    elif RUN_DAY_FILTER_SWEEP:
        asyncio.run(run_day_filter_sweep())
    elif RUN_DAILY_TP_SWEEP:
        asyncio.run(run_daily_tp_sweep())
    elif RUN_PNL_SAMPLE_SWEEP:
        asyncio.run(run_pnl_sample_sweep())
    elif RUN_ENTRY_TIME_SWEEP:
        asyncio.run(run_entry_time_sweep())
    elif RUN_DIRECTION_TIME_SWEEP:
        asyncio.run(run_direction_time_sweep())
    elif RUN_BIAS_SWEEP:
        asyncio.run(run_bias_sweep())
    elif RUN_COMBO_SWEEP:
        asyncio.run(run_combo_sweep())
    elif RUN_MASTER_SWEEP:
        asyncio.run(run_master_sweep())
    elif RUN_POS_TRAIL_SWEEP:
        asyncio.run(run_pos_trail_sweep())
    elif RUN_MIN_CREDIT_SWEEP:
        asyncio.run(run_min_credit_sweep())
    elif RUN_MIN_OTM_SWEEP:
        asyncio.run(run_min_otm_sweep())
    elif RUN_MAX_CREDIT_SWEEP:
        asyncio.run(run_max_credit_sweep())
    elif RUN_CALENDAR_SWEEP:
        asyncio.run(run_calendar_event_sweep())
    elif RUN_CALL_SL_SWEEP:
        asyncio.run(run_call_sl_sweep())
    elif RUN_VIX_MAG_SWEEP:
        asyncio.run(run_vix_mag_sweep())
    elif RUN_GAP_CALL_SL_SWEEP:
        asyncio.run(run_gap_call_sl_sweep())
    elif RUN_VIX_SUB12_SL_SWEEP:
        asyncio.run(run_vix_sub12_sl_sweep())
    elif RUN_EOM_SL_SWEEP:
        asyncio.run(run_eom_sl_sweep())
    elif RUN_PER_POS_SL_SWEEP:
        asyncio.run(run_per_pos_sl_sweep())
    elif RUN_SL_GAP_SWEEP:
        asyncio.run(run_sl_gap_sweep())
    elif RUN_VIX_ENTRY_CUTOFF_SWEEP:
        asyncio.run(run_vix_entry_cutoff_sweep())
    elif RUN_CALENDAR_RISK_SL_SWEEP:
        asyncio.run(run_calendar_risk_sl_sweep())
    elif RUN_PRESSURE_VIX_SWEEP:
        asyncio.run(run_pressure_vix_sweep())
    elif RUN_SPREAD_WIDTH_SWEEP:
        asyncio.run(run_spread_width_sweep())
    elif RUN_TRAILING_STOP_SWEEP:
        asyncio.run(run_trailing_stop_sweep())
    elif RUN_EMA_SWEEP:
        asyncio.run(run_ema_sweep())
    elif RUN_SL_SWEEP:
        asyncio.run(run_sweep())
    else:
        asyncio.run(run())
        if RUN_BASELINE_COMPARISON:
            asyncio.run(run_baseline_comparison())

    save_quote_disk_cache()
