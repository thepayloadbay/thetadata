"""Research: Do FOMC announcement days and Triple Witching days hurt performance?

Tests four configs:
  1. Baseline — no calendar skips
  2. Skip FOMC days
  3. Skip Triple Witching days
  4. Skip both FOMC + TW

Also prints individual trade details on FOMC and TW days.
"""
from __future__ import annotations

import math
import os
import glob

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_SHORT_DISTANCE = 3.0
STRIKE_STEP = 5
RISK_AMOUNT = 2500.0
MIN_NET_CREDIT = 0.10
COMMISSION_PER_LEG = 0.50

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

FOMC_DATES = {
    "20220126","20220316","20220504","20220615","20220727","20220921",
    "20221102","20221214","20230201","20230322","20230503","20230614",
    "20230726","20230920","20231101","20231213","20240131","20240320",
    "20240501","20240612","20240731","20240918","20241107","20241218",
    "20250129","20250319","20250507","20250618","20250730","20250917",
    "20251029","20251210","20260128","20260318","20260506","20260617",
    "20260729","20260916","20261028","20261209",
}

TRIPLE_WITCHING_DATES = {
    "20220318","20220617","20220916","20221216","20230317","20230616",
    "20230915","20231215","20240315","20240621","20240920","20241220",
    "20250321","20250620","20250919","20251219","20260320","20260619",
    "20260918","20261218",
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
            "credit": round(credit, 4), "settlement": round(settlement, 4),
            "pnl": round(pnl, 2)}


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
            "credit": round(credit, 4), "settlement": round(settlement, 4),
            "pnl": round(pnl, 2)}


