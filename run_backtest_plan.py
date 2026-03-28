#!/usr/bin/env python3
"""
run_backtest_plan.py — expanded ~8-hour comprehensive backtest orchestration
=============================================================================
Phases:
  1.  Day Filter Sweep      — 43+ indicators, all thresholds
  2.  EMA Sweep             — 225 fast×slow combos (15×15)
  3.  Spread Width Sweep    — 10 width levels (50–400 pts)
  4.  Min Credit Sweep      — 14 credit thresholds
  5.  Stop Loss Sweep       — 11 SL levels
  6.  Daily TP Sweep        — 14 daily profit target levels
  7.  Trailing Stop Sweep   — 12 portfolio-trail levels
  8.  Position Trail Sweep  — 42 activation×pullback combos
  9.  Entry Time Sweep      — start × end × interval + DOW breakdown
 10.  Direction×Time Sweep  — PUT vs CALL vs Both-Sides per time slot
 11.  Bias Sweep            — daily indicator direction routers
 12.  Baseline Comparison   — EMA vs always-PUT vs always-CALL vs both-sides
 13.  Touch Exit Sweep      — $-touch and %-touch levels
 14.  Max Buying Power Sweep — 8 BP caps
 15.  PnL Sample Interval   — 6 MTM check frequencies
 16.  2D: TS × Daily TP     — 12 × 14 = 168 combos
 17.  2D: Width × TS        — 10 × 12 = 120 combos
 18.  2D: Width × Credit    — 10 × 14 = 140 combos
 19.  2D: Width × Daily TP  — 10 × 14 = 140 combos
 20.  3D: VIX × Width × TS  — 7 × 10 × 12 = 840 combos
 21.  MEGA 5D Combo         — VIX(7)×Width(7)×SL(4)×Credit(5)×TS(4) = 3,920 combos
 22.  Auto-extend           — runs if plan finishes under 6 hours

Usage:
  .venv/bin/python3 run_backtest_plan.py 2>&1 | tee logs/backtest_plan_$(date +%Y%m%d_%H%M%S).log
"""

import asyncio
import csv
import glob
import itertools
import os
import sys
import time
import importlib.util
from datetime import datetime

# ── Load the main backtest module ─────────────────────────────────────────────
print(f"\n{'='*60}")
print("  Loading metf_v35_bidask.py ...")
print(f"{'='*60}")

spec = importlib.util.spec_from_file_location("metf_v35_bidask", "metf_v35_bidask.py")
mod  = importlib.util.module_from_spec(spec)
sys.modules["metf_v35_bidask"] = mod
spec.loader.exec_module(mod)

# ── Date range ────────────────────────────────────────────────────────────────
mod.PILOT_YEAR_START = "2022-01-03"
mod.PILOT_YEAR_END   = "2026-03-25"
mod.USE_LOCAL_DATA   = True

print(f"  Date range : {mod.PILOT_YEAR_START} → {mod.PILOT_YEAR_END}")
print(f"  Data dir   : {mod.DATA_DIR}")
print(f"  Module loaded. _RUN_TS = {mod._RUN_TS}")

import logging as _logging
mod.logger.setLevel(_logging.INFO)
for _h in mod.logger.handlers:
    if _h.level == _logging.DEBUG:
        _h.setLevel(_logging.INFO)
print("  Logging set to INFO")


# ── Phase runner ──────────────────────────────────────────────────────────────
PHASE_LOG: list[dict] = []

