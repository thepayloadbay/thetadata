#!/usr/bin/env python3
"""
FINAL LastFive Stress Test & Edge Extraction
=============================================
Config: C3 VIX-adaptive + Tiered credit sizing + Stacked put filter
Bidirectional call+put, entry 15:55, exit 16:00 settlement
"""

import pandas as pd
import numpy as np
import math
import os
import warnings
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings("ignore")

BASE = "/Users/jeerapongwongchote/Documents/thetadata"
DATA = os.path.join(BASE, "data")

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
WIDTH = 5
MIN_CREDIT = 0.10
STARTING_EQUITY = 50_000


# ─── helpers ───────────────────────────────────────────────────────────────────

def trading_dates(start, end):
    """Generate trading dates (weekdays, not holidays, not early close for simplicity)."""
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


def extract_hhmm(ts_str):
    """'2024-01-02T15:55:00.000' -> '15:55'"""
    return ts_str[11:16]


def get_bar(df, hhmm):
    """Get a bar at specific hhmm from ohlc dataframe."""
    mask = df["timestamp"].str.contains(f"T{hhmm}:")
    rows = df[mask]
    if len(rows) == 0:
        return None
    return rows.iloc[0]


def get_spot_at(spx_df, hhmm):
    bar = get_bar(spx_df, hhmm)
    if bar is None:
        return None
    return float(bar["close"])


def get_vix_at(vix_df, hhmm):
    bar = get_bar(vix_df, hhmm)
    if bar is None:
        return None
    return float(bar["close"])


