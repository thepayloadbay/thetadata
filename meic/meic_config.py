from __future__ import annotations

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

# ---------------------------------------------
#  MARKET CALENDAR
# ---------------------------------------------
MARKET_HOLIDAYS = {
    "20250101","20250120","20250217","20250418","20250526","20250704",
    "20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260703",
    "20260907","20261126","20261225",
}

FOMC_DATES = {
    "20220126","20220316","20220504","20220615","20220727","20220921","20221102","20221214",
    "20230201","20230322","20230503","20230614","20230726","20230920","20231101","20231213",
    "20240131","20240320","20240501","20240612","20240731","20240918","20241107","20241218",
    "20250129","20250319","20250507","20250618","20250730","20250917","20251029","20251210",
    "20260128","20260318","20260506","20260617","20260729","20260916","20261028","20261209",
}

TRIPLE_WITCHING_DATES = {
    "20220318","20220617","20220916","20221216",
    "20230317","20230616","20230915","20231215",
    "20240315","20240621","20240920","20241220",
    "20250321","20250620","20250919","20251219",
    "20260320","20260619","20260918","20261218",
}

EARLY_CLOSE_DAYS = {
    "20241129","20241224",
    "20251128","20251224",
    "20261127","20261224",
}

def market_close_time(date_str: str) -> time:
    if date_str in EARLY_CLOSE_DAYS:
        return time(12, 59)
    return time(15, 59)


# ---------------------------------------------
#  GLOBAL CONFIG
# ---------------------------------------------
import pathlib as _pathlib
PROJECT_ROOT     = str(_pathlib.Path(__file__).resolve().parent.parent)

PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"
USE_LOCAL_DATA   = True
DATA_DIR         = os.path.join(PROJECT_ROOT, "data")
QUOTE_DISK_CACHE = os.path.join(PROJECT_ROOT, "data", "quote_disk_cache.parquet")

from datetime import datetime as _dt
_RUN_TS = _dt.now().strftime("%Y%m%d_%H%M%S")

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def _out(name: str) -> str:
    base, ext = name.rsplit(".", 1)
    return os.path.join(LOGS_DIR, f"{base}_{_RUN_TS}.{ext}")

SAVE_FILE        = _out("meic_trades.csv")
LOG_FILE         = _out("meic.log")
RUN_HISTORY_FILE = os.path.join(LOGS_DIR, "run_history.json")
RESULTS_FILE     = os.path.join(os.path.dirname(__file__), "RESULTS.md")

# +---------------------------------------------------------------------+
# |  STRATEGY: MEIC — Afternoon SPX Credit Spreads (Both Sides)         |
# |  Signal   : EMA crossover OR both_sides (sweep to determine)        |
# |  Execution: SPXW 0DTE credit spreads, afternoon entries             |
# +---------------------------------------------------------------------+

# -- Spread Parameters --
WIDTH             = 50.0     # 50-point wide spreads
QTY               = 2        # contracts per entry
MIN_NET_CREDIT    = 1.20     # minimum $1.20 credit ($120/contract)
MAX_NET_CREDIT    = None     # no cap
MIN_OTM_DISTANCE  = 30.0     # 30pts minimum OTM
MAX_OTM_DISTANCE  = None

# Dynamic width: use wider spread when VIX is elevated (more premium = more buffer)
# CONFIRMED R4: Dynamic width 60 @VIX>25 → P&L $101k, Sharpe 3.72
ENABLE_DYNAMIC_WIDTH     = True
DYNAMIC_WIDTH_VIX_THRESHOLD = 25.0
DYNAMIC_WIDTH_HIGH_VIX   = 60.0

# -- Direction --
# SWEEP RESULT: always_put $97k Sharpe 2.38 >> ema -$1k >> both_sides -$13k >> always_call -$79k
# Afternoon naturally favors PUT spreads (market settles/rallies into close)
DIRECTION_MODE    = "always_put"

# -- Entry Timing (afternoon) --
# SWEEP RESULT: 14:00-15:30 @5min = $97k Sharpe 2.38. Broad plateau 13:30-14:00 start.
ENTRY_START       = time(14, 0)
ENTRY_END         = time(15, 30)
ENTRY_INTERVAL    = 5
MAX_TRADES_DAY    = 20

# -- EMA Parameters --
EMA_FAST          = 14
EMA_SLOW          = 20

# -- Commission --
COMMISSION        = 0.50  # per contract per leg (TradeStation)

