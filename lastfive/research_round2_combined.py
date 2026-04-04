"""Research Round 2: Combined best findings from Round 1.

Configs A-F progressively layer improvements:
  A: Current baseline (fixed dist=3, VIX<=26, afternoon_ret<1.0%)
  B: A + skip PUT if afternoon_ret < -1.0% (symmetric downside)
  C: Rule C3 adaptive distance (VIX<18->d=0, 18-22->d=3, 22-26->d=5)
  D: C + symmetric afternoon filters (skip CALL if aft>=1.0%, skip PUT if aft<=-1.0%)
  E: D + skip FOMC days
  F: E + skip PUT if mom_30 < -0.10%
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
RISK_AMOUNT = 2500.0
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
    "20221102","20221214","20230201","20230322","20230503","20230614",
    "20230726","20230920","20231101","20231213","20240131","20240320",
    "20240501","20240612","20240731","20240918","20241107","20241218",
    "20250129","20250319","20250507","20250618","20250730","20250917",
    "20251029","20251210","20260128","20260318","20260506","20260617",
    "20260729","20260916","20261028","20261209",
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
def get_vix_at(vix_df: pd.DataFrame, hhmm: str) -> float | None:
    rows = vix_df[vix_df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """Signed afternoon return: (last - first) / first * 100."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_afternoon_return_abs(spx_df: pd.DataFrame) -> float | None:
    """Absolute afternoon return for the baseline filter."""
    ret = compute_afternoon_return(spx_df)
    if ret is None:
        return None
    return abs(ret)


