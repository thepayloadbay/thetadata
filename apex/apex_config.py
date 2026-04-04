"""Apex — Configuration & Calendar.

Autonomous 0DTE SPX credit spread research.
$30k BP constraint, no stop-loss reliance, real bid/ask pricing.
"""
from __future__ import annotations

import os
from datetime import datetime, time

# ——————————————————————————————————————
#  PATHS
# ——————————————————————————————————————
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "..", "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")

def _out(name: str) -> str:
    base, ext = name.rsplit(".", 1)
    return os.path.join(LOGS_DIR, f"{base}_{_RUN_TS}.{ext}")

SAVE_FILE = _out("apex_trades.csv")
LOG_FILE = _out("apex_run.log")

# ——————————————————————————————————————
#  BACKTEST RANGE
# ——————————————————————————————————————
PILOT_YEAR_START = "2022-01-03"
PILOT_YEAR_END = "2026-03-25"
STARTING_EQUITY = 100_000.0
ANNUAL_RISK_FREE_RATE = 0.05

# ——————————————————————————————————————
#  STRATEGY PARAMETERS (mutable by CLI / sweep)
# ——————————————————————————————————————

# Direction: "always_put", "always_call", "both_sides", "ema"
DIRECTION_MODE = "both_sides"

# Entry timing
ENTRY_TIME = "15:55:00"          # Single entry time (HH:MM:SS)
ENABLE_MULTI_ENTRY = False
ENTRY_START = time(10, 0)        # Multi-entry window start
ENTRY_END = time(14, 0)          # Multi-entry window end
ENTRY_INTERVAL = 30              # Minutes between entries
MAX_TRADES_DAY = 20              # Max positions per day

# Spread structure
WIDTH = 20.0                     # Spread width in points
MIN_OTM_DISTANCE = 12.0          # Minimum OTM distance from spot
STRIKE_STEP = 5                  # SPX strike grid
MIN_NET_CREDIT = 0.05            # Minimum credit to enter ($5/contract)
QTY = 15                         # Contracts per entry

# Account / BP
ACCOUNT_BP = 30_000.0            # Max buying power
COMMISSION = 0.50                # Per leg per contract

# Risk management (NO stop losses by default — skip bad days instead)
ENABLE_DAILY_SL = False
DAILY_SL = -2000.0               # Emergency circuit breaker only
ENABLE_PER_POS_SL = False
PER_POS_SL_AMOUNT = -500.0

# ——————————————————————————————————————
#  FILTERS (all OFF by default — research turns them on)
# ——————————————————————————————————————
ENABLE_VIX_MAX_FILTER = False
VIX_MAX = 30.0

ENABLE_VIX_MIN_FILTER = False
VIX_MIN = 12.0

ENABLE_VIX_TERM_FILTER = False   # VIX vs VIX9D ratio
VIX_TERM_MAX_RATIO = 1.2         # Skip if VIX/VIX9D > ratio (backwardation)

ENABLE_VVIX_FILTER = False
VVIX_MAX = 120.0

# Williams VIX Fix (synthetic fear from price — no external data)
ENABLE_WVF_FILTER = False
WVF_MODE = "bb"           # "bb" = above Bollinger Band, "percentile" = rank, "absolute" = raw value
WVF_PERCENTILE_MAX = 85   # For percentile mode: skip if WVF rank > this
WVF_ABSOLUTE_MAX = 5.0    # For absolute mode: skip if WVF > this

ENABLE_PRIOR_DAY_RETURN_FILTER = True
PRIOR_DAY_RETURN_MAX = 1.5       # Skip if |prior day return| > 1.5%

ENABLE_MORNING_MOMENTUM_FILTER = False
MORNING_MOMENTUM_MAX = 1.0       # Skip if |9:30→entry return| > X%

ENABLE_PRIOR_DAY_RANGE_FILTER = False
PRIOR_DAY_RANGE_MAX = 2.0        # Skip if prior-day range% > X%