# -- Benchmark / risk-free rate --
ANNUAL_RISK_FREE_RATE = 0.05
ACCOUNT_SIZE          = 50_000.0

# -- Per-Position Stop Loss --
# SWEEP RESULT: -$400 in optimized stack (Sharpe 2.96). Plateau -$300 to -$600.
ENABLE_PER_POS_SL     = True
PER_POS_SL_AMOUNT     = -400.0
ENABLE_ASYMMETRIC_SL  = False  # PUT-only strategy, no asymmetry needed
ASYM_PUT_SL_AMOUNT    = -400.0
ASYM_CALL_SL_AMOUNT   = -600.0

# -- Afternoon SL Tightening --
# Since all entries are afternoon, this acts as a late-session tighter SL
# CONFIRMED: Late SL -$300 @15:15 → Sharpe 2.96, DD -$14.7k (from -$16.5k)
ENABLE_AFTERNOON_SL_TIGHTEN = True
AFTERNOON_SL_CUTOFF  = time(15, 15)
AFTERNOON_SL_AMOUNT  = -300.0

# -- Daily Stop Loss --
DAILY_SL             = -3000.0    # will sweep
DAILY_TP             = None

# -- EOM Stop Loss --
ENABLE_EOM_SL        = True
EOM_SL_AMOUNT        = -300.0
EOM_SL_AMOUNT_DANGER = -300.0

# -- Open MTM Gate --
# CONFIRMED R2: MTM gate -$300 → DD -$14.7k→-$11.8k, Sharpe 3.77
ENABLE_OPEN_MTM_GATE      = True
OPEN_MTM_GATE_THRESHOLD   = -300.0

# -- VIX Filters --
VIX_MIN_FILTER    = None
VIX_MAX_FILTER    = 35.0

# -- Economic Calendar Filter --
ENABLE_ECON_FILTER = False
ECON_DATES = {
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
    "20250107","20250207","20250307","20250404","20250502","20250606",
    "20250711","20250801","20250905","20251003","20251107","20251205",
    "20260109","20260206","20260306","20260403","20260508","20260605",
    "20260710","20260807","20260904","20261002","20261106","20261204",
}

# -- Calendar Event Skips --
ENABLE_ECON_VIX_SKIP = False
ECON_VIX_SKIP_RULES  = []
ENABLE_CPI_SKIP      = False
ENABLE_FOMC_SKIP     = True   # CONFIRMED: FOMC skip cuts DD -$20k→-$16.5k, Sharpe 2.38→2.80
ENABLE_PCE_SKIP      = False
ENABLE_EOM_SKIP      = False
ENABLE_TW_SKIP       = False
ENABLE_EOQ_SKIP      = False
ENABLE_PRE_TW_SKIP   = False
ENABLE_POST_HOL_SKIP = False
ENABLE_CALENDAR_FILTER = False

# -- Dynamic SL (disabled for baseline; will sweep) --
ENABLE_DYNAMIC_SL    = False
DYNAMIC_SL_AMOUNT    = -800.0
DYNAMIC_SL_VIX_LOW   = 13.0
DYNAMIC_SL_VIX_HIGH  = (25.0, 30.0)
DYNAMIC_SL_VIX_MID   = (13.0, 13.5)

# -- Calendar event SLs (all disabled for baseline) --
ENABLE_CPI_SL    = False; CPI_SL_AMOUNT    = -500.0
ENABLE_FOMC_SL   = False; FOMC_SL_AMOUNT   = -500.0; FOMC_SL_AMOUNT_DANGER = -500.0
ENABLE_PCE_SL    = False; PCE_SL_AMOUNT    = -500.0; PCE_SL_AMOUNT_DANGER  = -500.0
ENABLE_EOQ_SL    = False; EOQ_SL_AMOUNT    = -500.0
ENABLE_PRE_TW_SL = False; PRE_TW_SL_AMOUNT = -500.0; PRE_TW_SL_AMOUNT_DANGER = -500.0
ENABLE_POST_HOL_SL = False; POST_HOL_SL_AMOUNT = -500.0
ENABLE_FOMC_VIX1520_SL = False; FOMC_VIX1520_SL_AMOUNT = -500.0; FOMC_VIX1520_RANGE = (15.0, 20.0)

