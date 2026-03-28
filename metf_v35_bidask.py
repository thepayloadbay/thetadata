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

# ─────────────────────────────────────────────
#  MARKET CALENDAR
# ─────────────────────────────────────────────
# Full holidays: market closed all day
MARKET_HOLIDAYS = {
    "20250101","20250120","20250217","20250418","20250526","20250704",
    "20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260703",
    "20260907","20261126","20261225",
}

# FOMC announcement days — kept as a reference set for the calendar event sweep.
# SWEEP RESULT (2022–2026): FOMC days are NET PROFITABLE — skipping costs $23,512 over 34 days.
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

# Triple witching days — kept as a reference set for the calendar event sweep.
# SWEEP RESULT (2022–2026): TW days are NET PROFITABLE — skipping costs $12,212 over 17 days.
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


# ─────────────────────────────────────────────
#  GLOBAL CONFIG
# ─────────────────────────────────────────────
PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"
MCP_URL          = "http://127.0.0.1:25503/mcp/sse"
USE_LOCAL_DATA   = True    # True → read local parquet files; False → live ThetaData MCP
DATA_DIR         = "data"  # root of local parquet cache (used when USE_LOCAL_DATA=True)

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

# ┌─────────────────────────────────────────────────────────────────────┐
# │  CONFIRMED BASELINE  (2022-01-03 → 2026-03-25)  run 2026-03-27     │
# │  Total P&L    : $462,444   Win rate : 93.0%                         │
# │  Max drawdown : -$9,922    Sharpe   : 10.83   Calmar  : 46.6        │
# │  Avg mo P&L   : $9,067     Trades   : 6,751   Days    : 958 / 1103  │
# │  Key settings : WIDTH=20, QTY=2, MIN_CREDIT=0.55, MIN_OTM=30        │
# │                 DIRECTION=vix_change, ENTRY 9:35–12:45 every 20min  │
# │                 DYN_SL: VIX<13 | (13–13.5) | (25–30) → SL=-$500    │
# │                 FOMC/TW/CPI/NFP all traded (filters removed)         │
# └─────────────────────────────────────────────────────────────────────┘
WIDTH          = 20.0
QTY            = 2
MIN_NET_CREDIT    = 0.55
MAX_NET_CREDIT    = None   # skip entry if best qualifying credit exceeds this; None = no cap
                           # SWEEP RESULT: None wins. High-credit days contribute ~$73k total.
                           # Capping at 0.60–1.00 always hurt P&L. 30pt OTM floor already
                           # handles blowup risk — adding a credit cap is redundant.
MIN_OTM_DISTANCE  = 30.0   # minimum OTM distance (pts) for short strike at entry; None = no filter
                           # SWEEP RESULT: 30pts optimal (P&L $320k, Calmar 3.64).
                           # None/10/15/20/25 all had net-negative or near-zero P&L at those levels.
                           # 30pt floor transformed baseline from ~$62k → $320k by eliminating
                           # close-in, low-OTM entries on dangerous days. 35–50 skip too many trades.
PUT_ONLY       = False  # legacy flag — use DIRECTION_MODE instead
DIRECTION_MODE = "vix_change"  # "vix_change" | "always_put" | "always_call" | "ema"
# vix_change: VIX falling → PUT spread (bullish); VIX rising → CALL spread (bearish)
COMMISSION     = 0.50  # per contract per leg (TradeStation)

# ── Benchmark / risk-free rate ──
ANNUAL_RISK_FREE_RATE = 0.05   # 5% — used to adjust Sharpe/Sortino (T-bill rate)
ACCOUNT_SIZE          = 50_000.0  # notional account size ($) — sets rf dollar cost per day

# ── Per-Position Trailing Stop (baseline) ──
# Activates per position once it has profited >= POS_TRAIL_ACTIVATION% of credit.
# Closes that position if P&L then pulls back >= POS_TRAIL_PULLBACK% of credit.
# Set POS_TRAIL_ACTIVATION = None to disable.
POS_TRAIL_ACTIVATION = None   # % of credit to activate trail; None = disabled
                              # SWEEP RESULT: ALL combos worse than no-trail baseline ($320k).
                              # Best tested was Act=90, Pull=20 → ~$120k. With 93%+ expiry win rate,
                              # trailing stops cut winning premium unnecessarily. Keep None.
POS_TRAIL_PULLBACK   = 5.0    # % of credit pullback to trigger close (only used when ACTIVATION set)
NET_DELTA_LIMIT  = 0.30   # max absolute net portfolio delta (0-1 scale per spread unit)
ENABLE_DELTA_LIMIT = False  # set True to enforce net delta limit on entries

# ── IV Rank / VIX Proxy Filter ──
ENABLE_IVR_FILTER = False   # skip day if VIX below threshold (IV not elevated enough to sell premium)
MIN_VIX_LEVEL     = 16.0    # minimum VIX level to trade (proxy for IV rank)

# ── Economic Calendar Filter ──
ENABLE_ECON_FILTER = False  # SWEEP RESULT: CPI days cost $29,790 and NFP days cost $36,214
                            # when skipped — both are net-profitable days for this strategy.
                            # Dynamic SL already handles the risk on volatile release days.
                            # Previously True (blocked new entries on CPI/NFP); data shows wrong call.

# ── Calendar Event Hard Skip ──
# Completely skips trading on the selected calendar event days (hard skip, like holidays).
# SWEEP RESULT (2022–2026): PCE + End-of-Month is the optimal combo —
#   skip 70 days, give up $26k P&L but cut max drawdown 35% (-$98k → -$64k),
#   Calmar 9.34 → 13.95, Sharpe 9.17 → 11.03.
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

# ── Calendar Event Date Sets ──
# Used by run_calendar_event_sweep() to test each event type independently.
# Computed once at import time from pandas date ranges + hard-coded release dates.
def _build_calendar_event_dates() -> "dict[str, set[str]]":
    """Return a dict mapping event_label → set of YYYYMMDD trading-day strings."""
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

    TW_MONTHS = {3, 6, 9, 12}   # triple witching — already in MARKET_HOLIDAYS
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

    # Full Moon dates — snapped to nearest prior trading day if weekend/holiday
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

    # PPI (Producer Price Index) release dates — BLS, ~2nd week of each month
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

    # PCE (Personal Consumption Expenditures) release dates — BEA, ~last Fri of month
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

    # CPI — full 2022–2026 range (ECON_DATES only has 2025–2026)
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

    # NFP — full 2022–2026 range
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

    # FOMC announcement days (currently hard-skipped in MARKET_HOLIDAYS — testing if justified)
    fomc_dates = {
        "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
        "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
        "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
        "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
        "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
    }

    # Triple Witching days (currently hard-skipped in MARKET_HOLIDAYS — testing if justified)
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
    return {
        "fomc":             fomc_dates,   # NOTE: needs MARKET_HOLIDAYS override in sweep
        "triple_witching":  tw_dates,     # NOTE: needs MARKET_HOLIDAYS override in sweep
        "cpi":              cpi_dates   & all_bdays,
        "nfp":              nfp_dates   & all_bdays,
        "ppi":              ppi_dates   & all_bdays,
        "pce":              _pce,
        "monthly_opex":     _opex,
        "end_of_month":     _eom,
        "end_of_quarter":   eoq,
        "first_weekly":     first_weekly,
        "full_moon":        full_moon,
        # ── Combinations ──
        "pce+end_of_month": _pce | _eom,
        "pce+eom+opex":     _pce | _eom | _opex,
    }

# ── VIX Regime Filter ──
ENABLE_VIX_REGIME  = False  # reduce contract size when VIX is elevated
HIGH_VIX_THRESHOLD = 30.0   # VIX level above which we cut size
HIGH_VIX_QTY       = 1      # reduced contracts during high-VIX regime

# ── VIX Day Filter ──
# Set to a float to skip trading days outside this VIX range. None = no filter.
VIX_MIN_FILTER = None   # e.g. 15.0 → skip days where VIX < 15
VIX_MAX_FILTER = None   # no cap — vix_change direction adapts to any VIX regime

VIX_ANALYSIS_FILE = _out("metf_v35_bidask_vix_analysis.csv")

# ── Daily Bar Indicator Filters ──
# Applied to PRIOR day's indicators before deciding to trade today.
# Set each to None to disable. dGapPercent uses today's open vs prior close.
DAY_FILTER_VIX_MAX      = None   # today's VIX <= X (mirrors VIX_MAX_FILTER but for combined filters)
DAY_FILTER_VIX_MIN      = None   # today's VIX >= X (mirrors VIX_MIN_FILTER)
DAY_FILTER_VIX_CHG_MAX  = None   # prior day VIX change% <= X (e.g. 5 = skip if VIX jumped >5%)
DAY_FILTER_VIX_CHG_MIN  = None   # prior day VIX change% >= X (e.g. -5 = skip if VIX dropped >5%)
DAY_FILTER_VARPC_MIN    = None   # prior day close position in range >= X (0–100; 50=closed above midpoint)
DAY_FILTER_RSI_MIN      = None   # prior day RSI(14) >= X (avoid oversold; e.g. 45)
DAY_FILTER_RSI_MAX      = None   # prior day RSI(14) <= X (avoid overbought; e.g. 75)
DAY_FILTER_DIST_SMA_MIN = None   # prior day (close-SMA20)/SMA20 >= X (e.g. -0.03 = no more than 3% below 20d mean)
DAY_FILTER_ATR_MAX      = None   # prior day ATR(14) <= X SPX points (avoid high-vol days)
DAY_FILTER_BODY_MAX     = None   # prior day abs(open-close) <= X SPX points (avoid large directional candles)
DAY_FILTER_KNIFE_MAX    = None   # prior day abs(close-SMA5)/SMA5 <= X (avoid price far from 5d SMA)
DAY_FILTER_STOCH_MIN    = None   # prior day Stoch %K(14) >= X (avoid oversold momentum)
DAY_FILTER_GAP_MAX      = None   # today's abs(gap%) <= X (avoid large gap opens; e.g. 0.5 = 0.5%)
DAY_FILTER_ABOVE_SMA5          = None   # True = only trade when prior close > SMA5
DAY_FILTER_ABOVE_SMA200        = None   # True = only trade when prior close > SMA200
DAY_FILTER_SKIP_VIX_RISE_DECEL = False  # True = skip days where VIX rising but decelerating
                                        # SWEEP RESULT: improves Sharpe 10.60→13.17 but costs ~$13k P&L.
                                        # Rise+decel days still profitable (92%+ WR, $312/day avg).
                                        # Inconsistent year-to-year — likely overfitting. Keep False.

