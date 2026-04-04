"""Research: Credit-Weighted Position Sizing for LastFive strategy.

Hypothesis: when credit is higher, the spread has more cushion before
losing money, so we can safely risk more. When credit is thin ($0.10-$0.15),
risk less.

Tests five sizing rules (A-E) with Rule C3 adaptive strikes, bidirectional,
entry 15:55, exit 16:00, width $5, min credit $0.10, afternoon<1.0%, VIX<=26.
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


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Sizing rules ────────────────────────────────────────────────────────
def sizing_a(credit: float) -> float | None:
    """Fixed $2,500 risk per side."""
    return 2500.0


def sizing_b(credit: float) -> float | None:
    """Tiered: low credit=less risk, high credit=more risk."""
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


def sizing_c(credit: float) -> float | None:
    """Linear scale: risk = min(5000, max(1000, credit * 5000))."""
    return min(5000.0, max(1000.0, credit * 5000.0))


def sizing_d(credit: float) -> float | None:
    """Aggressive tiered."""
    if credit < 0.20:
        return 1000.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 5000.0


def sizing_e(credit: float) -> float | None:
    """Conservative — skip low credit entirely."""
    if credit < 0.20:
        return None  # skip
    return 3000.0


SIZING_RULES = {
    "A_fixed":       sizing_a,
    "B_tiered":      sizing_b,
    "C_linear":      sizing_c,
    "D_aggressive":  sizing_d,
    "E_skip_low":    sizing_e,
}


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             sizing_fn) -> dict | None:
    """Simulate a call credit spread with given sizing rule."""
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

    # Apply sizing rule
    risk_amount = sizing_fn(credit)
    if risk_amount is None:
        return None  # sizing rule says skip

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

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
            "short_strike": short_strike, "risk_deployed": risk_amount}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str,
            sizing_fn) -> dict | None:
    """Simulate a put credit spread with given sizing rule."""
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

    # Apply sizing rule
    risk_amount = sizing_fn(credit)
    if risk_amount is None:
        return None  # sizing rule says skip

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

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
            "short_strike": short_strike, "risk_deployed": risk_amount}


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("Credit-Weighted Position Sizing — Rule C3 Adaptive Strikes, Bidirectional")
    print("=" * 110)

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

        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
            skipped["no_spx"] += 1
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

    # ── Run each sizing rule ────────────────────────────────────────────
    results = {}
    for sizing_name, sizing_fn in SIZING_RULES.items():
        all_trades = []
        all_day_pnls = []
        all_risks = []

        for dd in day_data:
            dist = rule_c3_dist(dd["vix1550"])
            day_pnl = 0.0
            has_trade = False

            ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                          "15:55:00", sizing_fn)
            if ct is not None:
                all_trades.append(ct)
                day_pnl += ct["pnl"]
                all_risks.append(ct["risk_deployed"])
                has_trade = True

            pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                         "15:55:00", sizing_fn)
            if pt is not None:
                all_trades.append(pt)
                day_pnl += pt["pnl"]
                all_risks.append(pt["risk_deployed"])
                has_trade = True

            if has_trade:
                all_day_pnls.append(day_pnl)

        n_trades = len(all_trades)
        if n_trades == 0:
            results[sizing_name] = None
            continue

        day_pnl_arr = np.array(all_day_pnls)
        total_pnl = day_pnl_arr.sum()

        # Max drawdown on daily equity curve
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
        sharpe = excess.mean() / day_pnl_arr.std() * np.sqrt(252) if day_pnl_arr.std() > 0 else 0

        # Calmar
        years = len(day_pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Avg credit and risk
        avg_credit = np.mean([t["credit"] for t in all_trades])
        avg_risk = np.mean(all_risks)

        # Profit factor
        trade_pnls = np.array([t["pnl"] for t in all_trades])
        gross_profit = trade_pnls[trade_pnls > 0].sum()
        gross_loss = abs(trade_pnls[trade_pnls < 0].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Avg win / avg loss
        wins_arr = trade_pnls[trade_pnls > 0]
        loss_arr = trade_pnls[trade_pnls < 0]
        avg_win = wins_arr.mean() if len(wins_arr) > 0 else 0
        avg_loss = loss_arr.mean() if len(loss_arr) > 0 else 0

        results[sizing_name] = {
            "trades": n_trades,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "avg_credit": avg_credit,
            "avg_risk": avg_risk,
            "pf": pf,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 140)
    print("COMPARISON TABLE — Credit-Weighted Position Sizing (Rule C3 Adaptive, Bidirectional)")
    print("=" * 140)
    print(f"{'Sizing':<14} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} "
          f"{'Sharpe':>8} {'Calmar':>8} {'PF':>6} {'Avg Risk':>10} "
          f"{'Avg Cr':>8} {'Avg Win':>9} {'Avg Loss':>10}")
    print("-" * 140)

    for name in SIZING_RULES:
        r = results.get(name)
        if r is None:
            print(f"{name:<14}  NO TRADES")
            continue
        print(
            f"{name:<14} {r['trades']:>7} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} ${r['avg_risk']:>8,.0f} "
            f"${r['avg_credit']:>6.2f} ${r['avg_win']:>7,.0f} "
            f"${r['avg_loss']:>8,.0f}"
        )
    print("=" * 140)

    # ── Sizing rule descriptions ────────────────────────────────────────
    print("\nSizing rules:")
    print("  A_fixed      : Fixed $2,500 risk per side (current baseline)")
    print("  B_tiered     : Credit < $0.20 -> $1,500 | $0.20-$0.50 -> $2,500 | > $0.50 -> $3,500")
    print("  C_linear     : risk = min($5,000, max($1,000, credit * 5,000))")
    print("  D_aggressive : Credit < $0.20 -> $1,000 | $0.20-$0.50 -> $2,500 | > $0.50 -> $5,000")
    print("  E_skip_low   : Credit < $0.20 -> SKIP | else $3,000")

    # ── Delta vs baseline (A) ──────────────────────────────────────────
    baseline = results.get("A_fixed")
    if baseline:
        print("\nDelta vs A_fixed baseline:")
        print("-" * 90)
        for name in SIZING_RULES:
            if name == "A_fixed":
                continue
            r = results.get(name)
            if r is None:
                print(f"  {name:<14}: NO TRADES")
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dsharpe = r["sharpe"] - baseline["sharpe"]
            dcalmar = r["calmar"] - baseline["calmar"]
            dtrades = r["trades"] - baseline["trades"]
            print(f"  {name:<14}: P&L {dpnl:>+10,.0f}  |  DD {ddd:>+8,.0f}  |  "
                  f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}  |  "
                  f"Trades {dtrades:>+4d}")

    # ── Credit distribution across all trades ──────────────────────────
    print("\nCredit distribution (from baseline A sizing):")
    print("-" * 60)
    # Re-collect credit data
    credits = []
    for dd in day_data:
        dist = rule_c3_dist(dd["vix1550"])
        ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                      "15:55:00", sizing_a)
        if ct:
            credits.append(ct["credit"])
        pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                     "15:55:00", sizing_a)
        if pt:
            credits.append(pt["credit"])

    credits = np.array(credits)
    buckets = [(0.10, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.00), (1.00, 99.0)]
    for lo, hi in buckets:
        mask = (credits >= lo) & (credits < hi)
        n = mask.sum()
        pct = n / len(credits) * 100
        label = f"${lo:.2f}-${hi:.2f}" if hi < 99 else f"${lo:.2f}+"
        print(f"  {label:<14}: {n:>4} trades ({pct:>5.1f}%)")

    print(f"\n  Total: {len(credits)} trades, avg credit ${credits.mean():.3f}, "
          f"median ${np.median(credits):.3f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
