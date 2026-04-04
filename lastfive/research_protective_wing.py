"""Research: Protective Stop-Loss Using Real 15:56/15:57 Option Prices.

At 15:55, sell the credit spread as normal. Then at 15:56 (or 15:57),
check if SPX has moved toward the short strike. If the move exceeds
a threshold, buy back the losing spread at real ask prices to cap loss.

Configs tested:
  A: No protection (baseline) — hold to settlement
  B: Stop at 15:56 if SPX moved >$2 toward short strike
  C: Stop at 15:56 if SPX moved >$3 toward short strike
  D: Stop at 15:57 if SPX moved >$3 toward short strike
  E: Stop at 15:56 AND 15:57, $2 threshold each check
  F: Stop at 15:56 if SPX moved >$3, but only close HALF the position

Buy-back cost = (short_ask - long_bid) at the check time * qty * 100.
Net P&L on stopped trade = (credit - close_cost) * qty * 100 - commissions.
If not stopped, hold to settlement as normal.

Base: Rule C3 VIX-adaptive, bidirectional, width $5, min credit $0.10,
tiered sizing, entry 15:55, commission $0.50/leg, starting equity $50k.
"""
from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
COMMISSION = 0.50
STRIKE_STEP = 5
STARTING_EQUITY = 50_000.0
ANNUAL_RF = 0.05

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
def spx_at(df: pd.DataFrame, time_str: str) -> float | None:
    rows = df[df["time_str"] == time_str]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def afternoon_return(spx_df: pd.DataFrame) -> float | None:
    af = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(af) < 2:
        return None
    s, e = float(af["close"].iloc[0]), float(af["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


def mom_30(spx_df: pd.DataFrame) -> float | None:
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    s, e = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


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
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float | None:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    elif vix <= 26:
        return 5.0
    else:
        return None


# ── Tiered sizing ──────────────────────────────────────────────────────
def tiered_sizing(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── Trade result dataclass ─────────────────────────────────────────────
@dataclass
class TradeResult:
    date: str
    side: str  # "CALL" or "PUT"
    short_strike: int
    long_strike: int
    qty: int
    credit: float
    settlement_pnl: float  # P&L if held to expiration (no stop)
    # For stop-loss evaluation:
    spx_entry: float       # SPX at 15:55
    spx_1556: float | None # SPX at 15:56
    spx_1557: float | None # SPX at 15:57
    # Buy-back costs at 15:56 and 15:57 (short_ask - long_bid)
    close_cost_1556: float | None  # cost per contract to close at 15:56
    close_cost_1557: float | None  # cost per contract to close at 15:57


# ── Simulate one side (collect all info needed for stop-loss analysis) ──
def sim_side(side: str, spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, spx_df: pd.DataFrame,
             date_str: str) -> TradeResult | None:
    """Simulate one side and collect stop-loss data."""
    right = "C" if side == "CALL" else "P"

    # Strike selection
    if side == "CALL":
        if dist == 0:
            short_strike = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
        else:
            short_strike = int(math.ceil((spot + dist) / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike + int(SPREAD_WIDTH)
    else:
        if dist == 0:
            short_strike = int(math.floor(spot / STRIKE_STEP) * STRIKE_STEP)
        else:
            short_strike = int(math.floor((spot - dist) / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike - int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == right]["strike"].unique())

    if short_strike not in available or long_strike not in available:
        if side == "CALL":
            candidates = sorted(s for s in available if s >= spot + dist)
        else:
            candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        if side == "CALL":
            long_strike = short_strike + int(SPREAD_WIDTH)
        else:
            long_strike = short_strike - int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    # Entry quotes at 15:55
    short_q = get_quote(quotes_df, right, short_strike, "15:55:00")
    long_q = get_quote(quotes_df, right, long_strike, "15:55:00")
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = tiered_sizing(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    # Settlement P&L (hold to expiration)
    if side == "CALL":
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = SPREAD_WIDTH
        else:
            settlement = spx_exit - short_strike
    else:
        if spx_exit >= short_strike:
            settlement = 0.0
        elif spx_exit <= long_strike:
            settlement = SPREAD_WIDTH
        else:
            settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    settlement_pnl = gross - comm

    # SPX at check times
    spx_1556 = spx_at(spx_df, "15:56:00")
    spx_1557 = spx_at(spx_df, "15:57:00")

    # Buy-back cost at 15:56: short_ask (cost to buy back short) - long_bid (proceeds from selling long)
    close_cost_1556 = None
    short_q_1556 = get_quote(quotes_df, right, short_strike, "15:56:00")
    long_q_1556 = get_quote(quotes_df, right, long_strike, "15:56:00")
    if short_q_1556 is not None and long_q_1556 is not None:
        close_cost_1556 = short_q_1556["ask"] - long_q_1556["bid"]

    # Buy-back cost at 15:57
    close_cost_1557 = None
    short_q_1557 = get_quote(quotes_df, right, short_strike, "15:57:00")
    long_q_1557 = get_quote(quotes_df, right, long_strike, "15:57:00")
    if short_q_1557 is not None and long_q_1557 is not None:
        close_cost_1557 = short_q_1557["ask"] - long_q_1557["bid"]

    return TradeResult(
        date=date_str, side=side,
        short_strike=short_strike, long_strike=long_strike,
        qty=qty, credit=credit,
        settlement_pnl=settlement_pnl,
        spx_entry=spot,
        spx_1556=spx_1556, spx_1557=spx_1557,
        close_cost_1556=close_cost_1556,
        close_cost_1557=close_cost_1557,
    )


# ── Stop-loss P&L computation ──────────────────────────────────────────
def is_threatened(trade: TradeResult, spx_check: float, threshold: float) -> bool:
    """Check if SPX at check time has moved toward the short strike by > threshold."""
    move = spx_check - trade.spx_entry
    if trade.side == "CALL":
        # Call spread is threatened when SPX rises toward short strike
        return move > threshold
    else:
        # Put spread is threatened when SPX falls toward short strike
        return move < -threshold


def compute_stopped_pnl(trade: TradeResult, close_cost: float,
                        qty_fraction: float = 1.0) -> float:
    """Compute P&L when buying back the spread at close_cost.

    close_cost = short_ask - long_bid (per contract, in option price terms).
    qty_fraction: 1.0 = close all, 0.5 = close half.

    For the closed portion: P&L = (credit - close_cost) * qty_closed * 100 - commissions
    For the remaining portion: P&L = settlement_pnl * (1 - qty_fraction)
    Commissions: 4 legs * qty_closed * $0.50 for the close transaction
                 (entry commissions already counted in settlement_pnl for remaining,
                  and we need to add them for the closed portion)
    """
    qty_close = max(1, int(trade.qty * qty_fraction))
    qty_remain = trade.qty - qty_close

    # P&L on closed portion: credit collected - cost to close back
    # Entry commission: 4 legs * qty_close * COMMISSION (already paid at entry)
    # Close commission: 4 legs * qty_close * COMMISSION (paid to close)
    closed_pnl = (trade.credit - close_cost) * qty_close * 100.0
    closed_comm = 4 * qty_close * COMMISSION  # entry commission
    close_txn_comm = 4 * qty_close * COMMISSION  # close transaction commission
    closed_net = closed_pnl - closed_comm - close_txn_comm

    # P&L on remaining portion (held to settlement)
    if qty_remain > 0 and trade.qty > 0:
        # Scale the settlement P&L proportionally
        remain_pnl = trade.settlement_pnl * (qty_remain / trade.qty)
    else:
        remain_pnl = 0.0

    return closed_net + remain_pnl


# ── Config definitions ─────────────────────────────────────────────────
@dataclass
class StopConfig:
    name: str
    check_times: list  # list of ("15:56:00", threshold, qty_fraction)
    # Each tuple: (time_str, threshold_dollars, fraction_to_close)


CONFIGS = {
    "A (no stop)": StopConfig("A (no stop)", []),
    "B ($2 @15:56)": StopConfig("B ($2 @15:56)", [("15:56:00", 2.0, 1.0)]),
    "C ($3 @15:56)": StopConfig("C ($3 @15:56)", [("15:56:00", 3.0, 1.0)]),
    "D ($3 @15:57)": StopConfig("D ($3 @15:57)", [("15:57:00", 3.0, 1.0)]),
    "E ($2 @56+57)": StopConfig("E ($2 @56+57)", [("15:56:00", 2.0, 1.0), ("15:57:00", 2.0, 1.0)]),
    "F ($3 @56 50%)": StopConfig("F ($3 @56 50%)", [("15:56:00", 3.0, 0.5)]),
}


def apply_stop(trade: TradeResult, config: StopConfig) -> tuple[float, bool, str]:
    """Apply stop-loss config to a trade.

    Returns (final_pnl, was_stopped, stop_description).
    """
    if not config.check_times:
        return trade.settlement_pnl, False, ""

    remaining_fraction = 1.0

    for check_time, threshold, close_fraction in config.check_times:
        if remaining_fraction <= 0:
            break

        # Get SPX and close cost at check time
        if check_time == "15:56:00":
            spx_check = trade.spx_1556
            close_cost = trade.close_cost_1556
        elif check_time == "15:57:00":
            spx_check = trade.spx_1557
            close_cost = trade.close_cost_1557
        else:
            continue

        if spx_check is None or close_cost is None:
            continue

        if is_threatened(trade, spx_check, threshold):
            # For multi-check configs (E), close_fraction applies to remaining
            actual_fraction = close_fraction * remaining_fraction
            actual_qty_close = max(1, int(trade.qty * actual_fraction))
            actual_qty_remain = trade.qty - actual_qty_close

            # Compute closed portion P&L
            closed_pnl = (trade.credit - close_cost) * actual_qty_close * 100.0
            closed_comm = 4 * actual_qty_close * COMMISSION  # entry
            close_txn_comm = 4 * actual_qty_close * COMMISSION  # close
            closed_net = closed_pnl - closed_comm - close_txn_comm

            # Compute remaining portion P&L (held to settlement)
            if actual_qty_remain > 0:
                remain_pnl = trade.settlement_pnl * (actual_qty_remain / trade.qty)
            else:
                remain_pnl = 0.0

            total_pnl = closed_net + remain_pnl
            move = abs(spx_check - trade.spx_entry)
            desc = (f"STOP {check_time[:5]} SPX move ${move:.1f} | "
                    f"close_cost=${close_cost:.2f} | closed {actual_qty_close}/{trade.qty}")
            return total_pnl, True, desc

    # No stop triggered — hold to settlement
    return trade.settlement_pnl, False, ""


# ── Compute metrics ─────────────────────────────────────────────────────
def compute_metrics(day_pnls: list[float], n_trades: int, n_wins: int) -> dict:
    arr = np.array(day_pnls)
    total = arr.sum()
    equity = np.cumsum(arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak).min()
    dd_pct = ((equity - peak) / peak).min() * 100
    years = len(arr) / 252
    annual_ret = total / years if years > 0 else 0
    calmar = annual_ret / abs(dd) if dd != 0 else 0
    daily_rf = ANNUAL_RF / 252
    excess = arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0
    worst = arr.min()
    wr = n_wins / n_trades * 100 if n_trades > 0 else 0
    pf_num = arr[arr > 0].sum()
    pf_den = abs(arr[arr < 0].sum())
    pf = pf_num / pf_den if pf_den > 0 else float("inf")
    return {
        "total_pnl": total, "max_dd": dd, "dd_pct": dd_pct,
        "sharpe": sharpe, "calmar": calmar,
        "worst_day": worst, "trades": n_trades, "wr": wr,
        "pf": pf,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 130)
    print("Protective Stop-Loss Using Real 15:56/15:57 Option Prices")
    print("  Buy back losing spread at ask prices if SPX moves toward short strike")
    print("=" * 130)

    # Collect all dates
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    # Load all day data
    print("Loading day data...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_skip": 0}

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

        dist = rule_c3_dist(vix1550)
        if dist is None:
            skipped["vix_skip"] += 1
            continue

        spot = spx_at(spx_df, "15:55:00")
        if spot is None:
            skipped["no_spx"] += 1
            continue

        spx_exit = spx_at(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = spx_at(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        aft_ret = afternoon_return(spx_df)
        m30 = mom_30(spx_df)

        day_data.append({
            "date": ds, "spot": spot, "spx_exit": spx_exit,
            "vix1550": vix1550, "dist": dist,
            "opts_df": opts_df, "spx_df": spx_df,
            "aft_ret": aft_ret, "mom30": m30,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Simulate all trades and collect stop-loss data ──────────────────
    print("\nSimulating trades with stop-loss data collection...")
    all_trades: list[TradeResult] = []
    day_trade_map: dict[str, list[TradeResult]] = {}  # date -> list of trades

    for dd in day_data:
        ds = dd["date"]
        trades_today = []

        # Call side: skip if afternoon >= 1.0%
        skip_call = dd["aft_ret"] is not None and dd["aft_ret"] >= 1.0
        if not skip_call:
            ct = sim_side("CALL", dd["spot"], dd["spx_exit"], dd["dist"],
                          dd["opts_df"], dd["spx_df"], ds)
            if ct is not None:
                trades_today.append(ct)
                all_trades.append(ct)

        # Put side: skip if afternoon < -0.75% AND mom30 < -0.10%
        skip_put = (dd["aft_ret"] is not None and dd["aft_ret"] < -0.75 and
                    dd["mom30"] is not None and dd["mom30"] < -0.10)
        if not skip_put:
            pt = sim_side("PUT", dd["spot"], dd["spx_exit"], dd["dist"],
                          dd["opts_df"], dd["spx_df"], ds)
            if pt is not None:
                trades_today.append(pt)
                all_trades.append(pt)

        if trades_today:
            day_trade_map[ds] = trades_today

    n_calls = sum(1 for t in all_trades if t.side == "CALL")
    n_puts = sum(1 for t in all_trades if t.side == "PUT")
    print(f"  Total trades: {len(all_trades)} ({n_calls} calls + {n_puts} puts)")
    print(f"  Trading days: {len(day_trade_map)}")

    # Check data availability for stop-loss
    has_1556 = sum(1 for t in all_trades if t.close_cost_1556 is not None)
    has_1557 = sum(1 for t in all_trades if t.close_cost_1557 is not None)
    print(f"  Trades with 15:56 close quotes: {has_1556}/{len(all_trades)}")
    print(f"  Trades with 15:57 close quotes: {has_1557}/{len(all_trades)}")

    # ── Apply each config and compute results ───────────────────────────
    print("\nApplying stop-loss configs...")
    results = {}
    stop_details = {}  # config -> list of (date, side, stopped_pnl, baseline_pnl, desc)

    for cfg_name, cfg in CONFIGS.items():
        day_pnls = []
        n_trades = 0
        n_wins = 0
        stops = []
        total_stop_cost = 0.0

        for ds in sorted(day_trade_map.keys()):
            day_total = 0.0
            for trade in day_trade_map[ds]:
                pnl, was_stopped, desc = apply_stop(trade, cfg)
                day_total += pnl
                n_trades += 1
                if pnl > 0:
                    n_wins += 1
                if was_stopped:
                    saved = pnl - trade.settlement_pnl
                    stops.append({
                        "date": ds, "side": trade.side,
                        "stopped_pnl": pnl,
                        "baseline_pnl": trade.settlement_pnl,
                        "saved": saved,
                        "desc": desc,
                        "credit": trade.credit,
                        "qty": trade.qty,
                    })
                    total_stop_cost += (trade.settlement_pnl - pnl)  # negative = stop helped

            day_pnls.append(day_total)

        m = compute_metrics(day_pnls, n_trades, n_wins)
        m["n_stops"] = len(stops)
        m["stop_cost"] = total_stop_cost  # total cost of stopping (positive = stops cost money)
        results[cfg_name] = m
        stop_details[cfg_name] = stops

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 140)
    print("COMPARISON TABLE — Protective Stop-Loss Using Real Option Prices")
    print("  Base: Rule C3 adaptive + tiered sizing + put filter | Entry 15:55 | Width $5 | Comm $0.50/leg")
    print("=" * 140)
    print(f"{'Config':<18} {'Trades':>7} {'Stops':>6} {'Total P&L':>12} {'Max DD':>10} "
          f"{'DD%':>7} {'WR%':>7} {'Sharpe':>8} {'Calmar':>8} {'PF':>6} {'Worst Day':>11} {'Stop Cost':>11}")
    print("-" * 140)

    baseline = results["A (no stop)"]
    for cfg_name in CONFIGS:
        r = results[cfg_name]
        marker = " <-- baseline" if "no stop" in cfg_name else ""
        print(
            f"{cfg_name:<18} {r['trades']:>7} {r['n_stops']:>6} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['dd_pct']:>6.1f}% {r['wr']:>6.1f}% "
            f"{r['sharpe']:>8.2f} {r['calmar']:>8.2f} {r['pf']:>6.2f} "
            f"${r['worst_day']:>9,.0f} ${r['stop_cost']:>9,.0f}{marker}"
        )
    print("=" * 140)

    # ── Delta vs baseline ───────────────────────────────────────────────
    print("\nDelta vs A (no stop) baseline:")
    print("-" * 120)
    for cfg_name in CONFIGS:
        if "no stop" in cfg_name:
            continue
        r = results[cfg_name]
        b = baseline
        dpnl = r["total_pnl"] - b["total_pnl"]
        ddd = r["max_dd"] - b["max_dd"]
        dsharpe = r["sharpe"] - b["sharpe"]
        dcalmar = r["calmar"] - b["calmar"]
        dworst = r["worst_day"] - b["worst_day"]
        print(f"  {cfg_name:<18}: P&L {dpnl:>+10,.0f}  |  DD {ddd:>+8,.0f}  |  "
              f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}  |  "
              f"Worst Day {dworst:>+8,.0f}  |  Stops: {r['n_stops']}")

    # ── Stop-out detail for each config ─────────────────────────────────
    for cfg_name in CONFIGS:
        stops = stop_details[cfg_name]
        if not stops:
            continue

        print(f"\n{'='*120}")
        print(f"Stop-out details for {cfg_name} ({len(stops)} stops):")
        print(f"{'='*120}")
        print(f"  {'Date':<12} {'Side':<6} {'Cr':>6} {'Qty':>4} {'Stopped P&L':>12} "
              f"{'Baseline P&L':>13} {'Saved':>10} {'Description'}")
        print(f"  {'-'*110}")

        total_saved = 0.0
        helped = 0
        hurt = 0
        for s in sorted(stops, key=lambda x: x["saved"], reverse=True):
            tag = "HELPED" if s["saved"] > 0 else "HURT"
            if s["saved"] > 0:
                helped += 1
            else:
                hurt += 1
            total_saved += s["saved"]
            print(f"  {s['date']:<12} {s['side']:<6} ${s['credit']:>5.2f} {s['qty']:>4} "
                  f"${s['stopped_pnl']:>10,.0f} ${s['baseline_pnl']:>11,.0f} "
                  f"${s['saved']:>8,.0f}  {tag:6} {s['desc']}")

        print(f"\n  Summary: {helped} helped, {hurt} hurt | Net saved: ${total_saved:>+,.0f}")
        avg_when_helped = np.mean([s["saved"] for s in stops if s["saved"] > 0]) if helped > 0 else 0
        avg_when_hurt = np.mean([s["saved"] for s in stops if s["saved"] <= 0]) if hurt > 0 else 0
        print(f"  Avg saved when helped: ${avg_when_helped:>+,.0f} | Avg cost when hurt: ${avg_when_hurt:>+,.0f}")

    # ── Close cost analysis ─────────────────────────────────────────────
    print(f"\n{'='*120}")
    print("Close Cost Analysis (spread bid/ask at 15:56 vs entry credit):")
    print(f"{'='*120}")
    costs_1556 = [(t.close_cost_1556, t.credit, t.settlement_pnl, t.side, t.date)
                  for t in all_trades if t.close_cost_1556 is not None]
    if costs_1556:
        close_costs = [c[0] for c in costs_1556]
        credits = [c[1] for c in costs_1556]
        ratios = [cc / cr if cr > 0 else 0 for cc, cr in zip(close_costs, credits)]
        print(f"  Close cost at 15:56 (short_ask - long_bid):")
        print(f"    Mean: ${np.mean(close_costs):.3f}  |  Median: ${np.median(close_costs):.3f}  |  "
              f"Min: ${np.min(close_costs):.3f}  |  Max: ${np.max(close_costs):.3f}")
        print(f"  Entry credit:")
        print(f"    Mean: ${np.mean(credits):.3f}  |  Median: ${np.median(credits):.3f}")
        print(f"  Cost/Credit ratio:")
        print(f"    Mean: {np.mean(ratios):.2f}x  |  Median: {np.median(ratios):.2f}x")

        # How often is close_cost > credit (i.e., closing costs more than you received)
        n_unprofitable_close = sum(1 for cc, cr in zip(close_costs, credits) if cc > cr)
        print(f"  Close cost > credit (would lose by closing): {n_unprofitable_close}/{len(costs_1556)} "
              f"({n_unprofitable_close/len(costs_1556)*100:.1f}%)")

    # ── Worst 10 loss days comparison ───────────────────────────────────
    print(f"\n{'='*120}")
    print("Worst 10 Loss Days — Baseline vs Best Stop Config:")
    print(f"{'='*120}")

    # Find best config by Sharpe
    best_cfg = max((k for k in CONFIGS if k != "A (no stop)"),
                   key=lambda k: results[k]["sharpe"])

    # Collect day-level P&Ls for baseline and best
    baseline_days = {}
    best_days = {}
    for ds in sorted(day_trade_map.keys()):
        bl_total = sum(t.settlement_pnl for t in day_trade_map[ds])
        baseline_days[ds] = bl_total

        best_total = 0.0
        for t in day_trade_map[ds]:
            pnl, _, _ = apply_stop(t, CONFIGS[best_cfg])
            best_total += pnl
        best_days[ds] = best_total

    worst_base = sorted(baseline_days.items(), key=lambda x: x[1])[:10]
    print(f"\n  {'Date':<12} {'Baseline':>12} {best_cfg:>18}")
    print(f"  {'-'*50}")
    for ds, bl_pnl in worst_base:
        bp = best_days.get(ds, 0)
        diff = bp - bl_pnl
        marker = f"  ({'+' if diff >= 0 else ''}{diff:,.0f})"
        print(f"  {ds:<12} ${bl_pnl:>10,.0f} ${bp:>16,.0f}{marker}")

    print("\nDone.")


if __name__ == "__main__":
    main()
