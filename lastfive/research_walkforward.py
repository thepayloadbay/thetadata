"""Walk-forward validation: Train (2022-01-03 to 2024-12-31) vs Test (2025-01-01 to 2026-03-25).

Runs the FULL strategy (call + put) on each period separately with identical logic.
Reports per-period and per-year stats to check out-of-sample stability.
"""
from __future__ import annotations

import math
import os
import glob
from collections import defaultdict

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"

SPREAD_WIDTH = 5.0
MIN_SHORT_DISTANCE = 3.0
STRIKE_STEP = 5
RISK_AMOUNT = 2500.0
MIN_NET_CREDIT = 0.10
COMMISSION_PER_LEG = 0.50
STARTING_EQUITY = 50_000.0

VIX_INTRADAY_MAX = 26.0
AFTERNOON_RETURN_MAX = 1.0

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

# Period definitions
TRAIN_START = "20220103"
TRAIN_END   = "20241231"
TEST_START  = "20250101"
TEST_END    = "20260325"


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


def afternoon_return(spx_df: pd.DataFrame) -> float | None:
    af = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(af) < 2:
        return None
    s = float(af["close"].iloc[0])
    e = float(af["close"].iloc[-1])
    if s <= 0:
        return None
    return (e - s) / s * 100


def get_quote(qdf: pd.DataFrame, right: str, strike: int, time_str: str) -> dict | None:
    mask = (qdf["right"] == right) & (qdf["strike"] == strike) & (qdf["time_str"] == time_str)
    rows = qdf[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


# ── Trade logic ─────────────────────────────────────────────────────────
def try_call_spread(spot: float, qdf: pd.DataFrame, spx_exit: float, etime: str):
    target = spot + MIN_SHORT_DISTANCE
    short_k = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
    long_k = short_k + int(SPREAD_WIDTH)

    available = set(qdf["strike"].unique())
    if short_k not in available or long_k not in available:
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
    target = spot - MIN_SHORT_DISTANCE
    short_k = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
    long_k = short_k - int(SPREAD_WIDTH)

    available = set(qdf["strike"].unique())
    if short_k not in available or long_k not in available:
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

    if spx_exit >= short_k:
        settlement = 0.0
    elif spx_exit <= long_k:
        settlement = SPREAD_WIDTH
    else:
        settlement = short_k - spx_exit

    commission = 4 * qty * COMMISSION_PER_LEG
    pnl = (credit - settlement) * qty * 100.0 - commission
    return {"side": "PUT", "short": short_k, "long": long_k, "qty": qty,
            "credit": credit, "settlement": settlement, "pnl": pnl}


# ── Run backtest on a date range ───────────────────────────────────────
def run_period(all_dates: list[str], start: str, end: str) -> list[dict]:
    """Run strategy on dates within [start, end]. Returns list of trade dicts."""
    trades = []
    for date_str in all_dates:
        if date_str < start or date_str > end:
            continue
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue

        spx_df = load_spx(date_str)
        if spx_df is None:
            continue

        # VIX filter at 15:50
        vix_df = load_vix(date_str)
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                continue

        # Afternoon return filter
        af_ret = afternoon_return(spx_df)
        if af_ret is not None and af_ret >= AFTERNOON_RETURN_MAX:
            continue

        # SPX at entry
        spot = get_price(spx_df, "15:55:00")
        if spot is None:
            continue

        # SPX at exit
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
            trades.append({"date": date_str, "year": date_str[:4], **ct})
        if pt:
            trades.append({"date": date_str, "year": date_str[:4], **pt})

    return trades


# ── Metrics ─────────────────────────────────────────────────────────────
def compute_metrics(trades: list[dict], label: str, years_duration: float | None = None) -> dict:
    if not trades:
        return {"label": label, "trades": 0, "total_pnl": 0, "max_dd": 0,
                "sharpe": 0, "profit_factor": 0, "win_rate": 0, "cagr": 0,
                "avg_win": 0, "avg_loss": 0, "wins": 0, "losses": 0}

    pnls = np.array([t["pnl"] for t in trades])
    equity = STARTING_EQUITY + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    max_dd = float(dd.min())
    total = float(pnls.sum())
    wins = int((pnls > 0).sum())
    losses = int((pnls <= 0).sum())
    wr = wins / len(pnls) * 100
    avg_win = float(pnls[pnls > 0].mean()) if wins > 0 else 0
    avg_loss = float(pnls[pnls <= 0].mean()) if losses > 0 else 0
    gross_profit = float(pnls[pnls > 0].sum()) if wins > 0 else 0
    gross_loss = abs(float(pnls[pnls <= 0].sum())) if losses > 0 else 0.001
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    # Trade-level Sharpe (annualized by sqrt(252))
    if len(pnls) > 1 and pnls.std() > 0:
        sharpe = (pnls.mean() / pnls.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    # CAGR
    final_eq = STARTING_EQUITY + total
    if years_duration and years_duration > 0 and final_eq > 0:
        cagr = (final_eq / STARTING_EQUITY) ** (1 / years_duration) - 1
    else:
        cagr = 0.0

    return {
        "label": label, "trades": len(pnls), "total_pnl": total,
        "max_dd": max_dd, "sharpe": sharpe, "profit_factor": pf,
        "win_rate": wr, "cagr": cagr * 100,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "wins": wins, "losses": losses,
    }


def print_table(results: list[dict], title: str):
    print(f"\n{'=' * 120}")
    print(f"  {title}")
    print(f"{'=' * 120}")
    print(f"{'Label':<35} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'WR%':>7} {'Sharpe':>8} {'PF':>7} {'CAGR':>8} {'Avg Win':>9} {'Avg Loss':>10}")
    print(f"{'-' * 120}")
    for r in results:
        if r["trades"] == 0:
            print(f"{r['label']:<35} {'NO TRADES':>7}")
            continue
        print(f"{r['label']:<35} {r['trades']:>7,} {r['total_pnl']:>12,.0f} {r['max_dd']:>10,.0f} {r['win_rate']:>6.1f}% {r['sharpe']:>8.2f} {r['profit_factor']:>7.2f} {r['cagr']:>7.1f}% {r['avg_win']:>9,.0f} {r['avg_loss']:>10,.0f}")
    print(f"{'=' * 120}")


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 120)
    print("  WALK-FORWARD VALIDATION: Train (2022-2024) vs Test (2025-2026)")
    print("=" * 120)

    # Collect all trading dates
    all_dates = set()
    for fpath in glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")):
        d = os.path.basename(fpath).replace(".parquet", "")
        all_dates.add(d)
    all_dates = sorted(all_dates)
    print(f"Total dates in data: {len(all_dates)}")

    # ── Run each period ────────────────────────────────────────────────
    print("\nRunning TRAIN period (2022-01-03 to 2024-12-31)...")
    train_trades = run_period(all_dates, TRAIN_START, TRAIN_END)
    print(f"  -> {len(train_trades)} trades")

    print("Running TEST period (2025-01-01 to 2026-03-25)...")
    test_trades = run_period(all_dates, TEST_START, TEST_END)
    print(f"  -> {len(test_trades)} trades")

    full_trades = train_trades + test_trades
    print(f"Running FULL period (2022-01-03 to 2026-03-25)... -> {len(full_trades)} trades")

    # ── 1. Overall comparison ──────────────────────────────────────────
    train_m = compute_metrics(train_trades, "TRAIN (2022-2024)", years_duration=3.0)
    test_m = compute_metrics(test_trades, "TEST  (2025-2026)", years_duration=1.23)  # ~15 months
    full_m = compute_metrics(full_trades, "FULL  (2022-2026)", years_duration=4.23)
    print_table([train_m, test_m, full_m], "OVERALL: Train vs Test vs Full")

    # ── 2. Breakdown by CALL vs PUT for each period ────────────────────
    train_call = [t for t in train_trades if t["side"] == "CALL"]
    train_put  = [t for t in train_trades if t["side"] == "PUT"]
    test_call  = [t for t in test_trades if t["side"] == "CALL"]
    test_put   = [t for t in test_trades if t["side"] == "PUT"]

    side_results = [
        compute_metrics(train_call, "TRAIN CALL", 3.0),
        compute_metrics(train_put,  "TRAIN PUT",  3.0),
        compute_metrics(test_call,  "TEST  CALL", 1.23),
        compute_metrics(test_put,   "TEST  PUT",  1.23),
    ]
    print_table(side_results, "CALL vs PUT BREAKDOWN by Period")

    # ── 3. Per-year stats ──────────────────────────────────────────────
    yearly_results = []
    for year in ["2022", "2023", "2024", "2025", "2026"]:
        year_trades = [t for t in full_trades if t["year"] == year]
        dur = 1.0 if year != "2026" else 0.23  # partial year
        m = compute_metrics(year_trades, f"{year} ALL", dur)
        yearly_results.append(m)

    print_table(yearly_results, "PER-YEAR CONSISTENCY CHECK (All Sides Combined)")

    # Per-year by side
    yearly_side = []
    for year in ["2022", "2023", "2024", "2025", "2026"]:
        yc = [t for t in full_trades if t["year"] == year and t["side"] == "CALL"]
        yp = [t for t in full_trades if t["year"] == year and t["side"] == "PUT"]
        dur = 1.0 if year != "2026" else 0.23
        yearly_side.append(compute_metrics(yc, f"{year} CALL", dur))
        yearly_side.append(compute_metrics(yp, f"{year} PUT",  dur))
    print_table(yearly_side, "PER-YEAR by CALL vs PUT")

    # ── 4. Key comparison summary ──────────────────────────────────────
    print("\n" + "=" * 80)
    print("  KEY COMPARISON: Does OOS match IS?")
    print("=" * 80)
    fmt = "{:<25} {:>15} {:>15} {:>15}"
    print(fmt.format("Metric", "TRAIN (IS)", "TEST (OOS)", "Ratio OOS/IS"))
    print("-" * 80)

    comparisons = [
        ("Total P&L",       train_m["total_pnl"],      test_m["total_pnl"]),
        ("Trades",          train_m["trades"],          test_m["trades"]),
        ("P&L / Trade",     train_m["total_pnl"] / max(1, train_m["trades"]),
                            test_m["total_pnl"] / max(1, test_m["trades"])),
        ("Win Rate %",      train_m["win_rate"],        test_m["win_rate"]),
        ("Sharpe",          train_m["sharpe"],          test_m["sharpe"]),
        ("Profit Factor",   train_m["profit_factor"],   test_m["profit_factor"]),
        ("Max Drawdown",    train_m["max_dd"],          test_m["max_dd"]),
        ("CAGR %",          train_m["cagr"],            test_m["cagr"]),
        ("Avg Win",         train_m["avg_win"],         test_m["avg_win"]),
        ("Avg Loss",        train_m["avg_loss"],        test_m["avg_loss"]),
    ]

    for name, is_val, oos_val in comparisons:
        if name == "Max Drawdown":
            # Drawdown is negative; ratio doesn't make sense same way
            ratio_str = f"{oos_val/is_val:.2f}x" if is_val != 0 else "N/A"
        elif is_val != 0:
            ratio_str = f"{oos_val/is_val:.2f}x"
        else:
            ratio_str = "N/A"

        if isinstance(is_val, float):
            if abs(is_val) > 100:
                print(fmt.format(name, f"${is_val:,.0f}", f"${oos_val:,.0f}", ratio_str))
            else:
                print(fmt.format(name, f"{is_val:.2f}", f"{oos_val:.2f}", ratio_str))
        else:
            print(fmt.format(name, f"{is_val}", f"{oos_val}", ratio_str))

    print("=" * 80)

    # ── 5. Monthly P&L for visual inspection ───────────────────────────
    print("\n" + "=" * 80)
    print("  MONTHLY P&L (all sides combined)")
    print("=" * 80)
    monthly = defaultdict(float)
    monthly_count = defaultdict(int)
    for t in full_trades:
        ym = t["date"][:6]  # YYYYMM
        monthly[ym] += t["pnl"]
        monthly_count[ym] += 1

    print(f"{'Month':<10} {'P&L':>10} {'Trades':>8} {'Period':>8}")
    print("-" * 40)
    for ym in sorted(monthly.keys()):
        period = "TRAIN" if ym <= "202412" else "TEST"
        print(f"{ym:<10} {monthly[ym]:>10,.0f} {monthly_count[ym]:>8} {period:>8}")

    # Count profitable months
    train_months = {k: v for k, v in monthly.items() if k <= "202412"}
    test_months  = {k: v for k, v in monthly.items() if k > "202412"}
    train_prof = sum(1 for v in train_months.values() if v > 0)
    test_prof  = sum(1 for v in test_months.values() if v > 0)
    print(f"\nProfitable months: TRAIN {train_prof}/{len(train_months)} ({train_prof/max(1,len(train_months))*100:.0f}%)  |  TEST {test_prof}/{len(test_months)} ({test_prof/max(1,len(test_months))*100:.0f}%)")

    print("\nDone.")


if __name__ == "__main__":
    main()
