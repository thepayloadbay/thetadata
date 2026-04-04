"""Research: Max Qty Cap — mechanically limit worst-case loss per side.

Hypothesis: capping the maximum number of contracts per side bounds the
worst-case loss to MAX_QTY * width * 100, regardless of credit received.
High-credit ATM trades currently size to 7-12 contracts; a cap limits tail risk.

Base config: Rule C3 VIX-adaptive (VIX<18->dist=0, 18-22->dist=3, 22-26->dist=5, >26->skip),
bidirectional call+put, entry 15:55, exit 16:00, width $5, min credit $0.10,
commission $0.50/leg, tiered sizing (cr<0.20->$1500, 0.20-0.50->$2500, >0.50->$3500).

qty = min(tiered_qty, MAX_QTY).  Max loss per side = MAX_QTY * width * 100.

Caps tested:
  A: No cap (baseline)
  B: max 3 -> worst case $1,500/side
  C: max 4 -> worst case $2,000/side
  D: max 5 -> worst case $2,500/side
  E: max 6 -> worst case $3,000/side
  F: max 7 -> worst case $3,500/side
  G: max 8 -> worst case $4,000/side
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
COMMISSION = 0.50  # per leg per contract
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

# Caps to test: None = no cap
CAPS = {
    "A_nocap":  None,
    "B_max3":   3,
    "C_max4":   4,
    "D_max5":   5,
    "E_max6":   6,
    "F_max7":   7,
    "G_max8":   8,
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


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """Signed afternoon return (13:00 -> 15:54)."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 15:25 to 15:54."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    s, e = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


def get_quote(quotes_df: pd.DataFrame, right: str, strike: int,
              time_str: str) -> dict | None:
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
            "mid": float(r["mid"])}


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    elif vix <= 26:
        return 5.0
    else:
        return -1.0  # skip


