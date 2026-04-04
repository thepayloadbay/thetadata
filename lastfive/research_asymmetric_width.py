#!/usr/bin/env python3
"""
Research: Asymmetric Spread Widths — wider spreads when credit is high.

Tests 4 width rules:
  A: Fixed $5 (current baseline)
  B: cr<0.30->w=5, 0.30-0.70->w=10, >0.70->w=15
  C: cr<0.50->w=5, >=0.50->w=10
  D: Always w=10

Base config: Rule C3 VIX-adaptive + tiered sizing + stacked put filter
Settlement: SPX-based (if beyond long strike, spread = width)
"""

import pandas as pd
import numpy as np
import math
import os
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

DATA = "/Users/jeerapongwongchote/Documents/thetadata/data"

HOLIDAYS = {
    "20220117","20220221","20220415","20220530","20220620","20220704",
    "20220905","20221124","20221226","20230102","20230116","20230220",
    "20230407","20230529","20230619","20230704","20230904","20231123",
    "20231225","20240101","20240115","20240219","20240329","20240527",
    "20240619","20240704","20240902","20241128","20241225","20250101",
    "20250120","20250217","20250418","20250526","20250704","20250901",
    "20251127","20251225","20260101","20260119","20260216","20260403",
    "20260525","20260703","20260907","20261126","20261225",
}
EARLY_CLOSE = {
    "20221125","20231124","20241129","20241224","20251128","20251224",
    "20261127","20261224",
}

START_DATE = "2022-01-03"
END_DATE = "2026-03-25"

COMMISSION_PER_LEG = 0.50
MIN_CREDIT = 0.10
STARTING_EQUITY = 50_000


# ─── helpers ──────────────────────────────────────────────────────────────────

def trading_dates(start, end):
    dates = []
    d = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    while d <= e:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in HOLIDAYS and ds not in EARLY_CLOSE:
            dates.append(ds)
        d += timedelta(days=1)
    return dates