ENABLE_ADAPTIVE_OTM = False       # VIX-adaptive OTM distance
ADAPTIVE_OTM_RULES = [            # (vix_lo, vix_hi, otm_distance)
    (0.0, 20.0, 12.0),
    (20.0, 25.0, 15.0),
    (25.0, 30.0, 20.0),
    (30.0, 999.0, 25.0),
]

ENABLE_INTRADAY_RANGE_FILTER = False  # Skip if intraday range by 15:00 > X%
INTRADAY_RANGE_MAX = 2.0

ENABLE_GAP_FILTER = False             # Skip if today's open gaps from prior close
GAP_MAX = 0.7                         # Max gap% allowed

ENABLE_CONSECUTIVE_CALM_FILTER = False # Only trade after N consecutive calm days
CONSECUTIVE_CALM_DAYS = 2
CONSECUTIVE_CALM_THRESHOLD = 1.0      # Each day must have return < this %

ENABLE_OI_FILTER = False              # Skip if nearby OI at short strike is too low
OI_MIN_NEARBY = 1000                  # Min total OI within 10pts of short strike
OI_NEARBY_RANGE = 10                  # Points either side to sum OI

ENABLE_STRADDLE_FILTER = False        # Skip if 15:50 ATM straddle > threshold
STRADDLE_MAX = 5.0                    # Max straddle price to allow entry
STRADDLE_TIME = "15:50:00"            # Time to check straddle

# VIX/16-derived filters (from deep VIX analysis)
ENABLE_VOL_CONSUMED_FILTER = False    # Skip if daily range by 15:00 > N × VIX/16 expected
VOL_CONSUMED_MAX = 1.0                # Max ratio of (range by 15:00) / (VIX/16 daily pts)

ENABLE_VOL_SURPRISE_FILTER = False    # Skip if actual daily range >> VIX prediction
VOL_SURPRISE_MAX = 2.0                # Max ratio (daily range / VIX/16 expected). >2x = 14.8% breach

ENABLE_VIX1D_RATIO_FILTER = False     # Skip if VIX1D/VIX > threshold (backwardation = risky)
VIX1D_RATIO_MAX = 1.1                 # VIX1D/VIX > 1.1 → 7.3% breach rate

ENABLE_SAFETY_RATIO_FILTER = False    # Skip if d=12 / expected_5min_move < threshold
SAFETY_RATIO_MIN = 1.5                # d=12 / (VIX@15:50/16 × spot × √(5/390))

ENABLE_EMA_DIRECTION = False
EMA_FAST = 13
EMA_SLOW = 48

ENABLE_GEX_FILTER = True         # GEX regime filter (uses SqueezeMetrics multi-expiry GEX)
GEX_MIN_QUINTILE = 4             # Trade only when prior-day SQZ GEX >= this quintile (1-5)
# SQZ GEX quintile thresholds (from squeezemetrics.csv full history):
#   Q1: < -700M (volatile, trending)
#   Q2: -700M to 1.8B
#   Q3: 1.8B to 3.7B
#   Q4: 3.7B to 5.4B (calm)
#   Q5: > 5.4B (ultra-calm, dealer-pinned)
# Q4+ skip W+Th: $15,098 P&L, -$172 DD, Sharpe 10.26, WR 95.4%, PF 34.6x
# Q5 skip W+Th: $10,560 P&L, $0 DD, Sharpe 10.26, WR 97.6%, PF inf

ENABLE_FOMC_SKIP = False
ENABLE_CPI_SKIP = False
ENABLE_NFP_SKIP = False
ENABLE_OPEX_SKIP = False
ENABLE_TW_SKIP = False

ENABLE_DOW_FILTER = True
SKIP_DAYS_OF_WEEK = {2, 3}       # Skip Wed (FOMC) + Thu