def run_phase(phase_num: int, name: str, fn, overrides: dict | None = None):
    print(f"\n{'='*60}")
    print(f"  Phase {str(phase_num):>4s}: {name}")
    print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if overrides:
        print(f"  Overrides: {overrides}")
    print(f"{'='*60}")

    original: dict = {}
    if overrides:
        for k, v in overrides.items():
            original[k] = getattr(mod, k, None)
            setattr(mod, k, v)

    if isinstance(phase_num, int) and phase_num < START_FROM_PHASE:
        print(f"  Skipping (already complete)")
        return True

    t0 = time.time()
    status = "OK"
    error_msg = ""
    try:
        asyncio.run(fn())
    except Exception as e:
        import traceback
        status = "ERROR"
        error_msg = str(e)
        print(f"\n  !! Phase {phase_num} ERROR: {e}")
        traceback.print_exc()
    finally:
        for k, v in original.items():
            setattr(mod, k, v)

    elapsed = time.time() - t0
    print(f"\n  Phase {str(phase_num):>4s} {status} — elapsed: {elapsed/60:.1f} min")
    PHASE_LOG.append({
        "phase": phase_num, "name": name, "status": status,
        "elapsed": f"{elapsed/60:.1f}m", "error": error_msg,
        "elapsed_sec": elapsed,
    })
    return status == "OK"


# ── CSV helpers ───────────────────────────────────────────────────────────────
def _latest_file(pattern: str) -> str | None:
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None

def _top_values(csv_pattern, filter_name, value_col, sort_col="calmar", n=3, min_trades=30):
    fpath = _latest_file(csv_pattern)
    if not fpath:
        return []
    try:
        with open(fpath) as f:
            rows = list(csv.DictReader(f))
        if filter_name:
            rows = [r for r in rows if r.get("filter_name") == filter_name]
        rows = [r for r in rows if int(r.get("num_trades", 0) or 0) >= min_trades]
        rows_sorted = sorted(rows, key=lambda r: float(r.get(sort_col) or 0), reverse=True)
        vals = []
        for r in rows_sorted[:n]:
            raw = r.get(value_col, "")
            if raw in ("None", "—", "", None):
                vals.append(None)
            else:
                try:
                    vals.append(float(raw))
                except ValueError:
                    vals.append(raw)
        return vals
    except Exception as e:
        print(f"  [warn] Could not read {csv_pattern}: {e}")
        return []

def _parse_num(val, cast=float):
    if val is None:
        return None
    try:
        return cast(val)
    except Exception:
        return None


# ── 2D / 3D inline sweep helpers ─────────────────────────────────────────────

async def _run_2d_sweep(name, dim1_name, dim1_vals, dim2_name, dim2_vals,
                        sim_kwargs_fn, out_file, extra_cols=None):
    """Generic 2D combo sweep. sim_kwargs_fn(d1, d2) returns extra kwargs for _simulate_day."""
    import pandas as pd
    date_list = pd.date_range(mod.PILOT_YEAR_START, mod.PILOT_YEAR_END, freq='B')
    combos = list(itertools.product(dim1_vals, dim2_vals))
    trade_map = {c: [] for c in combos}

    mod.logger.info(f"{'='*60}")
    mod.logger.info(f"  2D SWEEP: {name}")
    mod.logger.info(f"  {dim1_name}: {dim1_vals}")
    mod.logger.info(f"  {dim2_name}: {dim2_vals}")
    mod.logger.info(f"  Combos: {len(combos)}")
    mod.logger.info(f"{'='*60}")

    async with mod._get_session() as session:
        for d in date_list:
            d_str = d.strftime("%Y%m%d")
            if d_str in mod.MARKET_HOLIDAYS:
                continue
            day_data = await mod._fetch_day_data(session, d_str)
            if day_data is None:
                continue
            for (v1, v2) in combos:
                kw = sim_kwargs_fn(v1, v2)
                trades, _ = await mod._simulate_day(
                    session, day_data, mod.DAILY_SL,
                    baseline_mode="always_put",
                    spread_width=mod.WIDTH,
                    min_credit=mod.MIN_NET_CREDIT,
                    entry_start=mod.ENTRY_START,
                    entry_end=mod.ENTRY_END,
                    entry_interval=mod.ENTRY_INTERVAL,
                    **kw,
                )
                trade_map[(v1, v2)].extend(trades)

    cols = [dim1_name, dim2_name, "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar"]
    if extra_cols:
        cols += extra_cols
    rows = []
    for (v1, v2), trades in trade_map.items():
        m = mod.compute_metrics(trades)
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] < 0 else float("inf")
        pf = f"{m['profit_factor']:.3f}" if m['profit_factor'] != float("inf") else "inf"
        rows.append({
            dim1_name:       str(v1) if v1 is not None else "none",
            dim2_name:       str(v2) if v2 is not None else "none",
            "num_trades":    m["num_trades"],
            "win_rate_pct":  f"{m['win_rate']:.1f}",
            "total_pnl":     f"{m['total_pnl']:.2f}",
            "avg_win":       f"{m['avg_win']:.2f}",
            "avg_loss":      f"{m['avg_loss']:.2f}",
            "profit_factor": pf,
            "max_drawdown":  f"{m['max_drawdown']:.2f}",
            "calmar":        f"{cal:.4f}" if cal != float("inf") else "inf",
            "_calmar_num":   cal,
        })
    rows.sort(key=lambda r: float(r["_calmar_num"]) if r["_calmar_num"] != float("inf") else 9999, reverse=True)

    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols if k in row})

    mod.logger.info(f"\n  {name} complete — {len(rows)} combos → {out_file}")
    hdr = f"  {dim1_name:>12} | {dim2_name:>12} | {'Trades':>7} | {'WR%':>6} | {'Total P&L':>12} | {'Max DD':>10} | {'Calmar':>8}"
    sep = "─" * len(hdr)
    mod.logger.info(sep)
    mod.logger.info(f"  TOP 20 BY CALMAR — {name}")
    mod.logger.info(sep)
    mod.logger.info(hdr)
    mod.logger.info(sep)
    for row in rows[:20]:
        mod.logger.info(
            f"  {row[dim1_name]:>12} | {row[dim2_name]:>12} | "
            f"{row['num_trades']:>7} | {row['win_rate_pct']:>5}% | "
            f"${float(row['total_pnl']):>11,.2f} | "
            f"${float(row['max_drawdown']):>9,.2f} | {row['calmar']:>8}"
        )
    mod.logger.info(sep)


