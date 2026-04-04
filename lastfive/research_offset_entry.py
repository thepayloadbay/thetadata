#!/usr/bin/env python3
"""Research: Staggered Call/Put Entry Times — 15:55 vs 15:56 offset.

Hypothesis: Entering one side 1 minute later lets us observe SPX movement
and skip the second side if the market moves fast against it.

Configs tested (all Rule C3 VIX-adaptive, bidirectional, width $5,
min credit $0.10, commission $0.50/leg, tiered sizing, $50k starting equity):

A: Both at 15:55 (baseline)
B: Call 15:55, put 15:56. Skip put if |SPX(15:56) - SPX(15:55)| > $3
C: Call 15:55, put 15:56. Skip put if |move| > $2
D: Call 15:55, put 15:56. Skip put if |move| > $5
E: Put 15:55, call 15:56. Skip call if |move| > $3
F: Both at 15:55 conceptually, but at 15:56 if SPX moved >$3 toward
   either short strike, reduce that side's qty by 50%.
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
STRIKE_STEP = 5
STARTING_EQUITY = 50_000.0
ANNUAL_RF = 0.05
COMMISSION = 0.50  # per leg per contract

# Tiered sizing
TIER_LOW_CREDIT = 0.20
TIER_HIGH_CREDIT = 0.50
RISK_LOW = 1500.0
RISK_MID = 2500.0
RISK_HIGH = 3500.0

# Put filter
PUT_AFTERNOON_MIN = -0.75
PUT_MOM30_MIN = -0.10

MARKET_HOLIDAYS = {
    "20220117","20220221","20220415","20220530","20220620","20220704",
    "20220905","20221124","20221226",
    "20230102","20230116","20230220","20230407","20230529","20230619",
    "20230704","20230904","20231123","20231225",
    "20240101","20240115","20240219","20240329","20240527","20240619",
    "20240704","20240902","20241128","20241225",
    "20250101","20250120","20250217","20250418","20250526","20250704",
    "20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260703",
    "20260907","20261126","20261225",
}

EARLY_CLOSE_DAYS = {
    "20221125","20231124","20241129","20241224",
    "20251128","20251224","20261127","20261224",
}


# ── Data loaders ────────────────────────────────────────────────────────
def load_spx(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def load_vix(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "vix_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def load_options(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    return df


# ── Helpers ─────────────────────────────────────────────────────────────
def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def get_price_at_time(df: pd.DataFrame, time_str: str) -> float | None:
    """Get SPX close at exact time_str like '15:56:00'."""
    rows = df[df["time_str"] == time_str]
    if rows.empty:
        # fallback to hhmm
        hhmm = time_str[:5]
        rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100  # signed


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    start = float(bars["close"].iloc[0])
    end = float(bars["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def get_quote(quotes_df: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    mask = (
        (quotes_df["right"] == right) &
        (quotes_df["strike"] == strike) &
        (quotes_df["time_str"] == time_str)
    )
    rows = quotes_df[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


def rule_c3(vix: float) -> float:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


def tiered_risk(credit: float) -> float:
    if credit < TIER_LOW_CREDIT:
        return RISK_LOW
    elif credit > TIER_HIGH_CREDIT:
        return RISK_HIGH
    else:
        return RISK_MID


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             qty_mult: float = 1.0) -> dict | None:
    """Simulate a call credit spread. qty_mult scales qty (e.g., 0.5 for half size)."""
    target = spot + dist
    short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike + int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "C"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted(s for s in available if s >= spot + dist)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "C", short_strike, entry_time)
    long_q = get_quote(quotes_df, "C", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk = tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1
    if qty_mult < 1.0:
        qty = max(1, int(qty * qty_mult))

    if spx_exit <= short_strike:
        settlement = 0.0
    elif spx_exit >= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = spx_exit - short_strike

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "CALL", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "entry_time": entry_time}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str,
            qty_mult: float = 1.0) -> dict | None:
    """Simulate a put credit spread. qty_mult scales qty (e.g., 0.5 for half size)."""
    target = spot - dist
    short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike - int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "P"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike - int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "P", short_strike, entry_time)
    long_q = get_quote(quotes_df, "P", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk = tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1
    if qty_mult < 1.0:
        qty = max(1, int(qty * qty_mult))

    if spx_exit >= short_strike:
        settlement = 0.0
    elif spx_exit <= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "PUT", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "entry_time": entry_time}


# ── Config runners ────────────────────────────────────────────────────
def run_config_A(day_data: list) -> dict:
    """A: Both sides at 15:55 (baseline)."""
    trades = []
    day_pnls = []

    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        spot = dd["spot_1555"]
        if spot is None:
            continue
        day_pnl = 0.0
        traded = False

        # Call side: afternoon >= 1.0% already filtered at day level
        ct = sim_call(spot, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
        if ct is not None:
            trades.append(ct)
            day_pnl += ct["pnl"]
            traded = True

        # Put side with filter
        if not dd["skip_put"]:
            pt = sim_put(spot, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if pt is not None:
                trades.append(pt)
                day_pnl += pt["pnl"]
                traded = True

        if traded:
            day_pnls.append({"date": dd["date"], "pnl": day_pnl})

    return _summarize("A: Both@15:55 (baseline)", trades, day_pnls)


def run_config_BCD(day_data: list, threshold: float, label: str) -> dict:
    """B/C/D: Call at 15:55, put at 15:56. Skip put if |move| > threshold."""
    trades = []
    day_pnls = []
    skipped_by_gate = 0

    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        spot_55 = dd["spot_1555"]
        spot_56 = dd["spot_1556"]
        if spot_55 is None:
            continue
        day_pnl = 0.0
        traded = False

        # Call side at 15:55
        ct = sim_call(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
        if ct is not None:
            trades.append(ct)
            day_pnl += ct["pnl"]
            traded = True

        # Put side at 15:56 — gated by 1-min SPX move
        if not dd["skip_put"]:
            if spot_56 is not None:
                move = abs(spot_56 - spot_55)
                if move > threshold:
                    skipped_by_gate += 1
                else:
                    # Use 15:56 spot for strike calc, 15:56 quotes for credit
                    pt = sim_put(spot_56, dd["spx_exit"], dist, dd["opts_df"], "15:56:00")
                    if pt is not None:
                        trades.append(pt)
                        day_pnl += pt["pnl"]
                        traded = True
            else:
                # No 15:56 bar — fall back to 15:55
                pt = sim_put(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt is not None:
                    trades.append(pt)
                    day_pnl += pt["pnl"]
                    traded = True

        if traded:
            day_pnls.append({"date": dd["date"], "pnl": day_pnl})

    result = _summarize(label, trades, day_pnls)
    result["skipped_by_gate"] = skipped_by_gate
    return result


def run_config_E(day_data: list, threshold: float) -> dict:
    """E: Put at 15:55, call at 15:56. Skip call if |move| > threshold."""
    trades = []
    day_pnls = []
    skipped_by_gate = 0

    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        spot_55 = dd["spot_1555"]
        spot_56 = dd["spot_1556"]
        if spot_55 is None:
            continue
        day_pnl = 0.0
        traded = False

        # Put side at 15:55 (with put filter)
        if not dd["skip_put"]:
            pt = sim_put(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if pt is not None:
                trades.append(pt)
                day_pnl += pt["pnl"]
                traded = True

        # Call side at 15:56 — gated
        if spot_56 is not None:
            move = abs(spot_56 - spot_55)
            if move > threshold:
                skipped_by_gate += 1
            else:
                ct = sim_call(spot_56, dd["spx_exit"], dist, dd["opts_df"], "15:56:00")
                if ct is not None:
                    trades.append(ct)
                    day_pnl += ct["pnl"]
                    traded = True
        else:
            # No 15:56 bar — fall back to 15:55
            ct = sim_call(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if ct is not None:
                trades.append(ct)
                day_pnl += ct["pnl"]
                traded = True

        if traded:
            day_pnls.append({"date": dd["date"], "pnl": day_pnl})

    result = _summarize("E: Put@55,Call@56,gate$3", trades, day_pnls)
    result["skipped_by_gate"] = skipped_by_gate
    return result


def run_config_F(day_data: list) -> dict:
    """F: Both at 15:55, but at 15:56 reduce qty 50% on side where SPX moved >$3 toward short strike."""
    trades = []
    day_pnls = []
    reduced_count = 0

    for dd in day_data:
        dist = rule_c3(dd["vix1550"])
        spot_55 = dd["spot_1555"]
        spot_56 = dd["spot_1556"]
        if spot_55 is None:
            continue
        day_pnl = 0.0
        traded = False

        # Determine qty multipliers based on 15:56 movement
        call_mult = 1.0
        put_mult = 1.0

        if spot_56 is not None:
            spx_move = spot_56 - spot_55  # positive = up (toward call short), negative = down (toward put short)
            if spx_move > 3.0:
                # SPX moved up toward call short strike — reduce call qty
                call_mult = 0.5
                reduced_count += 1
            elif spx_move < -3.0:
                # SPX moved down toward put short strike — reduce put qty
                put_mult = 0.5
                reduced_count += 1

        # Call side at 15:55, possibly reduced
        ct = sim_call(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00",
                       qty_mult=call_mult)
        if ct is not None:
            trades.append(ct)
            day_pnl += ct["pnl"]
            traded = True

        # Put side at 15:55, possibly reduced (with put filter)
        if not dd["skip_put"]:
            pt = sim_put(spot_55, dd["spx_exit"], dist, dd["opts_df"], "15:55:00",
                          qty_mult=put_mult)
            if pt is not None:
                trades.append(pt)
                day_pnl += pt["pnl"]
                traded = True

        if traded:
            day_pnls.append({"date": dd["date"], "pnl": day_pnl})

    result = _summarize("F: Both@55,reduce@56>$3", trades, day_pnls)
    result["reduced_count"] = reduced_count
    return result


# ── Summarize results ────────────────────────────────────────────────
def _summarize(label: str, trades: list, day_pnls: list) -> dict:
    if not trades or not day_pnls:
        return {"label": label, "trades": 0}

    dpnl_arr = np.array([d["pnl"] for d in day_pnls])
    total_pnl = dpnl_arr.sum()

    # Max drawdown
    equity = np.cumsum(dpnl_arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd_arr = equity - peak
    max_dd = dd_arr.min()

    # Worst day
    worst_day_pnl = dpnl_arr.min()
    worst_day_idx = int(np.argmin(dpnl_arr))
    worst_day_date = day_pnls[worst_day_idx]["date"]

    n_trades = len(trades)
    n_wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = n_wins / n_trades * 100

    call_trades = [t for t in trades if t["side"] == "CALL"]
    put_trades = [t for t in trades if t["side"] == "PUT"]

    # Sharpe
    daily_rf = ANNUAL_RF / 252
    excess = dpnl_arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / dpnl_arr.std() * np.sqrt(252) if dpnl_arr.std() > 0 else 0

    # Calmar
    n_days = len(dpnl_arr)
    years = n_days / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    return {
        "label": label,
        "trades": n_trades,
        "n_calls": len(call_trades),
        "n_puts": len(put_trades),
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "sharpe": sharpe,
        "calmar": calmar,
        "n_days": n_days,
        "worst_day_pnl": worst_day_pnl,
        "worst_day_date": worst_day_date,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("OFFSET ENTRY RESEARCH — Staggered Call/Put Entry at 15:55 vs 15:56")
    print("=" * 120)
    print("Base: Rule C3 VIX-adaptive, bidirectional, $5 width, min credit $0.10,")
    print("      commission $0.50/leg, tiered sizing, put filter (aftn<-0.75% AND mom30<-0.10%)")
    print()

    # Collect all dates
    print("Loading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    # Pre-collect day data
    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_filter": 0, "afternoon_filter": 0}

    for i, ds in enumerate(all_dates):
        if i % 200 == 0:
            print(f"  Processing {i}/{len(all_dates)}...")

        if ds in MARKET_HOLIDAYS:
            skipped["holiday"] += 1
            continue
        if ds in EARLY_CLOSE_DAYS:
            skipped["early_close"] += 1
            continue

        spx_df = load_spx(ds)
        if spx_df is None:
            skipped["no_spx"] += 1
            continue

        vix_df = load_vix(ds)
        if vix_df is None:
            skipped["no_vix"] += 1
            continue

        vix1550 = get_price_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0:
            skipped["no_vix"] += 1
            continue

        # VIX > 26 skip
        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        # Afternoon return — compute signed version
        afternoon_ret = compute_afternoon_return(spx_df)

        # Call filter: skip day if afternoon >= 1.0%
        if afternoon_ret is not None and afternoon_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # Put filter: skip put if afternoon < -0.75% AND mom30 < -0.10%
        mom30 = compute_mom30(spx_df)
        skip_put = False
        if (afternoon_ret is not None and afternoon_ret < PUT_AFTERNOON_MIN
                and mom30 is not None and mom30 < PUT_MOM30_MIN):
            skip_put = True

        # Spot at entry times
        spot_1555 = get_price_at_time(spx_df, "15:55:00")
        spot_1556 = get_price_at_time(spx_df, "15:56:00")

        if spot_1555 is None:
            skipped["no_spx"] += 1
            continue

        # SPX at 16:00
        exit_row = spx_df[spx_df["hhmm"] == "16:00"]
        if exit_row.empty:
            exit_row = spx_df[spx_df["hhmm"] == "15:59"]
        if exit_row.empty:
            spx_exit = float(spx_df["close"].iloc[-1])
        else:
            spx_exit = float(exit_row["close"].iloc[0])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        day_data.append({
            "date": ds,
            "spot_1555": spot_1555,
            "spot_1556": spot_1556,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "afternoon_ret": afternoon_ret,
            "mom30": mom30,
            "skip_put": skip_put,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # Stats on 15:55->15:56 movement
    moves = []
    for dd in day_data:
        if dd["spot_1555"] is not None and dd["spot_1556"] is not None:
            moves.append(dd["spot_1556"] - dd["spot_1555"])
    moves_arr = np.array(moves) if moves else np.array([0])
    abs_moves = np.abs(moves_arr)

    print(f"\n  SPX move 15:55 -> 15:56 ({len(moves)} days):")
    print(f"    Mean abs: ${abs_moves.mean():.2f}  Median abs: ${np.median(abs_moves):.2f}")
    print(f"    Std: ${moves_arr.std():.2f}  Min: ${moves_arr.min():+.2f}  Max: ${moves_arr.max():+.2f}")
    for thresh in [2, 3, 5]:
        cnt = sum(1 for m in abs_moves if m > thresh)
        pct = cnt / len(abs_moves) * 100
        print(f"    Days |move| > ${thresh}: {cnt} ({pct:.1f}%)")

    n_no_56 = sum(1 for dd in day_data if dd["spot_1556"] is None)
    print(f"    Days without 15:56 bar: {n_no_56}")

    # ── Run all configs ──────────────────────────────────────────────
    print("\n--- Running configs ---")

    results = []

    print("  Running A: Both at 15:55 (baseline)...")
    results.append(run_config_A(day_data))

    print("  Running B: Call@15:55, Put@15:56, gate $3...")
    results.append(run_config_BCD(day_data, threshold=3.0, label="B: C@55,P@56,gate$3"))

    print("  Running C: Call@15:55, Put@15:56, gate $2...")
    results.append(run_config_BCD(day_data, threshold=2.0, label="C: C@55,P@56,gate$2"))

    print("  Running D: Call@15:55, Put@15:56, gate $5...")
    results.append(run_config_BCD(day_data, threshold=5.0, label="D: C@55,P@56,gate$5"))

    print("  Running E: Put@15:55, Call@15:56, gate $3...")
    results.append(run_config_E(day_data, threshold=3.0))

    print("  Running F: Both@15:55, reduce losing side at 15:56...")
    results.append(run_config_F(day_data))

    # ── Comparison table ──────────────────────────────────────────────
    print("\n" + "=" * 140)
    print("COMPARISON TABLE — Offset Entry Research")
    print("=" * 140)
    print(f"{'Config':<28} {'Trades':>7} {'C/P':>9} {'P&L':>12} {'Max DD':>10} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'Worst Day':>12} {'Days':>5} {'Gate Skip':>10}")
    print("-" * 140)

    for r in results:
        if r["trades"] == 0:
            print(f"{r['label']:<28}  NO TRADES")
            continue
        gate_skip = r.get("skipped_by_gate", r.get("reduced_count", "-"))
        worst = f"${r['worst_day_pnl']:,.0f}" if r.get("worst_day_pnl") is not None else "-"
        print(
            f"{r['label']:<28} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>5.1f}% "
            f"{r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"{worst:>12} {r['n_days']:>5} "
            f"{gate_skip!s:>10}"
        )
    print("=" * 140)

    # ── Delta vs baseline ──────────────────────────────────────────────
    base = results[0]
    if base["trades"] > 0:
        print(f"\nDelta vs A (baseline):")
        print("-" * 110)
        print(f"{'Config':<28} {'dP&L':>12} {'dMax DD':>10} {'dSharpe':>8} {'dCalmar':>9} {'dTrades':>8} {'dPuts':>7}")
        print("-" * 110)
        for r in results[1:]:
            if r["trades"] == 0:
                continue
            dpnl = r["total_pnl"] - base["total_pnl"]
            ddd = r["max_dd"] - base["max_dd"]
            dsh = r["sharpe"] - base["sharpe"]
            dcal = r["calmar"] - base["calmar"]
            dtr = r["trades"] - base["trades"]
            dput = r["n_puts"] - base["n_puts"]
            print(
                f"  {r['label']:<26} "
                f"${dpnl:>+10,.0f} ${ddd:>+8,.0f} "
                f"{dsh:>+8.2f} {dcal:>+9.2f} {dtr:>+8} {dput:>+7}"
            )
        print("-" * 110)

    # ── Worst day detail ──────────────────────────────────────────────
    print(f"\nWorst day details:")
    print("-" * 80)
    for r in results:
        if r["trades"] == 0:
            continue
        wd = r.get("worst_day_date", "?")
        wp = r.get("worst_day_pnl", 0)
        print(f"  {r['label']:<28} {wd}  ${wp:>+10,.0f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
