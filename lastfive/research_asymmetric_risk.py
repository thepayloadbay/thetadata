"""Research: Asymmetric risk allocation based on 30-min momentum direction.

Hypothesis: If SPX is trending up (mom30 > 0), the call side is threatened
and the put side is safe. Reduce call risk, increase put risk. Vice versa.

Tests six allocation rules (A-F) with Rule C3 VIX-adaptive strikes,
bidirectional call+put, entry 15:55, exit 16:00, width $5, min credit $0.10.

Call filter: afternoon >= 1.0% -> skip calls.
Put filter: afternoon < -0.75% AND mom30 < -0.10% -> skip puts.
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
COMMISSION = 0.50  # per leg
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
    """Signed afternoon return: (15:54 close - 13:00 close) / 13:00 close * 100."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100  # signed


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 15:25 to 15:54 bars, in percent (signed)."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
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
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float | None:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5; >26 -> skip."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    elif vix <= 26:
        return 5.0
    else:
        return None  # skip


# ── Base tiered sizing ──────────────────────────────────────────────────
def base_tier(credit: float) -> float:
    """cr<0.20 -> $1500, 0.20-0.50 -> $2500, >0.50 -> $3500."""
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── Asymmetric allocation rules ────────────────────────────────────────
def alloc_A(mom30: float, credit: float, side: str) -> float | None:
    """Baseline: equal risk both sides."""
    return base_tier(credit)


def alloc_B(mom30: float, credit: float, side: str) -> float | None:
    """50/150 split based on momentum direction."""
    tier = base_tier(credit)
    if mom30 > 0:
        # SPX rising: call threatened, put safe
        return tier * 0.50 if side == "CALL" else tier * 1.50
    elif mom30 < 0:
        # SPX falling: put threatened, call safe
        return tier * 1.50 if side == "CALL" else tier * 0.50
    else:
        return tier


def alloc_C(mom30: float, credit: float, side: str) -> float | None:
    """75/125 split based on momentum direction."""
    tier = base_tier(credit)
    if mom30 > 0:
        return tier * 0.75 if side == "CALL" else tier * 1.25
    elif mom30 < 0:
        return tier * 1.25 if side == "CALL" else tier * 0.75
    else:
        return tier


def alloc_D(mom30: float, credit: float, side: str) -> float | None:
    """33/167 split — aggressive asymmetry."""
    tier = base_tier(credit)
    if mom30 > 0:
        return tier * (1/3) if side == "CALL" else tier * (5/3)
    elif mom30 < 0:
        return tier * (5/3) if side == "CALL" else tier * (1/3)
    else:
        return tier


def alloc_E(mom30: float, credit: float, side: str) -> float | None:
    """0/200 — skip threatened side entirely, double safe side."""
    tier = base_tier(credit)
    if mom30 > 0:
        # Call threatened -> skip calls, put gets 2x
        if side == "CALL":
            return None  # skip
        else:
            return tier * 2.0
    elif mom30 < 0:
        # Put threatened -> skip puts, call gets 2x
        if side == "PUT":
            return None
        else:
            return tier * 2.0
    else:
        return tier


def alloc_F(mom30: float, credit: float, side: str) -> float | None:
    """Graduated by |mom30| magnitude."""
    tier = base_tier(credit)
    abs_mom = abs(mom30)

    if abs_mom < 0.05:
        # Low momentum -> equal
        return tier
    elif abs_mom < 0.10:
        # Moderate -> 75/125
        threatened_mult = 0.75
        safe_mult = 1.25
    else:
        # Strong -> 50/150
        threatened_mult = 0.50
        safe_mult = 1.50

    if mom30 > 0:
        return tier * threatened_mult if side == "CALL" else tier * safe_mult
    else:
        return tier * safe_mult if side == "CALL" else tier * threatened_mult


