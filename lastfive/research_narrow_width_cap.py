"""Research: Mechanical Max-Loss Capping for LastFive strategy.

FINDING: SPX 0DTE options only have $5 strike spacing, so widths of $2/$3
are impossible. The minimum constructible spread is $5.

Instead, we test mechanical ways to limit max loss per trade:
  A — width=5, standard tiers $1500/$2500/$3500 (baseline)
  B — width=5, reduced tiers $1000/$1500/$2000 (fewer contracts)
  C — width=5, tiny tiers $750/$1000/$1500 (very few contracts)
  D — width=10, standard tiers (fewer contracts but more credit/contract)
  E — width=5, standard tiers but max 5 contracts per side
  F — width=5, standard tiers but max 3 contracts per side
  G — width=5, adaptive tiers: low risk at dist=0, standard at dist=3/5

The insight: with standard tiers, a low-credit trade (cr=$0.12) gets
qty=int(1500/(5-0.12)/100) = 3 contracts, max loss = 3*(5-0.12)*100+6 = $1,470.
A high-credit trade (cr=$0.60) gets qty=int(3500/(5-0.60)/100) = 7,
max loss = 7*(5-0.60)*100+14 = $3,094. Reducing tiers or capping qty
mechanically limits the worst single trade.

Base: Rule C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00,
min credit $0.10, commission $0.50/leg.
Call filter: afternoon >= 1.0% -> skip.
Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip.
Starting equity $50,000.
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


# ── Tiered sizing with optional max qty ────────────────────────────────
def tiered_sizing(credit: float, tiers: dict) -> float:
    if credit < 0.20:
        return tiers["low"]
    elif credit <= 0.50:
        return tiers["mid"]
    else:
        return tiers["high"]


DEFAULT_TIERS = {"low": 1500.0, "mid": 2500.0, "high": 3500.0}


# ── Simulate one side (parameterized) ──────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             width: float, tiers: dict, max_qty: int | None = None) -> dict | None:
    if dist == 0:
        short_strike = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
        target = spot + dist
        short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike + int(width)

    available = set(quotes_df[quotes_df["right"] == "C"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted(s for s in available if s >= spot + dist)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(width)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "C", short_strike, entry_time)
    long_q = get_quote(quotes_df, "C", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = tiered_sizing(credit, tiers)
    max_loss_per = (width - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))
    if max_qty is not None:
        qty = min(qty, max_qty)

    if spx_exit <= short_strike:
        settlement = 0.0
    elif spx_exit >= long_strike:
        settlement = width
    else:
        settlement = spx_exit - short_strike

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "CALL", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "long_strike": long_strike,
            "max_loss_trade": max_loss_per * qty + comm}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str,
            width: float, tiers: dict, max_qty: int | None = None) -> dict | None:
    if dist == 0:
        short_strike = int(math.floor(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
        target = spot - dist
        short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike - int(width)

    available = set(quotes_df[quotes_df["right"] == "P"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike - int(width)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, "P", short_strike, entry_time)
    long_q = get_quote(quotes_df, "P", long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = tiered_sizing(credit, tiers)
    max_loss_per = (width - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))
    if max_qty is not None:
        qty = min(qty, max_qty)

    if spx_exit >= short_strike:
        settlement = 0.0
    elif spx_exit <= long_strike:
        settlement = width
    else:
        settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "PUT", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "long_strike": long_strike,
            "max_loss_trade": max_loss_per * qty + comm}


# ── Compute metrics ─────────────────────────────────────────────────────
def compute_metrics(day_pnls: list[float], trade_details: list[dict]) -> dict:
    if not day_pnls:
        return {"total_pnl": 0, "max_dd": 0, "sharpe": 0, "calmar": 0,
                "worst_day": 0, "worst_trade": 0, "trades": 0, "wr": 0,
                "avg_credit": 0, "avg_qty": 0, "max_qty_seen": 0}

    arr = np.array(day_pnls)
    total = arr.sum()
    equity = np.cumsum(arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak).min()
    years = len(arr) / 252
    annual_ret = total / years if years > 0 else 0
    calmar = annual_ret / abs(dd) if dd != 0 else 0
    daily_rf = ANNUAL_RF / 252
    excess = arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0
    worst_day = arr.min()

    all_pnls = [t["pnl"] for t in trade_details]
    all_credits = [t["credit"] for t in trade_details]
    all_qtys = [t["qty"] for t in trade_details]
    all_max_loss = [t["max_loss_trade"] for t in trade_details]
    n_trades = len(all_pnls)
    wins = sum(1 for p in all_pnls if p > 0)
    wr = wins / n_trades * 100 if n_trades > 0 else 0
    worst_trade = min(all_pnls) if all_pnls else 0
    avg_credit = np.mean(all_credits) if all_credits else 0
    avg_qty = np.mean(all_qtys) if all_qtys else 0
    max_qty_seen = max(all_qtys) if all_qtys else 0
    max_possible_loss = max(all_max_loss) if all_max_loss else 0

    return {
        "total_pnl": total,
        "max_dd": dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "worst_day": worst_day,
        "worst_trade": worst_trade,
        "trades": n_trades,
        "wr": wr,
        "avg_credit": avg_credit,
        "avg_qty": avg_qty,
        "max_qty_seen": max_qty_seen,
        "max_possible_loss": max_possible_loss,
    }


# ── Config definitions ─────────────────────────────────────────────────
CONFIGS = {
    "A (baseline)": {
        "width": 5, "tiers": DEFAULT_TIERS, "max_qty": None,
        "desc": "w=5, tiers $1.5k/$2.5k/$3.5k",
    },
    "B (low risk)": {
        "width": 5, "tiers": {"low": 1000.0, "mid": 1500.0, "high": 2000.0}, "max_qty": None,
        "desc": "w=5, tiers $1k/$1.5k/$2k",
    },
    "C (tiny risk)": {
        "width": 5, "tiers": {"low": 750.0, "mid": 1000.0, "high": 1500.0}, "max_qty": None,
        "desc": "w=5, tiers $750/$1k/$1.5k",
    },
    "D (w=10)": {
        "width": 10, "tiers": DEFAULT_TIERS, "max_qty": None,
        "desc": "w=10, tiers $1.5k/$2.5k/$3.5k (fewer contracts, more credit each)",
    },
    "E (max 5 qty)": {
        "width": 5, "tiers": DEFAULT_TIERS, "max_qty": 5,
        "desc": "w=5, std tiers, max 5 contracts/side",
    },
    "F (max 3 qty)": {
        "width": 5, "tiers": DEFAULT_TIERS, "max_qty": 3,
        "desc": "w=5, std tiers, max 3 contracts/side",
    },
    "G (adpt tiers)": {
        "width": 5,
        "tiers_by_dist": {
            0: {"low": 1000.0, "mid": 1500.0, "high": 2000.0},
            3: DEFAULT_TIERS,
            5: DEFAULT_TIERS,
        },
        "tiers": DEFAULT_TIERS,  # fallback
        "max_qty": None,
        "desc": "w=5, low tiers at dist=0, std at dist=3/5",
    },
}


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 140)
    print("Mechanical Max-Loss Capping — Rule C3 Adaptive + Tiered Sizing + Put Filters")
    print("NOTE: SPX 0DTE strikes are $5 apart. Widths of $2/$3 are NOT constructible.")
    print("      Instead testing: reduced risk budgets, wider widths, and qty caps.")
    print("=" * 140)

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
    print("Loading day data (SPX, VIX, options)...")
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
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "dist": dist,
            "opts_df": opts_df,
            "aft_ret": aft_ret,
            "mom30": m30,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Simulate each config ───────────────────────────────────────────
    results = {}
    for label, cfg in CONFIGS.items():
        print(f"\nSimulating {label} — {cfg['desc']}...")
        day_pnls = []
        trade_details = []

        for dd in day_data:
            dist = dd["dist"]
            width = cfg["width"]
            max_qty = cfg["max_qty"]

            # Adaptive tiers by distance
            if "tiers_by_dist" in cfg:
                tiers = cfg["tiers_by_dist"].get(int(dist), cfg["tiers"])
            else:
                tiers = cfg["tiers"]

            day_total = 0.0
            has_trade = False

            # Call side: skip if afternoon >= 1.0%
            skip_call = dd["aft_ret"] is not None and dd["aft_ret"] >= 1.0
            if not skip_call:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist,
                              dd["opts_df"], "15:55:00", width, tiers, max_qty)
                if ct is not None:
                    day_total += ct["pnl"]
                    trade_details.append(ct)
                    has_trade = True

            # Put side: skip if afternoon < -0.75% AND mom30 < -0.10%
            skip_put = (dd["aft_ret"] is not None and dd["aft_ret"] < -0.75 and
                        dd["mom30"] is not None and dd["mom30"] < -0.10)
            if not skip_put:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist,
                             dd["opts_df"], "15:55:00", width, tiers, max_qty)
                if pt is not None:
                    day_total += pt["pnl"]
                    trade_details.append(pt)
                    has_trade = True

            if has_trade:
                day_pnls.append(day_total)

        m = compute_metrics(day_pnls, trade_details)
        results[label] = m

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 155)
    print("COMPARISON TABLE — Mechanical Max-Loss Capping")
    print("  Base: Rule C3 adaptive + tiered sizing + put filter (aftn<-0.75% & mom30<-0.10%)")
    print("=" * 155)
    hdr = (f"{'Config':<16} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} "
           f"{'Sharpe':>8} {'Calmar':>8} {'Worst Day':>11} {'Worst Trd':>11} "
           f"{'Avg Cr':>8} {'Avg Qty':>8} {'Max Qty':>8}")
    print(hdr)
    print("-" * 155)

    for label in CONFIGS:
        r = results[label]
        marker = " <-- baseline" if "baseline" in label else ""
        print(
            f"{label:<16} {r['trades']:>7} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"${r['worst_day']:>9,.0f} ${r['worst_trade']:>9,.0f} "
            f"${r['avg_credit']:>6.2f} {r['avg_qty']:>7.1f} {r['max_qty_seen']:>7}{marker}"
        )
    print("=" * 155)

    # ── Delta vs baseline ───────────────────────────────────────────────
    baseline = results["A (baseline)"]
    print("\nDelta vs A (baseline):")
    print("-" * 120)
    for label in CONFIGS:
        if "baseline" in label:
            continue
        r = results[label]
        dpnl = r["total_pnl"] - baseline["total_pnl"]
        ddd = r["max_dd"] - baseline["max_dd"]  # positive = less DD
        dsharpe = r["sharpe"] - baseline["sharpe"]
        dcalmar = r["calmar"] - baseline["calmar"]
        dworst_t = r["worst_trade"] - baseline["worst_trade"]
        dworst_d = r["worst_day"] - baseline["worst_day"]
        print(f"  {label:<16}: P&L {dpnl:>+10,.0f}  |  DD {ddd:>+8,.0f}  |  "
              f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}  |  "
              f"Worst Day {dworst_d:>+8,.0f}  |  Worst Trade {dworst_t:>+8,.0f}")

    # ── Max loss budget analysis ────────────────────────────────────────
    print("\n" + "=" * 100)
    print("MAX LOSS BUDGET — Theoretical max loss per trade (width*qty*100 + commission)")
    print("=" * 100)
    for label in CONFIGS:
        r = results[label]
        print(f"  {label:<16}: Max possible loss/trade = ${r['max_possible_loss']:>8,.0f}  |  "
              f"Actual worst trade = ${r['worst_trade']:>8,.0f}  |  "
              f"Avg qty = {r['avg_qty']:.1f}")

    # ── Risk-adjusted scaling analysis ──────────────────────────────────
    print("\n" + "=" * 100)
    print("EFFICIENCY — P&L per unit of max DD (higher = better risk-adjusted)")
    print("=" * 100)
    for label in CONFIGS:
        r = results[label]
        if r["max_dd"] != 0:
            efficiency = r["total_pnl"] / abs(r["max_dd"])
        else:
            efficiency = 0
        print(f"  {label:<16}: P&L/DD = {efficiency:>6.2f}x  |  "
              f"P&L=${r['total_pnl']:>10,.0f}  |  DD=${r['max_dd']:>8,.0f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