DAILY_TP       = 750.0
DAILY_SL       = None

# ── Dynamic Stop Loss ──
# Danger zones:
#   1. VIX < 13 or VIX 25–30 → apply SL (original rule)
#   2. VIX 13–17 → apply SL (CALL spreads blow up in low-VIX bullish drift)
# All other VIX levels use no stop loss (DAILY_SL = None).
ENABLE_DYNAMIC_SL    = True
DYNAMIC_SL_AMOUNT    = -500.0        # stop loss applied on danger-zone days
DYNAMIC_SL_VIX_LOW   = 13.0         # apply SL when VIX < this
DYNAMIC_SL_VIX_HIGH  = (25.0, 30.0) # apply SL when VIX is in this range (inclusive)
DYNAMIC_SL_VIX_MID   = (13.0, 13.5) # apply SL when VIX is in this range (low-VIX CALL blowup zone)
                                     # SWEEP RESULT: (13.0, 13.5) optimal → $418,774, Sharpe 10.60.
                                     # Prior setting (13.0, 17.0) was too wide — applying SL on VIX 15–17
                                     # days (which have 84.5% WR) cost ~$98k unnecessarily.
                                     # Tightening to just the 13–13.5 danger band unlocked that P&L.

# ── Month + Direction Stop Loss ──
# Applies a stop loss on specific calendar months where PUT or CALL spreads
# historically underperform. Keys are month numbers (1–12).
# "CALL" = apply SL only on CALL spread days, "BOTH" = apply on any direction.
ENABLE_MONTH_DIR_SL  = False  # SWEEP RESULT: costs -$49k vs baseline. Blanket monthly rules
                              # don't account for within-month VIX variation. Jun 2025 alone:
                              # +$11,604 → -$5,808 after applying SL. Keep disabled.
MONTH_DIR_SL_AMOUNT  = -500.0
MONTH_DIR_SL_RULES   = {
    6:  "BOTH",   # Jun: both PUT (83.9%) and CALL (84.4%) weak historically
    7:  "CALL",   # Jul: CALL weak (83.1% WR historically)
    9:  "CALL",   # Sep: CALL weak (91.1%, avg only $26.54 historically)
    3:  "CALL",   # Mar: CALL weak (88.7%, avg only $36.74 historically)
}

ENTRY_START    = time(9, 35)   # SWEEP RESULT: 9:35–12:45 with 20min interval has best Calmar (3.64).
ENTRY_END      = time(12, 45)  # 5-min interval boosts P&L to ~$143k but doubles drawdown to -$66k.
ENTRY_INTERVAL = 20            # More frequent entries = more exposure on bad days. 20min is the
                               # sweet spot balancing opportunity capture vs risk concentration.
MAX_TRADES_DAY = 10
PNL_SAMPLE_INTERVAL = 5  # fetch MTM quotes every N minutes (reduces API calls ~5x vs every 1-min bar)

EMA_FAST       = 10
EMA_SLOW       = 30

# ── Touch Exit ──
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
TOUCH_SWEEP_FILE    = _out("metf_v35_bidask_touch_sweep.csv")

# ── Stop Loss Sweep ──
# Set RUN_SL_SWEEP = True to run a parameter sweep instead of the normal single backtest.
RUN_SL_SWEEP    = False
SWEEP_SL_LEVELS = [-250, -300, -500, -750, -900, -1200, -1500, -1800, -2000, None]  # None = no stop loss
SWEEP_SAVE_FILE = _out("meft_v35_bidask_sl_sweep.csv")

# ── Daily Profit Target Sweep ──
# Sweeps the intraday profit target (closes all positions when day P&L >= level).
# None = no daily profit target (let positions run to expiration).
RUN_DAILY_TP_SWEEP    = False
SWEEP_DAILY_TP_LEVELS = [500.0, 550.0, 600.0, 650.0, 700.0, 750.0, 800.0, 900.0, 1100.0, None]  # None = no daily TP
DAILY_TP_SWEEP_FILE   = _out("metf_v35_bidask_daily_tp_sweep.csv")

# ── PnL Sample Interval Sweep ──
# Sweeps how frequently (in minutes) the MTM check runs to evaluate open positions.
# Lower = more responsive (catches moves faster); higher = fewer checks per day.
RUN_PNL_SAMPLE_SWEEP    = False
SWEEP_PNL_SAMPLE_INTERVALS = [1, 2, 3, 5, 10, 15]  # minutes between MTM checks
PNL_SAMPLE_SWEEP_FILE   = _out("metf_v35_bidask_pnl_sample_sweep.csv")

# ── Max Buying Power Sweep ──
# Caps total committed buying power across all open positions at once.
# At entry, qty is dynamically reduced so that (committed_bp + new_bp) stays within limit.
# None = no limit. BP per contract = (spread_width - credit) × 100.
MAX_BUYING_POWER     = None   # active single-run setting (None = no cap)
RUN_MAX_BP_SWEEP     = False
SWEEP_MAX_BP_LEVELS  = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 40_000, None]
MAX_BP_SWEEP_FILE    = _out("metf_v35_bidask_max_bp_sweep.csv")

# ── Daily Bar Indicator Filter Sweep ──
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
    # ── Momentum oscillators ──
    "dAdx_min":         [15, 20, 25, 30, 40],           # prior day ADX(14) >= X (trending)
    "dCci_min":         [-100, -50, 0, 50, 100],        # prior day CCI(20) >= X
    "dCci_max":         [0, 50, 100, 150, 200],         # prior day CCI(20) <= X
    "dCmo_min":         [-50, -25, 0, 25, 50],          # prior day CMO(9) >= X
    "dMacdBull":        [True],                         # prior day MACD > Signal (bullish cross)
    "dMomentum_min":    [-50, -20, 0, 20, 50],          # prior day Momentum(10) >= X pts
    "dStochD_min":      [20, 30, 40, 50],               # prior day Stoch %D(3) >= X
    "dStochRsiK_min":   [20, 30, 40, 50],               # prior day StochRSI K(3) >= X
    # ── Intraday open-to-entry change ──
    "openChgPct_max":   [0.25, 0.5, 1.0, 1.5, 2.0],    # (entry_price - day_open) / day_open * 100 <= X%
    "openChgPct_min":   [-2.0, -1.5, -1.0, -0.5, -0.25],  # (entry_price - day_open) / day_open * 100 >= X%
    # ── Expected move (VIX-implied 1-day 1σ) ──
    "dExpMoveUSD_min":  [10, 15, 20, 25, 30],           # today expected move >= $X
    "dExpMoveUSD_max":  [30, 40, 50, 60, 80],           # today expected move <= $X
    "dExpMovePct_min":  [0.5, 0.75, 1.0, 1.25, 1.5],   # today expected move >= X%
    "dExpMovePct_max":  [1.0, 1.5, 2.0, 2.5, 3.0],     # today expected move <= X%
    # ── Prior-day trading range ──
    "dRangePct_min":    [0.25, 0.5, 0.75, 1.0, 1.5],   # prior day (H-L)/prevC >= X%
    "dRangePct_max":    [0.75, 1.0, 1.5, 2.0, 3.0],    # prior day (H-L)/prevC <= X%
    # ── IV Rank (52-week VIX rank, prior close) ──
    "dIvRank_min":      [20, 30, 40, 50, 60],           # prior day IV Rank >= X (avoid low-IV)
    "dIvRank_max":      [40, 50, 60, 70, 80],           # prior day IV Rank <= X (avoid high-IV)
}
DAY_FILTER_SWEEP_FILE = _out("metf_v35_bidask_day_filter_sweep.csv")

# ── EMA Parameter Sweep ──
# Set RUN_EMA_SWEEP = True to sweep all (fast, slow) EMA combinations.
# Data is fetched once per day; the simulation is re-run for each combo from cache.
RUN_EMA_SWEEP       = False
SWEEP_EMA_FAST      = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
SWEEP_EMA_SLOW      = [18, 20, 22, 24, 26, 40, 45, 50, 55]
EMA_SWEEP_SAVE_FILE = _out("metf_v35_bidask_ema_sweep.csv")

# ── Trailing Stop Sweep ──
# When enabled, runs the backtest for each trailing stop level.
# A trailing stop closes all positions when portfolio P&L falls back by the given
# dollar amount from its intraday peak. If peak P&L never goes positive, the regular
# daily stop loss still applies. None = no trailing stop.
RUN_TRAILING_STOP_SWEEP  = False
SWEEP_TS_LEVELS          = [50, 100, 150, 200, 300, 400, 500, 600, None]
TRAILING_STOP_SWEEP_FILE = _out("metf_v35_bidask_trailing_stop_sweep.csv")

# ── Per-Position Trailing Stop Sweep ──
# When enabled, sweeps all (activation%, pullback%) combos for a per-position
# trailing stop. A position's trail activates once it has profited >= activation%
# of credit received; it then closes if P&L pulls back >= pullback% of credit
# from that position's peak. Both values are % of credit received (0-100 scale).
RUN_POS_TRAIL_SWEEP        = False
SWEEP_POS_TRAIL_ACTIVATION = [50, 60, 70, 80, 90]   # % of credit to activate trail
SWEEP_POS_TRAIL_PULLBACK   = [5, 10, 15, 20, 25]    # % of credit pullback to close
POS_TRAIL_SWEEP_FILE       = _out("metf_v35_bidask_pos_trail_sweep.csv")