ALLOC_RULES = {
    "A_equal":     alloc_A,
    "B_50_150":    alloc_B,
    "C_75_125":    alloc_C,
    "D_33_167":    alloc_D,
    "E_0_200":     alloc_E,
    "F_graduated": alloc_F,
}


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             risk_amount: float) -> dict | None:
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
            risk_amount: float) -> dict | None:
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
    print("=" * 130)
    print("Asymmetric Risk Allocation by 30-Min Momentum — Rule C3 Adaptive, Bidirectional")
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
    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_skip": 0, "no_spot": 0}

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

        # VIX > 26 -> skip entirely (Rule C3)
        dist = rule_c3_dist(vix1550)
        if dist is None:
            skipped["vix_skip"] += 1
            continue

        # Afternoon return (signed)
        aft_ret = compute_afternoon_return(spx_df)

        # 30-min momentum
        mom30 = compute_mom30(spx_df)

        # Spot at 15:55
        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
            skipped["no_spot"] += 1
            continue
        spot = float(spot_row["close"].iloc[0])

        # Exit at 16:00 (settlement)
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
            "dist": dist,
            "aft_ret": aft_ret,
            "mom30": mom30 if mom30 is not None else 0.0,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # mom30 distribution
    mom_vals = [d["mom30"] for d in day_data]
    mom_arr = np.array(mom_vals)
    print(f"\n  mom30 distribution: mean={mom_arr.mean():.4f}%, median={np.median(mom_arr):.4f}%, "
          f"std={mom_arr.std():.4f}%")
    print(f"  mom30 > 0: {(mom_arr > 0).sum()} days, mom30 < 0: {(mom_arr < 0).sum()} days, "
          f"mom30 == 0: {(mom_arr == 0).sum()} days")

    # ── Run each allocation rule ────────────────────────────────────────
    results = {}
    for rule_name, alloc_fn in ALLOC_RULES.items():
        all_trades = []
        day_pnls = []

        for dd in day_data:
            day_pnl = 0.0
            has_trade = False

            # ── Call side ──
            # Call filter: skip if afternoon >= 1.0%
            skip_call = (dd["aft_ret"] is not None and dd["aft_ret"] >= 1.0)
            if not skip_call:
                call_risk = alloc_fn(dd["mom30"], 0.0, "CALL")  # placeholder credit
                if call_risk is not None:
                    # We need the actual credit to compute tier, so do a 2-pass:
                    # First get credit from a test sim, then apply alloc with real credit
                    ct = _sim_call_with_alloc(dd, alloc_fn)
                    if ct is not None:
                        all_trades.append(ct)
                        day_pnl += ct["pnl"]
                        has_trade = True

            # ── Put side ──
            # Put filter: skip if afternoon < -0.75% AND mom30 < -0.10%
            skip_put = (dd["aft_ret"] is not None and dd["mom30"] is not None and
                        dd["aft_ret"] < -0.75 and dd["mom30"] < -0.10)
            if not skip_put:
                put_risk = alloc_fn(dd["mom30"], 0.0, "PUT")  # placeholder
                if put_risk is not None:
                    pt = _sim_put_with_alloc(dd, alloc_fn)
                    if pt is not None:
                        all_trades.append(pt)
                        day_pnl += pt["pnl"]
                        has_trade = True

            if has_trade:
                day_pnls.append({"date": dd["date"], "pnl": day_pnl})

        results[rule_name] = compute_stats(all_trades, day_pnls)

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 150)
    print("COMPARISON TABLE — Asymmetric Risk Allocation by mom30 Direction")
    print("=" * 150)
    print(f"{'Rule':<14} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} "
          f"{'Sharpe':>8} {'Calmar':>8} {'Worst Day':>11} "
          f"{'Call Tr':>8} {'Put Tr':>8} {'Call P&L':>10} {'Put P&L':>10}")
    print("-" * 150)

    for name in ALLOC_RULES:
        r = results[name]
        if r is None:
            print(f"{name:<14}  NO TRADES")
            continue
        print(
            f"{name:<14} {r['trades']:>7} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"${r['worst_day']:>9,.0f} "
            f"{r['call_trades']:>8} {r['put_trades']:>8} "
            f"${r['call_pnl']:>8,.0f} ${r['put_pnl']:>8,.0f}"
        )
    print("=" * 150)

    # ── Delta vs baseline (A) ──────────────────────────────────────────
    baseline = results.get("A_equal")
    if baseline:
        print("\nDelta vs A_equal baseline:")
        print("-" * 100)
        for name in ALLOC_RULES:
            if name == "A_equal":
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

    # ── Rule descriptions ──────────────────────────────────────────────
    print("\nAllocation rules (base tier: cr<0.20->$1500, 0.20-0.50->$2500, >0.50->$3500):")
    print("  A_equal     : Both sides get full tier (current baseline)")
    print("  B_50_150    : Threatened side 50% of tier, safe side 150%")
    print("  C_75_125    : Threatened side 75% of tier, safe side 125%")
    print("  D_33_167    : Threatened side 33% of tier, safe side 167%")
    print("  E_0_200     : Skip threatened side entirely, safe side gets 200%")
    print("  F_graduated : |mom30|<0.05%: equal; 0.05-0.10%: 75/125; >0.10%: 50/150")
    print(f"\n  mom30 = SPX return 15:25->15:54. 'Threatened' = side SPX is moving toward.")
    print("  If mom30 > 0 (rising): call threatened, put safe. Vice versa.")

    print("\nDone.")