async def _run_mega_combo(vix_vals, width_vals, sl_vals, credit_vals, ts_vals, out_file):
    """5D mega combo: VIX × Width × SL × Credit × TrailingStop."""
    import pandas as pd
    date_list = pd.date_range(mod.PILOT_YEAR_START, mod.PILOT_YEAR_END, freq='B')
    combos = list(itertools.product(vix_vals, width_vals, sl_vals, credit_vals, ts_vals))

    mod.logger.info(f"{'='*60}")
    mod.logger.info(f"  MEGA 5D COMBO SWEEP")
    mod.logger.info(f"  VIX    : {vix_vals}")
    mod.logger.info(f"  Width  : {width_vals}")
    mod.logger.info(f"  SL     : {sl_vals}")
    mod.logger.info(f"  Credit : {credit_vals}")
    mod.logger.info(f"  TS     : {ts_vals}")
    mod.logger.info(f"  Combos : {len(combos)}")
    mod.logger.info(f"  Output : {out_file}")
    mod.logger.info(f"{'='*60}")

    trade_map = {c: [] for c in combos}

    async with mod._get_session() as session:
        for i, d in enumerate(date_list):
            d_str = d.strftime("%Y%m%d")
            if d_str in mod.MARKET_HOLIDAYS:
                continue
            day_data = await mod._fetch_day_data(session, d_str)
            if day_data is None:
                continue
            vix_level = day_data.get("vix_level")
            if (i + 1) % 50 == 0:
                mod.logger.info(f"  Day {i+1}/{len(date_list)} | {d_str} | VIX={vix_level}")
            for (vix_max, width, sl, mc, ts) in combos:
                if vix_max is not None and vix_level is not None and vix_level > vix_max:
                    continue
                trades, _ = await mod._simulate_day(
                    session, day_data, sl,
                    spread_width=float(width),
                    min_credit=mc,
                    baseline_mode="always_put",
                    entry_start=mod.ENTRY_START,
                    entry_end=mod.ENTRY_END,
                    entry_interval=mod.ENTRY_INTERVAL,
                    trailing_stop=ts,
                    touch_exit_dollars=None,
                    touch_exit_pct=None,
                )
                trade_map[(vix_max, width, sl, mc, ts)].extend(trades)

    cols = ["vix_max", "width", "daily_sl", "min_credit", "trailing_stop",
            "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar"]
    rows = []
    for (vix_max, width, sl, mc, ts), trades in trade_map.items():
        if not trades:
            continue
        m = mod.compute_metrics(trades)
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "vix_max":       str(vix_max) if vix_max is not None else "none",
            "width":         width,
            "daily_sl":      str(sl) if sl is not None else "none",
            "min_credit":    mc,
            "trailing_stop": str(ts) if ts is not None else "none",
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(cal, 4) if cal != float("inf") else 9999.0,
            "_calmar_num":   cal,
        })
    rows.sort(key=lambda r: r["_calmar_num"] if r["_calmar_num"] != float("inf") else 9999, reverse=True)

    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})

    mod.logger.info(f"\n  Mega combo complete — {len(rows)} combos → {out_file}")

    hdr = (f"  {'Rank':>4}  {'Calmar':>8}  {'P&L':>10}  {'Max DD':>10}  "
           f"{'Trades':>7}  {'WR%':>5}  Params")
    mod.logger.info("─" * 90)
    mod.logger.info("  MEGA COMBO TOP 30 (by Calmar)")
    mod.logger.info("─" * 90)
    mod.logger.info(hdr)
    mod.logger.info("─" * 90)
    for i, r in enumerate(rows[:30], 1):
        params = (f"vix={r['vix_max']} w={r['width']} sl={r['daily_sl']} "
                  f"mc={r['min_credit']} ts={r['trailing_stop']}")
        mod.logger.info(
            f"  {i:>4}  {float(r['calmar']):>8.2f}  "
            f"${float(r['total_pnl']):>9,.0f}  "
            f"${float(r['max_drawdown']):>9,.0f}  "
            f"{r['num_trades']:>7}  {r['win_rate_pct']:>5}%  {params}"
        )
    mod.logger.info("─" * 90)


