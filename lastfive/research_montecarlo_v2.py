"""
Monte Carlo v2 — Rule C3 + Tiered Sizing (Config B from credit_sizing)

Generates the full trade list internally, then runs 10,000 Monte Carlo
shuffled simulations.  Compares to the fixed $2,500 baseline.

Rule C3: VIX<18 -> dist=0, 18-22 -> dist=3, 22-26 -> dist=5
Tiered:  credit<0.20 -> $1,500, 0.20-0.50 -> $2,500, >0.50 -> $3,500
Bidirectional, entry 15:55, exit 16:00, width $5, min credit $0.10,
afternoon<1.0%, commission $0.50/leg.
"""
from __future__ import annotations

import glob
import math
import os
import time

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

N_SIMS = 10_000
RUIN_50 = 25_000
RUIN_30 = 35_000

np.random.seed(42)


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


# ── Rule C3 ─────────────────────────────────────────────────────────────
def rule_c3_dist(vix: float) -> float:
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── Sizing rules ────────────────────────────────────────────────────────
def sizing_tiered(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


def sizing_fixed(credit: float) -> float:
    return 2500.0


# ── Simulate spread ────────────────────────────────────────────────────
def sim_call(spot, spx_exit, dist, quotes_df, entry_time, sizing_fn):
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
    if risk_amount is None:
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
    return gross - comm


def sim_put(spot, spx_exit, dist, quotes_df, entry_time, sizing_fn):
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
    if risk_amount is None:
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
    return gross - comm


# ── Generate trade list ─────────────────────────────────────────────────
def generate_trades(sizing_fn, label: str):
    print(f"\n  Generating trades for [{label}]...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = sorted({
        os.path.basename(f).replace(".parquet", "")
        for f in all_spx
        if START_DATE <= os.path.basename(f).replace(".parquet", "") <= END_DATE
    })

    trades = []
    day_pnls = []

    for ds in all_dates:
        if ds in MARKET_HOLIDAYS or ds in EARLY_CLOSE_DAYS:
            continue

        spx_df = load_spx(ds)
        if spx_df is None:
            continue
        vix_df = load_vix(ds)
        if vix_df is None:
            continue

        vix1550 = get_price_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0 or vix1550 > 26:
            continue

        aft_ret = compute_afternoon_return(spx_df)
        if aft_ret is not None and aft_ret >= 1.0:
            continue

        spot_row = spx_df[spx_df["hhmm"] == "15:55"]
        if spot_row.empty:
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
            continue

        dist = rule_c3_dist(vix1550)
        day_pnl = 0.0
        has_trade = False

        ct = sim_call(spot, spx_exit, dist, opts_df, "15:55:00", sizing_fn)
        if ct is not None:
            trades.append(ct)
            day_pnl += ct
            has_trade = True

        pt = sim_put(spot, spx_exit, dist, opts_df, "15:55:00", sizing_fn)
        if pt is not None:
            trades.append(pt)
            day_pnl += pt
            has_trade = True

        if has_trade:
            day_pnls.append(day_pnl)

    trades = np.array(trades)
    day_pnls = np.array(day_pnls)
    print(f"    {len(trades)} trades, {len(day_pnls)} trading days")
    print(f"    Total P&L: ${trades.sum():,.0f}")
    print(f"    Win rate: {(trades > 0).mean():.1%}")
    print(f"    Avg win:  ${trades[trades > 0].mean():,.0f}")
    print(f"    Avg loss: ${trades[trades < 0].mean():,.0f}")
    return trades, day_pnls


# ── Monte Carlo engine ──────────────────────────────────────────────────
def run_monte_carlo(pnl: np.ndarray, label: str):
    n = len(pnl)
    final_equities = np.zeros(N_SIMS)
    max_drawdowns = np.zeros(N_SIMS)
    max_consec_losses = np.zeros(N_SIMS, dtype=int)
    min_equities = np.zeros(N_SIMS)

    for i in range(N_SIMS):
        shuffled = np.random.permutation(pnl)
        equity_curve = STARTING_EQUITY + np.cumsum(shuffled)
        equity_with_start = np.concatenate([[STARTING_EQUITY], equity_curve])

        final_equities[i] = equity_curve[-1]
        min_equities[i] = equity_with_start.min()

        running_max = np.maximum.accumulate(equity_with_start)
        drawdowns = equity_with_start - running_max
        max_drawdowns[i] = drawdowns.min()

        losses = shuffled < 0
        max_streak = 0
        current_streak = 0
        for trade_is_loss in losses:
            if trade_is_loss:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        max_consec_losses[i] = max_streak

    return final_equities, max_drawdowns, max_consec_losses, min_equities