# -- Disabled features (carried from MEDS, all off) --
# Momentum filter: skip PUT entry when SPX dropped fast (catching falling knife)
ENABLE_MOMENTUM_FILTER   = False
MOMENTUM_FILTER_BARS     = 5      # lookback bars (minutes)
MOMENTUM_FILTER_PTS      = -10.0  # skip if SPX dropped more than this in lookback

# ---- CREATIVE IMPROVEMENTS (R6-R10) ----

# R6: EMA direction gate for VIX 20-25 zone
# Only enter when EMA is bullish (fast > slow) in the weak VIX 20-25 zone.
# VIX 20-25 has 65% WR — worst profitable zone. EMA bullish = market trending up = safe for puts.
# CONFIRMED R6: Sharpe 3.72→4.00. Filters VIX 20-25 to EMA bullish only.
ENABLE_EMA_VIX_GATE      = True
EMA_VIX_GATE_LO          = 20.0   # gate active when VIX >= this
EMA_VIX_GATE_HI          = 25.0   # gate active when VIX < this

# R7: Time-decay qty reduction
# Reduce qty for late entries (14:45+) where WR drops from 71% to 62%.
ENABLE_LATE_QTY_REDUCTION = False
LATE_QTY_CUTOFF           = time(14, 45)  # reduce qty after this time
LATE_QTY                  = 1             # qty for late entries

# R8: Economic event day upsizing
# CPI/NFP days have 86%/83% WR. By afternoon, volatility is front-loaded.
ENABLE_ECON_UPSIZING      = False
ECON_UPSIZE_QTY           = 4             # qty on CPI/NFP days
ECON_UPSIZE_EVENTS        = {"cpi", "nfp"}  # which events trigger upsizing

# R9: Adaptive OTM by entry time
# Scale MIN_OTM distance by entry time (gamma risk increases near expiry).
# CONFIRMED R9: DD -$10.3k→-$9.2k. Wider OTM for late entries.
ENABLE_ADAPTIVE_OTM_TIME  = True
OTM_TIME_RULES = [  # (start_time, min_otm)
    (time(14, 0),  30),   # 14:00-14:59: 30pt (baseline)
    (time(15, 0),  40),   # 15:00-15:14: 40pt
    (time(15, 15), 50),   # 15:15+: 50pt
]

# R10: Winning streak acceleration (REJECTED: trades settle EOD, no intraday streak)
ENABLE_STREAK_ACCEL       = False
STREAK_ACCEL_COUNT        = 2
STREAK_ACCEL_QTY_BONUS    = 1

# R10 ALT: Late credit floor
# Require higher credit for late entries (more compensation for gamma risk).
ENABLE_LATE_CREDIT_FLOOR  = False
LATE_CREDIT_CUTOFF        = time(15, 0)   # raise credit floor after this time
LATE_CREDIT_MIN           = 1.80          # higher min credit for late entries

# -- ORB Containment Filter --
# Skip entry if SPX has broken out of the 9:30-10:00 opening range by entry time.
# ORB = max(high) / min(low) of 1-min bars from 09:30-10:00. No look-ahead bias.
ENABLE_ORB_FILTER        = False

# -- Prior-Day Candle Direction Filter --
# Only enter PUT credit spreads when prior day was bullish (close > open).
# Bearish prior days → skip. Uses T-1 daily OHLC, no look-ahead bias.
ENABLE_PRIOR_DAY_DIRECTION_FILTER = False