# ── Minimum Net Credit Sweep ──
# When enabled, sweeps the minimum credit required to enter a spread.
# Higher values = fewer but higher-quality entries. Lower values = more trades
# but smaller credits. Results are sorted by total_pnl descending.
RUN_MIN_CREDIT_SWEEP    = False
SWEEP_MIN_CREDIT_LEVELS = [0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 1.00]
MIN_CREDIT_SWEEP_FILE   = _out("metf_v35_bidask_min_credit_sweep.csv")

# ── Max Net Credit Sweep ──
# Sweeps an upper bound on credit collected. If the best qualifying spread gives
# credit above this cap, the entry is skipped (too close to money / too risky).
# None means no cap (baseline behaviour).
RUN_MAX_CREDIT_SWEEP    = False
SWEEP_MAX_CREDIT_LEVELS = [None, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 1.00]
MAX_CREDIT_SWEEP_FILE   = _out("metf_v35_bidask_max_credit_sweep.csv")

# ── Min OTM Distance Sweep ──
# Sweeps the minimum OTM distance (pts) the short strike must be from spot at entry.
# Trades where the nearest qualifying spread is closer than this are skipped.
RUN_MIN_OTM_SWEEP    = False
SWEEP_MIN_OTM_LEVELS = [None, 10, 15, 20, 25, 30, 35, 40, 50]
MIN_OTM_SWEEP_FILE   = _out("metf_v35_bidask_min_otm_sweep.csv")

# ── Spread Width Sweep ──
# When enabled, runs the full backtest across each spread width (distance between
# short and long strike). Market data is fetched once per day and re-used for every
# width — no extra API calls. Results are sorted by total_pnl descending.
RUN_SPREAD_WIDTH_SWEEP  = False
SWEEP_SPREAD_WIDTHS     = [10, 15, 20, 30, 40, 50]
SPREAD_WIDTH_SWEEP_FILE = _out("metf_v35_bidask_spread_width_sweep.csv")

# ── Naive Baseline Comparison ──
# When True, re-runs the same date range after the main backtest with three naive baselines
# and prints/saves a side-by-side summary. Ignored when RUN_SL_SWEEP = True.
RUN_BASELINE_COMPARISON  = False
BASELINE_COMPARISON_FILE = _out("metf_v35_bidask_baseline_comparison.csv")

# ── Strike Distance Analysis ──
# When True, buckets trades by short-strike distance from spot at entry and reports
# trade count, win rate, avg P&L, and total P&L per bucket.
RUN_STRIKE_DISTANCE_ANALYSIS  = True
STRIKE_DISTANCE_ANALYSIS_FILE = _out("metf_v35_bidask_strike_distance_analysis.csv")

# ── PUT vs CALL Split Analysis ──
# When True, groups trades by spread type and reports key metrics for each.
RUN_PUT_CALL_ANALYSIS  = True
PUT_CALL_SPLIT_FILE    = _out("metf_v35_bidask_put_call_split.csv")

# ── Combo Sweep ──
# Set RUN_COMBO_SWEEP = True to run a full cross-parameter grid search.
# All combinations of (vix_max × width × daily_sl × min_credit) are tested.
# Data is fetched once per day; every combo is re-simulated from the cache.
# Results are saved to COMBO_SWEEP_FILE and auto-analyzed (top combos by P&L
# and Calmar, parameter sensitivity rankings, and per-VIX-bucket best combos).
RUN_COMBO_SWEEP      = False
# Sweep 13: entry time window at best settings (width=200, mc=0.56)
# Note: RUN_ENTRY_TIME_SWEEP handles this — keep combo sweep off until done
COMBO_VIX_MAX        = [None]
COMBO_WIDTHS         = [200]
COMBO_SL_LEVELS      = [None]
COMBO_MIN_CREDITS    = [0.56]
COMBO_TS_LEVELS      = [None]   # trailing stop levels for mega combo sweep
COMBO_TP_LEVELS      = [None]   # daily TP levels for mega combo sweep
COMBO_SWEEP_FILE     = _out("metf_v35_bidask_combo_sweep.csv")

# ── Entry Time Sweep ──
# Sweeps all combinations of entry_start × entry_interval (entry_end is fixed).
# Data is fetched once per day; every combo is re-simulated from the cache.
# Results are saved to ENTRY_TIME_SWEEP_FILE.
RUN_ENTRY_TIME_SWEEP   = False
ENTRY_TIME_STARTS      = [time(9, 30), time(9, 45), time(10, 0), time(10, 30), time(11, 0)]
ENTRY_TIME_ENDS        = [time(12, 45), time(13, 30), time(14, 0)]
ENTRY_TIME_INTERVALS   = [5, 10, 15, 20, 30]            # minutes between entry attempts
ENTRY_TIME_SWEEP_FILE  = _out("metf_v35_bidask_entry_time_sweep.csv")
ENTRY_TIME_DOW_FILE    = _out("metf_v35_bidask_entry_time_dow.csv")

# ── Direction × Time Sweep ──
# Compares PUT-only, CALL-only, and Both-Sides at each entry time slot to find
# which direction mode is most profitable at each time of day (and per DOW).
RUN_DIRECTION_TIME_SWEEP = False
DIRECTION_TIME_SLOTS = [                              # individual slots to test
    time(9, 35), time(9, 55), time(10, 15), time(10, 35), time(10, 55),
    time(11, 15), time(11, 35), time(11, 55), time(12, 15), time(12, 35),
]
DIRECTION_TIME_SWEEP_FILE = _out("metf_v35_bidask_direction_time_sweep.csv")
DIRECTION_TIME_DOW_FILE   = _out("metf_v35_bidask_direction_time_dow.csv")

# ── Master Sweep ──
# Set RUN_MASTER_SWEEP = True to run ALL sweeps (SL, EMA, trailing stop, spread
# width, baseline comparison) in a single pass — data is fetched once per day
# and every sweep variant is re-simulated from the cache.  All per-sweep flags
# above are ignored when this mode is active.
RUN_MASTER_SWEEP = False

# ── Calendar Event Filter Sweep ──
# Tests the P&L impact of skipping each calendar event type independently.
# Events: CPI, NFP, PPI, PCE, monthly OPEX, end-of-month, end-of-quarter,
#         first weekly expiry, and full moon.
# For each event, all days that fall on that event are removed from the pool
# and the full backtest is re-run. The delta vs. baseline shows whether
# skipping those days would have hurt or improved P&L.
RUN_CALENDAR_SWEEP  = False
CALENDAR_SWEEP_FILE = _out("metf_v35_bidask_calendar_sweep.csv")

# ── Bias Sweep ──
# Tests each daily indicator as a direction router: bullish signal → PUT spread,
# bearish signal → CALL spread.  Compares against always-PUT, always-CALL, and
# intraday-EMA baselines.
RUN_BIAS_SWEEP      = False
BIAS_SWEEP_FILE     = _out("metf_v35_bidask_bias_sweep.csv")

LOG_COLS = [
    "entry_date", "entry_time", "option_type",
    "short_strike", "long_strike", "width", "spread_width", "credit_received",
    "entry_short_bid", "entry_short_ask", "entry_short_mid",
    "entry_long_bid",  "entry_long_ask",  "entry_long_mid",
    "profit_target", "stop_loss", "ema13", "ema48",
    "qty", "vix_level", "strike_distance",
    "profit_date_time", "profit_price",
    "win", "loss", "outcome", "pnl_earned",
    "close_date", "close_time",
]


