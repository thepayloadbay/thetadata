"""Research: Sweep VIX ATM threshold for Rule C3.

Rule C3 uses VIX(15:50) < 18 as the cutoff for ATM (dist=0).
This sweep tests cutoffs from 14 to 22 to confirm 18 is on a plateau
(not an overfitted peak).

For each cutoff X:
  VIX < X      -> dist=0 (ATM)
  X <= VIX < 22 -> dist=3
  22 <= VIX < 26 -> dist=5
  VIX >= 26    -> skip

Baseline: Fixed dist=3 (no VIX adaptive).
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
RISK_AMOUNT = 2500.0
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


# ── Simulate one side ─────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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
            "short_strike": short_strike, "dist_used": short_strike - spot}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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
            "short_strike": short_strike, "dist_used": spot - short_strike}


# ── Main ──────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("VIX ATM Threshold Sweep — Rule C3 Cutoff Sensitivity")
    print("  VIX < X -> dist=0 (ATM) | X <= VIX < 22 -> dist=3 | 22 <= VIX < 26 -> dist=5 | VIX >= 26 -> skip")
    print("  + Fixed dist=3 baseline (no VIX adaptive)")
    print("=" * 110)

    # ── Load all eligible days ──────────────────────────────────────────
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    print("Pre-loading day data...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_high": 0, "afternoon_filter": 0}

    for i, ds in enumerate(all_dates):
        if i % 200 == 0:
            print(f"  {i}/{len(all_dates)}...")

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

        # VIX > 26 always skipped
        if vix1550 > 26:
            skipped["vix_high"] += 1
            continue

        # Afternoon return filter
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
            "date": ds, "spot": spot, "spx_exit": spx_exit,
            "vix1550": vix1550, "opts_df": opts_df,
        })

    print(f"\n  Eligible days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── VIX distribution ───────────────────────────────────────────────
    vix_vals = [dd["vix1550"] for dd in day_data]
    print(f"\n  VIX(15:50) distribution:")
    for lo, hi in [(0,14),(14,15),(15,16),(16,17),(17,18),(18,19),(19,20),(20,22),(22,26)]:
        cnt = sum(1 for v in vix_vals if lo <= v < hi)
        print(f"    VIX {lo:>2}-{hi:<2}: {cnt:>4} days ({cnt/len(vix_vals)*100:.1f}%)")

    # ── Build configs ──────────────────────────────────────────────────
    # Cutoff sweep: 14..22
    cutoffs = list(range(14, 23))

    def make_rule(cutoff):
        def rule(vix):
            if vix < cutoff:
                return 0.0
            elif vix < 22:
                return 3.0
            else:
                return 5.0
        return rule

    configs = {}
    # Baseline: fixed dist=3
    configs["Fixed_3"] = lambda vix: 3.0
    for c in cutoffs:
        configs[f"VIX<{c}"] = make_rule(c)

    # ── Run each config ────────────────────────────────────────────────
    results = {}
    for cfg_name, rule_fn in configs.items():
        call_trades = []
        put_trades = []
        day_pnls = []

        for dd in day_data:
            dist = rule_fn(dd["vix1550"])
            day_pnl = 0.0

            ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if ct is not None:
                call_trades.append(ct)
                day_pnl += ct["pnl"]

            pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
            if pt is not None:
                put_trades.append(pt)
                day_pnl += pt["pnl"]

            if ct is not None or pt is not None:
                day_pnls.append(day_pnl)

        all_trades = call_trades + put_trades
        n = len(all_trades)
        if n == 0:
            results[cfg_name] = None
            continue

        pnl_arr = np.array(day_pnls)
        total_pnl = pnl_arr.sum()

        # Max drawdown
        equity = np.cumsum(pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        max_dd = (equity - peak).min()

        # Win rate
        n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
        wr = n_wins / n * 100

        # Sharpe
        daily_rf = ANNUAL_RF / 252
        excess = pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = excess.mean() / pnl_arr.std() * np.sqrt(252) if pnl_arr.std() > 0 else 0

        # Calmar
        years = len(pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Days using dist=0 (ATM)
        n_atm = sum(1 for dd in day_data if rule_fn(dd["vix1550"]) == 0.0)

        results[cfg_name] = {
            "trades": n,
            "n_calls": len(call_trades),
            "n_puts": len(put_trades),
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "n_atm_days": n_atm,
            "n_trading_days": len(day_pnls),
        }

    # ── Print results ──────────────────────────────────────────────────
    print("\n" + "=" * 130)
    print("RESULTS — VIX ATM Threshold Sweep")
    print("=" * 130)
    print(f"{'Config':<10} {'Trades':>7} {'C/P':>9} {'Total P&L':>12} {'Max DD':>10} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'ATM days':>9} {'TradeDays':>10}")
    print("-" * 130)

    # Print baseline first
    r = results.get("Fixed_3")
    if r:
        print(f"{'Fixed_3':<10} {r['trades']:>7} {r['n_calls']:>4}/{r['n_puts']:<4} "
              f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
              f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
              f"{r['n_atm_days']:>9} {r['n_trading_days']:>10}")
    print("-" * 130)

    # Then cutoff sweep sorted
    for c in cutoffs:
        name = f"VIX<{c}"
        r = results.get(name)
        if r is None:
            print(f"{name:<10}  NO TRADES")
            continue
        marker = " <<<" if c == 18 else ""
        print(f"{name:<10} {r['trades']:>7} {r['n_calls']:>4}/{r['n_puts']:<4} "
              f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
              f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
              f"{r['n_atm_days']:>9} {r['n_trading_days']:>10}{marker}")

    print("=" * 130)

    # ── Delta vs baseline ──────────────────────────────────────────────
    baseline = results.get("Fixed_3")
    if baseline:
        print(f"\n{'Config':<10} {'dP&L':>10} {'dDD':>10} {'dSharpe':>8} {'dCalmar':>8}")
        print("-" * 50)
        for c in cutoffs:
            name = f"VIX<{c}"
            r = results.get(name)
            if r is None:
                continue
            dp = r["total_pnl"] - baseline["total_pnl"]
            dd = r["max_dd"] - baseline["max_dd"]
            ds = r["sharpe"] - baseline["sharpe"]
            dc = r["calmar"] - baseline["calmar"]
            marker = " <<<" if c == 18 else ""
            print(f"{name:<10} {dp:>+10,.0f} {dd:>+10,.0f} {ds:>+8.2f} {dc:>+8.2f}{marker}")
        print("-" * 50)

    # ── Plateau analysis ───────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("PLATEAU ANALYSIS — Is VIX<18 on a plateau or an overfitted peak?")
    print("=" * 80)

    sweep_pnls = {}
    sweep_sharpes = {}
    sweep_calmars = {}
    for c in cutoffs:
        name = f"VIX<{c}"
        r = results.get(name)
        if r:
            sweep_pnls[c] = r["total_pnl"]
            sweep_sharpes[c] = r["sharpe"]
            sweep_calmars[c] = r["calmar"]

    # Check neighbors of 18
    target = 18
    neighbors = [target - 2, target - 1, target, target + 1, target + 2]
    print(f"\nNeighborhood of VIX<{target}:")
    print(f"  {'Cutoff':<8} {'P&L':>10} {'Sharpe':>8} {'Calmar':>8}")
    for c in neighbors:
        if c in sweep_pnls:
            marker = " <<<" if c == target else ""
            print(f"  VIX<{c:<4} ${sweep_pnls[c]:>9,.0f} {sweep_sharpes[c]:>8.2f} {sweep_calmars[c]:>8.2f}{marker}")

    # Determine plateau vs peak
    if target in sweep_pnls:
        t_pnl = sweep_pnls[target]
        t_sharpe = sweep_sharpes[target]

        # Check if neighbors within 10% of P&L
        close_neighbors = []
        for c in [target - 1, target + 1]:
            if c in sweep_pnls:
                pct_diff = abs(sweep_pnls[c] - t_pnl) / abs(t_pnl) * 100
                close_neighbors.append((c, pct_diff))

        # Also check broader range
        broad_neighbors = []
        for c in [target - 2, target - 1, target + 1, target + 2]:
            if c in sweep_pnls:
                pct_diff = abs(sweep_pnls[c] - t_pnl) / abs(t_pnl) * 100
                broad_neighbors.append((c, pct_diff))

        avg_pct_diff_close = np.mean([d for _, d in close_neighbors]) if close_neighbors else 999
        avg_pct_diff_broad = np.mean([d for _, d in broad_neighbors]) if broad_neighbors else 999

        print(f"\n  Avg P&L deviation from VIX<{target}:")
        print(f"    Immediate neighbors (+/-1): {avg_pct_diff_close:.1f}%")
        print(f"    Broad neighbors (+/-2):     {avg_pct_diff_broad:.1f}%")

        if avg_pct_diff_close < 10:
            verdict = "PLATEAU — neighbors within 10% of P&L. VIX<18 is NOT an overfitted peak."
        elif avg_pct_diff_close < 20:
            verdict = "MILD PLATEAU — neighbors within 20%. Reasonably stable but not perfectly flat."
        else:
            verdict = "PEAK — neighbors diverge >20%. VIX<18 may be overfitted."

        print(f"\n  >>> VERDICT: {verdict}")

        # Also check Sharpe plateau
        sharpe_diffs = []
        for c in [target - 1, target + 1]:
            if c in sweep_sharpes:
                sharpe_diffs.append(abs(sweep_sharpes[c] - t_sharpe))
        avg_sharpe_diff = np.mean(sharpe_diffs) if sharpe_diffs else 999
        print(f"  >>> Sharpe deviation from neighbors: {avg_sharpe_diff:.2f}")

    # Best cutoff
    if sweep_pnls:
        best_pnl_cutoff = max(sweep_pnls, key=sweep_pnls.get)
        best_sharpe_cutoff = max(sweep_sharpes, key=sweep_sharpes.get)
        best_calmar_cutoff = max(sweep_calmars, key=sweep_calmars.get)
        print(f"\n  Best P&L cutoff:    VIX<{best_pnl_cutoff} (${sweep_pnls[best_pnl_cutoff]:,.0f})")
        print(f"  Best Sharpe cutoff: VIX<{best_sharpe_cutoff} ({sweep_sharpes[best_sharpe_cutoff]:.2f})")
        print(f"  Best Calmar cutoff: VIX<{best_calmar_cutoff} ({sweep_calmars[best_calmar_cutoff]:.2f})")

    print("\nDone.")


if __name__ == "__main__":
    main()
