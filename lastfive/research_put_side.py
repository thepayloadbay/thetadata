"""Research: Can we add PUT credit spreads alongside the existing CALL credit spreads?

Tests three modes:
  1. PUT-only  — sell put spread at 15:55, same filters as call side
  2. CALL-only — existing strategy (for comparison)
  3. BOTH      — iron condor style: call spread + put spread on same day
"""
from __future__ import annotations

import math
import os
import glob
from datetime import datetime

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_SHORT_DISTANCE = 3.0  # $3 below spot for puts, $3 above for calls (matching current config)
STRIKE_STEP = 5
RISK_AMOUNT = 2500.0
MIN_NET_CREDIT = 0.10
COMMISSION_PER_LEG = 0.50

# Filters
VIX_INTRADAY_MAX = 26.0
AFTERNOON_RETURN_MAX = 1.0  # percent

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


# ── Data helpers ────────────────────────────────────────────────────────
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


def load_quotes(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
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


def get_price(df: pd.DataFrame, time_str: str) -> float | None:
    rows = df[df["time_str"] == time_str]
    if rows.empty:
        rows = df[df["hhmm"] == time_str[:5]]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def get_quote(qdf: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    mask = (qdf["right"] == right) & (qdf["strike"] == strike) & (qdf["time_str"] == time_str)
    rows = qdf[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


def afternoon_return(spx_df: pd.DataFrame) -> float | None:
    af = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(af) < 2:
        return None
    s = float(af["close"].iloc[0])
    e = float(af["close"].iloc[-1])
    if s <= 0:
        return None
    return (e - s) / s * 100


# ── Trade logic ─────────────────────────────────────────────────────────
def try_call_spread(spot: float, qdf: pd.DataFrame, spx_exit: float, etime: str):
    """Try to open a call credit spread. Returns trade dict or None."""
    # Short call: lowest strike >= spot + MIN_SHORT_DISTANCE, rounded up to STRIKE_STEP
    target = spot + MIN_SHORT_DISTANCE
    short_k = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_k = short_k + int(SPREAD_WIDTH)

    available = set(qdf["strike"].unique())
    if short_k not in available or long_k not in available:
        # Try next available strike
        candidates = sorted(s for s in available if s >= target)
        if not candidates:
            return None
        short_k = int(candidates[0])
        long_k = short_k + int(SPREAD_WIDTH)
        if long_k not in available:
            return None

    sq = get_quote(qdf, "C", short_k, etime)
    lq = get_quote(qdf, "C", long_k, etime)
    if sq is None or lq is None:
        return None

    credit = sq["bid"] - lq["ask"]
    if credit < MIN_NET_CREDIT:
        return None

    max_loss_pc = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(RISK_AMOUNT / max_loss_pc)) if max_loss_pc > 0 else 1

    # Settlement
    if spx_exit <= short_k:
        settlement = 0.0
    elif spx_exit >= long_k:
        settlement = SPREAD_WIDTH
    else:
        settlement = spx_exit - short_k

    commission = 4 * qty * COMMISSION_PER_LEG
    pnl = (credit - settlement) * qty * 100.0 - commission
    return {"side": "CALL", "short": short_k, "long": long_k, "qty": qty,
            "credit": credit, "settlement": settlement, "pnl": pnl}


def try_put_spread(spot: float, qdf: pd.DataFrame, spx_exit: float, etime: str):
    """Try to open a put credit spread. Returns trade dict or None."""
    # Short put: $3 below spot, rounded DOWN to 5-pt grid
    target = spot - MIN_SHORT_DISTANCE
    short_k = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_k = short_k - int(SPREAD_WIDTH)

    available = set(qdf["strike"].unique())
    if short_k not in available or long_k not in available:
        # Try next available strike down
        candidates = sorted((s for s in available if s <= target), reverse=True)
        if not candidates:
            return None
        short_k = int(candidates[0])
        long_k = short_k - int(SPREAD_WIDTH)
        if long_k not in available:
            return None

    sq = get_quote(qdf, "P", short_k, etime)
    lq = get_quote(qdf, "P", long_k, etime)
    if sq is None or lq is None:
        return None

    credit = sq["bid"] - lq["ask"]
    if credit < MIN_NET_CREDIT:
        return None

    max_loss_pc = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(RISK_AMOUNT / max_loss_pc)) if max_loss_pc > 0 else 1

    # Settlement: put spread
    if spx_exit >= short_k:
        settlement = 0.0  # OTM, full win
    elif spx_exit <= long_k:
        settlement = SPREAD_WIDTH  # max loss
    else:
        settlement = short_k - spx_exit

    commission = 4 * qty * COMMISSION_PER_LEG
    pnl = (credit - settlement) * qty * 100.0 - commission
    return {"side": "PUT", "short": short_k, "long": long_k, "qty": qty,
            "credit": credit, "settlement": settlement, "pnl": pnl}


# ── Metrics ─────────────────────────────────────────────────────────────
def compute_metrics(pnls: list[float], label: str, starting_eq: float = 50000.0):
    if not pnls:
        print(f"  {label}: NO TRADES")
        return {}
    arr = np.array(pnls)
    equity = starting_eq + np.cumsum(arr)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    max_dd = float(dd.min())
    total = float(arr.sum())
    wins = int((arr > 0).sum())
    losses = int((arr <= 0).sum())
    wr = wins / len(arr) * 100
    avg_win = float(arr[arr > 0].mean()) if wins > 0 else 0
    avg_loss = float(arr[arr <= 0].mean()) if losses > 0 else 0

    # Annualized Sharpe (252 trading days)
    if len(arr) > 1 and arr.std() > 0:
        daily_mean = arr.mean()
        daily_std = arr.std()
        sharpe = (daily_mean / daily_std) * np.sqrt(252)
    else:
        sharpe = 0.0

    calmar = abs(total / max_dd) if max_dd != 0 else 0.0

    return {
        "label": label,
        "trades": len(arr),
        "total_pnl": total,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "wins": wins,
        "losses": losses,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  RESEARCH: PUT CREDIT SPREADS + IRON CONDOR")
    print("=" * 70)

    # Collect all trading dates
    all_dates = set()
    for fpath in glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")):
        d = os.path.basename(fpath).replace(".parquet", "")
        if START_DATE <= d <= END_DATE:
            all_dates.add(d)
    trading_dates = sorted(all_dates)
    print(f"Total date candidates: {len(trading_dates)}")

    call_pnls = []
    put_pnls = []
    both_pnls = []  # combined P&L on days where both trade

    call_only_pnls = []  # call trades on days put didn't trade
    put_only_pnls = []   # put trades on days call didn't trade

    skip_counts = {"holiday": 0, "early_close": 0, "no_data": 0, "filter": 0,
                   "no_spx_entry": 0, "no_spx_exit": 0}
    trade_count = 0

    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS:
            skip_counts["holiday"] += 1
            continue
        if date_str in EARLY_CLOSE_DAYS:
            skip_counts["early_close"] += 1
            continue

        spx_df = load_spx(date_str)
        if spx_df is None:
            skip_counts["no_data"] += 1
            continue

        # VIX filter at 15:50
        vix_df = load_vix(date_str)
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                skip_counts["filter"] += 1
                continue

        # Afternoon return filter
        af_ret = afternoon_return(spx_df)
        if af_ret is not None and af_ret >= AFTERNOON_RETURN_MAX:
            skip_counts["filter"] += 1
            continue

        # SPX at entry (15:55)
        spot = get_price(spx_df, "15:55:00")
        if spot is None:
            skip_counts["no_spx_entry"] += 1
            continue

        # SPX at exit (16:00 or fallback)
        spx_exit = get_price(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = get_price(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])
        if spx_exit is None or spx_exit <= 0:
            skip_counts["no_spx_exit"] += 1
            continue

        # Load quotes
        qdf = load_quotes(date_str)
        if qdf is None:
            skip_counts["no_data"] += 1
            continue

        etime = "15:55:00"
        call_trade = try_call_spread(spot, qdf, spx_exit, etime)
        put_trade = try_put_spread(spot, qdf, spx_exit, etime)

        if call_trade:
            call_pnls.append(call_trade["pnl"])
        if put_trade:
            put_pnls.append(put_trade["pnl"])

        if call_trade and put_trade:
            both_pnls.append(call_trade["pnl"] + put_trade["pnl"])
        elif call_trade and not put_trade:
            call_only_pnls.append(call_trade["pnl"])
        elif put_trade and not call_trade:
            put_only_pnls.append(put_trade["pnl"])

        trade_count += 1

    # ── Results ─────────────────────────────────────────────────────────
    print(f"\nSkip reasons: {skip_counts}")
    print(f"Days with at least one side tradeable: {trade_count}")

    results = []
    results.append(compute_metrics(call_pnls, "CALL-only (existing)"))
    results.append(compute_metrics(put_pnls, "PUT-only"))
    # BOTH = iron condor: sum of call+put P&L on every day where BOTH traded
    results.append(compute_metrics(both_pnls, "BOTH (iron condor days)"))

    # Combined equity: all call trades + all put trades (independent sizing)
    all_combined = []
    ci, pi = 0, 0
    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        # We need to re-derive which days had call/put — use index tracking
    # Simpler: just concatenate call_pnls + put_pnls in order isn't right either.
    # Instead, let's track day-by-day combined P&L properly.

    # Re-run collecting combined daily P&L
    combined_daily = []
    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue

        spx_df = load_spx(date_str)
        if spx_df is None:
            continue

        vix_df = load_vix(date_str)
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                continue

        af_ret = afternoon_return(spx_df)
        if af_ret is not None and af_ret >= AFTERNOON_RETURN_MAX:
            continue

        spot = get_price(spx_df, "15:55:00")
        if spot is None:
            continue

        spx_exit = get_price(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = get_price(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])
        if spx_exit is None or spx_exit <= 0:
            continue

        qdf = load_quotes(date_str)
        if qdf is None:
            continue

        etime = "15:55:00"
        ct = try_call_spread(spot, qdf, spx_exit, etime)
        pt = try_put_spread(spot, qdf, spx_exit, etime)
        day_pnl = 0.0
        if ct:
            day_pnl += ct["pnl"]
        if pt:
            day_pnl += pt["pnl"]
        if ct or pt:
            combined_daily.append(day_pnl)

    results.append(compute_metrics(combined_daily, "COMBINED (call+put all days)"))

    # Print table
    print("\n" + "=" * 100)
    print(f"{'Strategy':<30} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'Sharpe':>8} {'Calmar':>8} {'WR%':>7} {'Avg Win':>9} {'Avg Loss':>10}")
    print("-" * 100)
    for r in results:
        if not r:
            continue
        print(f"{r['label']:<30} {r['trades']:>7,} {r['total_pnl']:>12,.0f} {r['max_dd']:>10,.0f} {r['sharpe']:>8.2f} {r['calmar']:>8.2f} {r['win_rate']:>6.1f}% {r['avg_win']:>9,.0f} {r['avg_loss']:>10,.0f}")
    print("=" * 100)

    # Year-by-year breakdown for PUT-only
    print("\n\nYEAR-BY-YEAR P&L BREAKDOWN:")
    print("-" * 80)

    # Rerun to get per-year data
    yearly_call = {}
    yearly_put = {}
    yearly_both = {}

    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        year = date_str[:4]

        spx_df = load_spx(date_str)
        if spx_df is None:
            continue

        vix_df = load_vix(date_str)
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                continue

        af_ret = afternoon_return(spx_df)
        if af_ret is not None and af_ret >= AFTERNOON_RETURN_MAX:
            continue

        spot = get_price(spx_df, "15:55:00")
        if spot is None:
            continue

        spx_exit = get_price(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = get_price(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])
        if spx_exit is None or spx_exit <= 0:
            continue

        qdf = load_quotes(date_str)
        if qdf is None:
            continue

        etime = "15:55:00"
        ct = try_call_spread(spot, qdf, spx_exit, etime)
        pt = try_put_spread(spot, qdf, spx_exit, etime)

        if ct:
            yearly_call.setdefault(year, []).append(ct["pnl"])
        if pt:
            yearly_put.setdefault(year, []).append(pt["pnl"])
        if ct or pt:
            day_pnl = (ct["pnl"] if ct else 0) + (pt["pnl"] if pt else 0)
            yearly_both.setdefault(year, []).append(day_pnl)

    print(f"{'Year':<6} {'CALL P&L':>12} {'CALL Trades':>12} {'PUT P&L':>12} {'PUT Trades':>12} {'COMBINED':>12} {'COMB Trades':>12}")
    for year in sorted(set(list(yearly_call.keys()) + list(yearly_put.keys()))):
        cp = yearly_call.get(year, [])
        pp = yearly_put.get(year, [])
        bp = yearly_both.get(year, [])
        print(f"{year:<6} {sum(cp):>12,.0f} {len(cp):>12} {sum(pp):>12,.0f} {len(pp):>12} {sum(bp):>12,.0f} {len(bp):>12}")

    # Worst 10 days for PUT side
    print("\n\nWORST 10 DAYS — PUT SPREAD:")
    print("-" * 80)
    put_trades_detail = []
    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue

        spx_df = load_spx(date_str)
        if spx_df is None:
            continue

        vix_df = load_vix(date_str)
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                continue

        af_ret = afternoon_return(spx_df)
        if af_ret is not None and af_ret >= AFTERNOON_RETURN_MAX:
            continue

        spot = get_price(spx_df, "15:55:00")
        if spot is None:
            continue
        spx_exit = get_price(spx_df, "16:00:00")
        if spx_exit is None:
            spx_exit = get_price(spx_df, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])
        if spx_exit is None or spx_exit <= 0:
            continue

        qdf = load_quotes(date_str)
        if qdf is None:
            continue

        pt = try_put_spread(spot, qdf, spx_exit, "15:55:00")
        if pt:
            put_trades_detail.append({
                "date": date_str, "spot": spot, "exit": spx_exit,
                "short": pt["short"], "long": pt["long"],
                "credit": pt["credit"], "settlement": pt["settlement"],
                "qty": pt["qty"], "pnl": pt["pnl"],
                "move": spx_exit - spot,
            })

    put_trades_detail.sort(key=lambda x: x["pnl"])
    for t in put_trades_detail[:10]:
        print(f"  {t['date']}  SPX {t['spot']:.0f}->{t['exit']:.0f} (move {t['move']:+.1f})  "
              f"P {t['short']}/{t['long']} x{t['qty']}  "
              f"credit=${t['credit']:.2f}  settle=${t['settlement']:.2f}  P&L=${t['pnl']:+,.0f}")

    print("\n\nBEST 10 DAYS — PUT SPREAD:")
    print("-" * 80)
    for t in put_trades_detail[-10:]:
        print(f"  {t['date']}  SPX {t['spot']:.0f}->{t['exit']:.0f} (move {t['move']:+.1f})  "
              f"P {t['short']}/{t['long']} x{t['qty']}  "
              f"credit=${t['credit']:.2f}  settle=${t['settlement']:.2f}  P&L=${t['pnl']:+,.0f}")


if __name__ == "__main__":
    main()
