"""Research: Intraday Volatility Gate — Widen strike distance when SPX bars are wide before entry.

Compute "pre-entry bar volatility": average |high - low| for the 30 individual
1-min bars from 15:25 to 15:54. Then test widening/skipping/risk-cut rules.

Base config: Rule C3 VIX-adaptive (VIX<18->dist=0, 18-22->dist=3, 22-26->dist=5, >26->skip),
bidirectional call+put, entry 15:55, exit 16:00, width $5, min credit $0.10,
commission $0.50/leg. Call filter: afternoon>=1.0%. Put filter: afternoon<-0.75% AND mom30<-0.10%.
Tiered sizing: cr<0.20->$1500, 0.20-0.50->$2500, >0.50->$3500. Starting equity $50,000.

Variants:
  A: No gate (baseline Rule C3)
  B: Widen +5 if avg 1-min bar range > $2
  C: Widen +5 if avg 1-min bar range > $3
  D: Widen +5 if avg 1-min bar range > $4
  E: Skip if avg bar range > $3
  F: Graduated: +3 if >$2, +5 if >$3, +10 if >$5
  G: Halve risk if avg bar range > $3
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


# ── Tiered sizing ──────────────────────────────────────────────────────
def get_tiered_risk(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit > 0.50:
        return 3500.0
    else:
        return 2500.0


# ── Rule C3 VIX-adaptive distance ─────────────────────────────────────
def rule_c3_dist(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5; >26 -> skip (returns -1)."""
    if vix > 26:
        return -1.0  # signal to skip
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Data loaders ──────────────────────────────────────────────────────
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


