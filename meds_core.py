"""meds_core — re-exports from meds_config and meds_engine.

Downstream modules (meds.py, meds_reporting.py, meds_sweeps.py) import
from meds_core.  This shim keeps those imports working after the split
into meds_config (constants) and meds_engine (logic).
"""
from meds_config import *
from meds_config import (
    _build_calendar_event_dates,
    _out,
    _RUN_TS,
)

from meds_engine import *
from meds_engine import (
    _apply_fdr_to_rows,
    _build_daily_indicators,
    _compute_pvalue,
    _CPI_DATES,
    _DAILY_INDICATORS,
    _EOM_DATES,
    _EOQ_DATES,
    _fdr_adjust,
    _fetch_day_data,
    _get_baseline_mode,
    _get_effective_sl,
    _get_session,
    _load_local_quotes,
    _local_quotes_date,
    _local_quotes_df,
    _passes_active_day_filters,
    _passes_filter,
    _PCE_DATES,
    _POST_HOL_DATES,
    _PRE_TW_DATES,
    _prior_day_ind,
    _quote_cache,
    _simulate_day,
    _sl_label,
)