def load_parquet(year, subdir, date_str):
    path = os.path.join(DATA, str(year), subdir, f"{date_str}.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)
    return None


def get_close_at(df, hhmm):
    """Get close price at HH:MM bar. hhmm like '15:55'."""
    mask = df["hhmm"] == hhmm
    rows = df[mask]
    if len(rows) == 0:
        return None
    return float(rows.iloc[0]["close"])


def compute_afternoon_return(spx_df):
    """SPX return from 13:00 to 15:54."""
    c1300 = get_close_at(spx_df, "13:00")
    c1554 = get_close_at(spx_df, "15:54")
    if c1300 is None or c1554 is None or c1300 == 0:
        return None
    return (c1554 - c1300) / c1300 * 100


def compute_mom30(spx_df):
    """SPX return from 15:25 to 15:54."""
    c1525 = get_close_at(spx_df, "15:25")
    c1554 = get_close_at(spx_df, "15:54")
    if c1525 is None or c1554 is None or c1525 == 0:
        return None
    return (c1554 - c1525) / c1525 * 100


def get_option_bid_ask(quotes_df, right, strike, hhmm):
    """Get bid/ask for an option at given time."""
    mask = (quotes_df["right"] == right) & \
           (quotes_df["strike"] == strike) & \
           (quotes_df["hhmm"] == hhmm)
    rows = quotes_df[mask]
    if len(rows) == 0:
        return None, None
    row = rows.iloc[0]
    return float(row["bid"]), float(row["ask"])


# ─── C3 VIX-adaptive distance ────────────────────────────────────────────────

def get_distance(vix):
    if vix is None:
        return None
    if vix < 18:
        return 0
    elif vix < 22:
        return 3
    elif vix <= 26:
        return 5
    else:
        return None  # skip


# ─── tiered sizing ───────────────────────────────────────────────────────────

def get_risk(credit):
    if credit < 0.20:
        return 1500
    elif credit <= 0.50:
        return 2500
    else:
        return 3500


# ─── width rules ─────────────────────────────────────────────────────────────

def width_A(credit):
    """Fixed $5"""
    return 5

def width_B(credit):
    """cr<0.30->5, 0.30-0.70->10, >0.70->15"""
    if credit < 0.30:
        return 5
    elif credit <= 0.70:
        return 10
    else:
        return 15

def width_C(credit):
    """cr<0.50->5, >=0.50->10"""
    if credit < 0.50:
        return 5
    else:
        return 10

def width_D(credit):
    """Always $10"""
    return 10


WIDTH_RULES = {
    "A (fixed $5)":     width_A,
    "B (cr-adaptive)":  width_B,
    "C (mod adaptive)": width_C,
    "D (always $10)":   width_D,
}


# ─── backtest ────────────────────────────────────────────────────────────────

def run_backtest(dates, width_fn):
    trades = []

    for date_str in dates:
        year = date_str[:4]

        spx_df = load_parquet(year, "spx_ohlc", date_str)
        vix_df = load_parquet(year, "vix_ohlc", date_str)
        quotes_df = load_parquet(year, "option_quotes", date_str)
        if spx_df is None or vix_df is None or quotes_df is None:
            continue

        # Add hhmm column
        spx_df["hhmm"] = spx_df["timestamp"].str[11:16]
        vix_df["hhmm"] = vix_df["timestamp"].str[11:16]
        quotes_df["hhmm"] = quotes_df["timestamp"].str[11:16]

        # VIX at 15:50
        vix_1550 = get_close_at(vix_df, "15:50")
        dist = get_distance(vix_1550)
        if dist is None:
            continue

        # SPX at 15:55 (entry)
        spot = get_close_at(spx_df, "15:55")
        if spot is None:
            spot = get_close_at(spx_df, "15:54")
        if spot is None:
            continue

        # SPX settlement at 16:00
        spx_exit = get_close_at(spx_df, "16:00")
        if spx_exit is None:
            spx_exit = get_close_at(spx_df, "15:59")
        if spx_exit is None:
            spx_exit = get_close_at(spx_df, "15:58")
        if spx_exit is None:
            continue

        # Filters
        afternoon_ret = compute_afternoon_return(spx_df)
        mom_30 = compute_mom30(spx_df)

        # ── CALL SIDE ──
        # Call filter: afternoon >= 1.0% -> skip
        skip_call = (afternoon_ret is not None and afternoon_ret >= 1.0)
        if not skip_call:
            call_short = math.ceil((spot + dist) / 5) * 5

            # Get short leg bid at 15:55
            short_bid, _ = get_option_bid_ask(quotes_df, "C", call_short, "15:55")
            if short_bid is not None:
                # First get $5-width credit to decide the width rule input
                _, long_ask_5 = get_option_bid_ask(quotes_df, "C", call_short + 5, "15:55")
                if long_ask_5 is not None:
                    base_credit = short_bid - long_ask_5
                    if base_credit >= MIN_CREDIT:
                        width = width_fn(base_credit)
                        call_long = call_short + width

                        # Get actual credit for chosen width
                        _, long_ask = get_option_bid_ask(quotes_df, "C", call_long, "15:55")
                        if long_ask is not None:
                            credit = short_bid - long_ask
                            if credit >= MIN_CREDIT:
                                risk = get_risk(credit)
                                max_loss_per = (width - credit) * 100
                                if max_loss_per > 0:
                                    qty = math.floor(risk / max_loss_per)
                                    if qty < 1:
                                        qty = 1

                                    # Settlement: SPX-based
                                    if spx_exit <= call_short:
                                        spread_val = 0.0
                                    elif spx_exit >= call_long:
                                        spread_val = width
                                    else:
                                        spread_val = spx_exit - call_short

                                    # Commission: $0.50/leg, 2 legs per spread
                                    commission = 2 * COMMISSION_PER_LEG * qty
                                    pnl = (credit - spread_val) * 100 * qty - commission

                                    trades.append({
                                        "date": date_str, "side": "CALL",
                                        "credit": credit, "width": width,
                                        "qty": qty, "pnl": pnl,
                                    })

        # ── PUT SIDE ──
        # Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip
        skip_put = False
        if afternoon_ret is not None and mom_30 is not None:
            if afternoon_ret < -0.75 and mom_30 < -0.10:
                skip_put = True

        if not skip_put:
            put_short = math.floor((spot - dist) / 5) * 5

            short_bid, _ = get_option_bid_ask(quotes_df, "P", put_short, "15:55")
            if short_bid is not None:
                _, long_ask_5 = get_option_bid_ask(quotes_df, "P", put_short - 5, "15:55")
                if long_ask_5 is not None:
                    base_credit = short_bid - long_ask_5
                    if base_credit >= MIN_CREDIT:
                        width = width_fn(base_credit)
                        put_long = put_short - width

                        _, long_ask = get_option_bid_ask(quotes_df, "P", put_long, "15:55")
                        if long_ask is not None:
                            credit = short_bid - long_ask
                            if credit >= MIN_CREDIT:
                                risk = get_risk(credit)
                                max_loss_per = (width - credit) * 100
                                if max_loss_per > 0:
                                    qty = math.floor(risk / max_loss_per)
                                    if qty < 1:
                                        qty = 1

                                    # Settlement: SPX-based
                                    if spx_exit >= put_short:
                                        spread_val = 0.0
                                    elif spx_exit <= put_long:
                                        spread_val = width
                                    else:
                                        spread_val = put_short - spx_exit

                                    commission = 2 * COMMISSION_PER_LEG * qty
                                    pnl = (credit - spread_val) * 100 * qty - commission

                                    trades.append({
                                        "date": date_str, "side": "PUT",
                                        "credit": credit, "width": width,
                                        "qty": qty, "pnl": pnl,
                                    })

    return trades


def compute_stats(trades):
    if not trades:
        return None

    df = pd.DataFrame(trades)
    pnls = df["pnl"].values
    total_pnl = pnls.sum()
    n_trades = len(pnls)
    wins = (pnls > 0).sum()
    wr = wins / n_trades * 100

    # Max drawdown
    equity = STARTING_EQUITY + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[STARTING_EQUITY], equity]))
    dd = np.concatenate([[STARTING_EQUITY], equity]) - peak
    max_dd = dd.min()

    # Sharpe (daily P&L, annualized)
    daily_pnl = df.groupby("date")["pnl"].sum()
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)
    else:
        sharpe = 0.0

    avg_credit = df["credit"].mean()
    avg_width = df["width"].mean()

    # Profit factor
    gross_profit = pnls[pnls > 0].sum()
    gross_loss = abs(pnls[pnls < 0].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "trades": n_trades,
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "sharpe": sharpe,
        "avg_credit": avg_credit,
        "avg_width": avg_width,
        "pf": pf,
    }


# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_dates = trading_dates(START_DATE, END_DATE)
    print(f"Trading dates: {len(all_dates)}")
    print(f"Range: {all_dates[0]} to {all_dates[-1]}")
    print()

    print("=" * 115)
    print("ASYMMETRIC SPREAD WIDTH COMPARISON")
    print("Base: C3 VIX-adaptive + tiered sizing + stacked put filter")
    print("Settlement: SPX-based (if beyond long strike, spread = width)")
    print("Width rules:")
    print("  A: Fixed $5 (current baseline)")
    print("  B: Credit-adaptive: cr<0.30->$5, 0.30-0.70->$10, >0.70->$15")
    print("  C: Moderate adaptive: cr<0.50->$5, >=0.50->$10")
    print("  D: Always $10")
    print("=" * 115)
    print()

    results = {}
    for label, fn in WIDTH_RULES.items():
        print(f"  Running {label}...")
        trades = run_backtest(all_dates, fn)
        stats = compute_stats(trades)
        results[label] = (stats, trades)

    print()
    print("=" * 120)
    hdr = (f"{'Rule':<20} {'Trades':>6} {'Total P&L':>11} {'Max DD':>10} "
           f"{'WR':>6} {'Sharpe':>7} {'PF':>6} {'AvgCred':>8} {'AvgWid':>7}")
    print(hdr)
    print("-" * 120)

    for label in WIDTH_RULES:
        stats, _ = results[label]
        if stats is None:
            print(f"{label:<20}   NO TRADES")
            continue
        s = stats
        print(f"{label:<20} {s['trades']:>6} ${s['total_pnl']:>10,.0f} "
              f"${s['max_dd']:>9,.0f} {s['wr']:>5.1f}% {s['sharpe']:>7.2f} "
              f"{s['pf']:>6.2f} ${s['avg_credit']:>7.3f} {s['avg_width']:>6.1f}")

    print("=" * 120)
    print()

    # ── Credit bucket breakdown for baseline (A) ──
    print("=" * 80)
    print("CREDIT DISTRIBUTION (Rule A baseline)")
    print("=" * 80)
    stats_a, trades_a = results["A (fixed $5)"]
    if trades_a:
        df_a = pd.DataFrame(trades_a)
        bins = [0, 0.20, 0.30, 0.50, 0.70, 1.0, 999]
        labels_bin = ["<0.20", "0.20-0.30", "0.30-0.50", "0.50-0.70", "0.70-1.00", ">1.00"]
        df_a["cr_bin"] = pd.cut(df_a["credit"], bins=bins, labels=labels_bin, right=False)
        for b in labels_bin:
            subset = df_a[df_a["cr_bin"] == b]
            if len(subset) == 0:
                continue
            n = len(subset)
            pnl = subset["pnl"].sum()
            wr_b = (subset["pnl"] > 0).sum() / n * 100
            print(f"  Credit {b:<12}: {n:>4} trades, P&L ${pnl:>9,.0f}, WR {wr_b:.1f}%")
    print()

    # ── Call vs Put breakdown by rule ──
    print("=" * 80)
    print("CALL vs PUT BREAKDOWN BY RULE")
    print("=" * 80)
    for label in WIDTH_RULES:
        _, trades_list = results[label]
        if not trades_list:
            continue
        df = pd.DataFrame(trades_list)
        for side in ["CALL", "PUT"]:
            sub = df[df["side"] == side]
            if len(sub) == 0:
                continue
            pnl = sub["pnl"].sum()
            n = len(sub)
            wr_s = (sub["pnl"] > 0).sum() / n * 100
            avg_w = sub["width"].mean()
            print(f"  {label:<20} {side:<4}: {n:>4} trades, P&L ${pnl:>9,.0f}, "
                  f"WR {wr_s:.1f}%, AvgWidth {avg_w:.1f}")
    print()
    print("DONE")