# ── Helpers ───────────────────────────────────────────────────────────
def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """Signed afternoon return from 13:00 to 15:54."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """30-min momentum: 15:25 to 15:54."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    start = float(bars["close"].iloc[0])
    end = float(bars["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_pre_entry_vol(spx_df: pd.DataFrame) -> float | None:
    """Average |high - low| for the 30 1-min bars from 15:25:00 to 15:54:00."""
    bars = spx_df[(spx_df["time_str"] >= "15:25:00") & (spx_df["time_str"] <= "15:54:00")]
    if len(bars) < 10:  # need reasonable sample
        return None
    ranges = (bars["high"] - bars["low"]).abs()
    return float(ranges.mean())


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


# ── Simulate one side ────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str, risk_override: float | None = None) -> dict | None:
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

    risk = risk_override if risk_override is not None else get_tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

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
            quotes_df: pd.DataFrame, entry_time: str, risk_override: float | None = None) -> dict | None:
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

    risk = risk_override if risk_override is not None else get_tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

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


# ── Variant definitions ──────────────────────────────────────────────
def variant_a(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """A: No gate (baseline). Returns (dist, risk_override)."""
    return base_dist, None


def variant_b(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """B: Widen +5 if avg bar range > $2."""
    if avg_range > 2.0:
        return base_dist + 5.0, None
    return base_dist, None


def variant_c(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """C: Widen +5 if avg bar range > $3."""
    if avg_range > 3.0:
        return base_dist + 5.0, None
    return base_dist, None


def variant_d(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """D: Widen +5 if avg bar range > $4."""
    if avg_range > 4.0:
        return base_dist + 5.0, None
    return base_dist, None


def variant_e(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """E: Skip if avg bar range > $3. Returns (-1, None) to signal skip."""
    if avg_range > 3.0:
        return -1.0, None  # skip
    return base_dist, None


def variant_f(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """F: Graduated widening: +3 if >$2, +5 if >$3, +10 if >$5."""
    if avg_range > 5.0:
        return base_dist + 10.0, None
    elif avg_range > 3.0:
        return base_dist + 5.0, None
    elif avg_range > 2.0:
        return base_dist + 3.0, None
    return base_dist, None


def variant_g(base_dist: float, avg_range: float) -> tuple[float, float | None]:
    """G: Halve risk if avg bar range > $3 (don't widen)."""
    if avg_range > 3.0:
        return base_dist, 0.5  # special: fraction multiplier for risk
    return base_dist, None


VARIANTS = {
    "A_baseline":    variant_a,
    "B_widen2":      variant_b,
    "C_widen3":      variant_c,
    "D_widen4":      variant_d,
    "E_skip3":       variant_e,
    "F_graduated":   variant_f,
    "G_halfrisk3":   variant_g,
}


# ── Main ─────────────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("RESEARCH: Intraday Volatility Gate — Widen strike distance when SPX bars are wide")
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
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_skip": 0, "afternoon_skip": 0}

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

        # VIX > 26 -> skip (all variants share this)
        base_dist = rule_c3_dist(vix1550)
        if base_dist < 0:
            skipped["vix_skip"] += 1
            continue

        # Afternoon return (signed)
        afternoon_ret = compute_afternoon_return(spx_df)

        # Call filter: skip call if afternoon >= 1.0%
        call_allowed = True
        if afternoon_ret is not None and afternoon_ret >= 1.0:
            call_allowed = False

        # Put filter: skip put if afternoon < -0.75% AND mom30 < -0.10%
        put_allowed = True
        mom30 = compute_mom30(spx_df)
        if (afternoon_ret is not None and afternoon_ret < -0.75
                and mom30 is not None and mom30 < -0.10):
            put_allowed = False

        if not call_allowed and not put_allowed:
            skipped["afternoon_skip"] += 1
            continue

        # Spot at 15:55
        spot = get_price_at(spx_df, "15:55")
        if spot is None:
            skipped["no_spx"] += 1
            continue

        # SPX at 16:00
        spx_exit = get_price_at(spx_df, "16:00")
        if spx_exit is None:
            spx_exit = get_price_at(spx_df, "15:59")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        # Pre-entry volatility
        pre_vol = compute_pre_entry_vol(spx_df)

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        day_data.append({
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "base_dist": base_dist,
            "afternoon_ret": afternoon_ret,
            "mom30": mom30,
            "call_allowed": call_allowed,
            "put_allowed": put_allowed,
            "pre_vol": pre_vol,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # Pre-entry vol distribution
    vol_vals = [dd["pre_vol"] for dd in day_data if dd["pre_vol"] is not None]
    if vol_vals:
        print(f"\n  Pre-entry avg bar range distribution (n={len(vol_vals)}):")
        pcts = [10, 25, 50, 75, 90, 95, 99]
        vals = np.percentile(vol_vals, pcts)
        for p, v in zip(pcts, vals):
            print(f"    P{p:>2}: ${v:.2f}")
        print(f"    Mean: ${np.mean(vol_vals):.2f}")
        thresholds = [2.0, 3.0, 4.0, 5.0]
        for t in thresholds:
            cnt = sum(1 for v in vol_vals if v > t)
            print(f"    Days with avg range > ${t:.0f}: {cnt} ({cnt/len(vol_vals)*100:.1f}%)")

    # ── Run each variant ───────────────────────────────────────────────
    results = {}
    for var_name, var_fn in VARIANTS.items():
        all_trades = []
        day_pnls = []

        for dd in day_data:
            pre_vol = dd["pre_vol"]
            if pre_vol is None:
                pre_vol = 0.0  # treat missing as calm

            dist, risk_mod = var_fn(dd["base_dist"], pre_vol)

            # Skip signal
            if dist < 0:
                continue

            day_pnl = 0.0
            day_traded = False

            # Determine risk override for variant G
            risk_override = None
            if risk_mod is not None and isinstance(risk_mod, float) and risk_mod < 1.0:
                # This is a risk multiplier — will be applied per-trade below
                pass

            # Call side
            if dd["call_allowed"]:
                # For variant G, compute halved risk
                call_risk = None
                if risk_mod is not None and isinstance(risk_mod, float) and risk_mod < 1.0:
                    # We need credit first, so pass a flag
                    call_risk = "half"

                if call_risk == "half":
                    # Simulate with halved tiered risk
                    ct = _sim_call_halved(dd["spot"], dd["spx_exit"], dist,
                                         dd["opts_df"], "15:55:00")
                else:
                    ct = sim_call(dd["spot"], dd["spx_exit"], dist,
                                 dd["opts_df"], "15:55:00")
                if ct is not None:
                    all_trades.append(ct)
                    day_pnl += ct["pnl"]
                    day_traded = True

            # Put side
            if dd["put_allowed"]:
                if risk_mod is not None and isinstance(risk_mod, float) and risk_mod < 1.0:
                    pt = _sim_put_halved(dd["spot"], dd["spx_exit"], dist,
                                        dd["opts_df"], "15:55:00")
                else:
                    pt = sim_put(dd["spot"], dd["spx_exit"], dist,
                                dd["opts_df"], "15:55:00")
                if pt is not None:
                    all_trades.append(pt)
                    day_pnl += pt["pnl"]
                    day_traded = True

            if day_traded:
                day_pnls.append({"date": dd["date"], "pnl": day_pnl})

        n_trades = len(all_trades)
        if n_trades == 0:
            results[var_name] = None
            continue

        pnl_arr = np.array([d["pnl"] for d in day_pnls])
        total_pnl = pnl_arr.sum()

        # Equity curve & max DD
        equity = np.cumsum(pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = equity - peak
        max_dd = dd_arr.min()

        # Worst day
        worst_day_idx = np.argmin(pnl_arr)
        worst_day_pnl = pnl_arr[worst_day_idx]
        worst_day_date = day_pnls[worst_day_idx]["date"]

        # Win rate
        n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
        wr = n_wins / n_trades * 100

        # Sharpe
        daily_rf = ANNUAL_RF / 252
        excess = pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = excess.mean() / pnl_arr.std() * np.sqrt(252) if pnl_arr.std() > 0 else 0

        # Calmar
        years = len(pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Trade counts
        n_calls = sum(1 for t in all_trades if t["side"] == "CALL")
        n_puts = sum(1 for t in all_trades if t["side"] == "PUT")

        results[var_name] = {
            "trades": n_trades,
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "worst_day_pnl": worst_day_pnl,
            "worst_day_date": worst_day_date,
            "trading_days": len(day_pnls),
        }

    # ── Print comparison ──────────────────────────────────────────────
    print("\n" + "=" * 150)
    print("COMPARISON TABLE — Intraday Volatility Gate (Pre-entry 30-bar avg range)")
    print("=" * 150)
    print(f"{'Variant':<15} {'Trades':>7} {'C/P':>9} {'Total P&L':>12} {'Max DD':>10} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'Worst Day':>12} {'Worst Date':>11} {'Days':>5}")
    print("-" * 150)

    for var_name in VARIANTS:
        r = results.get(var_name)
        if r is None:
            print(f"{var_name:<15}  NO TRADES")
            continue
        print(
            f"{var_name:<15} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"${r['worst_day_pnl']:>10,.0f} {r['worst_day_date']:>11} "
            f"{r['trading_days']:>5}"
        )
    print("=" * 150)

    # ── Delta vs baseline ────────────────────────────────────────────
    baseline = results.get("A_baseline")
    if baseline:
        print("\nDelta vs A (baseline):")
        print("-" * 100)
        print(f"{'Variant':<15} {'dP&L':>10} {'dMaxDD':>10} {'dWR':>7} {'dSharpe':>9} {'dCalmar':>9} {'dTrades':>8}")
        print("-" * 100)
        for var_name in VARIANTS:
            if var_name == "A_baseline":
                continue
            r = results.get(var_name)
            if r is None:
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dwr = r["wr"] - baseline["wr"]
            dsharpe = r["sharpe"] - baseline["sharpe"]
            dcalmar = r["calmar"] - baseline["calmar"]
            dtrades = r["trades"] - baseline["trades"]
            print(
                f"{var_name:<15} "
                f"${dpnl:>+9,.0f} ${ddd:>+9,.0f} "
                f"{dwr:>+6.1f}% {dsharpe:>+8.2f} {dcalmar:>+9.2f} "
                f"{dtrades:>+8}"
            )
        print("-" * 100)

    # ── Variant descriptions ──────────────────────────────────────────
    print("\nVariant definitions:")
    print("  A: No gate (baseline Rule C3 VIX-adaptive)")
    print("  B: Widen +5 if avg 1-min bar range > $2")
    print("  C: Widen +5 if avg 1-min bar range > $3")
    print("  D: Widen +5 if avg 1-min bar range > $4")
    print("  E: Skip trading entirely if avg bar range > $3")
    print("  F: Graduated: +3 if >$2, +5 if >$3, +10 if >$5")
    print("  G: Halve risk (50%) if avg bar range > $3")

    print("\nDone.")


# ── Halved-risk simulation helpers ────────────────────────────────────
def _sim_call_halved(spot, spx_exit, dist, quotes_df, entry_time):
    """Simulate call with 50% of normal tiered risk."""
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

    risk = get_tiered_risk(credit) * 0.5  # halved
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

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


def _sim_put_halved(spot, spx_exit, dist, quotes_df, entry_time):
    """Simulate put with 50% of normal tiered risk."""
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

    risk = get_tiered_risk(credit) * 0.5  # halved
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

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


if __name__ == "__main__":
    main()
