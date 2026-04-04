#!/usr/bin/env python3
"""
Research: Widen strike distance on the momentum-threatened side.

Tests whether pushing the short strike further OTM on the side momentum
is heading toward (while optionally tightening the safe side) improves
risk-adjusted returns.
"""

import math
import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── constants ──────────────────────────────────────────────────────────
DATA_ROOT = Path("/Users/jeerapongwongchote/Documents/thetadata/data")
YEARS = range(2022, 2027)

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

WIDTH = 5
MIN_CREDIT = 0.10
COMMISSION_PER_LEG = 0.50
COMMISSION = COMMISSION_PER_LEG * 4   # 2 legs open + 2 legs close
START_EQUITY = 50_000

ENTRY_TIME = "15:55"
EXIT_TIME  = "16:00"
MOM_START  = "15:25"
MOM_END    = "15:54"


# ── helpers ────────────────────────────────────────────────────────────
def get_trading_dates():
    """Collect all trading dates that have all three data files."""
    dates = []
    for year in YEARS:
        oq = DATA_ROOT / str(year) / "option_quotes"
        if not oq.exists():
            continue
        for f in sorted(oq.glob("*.parquet")):
            d = f.stem
            if d in HOLIDAYS:
                continue
            if d in EARLY_CLOSE:
                continue
            spx = DATA_ROOT / str(year) / "spx_ohlc" / f"{d}.parquet"
            vix = DATA_ROOT / str(year) / "vix_ohlc" / f"{d}.parquet"
            if spx.exists() and vix.exists():
                dates.append((year, d))
    return dates


def load_day(year, date_str):
    """Load SPX OHLC, VIX OHLC, and option quotes for a single day."""
    spx = pd.read_parquet(DATA_ROOT / str(year) / "spx_ohlc" / f"{date_str}.parquet")
    vix = pd.read_parquet(DATA_ROOT / str(year) / "vix_ohlc" / f"{date_str}.parquet")
    opts = pd.read_parquet(DATA_ROOT / str(year) / "option_quotes" / f"{date_str}.parquet")

    # normalise timestamps to HH:MM
    for df in [spx, vix, opts]:
        df["time"] = df["timestamp"].str[11:16]

    return spx, vix, opts


def vix_adaptive_dist(vix_close):
    """VIX-adaptive base distance (Rule C3)."""
    if vix_close < 18:
        return 0
    elif vix_close < 22:
        return 3
    elif vix_close < 26:
        return 5
    else:
        return None   # skip day


def tier_size(credit):
    if credit < 0.20:
        return 1500
    elif credit <= 0.50:
        return 2500
    else:
        return 3500


def get_credit(opts, right, short_strike, long_strike, time_str):
    """Get net credit (short bid - long ask) at a given time."""
    at_time = opts[opts["time"] == time_str]
    short_row = at_time[(at_time["right"] == right) & (at_time["strike"] == short_strike)]
    long_row  = at_time[(at_time["right"] == right) & (at_time["strike"] == long_strike)]
    if short_row.empty or long_row.empty:
        return None
    sb = short_row["bid"].iloc[0]
    la = long_row["ask"].iloc[0]
    if sb <= 0 or la < 0:
        return None
    credit = sb - la
    return credit if credit >= MIN_CREDIT else None


def get_settle_value(opts, right, strike, time_str):
    """Get option mid at settlement time."""
    at_time = opts[opts["time"] == time_str]
    row = at_time[(at_time["right"] == right) & (at_time["strike"] == strike)]
    if row.empty:
        return None
    return row["mid"].iloc[0]


def compute_spread_pnl(opts, right, short_strike, long_strike, credit):
    """Compute P&L for one spread at settlement."""
    # At 16:00 settlement, use mid
    short_val = get_settle_value(opts, right, short_strike, EXIT_TIME)
    long_val  = get_settle_value(opts, right, long_strike, EXIT_TIME)
    if short_val is None or long_val is None:
        # fallback: intrinsic would be needed, but just skip
        return None
    debit = short_val - long_val  # cost to close
    pnl_per_contract = (credit - debit) * 100
    return pnl_per_contract