ENABLE_SPEED_FILTER      = False
ENABLE_WONGING           = False
ENABLE_PRESSURE_FILTER   = False
ENABLE_VIX_REGIME        = False
ENABLE_LOW_VIX_HALF_SIZE = False
# Kelly zone sizing: scale qty by VIX zone
# VIX 15-20 has 80% WR → qty 3. VIX 20-25 has 65% WR → qty 1. VIX 25-30 has 72% → qty 2.
KELLY_ZONE_QTY_MEIC = [   # (vix_lo, vix_hi, qty)
    (0.0,  15.0, 2),      # VIX <15: rare zone, keep baseline
    (15.0, 20.0, 3),      # VIX 15-20: 80% WR sweet spot → upsize
    (20.0, 25.0, 2),      # VIX 20-25: 65% WR → baseline
    (25.0, 30.0, 2),      # VIX 25-30: 72% WR → baseline
    (30.0, 999.0, 1),     # VIX 30+: already skipped, safety
]
# CONFIRMED R3: Kelly A (upsize 15-20 only): $99k PnL, DD -$10.3k, Sharpe 3.68
ENABLE_KELLY_SIZING      = True
ENABLE_TIME_DECAY_SIZING = False
ENABLE_IVR_FILTER        = False
ENABLE_DELTA_LIMIT       = False
ENABLE_EARLY_PROFIT_TAKE = False
ENABLE_MAX_SL_GATE       = False
ENABLE_DAY_NEG_SL        = False
ENABLE_MAX_DIR_ENTRIES   = False
ENABLE_MOMENTUM_CONFIRM  = False
ENABLE_STRIKE_DIVERSITY  = False
ENABLE_CASCADE_CLOSE     = False
ENABLE_MAX_CONCURRENT_DIR = False
ENABLE_REVERSAL_DAMPENER  = False
ENABLE_REALIZED_LOSS_GATE = False
ENABLE_FLIP_COOLDOWN      = False
ENABLE_HARD_POS_LOSS_CAP  = False
ENABLE_MAX_TOTAL_POSITIONS = False
ENABLE_POST_SL_PAUSE       = False
ENABLE_WHIPSAW_DETECTOR    = False
ENABLE_RANGE_GATE          = False
ENABLE_STRIKE_COOLDOWN     = False
ENABLE_CONSEC_LOSS_PAUSE   = False
ENABLE_EARLY_LOSS_GATE     = False
ENABLE_AFTERNOON_POS_TRAIL = False
ENABLE_POSITION_AGE_CAP    = False
ENABLE_ADAPTIVE_AFTN_CUTOFF = False
ENABLE_INTRADAY_ATR_GATE   = False
ENABLE_EARLY_SL_DIR_LOCK   = False
ENABLE_PROFIT_MGMT         = False
ENABLE_VIX_HIGH_SYMMETRIC_SL = False
ENABLE_MONTH_DIR_SL        = False
ENABLE_GAP_CALL_SL         = False
ENABLE_BACKWARDATION_SL    = False
ENABLE_VIX_MID_SAFE_SL     = False
ENABLE_VIX_SUB12_SL        = False
ENABLE_TIME_BASED_SL       = False
ENABLE_EMA_CONFIDENCE_SIZING = False
ENABLE_HARD_TIME_EXIT      = False
HARD_TIME_EXIT_TIME        = None
ENABLE_SL_GAP_REENTRY     = False
SL_GAP_MINUTES            = 30
# L1: Thursday skip — Thursday has 55.5% WR, -$8.4k total (only losing DOW)
ENABLE_THURSDAY_SKIP       = False

# L2: First-entry canary — half-size first entry, if losing at next MTM, stop day
ENABLE_CANARY_ENTRY        = False
CANARY_QTY                 = 1      # qty for canary (first) entry
CANARY_CHECK_BARS          = 2      # check P&L after this many MTM bars (2 = ~10min)

# L3: Daily loss cap (tighter) — cap cumulative day loss
DAILY_LOSS_CAP             = -2000.0  # fires when realized+unrealized <= this

# L4: Morning range gate — skip day if morning SPX range is too large
ENABLE_MORNING_RANGE_GATE  = False
MORNING_RANGE_MAX_PCT      = 1.5   # skip if morning (9:30-14:00) range > this % of spot

# L5: Post-loss day cooldown — skip next day after large loss day
ENABLE_POST_LOSS_COOLDOWN  = False
POST_LOSS_THRESHOLD        = -1500.0  # trigger cooldown if day P&L <= this
ENABLE_ML_VETO             = False