def print_mc_results(label, final_eq, max_dd, max_cl, min_eq, n_trades):
    total_pnl = final_eq - STARTING_EQUITY
    pcts = [5, 25, 50, 75, 95]

    print()
    print("=" * 70)
    print(f"  MONTE CARLO — {label} ({n_trades} trades, {N_SIMS:,} sims)")
    print("=" * 70)

    # Risk of ruin
    ror_50 = (min_eq < RUIN_50).mean()
    ror_30 = (min_eq < RUIN_30).mean()
    prob_profit = (final_eq > STARTING_EQUITY).mean()

    print(f"\n  Risk of Ruin (equity < $25k / 50% loss): {ror_50:.2%}")
    print(f"  Risk of Ruin (equity < $35k / 30% loss): {ror_30:.2%}")
    print(f"  Probability of Profit (end > $50k):      {prob_profit:.2%}")

    # Final equity
    fe_pcts = np.percentile(final_eq, pcts)
    print(f"\n  Final Equity Distribution:")
    for p, v in zip(pcts, fe_pcts):
        print(f"    {p:>3}th percentile: ${v:>12,.0f}")
    print(f"    Mean:            ${final_eq.mean():>12,.0f}")

    # P&L CI
    ci_low, ci_high = np.percentile(total_pnl, [2.5, 97.5])
    print(f"\n  95% Confidence Interval on Total P&L:")
    print(f"    [${ci_low:>+12,.0f}  to  ${ci_high:>+12,.0f}]")

    # Max DD
    dd_pcts = np.percentile(max_dd, pcts)
    print(f"\n  Max Drawdown Distribution:")
    for p, v in zip(pcts, dd_pcts):
        print(f"    {p:>3}th percentile: ${v:>12,.0f}")
    print(f"    Mean:            ${max_dd.mean():>12,.0f}")
    print(f"    Worst (min):     ${max_dd.min():>12,.0f}")

    # Consecutive losses
    cl_pcts = np.percentile(max_cl, pcts)
    print(f"\n  Max Consecutive Losses Distribution:")
    for p, v in zip(pcts, cl_pcts):
        print(f"    {p:>3}th percentile: {v:>5.0f}")
    print(f"    Mean:            {max_cl.mean():>5.1f}")
    print(f"    Worst (max):     {max_cl.max():>5d}")

    return {
        "ror_50": ror_50, "ror_30": ror_30, "prob_profit": prob_profit,
        "median_eq": np.median(final_eq), "mean_eq": final_eq.mean(),
        "median_pnl": np.median(total_pnl),
        "median_dd": np.median(max_dd), "mean_dd": max_dd.mean(),
        "worst_dd": max_dd.min(),
        "median_cl": np.median(max_cl), "max_cl": max_cl.max(),
        "ci_low": ci_low, "ci_high": ci_high,
        "p5_eq": fe_pcts[0], "p95_eq": fe_pcts[4],
        "p5_dd": dd_pcts[0], "p95_dd": dd_pcts[4],
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print("=" * 70)
    print("  Monte Carlo v2 — Rule C3 + Tiered Sizing vs Fixed $2,500 Baseline")
    print("=" * 70)

    # Generate trades for both configs
    tiered_trades, tiered_days = generate_trades(sizing_tiered, "C3 + Tiered")
    fixed_trades, fixed_days = generate_trades(sizing_fixed, "C3 + Fixed $2,500")

    # Run Monte Carlo for both
    print("\n  Running 10,000 Monte Carlo simulations (Tiered)...")
    t_fe, t_dd, t_cl, t_me = run_monte_carlo(tiered_trades, "Tiered")
    tiered_stats = print_mc_results(
        "RULE C3 + TIERED SIZING", t_fe, t_dd, t_cl, t_me, len(tiered_trades)
    )

    print("\n  Running 10,000 Monte Carlo simulations (Fixed)...")
    f_fe, f_dd, f_cl, f_me = run_monte_carlo(fixed_trades, "Fixed")
    fixed_stats = print_mc_results(
        "RULE C3 + FIXED $2,500 (BASELINE)", f_fe, f_dd, f_cl, f_me, len(fixed_trades)
    )

    # ── Side-by-side comparison ─────────────────────────────────────────
    print()
    print("=" * 70)
    print("  COMPARISON: TIERED vs FIXED BASELINE")
    print("=" * 70)

    rows = [
        ("Trades", f"{len(tiered_trades)}", f"{len(fixed_trades)}"),
        ("Total P&L (actual)", f"${tiered_trades.sum():>+,.0f}", f"${fixed_trades.sum():>+,.0f}"),
        ("Win Rate", f"{(tiered_trades>0).mean():.1%}", f"{(fixed_trades>0).mean():.1%}"),
        ("Risk of Ruin (50%)", f"{tiered_stats['ror_50']:.2%}", f"{fixed_stats['ror_50']:.2%}"),
        ("Risk of Ruin (30%)", f"{tiered_stats['ror_30']:.2%}", f"{fixed_stats['ror_30']:.2%}"),
        ("Prob of Profit", f"{tiered_stats['prob_profit']:.2%}", f"{fixed_stats['prob_profit']:.2%}"),
        ("Median Final Equity", f"${tiered_stats['median_eq']:>,.0f}", f"${fixed_stats['median_eq']:>,.0f}"),
        ("Median P&L", f"${tiered_stats['median_pnl']:>+,.0f}", f"${fixed_stats['median_pnl']:>+,.0f}"),
        ("95% CI P&L Low", f"${tiered_stats['ci_low']:>+,.0f}", f"${fixed_stats['ci_low']:>+,.0f}"),
        ("95% CI P&L High", f"${tiered_stats['ci_high']:>+,.0f}", f"${fixed_stats['ci_high']:>+,.0f}"),
        ("5th pct Final Equity", f"${tiered_stats['p5_eq']:>,.0f}", f"${fixed_stats['p5_eq']:>,.0f}"),
        ("95th pct Final Equity", f"${tiered_stats['p95_eq']:>,.0f}", f"${fixed_stats['p95_eq']:>,.0f}"),
        ("Median Max DD", f"${tiered_stats['median_dd']:>,.0f}", f"${fixed_stats['median_dd']:>,.0f}"),
        ("Mean Max DD", f"${tiered_stats['mean_dd']:>,.0f}", f"${fixed_stats['mean_dd']:>,.0f}"),
        ("Worst Max DD", f"${tiered_stats['worst_dd']:>,.0f}", f"${fixed_stats['worst_dd']:>,.0f}"),
        ("5th pct DD", f"${tiered_stats['p5_dd']:>,.0f}", f"${fixed_stats['p5_dd']:>,.0f}"),
        ("95th pct DD", f"${tiered_stats['p95_dd']:>,.0f}", f"${fixed_stats['p95_dd']:>,.0f}"),
        ("Median Consec Losses", f"{tiered_stats['median_cl']:.0f}", f"{fixed_stats['median_cl']:.0f}"),
        ("Worst Consec Losses", f"{tiered_stats['max_cl']}", f"{fixed_stats['max_cl']}"),
    ]

    print(f"\n  {'Metric':<25} {'Tiered':>22} {'Fixed $2,500':>22}")
    print(f"  {'-'*25} {'-'*22} {'-'*22}")
    for name, tv, fv in rows:
        print(f"  {name:<25} {tv:>22} {fv:>22}")

    elapsed = time.time() - t0
    print(f"\n  Elapsed: {elapsed:.0f}s")

    # ── Summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    delta_pnl = tiered_trades.sum() - fixed_trades.sum()
    delta_dd = tiered_stats["median_dd"] - fixed_stats["median_dd"]
    print(f"""
  Tiered sizing (C3 + credit-weighted) vs Fixed $2,500:
  - Tiered P&L delta:   ${delta_pnl:>+,.0f}
  - Tiered median DD:   ${tiered_stats['median_dd']:>,.0f}  vs  ${fixed_stats['median_dd']:>,.0f}  (delta: ${delta_dd:>+,.0f})
  - Tiered risk of ruin (50%): {tiered_stats['ror_50']:.2%}  vs  {fixed_stats['ror_50']:.2%}
  - Tiered risk of ruin (30%): {tiered_stats['ror_30']:.2%}  vs  {fixed_stats['ror_30']:.2%}
  - Tiered prob of profit:     {tiered_stats['prob_profit']:.2%}  vs  {fixed_stats['prob_profit']:.2%}
  - Tiered 95% CI: [${tiered_stats['ci_low']:>+,.0f} to ${tiered_stats['ci_high']:>+,.0f}]
  - Fixed  95% CI: [${fixed_stats['ci_low']:>+,.0f} to ${fixed_stats['ci_high']:>+,.0f}]
""")


if __name__ == "__main__":
    main()