# ── strategy variants ──────────────────────────────────────────────────
def compute_distances(variant, base_dist, mom30):
    """
    Returns (call_dist, put_dist) for given variant.
    """
    cd = base_dist
    pd_ = base_dist

    if variant == "A":
        pass  # baseline

    elif variant == "B":
        # +5 on threatened side
        if mom30 > 0:
            cd = base_dist + 5
        elif mom30 < 0:
            pd_ = base_dist + 5

    elif variant == "C":
        # +3 on threatened side
        if mom30 > 0:
            cd = base_dist + 3
        elif mom30 < 0:
            pd_ = base_dist + 3

    elif variant == "D":
        # +5 only if |mom30| > 0.05%
        if abs(mom30) > 0.05:
            if mom30 > 0:
                cd = base_dist + 5
            else:
                pd_ = base_dist + 5

    elif variant == "E":
        # +5 threatened, -3 safe
        if mom30 > 0:
            cd = base_dist + 5
            pd_ = max(base_dist - 3, 0)
        elif mom30 < 0:
            pd_ = base_dist + 5
            cd = max(base_dist - 3, 0)

    elif variant == "F":
        # graduated: +3 if 0.03-0.07%, +5 if > 0.07%
        am = abs(mom30)
        if am > 0.07:
            add = 5
        elif am > 0.03:
            add = 3
        else:
            add = 0
        if add > 0:
            if mom30 > 0:
                cd = base_dist + add
            else:
                pd_ = base_dist + add

    return cd, pd_


# ── main backtest ──────────────────────────────────────────────────────
def run_backtest(variant, dates_data):
    """
    Run backtest for a given variant across all preloaded day data.
    Returns dict of metrics.
    """
    equity = START_EQUITY
    daily_pnls = []
    trade_count = 0
    wins = 0

    for day_info in dates_data:
        date_str = day_info["date"]
        spx = day_info["spx"]
        vix_close = day_info["vix_close"]
        opts = day_info["opts"]
        mom30 = day_info["mom30"]
        spot = day_info["spot"]
        afternoon_ret = day_info["afternoon_ret"]

        # VIX filter
        base_dist = vix_adaptive_dist(vix_close)
        if base_dist is None:
            daily_pnls.append(0)
            continue

        # compute variant distances
        call_dist, put_dist = compute_distances(variant, base_dist, mom30)

        day_pnl = 0

        # ── CALL CREDIT SPREAD ──
        # Filter: afternoon return >= 1.0%
        if afternoon_ret >= 1.0:
            short_call = math.ceil((spot + call_dist) / 5) * 5
            long_call  = short_call + WIDTH
            credit_c = get_credit(opts, "C", short_call, long_call, ENTRY_TIME)
            if credit_c is not None:
                pnl_per = compute_spread_pnl(opts, "C", short_call, long_call, credit_c)
                if pnl_per is not None:
                    size = tier_size(credit_c)
                    n_contracts = max(1, int(size / (WIDTH * 100)))
                    raw_pnl = pnl_per * n_contracts - COMMISSION * n_contracts
                    day_pnl += raw_pnl
                    trade_count += 1
                    if raw_pnl > 0:
                        wins += 1

        # ── PUT CREDIT SPREAD ──
        # Filter: afternoon return < -0.75% AND mom30 < -0.10%
        if afternoon_ret < -0.75 and mom30 < -0.10:
            short_put = math.floor((spot - put_dist) / 5) * 5
            long_put  = short_put - WIDTH
            credit_p = get_credit(opts, "P", short_put, long_put, ENTRY_TIME)
            if credit_p is not None:
                pnl_per = compute_spread_pnl(opts, "P", short_put, long_put, credit_p)
                if pnl_per is not None:
                    size = tier_size(credit_p)
                    n_contracts = max(1, int(size / (WIDTH * 100)))
                    raw_pnl = pnl_per * n_contracts - COMMISSION * n_contracts
                    day_pnl += raw_pnl
                    trade_count += 1
                    if raw_pnl > 0:
                        wins += 1

        daily_pnls.append(day_pnl)
        equity += day_pnl

    # ── metrics ──
    daily_pnls = np.array(daily_pnls)
    total_pnl = daily_pnls.sum()
    cum = np.cumsum(daily_pnls)
    peak = np.maximum.accumulate(cum + START_EQUITY)
    dd = cum + START_EQUITY - peak
    max_dd = dd.min()
    wr = wins / trade_count * 100 if trade_count > 0 else 0

    # Sharpe (annualised, trading-day only pnl)
    trading_pnls = daily_pnls[daily_pnls != 0]
    if len(trading_pnls) > 1 and trading_pnls.std() > 0:
        sharpe = trading_pnls.mean() / trading_pnls.std() * np.sqrt(252)
    else:
        sharpe = 0

    # Calmar
    calmar = (total_pnl / (len(daily_pnls) / 252)) / abs(max_dd) if max_dd != 0 else 0

    worst_day = daily_pnls.min()

    return {
        "variant": variant,
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "win_rate": wr,
        "trades": trade_count,
        "sharpe": sharpe,
        "calmar": calmar,
        "worst_day": worst_day,
    }