# Misc settings not swept but needed by engine
POS_TRAIL_ACTIVATION = None
POS_TRAIL_PULLBACK   = 5.0
NET_DELTA_LIMIT      = 0.30
INTRADAY_ENTRY_GATE  = None
PREMIUM_BUYBACK_EXIT = None
TOUCH_EXIT_DOLLARS   = None
TOUCH_EXIT_PCT       = None
# VIX zone skip: CONFIRMED R1: skip VIX 30-35 → Sharpe 2.96→3.75
SKIP_VIX_RANGE       = (30.0, 35.0)
COMPUTE_OPENING_SKEW = False
OPENING_SKEW_OTM     = 50
PNL_SAMPLE_INTERVAL  = 1     # 1-min MTM for afternoon (less time to recover)
DANGER_PNL_SAMPLE_INTERVAL = 1
KELLY_ZONE_QTY       = []
LOW_VIX_THRESHOLD    = 13.0
MID_VIX_BAND         = (25.0, 30.0)
HIGH_VIX_THRESHOLD   = 30.0
HIGH_VIX_QTY         = 1
LOW_VIX_QTY          = 1
MIN_VIX_LEVEL        = 16.0
SPEED_LOOKBACK       = 5
SPEED_THRESHOLD      = 15.0
WONG_LOOKBACK        = 5
WONG_THRESHOLD       = 0.05
PRESSURE_DISTANCE_THRESHOLD = 45.0
PRESSURE_FILTER_VIX_MIN = 15.0
PRESSURE_FILTER_VIX_MAX = 20.0
MAX_TRADES_DAY_VIX_LO_HI = None
ENABLE_OTM_DISTANCE_VIX_RANGE = False
MIN_OTM_DISTANCE_VIX_RANGE_LO = 15.0
MIN_OTM_DISTANCE_VIX_RANGE_HI = 20.0
MIN_OTM_DISTANCE_VIX_LO_HI    = 40.0
TIME_DECAY_CUTOFF    = time(12, 0)
TIME_DECAY_QTY       = 1
DAY_NEG_THRESHOLD    = -500.0
DAY_NEG_SL_AMOUNT    = -150.0
MAX_DIR_ENTRIES      = 5
MOMENTUM_CONFIRM_BARS = 10
MOMENTUM_CONFIRM_THRESHOLD = 0.0
STRIKE_DIVERSITY_MIN_DIFF = 5
MAX_CONCURRENT_DIR   = 3
REVERSAL_DAMPENER_PTS = 10
REALIZED_LOSS_GATE_AMOUNT = -1200.0
FLIP_COOLDOWN_BARS   = 1
HARD_POS_LOSS_CAP    = -800.0
MAX_TOTAL_POSITIONS  = 6
POST_SL_PAUSE_MINUTES = 30
STRIKE_COOLDOWN_MINUTES = 30
STRIKE_COOLDOWN_RANGE = 10
CONSEC_LOSS_THRESHOLD = 2
CONSEC_LOSS_PAUSE_MINUTES = 30
EARLY_PROFIT_TAKE_PCT = 0.50
MAX_SL_COUNT         = 2
AFTERNOON_TRAIL_CUTOFF = time(13, 0)
AFTERNOON_TRAIL_AMOUNT = 200.0
POSITION_AGE_CAP_MINUTES = 120
ADAPTIVE_AFTN_VIX_THRESHOLD = 20.0
ADAPTIVE_AFTN_EARLY_CUTOFF = time(12, 0)
ATR_GATE_BARS        = 5
ATR_GATE_MULTIPLIER  = 1.5
EARLY_SL_LOCK_MINUTES = 30
EARLY_LOSS_GATE_COUNT = 3
RANGE_GATE_FRACTION  = 0.5
PROFIT_ACTIVATION_TRIGGER = 500.0
PROFIT_TRAIL_AMOUNT  = 200.0
PROFIT_HARD_SL       = -500.0
VIX_HIGH_SL_THRESHOLD = 25.0
MONTH_DIR_SL_AMOUNT  = -500.0
MONTH_DIR_SL_RULES   = {}
GAP_CALL_SL_AMOUNT   = -500.0
BACKWARDATION_SPREAD_THRESHOLD = 0.0
BACKWARDATION_SL_AMOUNT = -500.0
VIX_MID_SAFE_SL_RANGE = (13.0, 17.0)
VIX_MID_SAFE_SL_AMOUNT = -500.0
VIX_SUB12_THRESHOLD  = 12.0
VIX_SUB12_SL_AMOUNT  = -500.0
TIME_SL_CUTOFF       = time(11, 0)
TIME_SL_EARLY_AMOUNT = -200.0
TIME_SL_LATE_AMOUNT  = -150.0
EMA_CONFIDENCE_THRESHOLD = 1.5
EMA_LOW_CONFIDENCE_QTY = 1
EMA_HIGH_CONFIDENCE_THRESHOLD = 1.5
EMA_HIGH_CONFIDENCE_QTY = 3
ML_PREDICTIONS_FILE  = ""