def compute_mom_30(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 15:25 to 15:54 (signed)."""
    bars = spx_df[(spx_df["time_str"] >= "15:25:00") & (spx_df["time_str"] <= "15:54:00")]
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
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


# ── Strike distance rules ──────────────────────────────────────────────
def fixed_3(vix: float) -> float:
    return 3.0


def rule_c3(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    target = spot + dist
    short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike + int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "C"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted(s for s in available if s >= target)
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

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

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
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
    target = spot - dist
    short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike - int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == "P"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted((s for s in available if s <= target), reverse=True)
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

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

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


# ── Config definitions ──────────────────────────────────────────────────
def make_configs():
    """Return list of (name, config_dict) for each test."""
    return [
        ("A_baseline", {
            "dist_fn": fixed_3,
            "skip_call_fn": lambda aft, mom30, ds: False,
            "skip_put_fn": lambda aft, mom30, ds: False,
            "aft_filter": "abs",       # use abs(afternoon_ret) < 1.0% to skip BOTH sides
            "aft_threshold": 1.0,
            "vix_max": 26.0,
            "skip_fomc": False,
        }),
        ("B_put_aft", {
            "dist_fn": fixed_3,
            "skip_call_fn": lambda aft, mom30, ds: False,
            "skip_put_fn": lambda aft, mom30, ds: (aft is not None and aft <= -1.0),
            "aft_filter": "abs",
            "aft_threshold": 1.0,
            "vix_max": 26.0,
            "skip_fomc": False,
        }),
        ("C_ruleC3", {
            "dist_fn": rule_c3,
            "skip_call_fn": lambda aft, mom30, ds: False,
            "skip_put_fn": lambda aft, mom30, ds: False,
            "aft_filter": "abs",
            "aft_threshold": 1.0,
            "vix_max": 26.0,
            "skip_fomc": False,
        }),
        ("D_C3+symAft", {
            "dist_fn": rule_c3,
            "skip_call_fn": lambda aft, mom30, ds: (aft is not None and aft >= 1.0),
            "skip_put_fn": lambda aft, mom30, ds: (aft is not None and aft <= -1.0),
            "aft_filter": "none",       # side-specific filters handle it
            "aft_threshold": None,
            "vix_max": 26.0,
            "skip_fomc": False,
        }),
        ("E_D+FOMC", {
            "dist_fn": rule_c3,
            "skip_call_fn": lambda aft, mom30, ds: (aft is not None and aft >= 1.0),
            "skip_put_fn": lambda aft, mom30, ds: (aft is not None and aft <= -1.0),
            "aft_filter": "none",
            "aft_threshold": None,
            "vix_max": 26.0,
            "skip_fomc": True,
        }),
        ("F_E+putMom", {
            "dist_fn": rule_c3,
            "skip_call_fn": lambda aft, mom30, ds: (aft is not None and aft >= 1.0),
            "skip_put_fn": lambda aft, mom30, ds: (
                (aft is not None and aft <= -1.0) or
                (mom30 is not None and mom30 < -0.10)
            ),
            "aft_filter": "none",
            "aft_threshold": None,
            "vix_max": 26.0,
            "skip_fomc": True,
        }),
    ]


# ── Main simulation ────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("ROUND 2: COMBINED CONFIGS (A through F)")
    print("=" * 120)

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

    # Pre-load day data
    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = 0

    for i, ds in enumerate(all_dates):
        if i % 200 == 0:
            print(f"  Processing {i}/{len(all_dates)}...")

        if ds in MARKET_HOLIDAYS or ds in EARLY_CLOSE_DAYS:
            skipped += 1
            continue

        spx_df = load_spx(ds)
        if spx_df is None:
            continue

        vix_df = load_vix(ds)
        if vix_df is None:
            continue

        vix1550 = get_vix_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0:
            continue

        # Spot at 15:55
        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
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

        # Afternoon return (signed)
        aft_ret = compute_afternoon_return(spx_df)
        aft_ret_abs = abs(aft_ret) if aft_ret is not None else None

        # 30-min momentum
        mom30 = compute_mom_30(spx_df)

        opts_df = load_options(ds)
        if opts_df is None:
            continue

        day_data.append({
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "aft_ret": aft_ret,
            "aft_ret_abs": aft_ret_abs,
            "mom30": mom30,
            "opts_df": opts_df,
        })

    print(f"\n  Total eligible days loaded: {len(day_data)}")

    # ── Run each config ───────────────────────────────────────────────
    configs = make_configs()
    results = {}

    for cfg_name, cfg in configs:
        print(f"\n  Running {cfg_name}...", end=" ", flush=True)

        call_trades = []
        put_trades = []
        day_pnls = []
        trade_pnls = []  # individual trade P&Ls for trade-level Sharpe
        trade_credits = []

        for dd in day_data:
            ds = dd["date"]
            vix = dd["vix1550"]
            aft = dd["aft_ret"]
            aft_abs = dd["aft_ret_abs"]
            mom30 = dd["mom30"]

            # VIX filter
            if vix > cfg["vix_max"]:
                continue

            # FOMC skip
            if cfg["skip_fomc"] and ds in FOMC_DATES:
                continue

            # Global afternoon filter (Config A, B, C use abs < 1.0%)
            if cfg["aft_filter"] == "abs":
                if aft_abs is not None and aft_abs >= cfg["aft_threshold"]:
                    continue

            dist = cfg["dist_fn"](vix)
            day_pnl = 0.0
            has_trade = False

            # ── CALL SIDE ──
            skip_call = cfg["skip_call_fn"](aft, mom30, ds)
            if not skip_call:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct is not None:
                    call_trades.append(ct)
                    day_pnl += ct["pnl"]
                    trade_pnls.append(ct["pnl"])
                    trade_credits.append(ct["credit"])
                    has_trade = True

            # ── PUT SIDE ──
            skip_put = cfg["skip_put_fn"](aft, mom30, ds)
            if not skip_put:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt is not None:
                    put_trades.append(pt)
                    day_pnl += pt["pnl"]
                    trade_pnls.append(pt["pnl"])
                    trade_credits.append(pt["credit"])
                    has_trade = True

            if has_trade:
                day_pnls.append(day_pnl)

        # Compute stats
        all_trades = call_trades + put_trades
        n_trades = len(all_trades)
        n_calls = len(call_trades)
        n_puts = len(put_trades)

        if n_trades == 0:
            results[cfg_name] = None
            print("NO TRADES")
            continue

        pnl_arr = np.array(trade_pnls)
        day_pnl_arr = np.array(day_pnls)
        total_pnl = pnl_arr.sum()

        # Max drawdown on daily equity curve
        equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_curve = equity - peak
        max_dd = dd_curve.min()

        # Win rate
        n_wins = (pnl_arr > 0).sum()
        wr = n_wins / n_trades * 100

        # Sharpe: trade-level, annualized by sqrt(min(n, 252))
        trade_mean = pnl_arr.mean()
        trade_std = pnl_arr.std()
        ann_factor = np.sqrt(min(n_trades, 252))
        sharpe = (trade_mean / trade_std) * ann_factor if trade_std > 0 else 0

        # Calmar
        n_days = len(day_pnl_arr)
        years = n_days / 252
        annual_pnl = total_pnl / years if years > 0 else 0
        calmar = annual_pnl / abs(max_dd) if max_dd != 0 else 0

        # Profit factor
        gross_wins = pnl_arr[pnl_arr > 0].sum()
        gross_losses = abs(pnl_arr[pnl_arr <= 0].sum())
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        # Avg credit
        avg_credit = np.mean(trade_credits) if trade_credits else 0

        # Call/Put breakdown
        call_pnl = sum(t["pnl"] for t in call_trades)
        put_pnl = sum(t["pnl"] for t in put_trades)

        results[cfg_name] = {
            "trades": n_trades,
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_pnl": total_pnl,
            "call_pnl": call_pnl,
            "put_pnl": put_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "pf": pf,
            "avg_credit": avg_credit,
            "day_pnls": day_pnl_arr,
        }
        print(f"done  P&L=${total_pnl:+,.0f}  Sharpe={sharpe:.2f}  Trades={n_trades}")

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 150)
    print("ROUND 2 COMBINED CONFIGS — COMPARISON TABLE")
    print("=" * 150)
    print(f"{'Config':<16} {'P&L':>10} {'MaxDD':>9} {'WR%':>6} {'Trades':>7} "
          f"{'(C/P)':>9} {'Sharpe':>7} {'Calmar':>8} {'PF':>6} {'AvgCr':>6} "
          f"{'Call$':>9} {'Put$':>9}")
    print("-" * 150)

    for cfg_name, _ in configs:
        r = results.get(cfg_name)
        if r is None:
            print(f"{cfg_name:<16}  NO TRADES")
            continue
        print(
            f"{cfg_name:<16} "
            f"${r['total_pnl']:>8,.0f} "
            f"${r['max_dd']:>7,.0f} "
            f"{r['wr']:>5.1f}% "
            f"{r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"{r['sharpe']:>7.2f} "
            f"{r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} "
            f"${r['avg_credit']:>4.2f} "
            f"${r['call_pnl']:>7,.0f} "
            f"${r['put_pnl']:>7,.0f}"
        )

    # ── Delta vs baseline ───────────────────────────────────────────────
    baseline = results.get("A_baseline")
    if baseline:
        print("\n" + "-" * 100)
        print("DELTA vs A_baseline:")
        print(f"{'Config':<16} {'dP&L':>10} {'dMaxDD':>9} {'dWR':>6} {'dTrades':>8} "
              f"{'dSharpe':>8} {'dCalmar':>9} {'dPF':>7}")
        print("-" * 100)
        for cfg_name, _ in configs:
            if cfg_name == "A_baseline":
                continue
            r = results.get(cfg_name)
            if r is None:
                continue
            print(
                f"{cfg_name:<16} "
                f"{r['total_pnl'] - baseline['total_pnl']:>+10,.0f} "
                f"{r['max_dd'] - baseline['max_dd']:>+9,.0f} "
                f"{r['wr'] - baseline['wr']:>+5.1f}% "
                f"{r['trades'] - baseline['trades']:>+8} "
                f"{r['sharpe'] - baseline['sharpe']:>+8.2f} "
                f"{r['calmar'] - baseline['calmar']:>+9.2f} "
                f"{r['pf'] - baseline['pf']:>+7.2f}"
            )

    # ── Year-by-year P&L ────────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("YEAR-BY-YEAR P&L BREAKDOWN")
    print("=" * 120)

    # Rerun per year for each config
    years_set = sorted(set(dd["date"][:4] for dd in day_data))
    header = f"{'Config':<16}"
    for yr in years_set:
        header += f" {yr:>10}"
    print(header)
    print("-" * 120)

    for cfg_name, cfg in configs:
        yr_pnl = {yr: 0.0 for yr in years_set}
        for dd in day_data:
            ds = dd["date"]
            vix = dd["vix1550"]
            aft = dd["aft_ret"]
            aft_abs = dd["aft_ret_abs"]
            mom30 = dd["mom30"]

            if vix > cfg["vix_max"]:
                continue
            if cfg["skip_fomc"] and ds in FOMC_DATES:
                continue
            if cfg["aft_filter"] == "abs":
                if aft_abs is not None and aft_abs >= cfg["aft_threshold"]:
                    continue

            dist = cfg["dist_fn"](vix)
            day_pnl = 0.0

            skip_call = cfg["skip_call_fn"](aft, mom30, ds)
            if not skip_call:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct:
                    day_pnl += ct["pnl"]

            skip_put = cfg["skip_put_fn"](aft, mom30, ds)
            if not skip_put:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt:
                    day_pnl += pt["pnl"]

            yr_pnl[ds[:4]] += day_pnl

        row = f"{cfg_name:<16}"
        for yr in years_set:
            row += f" ${yr_pnl[yr]:>8,.0f}"
        print(row)

    print("-" * 120)

    # ── Config descriptions ─────────────────────────────────────────────
    print("\nConfig descriptions:")
    print("  A_baseline   : Fixed dist=3, VIX<=26, |afternoon_ret|<1.0% (both sides)")
    print("  B_put_aft    : A + skip PUT if afternoon_ret <= -1.0%")
    print("  C_ruleC3     : VIX<18->d=0, 18-22->d=3, 22-26->d=5; |afternoon_ret|<1.0%")
    print("  D_C3+symAft  : C3 + skip CALL if aft>=1.0%, skip PUT if aft<=-1.0%")
    print("  E_D+FOMC     : D + skip FOMC days entirely")
    print("  F_E+putMom   : E + also skip PUT if mom_30 < -0.10%")

    print("\nDone.")


if __name__ == "__main__":
    main()
