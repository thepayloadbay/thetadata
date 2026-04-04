#!/usr/bin/env python3
"""Sweep entry times × short distances to find optimal trade-off.

Tests combinations of:
  - Entry time: 15:40, 15:45, 15:50, 15:53, 15:55, 15:57
  - Short distance: 3, 5, 7, 10, 15, 20
  - Spread width: 5 (fixed)

For each combo, counts trades, win rate, total P&L, avg credit, max DD.
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

MARKET_HOLIDAYS = {
    "20220117", "20220221", "20220415", "20220530", "20220620", "20220704",
    "20220905", "20221124", "20221226",
    "20230102", "20230116", "20230220", "20230407", "20230529", "20230619",
    "20230704", "20230904", "20231123", "20231225",
    "20240101", "20240115", "20240219", "20240329", "20240527", "20240619",
    "20240704", "20240902", "20241128", "20241225",
    "20250101", "20250120", "20250217", "20250418", "20250526", "20250704",
    "20250901", "20251127", "20251225",
    "20260101", "20260119", "20260216", "20260403", "20260525", "20260703",
    "20260907", "20261126", "20261225",
}
EARLY_CLOSE_DAYS = {
    "20221125", "20231124", "20241129", "20241224",
    "20251128", "20251224", "20261127", "20261224",
}

SPREAD_WIDTH = 5.0
RISK_AMOUNT = 2500.0
COMMISSION = 0.50
STARTING_EQUITY = 50000.0

ENTRY_TIMES = ["15:40:00", "15:45:00", "15:50:00", "15:53:00", "15:55:00", "15:57:00"]
SHORT_DISTANCES = [3, 5, 7, 10, 15, 20]


def run_sweep():
    # Pre-load all data
    print("Loading data...")
    spx_cache = {}
    quotes_cache = {}

    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        if date_str < "20220103" or date_str > "20260325":
            continue
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        try:
            df = pd.read_parquet(fpath)
            if not df.empty:
                df["time_str"] = df["timestamp"].str[11:19]
                spx_cache[date_str] = df
        except Exception:
            pass

    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "option_quotes", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        if date_str < "20220103" or date_str > "20260325":
            continue
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        try:
            df = pd.read_parquet(fpath)
            if not df.empty:
                df["time_str"] = df["timestamp"].str[11:19]
                quotes_cache[date_str] = df
        except Exception:
            pass

    trade_dates = sorted(set(spx_cache.keys()) & set(quotes_cache.keys()))
    print(f"Loaded {len(trade_dates)} trading days with both SPX + quotes data")

    results = []

    for etime in ENTRY_TIMES:
        for dist in SHORT_DISTANCES:
            trades = 0
            wins = 0
            total_pnl = 0.0
            credits = []
            pnls = []
            eq = STARTING_EQUITY
            peak = eq
            max_dd = 0.0

            for date_str in trade_dates:
                spx_df = spx_cache[date_str]
                quotes_df = quotes_cache[date_str]

                # Get SPX at entry time
                bar = spx_df[spx_df["time_str"] == etime]
                if bar.empty:
                    continue
                spot = float(bar["close"].iloc[0])

                # Find short strike
                target = spot + dist
                short_strike = int(math.ceil(target / 5) * 5)
                long_strike = short_strike + int(SPREAD_WIDTH)

                # Get quotes at entry time
                entry_calls = quotes_df[(quotes_df["right"] == "C") & (quotes_df["time_str"] == etime)]
                short_q = entry_calls[entry_calls["strike"] == short_strike]
                long_q = entry_calls[entry_calls["strike"] == long_strike]

                if short_q.empty or long_q.empty:
                    continue

                credit = float(short_q["bid"].iloc[0]) - float(long_q["ask"].iloc[0])
                if credit <= 0:
                    continue

                # Position sizing
                max_loss_per = (SPREAD_WIDTH - credit) * 100.0
                qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

                # Get SPX at close (16:00 or 15:59)
                exit_bar = spx_df[spx_df["time_str"] == "16:00:00"]
                if exit_bar.empty:
                    exit_bar = spx_df[spx_df["time_str"] == "15:59:00"]
                if exit_bar.empty:
                    spx_exit = float(spx_df["close"].iloc[-1])
                else:
                    spx_exit = float(exit_bar["close"].iloc[0])

                # Settlement
                if spx_exit <= short_strike:
                    settlement = 0.0
                elif spx_exit >= long_strike:
                    settlement = SPREAD_WIDTH
                else:
                    settlement = spx_exit - short_strike

                gross_pnl = (credit - settlement) * qty * 100.0
                commission = 4 * qty * COMMISSION
                net_pnl = gross_pnl - commission

                trades += 1
                if net_pnl > 0:
                    wins += 1
                total_pnl += net_pnl
                credits.append(credit)
                pnls.append(net_pnl)

                eq += net_pnl
                peak = max(peak, eq)
                dd = eq - peak
                if dd < max_dd:
                    max_dd = dd

            wr = wins / trades * 100 if trades else 0
            avg_credit = np.mean(credits) if credits else 0
            avg_pnl = np.mean(pnls) if pnls else 0

            # Sharpe (trade-level, annualized roughly)
            if pnls and np.std(pnls) > 0:
                sharpe_trade = np.mean(pnls) / np.std(pnls) * math.sqrt(min(trades, 252))
            else:
                sharpe_trade = 0

            results.append({
                "entry_time": etime[:5],
                "distance": dist,
                "trades": trades,
                "win_rate": round(wr, 1),
                "total_pnl": round(total_pnl),
                "max_dd": round(max_dd),
                "avg_credit": round(avg_credit, 2),
                "avg_pnl": round(avg_pnl),
                "sharpe": round(sharpe_trade, 2),
            })

            print(f"  {etime[:5]} dist={dist:>2} | {trades:>4} trades | WR={wr:>5.1f}% | P&L=${total_pnl:>8,.0f} | DD=${max_dd:>7,.0f} | avg_cr=${avg_credit:.2f} | Sharpe={sharpe_trade:.2f}")

    # Summary table
    print("\n" + "=" * 100)
    print("SWEEP RESULTS — Entry Time × Short Distance")
    print("=" * 100)
    print(f"{'Time':>5} | {'Dist':>4} | {'Trades':>6} | {'WR':>6} | {'P&L':>10} | {'Max DD':>8} | {'Avg Cr':>7} | {'Avg P&L':>8} | {'Sharpe':>6}")
    print("-" * 100)
    for r in sorted(results, key=lambda x: -x["total_pnl"]):
        print(f"{r['entry_time']:>5} | {r['distance']:>4} | {r['trades']:>6} | {r['win_rate']:>5.1f}% | ${r['total_pnl']:>8,} | ${r['max_dd']:>7,} | ${r['avg_credit']:>6.2f} | ${r['avg_pnl']:>7,} | {r['sharpe']:>6.2f}")


if __name__ == "__main__":
    run_sweep()