# ─────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    from datetime import datetime as _dt
    run_ts   = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_FILE.replace(".log", f"_{run_ts}.log")

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("meft")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File — DEBUG and above, unique filename per run
    fh = logging.FileHandler(log_path, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info(f"Log file  : {log_path}")
    return logger

logger = setup_logger()


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────


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


# ─────────────────────────────────────────────
#  DAILY BAR INDICATORS
# ─────────────────────────────────────────────
_DAILY_INDICATORS: dict = {}   # date_str -> dict of indicator values; built once at startup


def _get_baseline_mode(date_str: str) -> str | None:
    """Return the baseline_mode for _simulate_day based on DIRECTION_MODE.

    "vix_change" reads today's VIX % change from _DAILY_INDICATORS:
        VIX falling (chg <= 0) → "always_put"  (market calmer, sell PUT spread)
        VIX rising  (chg >  0) → "always_call" (market fearful, sell CALL spread)
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
    return None  # "ema" — use intraday EMA direction


def _build_daily_indicators() -> dict:
    """Aggregate 1-min SPX OHLC parquets to daily bars and compute indicators.

    Reads all years present in DATA_DIR so rolling windows (SMA200, ATR14, RSI14, etc.)
    are properly warmed up before the backtest window starts.

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

    # ── Merge daily VIX closes ──
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

    # ── Single-bar indicators ──
    hl = (d["high"] - d["low"]).clip(lower=0.01)
    d["dVarPct"]     = (d["close"] - d["low"]) / hl * 100
    d["dBodySize"]   = (d["open"] - d["close"]).abs()
    d["dGapPercent"] = (d["open"] - d["prev_close"]) / d["prev_close"] * 100

    # ── Moving averages ──
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

    # ── ATR(14): True Range = max(H-L, |H-prevC|, |L-prevC|) ──
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["prev_close"]).abs(),
        (d["low"]  - d["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    d["dATR"] = tr.rolling(14, min_periods=1).mean()

    # ── RSI(14) ──
    delta = d["close"].diff()
    gain  = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss  = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs    = gain / loss.replace(0.0, float("nan"))
    d["dRsi"] = 100.0 - (100.0 / (1.0 + rs))

    # ── Stochastic %K(14) and %D(3) ──
    low14  = d["low"].rolling(14,  min_periods=1).min()
    high14 = d["high"].rolling(14, min_periods=1).max()
    d["dStoch"]  = (d["close"] - low14) / (high14 - low14).clip(lower=0.01) * 100
    d["dStochD"] = d["dStoch"].rolling(3, min_periods=1).mean()

    # ── ADX(14) ──
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

    # ── CCI(20) ──
    tp         = (d["high"] + d["low"] + d["close"]) / 3
    sma_tp     = tp.rolling(20, min_periods=1).mean()
    mean_dev   = tp.rolling(20, min_periods=1).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    d["dCci"]  = (tp - sma_tp) / (0.015 * mean_dev.clip(lower=0.01))

    # ── CMO(9) ──
    delta_cmo  = d["close"].diff()
    sum_up9    = delta_cmo.clip(lower=0).rolling(9, min_periods=1).sum()
    sum_dn9    = (-delta_cmo.clip(upper=0)).rolling(9, min_periods=1).sum()
    d["dCmo"]  = 100 * (sum_up9 - sum_dn9) / (sum_up9 + sum_dn9).clip(lower=0.01)

    # ── MACD(12,26,9) ──
    ema12          = d["close"].ewm(span=12, adjust=False).mean()
    ema26          = d["close"].ewm(span=26, adjust=False).mean()
    d["dMacd"]     = ema12 - ema26
    d["dMacdSig"]  = d["dMacd"].ewm(span=9, adjust=False).mean()
    d["dMacdHist"] = d["dMacd"] - d["dMacdSig"]
    d["dMacdBull"] = d["dMacd"] > d["dMacdSig"]  # True = MACD above signal line

    # ── Momentum(10) ──
    d["dMomentum"] = d["close"] - d["close"].shift(10)

    # ── StochRSI(14,14,3,3): stochastic of RSI(14), smoothed K(3), D(3) ──
    rsi_low14       = d["dRsi"].rolling(14, min_periods=1).min()
    rsi_high14      = d["dRsi"].rolling(14, min_periods=1).max()
    stoch_rsi_raw   = (d["dRsi"] - rsi_low14) / (rsi_high14 - rsi_low14).clip(lower=0.01) * 100
    d["dStochRsiK"] = stoch_rsi_raw.rolling(3, min_periods=1).mean()
    d["dStochRsiD"] = d["dStochRsiK"].rolling(3, min_periods=1).mean()

    # ── Expected Move (VIX-implied 1-day 1σ): requires merged VIX data ──
    if "vix_close" in d.columns:
        d["dExpMoveUSD"] = d["close"] * d["vix_close"] / 100.0 / math.sqrt(252)
        d["dExpMovePct"] = d["vix_close"] / 100.0 / math.sqrt(252) * 100  # same as VIX/sqrt(252)

    # ── IV Rank (52-week VIX percentile, prior close) ──
    if "vix_close" in d.columns:
        vix_s          = d["vix_close"]
        vix_min365     = vix_s.rolling(365, min_periods=30).min()
        vix_max365     = vix_s.rolling(365, min_periods=30).max()
        d["dIvRank"]   = (vix_s - vix_min365) / (vix_max365 - vix_min365).clip(lower=0.01) * 100

    # ── Prior-day trading range as % of prior close ──
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


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  SESSION FACTORY
# ─────────────────────────────────────────────

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
# ─────────────────────────────────────────────
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
        logger.info(f"Got {len(strikes)} strikes. Range: {strikes[0]} – {strikes[-1]}")
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
    logger.info(f"Got {len(strikes)} strikes. Range: {strikes[0]} – {strikes[-1]}")
    return strikes


# ─── Local option_quotes cache (loaded once per trading day) ───
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


# ─────────────────────────────────────────────
#  QUOTE CACHE  (lazy — populated during normal fetches)
# ─────────────────────────────────────────────
_quote_cache: dict = {}  # (right, strike, bar_time_str) -> dict | None


def clear_day_cache():
    """Call once at the start of each day to discard stale quotes."""
    global _quote_cache
    _quote_cache.clear()


async def fetch_quote_cached(
    session, date_str, expiry, right, strike, bar_time_str
) -> dict | None:
    """fetch_quote_at_time with a cache layer — never fetches the same quote twice."""
    key = (right, strike, bar_time_str)
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


# ─────────────────────────────────────────────
#  PER-DAY PROCESSOR  (split into fetch + simulate for SL sweep reuse)
# ─────────────────────────────────────────────

def _sl_label(sl) -> str:
    """Human-readable label for a stop-loss level (None → 'none')."""
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

    # ── 1. Fetch OHLC (today + prior day), VIX, and strikes all in parallel ──
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
            logger.warning("Not enough bars — skipping day.")
            return None
        prev_path = pathlib.Path(DATA_DIR) / prev_str[:4] / "spx_ohlc" / f"{prev_str}.parquet"
        seed_closes = []
        if prev_path.exists():
            df_prev     = pd.read_parquet(prev_path)
            seed_closes = df_prev["close"].tolist()[-seed_bars:]
            logger.info(f"Seeding EMA with {len(seed_closes)} bars from {prev_str}")
        else:
            logger.warning("Could not find prior day — EMA will warm up during the day")
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
            logger.warning("Not enough bars — skipping day.")
            return None
        seed_closes = []
        if not prev_hist.isError:
            prev_closes = [float(f) for f in re.findall(r'"close":\s*([\d\.]+)', prev_hist.content[0].text)]
            seed_closes = prev_closes[-seed_bars:]
            logger.info(f"Seeding EMA with {len(seed_closes)} bars from {prev_str}")
        else:
            logger.warning("Could not fetch prior day — EMA will warm up during the day")
    all_closes = seed_closes + closes
    seed_len   = len(seed_closes)

    # ── Process VIX ──
    if vix_level is not None:
        logger.info(f"VIX level: {vix_level:.2f}")
    else:
        logger.warning("Could not fetch VIX — VIX-based filters disabled for today")

    # ── IV Rank filter: skip entire day if VIX is too low ──
    if ENABLE_IVR_FILTER and vix_level is not None and vix_level < MIN_VIX_LEVEL:
        logger.info(f"VIX={vix_level:.2f} < {MIN_VIX_LEVEL} (MIN_VIX_LEVEL) — skipping day (IVR filter)")
        return None

    # ── VIX day range filter ──
    if VIX_MIN_FILTER is not None and vix_level is not None and vix_level < VIX_MIN_FILTER:
        logger.info(f"VIX={vix_level:.2f} < {VIX_MIN_FILTER} (VIX_MIN_FILTER) — skipping day")
        return None
    if VIX_MAX_FILTER is not None and vix_level is not None and vix_level > VIX_MAX_FILTER:
        logger.info(f"VIX={vix_level:.2f} > {VIX_MAX_FILTER} (VIX_MAX_FILTER) — skipping day")
        return None

    # ── Daily indicator filters ──
    if _DAILY_INDICATORS and not _passes_active_day_filters(date_str, vix_level):
        logger.info(f"Day filter blocked {date_str} — skipping")
        return None

    # ── Economic calendar: block new entries on high-impact event days ──
    econ_skip_entries = ENABLE_ECON_FILTER and date_str in ECON_DATES
    if econ_skip_entries:
        logger.info(f"Economic event date {date_str} — new entries disabled today (econ filter)")

    # ── VIX regime: determine effective contract qty ──
    in_high_vix_regime = ENABLE_VIX_REGIME and vix_level is not None and vix_level >= HIGH_VIX_THRESHOLD
    trade_qty = HIGH_VIX_QTY if in_high_vix_regime else QTY
    if in_high_vix_regime:
        logger.info(f"VIX={vix_level:.2f} >= {HIGH_VIX_THRESHOLD} (HIGH_VIX_THRESHOLD) — reducing size to {HIGH_VIX_QTY} contracts")

    # ── Process strikes ──
    if not all_strikes:
        logger.warning("No strikes found — skipping day.")
        return None

    # ── 2b. Clear quote cache for this day ──
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
) -> tuple:
    """Run the intraday simulation using the pre-populated quote cache.

    Args:
        day_data:      dict returned by _fetch_day_data.
        daily_sl:      stop-loss threshold (float) or None for no stop loss.
        trailing_stop: dollar amount to trail from peak portfolio P&L (positive float),
                       or None to disable. Triggers only after peak P&L has gone positive.
        baseline_mode: None           → normal EMA direction (main strategy)
                       "always_put"   → always trade PUT spreads
                       "always_call"  → always trade CALL spreads
                       "no_ema_filter"→ enter every bar using EMA direction (control baseline)
                       "both_sides"   → enter PUT + CALL spread on every bar (iron condor style)
        ema_fast:      Fast EMA period override (defaults to global EMA_FAST).
        ema_slow:      Slow EMA period override (defaults to global EMA_SLOW).

    Returns:
        (trades, day_pnl) — trades are NOT written to any file.
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

    day_trades_log   = []
    active_positions = []
    stopped_today    = False
    daily_trades     = 0
    current_day_pnl  = 0.0
    peak_day_pnl     = 0.0  # highest portfolio P&L seen this day (for trailing stop)
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

        # ── 3. Mark-to-market ──
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

            # ── Per-position trailing stop ──
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

            # ── Per-position Touch Exit ──
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

            dd = current_day_pnl - peak_day_pnl
            logger.debug(f"[{bar_label}] MTM: {len(active_positions)} open positions | day P&L=${current_day_pnl:.2f} | peak=${peak_day_pnl:.2f} | dd=${dd:.2f}")

        # ── 4. Settlement ──
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
            logger.info(f"[{bar_label}] CLOSING all positions → {outcome} | P&L=${current_day_pnl:.2f} (before commission)")
            for pos in active_positions:
                # 2 legs × 2 (open + close) × position qty × commission rate
                commission_per_pos = 2 * 2 * pos["qty"] * COMMISSION
                if pos["stale_bars"] > 0:
                    logger.warning(f"  Stale data: {pos['option_type']} {pos['short_strike']}/{pos['long_strike']} had {pos['stale_bars']} bars with missing quotes — P&L may be inaccurate")
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

        # ── 5. Entry ──
        _entry_start    = entry_start    if entry_start    is not None else ENTRY_START
        _entry_end      = entry_end      if entry_end      is not None else ENTRY_END
        _entry_interval = entry_interval if entry_interval is not None else ENTRY_INTERVAL
        in_window   = _entry_start <= curr_time <= _entry_end
        on_interval = (dt.minute % _entry_interval == 0)
        can_enter   = in_window and on_interval and not stopped_today and daily_trades < MAX_TRADES_DAY and not econ_skip_entries

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

        for opt_type, right in sides_to_enter:
            logger.info(f"[{bar_label}] Entry attempt | spot={curr_price:.2f} EMA13={e13:.2f} EMA48={e48:.2f} | {opt_type}")

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
                        break  # closest qualifying strike already exceeds cap — skip entry
                    short_strike, long_strike, short_q, long_q, credit = s, l, sq, lq, c
                    logger.info(f"[{bar_label}] Found spread at offset={offset}: {s}/{l} credit={c:.3f} (bid-ask)")
                    break

            if short_strike is None:
                logger.warning(f"[{bar_label}] No spread found with credit >= {credit_threshold} — skipping.")
                continue

            # ── Net delta check ──
            current_net_delta = sum(
                approx_spread_delta(curr_price, p["short_strike"], p["long_strike"])
                for p in active_positions
            )
            new_delta = approx_spread_delta(curr_price, short_strike, long_strike)
            projected_delta = current_net_delta + new_delta
            logger.debug(f"[{bar_label}] Net delta: current={current_net_delta:.3f} new={new_delta:.3f} projected={projected_delta:.3f} limit=±{NET_DELTA_LIMIT}")
            if ENABLE_DELTA_LIMIT and abs(projected_delta) > NET_DELTA_LIMIT:
                logger.warning(f"[{bar_label}] Net delta limit breached ({projected_delta:.3f}) — skipping {opt_type} spread.")
                continue

            # ── Buying power cap: dynamically reduce qty to stay within budget ──
            entry_qty = trade_qty
            if max_buying_power is not None:
                committed_bp  = sum((p["spread_width"] - p["credit_received"]) * p["qty"] * 100 for p in active_positions)
                available_bp  = max_buying_power - committed_bp
                bp_per_contract = (spread_width - credit) * 100
                max_qty = int(available_bp // bp_per_contract) if bp_per_contract > 0 else 0
                entry_qty = min(trade_qty, max_qty)
                if entry_qty <= 0:
                    logger.warning(f"[{bar_label}] Buying power limit (${max_buying_power:,.0f}) reached — skipping entry.")
                    continue

            # ── Price change % from prior close filter ──
            if prior_close and (price_chg_pct_min is not None or price_chg_pct_max is not None):
                chg_pct = (curr_price - prior_close) / prior_close * 100
                if price_chg_pct_min is not None and chg_pct < price_chg_pct_min:
                    logger.warning(f"[{bar_label}] Price chg {chg_pct:.2f}% < min {price_chg_pct_min}% — skipping entry.")
                    continue
                if price_chg_pct_max is not None and chg_pct > price_chg_pct_max:
                    logger.warning(f"[{bar_label}] Price chg {chg_pct:.2f}% > max {price_chg_pct_max}% — skipping entry.")
                    continue

            # ── Price change in standard deviations from prior close ──
            # daily_sigma = prior_close × (VIX/100) / √252 (VIX-implied 1-day 1σ move)
            if prior_close and vix_level and (price_chg_sd_min is not None or price_chg_sd_max is not None):
                daily_sigma = prior_close * (vix_level / 100.0) / math.sqrt(252)
                if daily_sigma > 0:
                    chg_sd = (curr_price - prior_close) / daily_sigma
                    if price_chg_sd_min is not None and chg_sd < price_chg_sd_min:
                        logger.warning(f"[{bar_label}] Price chg {chg_sd:.2f}σ < min {price_chg_sd_min}σ — skipping entry.")
                        continue
                    if price_chg_sd_max is not None and chg_sd > price_chg_sd_max:
                        logger.warning(f"[{bar_label}] Price chg {chg_sd:.2f}σ > max {price_chg_sd_max}σ — skipping entry.")
                        continue

            # ── Price change % from day open to entry bar ──
            if day_open and (open_chg_pct_min is not None or open_chg_pct_max is not None):
                open_chg = (curr_price - day_open) / day_open * 100
                if open_chg_pct_min is not None and open_chg < open_chg_pct_min:
                    logger.warning(f"[{bar_label}] Open chg {open_chg:.2f}% < min {open_chg_pct_min}% — skipping entry.")
                    continue
                if open_chg_pct_max is not None and open_chg > open_chg_pct_max:
                    logger.warning(f"[{bar_label}] Open chg {open_chg:.2f}% > max {open_chg_pct_max}% — skipping entry.")
                    continue

            strike_dist = round(abs(short_strike - curr_price), 2)
            logger.info(f"[{bar_label}] ENTERING {opt_type} spread {short_strike}/{long_strike} credit={credit:.3f} (bid-ask) x {entry_qty} x 100 = ${credit*entry_qty*100:.2f} | net_delta={projected_delta:.3f} | strike_dist={strike_dist:.1f}pts")
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
    if ENABLE_DYNAMIC_SL and day_data.get("vix_level") is not None:
        vix = day_data["vix_level"]
        in_danger = (
            (vix < DYNAMIC_SL_VIX_LOW) or
            (DYNAMIC_SL_VIX_HIGH[0] <= vix <= DYNAMIC_SL_VIX_HIGH[1]) or
            (DYNAMIC_SL_VIX_MID[0] <= vix <= DYNAMIC_SL_VIX_MID[1])
        )
        effective_sl = DYNAMIC_SL_AMOUNT if in_danger else None
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
    return effective_sl


async def process_day(session, date_str: str) -> tuple:
    """Fetch data for one trading day and run the simulation with the configured DAILY_SL.

    Writes each closed trade to SAVE_FILE (existing behaviour preserved).
    """
    day_data = await _fetch_day_data(session, date_str)
    if day_data is None:
        return [], 0.0

    effective_sl = _get_effective_sl(day_data, date_str)
    if ENABLE_DYNAMIC_SL and day_data.get("vix_level") is not None:
        vix = day_data["vix_level"]
        in_danger = (
            (vix < DYNAMIC_SL_VIX_LOW) or
            (DYNAMIC_SL_VIX_HIGH[0] <= vix <= DYNAMIC_SL_VIX_HIGH[1]) or
            (DYNAMIC_SL_VIX_MID[0] <= vix <= DYNAMIC_SL_VIX_MID[1])
        )
        if in_danger:
            logger.info(f"Dynamic SL active: VIX={vix:.2f} in danger zone → SL=${DYNAMIC_SL_AMOUNT:.0f}")
    if ENABLE_MONTH_DIR_SL and effective_sl == MONTH_DIR_SL_AMOUNT:
        month = int(date_str[4:6])
        rule  = MONTH_DIR_SL_RULES.get(month, "")
        direction = _get_baseline_mode(date_str)
        logger.info(f"Month/Dir SL active: month={month} rule={rule} direction={'CALL' if direction == 'always_call' else 'PUT'} → SL=${MONTH_DIR_SL_AMOUNT:.0f}")

    baseline_mode = _get_baseline_mode(date_str)
    trades, day_pnl = await _simulate_day(
        session, day_data, effective_sl,
        pos_trail_activation=POS_TRAIL_ACTIVATION,
        pos_trail_pullback=POS_TRAIL_PULLBACK,
        baseline_mode=baseline_mode,
        min_otm_distance=MIN_OTM_DISTANCE,
        max_credit=MAX_NET_CREDIT,
    )
    for pos in trades:
        append_trade(pos)
    return trades, day_pnl


# ─────────────────────────────────────────────
#  VIX CORRELATION ANALYSIS
# ─────────────────────────────────────────────
VIX_BUCKETS = [
    ("< 15",  None, 15.0),
    ("15–20", 15.0, 20.0),
    ("20–25", 20.0, 25.0),
    ("25–30", 25.0, 30.0),
    ("30+",   30.0, None),
]


def print_vix_analysis(all_trades: list) -> None:
    """Bucket trades by VIX level and report win rate, avg P&L, and count.
    Prints to console and saves to VIX_ANALYSIS_FILE."""
    rows = []
    sep = "─" * 62
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

    # ── Save to CSV ──
    analysis_cols = ["vix_range", "trade_count", "win_rate_pct", "avg_pnl", "total_pnl"]
    with open(VIX_ANALYSIS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=analysis_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"  VIX analysis saved to {VIX_ANALYSIS_FILE}")
    logger.info(sep)


# ─────────────────────────────────────────────
#  STRIKE DISTANCE ANALYSIS
# ─────────────────────────────────────────────
STRIKE_DISTANCE_BUCKETS = [
    ("0–10",   0,   10),
    ("10–20",  10,  20),
    ("20–30",  20,  30),
    ("30–40",  30,  40),
    ("40–50",  40,  50),
    ("50+",    50,  None),
]


def print_strike_distance_analysis(all_trades: list) -> None:
    """Bucket trades by short-strike distance from spot at entry and report
    trade count, win rate, avg P&L, and total P&L per bucket.
    Prints to console and saves to STRIKE_DISTANCE_ANALYSIS_FILE."""
    rows = []
    sep = "─" * 70
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


# ─────────────────────────────────────────────
#  PUT vs CALL SPLIT ANALYSIS
# ─────────────────────────────────────────────
def print_put_call_analysis(all_trades: list) -> None:
    """Group trades by PUT vs CALL spread type and report key metrics.
    Prints to console and saves to PUT_CALL_SPLIT_FILE."""
    rows = []
    sep = "─" * 95
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


# ─────────────────────────────────────────────
#  PERFORMANCE ANALYTICS
# ─────────────────────────────────────────────
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

    # Per-day P&L — used for Sharpe/Sortino and daily stats
    day_pnls: dict = {}
    for t in all_trades:
        day_pnls.setdefault(t["entry_date"], 0.0)
        day_pnls[t["entry_date"]] += t["pnl_earned"]
    best_day  = max(day_pnls.values()) if day_pnls else 0
    worst_day = min(day_pnls.values()) if day_pnls else 0

    # Sharpe & Sortino — daily P&L basis, rf-adjusted, annualised with sqrt(252)
    # rf_daily = opportunity cost of tying up ACCOUNT_SIZE in T-bills for one day
    rf_daily     = ANNUAL_RISK_FREE_RATE * ACCOUNT_SIZE / 252
    daily_vals   = list(day_pnls.values())
    n_days       = len(daily_vals)
    mean_daily   = sum(daily_vals) / n_days if n_days else 0
    var_daily    = sum((p - mean_daily) ** 2 for p in daily_vals) / n_days if n_days else 0
    std_daily    = math.sqrt(var_daily)
    sharpe       = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily else 0

    downside_d   = [p for p in daily_vals if p < mean_daily]
    down_var_d   = sum((p - mean_daily) ** 2 for p in downside_d) / len(downside_d) if downside_d else 0
    sortino      = ((mean_daily - rf_daily) / math.sqrt(down_var_d) * math.sqrt(252)) if down_var_d else 0

    # Max drawdown (on cumulative P&L curve per trade)
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = min(max_dd, equity - peak)
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    sep = "─" * 52
    logger.info(sep)
    logger.info("  PERFORMANCE REPORT")
    logger.info(sep)
    logger.info(f"  Period          : {date_list[0].strftime('%Y-%m-%d')} → {date_list[-1].strftime('%Y-%m-%d')}")
    days_in_range  = len(date_list)
    days_traded    = len(day_pnls)
    days_skipped   = days_in_range - days_traded
    logger.info(f"  Business days   : {days_in_range}  (skipped {days_skipped} — holidays/FOMC/TW/calendar filter)")
    logger.info(f"  Days traded     : {days_traded}")
    logger.info(f"  Total trades    : {n}  (wins: {len(wins)}  losses: {len(losses)})")
    logger.info(sep)
    logger.info(f"  Total P&L       : ${total_pnl:>10,.2f}")
    logger.info(f"  Win rate        : {win_rate:>9.1f}%")
    logger.info(f"  Avg win         : ${avg_win:>10,.2f}")
    logger.info(f"  Avg loss        : ${avg_loss:>10,.2f}")
    logger.info(f"  Expectancy/trade: ${expectancy:>10,.2f}")
    logger.info(f"  Profit factor   : {profit_factor:>10.2f}x")
    logger.info(sep)
    logger.info(f"  Sharpe ratio    : {sharpe:>10.2f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, acct=${ACCOUNT_SIZE:,.0f})")
    logger.info(f"  Sortino ratio   : {sortino:>10.2f}")
    logger.info(f"  Max drawdown    : ${max_dd:>10,.2f}  ({max_dd_pct:.1f}%)")
    logger.info(sep)
    logger.info(f"  Best day        : ${best_day:>10,.2f}")
    logger.info(f"  Worst day       : ${worst_day:>10,.2f}")
    logger.info(sep)


# ─────────────────────────────────────────────
#  RUN HISTORY  (persist last N run summaries)
# ─────────────────────────────────────────────
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
        "period":        f"{PILOT_YEAR_START} → {PILOT_YEAR_END}",
        "days_in_range": days_in_range,
        "days_traded":   days_traded,
        # key params
        "width":         WIDTH,
        "qty":           QTY,
        "min_credit":    MIN_NET_CREDIT,
        "max_credit":    str(MAX_NET_CREDIT),
        "min_otm":       str(MIN_OTM_DISTANCE),
        "direction":     DIRECTION_MODE,
        "entry_window":  f"{ENTRY_START.strftime('%H:%M')}–{ENTRY_END.strftime('%H:%M')} every {ENTRY_INTERVAL}min",
        "dyn_sl":        f"VIX<{DYNAMIC_SL_VIX_LOW} | {DYNAMIC_SL_VIX_MID} | {DYNAMIC_SL_VIX_HIGH}" if ENABLE_DYNAMIC_SL else "off",
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
    logger.info(f"Run summary saved → {RUN_HISTORY_FILE}")


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
    sep2 = "─" * w
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
            f"  Params → min_otm={otm}  dyn_sl={dyn}  "
            f"entry={win}  cal_filter={cal_f}"
        )
    logger.info(sep)
    logger.info("")


# ─────────────────────────────────────────────
#  METRICS HELPER  (shared by normal run + sweep)
# ─────────────────────────────────────────────
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

    # Sharpe & Sortino — daily P&L basis, rf-adjusted, annualised with sqrt(252)
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


# ─────────────────────────────────────────────
#  STATISTICAL HELPERS  (p-value, FDR correction)
# ─────────────────────────────────────────────
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

    sig_fdr = True  → survives Benjamini-Hochberg correction at alpha = 0.05.
    sig_fdr = False → result is not statistically significant after correction.
    """
    if not rows:
        return rows
    pvals    = [r.get(p_col, 1.0) for r in rows]
    adjusted = _fdr_adjust(pvals)
    for r, adj in zip(rows, adjusted):
        r["p_fdr"]   = round(adj, 4)
        r["sig_fdr"] = adj < 0.05
    return rows


# ─────────────────────────────────────────────
#  STOP-LOSS SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Write summary CSV ──
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

    logger.info(f"\nSweep complete — summary saved to {SWEEP_SAVE_FILE}")

    # ── Print comparison table ──
    col_w = 12
    hdr   = (f"{'SL':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "─" * len(hdr)
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

    # ── VIX analysis across all sweep trades (combined) ──
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


# ─────────────────────────────────────────────
#  EMA PARAMETER SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_ema_sweep():
    """Run an EMA fast/slow parameter sweep.

    For each date the market data is fetched exactly once (seeded with enough
    bars for the largest slow EMA period).  The simulation is then re-run for
    every (fast, slow) combination against the in-memory quote cache — no extra
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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Build result rows sorted by total_pnl descending ──
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

    # ── Write CSV (drop sort key) ──
    with open(EMA_SWEEP_SAVE_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ema_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in ema_sweep_cols})

    logger.info(f"\nEMA sweep complete — summary saved to {EMA_SWEEP_SAVE_FILE}")

    # ── Print comparison table ──
    hdr = (
        f"  {'Fast':>4} | {'Slow':>4} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  MINIMUM NET CREDIT SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for mc in SWEEP_MIN_CREDIT_LEVELS:
                trades, day_pnl = await _simulate_day(session, day_data, DAILY_SL, min_credit=mc)
                mc_trades[mc].extend(trades)
                logger.debug(f"  min_credit={mc} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # ── Build result rows ──
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

    logger.info(f"\nMin credit sweep complete — saved to {MIN_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MinCredit':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  MINIMUM OTM DISTANCE SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Build result rows ──
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

    logger.info(f"\nMin OTM sweep complete — saved to {MIN_OTM_SWEEP_FILE}")

    hdr = (
        f"  {'MinOTM':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  MAX NET CREDIT SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Build result rows ──
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

    logger.info(f"\nMax credit sweep complete — saved to {MAX_CREDIT_SWEEP_FILE}")

    hdr = (
        f"  {'MaxCredit':>10} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  DAILY PROFIT TARGET SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for tp in SWEEP_DAILY_TP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode="always_put",
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    daily_tp=tp,
                )
                tp_trades[str(tp)].extend(trades)
                logger.debug(f"  daily_tp={tp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # ── Build result rows ──
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

    logger.info(f"\nDaily TP sweep complete — saved to {DAILY_TP_SWEEP_FILE}")

    hdr = (
        f"  {'DailyTP':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  PNL SAMPLE INTERVAL SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for iv in SWEEP_PNL_SAMPLE_INTERVALS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode="always_put",
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    pnl_sample_interval=iv,
                )
                interval_trades[iv].extend(trades)
                logger.debug(f"  pnl_sample={iv}m | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # ── Build result rows ──
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

    logger.info(f"\nPnL sample interval sweep complete — saved to {PNL_SAMPLE_SWEEP_FILE}")

    hdr = (
        f"  {'Interval':>9} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  MAX BUYING POWER SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
                continue

            day_data = await _fetch_day_data(session, d_str)
            if day_data is None:
                continue

            for bp in SWEEP_MAX_BP_LEVELS:
                trades, day_pnl = await _simulate_day(
                    session, day_data, DAILY_SL,
                    baseline_mode="always_put",
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    max_buying_power=bp,
                )
                bp_trades[str(bp)].extend(trades)
                logger.debug(f"  max_bp={bp} | {d_str} | {len(trades)} trades | day P&L=${day_pnl:,.2f}")

    # ── Build result rows ──
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

    logger.info(f"\nMax BP sweep complete — saved to {MAX_BP_SWEEP_FILE}")

    hdr = (
        f"  {'Max BP':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Calmar':>8} | {'Avg Qty':>7}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  TOUCH EXIT SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_touch_sweep():
    """Sweep Touch Exit thresholds — both dollar and % variants.

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
                logger.info(f"Skipping {d_str} — market holiday")
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
                    baseline_mode="always_put",
                    spread_width=WIDTH,
                    min_credit=MIN_NET_CREDIT,
                    entry_start=ENTRY_START,
                    entry_end=ENTRY_END,
                    entry_interval=ENTRY_INTERVAL,
                    touch_exit_dollars=t_dollars,
                    touch_exit_pct=t_pct,
                )
                trade_map[str((kind, val))].extend(trades)

    # ── Build result rows ──
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

    logger.info(f"\nTouch exit sweep complete — saved to {TOUCH_SWEEP_FILE}")

    hdr = (
        f"  {'Type':>6} | {'Value':>8} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  DAILY BAR INDICATOR FILTER SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_day_filter_sweep():
    """Test each daily indicator as an independent day-selection filter.

    Data is fetched once per day (respecting VIX and other global filters).
    For each (indicator, threshold) combo the same day pool is re-filtered and
    re-simulated. Results sorted by Calmar descending within each indicator group.
    """
    global _DAILY_INDICATORS
    if not _DAILY_INDICATORS:
        logger.info("Building daily indicator table from local parquets…")
        _DAILY_INDICATORS = _build_daily_indicators()
        logger.info(f"  → {len(_DAILY_INDICATORS)} daily bars loaded")

    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    logger.info("=" * 70)
    logger.info("MEFT v35: DAILY BAR INDICATOR FILTER SWEEP MODE")
    logger.info(f"Filters   : {list(SWEEP_DAY_FILTERS.keys())}")
    logger.info(f"Output    : {DAY_FILTER_SWEEP_FILE}")
    logger.info("=" * 70)

    # ── Step 1: Pre-fetch all days with VIX filter OFF so vix_max can be swept ──
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
        # ── Step 2: Baseline (no extra filter) ──
        baseline_trades = []
        for d_str, day_data in day_pool.items():
            trades, _ = await _simulate_day(
                session, day_data, DAILY_SL,
                baseline_mode="always_put",
                spread_width=WIDTH,
                min_credit=MIN_NET_CREDIT,
                entry_start=ENTRY_START,
                entry_end=ENTRY_END,
                entry_interval=ENTRY_INTERVAL,
            )
            baseline_trades.extend(trades)

        # ── Step 3: For each filter × threshold, re-filter the day pool and simulate ──
        cols = [
            "filter_name", "threshold", "days_traded", "days_filtered_out",
            "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar",
        ]
        rows = []

        # Baseline row
        bm = compute_metrics(baseline_trades)
        bpf = f"{bm['profit_factor']:.2f}" if bm['profit_factor'] != float("inf") else "inf"
        bcalmar = bm["total_pnl"] / abs(bm["max_drawdown"]) if bm["max_drawdown"] != 0 else float("inf")
        rows.append({
            "filter_name":       "BASELINE (no filter)",
            "threshold":         "—",
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
        })

        for filter_name, thresholds in SWEEP_DAY_FILTERS.items():
            for threshold in thresholds:
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
                        baseline_mode="always_put",
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
                rows.append({
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
                })
                logger.info(
                    f"  {filter_name:<22} threshold={str(threshold):<8} "
                    f"days={days_in:>3} | trades={m['num_trades']:>5} | "
                    f"pnl=${m['total_pnl']:>10,.2f} | calmar={cal:>7.2f}"
                )

    # ── Save CSV ──
    with open(DAY_FILTER_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    logger.info(f"\nDay filter sweep complete — saved to {DAY_FILTER_SWEEP_FILE}")

    # ── Print grouped summary (best threshold per indicator by Calmar) ──
    hdr = (
        f"  {'Filter':<22} | {'Threshold':>10} | {'Days':>5} | {'Trades':>7} | "
        f"{'WR%':>6} | {'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    )
    sep = "─" * len(hdr)
    logger.info(sep)
    logger.info("  DAY FILTER SWEEP — BEST THRESHOLD PER INDICATOR (by Calmar)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)

    # Group by filter, pick best Calmar per group
    best: dict[str, dict] = {}
    for row in rows:
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


# ─────────────────────────────────────────────
#  CALENDAR EVENT FILTER SWEEP RUNNER
# ─────────────────────────────────────────────
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

    # ── Step 1: Pre-fetch all days ──
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
        # ── Step 2: Baseline (no calendar filter, full settings) ──
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
            "pnl_delta":     "—",
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

        # ── Step 3: For each event type, remove those dates and re-simulate ──
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

    # ── Save CSV ──
    with open(CALENDAR_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in cols})
    logger.info(f"\nCalendar event sweep saved → {CALENDAR_SWEEP_FILE}")

    # ── Print summary table sorted by P&L delta ──
    data_rows = [r for r in rows if r["pnl_delta"] != "—"]
    data_rows.sort(key=lambda r: float(r["pnl_delta"]), reverse=True)

    hdr = (
        f"  {'Event Type':<20} | {'Skipped':>7} | {'Traded':>6} | "
        f"{'Trades':>7} | {'WR%':>5} | {'Total P&L':>12} | "
        f"{'Delta':>11} | {'Max DD':>10} | {'Calmar':>8} | {'Sharpe':>7}"
    )
    sep = "─" * len(hdr)
    logger.info("")
    logger.info(sep)
    logger.info("  CALENDAR EVENT SWEEP — sorted by P&L delta (positive = skipping helps)")
    logger.info(sep)
    logger.info(hdr)
    logger.info(sep)
    # Print baseline first
    br = rows[0]
    logger.info(
        f"  {br['event_type']:<20} | {'—':>7} | {br['days_traded']:>6} | "
        f"{br['num_trades']:>7} | {br['win_rate_pct']:>4}% | "
        f"${float(br['total_pnl']):>11,.2f} | {'—':>11} | "
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


# ─────────────────────────────────────────────
#  TRAILING STOP SWEEP RUNNER
# ─────────────────────────────────────────────
def _ts_label(ts) -> str:
    """Human-readable label for a trailing stop level (None → 'none')."""
    return "none" if ts is None else str(int(ts))


async def run_trailing_stop_sweep():
    """Run a trailing stop parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every level in SWEEP_TS_LEVELS using the
    in-memory quote cache — no extra API calls are needed.

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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Write summary CSV ──
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

    logger.info(f"\nTrailing stop sweep complete — summary saved to {TRAILING_STOP_SWEEP_FILE}")

    # ── Print comparison table ──
    col_w = 10
    hdr   = (f"{'Trail $':>{col_w}} | {'Trades':>7} | {'WR%':>6} | "
             f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
             f"{'Max DD':>10} | {'PF':>6}")
    sep   = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  PER-POSITION TRAILING STOP SWEEP RUNNER
# ─────────────────────────────────────────────
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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Build result rows ──
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

    # ── Write CSV ──
    with open(POS_TRAIL_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pt_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in pt_cols})

    logger.info(f"\nPos trail sweep complete — saved to {POS_TRAIL_SWEEP_FILE}")

    # ── Print table ──
    hdr = (
        f"  {'Act%':>5} | {'Pull%':>5} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10}"
    )
    sep = "─" * len(hdr)
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


# ─────────────────────────────────────────────
#  SPREAD WIDTH SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_spread_width_sweep():
    """Run a spread width parameter sweep.

    For each date the market data is fetched exactly once.  The simulation is
    then re-run independently for every width in SWEEP_SPREAD_WIDTHS using the
    in-memory quote cache — no extra API calls are needed.

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
                logger.info(f"Skipping {d_str} — market holiday")
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

    # ── Build result rows ──
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

    # ── Write CSV (drop sort key) ──
    with open(SPREAD_WIDTH_SWEEP_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sw_sweep_cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in sw_sweep_cols})

    logger.info(f"\nSpread width sweep complete — summary saved to {SPREAD_WIDTH_SWEEP_FILE}")

    # ── Print comparison table ──
    hdr = (
        f"  {'Width':>6} | {'Trades':>7} | {'WR%':>6} | "
        f"{'Total P&L':>12} | {'Avg Win':>9} | {'Avg Loss':>9} | "
        f"{'Prof Fac':>9} | {'Max DD':>10} | {'Avg Credit':>11}"
    )
    sep = "─" * len(hdr)
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

    # ── Full performance report for the default WIDTH ──
    default_trades = width_trades.get(WIDTH, [])
    if default_trades:
        logger.info(f"\n  Full performance report for default width={WIDTH}:")
        print_performance_report(default_trades, date_list)


# ─────────────────────────────────────────────
#  NAIVE BASELINE COMPARISON
# ─────────────────────────────────────────────
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

    # ── Build comparison rows ──
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

    # ── SPX buy-and-hold benchmark ──
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

    # ── Save CSV ──
    with open(BASELINE_COMPARISON_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=comp_cols)
        w.writeheader()
        w.writerows(rows)
    logger.info(f"Baseline comparison saved to {BASELINE_COMPARISON_FILE}")

    # ── Print comparison table ──
    sep = "─" * 90
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

    # ── Print SPX benchmark ──
    if spx_row:
        logger.info("")
        logger.info(f"  SPX BUY-AND-HOLD BENCHMARK  "
                    f"({spx_row['start']} → {spx_row['end']} | "
                    f"{spx_row['start_price']:,.0f} → {spx_row['end_price']:,.0f})")
        logger.info(sep)
        logger.info(f"  Total return    : {spx_row['total_return_pct']:>8.1f}%")
        logger.info(f"  Ann. return     : {spx_row['ann_return_pct']:>8.1f}%  (CAGR)")
        logger.info(f"  Sharpe ratio    : {spx_row['sharpe']:>8.3f}  (rf={ANNUAL_RISK_FREE_RATE*100:.1f}%, daily returns)")
        logger.info(f"  Sortino ratio   : {spx_row['sortino']:>8.3f}")
        logger.info(f"  Max drawdown    : {spx_row['max_drawdown_pct']:>8.1f}%  (price-based)")
        logger.info(sep)


# ─────────────────────────────────────────────
#  MASTER SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_master_sweep():
    """Run ALL sweep variants in a single pass.

    For each trading day:
      1. Market data is fetched exactly once (seeded for the largest EMA period).
      2. Every sweep variant is re-simulated against the in-memory quote cache —
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

    # ── Accumulators ──
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
                logger.info(f"Skipping {d_str} — market holiday")
                continue

            # ── Fetch market data once per day ──
            clear_day_cache()
            day_data = await _fetch_day_data(session, d_str, seed_bars=max_seed)
            if day_data is None:
                continue

            logger.info(
                f"Day {i+1:>3}/{len(date_list)} | {d_str} — running all sweep variants..."
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
    #  OUTPUT — SL SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — EMA SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {EMA_SWEEP_SAVE_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — TRAILING STOP SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {TRAILING_STOP_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — SPREAD WIDTH SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {SPREAD_WIDTH_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — PER-POSITION TRAILING STOP SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {POS_TRAIL_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — MIN CREDIT SWEEP
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
    sep = "─" * len(hdr)
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
    logger.info(f"  Saved → {MIN_CREDIT_SWEEP_FILE}")

    # ════════════════════════════════════════════════════════════
    #  OUTPUT — BASELINE COMPARISON
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
    sep = "─" * 90
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
    logger.info(f"  Saved → {BASELINE_COMPARISON_FILE}")

    # ════════════════════════════════════════════════════════════
    #  FULL PERFORMANCE REPORT — default config (SL=DAILY_SL, WIDTH=WIDTH)
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


# ─────────────────────────────────────────────
#  ENTRY TIME SWEEP RUNNER
# ─────────────────────────────────────────────
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
    # (combo, dow) → trades, where dow: 0=Mon … 4=Fri
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
            logger.info(f"Day {i+1:>3}/{len(date_list)} | {d_str} — running {len(combos)} entry-time combos...")

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

    # ── Build results table ──
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
    logger.info(f"\nEntry time sweep complete — {len(rows)} combos saved to {ENTRY_TIME_SWEEP_FILE}")

    sep  = "─" * 100
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

    # ── Day-of-week breakdown ────────────────────────────────────────────────
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


# ─────────────────────────────────────────────
#  DIRECTION × TIME SWEEP
# ─────────────────────────────────────────────
async def run_direction_time_sweep():
    """For each entry time slot, compare PUT-only, CALL-only, and Both-Sides.

    Data is fetched once per day. Each (slot, mode) combo is simulated with a
    one-bar entry window (entry_start == entry_end == slot) so exactly one entry
    attempt is made per day per combo.

    Outputs:
      DIRECTION_TIME_SWEEP_FILE — aggregate metrics per (slot, mode)
      DIRECTION_TIME_DOW_FILE   — same broken down by day of week
    Console prints the best mode per slot and per (slot, DOW).
    """
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq="B")

    _DIRECTION_MODES = [
        ("PUT Only",   "always_put"),
        ("CALL Only",  "always_call"),
        ("Both Sides", "both_sides"),
    ]
    DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

    # (slot_str, mode_label) → trades
    slot_mode_trades: dict[tuple, list] = {}
    # (slot_str, mode_label, dow_name) → trades
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

    # ── Build aggregate rows ─────────────────────────────────────────────────
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

    # ── Build DOW rows ───────────────────────────────────────────────────────
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

    # ── Console: best mode per slot (aggregate) ──────────────────────────────
    sep  = "─" * 80
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

    # ── Console: best mode per slot × DOW ────────────────────────────────────
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


# ─────────────────────────────────────────────
#  BIAS SWEEP
# ─────────────────────────────────────────────
async def run_bias_sweep():
    """Test daily indicators as direction routers: bullish signal → PUT spread,
    bearish signal → CALL spread.

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
        # VIX falling → calmer market → bullish → PUT; VIX rising → fearful → CALL
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
        # Prior day closed near high → bullish → trade PUT today
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

    # ── Build results ────────────────────────────────────────────────────────
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
    logger.info(f"\nBias sweep complete — saved to {BIAS_SWEEP_FILE}")

    sep  = "─" * 110
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


# ─────────────────────────────────────────────
#  COMBO SWEEP RUNNER
# ─────────────────────────────────────────────
async def run_combo_sweep():
    """Cross-parameter grid search across VIX filter, spread width, stop-loss,
    and minimum credit simultaneously.

    For each trading day:
      1. Market data is fetched exactly once (seeded for EMA_SLOW bars).
      2. Every combination of (vix_max × width × daily_sl × min_credit) is
         re-simulated against the in-memory quote cache — no extra I/O.

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

    # ── Pre-sweep data coverage check ──
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
    logger.info("─" * 70)
    logger.info("  DATA COVERAGE (option_quotes)")
    for yr, c in coverage.items():
        status = "✓ COMPLETE" if c["pct"] >= 95 else "⚠ PARTIAL" if c["pct"] >= 50 else "✗ INCOMPLETE"
        logger.info(f"  {yr}: {c['available']:>3}/{c['expected']:>3} days ({c['pct']:>5.1f}%)  {status}")
    all_complete = all(c["pct"] >= 95 for c in coverage.values())
    if not all_complete:
        logger.warning("  WARNING: Some years have incomplete data — results may be biased toward available periods!")
    logger.info("=" * 70)

    # key → list of trades
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
                f"Day {i+1:>3}/{len(date_list)} | {d_str} | VIX={vix_level} — running {len(combos)} combos..."
            )

            for (vix_max, width, sl, mc) in combos:
                # Apply VIX filter at day level — skip simulation entirely if filtered
                if vix_max is not None and vix_level is not None and vix_level > vix_max:
                    continue
                trades, _ = await _simulate_day(
                    session, day_data, sl,
                    spread_width=float(width),
                    min_credit=mc if mc > 0 else None,
                    baseline_mode="always_put",   # PUT_ONLY=True equivalent
                )
                combo_trades[(vix_max, width, sl, mc)].extend(trades)

    # ── Build results table ──
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

    # ── Post-sweep coverage summary ──
    logger.info("\n" + "=" * 70)
    logger.info("  DATA COVERAGE SUMMARY (days actually processed by sweep)")
    total_processed = sum(days_processed.values())
    total_skipped   = sum(days_skipped.values())
    for yr in years_in_range:
        p = days_processed.get(yr, 0)
        s = days_skipped.get(yr, 0)
        pct = round(p / (p + s) * 100, 1) if (p + s) > 0 else 0
        status = "✓" if pct >= 95 else "⚠ PARTIAL — results may be skewed!"
        logger.info(f"  {yr}: {p:>3} processed, {s:>3} skipped ({pct:.1f}% coverage)  {status}")
    logger.info(f"  TOTAL: {total_processed} days processed, {total_skipped} skipped")
    if total_skipped > 0:
        logger.warning(f"  ⚠ {total_skipped} days had missing data and were excluded from ALL combos.")
        logger.warning("    Results reflect only the periods with available data — interpret with caution!")
    logger.info("=" * 70)

    if not rows:
        logger.warning("Combo sweep produced no trades.")
        return

    # Save full results CSV — prepend coverage metadata as comment rows
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
    logger.info(f"\nCombo sweep complete — {len(rows)} combos saved to {COMBO_SWEEP_FILE}")

    # ── Analysis ──
    sep  = "─" * 100
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

    # 3. Parameter sensitivity — average P&L per value of each parameter
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
    logger.info(f"\n  Full results saved → {COMBO_SWEEP_FILE}")


# ─────────────────────────────────────────────
#  MAIN RUNNER
# ─────────────────────────────────────────────
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


async def run():
    date_list = pd.date_range(PILOT_YEAR_START, PILOT_YEAR_END, freq='B')

    # ── Resume or new run? ──
    resume_from = None
    if os.path.exists(SAVE_FILE):
        print(f"\nFound existing log: {SAVE_FILE}")
        if sys.stdin.isatty():
            choice = input("Resume where you left off? [y/n]: ").strip().lower()
        else:
            choice = "n"  # non-interactive (background/sweep) — always start fresh
            print("Non-interactive mode — starting fresh.")
        if choice == "y":
            existing_trades, resume_from = load_existing_trades()
            print(f"Resuming — {len(existing_trades)} existing trades loaded"
                  + (f", skipping up to {resume_from}." if resume_from else "."))
        else:
            existing_trades = []
            init_log_file()
            print("Starting fresh — existing log overwritten.")
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
    logger.info(f"Trades  → {SAVE_FILE}")
    logger.info(f"Log     → {LOG_FILE}")
    logger.info("=" * 60)

    async with _get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in MARKET_HOLIDAYS:
                logger.info(f"Skipping {d_str} — market holiday")
                continue
            if d_str in _CALENDAR_SKIP_DATES:
                logger.info(f"Skipping {d_str} — calendar filter ({', '.join(sorted(CALENDAR_FILTER_EVENTS))})")
                continue
            if resume_from and d_str <= resume_from:
                logger.info(f"Skipping {d_str} — already processed (resume)")
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

    logger.info(f"DONE — {len(all_trades)} trades logged to {SAVE_FILE}")
    print_performance_report(all_trades, date_list)
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
    _parser.add_argument("--min-otm-distance", default=None, type=float, help="Override MIN_OTM_DISTANCE (pts)")
    _parser.add_argument("--max-credit",       default=None, type=float, help="Override MAX_NET_CREDIT cap")
    _args = _parser.parse_args()
    if _args.start:           PILOT_YEAR_START    = _args.start
    if _args.end:             PILOT_YEAR_END      = _args.end
    if _args.out:             SAVE_FILE           = _args.out
    if _args.sl_vix_mid_low is not None and _args.sl_vix_mid_high is not None:
        DYNAMIC_SL_VIX_MID = (_args.sl_vix_mid_low, _args.sl_vix_mid_high)
    if _args.min_otm_distance is not None:
        MIN_OTM_DISTANCE = _args.min_otm_distance
    if _args.max_credit is not None:
        MAX_NET_CREDIT = _args.max_credit

    # Build calendar skip set (PCE + End-of-Month hard skip)
    _CALENDAR_SKIP_DATES: set[str] = set()
    if ENABLE_CALENDAR_FILTER and CALENDAR_FILTER_EVENTS:
        _cal_event_sets = _build_calendar_event_dates()
        for _ev in CALENDAR_FILTER_EVENTS:
            _CALENDAR_SKIP_DATES |= _cal_event_sets.get(_ev, set())
        logger.info(
            f"Calendar filter active: {sorted(CALENDAR_FILTER_EVENTS)} — "
            f"{len(_CALENDAR_SKIP_DATES)} dates will be hard-skipped"
        )

    # Build daily indicators if vix_change direction mode or any day filter is active
    _any_filter_active = any([
        DIRECTION_MODE == "vix_change",
        DAY_FILTER_VARPC_MIN, DAY_FILTER_RSI_MIN, DAY_FILTER_RSI_MAX,
        DAY_FILTER_DIST_SMA_MIN, DAY_FILTER_ATR_MAX, DAY_FILTER_BODY_MAX,
        DAY_FILTER_KNIFE_MAX, DAY_FILTER_STOCH_MIN, DAY_FILTER_GAP_MAX,
        DAY_FILTER_ABOVE_SMA5, DAY_FILTER_ABOVE_SMA200,
        DAY_FILTER_SKIP_VIX_RISE_DECEL,
        RUN_DAY_FILTER_SWEEP,
    ])
    if _any_filter_active:
        _DAILY_INDICATORS.update(_build_daily_indicators())

    if RUN_MAX_BP_SWEEP:
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
