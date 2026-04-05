"""LastFive — Configuration.

All strategy parameters, market calendar, and file paths.

Strategy: Sell short call + put credit spreads on SPX 5 minutes before market close (15:55).
VIX-adaptive strike: ATM when VIX<18, $3 OTM when VIX 18-22, $5 OTM when VIX 22-26.
Tiered sizing: scale risk based on credit level.
Put filter: skip put when afternoon<-0.75% AND 30-min momentum<-0.10%.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, time

import pandas as pd

# ---------------------------------------------
#  MARKET CALENDAR
# ---------------------------------------------
MARKET_HOLIDAYS = {
    # 2022
    "20220117", "20220221", "20220415", "20220530", "20220620", "20220704",
    "20220905", "20221124", "20221226",
    # 2023
    "20230102", "20230116", "20230220", "20230407", "20230529", "20230619",
    "20230704", "20230904", "20231123", "20231225",
    # 2024
    "20240101", "20240115", "20240219", "20240329", "20240527", "20240619",
    "20240704", "20240902", "20241128", "20241225",
    # 2025
    "20250101", "20250120", "20250217", "20250418", "20250526", "20250704",
    "20250901", "20251127", "20251225",
    # 2026
    "20260101", "20260119", "20260216", "20260403", "20260525", "20260703",
    "20260907", "20261126", "20261225",
}

EARLY_CLOSE_DAYS = {
    "20221125", "20231124", "20241129", "20241224",
    "20251128", "20251224", "20261127", "20261224",
}

FOMC_DATES = {
    "20220126", "20220316", "20220504", "20220615", "20220727", "20220921", "20221102", "20221214",
    "20230201", "20230322", "20230503", "20230614", "20230726", "20230920", "20231101", "20231213",
    "20240131", "20240320", "20240501", "20240612", "20240731", "20240918", "20241107", "20241218",
    "20250129", "20250319", "20250507", "20250618", "20250730", "20250917", "20251029", "20251210",
    "20260128", "20260318", "20260506", "20260617", "20260729", "20260916", "20261028", "20261209",
}

TRIPLE_WITCHING_DATES = {
    "20220318", "20220617", "20220916", "20221216",
    "20230317", "20230616", "20230915", "20231215",
    "20240315", "20240621", "20240920", "20241220",
    "20250321", "20250620", "20250919", "20251219",
    "20260320", "20260619", "20260918", "20261218",
}

def market_close_time(date_str: str) -> time:
    if date_str in EARLY_CLOSE_DAYS:
        return time(12, 59)
    return time(15, 59)

def entry_time(date_str: str) -> str:
    """Return the entry bar time string (8 min before close).

    15:52 entry is the single biggest improvement found:
    +$142k P&L (+69%), DD -45%, Sharpe 4.20→6.71, Calmar 41→127.
    More time for theta decay + more time for touch to detect and exit threats.
    Confirmed by OA backtester ($98,779) as optimal entry time.
    """
    if date_str in EARLY_CLOSE_DAYS:
        return "12:52:00"
    return "15:52:00"


# ---------------------------------------------
#  GLOBAL CONFIG
# ---------------------------------------------
PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR     = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END   = "2026-03-25"

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")

def _out(name: str) -> str:
    base, ext = name.rsplit(".", 1)
    return os.path.join(LOGS_DIR, f"{base}_{_RUN_TS}.{ext}")

SAVE_FILE        = _out("lastfive_trades.csv")
LOG_FILE         = _out("lastfive.log")
RUN_HISTORY_FILE = os.path.join(LOGS_DIR, "run_history.json")

# +----------------------------------------------------------------------+
# |  STRATEGY: LastFive — Short Call + Put Spread 8 Min Before Close      |
# |  Entry    : 15:52 (or 12:52 on early close days)                     |
# |  Exit     : 16:00 close (0DTE expiration)                            |
# |  Strike   : VIX-adaptive (ATM when calm, wider when volatile)        |
# |  Sizing   : Tiered by credit level                                   |
# |  Filter   : VIX(15:50)<=26, afternoon<1.0%, put momentum filter      |
# |  Touch    : Call $0 / Put -$1 continuous (bar CLOSE, OA-style)       |
# +----------------------------------------------------------------------+
# |  CONFIRMED BASELINE  (2022-01-03 -> 2026-03-25)  run 2026-04-04     |
# |  Total P&L    : $330,209   Win rate : 75.5%   PF : 3.10             |
# |  Max drawdown : -$1,819    Sharpe   : 10.64   Calmar : 16.96        |
# |  Trades       : ~1,660 (call + put)                                 |
# |  Touch exits  : bar CLOSE (OA-matched), Call $0 / Put -$1           |
# |  Entry strike : bar OPEN (99.8% match with OA)                      |
# +----------------------------------------------------------------------+

# === BIDIRECTIONAL MODE ===
ENABLE_CALL_SIDE   = True   # Sell call credit spreads above spot
ENABLE_PUT_SIDE    = True   # Sell put credit spreads below spot

# === VIX-ADAPTIVE STRIKE DISTANCE (Rule C3) ===
# When VIX is low, SPX barely moves in last 5 min — sell closer for more credit.
# When VIX is high, need more distance for safety.
# Plateau-validated: VIX<17 to VIX<19 all within 5% of P&L. Walk-forward confirmed.
ENABLE_VIX_ADAPTIVE = True
VIX_ADAPTIVE_MODE  = "buckets"  # "buckets" = discrete Rule C3, "vix16" = continuous VIX/16 formula
VIX_ATM_CUTOFF     = 18.0   # VIX(15:50) < this → dist=0 (ATM, nearest 5-pt strike)
VIX_MID_CUTOFF     = 22.0   # VIX(15:50) 18-22 → dist=3
VIX_WIDE_CUTOFF    = 26.0   # VIX(15:50) 22-26 → dist=5; > 26 → skip day
DIST_ATM           = 0.0    # Distance when VIX < ATM_CUTOFF
DIST_MID           = 3.0    # Distance when VIX in [ATM_CUTOFF, MID_CUTOFF)
DIST_WIDE          = 5.0    # Distance when VIX in [MID_CUTOFF, WIDE_CUTOFF]

# VIX/16 continuous mode params (only used when VIX_ADAPTIVE_MODE = "vix16")
VIX16_MULTIPLIER   = 1.5    # dist = multiplier * expected_5min_move (rounded up to STRIKE_STEP)
VIX16_MIN_DIST     = 5.0    # Minimum distance floor (never go ATM in vix16 mode)

# === RANGE BUDGET ADJUSTMENT ===
# When <50% of VIX-implied daily range is consumed by 15:50, the day is quiet.
# Tighten distance by $2 to collect more credit on these safe days.
# Result: +$11.5k P&L, same DD, same Sharpe. Adds ~16 trades on quiet days.
# Range consumed = actual_high_low / (SPX × VIX/100 / √252 × 1.6)
ENABLE_RANGE_BUDGET_TIGHTEN = True
RANGE_BUDGET_QUIET_THRESHOLD = 0.50  # Consumed < this = quiet day
RANGE_BUDGET_TIGHTEN_AMOUNT = 2.0    # Reduce distance by this much on quiet days

# Fallback fixed distance (used when VIX-adaptive is disabled)
MIN_SHORT_DISTANCE = 3.0

# === SPREAD PARAMETERS ===
SPREAD_WIDTH       = 5.0    # Long leg is $5 beyond the short leg
MIN_NET_CREDIT     = 0.10   # Skip side if credit < this

# === TIERED CREDIT SIZING ===
# Higher credit = more cushion before loss = can risk more safely.
# Walk-forward validated: OOS advantage persists and grows.
ENABLE_TIERED_SIZING = True
TIER_LOW_CREDIT      = 0.20  # Credit < this → low risk
TIER_HIGH_CREDIT     = 0.50  # Credit > this → high risk
RISK_LOW             = 1500.0  # Risk when credit < TIER_LOW_CREDIT
RISK_MID             = 2500.0  # Risk when credit in [TIER_LOW, TIER_HIGH]
RISK_HIGH            = 3500.0  # Risk when credit > TIER_HIGH_CREDIT
# Fallback fixed risk (used when tiered sizing is disabled)
RISK_AMOUNT          = 2500.0

# === MAX CONTRACT CAP ===
# Cap max contracts per side to mechanically limit worst-case loss.
# Oversized ATM trades (8-12 contracts) add more variance than return.
# Max 7: Sharpe 5.12→5.52, DD -21%, worst day -$3,444→-$2,919.
MAX_QTY_PER_SIDE     = 99   # Effectively uncapped — Run #6 config ($288k)

# === STACKED PUT FILTER ===
# Skip PUT side when BOTH conditions are true (afternoon selling + momentum into close).
# Cuts max DD by 33% while adding P&L. Only drops ~19 put trades.
ENABLE_PUT_MOMENTUM_FILTER = True
PUT_AFTERNOON_MIN          = -0.75  # Skip put if afternoon return < this % AND...
PUT_MOM30_MIN              = -0.10  # ...30-min momentum (15:25-15:54) < this %

# === STRIKE SELECTION ===
STRIKE_STEP = 5  # SPX/SPXW strike increment

# === ENTRY / EXIT ===
ENTRY_MINUTES_BEFORE_CLOSE = 8  # 15:52 entry (was 5 = 15:55)
EXIT_TIME = "16:00:00"

# === CONTINUOUS TOUCH EXIT (OA-style) ===
# After entry, check each 1-min bar CLOSE from entry+1 through 15:59.
# If SPX close breaches the touch threshold relative to the short strike, buy back spread.
# Call Touch $0 = exit when bar close >= short call strike.
# Put Touch -$1 = exit when bar close <= short put strike - $1 (allow $1 ITM).
# Discovery: OA uses bar CLOSE for touch, not LOW/HIGH.
# LOW/HIGH catches intrabar wicks that reverse — causes 226 extra false exits (-$46k).
# With bar CLOSE: matches OA trade count and P&L within 10%.
ENABLE_TOUCH_EXIT          = True
CALL_TOUCH_VALUE           = 0.0    # Exit call when bar close >= short_strike - this value
PUT_TOUCH_VALUE            = -1.0   # Exit put when bar close <= short_strike + this value

# === LEGACY PROTECTIVE BUY-BACK (Disabled — replaced by continuous touch) ===
ENABLE_PROTECTIVE_BUYBACK  = False
STOP_CHECK_TIMES           = ["15:56:00", "15:57:00"]
STOP_THRESHOLD             = 2.0

# === CALL-SIDE AFTERNOON FILTER ===
ENABLE_AFTERNOON_FILTER    = True
AFTERNOON_RETURN_MAX       = 1.0    # Skip ALL trades if afternoon return >= this %

# === VIX INTRADAY FILTER ===
ENABLE_VIX_INTRADAY_FILTER = True
VIX_INTRADAY_MAX           = 26.0   # Skip if VIX(15:50) > this

# === VIX FILTERS (daily, prior-day close) ===
VIX_MAX_FILTER: float | None = None
VIX_MIN_FILTER: float | None = None

# === VIX TERM STRUCTURE FILTER (prior-day) ===
ENABLE_VIX_TERM_FILTER = False
VIX_TERM_MAX_RATIO     = 1.2    # Skip if VIX/VIX9D > this (backwardation)

# === VVIX FILTER (prior-day) ===
ENABLE_VVIX_FILTER = False
VVIX_MAX           = 120.0      # Skip if VVIX > this

# === PARKINSON RATIO ADAPTIVE DISTANCE ===
# When closing period (15:25-15:50) is hotter than full day, widen distance.
# Parkinson ratio = closing_vol / full_day_vol (both Parkinson estimator from OHLC).
# Ratio > 1.0 = close is hotter than the day average → more risk → widen distance.
# Result: DD -$2,739→-$2,058 (-25%), Sharpe 6.71→6.81, Calmar 131→158, -$36k P&L.
# First filter to improve DD without skipping any days.
ENABLE_PARKINSON_RATIO_WIDEN   = True
PARKINSON_RATIO_THRESHOLD      = 1.0    # Widen if ratio > this
PARKINSON_RATIO_WIDEN_AMOUNT   = 2.0    # Add this much distance when triggered

# === PARKINSON RATIO FILTER (skip day entirely — not adopted, costs too much P&L) ===
ENABLE_PARKINSON_RATIO_FILTER = False
PARKINSON_RATIO_MAX           = 1.2    # Skip if closing vol > 1.2x full-day vol (close is hotter)

# === KAUFMAN EFFICIENCY RATIO (trend/chop from 1-min bars at 15:54) ===
ENABLE_ER_FILTER = False
ER_LOOKBACK      = 10        # Number of 1-min bars (10 = 15:45-15:54)
ER_MAX           = 0.65      # Skip if ER > this (strong trend = dangerous)

# === PARKINSON VOLATILITY FILTER (realized micro-vol from OHLC, 15:25-15:54) ===
ENABLE_PARKINSON_FILTER = False
PARKINSON_MAX           = 25.0   # Skip if Parkinson annualized vol > this %

# === RANGE BUDGET FILTER (H2-V16-1: fraction of VIX/16 expected range consumed by 15:54) ===
ENABLE_RANGE_BUDGET_FILTER = False
RANGE_BUDGET_MIN           = 0.7    # Skip if range_budget < this (too much move budget remaining)

# === SPREAD COMPRESSION FILTER (VSA-inspired, no volume needed) ===
ENABLE_SPREAD_COMPRESSION_FILTER = False
SPREAD_COMPRESSION_SKIP_PCTILE   = 70   # Skip if pre-entry bar ranges are above this day-percentile

# === WILLIAMS VIX FIX (price-derived fear — no external data) ===
ENABLE_WVF_FILTER  = False
WVF_MODE           = "bb"       # "bb" = above Bollinger Band, "percentile" = rank
WVF_PERCENTILE_MAX = 85         # For percentile mode: skip if WVF rank > this

# === PREMIUM/DISCOUNT ZONE SIDE FILTER (H2-SMC-4) ===
# At entry, compute zone = (SPX - day_low) / (day_high - day_low) using bars up to 15:54.
# zone > 0.7 = premium zone = skip CALL side (overextended up, risk of reversion)
# zone < 0.3 = discount zone = skip PUT side (overextended down, risk of reversion)
# This is SIDE SELECTION, not day skipping.
ENABLE_ZONE_SIDE_FILTER     = False
ZONE_PREMIUM_THRESHOLD      = 0.7    # Skip calls when zone > this
ZONE_DISCOUNT_THRESHOLD     = 0.3    # Skip puts when zone < this

# === FRACTAL EFFICIENCY FILTER (H2-FVA-1) ===
# FE = log(abs(close[-1] - close[-n]) / sum_of_bar_ranges) / log(n)
# Computed on 1-min bars 15:20-15:50 (30 bars before entry).
# FE near 1 = trending (dangerous). FE near 0.5 = random walk. FE < 0.5 = mean-reverting.
# Skip day if FE > threshold (strongly trending into close).
ENABLE_FRACTAL_FILTER       = False
FRACTAL_MAX                 = 0.65   # Skip if FE > this

# === CALM STREAK VIX ADJUSTMENT (H2-VXC-2) ===
# Count consecutive days where VIX close < SMA(VIX, 5).
# When calm streak > threshold, tighten VIX/16 multiplier (market has been calm for a while).
# This is a DISTANCE MODIFIER, not a filter.
ENABLE_CALM_STREAK_ADJUST      = False
CALM_STREAK_THRESHOLD          = 10    # Tighten after this many consecutive calm days
CALM_STREAK_MULT_REDUCTION     = 0.2   # Reduce distance multiplier by 20%

# === CALENDAR FILTERS ===
ENABLE_FOMC_SKIP = False  # Walk-forward showed FOMC skip is overfit — hurts OOS
ENABLE_TW_SKIP   = False  # TW days are profitable — do NOT skip

# === ACCOUNT ===
STARTING_EQUITY    = 50_000.0
ANNUAL_RF_RATE     = 0.05
COMMISSION         = 0.50  # per contract per leg (TradeStation)

# === LOGGING ===
LOG_COLS = [
    "date", "side", "entry_time", "exit_time",
    "spx_at_entry", "spx_at_exit",
    "short_strike", "long_strike",
    "qty", "credit_received", "spread_value_exit",
    "pnl_earned", "win", "loss",
    "equity_after",
    "vix_level", "indicator_value",
]

# === ANALYSIS FLAGS ===
RUN_VIX_ANALYSIS  = True
RUN_MONTHLY_PNL   = True
RUN_WORST_DAYS    = True