# ═════════════════════════════════════════════════════════════════════════════
#  RUN ALL PHASES
# ═════════════════════════════════════════════════════════════════════════════
plan_start = time.time()
_ts = mod._RUN_TS
START_FROM_PHASE = 22  # resume point — set to 1 to run all phases

# ── Phase 1: Day Filter Sweep ─────────────────────────────────────────────
run_phase(1, "Day Filter Sweep (43+ indicators)", mod.run_day_filter_sweep,
          overrides={"RUN_DAY_FILTER_SWEEP": True})

# ── Phase 2: EMA Sweep (expanded 15×15 = 225 combos) ─────────────────────
run_phase(2, "EMA Sweep (15×15 = 225 combos)", mod.run_ema_sweep,
          overrides={
              "RUN_EMA_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_EMA_FAST": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20],
              "SWEEP_EMA_SLOW": [18, 20, 22, 24, 26, 28, 30, 32, 36, 40, 45, 50, 55, 60, 70],
          })

# ── Phase 3: Spread Width Sweep (expanded) ───────────────────────────────
run_phase(3, "Spread Width Sweep (10 levels: 50–400 pts)", mod.run_spread_width_sweep,
          overrides={
              "RUN_SPREAD_WIDTH_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_SPREAD_WIDTHS": [50, 75, 100, 125, 150, 175, 200, 250, 300, 400],
          })

# ── Phase 4: Min Credit Sweep (expanded) ─────────────────────────────────
run_phase(4, "Min Credit Sweep (14 levels)", mod.run_min_credit_sweep,
          overrides={
              "RUN_MIN_CREDIT_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_MIN_CREDIT_LEVELS": [0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                                          0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.00],
          })

# ── Phase 5: Stop Loss Sweep (expanded) ──────────────────────────────────
run_phase(5, "Stop Loss Sweep (11 levels)", mod.run_sweep,
          overrides={
              "RUN_SL_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_SL_LEVELS": [-150, -250, -300, -500, -750, -900,
                                  -1200, -1500, -2000, -3000, None],
          })

