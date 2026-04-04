"""SimSearch configuration — all paths, parameters, and feature definitions."""
from __future__ import annotations

import os
import pathlib
from datetime import datetime

# -------------------------------------------
#  Paths
# -------------------------------------------
PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
REFS_DIR = os.path.join(PROJECT_ROOT, "references")
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Pre-built feature cache
FEATURE_CACHE = os.path.join(LOGS_DIR, "feature_cache.parquet")

# -------------------------------------------
#  Date ranges
# -------------------------------------------
# Tier 1 features available from VIX history start
TIER1_START = "1993-01-01"   # VIX reliable from 1993
# Tier 2 features available from SPX 1-minute data
TIER2_START = "2013-01-03"
# Tier 3 features available from options data
TIER3_START = "2022-01-03"

# Default query range for validation
VALIDATION_START = "2023-01-03"  # 1 year of warmup for Tier 3
VALIDATION_END = "2026-03-25"

# -------------------------------------------
#  Similarity parameters
# -------------------------------------------
TOP_K = 20                   # Number of similar days to return
MIN_SIMILARITY = 0.3         # Minimum cosine similarity to include
LOOKBACK_DAYS = None          # None = use all history; int = rolling window

# Feature tier weights (higher = more important)
TIER_WEIGHTS = {
    1: 1.0,   # Price & volatility
    2: 1.5,   # Intraday shape (more informative)
    3: 2.0,   # Options & positioning (most informative)
}

# -------------------------------------------
#  Feature groups & weights
# -------------------------------------------
# Individual feature weight overrides (default 1.0)
# Weights calibrated from ablation correlation study (2026-04-04).
# Features with |r| > 0.4 with range get weight 2.0-3.0.
# Features with |r| < 0.05 get weight 0.1 (nearly excluded).
FEATURE_WEIGHTS = {
    # Tier 1 — dominant range predictors
    "vix_prev_close": 3.0,            # r=0.74
    "realized_vol_20d": 3.0,          # r=0.64
    "spx_prev_range_pct": 2.5,        # r=0.65
    "spx_dist_from_20d_high": 2.0,    # r=0.69
    "spx_prev_return_20d": 1.5,       # r=0.46
    "spx_above_sma50": 1.5,           # r=0.40
    "spx_rsi_14": 1.5,                # r=0.38
    # Cross-asset (expected moderate signal)
    "tlt_prev_return_1d": 1.5,
    "gld_prev_return_1d": 1.0,
    # Multi-day context
    "spx_prev2_range_pct": 2.0,       # vol clustering
    "vix_momentum_3d": 1.5,
    # Calendar — ablation says noise
    "dow": 0.1,
    "month": 0.1,
    "is_month_end": 0.1,
    "is_quarter_end": 0.1,
    "is_triple_witching": 0.1,
    "days_to_next_fomc": 0.2,
    # Weak features
    "spx_gap_pct": 0.3,               # r=0.08
    # Tier 2 — intraday shape
    "prev_morning_range_pct": 2.0,     # r=0.54
    "prev_midday_range_pct": 2.0,      # r=0.55
    "prev_afternoon_range_pct": 2.0,   # r=0.52
    "prev_max_1min_move_pct": 1.5,     # r=0.48
    # Tier 3 — options (IV dominates, GEX weak for range)
    "iv_atm_open": 3.0,               # r=0.77
    "iv_atm_put_open": 2.5,           # r=0.74
    "iv_atm_call_open": 2.5,          # r=0.75
    "gamma_concentration": 2.0,        # r=0.58
    "vix1d_vix_ratio": 2.0,           # r=0.53
    "call_wall_distance_pct": 1.5,     # r=0.47
    "vvix_level": 1.5,                # r=0.45
    "gex_regime_encoded": 0.5,         # r=0.17 — surprisingly weak
    "iv_skew_25d": 0.2,               # r=0.004 — noise
}

# -------------------------------------------
#  Market calendar (imported from meds_config)
# -------------------------------------------
MARKET_HOLIDAYS = {
    "20220117", "20220221", "20220415", "20220530", "20220620", "20220704",
    "20220905", "20221127", "20221226",
    "20230102", "20230116", "20230220", "20230407", "20230529", "20230619",
    "20230704", "20230904", "20231123", "20231225",
    "20240101", "20240115", "20240219", "20240329", "20240527", "20240619",
    "20240704", "20240902", "20241128", "20241225",
    "20250101", "20250120", "20250217", "20250418", "20250526", "20250704",
    "20250901", "20251127", "20251225",
    "20260101", "20260119", "20260216", "20260403", "20260525", "20260703",
    "20260907", "20261126", "20261225",
}

FOMC_DATES = {
    "20220126", "20220316", "20220504", "20220615", "20220727", "20220921",
    "20221102", "20221214",
    "20230201", "20230322", "20230503", "20230614", "20230726", "20230920",
    "20231101", "20231213",
    "20240131", "20240320", "20240501", "20240612", "20240731", "20240918",
    "20241107", "20241218",
    "20250129", "20250319", "20250507", "20250618", "20250730", "20250917",
    "20251029", "20251210",
    "20260128", "20260318", "20260506", "20260617", "20260729", "20260916",
    "20261028", "20261209",
}

TRIPLE_WITCHING_DATES = {
    "20220318", "20220617", "20220916", "20221216",
    "20230317", "20230616", "20230915", "20231215",
    "20240315", "20240621", "20240920", "20241220",
    "20250321", "20250620", "20250919", "20251219",
    "20260320", "20260619", "20260918", "20261218",
}

# -------------------------------------------
#  Run timestamp
# -------------------------------------------
RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")

def out_path(name: str) -> str:
    """Generate timestamped output path in logs/."""
    base, ext = os.path.splitext(name)
    return os.path.join(LOGS_DIR, f"{base}_{RUN_TS}{ext}")