# ——————————————————————————————————————
#  CALENDAR
# ——————————————————————————————————————
MARKET_HOLIDAYS = {
    "20220117", "20220221", "20220415", "20220530", "20220620",
    "20220704", "20220905", "20221127", "20221226",
    "20230102", "20230116", "20230220", "20230407", "20230529",
    "20230619", "20230704", "20230904", "20231123", "20231225",
    "20240101", "20240115", "20240219", "20240329", "20240527",
    "20240619", "20240704", "20240902", "20241128", "20241225",
    "20250101", "20250120", "20250217", "20250418", "20250526",
    "20250704", "20250901", "20251127", "20251225",
    "20260101", "20260119", "20260216", "20260403", "20260525",
    "20260703", "20260907", "20261126", "20261225",
}

EARLY_CLOSE_DAYS = {
    "20221125", "20231124", "20241129", "20241224",
    "20251128", "20251224", "20261127", "20261224",
}

FOMC_DATES = {
    "20220126", "20220316", "20220504", "20220615", "20220727",
    "20220921", "20221102", "20221214",
    "20230201", "20230322", "20230503", "20230614", "20230726",
    "20230920", "20231101", "20231213",
    "20240131", "20240320", "20240501", "20240612", "20240731",
    "20240918", "20241107", "20241218",
    "20250129", "20250319", "20250507", "20250618", "20250730",
    "20250917", "20251029", "20251210",
    "20260128", "20260318", "20260429", "20260617", "20260729",
    "20260916", "20261104", "20261216",
}

CPI_DATES = {
    "20220112", "20220210", "20220310", "20220412", "20220511",
    "20220610", "20220713", "20220810", "20220913", "20221013",
    "20221110", "20221213",
    "20230112", "20230214", "20230314", "20230412", "20230510",
    "20230613", "20230712", "20230810", "20230913", "20231012",
    "20231114", "20231212",
    "20240111", "20240213", "20240312", "20240410", "20240515",
    "20240612", "20240711", "20240814", "20240911", "20241010",
    "20241113", "20241211",
    "20250115", "20250212", "20250312", "20250410", "20250513",
    "20250611", "20250710", "20250812", "20250910", "20251015",
    "20251112", "20251210",
    "20260114", "20260211", "20260311", "20260414", "20260512",
    "20260610", "20260715", "20260812", "20260916", "20261014",
    "20261112", "20261209",
}

NFP_DATES = {
    "20220107", "20220204", "20220304", "20220401", "20220506",
    "20220603", "20220708", "20220805", "20220902", "20221007",
    "20221104", "20221202",
    "20230106", "20230203", "20230310", "20230407", "20230505",
    "20230602", "20230707", "20230804", "20230901", "20231006",
    "20231103", "20231208",
    "20240105", "20240202", "20240308", "20240405", "20240503",
    "20240607", "20240705", "20240802", "20240906", "20241004",
    "20241101", "20241206",
    "20250110", "20250207", "20250307", "20250404", "20250502",
    "20250606", "20250703", "20250801", "20250905", "20251003",
    "20251107", "20251205",
    "20260109", "20260206", "20260306", "20260403", "20260508",
    "20260605", "20260702", "20260807", "20260904", "20261002",
    "20261106", "20261204",
}

TRIPLE_WITCHING_DATES = {
    "20220318", "20220617", "20220916", "20221216",
    "20230317", "20230616", "20230915", "20231215",
    "20240315", "20240621", "20240920", "20241220",
    "20250321", "20250620", "20250919", "20251219",
    "20260320", "20260619", "20260918", "20261218",
}

# ——————————————————————————————————————
#  TRADE LOG COLUMNS
# ——————————————————————————————————————
LOG_COLS = [
    "entry_date", "entry_time", "option_type", "short_strike", "long_strike",
    "width", "credit_received", "short_bid", "short_ask", "long_bid", "long_ask",
    "qty", "vix_level", "otm_distance", "exit_value", "pnl", "outcome",
    "close_time", "spx_at_entry", "spx_at_exit", "equity_after",
]
