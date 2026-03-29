#!/usr/bin/env python3
"""
vix_zone_filter_test.py

Tests CALL-specific day filters applied only when VIX is within a target zone.
Motivation: all major drawdown events are CALL spreads, many clustering on
low-IV (IvRank<15), overbought (RSI>65), narrow-range (<0.75%), or EOM days.

Logic: skip day if it is a CALL day AND VIX in [VIX_ZONE_MIN, VIX_ZONE_MAX)
       AND secondary filter condition is met (skip-worthy).
"""

import asyncio
import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meds as m

# ── Config ──────────────────────────────────────────────────────────────────
# VIX sub-ranges to sweep (inclusive lower, exclusive upper)
VIX_RANGES = [
    (13.0, 25.0, "VIX 13–25"),
    (15.0, 25.0, "VIX 15–25"),
    (15.0, 20.0, "VIX 15–20"),
    (20.0, 25.0, "VIX 20–25"),
]

# Filter spec: (filter_name, threshold, description)
#   filter_name  — must match a key in _passes_filter OR the special "eom" sentinel
#   threshold    — value passed to _passes_filter; for "eom" it is ignored
#   description  — human-readable label
CALL_FILTERS = [
    ("dIvRank_max",   15,   "IvRank<15 (skip low-IV CALL)"),
    ("dRsi_min",      65,   "RSI>65 (skip overbought CALL)"),
    ("dRangePct_max", 0.75, "RangePct<0.75% (skip narrow-range CALL)"),
    ("eom",           None, "EOM/EOQ (skip last-trading-day CALL)"),
]
# ────────────────────────────────────────────────────────────────────────────

_original_passes = m._passes_active_day_filters


def make_call_zone_filter(filter_name, threshold, vix_min, vix_max):
    """Return a patched _passes_active_day_filters that skips a day only when:
      - the day's direction is CALL, AND
      - VIX is within [vix_min, vix_max), AND
      - the secondary condition is triggered (e.g. low IV rank, high RSI, EOM).
    All other days pass through the original filter unchanged.
    """
    def f(date_str, vix_level=None):
        if not _original_passes(date_str, vix_level):
            return False
        # Only apply to CALL days
        if m._get_baseline_mode(date_str) != "always_call":
            return True
        # Only apply within target VIX range
        if vix_level is None:
            return True
        if not (vix_min <= vix_level < vix_max):
            return True
        # EOM special case
        if filter_name == "eom":
            return date_str not in m._EOM_DATES
        # Generic filter: _passes_filter returns True when condition is OK to trade.
        # For max-style filters (IvRank_max, RangePct_max, Rsi_min used as upper cap):
        # We want to SKIP when value is BELOW threshold (for IvRank/RangePct)
        # or ABOVE threshold (for RSI). _passes_filter already handles the semantics:
        #   dIvRank_max  returns True if IvRank <= threshold  → skip when passes=False
        #   dRsi_min     returns True if RSI >= threshold     → skip when passes=False
        #   dRangePct_max returns True if range <= threshold  → skip when passes=False
        # So: if _passes_filter returns False, the condition is met — skip this day.
        if not m._passes_filter(filter_name, threshold, date_str):
            return False
        return True
    return f


def last_run():
    with open(m.RUN_HISTORY_FILE) as fh:
        history = json.load(fh)
    return history[-1]


def fmt(label, e):
    pnl    = e.get("total_pnl", 0)
    dd     = e.get("max_drawdown", 0)
    sharpe = e.get("sharpe", 0)
    wr     = e.get("win_rate_pct", 0)
    trades = e.get("total_trades", 0)
    days   = e.get("days_traded", 0)
    calmar = e.get("calmar", pnl / abs(dd) if dd else float("inf"))
    print(
        f"  {label:<42}  days={days:>4}  trades={trades:>5}  "
        f"P&L=${pnl:>10,.0f}  DD=${dd:>9,.0f}  "
        f"Sharpe={sharpe:>5.2f}  Calmar={calmar:>6.2f}  WR={wr:>5.1f}%"
    )


# ── Build indicators and module-level state ──────────────────────────────────
print("Building daily indicators...")
m._DAILY_INDICATORS.update(m._build_daily_indicators())
print(f"  {len(m._DAILY_INDICATORS)} days loaded")

m._CALENDAR_SKIP_DATES = set()
cal_event_sets = m._build_calendar_event_dates()
if m.ENABLE_CALENDAR_FILTER and m.CALENDAR_FILTER_EVENTS:
    for ev in m.CALENDAR_FILTER_EVENTS:
        m._CALENDAR_SKIP_DATES |= cal_event_sets.get(ev, set())
m._EOM_DATES.update(cal_event_sets.get("end_of_month", set()))
print(f"  EOM dates: {len(m._EOM_DATES)}")
print(f"  Calendar skip dates: {len(m._CALENDAR_SKIP_DATES)}\n")

# Suppress per-trade log noise during sweep
m.logger.setLevel(logging.WARNING)

print("=" * 110)
print("CALL-SPECIFIC CONDITIONAL FILTER SWEEP — skip CALL day only when VIX in zone AND condition met")
print("=" * 110)

results = []

# ── Baseline ─────────────────────────────────────────────────────────────────
print("\nBaseline...", flush=True)
asyncio.run(m.run())
e = last_run()
fmt("BASELINE (no filter)", e)
results.append(("BASELINE", "", e))

# ── Filter × VIX range sweep ─────────────────────────────────────────────────
for filter_name, threshold, filter_desc in CALL_FILTERS:
    print()
    for vix_min, vix_max, range_label in VIX_RANGES:
        label = f"{filter_desc} | {range_label}"
        print(f"  {label}...", flush=True)
        m._passes_active_day_filters = make_call_zone_filter(
            filter_name, threshold, vix_min, vix_max
        )
        asyncio.run(m.run())
        m._passes_active_day_filters = _original_passes
        e = last_run()
        fmt(label, e)
        results.append((filter_desc, range_label, e))

# ── Summary table ─────────────────────────────────────────────────────────────
print("\n" + "=" * 110)
print("SUMMARY")
print("=" * 110)
print(f"  {'Filter':<42}  {'Days':>4}  {'Trades':>6}  {'P&L':>11}  {'Max DD':>10}  {'Sharpe':>6}  {'Calmar':>7}  {'WR':>6}")
print("  " + "-" * 105)
for filter_desc, range_label, e in results:
    label  = "BASELINE" if range_label == "" else f"{filter_desc} | {range_label}"
    pnl    = e.get("total_pnl", 0)
    dd     = e.get("max_drawdown", 0)
    sharpe = e.get("sharpe", 0)
    wr     = e.get("win_rate_pct", 0)
    trades = e.get("total_trades", 0)
    days   = e.get("days_traded", 0)
    calmar = e.get("calmar", pnl / abs(dd) if dd else float("inf"))
    print(
        f"  {label:<42}  {days:>4}  {trades:>6}  ${pnl:>10,.0f}  ${dd:>9,.0f}  "
        f"{sharpe:>6.2f}  {calmar:>7.2f}  {wr:>5.1f}%"
    )