def _sim_call_with_alloc(dd, alloc_fn) -> dict | None:
    """Get credit first, then compute allocation, then simulate."""
    target = dd["spot"] + dd["dist"]
    short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike + int(SPREAD_WIDTH)
    opts_df = dd["opts_df"]

    available = set(opts_df[opts_df["right"] == "C"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted(s for s in available if s >= dd["spot"] + dd["dist"])
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike + int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(opts_df, "C", short_strike, "15:55:00")
    long_q = get_quote(opts_df, "C", long_strike, "15:55:00")
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = alloc_fn(dd["mom30"], credit, "CALL")
    if risk_amount is None:
        return None

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    spx_exit = dd["spx_exit"]
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


def _sim_put_with_alloc(dd, alloc_fn) -> dict | None:
    """Get credit first, then compute allocation, then simulate."""
    target = dd["spot"] - dd["dist"]
    short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_strike = short_strike - int(SPREAD_WIDTH)
    opts_df = dd["opts_df"]

    available = set(opts_df[opts_df["right"] == "P"]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        candidates = sorted((s for s in available if s <= dd["spot"] - dd["dist"]), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        long_strike = short_strike - int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(opts_df, "P", short_strike, "15:55:00")
    long_q = get_quote(opts_df, "P", long_strike, "15:55:00")
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    risk_amount = alloc_fn(dd["mom30"], credit, "PUT")
    if risk_amount is None:
        return None

    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    spx_exit = dd["spx_exit"]
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


def compute_stats(trades: list, day_pnls: list) -> dict | None:
    if not trades or not day_pnls:
        return None

    n_trades = len(trades)
    pnl_arr = np.array([d["pnl"] for d in day_pnls])
    total_pnl = pnl_arr.sum()

    # Max drawdown
    equity = np.cumsum(pnl_arr) + STARTING_EQUITY
    peak = np.maximum.accumulate(equity)
    dd_arr = equity - peak
    max_dd = dd_arr.min()

    # Win rate (per trade)
    n_wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = n_wins / n_trades * 100

    # Sharpe
    daily_rf = ANNUAL_RF / 252
    excess = pnl_arr - daily_rf * STARTING_EQUITY
    sharpe = excess.mean() / pnl_arr.std() * np.sqrt(252) if pnl_arr.std() > 0 else 0

    # Calmar
    years = len(pnl_arr) / 252
    annual_ret = total_pnl / years if years > 0 else 0
    calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

    # Worst day
    worst_day = pnl_arr.min()

    # By-side stats
    call_trades = [t for t in trades if t["side"] == "CALL"]
    put_trades = [t for t in trades if t["side"] == "PUT"]
    call_pnl = sum(t["pnl"] for t in call_trades)
    put_pnl = sum(t["pnl"] for t in put_trades)

    return {
        "trades": n_trades,
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "wr": wr,
        "sharpe": sharpe,
        "calmar": calmar,
        "worst_day": worst_day,
        "call_trades": len(call_trades),
        "put_trades": len(put_trades),
        "call_pnl": call_pnl,
        "put_pnl": put_pnl,
    }


if __name__ == "__main__":
    main()