# ── Phase 6: Daily TP Sweep (expanded) ───────────────────────────────────
run_phase(6, "Daily Profit Target Sweep (14 levels)", mod.run_daily_tp_sweep,
          overrides={
              "RUN_DAILY_TP_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_DAILY_TP_LEVELS": [400.0, 450.0, 500.0, 550.0, 600.0, 650.0,
                                        700.0, 750.0, 800.0, 900.0, 1000.0, 1100.0,
                                        1200.0, None],
          })

# ── Phase 7: Portfolio Trailing Stop (expanded) ───────────────────────────
run_phase(7, "Portfolio Trailing Stop Sweep (12 levels)", mod.run_trailing_stop_sweep,
          overrides={
              "RUN_TRAILING_STOP_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_TS_LEVELS": [50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 800, None],
          })

# ── Phase 8: Per-Position Trail (expanded 7×6=42 combos) ─────────────────
run_phase(8, "Per-Position Trail Sweep (42 combos)", mod.run_pos_trail_sweep,
          overrides={
              "RUN_POS_TRAIL_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_POS_TRAIL_ACTIVATION": [40, 50, 60, 70, 80, 90, 100],
              "SWEEP_POS_TRAIL_PULLBACK":   [5, 10, 15, 20, 25, 30],
          })

# ── Phase 9: Entry Time Sweep ─────────────────────────────────────────────
run_phase(9, "Entry Time Sweep (start × end × interval + DOW)", mod.run_entry_time_sweep,
          overrides={
              "RUN_ENTRY_TIME_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
          })

# ── Phase 10: Direction × Time ────────────────────────────────────────────
run_phase(10, "Direction × Time Sweep (PUT/CALL/Both per slot)", mod.run_direction_time_sweep,
          overrides={
              "RUN_DIRECTION_TIME_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
          })

# ── Phase 11: Bias Sweep ──────────────────────────────────────────────────
run_phase(11, "Directional Bias Sweep", mod.run_bias_sweep,
          overrides={
              "RUN_BIAS_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
          })

# ── Phase 12: Baseline Comparison ────────────────────────────────────────
run_phase(12, "Baseline Comparison", mod.run_baseline_comparison,
          overrides={
              "RUN_BASELINE_COMPARISON": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
          })

# ── Phase 13: Touch Exit Sweep ────────────────────────────────────────────
run_phase(13, "Touch Exit Sweep (dollar + pct)", mod.run_touch_sweep,
          overrides={
              "RUN_TOUCH_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_TOUCH_DOLLARS": [-30, -20, -10, -5, -2, 0, 2, 5, 10, 20, 30, 50],
              "SWEEP_TOUCH_PCT":     [-3.0, -2.0, -1.0, -0.5, -0.25, 0,
                                      0.25, 0.5, 1.0, 1.5, 2.0, 3.0],
          })

# ── Phase 14: Max Buying Power Sweep ─────────────────────────────────────
run_phase(14, "Max Buying Power Sweep (8 levels)", mod.run_max_bp_sweep,
          overrides={
              "RUN_MAX_BP_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_MAX_BP_LEVELS": [5_000, 10_000, 15_000, 20_000,
                                      25_000, 30_000, 40_000, None],
          })

# ── Phase 15: PnL Sample Interval ────────────────────────────────────────
run_phase(15, "PnL Sample Interval Sweep (6 levels)", mod.run_pnl_sample_sweep,
          overrides={
              "RUN_PNL_SAMPLE_SWEEP": True,
              "VIX_MAX_FILTER": None,
              "VIX_MIN_FILTER": None,
              "SWEEP_PNL_SAMPLE_INTERVALS": [1, 2, 3, 5, 10, 15],
          })

# ── Phase 16: 2D — TS × Daily TP (12×14 = 168 combos) ───────────────────
_ts_levels  = [50, 100, 150, 200, 300, 400, 500, 600, 700, 800, 1000, None]
_tp_levels  = [400, 450, 500, 550, 600, 650, 700, 750, 800, 900, 1000, 1100, 1200, None]
_out_16 = os.path.join(mod.LOGS_DIR, f"sweep_2d_ts_tp_{_ts}.csv")

