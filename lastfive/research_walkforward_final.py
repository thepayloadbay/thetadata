"""Walk-Forward Validation: COMBINED best config (Rule C3 + Tiered sizing + FOMC skip).

Tests 4 configs on TRAIN vs TEST:
  1. Baseline: Fixed dist=3, fixed $2500 risk
  2. Rule C3 only: VIX-adaptive distance, fixed $2500 risk
  3. Rule C3 + Tiered sizing: VIX-adaptive + credit-tiered risk
  4. Rule C3 + Tiered + FOMC skip

Split:
  TRAIN (in-sample):  2022-01-03 to 2024-12-31
  TEST  (out-of-sample): 2025-01-01 to 2026-03-25

Key question: does the combined C3+Tiered advantage persist out-of-sample?
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
BASE_RISK = 2500.0
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

FOMC_DATES = {
    "20220126","20220316","20220504","20220615","20220727","20220921",
    "20221102","20221214",
    "20230201","20230322","20230503","20230614","20230726","20230920",
    "20231101","20231213",
    "20240131","20240320","20240501","20240612","20240731","20240918",
    "20241107","20241218",
    "20250129","20250319","20250507","20250618","20250730","20250917",
    "20251029","20251210",
    "20260128","20260318","20260506","20260617","20260729","20260916",
    "20261028","20261209",
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
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return abs(end - start) / start * 100


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


# ── Distance rules ────────────────────────────────────────────────────
def rule_c3(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


def fixed_3(_vix: float) -> float:
    return 3.0


# ── Sizing rules ──────────────────────────────────────────────────────
def fixed_sizing(_credit: float) -> float:
    return BASE_RISK


def tiered_sizing(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── Simulate one side ─────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             sizing_fn=fixed_sizing) -> dict | None:
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

    risk_amount = sizing_fn(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk_amount / max_loss_per)) if max_loss_per > 0 else 1

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
            "short_strike": short_strike, "dist_used": short_strike - spot}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str,
            sizing_fn=fixed_sizing) -> dict | None:
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

    risk_amount = sizing_fn(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk_amount / max_loss_per)) if max_loss_per > 0 else 1

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
            "short_strike": short_strike, "dist_used": spot - short_strike}


# ── Compute metrics ──────────────────────────────────────────────────
def compute_metrics(day_pnls: list[float], trades: list[dict]) -> dict | None:
    if not trades or not day_pnls:
        return None

    arr = np.array(day_pnls)
    total_pnl = arr.sum()
    n_trades = len(trades)
    n_calls = sum(1 for t in trades if t["side"] == "CALL")
    n_puts = sum(1 for t in trades if t["side"] == "PUT")

    # Max drawdown
    equity = np.cumsum(arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    max_dd = (equity - peak).min()

    # Win rate
    n_wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = n_wins / n_trades * 100

    # Sharpe
    daily_rf = ANNUAL_RF / 252
    excess = arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0

    # Calmar
    years = len(arr) / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    pnl_per_trade = total_pnl / n_trades

    return {
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "n_trades": n_trades,
        "n_calls": n_calls,
        "n_puts": n_puts,
        "sharpe": sharpe,
        "calmar": calmar,
        "pnl_per_trade": pnl_per_trade,
        "n_days": len(arr),
    }


# ── Config definitions ───────────────────────────────────────────────
CONFIGS = {
    "1_Baseline": {
        "dist_fn": fixed_3,
        "sizing_fn": fixed_sizing,
        "fomc_skip": False,
        "label": "Fixed d=3, $2500",
    },
    "2_C3_only": {
        "dist_fn": rule_c3,
        "sizing_fn": fixed_sizing,
        "fomc_skip": False,
        "label": "C3 adaptive, $2500",
    },
    "3_C3+Tiered": {
        "dist_fn": rule_c3,
        "sizing_fn": tiered_sizing,
        "fomc_skip": False,
        "label": "C3 + Tiered sizing",
    },
    "4_C3+Tier+FOMC": {
        "dist_fn": rule_c3,
        "sizing_fn": tiered_sizing,
        "fomc_skip": True,
        "label": "C3 + Tiered + FOMC skip",
    },
}


# ── Main ──────────────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("WALK-FORWARD VALIDATION: Combined Best Config")
    print("  TRAIN: 2022-01-03 to 2024-12-31 (in-sample)")
    print("  TEST:  2025-01-01 to 2026-03-25 (out-of-sample)")
    print("=" * 120)

    # ── Load all eligible days ─────────────────────────────────────────
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if "20220103" <= ds <= "20260325":
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_high": 0, "afternoon_filter": 0}

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

        # VIX > 26 filter (all configs skip these)
        if vix1550 > 26:
            skipped["vix_high"] += 1
            continue

        # Afternoon return filter < 1.0%
        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # Spot at 15:55
        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
            skipped["no_spx"] += 1
            continue
        spot = float(spot_row["close"].iloc[0])

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
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Split into TRAIN / TEST ────────────────────────────────────────
    train_data = [d for d in day_data if d["date"] <= "20241231"]
    test_data = [d for d in day_data if d["date"] >= "20250101"]
    print(f"\n  TRAIN days: {len(train_data)}  ({train_data[0]['date']} to {train_data[-1]['date']})")
    print(f"  TEST  days: {len(test_data)}   ({test_data[0]['date']} to {test_data[-1]['date']})")

    # ── VIX distribution per period ────────────────────────────────────
    print("\n  VIX distribution:")
    brackets = [(0, 18), (18, 22), (22, 26)]
    for label, data in [("TRAIN", train_data), ("TEST", test_data)]:
        vvals = [d["vix1550"] for d in data]
        dist_str = "  ".join(
            f"VIX {lo}-{hi}: {sum(1 for v in vvals if lo <= v < hi)} ({sum(1 for v in vvals if lo <= v < hi)/len(vvals)*100:.0f}%)"
            for lo, hi in brackets
        )
        print(f"    {label}: {dist_str}")

    # ── Simulate each config on each period ────────────────────────────
    periods = {"TRAIN": train_data, "TEST": test_data, "FULL": day_data}
    results = {}   # (config_name, period_name) -> metrics
    trade_log = {} # (config_name, period_name) -> list of trades

    for cfg_name, cfg in CONFIGS.items():
        dist_fn = cfg["dist_fn"]
        sizing_fn = cfg["sizing_fn"]
        fomc_skip = cfg["fomc_skip"]

        for period_name, pdata in periods.items():
            trades = []
            day_pnls = []

            for dd in pdata:
                # FOMC skip
                if fomc_skip and dd["date"] in FOMC_DATES:
                    continue

                dist = dist_fn(dd["vix1550"])
                day_pnl = 0.0
                day_trades = []

                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                              "15:55:00", sizing_fn)
                if ct is not None:
                    ct["date"] = dd["date"]
                    ct["vix"] = dd["vix1550"]
                    ct["dist"] = dist
                    day_trades.append(ct)
                    day_pnl += ct["pnl"]

                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                             "15:55:00", sizing_fn)
                if pt is not None:
                    pt["date"] = dd["date"]
                    pt["vix"] = dd["vix1550"]
                    pt["dist"] = dist
                    day_trades.append(pt)
                    day_pnl += pt["pnl"]

                if day_trades:
                    day_pnls.append(day_pnl)
                    trades.extend(day_trades)

            key = (cfg_name, period_name)
            trade_log[key] = trades
            results[key] = compute_metrics(day_pnls, trades)

    # ── Also compute year-by-year breakdown ────────────────────────────
    years_all = sorted(set(d["date"][:4] for d in day_data))
    yearly = {}  # (config_name, year) -> metrics

    for cfg_name, cfg in CONFIGS.items():
        dist_fn = cfg["dist_fn"]
        sizing_fn = cfg["sizing_fn"]
        fomc_skip = cfg["fomc_skip"]

        for yr in years_all:
            yr_data = [d for d in day_data if d["date"][:4] == yr]
            trades = []
            day_pnls = []

            for dd in yr_data:
                if fomc_skip and dd["date"] in FOMC_DATES:
                    continue

                dist = dist_fn(dd["vix1550"])
                day_pnl = 0.0
                day_trades = []

                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                              "15:55:00", sizing_fn)
                if ct is not None:
                    ct["date"] = dd["date"]
                    ct["vix"] = dd["vix1550"]
                    ct["dist"] = dist
                    day_trades.append(ct)
                    day_pnl += ct["pnl"]

                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                             "15:55:00", sizing_fn)
                if pt is not None:
                    pt["date"] = dd["date"]
                    pt["vix"] = dd["vix1550"]
                    pt["dist"] = dist
                    day_trades.append(pt)
                    day_pnl += pt["pnl"]

                if day_trades:
                    day_pnls.append(day_pnl)
                    trades.extend(day_trades)

            yearly[(cfg_name, yr)] = compute_metrics(day_pnls, trades)

    # ══════════════════════════════════════════════════════════════════════
    #  PRINT RESULTS
    # ══════════════════════════════════════════════════════════════════════
    cfg_names = list(CONFIGS.keys())

    print("\n" + "=" * 130)
    print("MAIN COMPARISON: 4 CONFIGS x TRAIN / TEST / FULL")
    print("=" * 130)

    header = (
        f"{'Config':<20} {'Period':<6} {'P&L':>10} {'Max DD':>10} {'WR%':>6} "
        f"{'Trades':>7} {'C/P':>9} {'Sharpe':>7} {'Calmar':>8} {'$/Trade':>9}"
    )
    print(header)
    print("-" * 130)

    for cfg_name in cfg_names:
        for period_name in ["TRAIN", "TEST", "FULL"]:
            r = results[(cfg_name, period_name)]
            if r is None:
                print(f"{cfg_name:<20} {period_name:<6}  NO TRADES")
                continue
            print(
                f"{cfg_name:<20} {period_name:<6} "
                f"${r['total_pnl']:>8,.0f} ${r['max_dd']:>8,.0f} "
                f"{r['wr']:>5.1f}% "
                f"{r['n_trades']:>7} {r['n_calls']:>4}/{r['n_puts']:<4} "
                f"{r['sharpe']:>7.2f} {r['calmar']:>8.2f} ${r['pnl_per_trade']:>7.0f}"
            )
        print("-" * 130)

    # ── DELTA vs Baseline per period ──────────────────────────────────
    print("\nDELTA vs BASELINE (each config minus Baseline):")
    print("-" * 100)
    print(f"{'Config':<20} {'Period':<6} {'dP&L':>10} {'dMaxDD':>10} {'dWR':>7} {'dSharpe':>8} {'dCalmar':>9} {'d$/Trade':>10}")
    print("-" * 100)

    for cfg_name in cfg_names[1:]:  # skip baseline itself
        for period_name in ["TRAIN", "TEST"]:
            rc = results[(cfg_name, period_name)]
            rb = results[("1_Baseline", period_name)]
            if rc is None or rb is None:
                continue
            dp = rc["total_pnl"] - rb["total_pnl"]
            ddd = rc["max_dd"] - rb["max_dd"]
            dwr = rc["wr"] - rb["wr"]
            ds = rc["sharpe"] - rb["sharpe"]
            dc = rc["calmar"] - rb["calmar"]
            dpt = rc["pnl_per_trade"] - rb["pnl_per_trade"]
            print(
                f"{cfg_name:<20} {period_name:<6} "
                f"{'$':>1}{dp:>+9,.0f} {'$':>1}{ddd:>+9,.0f} {dwr:>+6.1f}% "
                f"{ds:>+8.2f} {dc:>+9.2f} {'$':>1}{dpt:>+9.0f}"
            )
        print("-" * 100)

    # ── Year-by-year P&L ──────────────────────────────────────────────
    print("\nYEAR-BY-YEAR P&L:")
    print("-" * 100)
    yr_header = f"{'Config':<20}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    yr_header += f" {'TOTAL':>12}"
    print(yr_header)
    print("-" * 100)

    for cfg_name in cfg_names:
        row = f"{cfg_name:<20}"
        total = 0
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" ${m['total_pnl']:>10,.0f}"
                total += m['total_pnl']
            else:
                row += f" {'N/A':>12}"
        row += f" ${total:>10,.0f}"
        print(row)
    print("-" * 100)

    # ── Year-by-year Sharpe ───────────────────────────────────────────
    print("\nYEAR-BY-YEAR SHARPE:")
    print("-" * 100)
    yr_header = f"{'Config':<20}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 100)

    for cfg_name in cfg_names:
        row = f"{cfg_name:<20}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['sharpe']:>12.2f}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 100)

    # ── Year-by-year WR ──────────────────────────────────────────────
    print("\nYEAR-BY-YEAR WIN RATE (%):")
    print("-" * 100)
    yr_header = f"{'Config':<20}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 100)

    for cfg_name in cfg_names:
        row = f"{cfg_name:<20}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['wr']:>11.1f}%"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 100)

    # ── Year-by-year Trades ──────────────────────────────────────────
    print("\nYEAR-BY-YEAR TRADES:")
    print("-" * 100)
    yr_header = f"{'Config':<20}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 100)

    for cfg_name in cfg_names:
        row = f"{cfg_name:<20}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" {m['n_trades']:>12}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 100)

    # ── Year-by-year Max DD ──────────────────────────────────────────
    print("\nYEAR-BY-YEAR MAX DRAWDOWN:")
    print("-" * 100)
    yr_header = f"{'Config':<20}"
    for yr in years_all:
        yr_header += f" {yr:>12}"
    print(yr_header)
    print("-" * 100)

    for cfg_name in cfg_names:
        row = f"{cfg_name:<20}"
        for yr in years_all:
            m = yearly.get((cfg_name, yr))
            if m:
                row += f" ${m['max_dd']:>10,.0f}"
            else:
                row += f" {'N/A':>12}"
        print(row)
    print("-" * 100)

    # ── OOS retention analysis ────────────────────────────────────────
    print("\n" + "=" * 130)
    print("OOS RETENTION ANALYSIS")
    print("=" * 130)
    print("(Does the improvement over Baseline seen in TRAIN persist in TEST?)\n")

    train_days = len(train_data)
    test_days = len(test_data)
    scale = test_days / train_days  # to normalize P&L by period length

    rb_train = results[("1_Baseline", "TRAIN")]
    rb_test = results[("1_Baseline", "TEST")]

    print(f"{'Config':<20} {'TRAIN dP&L':>12} {'TEST dP&L':>12} "
          f"{'TRAIN dSharpe':>14} {'TEST dSharpe':>13} {'OOS P&L Ret%':>13} {'Verdict':>12}")
    print("-" * 100)

    for cfg_name in cfg_names[1:]:
        rc_train = results[(cfg_name, "TRAIN")]
        rc_test = results[(cfg_name, "TEST")]
        if rc_train is None or rc_test is None:
            continue

        dp_train = rc_train["total_pnl"] - rb_train["total_pnl"]
        dp_test = rc_test["total_pnl"] - rb_test["total_pnl"]
        ds_train = rc_train["sharpe"] - rb_train["sharpe"]
        ds_test = rc_test["sharpe"] - rb_test["sharpe"]

        # Normalize train delta to test period length for retention calc
        dp_train_norm = dp_train * scale
        retention = (dp_test / dp_train_norm * 100) if dp_train_norm != 0 else 0

        if dp_test > 0 and ds_test > 0:
            verdict = "VALIDATED"
        elif dp_test > 0:
            verdict = "PARTIAL"
        else:
            verdict = "OVERFIT"

        print(
            f"{cfg_name:<20} ${dp_train:>+10,.0f} ${dp_test:>+10,.0f} "
            f"{ds_train:>+13.2f} {ds_test:>+12.2f} {retention:>12.0f}% {verdict:>12}"
        )
    print("-" * 100)

    # ── FOMC impact analysis ──────────────────────────────────────────
    print("\nFOMC SKIP IMPACT:")
    print("-" * 80)
    # Compare config 3 (no FOMC skip) vs 4 (with FOMC skip)
    for period_name in ["TRAIN", "TEST", "FULL"]:
        r3 = results[("3_C3+Tiered", period_name)]
        r4 = results[("4_C3+Tier+FOMC", period_name)]
        if r3 and r4:
            dp = r4["total_pnl"] - r3["total_pnl"]
            dt = r4["n_trades"] - r3["n_trades"]
            ds = r4["sharpe"] - r3["sharpe"]
            print(f"  {period_name}: FOMC skip -> dP&L=${dp:+,.0f}, dTrades={dt:+d}, dSharpe={ds:+.2f}")

    # ══════════════════════════════════════════════════════════════════════
    #  VERDICT
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("FINAL VERDICT")
    print("=" * 130)

    # The key config is 3_C3+Tiered
    r_key_train = results[("3_C3+Tiered", "TRAIN")]
    r_key_test = results[("3_C3+Tiered", "TEST")]
    r_base_train = results[("1_Baseline", "TRAIN")]
    r_base_test = results[("1_Baseline", "TEST")]

    dp_train = r_key_train["total_pnl"] - r_base_train["total_pnl"]
    dp_test = r_key_test["total_pnl"] - r_base_test["total_pnl"]
    ds_train = r_key_train["sharpe"] - r_base_train["sharpe"]
    ds_test = r_key_test["sharpe"] - r_base_test["sharpe"]

    print(f"\n  C3+Tiered vs Baseline:")
    print(f"    TRAIN: P&L advantage = ${dp_train:+,.0f}, Sharpe advantage = {ds_train:+.2f}")
    print(f"    TEST:  P&L advantage = ${dp_test:+,.0f}, Sharpe advantage = {ds_test:+.2f}")

    print(f"\n  C3+Tiered absolute performance:")
    print(f"    TRAIN: ${r_key_train['total_pnl']:,.0f} P&L, Sharpe {r_key_train['sharpe']:.2f}, "
          f"DD ${r_key_train['max_dd']:,.0f}, WR {r_key_train['wr']:.1f}%")
    print(f"    TEST:  ${r_key_test['total_pnl']:,.0f} P&L, Sharpe {r_key_test['sharpe']:.2f}, "
          f"DD ${r_key_test['max_dd']:,.0f}, WR {r_key_test['wr']:.1f}%")

    if dp_test > 0 and ds_test > 0:
        print("\n  >>> VALIDATED: C3+Tiered advantage PERSISTS out-of-sample.")
        print("      Both P&L and Sharpe improvement hold in the TEST period.")
    elif dp_test > 0:
        print("\n  >>> PARTIALLY VALIDATED: P&L advantage holds OOS, but Sharpe does not.")
    else:
        print("\n  >>> NOT VALIDATED: C3+Tiered advantage does NOT persist OOS.")

    # FOMC verdict
    r_fomc_test = results[("4_C3+Tier+FOMC", "TEST")]
    if r_fomc_test:
        dp_fomc = r_fomc_test["total_pnl"] - r_key_test["total_pnl"]
        print(f"\n  FOMC skip adds ${dp_fomc:+,.0f} in TEST period. "
              f"{'Worth keeping.' if dp_fomc > 0 else 'Not helpful OOS.'}")

    print("\nDone.")


if __name__ == "__main__":
    main()