# ── Tiered sizing ──────────────────────────────────────────────────────
def tiered_size(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             max_qty: int | None) -> dict | None:
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

    risk_amount = tiered_size(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    # Apply cap
    if max_qty is not None:
        qty = min(qty, max_qty)

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
            "short_strike": short_strike}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str,
            max_qty: int | None) -> dict | None:
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

    risk_amount = tiered_size(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    # Apply cap
    if max_qty is not None:
        qty = min(qty, max_qty)

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
            "short_strike": short_strike}


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 130)
    print("Max Qty Cap Research — Rule C3 VIX-Adaptive, Bidirectional, Tiered Sizing")
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

    # Pre-collect day data
    print("Loading day data...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_data": 0,
               "vix_filter": 0, "no_spot": 0}

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
            skipped["no_data"] += 1
            continue

        vix_df = load_vix(ds)
        if vix_df is None:
            skipped["no_data"] += 1
            continue

        vix1550 = get_price_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0:
            skipped["no_data"] += 1
            continue

        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        dist = rule_c3_dist(vix1550)
        if dist < 0:
            skipped["vix_filter"] += 1
            continue

        # Afternoon return (signed for put filter, absolute for call filter)
        aft_ret_signed = compute_afternoon_return(spx_df)
        aft_ret_abs = abs(aft_ret_signed) if aft_ret_signed is not None else None

        # Mom30 for put filter
        mom30 = compute_mom30(spx_df)

        # Call filter: afternoon abs >= 1.0% -> skip call
        skip_call = (aft_ret_abs is not None and aft_ret_abs >= 1.0)

        # Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip put
        skip_put = False
        if aft_ret_signed is not None and mom30 is not None:
            if aft_ret_signed < -0.75 and mom30 < -0.10:
                skip_put = True

        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
            skipped["no_spot"] += 1
            continue
        spot = float(spot_row["close"].iloc[0])

        exit_row = spx_df[spx_df["hhmm"] == "16:00"]
        if exit_row.empty:
            exit_row = spx_df[spx_df["hhmm"] == "15:59"]
        if exit_row.empty:
            spx_exit = float(spx_df["close"].iloc[-1])
        else:
            spx_exit = float(exit_row["close"].iloc[0])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_data"] += 1
            continue

        day_data.append({
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "dist": dist,
            "opts_df": opts_df,
            "skip_call": skip_call,
            "skip_put": skip_put,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Run each cap variant ────────────────────────────────────────────
    results = {}
    for cap_name, max_qty in CAPS.items():
        all_trades = []
        day_pnls = []  # (date, day_total_pnl)

        for dd in day_data:
            dist = dd["dist"]
            day_pnl = 0.0
            has_trade = False

            # Call side
            if not dd["skip_call"]:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist,
                              dd["opts_df"], "15:55:00", max_qty)
                if ct is not None:
                    ct["date"] = dd["date"]
                    all_trades.append(ct)
                    day_pnl += ct["pnl"]
                    has_trade = True

            # Put side
            if not dd["skip_put"]:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist,
                             dd["opts_df"], "15:55:00", max_qty)
                if pt is not None:
                    pt["date"] = dd["date"]
                    all_trades.append(pt)
                    day_pnl += pt["pnl"]
                    has_trade = True

            if has_trade:
                day_pnls.append((dd["date"], day_pnl))

        n_trades = len(all_trades)
        if n_trades == 0:
            results[cap_name] = None
            continue

        day_pnl_arr = np.array([x[1] for x in day_pnls])
        total_pnl = day_pnl_arr.sum()

        # Max drawdown
        equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = equity - peak
        max_dd = dd_arr.min()

        # Win rate (per trade)
        n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
        wr = n_wins / n_trades * 100

        # Sharpe (daily)
        daily_rf = ANNUAL_RF / 252
        excess = day_pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = (excess.mean() / day_pnl_arr.std() * np.sqrt(252)
                  if day_pnl_arr.std() > 0 else 0)

        # Calmar
        years = len(day_pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Worst day
        worst_day_idx = np.argmin(day_pnl_arr)
        worst_day_pnl = day_pnl_arr[worst_day_idx]
        worst_day_date = day_pnls[worst_day_idx][0]

        # Worst single trade
        trade_pnls = np.array([t["pnl"] for t in all_trades])
        worst_trade_idx = np.argmin(trade_pnls)
        worst_trade_pnl = trade_pnls[worst_trade_idx]

        # Avg qty
        avg_qty = np.mean([t["qty"] for t in all_trades])

        # Profit factor
        gross_profit = trade_pnls[trade_pnls > 0].sum()
        gross_loss = abs(trade_pnls[trade_pnls < 0].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        results[cap_name] = {
            "trades": n_trades,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "pf": pf,
            "worst_day": worst_day_pnl,
            "worst_day_date": worst_day_date,
            "worst_trade": worst_trade_pnl,
            "avg_qty": avg_qty,
            "max_qty_cap": max_qty,
        }

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 160)
    print("COMPARISON TABLE — Max Qty Cap (Rule C3 VIX-Adaptive, Bidirectional, Tiered Sizing)")
    print("  Call filter: afternoon_abs >= 1.0% -> skip | Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip")
    print("  qty = min(tiered_qty, MAX_QTY) | Max loss per side = MAX_QTY * $5 * 100")
    print("=" * 160)
    hdr = (f"{'Cap':<12} {'MaxLoss':>8} {'Trades':>7} {'P&L':>11} {'MaxDD':>9} {'WR%':>6} "
           f"{'Sharpe':>7} {'Calmar':>8} {'PF':>6} {'WorstDay':>10} "
           f"{'WorstTrade':>11} {'AvgQty':>7}")
    print(hdr)
    print("-" * 160)

    for cap_name in CAPS:
        r = results.get(cap_name)
        if r is None:
            print(f"{cap_name:<12}  NO TRADES")
            continue
        max_loss_str = "none" if r["max_qty_cap"] is None else f"${r['max_qty_cap'] * 500:,}"
        print(
            f"{cap_name:<12} {max_loss_str:>8} {r['trades']:>7} "
            f"${r['total_pnl']:>9,.0f} ${r['max_dd']:>7,.0f} "
            f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} ${r['worst_day']:>8,.0f} "
            f"${r['worst_trade']:>9,.0f} {r['avg_qty']:>7.1f}"
        )
    print("=" * 160)

    # ── Delta vs baseline ──────────────────────────────────────────────
    baseline = results.get("A_nocap")
    if baseline:
        print("\nDelta vs A_nocap baseline:")
        print("-" * 120)
        for cap_name in CAPS:
            if cap_name == "A_nocap":
                continue
            r = results.get(cap_name)
            if r is None:
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dsharpe = r["sharpe"] - baseline["sharpe"]
            dcalmar = r["calmar"] - baseline["calmar"]
            dpf = r["pf"] - baseline["pf"]
            dworst = r["worst_day"] - baseline["worst_day"]
            print(f"  {cap_name:<12}: P&L {dpnl:>+10,.0f}  DD {ddd:>+8,.0f} ({ddd/abs(baseline['max_dd'])*100:>+5.0f}%)  "
                  f"Sharpe {dsharpe:>+.2f}  Calmar {dcalmar:>+.2f}  "
                  f"PF {dpf:>+.2f}  WorstDay {dworst:>+8,.0f}")

    # ── Variant descriptions ───────────────────────────────────────────
    print("\nVariant descriptions:")
    print("  A_nocap : No cap — tiered sizing determines qty (can reach 7-12 on high-credit ATM)")
    print("  B_max3  : qty capped at 3  -> worst case $1,500/side")
    print("  C_max4  : qty capped at 4  -> worst case $2,000/side")
    print("  D_max5  : qty capped at 5  -> worst case $2,500/side")
    print("  E_max6  : qty capped at 6  -> worst case $3,000/side")
    print("  F_max7  : qty capped at 7  -> worst case $3,500/side")
    print("  G_max8  : qty capped at 8  -> worst case $4,000/side")

    # ── Qty distribution for baseline ──────────────────────────────────
    print("\n\nQty distribution (baseline A_nocap):")
    print("-" * 60)
    # Re-run baseline to get qty distribution
    qty_list = []
    for dd in day_data:
        dist = dd["dist"]
        if not dd["skip_call"]:
            ct = sim_call(dd["spot"], dd["spx_exit"], dist,
                          dd["opts_df"], "15:55:00", None)
            if ct is not None:
                qty_list.append(ct["qty"])
        if not dd["skip_put"]:
            pt = sim_put(dd["spot"], dd["spx_exit"], dist,
                         dd["opts_df"], "15:55:00", None)
            if pt is not None:
                qty_list.append(pt["qty"])

    qty_arr = np.array(qty_list)
    for q in sorted(set(qty_arr)):
        n = (qty_arr == q).sum()
        pct = n / len(qty_arr) * 100
        bar = "#" * int(pct / 2)
        print(f"  qty={int(q):>2}: {n:>4} trades ({pct:>5.1f}%) {bar}")
    print(f"\n  Mean: {qty_arr.mean():.1f}, Median: {np.median(qty_arr):.0f}, "
          f"Max: {qty_arr.max():.0f}, Min: {qty_arr.min():.0f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