def _ts_tp_kwargs(ts, tp):
    return {"trailing_stop": ts, "daily_tp": tp,
            "touch_exit_dollars": None, "touch_exit_pct": None}

run_phase(16, f"2D: TS × Daily TP ({len(_ts_levels)}×{len(_tp_levels)}={len(_ts_levels)*len(_tp_levels)} combos)",
          lambda: _run_2d_sweep("TS × Daily TP", "trailing_stop", _ts_levels,
                                "daily_tp", _tp_levels, _ts_tp_kwargs, _out_16))

# ── Phase 17: 2D — Width × TS (10×12 = 120 combos) ──────────────────────
_w_levels   = [50, 75, 100, 125, 150, 175, 200, 250, 300, 400]
_ts2_levels = [50, 100, 200, 300, 400, 500, 600, 700, 800, 1000, 1200, None]
_out_17 = os.path.join(mod.LOGS_DIR, f"sweep_2d_width_ts_{_ts}.csv")

def _width_ts_kwargs(w, ts):
    return {"spread_width": float(w), "trailing_stop": ts,
            "touch_exit_dollars": None, "touch_exit_pct": None}

run_phase(17, f"2D: Width × TS ({len(_w_levels)}×{len(_ts2_levels)}={len(_w_levels)*len(_ts2_levels)} combos)",
          lambda: _run_2d_sweep("Width × TS", "width", _w_levels,
                                "trailing_stop", _ts2_levels, _width_ts_kwargs, _out_17))

# ── Phase 18: 2D — Width × Credit (10×14 = 140 combos) ──────────────────
_mc_levels  = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.00]
_out_18 = os.path.join(mod.LOGS_DIR, f"sweep_2d_width_credit_{_ts}.csv")

def _width_mc_kwargs(w, mc):
    return {"spread_width": float(w), "min_credit": mc,
            "touch_exit_dollars": None, "touch_exit_pct": None}

run_phase(18, f"2D: Width × Credit ({len(_w_levels)}×{len(_mc_levels)}={len(_w_levels)*len(_mc_levels)} combos)",
          lambda: _run_2d_sweep("Width × Credit", "width", _w_levels,
                                "min_credit", _mc_levels, _width_mc_kwargs, _out_18))

# ── Phase 19: 2D — Width × Daily TP (10×14 = 140 combos) ────────────────
_out_19 = os.path.join(mod.LOGS_DIR, f"sweep_2d_width_tp_{_ts}.csv")

def _width_tp_kwargs(w, tp):
    return {"spread_width": float(w), "daily_tp": tp,
            "touch_exit_dollars": None, "touch_exit_pct": None}

run_phase(19, f"2D: Width × Daily TP ({len(_w_levels)}×{len(_tp_levels)}={len(_w_levels)*len(_tp_levels)} combos)",
          lambda: _run_2d_sweep("Width × Daily TP", "width", _w_levels,
                                "daily_tp", _tp_levels, _width_tp_kwargs, _out_19))

# ── Phase 20: 3D — VIX × Width × TS (7×10×12 = 840 combos) ──────────────
_vix3_levels = [12, 14, 14.5, 15, 16, 18, None]
_out_20 = os.path.join(mod.LOGS_DIR, f"sweep_3d_vix_width_ts_{_ts}.csv")