def compute_afternoon_return(spx_df):
    """SPX bars 13:00-15:54, (last-first)/first*100"""
    hhmm_series = spx_df["timestamp"].str[11:16]
    afternoon = spx_df[(hhmm_series >= "13:00") & (hhmm_series <= "15:54")]
    if len(afternoon) < 2:
        return None
    first_close = float(afternoon.iloc[0]["close"])
    last_close = float(afternoon.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def compute_mom_30(spx_df):
    """SPX bars 15:25-15:54, (last-first)/first*100"""
    hhmm_series = spx_df["timestamp"].str[11:16]
    afternoon = spx_df[(hhmm_series >= "15:25") & (hhmm_series <= "15:54")]
    if len(afternoon) < 2:
        return None
    first_close = float(afternoon.iloc[0]["close"])
    last_close = float(afternoon.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def get_quote(quotes_df, right, strike, hhmm):
    """Get mid price for a specific option at hhmm."""
    mask = (quotes_df["right"] == right) & \
           (quotes_df["strike"] == strike) & \
           (quotes_df["timestamp"].str.contains(f"T{hhmm}:"))
    rows = quotes_df[mask]
    if len(rows) == 0:
        return None
    row = rows.iloc[0]
    bid = float(row["bid"])
    ask = float(row["ask"])
    mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid}


def compute_credit(quotes_df, right, short_strike, long_strike, hhmm):
    """Compute credit spread entry credit (sell short, buy long)."""
    short_q = get_quote(quotes_df, right, short_strike, hhmm)
    long_q = get_quote(quotes_df, right, long_strike, hhmm)
    if short_q is None or long_q is None:
        return None, None, None
    # credit = short bid - long ask (conservative: sell at bid, buy at ask)
    credit = short_q["bid"] - long_q["ask"]
    return credit, short_q, long_q


def compute_settlement_pnl(quotes_df, right, short_strike, long_strike, entry_credit, hhmm_exit="16:00"):
    """Compute exit at settlement (16:00). If no 16:00 quote, use 15:59."""
    for try_hhmm in ["16:00", "15:59", "15:58"]:
        short_q = get_quote(quotes_df, right, short_strike, try_hhmm)
        long_q = get_quote(quotes_df, right, long_strike, try_hhmm)
        if short_q is not None and long_q is not None:
            # debit to close = short ask - long bid (buy back short at ask, sell long at bid)
            # But at settlement, use mid (intrinsic)
            exit_debit = short_q["mid"] - long_q["mid"]
            if exit_debit < 0:
                exit_debit = 0
            pnl = entry_credit - exit_debit
            return pnl, exit_debit
    return None, None


# ─── strategy configs ──────────────────────────────────────────────────────────

def config_original():
    """Original baseline: fixed dist=3, fixed $2500, no put filter."""
    return {"name": "Original Baseline", "vix_adaptive": False, "fixed_dist": 3,
            "tiered": False, "fixed_risk": 2500, "put_filter": False}

def config_c3_only():
    """Rule C3 VIX-adaptive only, fixed $2500."""
    return {"name": "C3 Only", "vix_adaptive": True,
            "tiered": False, "fixed_risk": 2500, "put_filter": False}

def config_c3_tiered():
    """C3 + tiered sizing, no put filter."""
    return {"name": "C3 + Tiered", "vix_adaptive": True,
            "tiered": True, "put_filter": False}

def config_final():
    """FINAL: C3 + Tiered + put filter."""
    return {"name": "FINAL", "vix_adaptive": True,
            "tiered": True, "put_filter": True}


def get_distance(vix, config):
    if config["vix_adaptive"]:
        if vix is None:
            return None  # skip
        if vix < 18:
            return 0
        elif vix < 22:
            return 3
        elif vix <= 26:
            return 5
        else:
            return None  # skip (VIX > 26)
    else:
        return config["fixed_dist"]


def get_risk_per_contract(credit, config):
    if config["tiered"]:
        if credit < 0.20:
            return 1500
        elif credit <= 0.50:
            return 2500
        else:
            return 3500
    else:
        return config["fixed_risk"]


def should_skip_put(afternoon_ret, mom_30, config):
    if not config["put_filter"]:
        return False
    if afternoon_ret is not None and mom_30 is not None:
        if afternoon_ret < -0.75 and mom_30 < -0.10:
            return True
    return False


def should_skip_call(afternoon_ret):
    """Skip call if afternoon_return >= 1.0%"""
    if afternoon_ret is not None and afternoon_ret >= 1.0:
        return True
    return False


# ─── main backtest engine ─────────────────────────────────────────────────────

def run_backtest(config, dates):
    trades = []
    equity = STARTING_EQUITY
    equity_curve = [STARTING_EQUITY]
    peak = STARTING_EQUITY
    max_dd = 0

    for date_str in dates:
        year = date_str[:4]

        # Load data
        spx_df = load_parquet(year, "spx_ohlc", date_str)
        vix_df = load_parquet(year, "vix_ohlc", date_str)
        quotes_df = load_parquet(year, "option_quotes", date_str)

        if spx_df is None or vix_df is None or quotes_df is None:
            continue

        # Get VIX at 15:50
        vix_1550 = get_vix_at(vix_df, "15:50")

        # Get distance
        dist = get_distance(vix_1550, config)
        if dist is None:
            continue  # skip day

        # Get SPX spot at 15:55
        spot = get_spot_at(spx_df, "15:55")
        if spot is None:
            spot = get_spot_at(spx_df, "15:54")
        if spot is None:
            continue

        # Compute filters
        afternoon_ret = compute_afternoon_return(spx_df)
        mom_30 = compute_mom_30(spx_df)

        # ── CALL SIDE ──
        skip_call = should_skip_call(afternoon_ret)
        if not skip_call:
            call_short = math.ceil((spot + dist) / 5) * 5
            call_long = call_short + WIDTH

            credit_call, _, _ = compute_credit(quotes_df, "C", call_short, call_long, "15:55")
            if credit_call is not None and credit_call >= MIN_CREDIT:
                net_credit_call = credit_call - 2 * COMMISSION_PER_LEG / 100  # per-share commission
                risk_per = get_risk_per_contract(credit_call, config)
                max_loss_per = (WIDTH - credit_call) * 100
                if max_loss_per > 0:
                    num_contracts = max(1, int(risk_per / max_loss_per))
                else:
                    num_contracts = 1

                pnl_per, exit_debit = compute_settlement_pnl(
                    quotes_df, "C", call_short, call_long, credit_call)

                if pnl_per is not None:
                    total_pnl = pnl_per * 100 * num_contracts - 2 * COMMISSION_PER_LEG * num_contracts
                    equity += total_pnl
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    max_dd = max(max_dd, dd)
                    equity_curve.append(equity)

                    trades.append({
                        "date": date_str, "side": "CALL", "spot": spot,
                        "short_strike": call_short, "long_strike": call_long,
                        "credit": credit_call, "exit_debit": exit_debit,
                        "pnl_per": pnl_per, "contracts": num_contracts,
                        "total_pnl": total_pnl, "vix": vix_1550,
                        "afternoon_ret": afternoon_ret, "mom_30": mom_30,
                        "equity": equity,
                    })

        # ── PUT SIDE ──
        skip_put = should_skip_put(afternoon_ret, mom_30, config)
        if not skip_put:
            put_short = math.floor((spot - dist) / 5) * 5
            put_long = put_short - WIDTH

            credit_put, _, _ = compute_credit(quotes_df, "P", put_short, put_long, "15:55")
            if credit_put is not None and credit_put >= MIN_CREDIT:
                net_credit_put = credit_put - 2 * COMMISSION_PER_LEG / 100
                risk_per = get_risk_per_contract(credit_put, config)
                max_loss_per = (WIDTH - credit_put) * 100
                if max_loss_per > 0:
                    num_contracts = max(1, int(risk_per / max_loss_per))
                else:
                    num_contracts = 1

                pnl_per, exit_debit = compute_settlement_pnl(
                    quotes_df, "P", put_short, put_long, credit_put)

                if pnl_per is not None:
                    total_pnl = pnl_per * 100 * num_contracts - 2 * COMMISSION_PER_LEG * num_contracts
                    equity += total_pnl
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    max_dd = max(max_dd, dd)
                    equity_curve.append(equity)

                    trades.append({
                        "date": date_str, "side": "PUT", "spot": spot,
                        "short_strike": put_short, "long_strike": put_long,
                        "credit": credit_put, "exit_debit": exit_debit,
                        "pnl_per": pnl_per, "contracts": num_contracts,
                        "total_pnl": total_pnl, "vix": vix_1550,
                        "afternoon_ret": afternoon_ret, "mom_30": mom_30,
                        "equity": equity,
                    })

    return trades, equity_curve, max_dd


def compute_stats(trades, equity_curve, max_dd, label=""):
    if not trades:
        return {"label": label, "trades": 0}

    df = pd.DataFrame(trades)
    total_pnl = df["total_pnl"].sum()
    n_trades = len(df)
    n_call = len(df[df["side"] == "CALL"])
    n_put = len(df[df["side"] == "PUT"])
    wins = df[df["total_pnl"] > 0]
    losses = df[df["total_pnl"] <= 0]
    wr = len(wins) / n_trades * 100 if n_trades > 0 else 0
    avg_credit = df["credit"].mean()
    worst_trade = df["total_pnl"].min()
    best_trade = df["total_pnl"].max()

    # Profit factor
    gross_profit = wins["total_pnl"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["total_pnl"].sum()) if len(losses) > 0 else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe (daily returns)
    daily_pnl = df.groupby("date")["total_pnl"].sum()
    if len(daily_pnl) > 1:
        sharpe = daily_pnl.mean() / daily_pnl.std() * np.sqrt(252) if daily_pnl.std() > 0 else 0
    else:
        sharpe = 0

    # Calmar
    calmar = 0
    if max_dd > 0:
        # annualized return
        n_days = len(daily_pnl)
        if n_days > 0:
            ann_ret = total_pnl / STARTING_EQUITY / (n_days / 252) * 100
            calmar = ann_ret / max_dd

    # CAGR
    final_eq = equity_curve[-1] if equity_curve else STARTING_EQUITY
    n_years = len(df.groupby("date")) / 252
    if n_years > 0 and final_eq > 0:
        cagr = (final_eq / STARTING_EQUITY) ** (1 / n_years) - 1
    else:
        cagr = 0

    return {
        "label": label, "trades": n_trades, "calls": n_call, "puts": n_put,
        "pnl": total_pnl, "max_dd_pct": max_dd, "wr": wr,
        "sharpe": sharpe, "calmar": calmar, "pf": pf,
        "cagr": cagr * 100, "avg_credit": avg_credit,
        "worst_trade": worst_trade, "best_trade": best_trade,
        "final_equity": final_eq,
    }


def print_stats(s):
    if s["trades"] == 0:
        print(f"  {s['label']}: NO TRADES")
        return
    print(f"  {s['label']}:")
    print(f"    Total P&L:     ${s['pnl']:>10,.2f}")
    print(f"    Final Equity:  ${s['final_equity']:>10,.2f}")
    print(f"    Trades:        {s['trades']} (C:{s['calls']} P:{s['puts']})")
    print(f"    Win Rate:      {s['wr']:.1f}%")
    print(f"    Max DD:        {s['max_dd_pct']:.2f}%")
    print(f"    Sharpe:        {s['sharpe']:.2f}")
    print(f"    Calmar:        {s['calmar']:.2f}")
    print(f"    Profit Factor: {s['pf']:.2f}")
    print(f"    CAGR:          {s['cagr']:.1f}%")
    print(f"    Avg Credit:    ${s['avg_credit']:.3f}")
    print(f"    Worst Trade:   ${s['worst_trade']:>10,.2f}")
    print(f"    Best Trade:    ${s['best_trade']:>10,.2f}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_dates = trading_dates(START_DATE, END_DATE)
    print(f"Total trading dates: {len(all_dates)}")
    print(f"Date range: {all_dates[0]} to {all_dates[-1]}")
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. FULL PERIOD — FINAL CONFIG
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("1. FULL PERIOD — FINAL CONFIG (2022-01-03 to 2026-03-25)")
    print("=" * 80)

    cfg_final = config_final()
    trades_final, eq_final, dd_final = run_backtest(cfg_final, all_dates)
    stats_final = compute_stats(trades_final, eq_final, dd_final, "FINAL")
    print_stats(stats_final)
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. WALK-FORWARD: Train (2022-2024) vs Test (2025-2026)
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("2. WALK-FORWARD: Train (2022-2024) vs Test (2025-2026)")
    print("=" * 80)

    train_dates = [d for d in all_dates if d < "20250101"]
    test_dates = [d for d in all_dates if d >= "20250101"]

    t_train, eq_train, dd_train = run_backtest(cfg_final, train_dates)
    t_test, eq_test, dd_test = run_backtest(cfg_final, test_dates)

    s_train = compute_stats(t_train, eq_train, dd_train, "TRAIN (2022-2024)")
    s_test = compute_stats(t_test, eq_test, dd_test, "TEST (2025-2026)")

    print_stats(s_train)
    print()
    print_stats(s_test)
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. YEAR-BY-YEAR
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("3. YEAR-BY-YEAR BREAKDOWN")
    print("=" * 80)

    for year in ["2022", "2023", "2024", "2025", "2026"]:
        yr_dates = [d for d in all_dates if d[:4] == year]
        if not yr_dates:
            continue
        t_yr, eq_yr, dd_yr = run_backtest(cfg_final, yr_dates)
        s_yr = compute_stats(t_yr, eq_yr, dd_yr, f"Year {year}")
        print_stats(s_yr)
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. MONTHLY P&L
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("4. MONTHLY P&L")
    print("=" * 80)

    if trades_final:
        df_trades = pd.DataFrame(trades_final)
        df_trades["month"] = df_trades["date"].str[:6]
        monthly = df_trades.groupby("month").agg(
            pnl=("total_pnl", "sum"),
            trades=("total_pnl", "count"),
            wr=("total_pnl", lambda x: (x > 0).sum() / len(x) * 100),
        )
        neg_months = 0
        print(f"  {'Month':<8} {'P&L':>10} {'Trades':>7} {'WR':>7}")
        print(f"  {'-'*8} {'-'*10} {'-'*7} {'-'*7}")
        for idx, row in monthly.iterrows():
            flag = " ***" if row["pnl"] < 0 else ""
            if row["pnl"] < 0:
                neg_months += 1
            print(f"  {idx:<8} ${row['pnl']:>9,.2f} {int(row['trades']):>7} {row['wr']:>6.1f}%{flag}")
        print(f"\n  Negative months: {neg_months} / {len(monthly)}")
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. WORST 15 DAYS
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("5. WORST 15 TRADES")
    print("=" * 80)

    if trades_final:
        df_trades = pd.DataFrame(trades_final)
        worst = df_trades.nsmallest(15, "total_pnl")
        print(f"  {'Date':<10} {'Side':<5} {'SPX':>8} {'Short':>7} {'Credit':>7} {'P&L':>10} {'VIX':>6} {'Cts':>4}")
        print(f"  {'-'*10} {'-'*5} {'-'*8} {'-'*7} {'-'*7} {'-'*10} {'-'*6} {'-'*4}")
        for _, r in worst.iterrows():
            vix_str = f"{r['vix']:.1f}" if r['vix'] is not None else "N/A"
            print(f"  {r['date']:<10} {r['side']:<5} {r['spot']:>8.2f} {r['short_strike']:>7} "
                  f"${r['credit']:>6.3f} ${r['total_pnl']:>9,.2f} {vix_str:>6} {r['contracts']:>4}")
    print()

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. COMPARISON TABLE
    # ═══════════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("6. COMPARISON TABLE — ALL CONFIGS")
    print("=" * 80)

    configs = [
        config_original(),
        config_c3_only(),
        config_c3_tiered(),
        config_final(),
    ]

    results = []
    for cfg in configs:
        print(f"\n  Running: {cfg['name']}...")
        t, eq, dd = run_backtest(cfg, all_dates)
        s = compute_stats(t, eq, dd, cfg["name"])
        results.append(s)

    print()
    print(f"  {'Config':<20} {'P&L':>10} {'Trades':>7} {'WR':>6} {'MaxDD':>7} {'Sharpe':>7} "
          f"{'Calmar':>7} {'PF':>6} {'CAGR':>7} {'AvgCr':>7} {'Worst':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*7} {'-'*6} {'-'*7} {'-'*7} {'-'*7} {'-'*6} {'-'*7} {'-'*7} {'-'*10}")
    for s in results:
        if s["trades"] == 0:
            print(f"  {s['label']:<20} NO TRADES")
            continue
        print(f"  {s['label']:<20} ${s['pnl']:>9,.0f} {s['trades']:>7} {s['wr']:>5.1f}% "
              f"{s['max_dd_pct']:>6.2f}% {s['sharpe']:>7.2f} {s['calmar']:>7.2f} "
              f"{s['pf']:>6.2f} {s['cagr']:>6.1f}% ${s['avg_credit']:>6.3f} ${s['worst_trade']:>9,.0f}")

    print()
    print("=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)