# -- Calendar Event Date Builder --
def _build_calendar_event_dates() -> "dict[str, set[str]]":
    all_bdays: set[str] = {
        d.strftime("%Y%m%d")
        for d in pd.bdate_range("2022-01-01", "2026-12-31")
        if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS
    }

    def _last_bday(year: int, month: int) -> "str | None":
        days_in = pd.Timestamp(year, month, 1).days_in_month
        for day in range(days_in, 0, -1):
            d_str = f"{year}{month:02d}{day:02d}"
            if d_str in all_bdays:
                return d_str
        return None

    eom: set[str] = set()
    eoq: set[str] = set()
    for year in range(2022, 2027):
        for month in range(1, 13):
            d = _last_bday(year, month)
            if d:
                eom.add(d)
                if month in {3, 6, 9, 12}:
                    eoq.add(d)

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
    fomc_dates = set(FOMC_DATES)

    pre_tw: set[str] = set()
    _all_bdays_sorted = sorted(all_bdays)
    for tw in TRIPLE_WITCHING_DATES:
        for i, d in enumerate(_all_bdays_sorted):
            if d >= tw:
                if i > 0:
                    pre_tw.add(_all_bdays_sorted[i - 1])
                break

    post_holiday: set[str] = set()
    for hol in MARKET_HOLIDAYS:
        for d in _all_bdays_sorted:
            if d > hol:
                post_holiday.add(d)
                break

    return {
        "fomc":                fomc_dates,
        "triple_witching":     TRIPLE_WITCHING_DATES,
        "cpi":                 cpi_dates & all_bdays,
        "nfp":                 nfp_dates & all_bdays,
        "pce":                 pce_dates & all_bdays,
        "end_of_month":        eom,
        "end_of_quarter":      eoq,
        "pre_triple_witching": pre_tw,
        "post_major_holiday":  post_holiday,
    }


# -- Day Indicator Filters (all disabled for baseline) --
DAY_FILTER_VIX_MAX      = None
DAY_FILTER_VIX_MIN      = None
DAY_FILTER_VIX_CHG_MAX  = None
DAY_FILTER_VIX_CHG_MIN  = None
DAY_FILTER_VARPC_MIN    = None
DAY_FILTER_RSI_MIN      = None
DAY_FILTER_RSI_MAX      = None
DAY_FILTER_DIST_SMA_MIN = None
DAY_FILTER_ATR_MAX      = None
DAY_FILTER_BODY_MAX     = None
DAY_FILTER_KNIFE_MAX    = None
DAY_FILTER_STOCH_MIN    = None
DAY_FILTER_GAP_MAX      = None
DAY_FILTER_ABOVE_SMA5   = None
DAY_FILTER_ABOVE_SMA200 = None
DAY_FILTER_ADX_MIN      = None
DAY_FILTER_RANGE_MAX    = None
DAY_FILTER_RANGE_MIN    = None
DAY_FILTER_EXP_MOVE_MAX = None
DAY_FILTER_EXP_MOVE_MIN = None
DAY_FILTER_CCI_MAX      = None
DAY_FILTER_IVR_MIN      = None
DAY_FILTER_SKIP_VIX_RISE_DECEL = False
RUN_DAY_FILTER_SWEEP    = False

# -- Sweep Flags (all off for baseline) --
RUN_SL_SWEEP             = False
RUN_ENTRY_TIME_SWEEP     = False
RUN_PER_POS_SL_SWEEP     = False
RUN_SPREAD_WIDTH_SWEEP   = False
RUN_MIN_CREDIT_SWEEP     = False
RUN_CALENDAR_SWEEP       = False
RUN_EMA_SWEEP            = False
RUN_BASELINE_COMPARISON  = False

# -- Analysis flags --
RUN_STRIKE_DISTANCE_ANALYSIS = True
RUN_PUT_CALL_ANALYSIS        = True

# -- Output files for analyses --
VIX_ANALYSIS_FILE              = _out("meic_vix_analysis.csv")
STRIKE_DISTANCE_ANALYSIS_FILE  = _out("meic_strike_distance_analysis.csv")
PUT_CALL_SPLIT_FILE            = _out("meic_put_call_split.csv")

# -- CSV trade log columns --
LOG_COLS = [
    "entry_date", "entry_time", "option_type",
    "short_strike", "long_strike", "width", "credit_received",
    "entry_short_bid", "entry_short_ask", "entry_short_mid",
    "entry_long_bid", "entry_long_ask", "entry_long_mid",
    "qty", "vix_level", "strike_distance",
    "opening_put_credit", "opening_call_credit", "pc_skew_ratio",
    "ema13", "ema48",
    "outcome", "pnl_earned", "peak_pnl",
    "close_date", "close_time",
    "win", "loss",
]