async def _run_3d_vix_width_ts():
    import pandas as pd
    date_list = pd.date_range(mod.PILOT_YEAR_START, mod.PILOT_YEAR_END, freq='B')
    combos = list(itertools.product(_vix3_levels, _w_levels, _ts2_levels))
    trade_map = {c: [] for c in combos}

    mod.logger.info(f"  3D VIX×Width×TS | combos={len(combos)}")

    # Temporarily disable VIX filter so all days enter pool
    saved_vix = mod.VIX_MAX_FILTER
    mod.VIX_MAX_FILTER = None
    try:
        async with mod._get_session() as session:
            for i, d in enumerate(date_list):
                d_str = d.strftime("%Y%m%d")
                if d_str in mod.MARKET_HOLIDAYS:
                    continue
                day_data = await mod._fetch_day_data(session, d_str)
                if day_data is None:
                    continue
                vix_level = day_data.get("vix_level")
                for (vix_max, width, ts) in combos:
                    if vix_max is not None and vix_level is not None and vix_level > vix_max:
                        continue
                    trades, _ = await mod._simulate_day(
                        session, day_data, mod.DAILY_SL,
                        spread_width=float(width),
                        min_credit=mod.MIN_NET_CREDIT,
                        baseline_mode="always_put",
                        entry_start=mod.ENTRY_START,
                        entry_end=mod.ENTRY_END,
                        entry_interval=mod.ENTRY_INTERVAL,
                        trailing_stop=ts,
                        touch_exit_dollars=None,
                        touch_exit_pct=None,
                    )
                    trade_map[(vix_max, width, ts)].extend(trades)
    finally:
        mod.VIX_MAX_FILTER = saved_vix

    cols = ["vix_max", "width", "trailing_stop",
            "num_trades", "win_rate_pct", "total_pnl",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown", "calmar"]
    rows = []
    for (vix_max, width, ts), trades in trade_map.items():
        if not trades:
            continue
        m = mod.compute_metrics(trades)
        cal = m["total_pnl"] / abs(m["max_drawdown"]) if m["max_drawdown"] < 0 else float("inf")
        pf = m["profit_factor"] if m["profit_factor"] != float("inf") else 9999.0
        rows.append({
            "vix_max":       str(vix_max) if vix_max is not None else "none",
            "width":         width,
            "trailing_stop": str(ts) if ts is not None else "none",
            "num_trades":    m["num_trades"],
            "win_rate_pct":  round(m["win_rate"], 1),
            "total_pnl":     round(m["total_pnl"], 2),
            "avg_win":       round(m["avg_win"], 2),
            "avg_loss":      round(m["avg_loss"], 2),
            "profit_factor": round(pf, 3),
            "max_drawdown":  round(m["max_drawdown"], 2),
            "calmar":        round(cal, 4) if cal != float("inf") else 9999.0,
            "_calmar_num":   cal,
        })
    rows.sort(key=lambda r: r["_calmar_num"] if r["_calmar_num"] != float("inf") else 9999, reverse=True)

    with open(_out_20, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in cols})
    mod.logger.info(f"  3D sweep complete — {len(rows)} combos → {_out_20}")
    for r in rows[:10]:
        mod.logger.info(
            f"  vix={r['vix_max']} w={r['width']} ts={r['trailing_stop']} | "
            f"trades={r['num_trades']} wr={r['win_rate_pct']}% "
            f"pnl=${float(r['total_pnl']):,.0f} calmar={r['calmar']}"
        )

run_phase(20, f"3D: VIX × Width × TS ({len(_vix3_levels)}×{len(_w_levels)}×{len(_ts2_levels)}={len(_vix3_levels)*len(_w_levels)*len(_ts2_levels)} combos)",
          _run_3d_vix_width_ts)

# ── Phase 21: MEGA 5D Combo ───────────────────────────────────────────────
_mega_vix    = [12, 14, 14.5, 15, 16, 18, None]           # 7
_mega_width  = [100, 150, 175, 200, 225, 250, 300]          # 7
_mega_sl     = [-750, -1200, -2000, None]                   # 4
_mega_credit = [0.40, 0.50, 0.55, 0.60, 0.70]              # 5
_mega_ts     = [300, 500, 700, None]                        # 4
_mega_combos = len(_mega_vix) * len(_mega_width) * len(_mega_sl) * len(_mega_credit) * len(_mega_ts)
_out_21 = os.path.join(mod.LOGS_DIR, f"sweep_mega_5d_{_ts}.csv")

run_phase(21, f"MEGA 5D Combo ({_mega_combos:,} combos: VIX×Width×SL×Credit×TS)",
          lambda: _run_mega_combo(_mega_vix, _mega_width, _mega_sl,
                                  _mega_credit, _mega_ts, _out_21))