# ── Metrics ─────────────────────────────────────────────────────────────
def compute_metrics(trades: list[dict], label: str, starting_eq: float = 50000.0) -> dict:
    if not trades:
        return {"label": label, "trades": 0, "total_pnl": 0, "max_dd": 0,
                "sharpe": 0, "calmar": 0, "win_rate": 0, "avg_win": 0,
                "avg_loss": 0, "wins": 0, "losses": 0, "profit_factor": 0}

    pnls = np.array([t["pnl"] for t in trades])
    equity = starting_eq + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    max_dd = float(dd.min())
    total = float(pnls.sum())
    wins = int((pnls > 0).sum())
    losses = int((pnls <= 0).sum())
    wr = wins / len(pnls) * 100
    avg_win = float(pnls[pnls > 0].mean()) if wins > 0 else 0
    avg_loss = float(pnls[pnls <= 0].mean()) if losses > 0 else 0
    gross_win = float(pnls[pnls > 0].sum()) if wins > 0 else 0
    gross_loss = abs(float(pnls[pnls <= 0].sum())) if losses > 0 else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    if len(pnls) > 1 and pnls.std() > 0:
        sharpe = (pnls.mean() / pnls.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    calmar = abs(total / max_dd) if max_dd != 0 else 0.0

    return {
        "label": label, "trades": len(pnls), "total_pnl": total,
        "max_dd": max_dd, "sharpe": sharpe, "calmar": calmar,
        "win_rate": wr, "avg_win": avg_win, "avg_loss": avg_loss,
        "wins": wins, "losses": losses, "profit_factor": pf,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("  RESEARCH: FOMC + TRIPLE WITCHING CALENDAR SKIP")
    print("=" * 80)

    # Collect all trading dates with SPX data
    all_dates = set()
    for fpath in glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")):
        d = os.path.basename(fpath).replace(".parquet", "")
        if START_DATE <= d <= END_DATE:
            all_dates.add(d)
    trading_dates = sorted(all_dates)
    print(f"Date candidates: {len(trading_dates)}")

    # ── Single pass: collect all trades with calendar tags ──────────────
    all_trades = []  # list of dicts with date, side, pnl, credit, etc + is_fomc, is_tw

    for date_str in trading_dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue

        spx_df = load_spx(date_str)
        if spx_df is None:
            continue

        # VIX filter at 15:50
        vix_df = load_vix(date_str)
        vix_val = None
        if vix_df is not None:
            vix_val = get_price(vix_df, "15:50:00")
            if vix_val is not None and vix_val > VIX_INTRADAY_MAX:
                continue

        # Afternoon return filter
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
        is_fomc = date_str in FOMC_DATES
        is_tw = date_str in TRIPLE_WITCHING_DATES

        ct = try_call_spread(spot, qdf, spx_exit, etime)
        pt = try_put_spread(spot, qdf, spx_exit, etime)

        for trade in [ct, pt]:
            if trade is not None:
                trade["date"] = date_str
                trade["spot"] = round(spot, 2)
                trade["exit_price"] = round(spx_exit, 2)
                trade["is_fomc"] = is_fomc
                trade["is_tw"] = is_tw
                trade["win"] = trade["pnl"] > 0
                trade["vix"] = round(vix_val, 1) if vix_val else None
                all_trades.append(trade)

    print(f"Total trades collected: {len(all_trades)}")

    # ── Build four filtered trade lists ─────────────────────────────────
    baseline   = all_trades  # no skips
    skip_fomc  = [t for t in all_trades if not t["is_fomc"]]
    skip_tw    = [t for t in all_trades if not t["is_tw"]]
    skip_both  = [t for t in all_trades if not t["is_fomc"] and not t["is_tw"]]

    configs = [
        (baseline,  "1. Baseline (no skips)"),
        (skip_fomc, "2. Skip FOMC days"),
        (skip_tw,   "3. Skip Triple Witching"),
        (skip_both, "4. Skip FOMC + TW"),
    ]

    results = []
    for trades, label in configs:
        results.append(compute_metrics(trades, label))

    # ── Comparison Table ────────────────────────────────────────────────
    print("\n" + "=" * 130)
    print(f"{'Config':<30} {'Trades':>7} {'Total P&L':>12} {'Max DD':>10} {'Sharpe':>8} "
          f"{'Calmar':>8} {'WR%':>7} {'PF':>6} {'Avg Win':>9} {'Avg Loss':>10}")
    print("-" * 130)
    for r in results:
        print(f"{r['label']:<30} {r['trades']:>7,} {r['total_pnl']:>12,.0f} "
              f"{r['max_dd']:>10,.0f} {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
              f"{r['win_rate']:>6.1f}% {r['profit_factor']:>6.2f} "
              f"{r['avg_win']:>9,.0f} {r['avg_loss']:>10,.0f}")
    print("=" * 130)

    # ── Delta from baseline ─────────────────────────────────────────────
    b = results[0]
    print("\n  DELTA FROM BASELINE:")
    print(f"  {'Config':<30} {'dTrades':>8} {'dP&L':>10} {'dMaxDD':>10} {'dSharpe':>8}")
    print(f"  {'-'*70}")
    for r in results[1:]:
        dt = r["trades"] - b["trades"]
        dp = r["total_pnl"] - b["total_pnl"]
        dd = r["max_dd"] - b["max_dd"]  # less negative = better
        ds = r["sharpe"] - b["sharpe"]
        print(f"  {r['label']:<30} {dt:>+8} {dp:>+10,.0f} {dd:>+10,.0f} {ds:>+8.2f}")

    # ── FOMC day deep dive ──────────────────────────────────────────────
    fomc_trades = [t for t in all_trades if t["is_fomc"]]
    tw_trades = [t for t in all_trades if t["is_tw"]]

    print("\n\n" + "=" * 110)
    print("  FOMC DAY TRADE DETAILS")
    print("=" * 110)
    if fomc_trades:
        fomc_pnls = [t["pnl"] for t in fomc_trades]
        fomc_wins = sum(1 for p in fomc_pnls if p > 0)
        fomc_losses = sum(1 for p in fomc_pnls if p <= 0)
        print(f"  Total trades: {len(fomc_trades)} | Wins: {fomc_wins} | Losses: {fomc_losses} | "
              f"WR: {fomc_wins/len(fomc_trades)*100:.0f}% | "
              f"Total P&L: ${sum(fomc_pnls):+,.0f} | Avg P&L: ${np.mean(fomc_pnls):+,.0f}")
        print()
        print(f"  {'Date':<12} {'Side':<6} {'SPX Entry':>10} {'SPX Exit':>10} {'Move':>8} "
              f"{'Short':>7} {'Long':>7} {'Qty':>5} {'Credit':>8} {'Settle':>8} {'P&L':>10} {'W/L':>5}")
        print(f"  {'-'*108}")
        for t in sorted(fomc_trades, key=lambda x: x["date"]):
            move = t["exit_price"] - t["spot"]
            wl = "WIN" if t["win"] else "LOSS"
            print(f"  {t['date']:<12} {t['side']:<6} {t['spot']:>10.2f} {t['exit_price']:>10.2f} "
                  f"{move:>+8.2f} {t['short']:>7} {t['long']:>7} {t['qty']:>5} "
                  f"${t['credit']:>7.2f} ${t['settlement']:>7.2f} ${t['pnl']:>+9,.0f} {wl:>5}")
    else:
        print("  No FOMC trades found (all filtered by VIX/afternoon)")

    # ── Triple Witching deep dive ───────────────────────────────────────
    print("\n\n" + "=" * 110)
    print("  TRIPLE WITCHING DAY TRADE DETAILS")
    print("=" * 110)
    if tw_trades:
        tw_pnls = [t["pnl"] for t in tw_trades]
        tw_wins = sum(1 for p in tw_pnls if p > 0)
        tw_losses = sum(1 for p in tw_pnls if p <= 0)
        print(f"  Total trades: {len(tw_trades)} | Wins: {tw_wins} | Losses: {tw_losses} | "
              f"WR: {tw_wins/len(tw_trades)*100:.0f}% | "
              f"Total P&L: ${sum(tw_pnls):+,.0f} | Avg P&L: ${np.mean(tw_pnls):+,.0f}")
        print()
        print(f"  {'Date':<12} {'Side':<6} {'SPX Entry':>10} {'SPX Exit':>10} {'Move':>8} "
              f"{'Short':>7} {'Long':>7} {'Qty':>5} {'Credit':>8} {'Settle':>8} {'P&L':>10} {'W/L':>5}")
        print(f"  {'-'*108}")
        for t in sorted(tw_trades, key=lambda x: x["date"]):
            move = t["exit_price"] - t["spot"]
            wl = "WIN" if t["win"] else "LOSS"
            print(f"  {t['date']:<12} {t['side']:<6} {t['spot']:>10.2f} {t['exit_price']:>10.2f} "
                  f"{move:>+8.2f} {t['short']:>7} {t['long']:>7} {t['qty']:>5} "
                  f"${t['credit']:>7.2f} ${t['settlement']:>7.2f} ${t['pnl']:>+9,.0f} {wl:>5}")
    else:
        print("  No Triple Witching trades found (all filtered by VIX/afternoon)")

    # ── Summary of skipped FOMC/TW dates ────────────────────────────────
    fomc_traded_dates = set(t["date"] for t in fomc_trades)
    fomc_in_range = sorted(d for d in FOMC_DATES if START_DATE <= d <= END_DATE)
    fomc_skipped_by_filter = [d for d in fomc_in_range if d not in fomc_traded_dates
                               and d not in MARKET_HOLIDAYS and d not in EARLY_CLOSE_DAYS]

    tw_traded_dates = set(t["date"] for t in tw_trades)
    tw_in_range = sorted(d for d in TRIPLE_WITCHING_DATES if START_DATE <= d <= END_DATE)
    tw_skipped_by_filter = [d for d in tw_in_range if d not in tw_traded_dates
                             and d not in MARKET_HOLIDAYS and d not in EARLY_CLOSE_DAYS]

    print(f"\n\n  FOMC dates in range: {len(fomc_in_range)} | Traded: {len(fomc_traded_dates)} | "
          f"Already filtered by VIX/afternoon: {len(fomc_skipped_by_filter)}")
    if fomc_skipped_by_filter:
        print(f"    Already-skipped dates: {', '.join(fomc_skipped_by_filter)}")

    print(f"  TW dates in range: {len(tw_in_range)} | Traded: {len(tw_traded_dates)} | "
          f"Already filtered by VIX/afternoon: {len(tw_skipped_by_filter)}")
    if tw_skipped_by_filter:
        print(f"    Already-skipped dates: {', '.join(tw_skipped_by_filter)}")


if __name__ == "__main__":
    main()
