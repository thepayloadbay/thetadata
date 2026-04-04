"""Zenith Fade CCS — Configuration.

All strategy parameters, market calendar, and file paths.
Ported from PineScript "Zenith Fade CCS" indicator.

Strategy: Sell credit call spreads on SPX when upside exhaustion signals fire.
Entry: Signal day's close (~3:50 PM).  Exit: Next day's 4:00 PM close.
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


# ---------------------------------------------
#  GLOBAL CONFIG
# ---------------------------------------------
PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR     = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

PILOT_YEAR_START = "2022-01-03"   # SPX OHLC data starts here
PILOT_YEAR_END   = "2026-03-25"

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")

def _out(name: str) -> str:
    """Insert _RUN_TS before the extension and place in LOGS_DIR."""
    base, ext = name.rsplit(".", 1)
    return os.path.join(LOGS_DIR, f"{base}_{_RUN_TS}.{ext}")

SAVE_FILE        = _out("zenith_trades.csv")
LOG_FILE         = _out("zenith.log")
RUN_HISTORY_FILE = os.path.join(LOGS_DIR, "run_history.json")

# +----------------------------------------------------------------------+
# |  STRATEGY: Zenith Fade CCS (Credit Call/Put Spread)                  |
# |  Signal   : Upside/downside exhaustion via variance + momentum       |
# |  Execution: SPX bidirectional credit spreads, 1 trade per signal day |
# |  Entry    : Signal day close (~3:50 PM)                              |
# |  Exit     : Next day close (4:00 PM) or intraday SL                 |
# +----------------------------------------------------------------------+
# |  HONEST BASELINE  (2022-01-03 -> 2026-04-01)  run 2026-04-04         |
# |  Total P&L    : $39,964    Win rate : 78.3%   Sharpe : 10.61        |
# |  Max drawdown : -$652      Calmar: 16.43   PF: 4.79                 |
# |  Trades       : 387        Entry: 9:45 AM bar                       |
# |  H17: put score 50, H16: SL -0.20, H4: skip big up days,           |
# |  H8: noon profit-taking. All plateau-validated or structural.        |
# +----------------------------------------------------------------------+

# === SIGNAL LOGIC MODE ===
# "classic"  = AND logic across all enabled filters (PineScript "Classic Filters")
# "dynamic"  = weighted exhaustion score (PineScript "Dynamic Score")
SIGNAL_MODE = "dynamic"

# === DYNAMIC SCORE PARAMETERS ===
# exhaustionScore = (variancePercent * wVar) + (dRelVol * wVol) - (risingRocket * wRocket)
# Signal fires when exhaustionScore >= scoreThreshold
# NOTE: dRelVol is constant 1.0 (no SPX volume) so score = varPct*1.4 + 8.9 - rocket*274
W_VAR              = 1.4
W_VOL              = 8.9
W_ROCKET           = 274.0
SCORE_THRESHOLD    = 68.0   # Plateau-validated: 66-72 all Sharpe > 1.8. Was 41.5 (PineScript default)

# === CLASSIC FILTER THRESHOLDS ===
VARIANCE_THRESHOLD      = 55.2    # Close-to-Low variance %
CLUSTER_THRESHOLD       = 4       # Number of variance-low days in lookback
CLUSTER_LOOKBACK        = 4       # Bars to look back for cluster count (same as ClusterThreshold in PS)
REL_VOL_THRESHOLD       = 0.25    # Volume / SMA(volume, 20)
BODY_SIZE_MULTIPLIER    = 0.011   # Body >= ATR * multiplier
BODY_RATIO              = 1.0     # Body / Range (1.0 = filter off)
RISING_ROCKET_THRESHOLD = 0.058   # (close - SMA5) / SMA5
RSI_THRESHOLD           = 80.0    # RSI(14) threshold
DIST_SMA_THRESHOLD      = -0.01   # (close - SMA20) / SMA20
STOCH_THRESHOLD         = 20.0    # Stochastic(14) threshold
GAP_THRESHOLD           = -0.005  # Gap down limit (negative = allow gap down this big)

# === CLASSIC FILTER TOGGLES ===
USE_VARIANCE_FILTER      = True
USE_REL_VOL_FILTER       = True
USE_MIN_MOVE_FILTER      = True
USE_RISING_ROCKET_FILTER = True
USE_CLUSTER_FILTER       = True
USE_TREND_SMA_FILTER     = False   # Close <= SMA200
USE_GAP_FILTER           = False
USE_BODY_RATIO_FILTER    = False
USE_VIX_FILTER           = False   # VIX >= VIX SMA(20)
USE_RSI_FILTER           = False
USE_DIST_SMA_FILTER      = False
USE_STOCH_FILTER         = False

# ORB (Opening Range Breakout) filters
USE_ORB_FILTER           = False   # Skip if ORB width > threshold (too volatile)
ORB_MAX_WIDTH            = 20.0    # Max ORB width in SPX points
USE_ORB_CONTAINMENT_FILTER = False # Only trade if close stayed inside ORB (range-bound day)

# === STRIKE LOGIC ===
STRIKE_OFFSET  = 30.0    # Default OTM offset (used when adaptive is off)
SPREAD_WIDTH   = 10.0    # Width of the call spread in points

# -- Improvement #1: VIX-Adaptive Strike Offset --
ENABLE_VIX_ADAPTIVE_OFFSET = False  # REVERTED: tier boundaries not swept. Use fixed 30pt
VIX_OFFSET_TIERS = [
    # (vix_max, call_offset, put_offset)
    (13.0,  15.0,  15.0),   # Very calm: sell closer for bigger credit
    (18.0,  20.0,  20.0),   # Low vol
    (22.0,  30.0,  30.0),   # Default
]
# If VIX exceeds all tiers, use STRIKE_OFFSET / PUT_STRIKE_OFFSET as fallback

# === CREDIT MODEL ===
# "fixed"  = PineScript's fixed $4 credit per $10 width (unrealistic at high offsets)
# "bs"     = Black-Scholes estimated credit using VIX as IV proxy
# "real"   = Actual bid/ask from ThetaData option_quotes (falls back to BS if unavailable)
CREDIT_MODEL       = "real"  # "fixed", "bs", or "real"
FIXED_CREDIT       = 4.0    # $ credit received per spread (only used when CREDIT_MODEL="fixed")
MAX_RISK_PER_SPREAD = SPREAD_WIDTH - FIXED_CREDIT  # $6.00 (for fixed model)

# === RISK MANAGEMENT ===
USE_FIXED_RISK     = True
RISK_AMOUNT        = 600.0   # $ risk per trade
TP_CAP             = 0.66    # Max profit = 66% of risk ($396)
SL_CAP             = -1.0    # Max loss = -100% of risk (-$600)

# -- Intraday Stop Loss --
# When enabled, loads 1-min SPX bars for exit day and checks if spread breaches SL intraday.
# If SPX high at any bar causes spread_return <= INTRADAY_SL_CAP, close at that bar's close.
# This is the biggest lever: sweep shows SL=-0.5 gives $101k vs -$8k without.
ENABLE_INTRADAY_SL = True
INTRADAY_SL_CAP    = -0.20   # Tightened from -0.25. Plateau-validated H16: Sharpe 9.40 vs 8.32

# -- Improvement #2: Minimum Credit Floor --
ENABLE_CREDIT_FLOOR = True
MIN_CREDIT          = 0.75   # Skip trade if credit < this (removes low-quality trades)

# -- Improvement #3: Danger-Score Sizing (replaces naive score sizing) --
# Danger score = composite of factors that predict losses:
#   - Score dead zone (78-95 calls, 25-32 puts) → +2
#   - VIX 16-18 (worst WR bucket) → +1
#   - Wednesday (worst day) → +0.5
#   - Low stochastic (<50) on call side → +1
#   - High credit (>3.5, closer to ATM) → +0.5
ENABLE_DANGER_SIZING = False   # REVERTED: in-sample fitted, not validated OOS
DANGER_LOW_MULT      = 3.0
DANGER_MED_MULT      = 1.5
DANGER_HIGH_THRESH   = 2.0

# -- Improvement #3b: Streak-Based Sizing Boost --
# When the signal fires N consecutive days, boost sizing (exhaustion building up)
ENABLE_STREAK_BOOST  = False   # REVERTED: 39-trade sample, not validated OOS
STREAK_BOOST_DAY     = 2
STREAK_BOOST_MULT    = 1.5
# Score sizing kept for backward compat but superseded by danger sizing
ENABLE_SCORE_SIZING = False
SCORE_SIZING_TIERS = [
    (85.0,  2.0),
    (75.0,  1.5),
    (68.0,  1.0),
]
PUT_SCORE_SIZING_TIERS = [
    (20.0,  2.0),
    (30.0,  1.5),
    (40.0,  1.0),
]

# -- Improvement #4: Overnight Gap Dynamic SL --
ENABLE_GAP_DYNAMIC_SL = False  # REVERTED: 0.3% threshold hand-picked, not validated
GAP_TIGHT_SL     = -0.15   # Tighten SL when gap is adverse (call: gap up, put: gap down)
GAP_WIDE_SL      = -0.40   # Widen SL when gap is favorable (call: gap down, put: gap up)
GAP_THRESHOLD    = 0.003   # 0.3% gap triggers adjustment

# -- Improvement #5: SL Re-Entry --
ENABLE_SL_REENTRY     = True
REENTRY_OFFSET        = 25.0   # New spread offset from current price at re-entry time
REENTRY_AFTER_MINUTES = 60     # Wait N minutes after SL trigger before checking for re-entry
REENTRY_PULLBACK_PCT  = 0.5    # SPX must pull back >= this fraction of the SL spike to re-enter

# -- Improvement #8: ATR-Scaled Spread Width --
ENABLE_ATR_WIDTH = False       # REVERTED: tier boundaries hand-picked, not swept
ATR_WIDTH_TIERS = [
    # (atr_max, call_width, put_width)
    (40.0,  10.0,  10.0),   # Calm: standard width
    (60.0,  15.0,  15.0),   # Normal: wider for more credit
    (999.0, 20.0,  20.0),   # Volatile: widest
]

# -- Improvement #9: Weekly Exhaustion Super-Signal --
ENABLE_WEEKLY_SUPER = False    # REVERTED: never validated OOS, creates 9x multiplier risk
WEEKLY_VAR_THRESHOLD = 70.0    # 5-day rolling variance must exceed this
SUPER_SIGNAL_WIDTH_MULT = 1.5  # Multiply width by this on super-signals
SUPER_SIGNAL_SIZE_MULT = 2.0   # Additional sizing multiplier for super-signals

# -- Improvement #10: Compound Growth Sizing --
ENABLE_COMPOUND_GROWTH = False
COMPOUND_GROWTH_STEP   = 30_000.0  # Every $N in cumulative P&L increases base risk
COMPOUND_GROWTH_ADD    = 150.0     # Add $N to base risk per step
COMPOUND_GROWTH_MAX    = 1500.0    # Max base risk cap (2.5x starting)

# -- Improvement #11: Iron Condor Days --
ENABLE_IRON_CONDOR = False     # REVERTED: 0.5% threshold arbitrary, inflates trade count
# When the daily bar qualifies for BOTH call (score>=68) and put (score<=40 from PRIOR context),
# enter both spreads. Uses the 5-day midpoint: if close > midpoint → call, if close < midpoint → put,
# if close is NEAR midpoint (within 0.5%) → both (iron condor).
IRON_CONDOR_RANGE_PCT = 0.005  # Within 0.5% of 5-day midpoint → both sides

# -- Improvement #12: Intraday Momentum Confirmation --
ENABLE_INTRADAY_CONFIRM = False # REVERTED: thresholds hand-picked, adds complexity
CONFIRM_LOOKBACK_BARS   = 30   # Check last 30 bars (minutes) of signal day
CONFIRM_FADE_THRESHOLD  = 3.0  # SPX dropped 3+ pts in last 30min → fade confirmed → 1.5x
CONFIRM_FADE_MULT       = 1.5  # Sizing boost for confirmed fades
CONFIRM_STRONG_TREND    = 10.0 # SPX moved 10+ pts WITH the exhaustion → skip (reversal done)

# === ACCOUNT ===
STARTING_EQUITY    = 100_000.0
ANNUAL_RF_RATE     = 0.045    # 4.5% risk-free rate for Sharpe

# === VIX FILTER SETTINGS ===
VIX_MAX_FILTER     = 22.0    # Skip when VIX > this. Sweep: VIX 20-30 is danger zone. VIX<22 Sharpe 1.90

# -- Improvement #6: VIX Term Structure Filter --
# Skip trades when VIX term structure is in backwardation (VIX1D > VIX = near-term panic).
# Contango (VIX > VIX1D) = calm expected short-term → ideal for selling premium.
# Deep contango (ratio < 0.85): 69.7% WR. Backwardation (ratio > 1.0): 54.2% WR.
ENABLE_TERM_STRUCTURE_FILTER = True
TERM_STRUCTURE_MAX_RATIO     = 1.00   # Skip when VIX1D/VIX > this (backwardation only)

# -- Improvement #7: Adaptive Rolling WR Sizing --
# Adjust sizing based on recent strategy performance (rolling 30-trade WR).
# Hot streak → size up. Cold streak → reduce size or pause.
ENABLE_ROLLING_WR_SIZING     = False  # REVERTED: momentum chasing, fitted to this curve
ROLLING_WR_WINDOW            = 30     # Look back N trades
ROLLING_WR_COLD_THRESH       = 50.0   # WR below this → skip trades (cold regime)
ROLLING_WR_HOT_THRESH        = 65.0   # WR above this → 1.5x sizing (hot regime)
ROLLING_WR_HOT_MULT          = 1.5    # Multiplier when hot

# === ECON DATES (CPI + NFP) ===
ECON_DATES = {
    # CPI 2022-2026
    "20220112","20220210","20220310","20220412","20220511","20220610",
    "20220713","20220810","20220913","20221013","20221110","20221213",
    "20230112","20230214","20230314","20230412","20230510","20230613",
    "20230712","20230810","20230913","20231012","20231114","20231212",
    "20240111","20240213","20240312","20240410","20240515","20240612",
    "20240711","20240814","20240911","20241010","20241113","20241211",
    "20250115","20250212","20250312","20250410","20250513","20250611",
    "20250715","20250812","20250910","20251015","20251112","20251210",
    "20260114","20260211","20260311","20260408","20260513","20260610",
    # NFP 2022-2026
    "20220107","20220204","20220304","20220401","20220506","20220603",
    "20220708","20220805","20220902","20221007","20221104","20221202",
    "20230106","20230203","20230310","20230407","20230505","20230602",
    "20230707","20230804","20230901","20231006","20231103","20231208",
    "20240105","20240202","20240308","20240405","20240503","20240607",
    "20240705","20240802","20240906","20241004","20241101","20241206",
    "20250107","20250207","20250307","20250404","20250502","20250606",
    "20250711","20250801","20250905","20251003","20251107","20251205",
    "20260109","20260206","20260306","20260403","20260508","20260605",
}

# === COMMISSION ===
COMMISSION         = 0.50    # per contract per leg (TradeStation)

# === CALENDAR FILTERS ===
ENABLE_FOMC_SKIP   = False
ENABLE_TW_SKIP     = False

# === LOGGING ===
LOG_COLS = [
    "signal_date", "trade_date", "signal_mode",
    "spx_close_signal", "spx_close_exit",
    "short_strike", "long_strike",
    "credit_received", "spread_value_exit",
    "raw_return", "capped_return", "pnl_earned",
    "win", "loss",
    "equity_after",
    "vix_level", "variance_pct", "rel_vol", "rising_rocket",
    "exhaustion_score",
    "body_size", "atr", "rsi", "stoch", "gap_pct",
    "cluster_count", "dist_from_sma",
]

# === POST-SIGNAL FILTERS (apply after any signal mode) ===
# These filters gate the signal AFTER it fires, regardless of Classic/Dynamic mode.
# === HOLDING PERIOD ===
HOLD_DAYS = 1   # 1 = exit at T close (default), 2 = exit at T+1 close

# === BIDIRECTIONAL MODE ===
# When True, also sells put spreads on downside exhaustion (low variance score)
ENABLE_PUT_SIDE        = True
PUT_SCORE_MAX          = 50.0    # Widened from 40. Plateau 45-55 validated (H17: +$5.3k, Sharpe 8.80)
PUT_STRIKE_OFFSET      = 30.0   # Points OTM for short put
PUT_SPREAD_WIDTH       = 10.0   # Width of put spread

# === POST-SIGNAL FILTERS ===
POST_FILTER_STOCH_MIN: float | None = None
POST_FILTER_DIST_SMA_MIN: float | None = None
POST_FILTER_CLUSTER_MAX: int | None = None

# -- H2-WPR-1: Dual Williams %R Exhaustion Filter --
# Require BOTH fast and slow %R to confirm exhaustion direction.
# Call side: both > OB level (overbought). Put side: both < OS level (oversold).
# When fast and slow disagree, exhaustion is not confirmed → skip trade.
USE_WPR_FILTER     = False
WPR_FAST_PERIOD    = 21     # Fast Williams %R lookback
WPR_SLOW_PERIOD    = 112    # Slow Williams %R lookback
WPR_OB_LEVEL       = -20.0  # Overbought threshold (above this = overbought)
WPR_OS_LEVEL       = -80.0  # Oversold threshold (below this = oversold)

# -- H4: Skip call trades after big up days (+1%) —
# After a +1% up day, call spread WR drops to 61% (vs 82% after down days).
# The exhaustion signal is weakest when the move was huge — likely trend continuation.
ENABLE_BIG_UP_DAY_SKIP   = True
BIG_UP_DAY_THRESHOLD     = 0.01   # Skip call trades when prior day return > 1%

# -- H8: Noon Profit-Taking --
# 74% of eventual losers were profitable at noon. Exiting winners early at noon
# locks in gains and avoids afternoon reversals. Biggest structural edge left.
ENABLE_NOON_PROFIT_TAKE  = True
NOON_TAKE_BAR            = 150    # Bar index for noon (9:30 + 150min = 12:00)
NOON_TAKE_PCT            = 0.50   # Exit if spread value < 50% of credit at noon (50%+ profit)

# === ANALYSIS FLAGS ===
RUN_VIX_ANALYSIS             = True
RUN_MONTHLY_PNL              = True
RUN_FILTER_CONTRIBUTION      = True
RUN_WORST_DAYS               = True

# === SWEEP FLAGS (all off by default) ===
RUN_SCORE_THRESHOLD_SWEEP    = False
RUN_WEIGHT_SWEEP             = False
RUN_STRIKE_OFFSET_SWEEP      = False
RUN_WIDTH_SWEEP              = False
RUN_FILTER_SWEEP             = False
RUN_TP_SL_SWEEP              = False
RUN_RISK_AMOUNT_SWEEP        = False
RUN_VARIANCE_SWEEP           = False
RUN_REL_VOL_SWEEP            = False
RUN_ROCKET_SWEEP             = False
RUN_VIX_FILTER_SWEEP         = False
