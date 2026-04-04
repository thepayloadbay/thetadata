#!/usr/bin/env python3
"""Research: HYBRID Loss Mitigation — combine offset entry, max loss budget, and sizing caps.

NOTE: SPX option strikes are on a $5 grid, so minimum spread width is $5.

Tests the BEST COMBINATION of realistic loss mitigation techniques:

  A: Current baseline — width=5, tiered sizing, no offset, no caps
  B: Offset entry only — width=5, tiered sizing, offset reduction at 15:56
  C: Max loss budget $1500 + offset — width=5, qty capped at floor(1500/(5*100))=3, offset
  D: Max loss $1500 + ATM risk cap $2000 + offset — width=5, tiered but ATM capped, offset
  E: Width=10 ATM + width=5 OTM + offset — wider spreads at ATM for fewer contracts
  F: All combined: w10 ATM, w5 OTM, max loss $2000, offset

All: Rule C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00,
     min credit $0.10, commission $0.50/leg.
     Call filter: afternoon >= 1.0%. Put filter: afternoon < -0.75% AND mom30 < -0.10%.
     Starting equity $50,000.

Offset entry: both sides enter at 15:55. At 15:56, check SPX. If moved >$3
toward either short strike, reduce that side's qty by 50% (cost = buying back
at 15:56 ask price for the contracts being unwound).
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

MIN_CREDIT = 0.10
STRIKE_STEP = 5
STARTING_EQUITY = 50_000.0
ANNUAL_RF = 0.05
COMMISSION = 0.50  # per leg per contract

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
    rows = df[df["time_str"] == time_str]
    if rows.empty:
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
    return (end - start) / start * 100


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
    return {"bid": float(r["bid"]), "ask": float(r["ask"]),
            "mid": float(r.get("mid", (r["bid"] + r["ask"]) / 2))}


def rule_c3(vix: float) -> float:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Generic spread simulation ──────────────────────────────────────────
def sim_spread(spot: float, spx_exit: float, dist: float,
               quotes_df: pd.DataFrame, entry_time: str,
               right: str, width: float, risk_amount: float,
               max_loss_budget: float | None = None) -> dict | None:
    """Simulate a credit spread (call or put).

    Parameters
    ----------
    width : spread width in dollars
    risk_amount : dollar risk budget for tiered sizing
    max_loss_budget : if set, qty = min(tiered_qty, floor(budget / (width*100)))
    """
    if right == "C":
        target = spot + dist
        short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike + int(width)
    else:  # PUT
        target = spot - dist
        short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike - int(width)

    available = set(quotes_df[quotes_df["right"] == right]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        if right == "C":
            candidates = sorted(s for s in available if s >= spot + dist)
        else:
            candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(width) if right == "C" else short_strike - int(width)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, right, short_strike, entry_time)
    long_q = get_quote(quotes_df, right, long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    max_loss_per = (width - credit) * 100.0
    if max_loss_per <= 0:
        return None

    qty = max(1, int(risk_amount / max_loss_per))

    # Apply max loss budget cap
    if max_loss_budget is not None:
        budget_qty = int(max_loss_budget / (width * 100.0))
        if budget_qty < 1:
            budget_qty = 1
        qty = min(qty, budget_qty)

    # Settlement
    if right == "C":
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = width
        else:
            settlement = spx_exit - short_strike
    else:
        if spx_exit >= short_strike:
            settlement = 0.0
        elif spx_exit <= long_strike:
            settlement = width
        else:
            settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {
        "side": right, "credit": credit, "pnl": net, "qty": qty,
        "short_strike": short_strike, "long_strike": long_strike,
        "width": width, "entry_time": entry_time,
    }


def compute_unwind_cost(quotes_df: pd.DataFrame, right: str,
                        short_strike: int, long_strike: int,
                        unwind_qty: int, time_str: str) -> float:
    """Cost to buy back `unwind_qty` contracts at 15:56 ask prices.

    Returns the NET cost (negative = costs money) to unwind the position.
    We buy back the short leg at ask, sell back the long leg at bid.
    """
    short_q = get_quote(quotes_df, right, short_strike, time_str)
    long_q = get_quote(quotes_df, right, long_strike, time_str)
    if short_q is None or long_q is None:
        return 0.0  # can't get quotes -> assume no unwind possible

    # Cost to buy back short leg (pay ask) minus proceeds from selling long leg (get bid)
    unwind_debit = short_q["ask"] - long_q["bid"]
    cost = unwind_debit * unwind_qty * 100.0
    # Plus commission for 2 legs * unwind_qty
    cost += 2 * unwind_qty * COMMISSION
    return cost


# ── Config A: Current baseline ──────────────────────────────────────────
def tiered_risk_standard(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0



def _sim_with_tiered(spot, spx_exit, dist, opts_df, entry_time, right,
                     width, max_loss_budget, risk_fn=None):
    """Simulate with tiered sizing: first get credit, then size."""
    if risk_fn is None:
        risk_fn = tiered_risk_standard

    if right == "C":
        target = spot + dist
        short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike + int(width)
    else:
        target = spot - dist
        short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike - int(width)

    available = set(opts_df[opts_df["right"] == right]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        if right == "C":
            candidates = sorted(s for s in available if s >= spot + dist)
        else:
            candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(width) if right == "C" else short_strike - int(width)
        if long_strike not in available:
            return None

    short_q = get_quote(opts_df, right, short_strike, entry_time)
    long_q = get_quote(opts_df, right, long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = risk_fn(credit)
    max_loss_per = (width - credit) * 100.0
    if max_loss_per <= 0:
        return None

    qty = max(1, int(risk_amount / max_loss_per))

    if max_loss_budget is not None:
        budget_qty = max(1, int(max_loss_budget / (width * 100.0)))
        qty = min(qty, budget_qty)

    # Settlement
    if right == "C":
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = width
        else:
            settlement = spx_exit - short_strike
    else:
        if spx_exit >= short_strike:
            settlement = 0.0
        elif spx_exit <= long_strike:
            settlement = width
        else:
            settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {
        "side": right, "credit": credit, "pnl": net, "qty": qty,
        "short_strike": short_strike, "long_strike": long_strike,
        "width": width, "entry_time": entry_time,
    }


# ── Offset entry helper ────────────────────────────────────────────────
def apply_offset_reduction(dd, trades_call, trades_put, call_result, put_result):
    """At 15:56, if SPX moved >$3 toward a short strike, reduce that side by 50%.

    Both sides entered at 15:55. At 15:56 we check SPX and potentially unwind
    half the contracts on the threatened side.

    Returns (adjusted_call_pnl, adjusted_put_pnl, was_reduced).
    """
    spot_55 = dd["spot_1555"]
    spot_56 = dd["spot_1556"]

    call_pnl = call_result["pnl"] if call_result else None
    put_pnl = put_result["pnl"] if put_result else None
    was_reduced = False

    if spot_56 is None:
        return call_pnl, put_pnl, was_reduced

    spx_move = spot_56 - spot_55  # positive = up, negative = down

    # SPX moved up >$3 toward call short strike -> reduce call qty by 50%
    if spx_move > 3.0 and call_result is not None:
        orig_qty = call_result["qty"]
        unwind_qty = orig_qty // 2
        if unwind_qty > 0:
            # Cost to unwind at 15:56 prices
            unwind_cost = compute_unwind_cost(
                dd["opts_df"], "C",
                call_result["short_strike"], call_result["long_strike"],
                unwind_qty, "15:56:00"
            )
            # Recompute: keep (orig_qty - unwind_qty) contracts to expiration
            kept_qty = orig_qty - unwind_qty
            width = call_result["width"]
            credit = call_result["credit"]
            spx_exit = dd["spx_exit"]
            short_s = call_result["short_strike"]
            long_s = call_result["long_strike"]

            if spx_exit <= short_s:
                settlement = 0.0
            elif spx_exit >= long_s:
                settlement = width
            else:
                settlement = spx_exit - short_s

            # P&L from kept contracts
            kept_pnl = (credit - settlement) * kept_qty * 100.0 - 4 * kept_qty * COMMISSION
            # P&L from unwound contracts: they collected credit at entry, paid unwind_debit
            # The entry credit for unwound contracts is already booked; unwind cost is additional
            unwound_entry_credit = credit * unwind_qty * 100.0 - 4 * unwind_qty * COMMISSION
            # At 15:56 we buy back the spread for unwind_cost
            # Total for unwound = entry_credit - unwind_cost
            # But unwind_cost already includes commission for the unwind legs
            # Entry comm was 4 legs * COMMISSION per contract (open legs)
            # Actually: entry had 2 legs to open (buy long, sell short) = 2 * COMMISSION * qty
            # We already paid entry commission for all contracts
            # At unwind we pay to close 2 legs = 2 * COMMISSION * unwind_qty
            # Simplify: entry credit for unwound = credit * unwind_qty * 100 - 2*COMMISSION*unwind_qty
            # Unwind cost = debit * unwind_qty * 100 + 2*COMMISSION*unwind_qty
            # Net for unwound portion = credit*unwind_qty*100 - 2*COMM*unwind_qty - unwind_cost

            # Let me redo this cleanly:
            # Original trade: open 2 legs * orig_qty * COMMISSION at entry
            # Kept contracts: settle at expiration -> pay 2 legs * kept_qty * COMMISSION (already counted above with 4 legs... wait)
            # The 4 * qty * COMMISSION in original sim = open(2 legs) + close(2 legs) at expiration
            # For unwound contracts: open(2 legs) at 15:55 + close(2 legs) at 15:56
            # For kept contracts: open(2 legs) at 15:55 + expire(2 legs) at 16:00

            # Redo properly:
            # Kept PnL: (credit - settlement) * kept_qty * 100 - 4 * kept_qty * COMMISSION
            # Unwound PnL: credit * unwind_qty * 100 - unwind_cost
            #   where unwind_cost = debit * unwind_qty * 100 + 2 * unwind_qty * COMMISSION
            #   and we also paid 2 * unwind_qty * COMMISSION to open
            short_q56 = get_quote(dd["opts_df"], "C", short_s, "15:56:00")
            long_q56 = get_quote(dd["opts_df"], "C", long_s, "15:56:00")
            if short_q56 is not None and long_q56 is not None:
                unwind_debit = short_q56["ask"] - long_q56["bid"]
                unwound_pnl = (credit - unwind_debit) * unwind_qty * 100.0 \
                              - 2 * unwind_qty * COMMISSION \
                              - 2 * unwind_qty * COMMISSION  # open + close
                call_pnl = kept_pnl + unwound_pnl
                was_reduced = True

    # SPX moved down >$3 toward put short strike -> reduce put qty by 50%
    if spx_move < -3.0 and put_result is not None:
        orig_qty = put_result["qty"]
        unwind_qty = orig_qty // 2
        if unwind_qty > 0:
            kept_qty = orig_qty - unwind_qty
            width = put_result["width"]
            credit = put_result["credit"]
            spx_exit = dd["spx_exit"]
            short_s = put_result["short_strike"]
            long_s = put_result["long_strike"]

            if spx_exit >= short_s:
                settlement = 0.0
            elif spx_exit <= long_s:
                settlement = width
            else:
                settlement = short_s - spx_exit

            kept_pnl = (credit - settlement) * kept_qty * 100.0 - 4 * kept_qty * COMMISSION

            short_q56 = get_quote(dd["opts_df"], "P", short_s, "15:56:00")
            long_q56 = get_quote(dd["opts_df"], "P", long_s, "15:56:00")
            if short_q56 is not None and long_q56 is not None:
                unwind_debit = short_q56["ask"] - long_q56["bid"]
                unwound_pnl = (credit - unwind_debit) * unwind_qty * 100.0 \
                              - 2 * unwind_qty * COMMISSION \
                              - 2 * unwind_qty * COMMISSION
                put_pnl = kept_pnl + unwound_pnl
                was_reduced = True

    return call_pnl, put_pnl, was_reduced


# ── Config runners ────────────────────────────────────────────────────
def _run_generic(day_data, label, width_fn, risk_fn, max_loss_budget_fn, use_offset):
    """Generic runner for all configs.

    width_fn(dist) -> width in dollars (must be multiple of 5)
    risk_fn(credit) -> risk budget in dollars
    max_loss_budget_fn(width) -> max loss budget or None
    use_offset: bool — apply 15:56 offset reduction
    """
    trades = []
    day_pnls = []
    reductions = 0

    for dd in day_data:
        dist = dd["dist"]
        spot = dd["spot_1555"]
        width = width_fn(dist)
        budget = max_loss_budget_fn(width)
        day_pnl = 0.0
        traded = False

        call_result = None
        put_result = None

        if not dd["skip_call"]:
            call_result = _sim_with_tiered(spot, dd["spx_exit"], dist, dd["opts_df"],
                                            "15:55:00", "C", width, budget,
                                            risk_fn=risk_fn)
        if not dd["skip_put"]:
            put_result = _sim_with_tiered(spot, dd["spx_exit"], dist, dd["opts_df"],
                                           "15:55:00", "P", width, budget,
                                           risk_fn=risk_fn)

        if use_offset:
            adj_call, adj_put, reduced = apply_offset_reduction(
                dd, trades, trades, call_result, put_result)
            if reduced:
                reductions += 1
        else:
            adj_call = call_result["pnl"] if call_result else None
            adj_put = put_result["pnl"] if put_result else None

        if call_result is not None:
            t = dict(call_result)
            t["pnl"] = adj_call if adj_call is not None else call_result["pnl"]
            trades.append(t)
            day_pnl += t["pnl"]
            traded = True
        if put_result is not None:
            t = dict(put_result)
            t["pnl"] = adj_put if adj_put is not None else put_result["pnl"]
            trades.append(t)
            day_pnl += t["pnl"]
            traded = True

        if traded:
            day_pnls.append({"date": dd["date"], "pnl": day_pnl})

    result = _summarize(label, trades, day_pnls)
    result["reductions"] = reductions
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

    call_trades = [t for t in trades if t["side"] == "C"]
    put_trades = [t for t in trades if t["side"] == "P"]

    # Worst trade
    worst_trade = min(t["pnl"] for t in trades)

    # Sharpe
    daily_rf = ANNUAL_RF / 252
    excess = dpnl_arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / dpnl_arr.std() * np.sqrt(252) if dpnl_arr.std() > 0 else 0

    # Calmar
    n_days = len(dpnl_arr)
    years = n_days / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    # Profit factor
    trade_pnls = np.array([t["pnl"] for t in trades])
    gross_profit = trade_pnls[trade_pnls > 0].sum()
    gross_loss = abs(trade_pnls[trade_pnls < 0].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

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
        "pf": pf,
        "n_days": n_days,
        "worst_day_pnl": worst_day_pnl,
        "worst_day_date": worst_day_date,
        "worst_trade": worst_trade,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 130)
    print("HYBRID LOSS MITIGATION RESEARCH")
    print("Combining: narrow width + offset entry reduction + max loss budget")
    print("=" * 130)
    print("Base: Rule C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00")
    print("      Min credit $0.10, commission $0.50/leg, starting equity $50k")
    print("      Call filter: afternoon >= 1.0%. Put filter: aftn < -0.75% AND mom30 < -0.10%")
    print()
    print("Configs (all widths are multiples of $5 to match strike grid):")
    print("  A: Baseline — width=5, tiered sizing, no offset, no caps")
    print("  B: Offset entry only — width=5, tiered sizing, reduce@15:56 if SPX>$3")
    print("  C: Max loss budget $1500 + offset — qty capped at floor(1500/(w*100))")
    print("  D: Cap $1500 + ATM risk cap $2000 + offset")
    print("  E: Width=10 ATM + width=5 OTM + offset (fewer contracts at ATM)")
    print("  F: Width=10 ATM + width=5 OTM + max loss $2000 + offset")
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

        dist = rule_c3(vix1550)

        # Afternoon return
        afternoon_ret = compute_afternoon_return(spx_df)

        # Call filter: skip if afternoon >= 1.0%
        skip_call = (afternoon_ret is not None and afternoon_ret >= 1.0)

        # Put filter: skip put if afternoon < -0.75% AND mom30 < -0.10%
        mom30 = compute_mom30(spx_df)
        skip_put = False
        if (afternoon_ret is not None and afternoon_ret < -0.75
                and mom30 is not None and mom30 < -0.10):
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

        # Skip entire day if call filter fires (no call side) — but put can still trade
        # Actually, per baseline: skip_call only skips call, skip_put only skips put
        # If BOTH are skipped, still include day (just no trades that day)

        day_data.append({
            "date": ds,
            "spot_1555": spot_1555,
            "spot_1556": spot_1556,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "dist": dist,
            "afternoon_ret": afternoon_ret,
            "mom30": mom30,
            "skip_call": skip_call,
            "skip_put": skip_put,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Define configs ──────────────────────────────────────────────
    # Width functions (must return multiples of 5 for the $5 strike grid)
    w5_always = lambda dist: 5.0
    w10_atm_w5_otm = lambda dist: 10.0 if dist == 0 else 5.0

    # Risk functions
    tiered_std = tiered_risk_standard  # cr<0.20->$1500, 0.20-0.50->$2500, >0.50->$3500
    atm_cap_2000 = lambda cr: 2000.0 if cr >= 0.50 else tiered_risk_standard(cr)
    # For ATM cap variant: dist=0 always has high credit, so cap at $2000

    # Max loss budget functions
    no_budget = lambda w: None
    budget_1500 = lambda w: 1500.0
    budget_2000 = lambda w: 2000.0

    configs = [
        ("A: Baseline w5",         w5_always,       tiered_std, no_budget,   False),
        ("B: Offset only",         w5_always,       tiered_std, no_budget,   True),
        ("C: Cap$1500+offset",     w5_always,       tiered_std, budget_1500, True),
        ("D: Cap$1500+ATMcap+off", w5_always,       atm_cap_2000, budget_1500, True),
        ("E: w10ATM+w5OTM+off",    w10_atm_w5_otm,  tiered_std, no_budget,   True),
        ("F: w10ATM+cap$2k+off",   w10_atm_w5_otm,  tiered_std, budget_2000, True),
    ]

    # ── Run all configs ──────────────────────────────────────────────
    print("\n--- Running configs ---")
    results = []
    for label, wfn, rfn, bfn, use_off in configs:
        print(f"  Running {label}...")
        results.append(_run_generic(day_data, label, wfn, rfn, bfn, use_off))

    # ── Comparison table ──────────────────────────────────────────────
    print("\n" + "=" * 160)
    print("COMPARISON TABLE — Hybrid Loss Mitigation")
    print("=" * 160)
    print(f"{'Config':<22} {'Trades':>7} {'C/P':>9} {'P&L':>12} {'Max DD':>10} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'PF':>6} "
          f"{'Worst Day':>11} {'Worst Trd':>10} {'Days':>5} {'Offsets':>8}")
    print("-" * 160)

    for r in results:
        if r["trades"] == 0:
            print(f"{r['label']:<22}  NO TRADES")
            continue
        offsets = r.get("reductions", "-")
        print(
            f"{r['label']:<22} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>5.1f}% "
            f"{r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} "
            f"${r['worst_day_pnl']:>9,.0f} "
            f"${r['worst_trade']:>8,.0f} "
            f"{r['n_days']:>5} "
            f"{offsets!s:>8}"
        )
    print("=" * 160)

    # ── Delta vs baseline ──────────────────────────────────────────────
    base = results[0]
    if base["trades"] > 0:
        print(f"\nDelta vs A (baseline):")
        print("-" * 130)
        print(f"{'Config':<22} {'dP&L':>12} {'dMax DD':>10} {'dSharpe':>8} {'dCalmar':>9} "
              f"{'dPF':>7} {'dWrstDay':>10} {'dWrstTrd':>10} {'dTrades':>8}")
        print("-" * 130)
        for r in results[1:]:
            if r["trades"] == 0:
                continue
            dpnl = r["total_pnl"] - base["total_pnl"]
            ddd = r["max_dd"] - base["max_dd"]
            dsh = r["sharpe"] - base["sharpe"]
            dcal = r["calmar"] - base["calmar"]
            dpf = r["pf"] - base["pf"]
            dwd = r["worst_day_pnl"] - base["worst_day_pnl"]
            dwt = r["worst_trade"] - base["worst_trade"]
            dtr = r["trades"] - base["trades"]
            print(
                f"  {r['label']:<20} "
                f"${dpnl:>+10,.0f} ${ddd:>+8,.0f} "
                f"{dsh:>+8.2f} {dcal:>+9.2f} "
                f"{dpf:>+7.2f} "
                f"${dwd:>+8,.0f} ${dwt:>+8,.0f} "
                f"{dtr:>+8}"
            )
        print("-" * 130)

    # ── Worst day detail ──────────────────────────────────────────────
    print(f"\nWorst day details:")
    print("-" * 60)
    for r in results:
        if r["trades"] == 0:
            continue
        wd = r.get("worst_day_date", "?")
        wp = r.get("worst_day_pnl", 0)
        print(f"  {r['label']:<22} {wd}  ${wp:>+10,.0f}")

    # ── Risk-return efficiency ────────────────────────────────────────
    print(f"\nRisk-Return Efficiency (P&L per $1 of max DD):")
    print("-" * 70)
    for r in results:
        if r["trades"] == 0 or r["max_dd"] == 0:
            continue
        efficiency = r["total_pnl"] / abs(r["max_dd"])
        pnl_per_trade = r["total_pnl"] / r["trades"]
        print(f"  {r['label']:<22} P&L/DD: {efficiency:>6.2f}x   "
              f"P&L/trade: ${pnl_per_trade:>7,.0f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