# ── Phase 22: Auto-extend if finished early ───────────────────────────────
_elapsed_so_far = time.time() - plan_start
_target_seconds = 8 * 3600   # 8 hours
_remaining      = _target_seconds - _elapsed_so_far

if _remaining > 3600:  # more than 1 hour left — run extra phases
    print(f"\n  [Auto-extend] {_remaining/3600:.1f}h remaining — adding extra phases")

    # Extra A: even denser EMA sweep (20×20 = 400 combos, all valid fast < slow)
    run_phase("22a", "EMA Sweep Extended (20×20 combos)", mod.run_ema_sweep,
              overrides={
                  "RUN_EMA_SWEEP": True,
                  "VIX_MAX_FILTER": None,
                  "VIX_MIN_FILTER": None,
                  "SWEEP_EMA_FAST": list(range(5, 25)),   # 5–24
                  "SWEEP_EMA_SLOW": list(range(15, 75, 3)),  # 15,18,21,...,72
              })

    # Extra B: Mega combo round 2 with more TS granularity
    _mega2_ts     = [100, 200, 300, 400, 500, 600, 700, 800, None]  # 9
    _mega2_combos = len(_mega_vix) * len(_mega_width) * len(_mega_sl) * len(_mega_credit) * len(_mega2_ts)
    _out_22b = os.path.join(mod.LOGS_DIR, f"sweep_mega_5d_r2_{_ts}.csv")
    run_phase("22b", f"MEGA 5D Combo Round 2 ({_mega2_combos:,} combos, denser TS)",
              lambda: _run_mega_combo(_mega_vix, _mega_width, _mega_sl,
                                      _mega_credit, _mega2_ts, _out_22b))


# ═════════════════════════════════════════════════════════════════════════════
#  FINAL SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
total_elapsed = time.time() - plan_start
print(f"\n{'='*60}")
print("  BACKTEST PLAN COMPLETE")
print(f"  Total elapsed: {total_elapsed/3600:.2f} hours ({total_elapsed/60:.1f} min)")
print(f"{'='*60}")
print(f"\n{'Phase':>6}  {'Status':<8}  {'Elapsed':<10}  Name")
print("-"*70)
for p in PHASE_LOG:
    err = f"  ← {p['error'][:50]}" if p["error"] else ""
    print(f"  {str(p['phase']):>4}  {p['status']:<8}  {p['elapsed']:<10}  {p['name']}{err}")

print(f"\nAll outputs saved to: {mod.LOGS_DIR}/")
print(f"Timestamp prefix   : {mod._RUN_TS}")

# ── Quick ranking: best combos from mega sweep ───────────────────────────
for combo_file in [_out_21, _latest_file("logs/sweep_mega_5d_*.csv")]:
    if not combo_file or not os.path.exists(combo_file):
        continue
    try:
        with open(combo_file) as f:
            rows = list(csv.DictReader(f))
        rows = [r for r in rows if int(r.get("num_trades", 0) or 0) >= 30]
        rows_sorted = sorted(rows, key=lambda r: float(r.get("calmar") or 0), reverse=True)
        print(f"\nTop 10 combos by Calmar ({os.path.basename(combo_file)}):")
        print(f"{'Rank':>4}  {'Calmar':>7}  {'P&L':>10}  {'Max DD':>10}  {'Trades':>6}  Params")
        print("-"*80)
        for i, r in enumerate(rows_sorted[:10], 1):
            params = " ".join(f"{k}={r[k]}" for k in ["vix_max", "width", "daily_sl",
                                                        "min_credit", "trailing_stop"]
                              if k in r)
            print(f"  {i:>3}  {float(r['calmar']):>7.2f}  "
                  f"${float(r['total_pnl']):>9,.0f}  "
                  f"${float(r['max_drawdown']):>9,.0f}  "
                  f"{r['num_trades']:>6}  {params}")
        break
    except Exception as e:
        print(f"  [warn] Could not parse combo output: {e}")

print("\nDone.")