# ── data loading ───────────────────────────────────────────────────────
def preload_all_data():
    """Load and precompute all day-level data once."""
    dates = get_trading_dates()
    print(f"Found {len(dates)} trading dates")

    all_days = []
    skipped = 0
    for i, (year, date_str) in enumerate(dates):
        if (i + 1) % 100 == 0:
            print(f"  loading {i+1}/{len(dates)}...")
        try:
            spx, vix, opts = load_day(year, date_str)
        except Exception:
            skipped += 1
            continue

        # VIX close at 15:55
        vix_at = vix[vix["time"] == ENTRY_TIME]
        if vix_at.empty or vix_at["close"].iloc[0] <= 0:
            # try 15:54 or nearby
            vix_at = vix[vix["time"] <= ENTRY_TIME]
            vix_at = vix_at[vix_at["close"] > 0]
            if vix_at.empty:
                skipped += 1
                continue
        vix_close = vix_at["close"].iloc[-1]

        # SPX spot at 15:55
        spx_entry = spx[spx["time"] == ENTRY_TIME]
        if spx_entry.empty:
            skipped += 1
            continue
        spot = spx_entry["close"].iloc[0]

        # Afternoon return (open to 15:55): use 09:31 open as open proxy
        spx_open_row = spx[spx["time"] == "09:31"]
        if spx_open_row.empty:
            spx_open_row = spx[spx["time"] == "09:30"]
        if spx_open_row.empty:
            skipped += 1
            continue
        day_open = spx_open_row["open"].iloc[0]
        afternoon_ret = (spot / day_open - 1) * 100

        # mom30: return from 15:25 to 15:54
        spx_m_start = spx[spx["time"] == MOM_START]
        spx_m_end   = spx[spx["time"] == MOM_END]
        if spx_m_start.empty or spx_m_end.empty:
            skipped += 1
            continue
        p_start = spx_m_start["close"].iloc[0]
        p_end   = spx_m_end["close"].iloc[0]
        mom30 = (p_end / p_start - 1) * 100

        all_days.append({
            "date": date_str,
            "spx": spx,
            "vix_close": vix_close,
            "opts": opts,
            "mom30": mom30,
            "spot": spot,
            "afternoon_ret": afternoon_ret,
        })

    print(f"Loaded {len(all_days)} days, skipped {skipped}")
    return all_days


def main():
    print("=" * 70)
    print("RESEARCH: Widen Threatened Strike Distance")
    print("=" * 70)
    print()

    dates_data = preload_all_data()
    print()

    variants = ["A", "B", "C", "D", "E", "F"]
    labels = {
        "A": "Baseline (equal dist)",
        "B": "+5 threatened side",
        "C": "+3 threatened side",
        "D": "+5 if |mom30|>0.05%",
        "E": "+5 threat / -3 safe",
        "F": "Graduated +3/+5",
    }

    results = []
    for v in variants:
        print(f"Running variant {v}: {labels[v]}...")
        r = run_backtest(v, dates_data)
        results.append(r)

    # ── print table ──
    print()
    print("=" * 110)
    print(f"{'Var':<4} {'Description':<25} {'P&L':>10} {'MaxDD':>10} {'WR%':>7} {'Trades':>7} {'Sharpe':>8} {'Calmar':>8} {'Worst':>10}")
    print("-" * 110)
    for r in results:
        v = r["variant"]
        print(
            f"{v:<4} {labels[v]:<25} "
            f"${r['total_pnl']:>9,.0f} "
            f"${r['max_dd']:>9,.0f} "
            f"{r['win_rate']:>6.1f}% "
            f"{r['trades']:>6d}  "
            f"{r['sharpe']:>7.2f} "
            f"{r['calmar']:>7.2f} "
            f"${r['worst_day']:>9,.0f}"
        )
    print("=" * 110)

    # highlight best
    best_sharpe = max(results, key=lambda x: x["sharpe"])
    best_pnl = max(results, key=lambda x: x["total_pnl"])
    print()
    print(f"Best Sharpe:  {best_sharpe['variant']} ({labels[best_sharpe['variant']]}) = {best_sharpe['sharpe']:.2f}")
    print(f"Best P&L:     {best_pnl['variant']} ({labels[best_pnl['variant']]}) = ${best_pnl['total_pnl']:,.0f}")
    print()


if __name__ == "__main__":
    main()
