"""Research: Max-Loss-Budget Position Sizing for LastFive strategy.

Instead of sizing by risk / (width - credit), cap the ABSOLUTE worst case:
  qty = floor(max_loss_budget / (width * 100))

This guarantees worst-case loss never exceeds max_loss_budget regardless of credit.

Tests seven configs (A-G) with Rule C3 VIX-adaptive, bidirectional,
width $5, min credit $0.10, commission $0.50/leg, entry 15:55, exit 16:00.
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

# -- Config ------------------------------------------------------------------
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

# -- Put momentum filter thresholds (stacked) --------------------------------
PUT_AFTERNOON_MIN = -0.75  # skip put if afternoon_ret < this AND ...
PUT_MOM30_MIN = -0.10      # ... 30-min momentum < this


# -- Data loaders ------------------------------------------------------------
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


# -- Helpers -----------------------------------------------------------------
def get_price_at(df: pd.DataFrame, hhmm: str) -> float | None:
    rows = df[df["hhmm"] == hhmm]
    if rows.empty:
        return None
    val = float(rows["close"].iloc[0])
    return val if val > 0 else None


def compute_afternoon_return(spx_df: pd.DataFrame) -> float | None:
    """SPX return from 13:00 to 15:54 (raw, not abs)."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """SPX 30-min momentum (15:25 to 15:54)."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    start = float(bars["close"].iloc[0])
    end = float(bars["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def get_quote(quotes_df: pd.DataFrame, right: str, strike: int,
              time_str: str) -> dict | None:
    mask = (
        (quotes_df["right"] == right)
        & (quotes_df["strike"] == strike)
        & (quotes_df["time_str"] == time_str)
    )
    rows = quotes_df[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


# -- Rule C3 adaptive distance ----------------------------------------------
def rule_c3_dist(vix: float) -> float:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# -- Sizing functions --------------------------------------------------------
def tiered_risk(credit: float) -> float:
    """Current baseline tiered sizing: risk amount by credit level."""
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


def tiered_qty(credit: float) -> int:
    """A: Current tiered sizing. qty = floor(tier_risk / ((width-credit)*100))."""
    risk = tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return 1
    return max(1, int(risk / max_loss_per))


def max_loss_qty(budget: float) -> int:
    """Fixed qty from max-loss budget: floor(budget / (width * 100))."""
    return max(1, int(budget / (SPREAD_WIDTH * 100.0)))


def hybrid_qty(credit: float, cap_budget: float) -> int:
    """Hybrid: min of tiered qty and max-loss-capped qty."""
    t_qty = tiered_qty(credit)
    m_qty = max_loss_qty(cap_budget)
    return min(t_qty, m_qty)


# Config labels and sizing logic
CONFIGS = {
    "A_tiered":        lambda cr: tiered_qty(cr),
    "B_mlb_1000":      lambda cr: max_loss_qty(1000),
    "C_mlb_1500":      lambda cr: max_loss_qty(1500),
    "D_mlb_2000":      lambda cr: max_loss_qty(2000),
    "E_mlb_2500":      lambda cr: max_loss_qty(2500),
    "F_hybrid_1500":   lambda cr: hybrid_qty(cr, 1500),
    "G_hybrid_2000":   lambda cr: hybrid_qty(cr, 2000),
}


# -- Simulate one side -------------------------------------------------------
def sim_call(spot, spx_exit, dist, quotes_df, entry_time, qty_fn):
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

    qty = qty_fn(credit)

    if spx_exit <= short_strike:
        settlement = 0.0
    elif spx_exit >= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = spx_exit - short_strike

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm
    worst_case = qty * SPREAD_WIDTH * 100.0  # absolute worst if full loss

    return {"side": "CALL", "credit": credit, "pnl": net, "qty": qty,
            "worst_case": worst_case, "date": None}


def sim_put(spot, spx_exit, dist, quotes_df, entry_time, qty_fn):
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

    qty = qty_fn(credit)

    if spx_exit >= short_strike:
        settlement = 0.0
    elif spx_exit <= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm
    worst_case = qty * SPREAD_WIDTH * 100.0

    return {"side": "PUT", "credit": credit, "pnl": net, "qty": qty,
            "worst_case": worst_case, "date": None}


# -- Main --------------------------------------------------------------------
def main():
    print("=" * 120)
    print("Max-Loss-Budget Position Sizing — Rule C3 Adaptive, Bidirectional")
    print("=" * 120)

    # Collect dates
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    # Pre-collect day data with all filters
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

        # VIX intraday filter: skip if VIX(15:50) > 26
        if vix1550 > 26:
            skipped["vix_filter"] += 1
            continue

        # Afternoon return filter: skip if >= 1.0%
        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            skipped["afternoon_filter"] += 1
            continue

        # 30-min momentum for put filter
        mom30 = compute_mom30(spx_df)

        # Put momentum filter: skip put if afternoon < -0.75% AND mom30 < -0.10%
        skip_put = False
        if (aft_ret is not None and aft_ret < PUT_AFTERNOON_MIN
                and mom30 is not None and mom30 < PUT_MOM30_MIN):
            skip_put = True

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
            "skip_put": skip_put,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # -- Run each config -------------------------------------------------------
    results = {}

    for cfg_name, qty_fn in CONFIGS.items():
        trades = []
        day_pnls = []
        day_dates = []

        for dd in day_data:
            dist = rule_c3_dist(dd["vix1550"])
            day_pnl = 0.0
            has_trade = False

            ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                          "15:55:00", qty_fn)
            if ct is not None:
                ct["date"] = dd["date"]
                trades.append(ct)
                day_pnl += ct["pnl"]
                has_trade = True

            if not dd["skip_put"]:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"],
                             "15:55:00", qty_fn)
                if pt is not None:
                    pt["date"] = dd["date"]
                    trades.append(pt)
                    day_pnl += pt["pnl"]
                    has_trade = True

            if has_trade:
                day_pnls.append(day_pnl)
                day_dates.append(dd["date"])

        n_trades = len(trades)
        if n_trades == 0:
            results[cfg_name] = None
            continue

        day_pnl_arr = np.array(day_pnls)
        total_pnl = day_pnl_arr.sum()

        # Equity curve and drawdown
        equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = equity - peak
        max_dd = dd_arr.min()
        max_dd_pct = (dd_arr / peak).min() * 100

        # Win rate
        n_wins = sum(1 for t in trades if t["pnl"] > 0)
        wr = n_wins / n_trades * 100

        # Sharpe
        daily_rf = ANNUAL_RF / 252
        excess = day_pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = (excess.mean() / day_pnl_arr.std() * np.sqrt(252)
                  if day_pnl_arr.std() > 0 else 0)

        # Calmar
        years = len(day_pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # Worst day (sum of all trade P&Ls on that day)
        worst_day_idx = int(np.argmin(day_pnl_arr))
        worst_day_pnl = day_pnl_arr[worst_day_idx]
        worst_day_date = day_dates[worst_day_idx]

        # Worst single trade
        trade_pnls = np.array([t["pnl"] for t in trades])
        worst_trade_idx = int(np.argmin(trade_pnls))
        worst_trade_pnl = trade_pnls[worst_trade_idx]
        worst_trade_date = trades[worst_trade_idx]["date"]

        # Avg qty
        avg_qty = np.mean([t["qty"] for t in trades])

        # Max worst-case exposure (theoretical)
        max_worst_case = max(t["worst_case"] for t in trades)

        # Profit factor
        gross_profit = trade_pnls[trade_pnls > 0].sum()
        gross_loss = abs(trade_pnls[trade_pnls < 0].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Avg win / avg loss
        wins_arr = trade_pnls[trade_pnls > 0]
        loss_arr = trade_pnls[trade_pnls < 0]
        avg_win = wins_arr.mean() if len(wins_arr) > 0 else 0
        avg_loss = loss_arr.mean() if len(loss_arr) > 0 else 0

        # Call/put split
        n_calls = sum(1 for t in trades if t["side"] == "CALL")
        n_puts = sum(1 for t in trades if t["side"] == "PUT")

        results[cfg_name] = {
            "trades": n_trades,
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_pnl": total_pnl,
            "max_dd": max_dd,
            "max_dd_pct": max_dd_pct,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "pf": pf,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "avg_qty": avg_qty,
            "worst_day_pnl": worst_day_pnl,
            "worst_day_date": worst_day_date,
            "worst_trade_pnl": worst_trade_pnl,
            "worst_trade_date": worst_trade_date,
            "max_worst_case": max_worst_case,
        }

    # -- Print comparison table ------------------------------------------------
    print("\n" + "=" * 160)
    print("COMPARISON: Max-Loss-Budget Position Sizing (Rule C3 Adaptive, Bidirectional, Put Momentum Filter)")
    print("=" * 160)
    print(f"{'Config':<16} {'Trades':>7} {'P&L':>11} {'Max DD':>9} {'DD%':>7} "
          f"{'WR%':>6} {'Sharpe':>7} {'Calmar':>8} {'PF':>6} "
          f"{'Avg Qty':>8} {'Worst Day':>11} {'Worst Trd':>11} {'Max WC':>9}")
    print("-" * 160)

    for name in CONFIGS:
        r = results.get(name)
        if r is None:
            print(f"{name:<16}  NO TRADES")
            continue
        print(
            f"{name:<16} {r['trades']:>7} "
            f"${r['total_pnl']:>9,.0f} ${r['max_dd']:>7,.0f} "
            f"{r['max_dd_pct']:>6.1f}% "
            f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} "
            f"{r['avg_qty']:>8.1f} "
            f"${r['worst_day_pnl']:>9,.0f} "
            f"${r['worst_trade_pnl']:>9,.0f} "
            f"${r['max_worst_case']:>7,.0f}"
        )
    print("=" * 160)

    # -- Detailed per-config info -----------------------------------------------
    print("\n--- Config Descriptions ---")
    print("  A_tiered      : CURRENT BASELINE. qty = floor(tier_risk / ((5-credit)*100)). No worst-case cap.")
    print("                  Tiers: cr<$0.20 -> $1,500 risk | $0.20-$0.50 -> $2,500 | >$0.50 -> $3,500")
    print("  B_mlb_1000    : Max-loss budget $1,000. qty = floor(1000/500) = 2. Worst case = $1,000.")
    print("  C_mlb_1500    : Max-loss budget $1,500. qty = 3. Worst case = $1,500.")
    print("  D_mlb_2000    : Max-loss budget $2,000. qty = 4. Worst case = $2,000.")
    print("  E_mlb_2500    : Max-loss budget $2,500. qty = 5. Worst case = $2,500.")
    print("  F_hybrid_1500 : min(tiered_qty, 3). Tiered sizing capped at 3 contracts.")
    print("  G_hybrid_2000 : min(tiered_qty, 4). Tiered sizing capped at 4 contracts.")

    # -- Delta vs baseline -------------------------------------------------------
    baseline = results.get("A_tiered")
    if baseline:
        print("\n--- Delta vs A_tiered Baseline ---")
        print(f"{'Config':<16} {'dP&L':>11} {'dDD':>9} {'dSharpe':>9} {'dCalmar':>9} "
              f"{'dAvgQty':>9} {'dWorstDay':>11}")
        print("-" * 80)
        for name in CONFIGS:
            if name == "A_tiered":
                continue
            r = results.get(name)
            if r is None:
                print(f"  {name:<14}: NO TRADES")
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dsh = r["sharpe"] - baseline["sharpe"]
            dcal = r["calmar"] - baseline["calmar"]
            dqty = r["avg_qty"] - baseline["avg_qty"]
            dwd = r["worst_day_pnl"] - baseline["worst_day_pnl"]
            print(
                f"{name:<16} ${dpnl:>+9,.0f} ${ddd:>+7,.0f} "
                f"{dsh:>+9.2f} {dcal:>+9.2f} "
                f"{dqty:>+9.1f} ${dwd:>+9,.0f}"
            )

    # -- Worst days detail -------------------------------------------------------
    print("\n--- Worst Day and Worst Trade Details ---")
    for name in CONFIGS:
        r = results.get(name)
        if r is None:
            continue
        print(f"  {name:<16}: worst day {r['worst_day_date']} ${r['worst_day_pnl']:>+,.0f}  |  "
              f"worst trade {r['worst_trade_date']} ${r['worst_trade_pnl']:>+,.0f}  |  "
              f"max theoretical WC per trade = ${r['max_worst_case']:,.0f}")

    # -- Avg win/loss detail ------------------------------------------------------
    print("\n--- Avg Win / Avg Loss ---")
    print(f"{'Config':<16} {'Avg Win':>10} {'Avg Loss':>10} {'Win/Loss':>10}")
    print("-" * 50)
    for name in CONFIGS:
        r = results.get(name)
        if r is None:
            continue
        ratio = abs(r["avg_win"] / r["avg_loss"]) if r["avg_loss"] != 0 else 0
        print(f"{name:<16} ${r['avg_win']:>8,.0f} ${r['avg_loss']:>8,.0f} {ratio:>10.2f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
