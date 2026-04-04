"""Research: One-side-only entry when 30-min momentum is strongly directional.

Hypothesis: When SPX is rising strongly into the close (mom30 > threshold),
the CALL side is threatened (SPX may blow past call short strike) but the PUT
side benefits (SPX staying up means puts expire worthless). Vice versa for
drops. Skip the threatened side and only enter the safe side.

Tests thresholds: 0 (baseline, both sides always), 0.03%, 0.05%, 0.07%, 0.10%, 0.15%.

Base config: Rule C3 VIX-adaptive strikes, bidirectional, entry 15:55, exit 16:00,
width $5, min credit $0.10, commission $0.50/leg.
Tiered sizing: credit<0.20->$1500, 0.20-0.50->$2500, >0.50->$3500.
Call filter: afternoon >= 1.0%. Put filter: afternoon < -0.75% AND mom30 < -0.10%.
Starting equity $50,000.
"""
from __future__ import annotations

import glob
import math
import os
import time as _time

import numpy as np
import pandas as pd

# ── paths ──────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

# ── calendar ───────────────────────────────────────────────────────────
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

# ── strategy constants ─────────────────────────────────────────────────
SPREAD_WIDTH = 5.0
STRIKE_STEP = 5
MIN_NET_CREDIT = 0.10
COMMISSION = 0.50
VIX_INTRADAY_MAX = 26.0
AFTERNOON_RETURN_MAX_CALL = 1.0   # call-side: skip if afternoon >= 1.0%
PUT_AFTERNOON_THRESH = -0.75       # put-side: skip if afternoon < -0.75%
PUT_MOM30_THRESH = -0.10           # put-side: AND mom30 < -0.10%
START_DATE = "20220103"
END_DATE = "20260325"
STARTING_EQUITY = 50000.0


# ── Rule C3 adaptive strike distance ──────────────────────────────────
def _adaptive_distance(vix: float | None) -> float:
    if vix is None:
        return 3.0
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


# ── tiered sizing ─────────────────────────────────────────────────────
def _tier_risk(credit: float) -> float:
    if credit < 0.20:
        return 1500.0
    elif credit <= 0.50:
        return 2500.0
    else:
        return 3500.0


# ── data helpers ───────────────────────────────────────────────────────
def _load_spx(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def _load_vix(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "vix_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    df["hhmm"] = df["timestamp"].str[11:16]
    return df


def _load_quotes(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(DATA_DIR, date_str[:4], "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["time_str"] = df["timestamp"].str[11:19]
    return df


def _spx_at(spx: pd.DataFrame, ts: str) -> float | None:
    m = spx[spx["time_str"] == ts]
    if m.empty:
        m = spx[spx["hhmm"] == ts[:5]]
    if m.empty:
        return None
    return float(m["close"].iloc[0])


def _vix_at(vix: pd.DataFrame, hhmm: str) -> float | None:
    m = vix[vix["hhmm"] == hhmm]
    if m.empty:
        return None
    v = float(m["close"].iloc[0])
    return v if v > 0 else None


def _afternoon_return(spx: pd.DataFrame) -> float | None:
    af = spx[(spx["hhmm"] >= "13:00") & (spx["time_str"] < "15:55:00")]
    if len(af) < 2:
        return None
    s, e = float(af["close"].iloc[0]), float(af["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


def _mom_30(spx: pd.DataFrame) -> float | None:
    """SPX return from 15:25 to 15:54."""
    bars = spx[(spx["hhmm"] >= "15:25") & (spx["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    s, e = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
    return (e - s) / s * 100 if s > 0 else None


def _get_quote(qdf: pd.DataFrame, right: str, strike: int, ts: str) -> dict | None:
    m = qdf[(qdf["right"] == right) & (qdf["strike"] == strike) & (qdf["time_str"] == ts)]
    if m.empty:
        return None
    r = m.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"])}


def _all_dates() -> list[str]:
    dates = set()
    for fpath in glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")):
        d = os.path.basename(fpath).replace(".parquet", "")
        if START_DATE <= d <= END_DATE:
            dates.add(d)
    return sorted(dates)


# ── build one side's trade ─────────────────────────────────────────────
def _build_call_trade(date_str, spot, spx_exit, qdf, available, dist):
    etime = "15:55:00"
    if dist == 0:
        call_short = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
        call_short = int(math.ceil((spot + dist) / STRIKE_STEP) * STRIKE_STEP)
    call_long = call_short + int(SPREAD_WIDTH)

    if call_short not in available or call_long not in available:
        cands = sorted(s for s in available if s >= spot + dist)
        if cands:
            call_short = int(cands[0])
            call_long = call_short + int(SPREAD_WIDTH)
    if call_short not in available or call_long not in available:
        return None

    sq = _get_quote(qdf, "C", call_short, etime)
    lq = _get_quote(qdf, "C", call_long, etime)
    if not sq or not lq:
        return None
    credit = sq["bid"] - lq["ask"]
    if credit < MIN_NET_CREDIT:
        return None

    risk_amt = _tier_risk(credit)
    mlp = (SPREAD_WIDTH - credit) * 100
    qty = max(1, int(risk_amt / mlp)) if mlp > 0 else 1

    if spx_exit <= call_short:
        settle = 0.0
    elif spx_exit >= call_long:
        settle = SPREAD_WIDTH
    else:
        settle = spx_exit - call_short

    pnl = (credit - settle) * qty * 100 - 4 * qty * COMMISSION
    return {"date": date_str, "side": "CALL", "pnl": pnl, "qty": qty,
            "credit": credit, "settle": settle}


def _build_put_trade(date_str, spot, spx_exit, qdf, available, dist):
    etime = "15:55:00"
    if dist == 0:
        put_short = int(math.floor(spot / STRIKE_STEP) * STRIKE_STEP)
    else:
        put_short = int(math.floor((spot - dist) / STRIKE_STEP) * STRIKE_STEP)
    put_long = put_short - int(SPREAD_WIDTH)

    if put_short not in available or put_long not in available:
        cands = sorted((s for s in available if s <= spot - dist), reverse=True)
        if cands:
            put_short = int(cands[0])
            put_long = put_short - int(SPREAD_WIDTH)
    if put_short not in available or put_long not in available:
        return None

    sq = _get_quote(qdf, "P", put_short, etime)
    lq = _get_quote(qdf, "P", put_long, etime)
    if not sq or not lq:
        return None
    credit = sq["bid"] - lq["ask"]
    if credit < MIN_NET_CREDIT:
        return None

    risk_amt = _tier_risk(credit)
    mlp = (SPREAD_WIDTH - credit) * 100
    qty = max(1, int(risk_amt / mlp)) if mlp > 0 else 1

    if spx_exit >= put_short:
        settle = 0.0
    elif spx_exit <= put_long:
        settle = SPREAD_WIDTH
    else:
        settle = put_short - spx_exit

    pnl = (credit - settle) * qty * 100 - 4 * qty * COMMISSION
    return {"date": date_str, "side": "PUT", "pnl": pnl, "qty": qty,
            "credit": credit, "settle": settle}


# ── preload all day data once ──────────────────────────────────────────
def _preload_days():
    """Returns list of day dicts with all needed data preloaded."""
    dates = _all_dates()
    days = []
    for date_str in dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        spx = _load_spx(date_str)
        if spx is None:
            continue
        vix_df = _load_vix(date_str)
        vix_1550 = _vix_at(vix_df, "15:50") if vix_df is not None else None

        # VIX > 26 skip
        if vix_1550 is not None and vix_1550 > VIX_INTRADAY_MAX:
            continue

        afternoon_ret = _afternoon_return(spx)
        mom30 = _mom_30(spx)

        # Get spot and exit
        spot = _spx_at(spx, "15:55:00")
        if spot is None:
            continue
        spx_exit = _spx_at(spx, "16:00:00")
        if spx_exit is None:
            spx_exit = _spx_at(spx, "15:59:00")
        if spx_exit is None:
            spx_exit = float(spx["close"].iloc[-1])

        qdf = _load_quotes(date_str)
        if qdf is None:
            continue

        available = set(qdf["strike"].unique())
        dist = _adaptive_distance(vix_1550)

        # Pre-build both side trades (before any filtering)
        call_trade = _build_call_trade(date_str, spot, spx_exit, qdf, available, dist)
        put_trade = _build_put_trade(date_str, spot, spx_exit, qdf, available, dist)

        days.append({
            "date": date_str,
            "afternoon_ret": afternoon_ret,
            "mom30": mom30,
            "call_trade": call_trade,
            "put_trade": put_trade,
        })

    return days


# ── run one config ─────────────────────────────────────────────────────
def _run_config(days, mom_threshold, label):
    """
    mom_threshold=None means baseline (both sides always, with standard filters).
    Otherwise: skip threatened side when |mom30| > threshold.
    """
    trades = []
    calls_skipped = 0
    puts_skipped = 0
    skipped_call_pnl = 0.0
    skipped_put_pnl = 0.0

    for day in days:
        afternoon_ret = day["afternoon_ret"]
        mom30 = day["mom30"]
        ct = day["call_trade"]
        pt = day["put_trade"]

        # ── Standard filters (always applied) ──
        # Call filter: skip call side if afternoon >= 1.0%
        skip_call_filter = (afternoon_ret is not None and afternoon_ret >= AFTERNOON_RETURN_MAX_CALL)
        # Put filter: skip put side if afternoon < -0.75% AND mom30 < -0.10%
        skip_put_filter = (afternoon_ret is not None and mom30 is not None
                           and afternoon_ret < PUT_AFTERNOON_THRESH
                           and mom30 < PUT_MOM30_THRESH)

        # ── Directional one-side logic ──
        skip_call_directional = False
        skip_put_directional = False
        if mom_threshold is not None and mom30 is not None:
            if mom30 > mom_threshold:
                # SPX rising -> call side threatened, skip it
                skip_call_directional = True
            elif mom30 < -mom_threshold:
                # SPX falling -> put side threatened, skip it
                skip_put_directional = True

        # Decide call
        enter_call = (ct is not None) and not skip_call_filter
        if enter_call and skip_call_directional:
            calls_skipped += 1
            skipped_call_pnl += ct["pnl"]
            enter_call = False

        # Decide put
        enter_put = (pt is not None) and not skip_put_filter
        if enter_put and skip_put_directional:
            puts_skipped += 1
            skipped_put_pnl += pt["pnl"]
            enter_put = False

        if enter_call:
            trades.append(ct)
        if enter_put:
            trades.append(pt)

    stats = _compute_stats(trades, label)
    stats["calls_skipped"] = calls_skipped
    stats["puts_skipped"] = puts_skipped
    stats["skipped_call_pnl"] = round(skipped_call_pnl, 0)
    stats["skipped_put_pnl"] = round(skipped_put_pnl, 0)
    return stats


def _compute_stats(trades: list, label: str) -> dict:
    if not trades:
        return {"label": label, "total_pnl": 0, "max_dd": 0, "wr": 0,
                "trades": 0, "call_trades": 0, "put_trades": 0, "sharpe": 0,
                "calmar": 0, "worst_day": 0}

    df = pd.DataFrame(trades)
    total_pnl = df["pnl"].sum()
    wins = (df["pnl"] > 0).sum()
    wr = wins / len(df) * 100

    calls = df[df["side"] == "CALL"]
    puts = df[df["side"] == "PUT"]

    # Daily P&L for Sharpe and DD
    daily = df.groupby("date")["pnl"].sum().sort_index()
    equity = STARTING_EQUITY + daily.cumsum()
    running_max = equity.cummax()
    dd = (equity - running_max).min()
    worst_day = daily.min()

    # Sharpe (annualized)
    sharpe = 0.0
    if daily.std() > 0:
        sharpe = (daily.mean() / daily.std()) * np.sqrt(252)

    # Calmar
    years = len(daily) / 252
    cagr_pct = ((STARTING_EQUITY + total_pnl) / STARTING_EQUITY) ** (1 / years) - 1 if years > 0 else 0
    calmar = abs(cagr_pct * STARTING_EQUITY / dd) if dd < 0 else 0

    return {
        "label": label,
        "total_pnl": round(total_pnl, 0),
        "max_dd": round(dd, 0),
        "wr": round(wr, 1),
        "trades": len(df),
        "call_trades": len(calls),
        "put_trades": len(puts),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "worst_day": round(worst_day, 0),
    }


# ── main ───────────────────────────────────────────────────────────────
def main():
    t0 = _time.time()
    print("Loading and pre-building trades for all days...")
    days = _preload_days()
    print(f"  {len(days)} trading days loaded in {_time.time()-t0:.1f}s\n")

    configs = [
        (None,   "A: Baseline (both sides)"),
        (0.03,   "B: thresh 0.03%"),
        (0.05,   "C: thresh 0.05%"),
        (0.07,   "D: thresh 0.07%"),
        (0.10,   "E: thresh 0.10%"),
        (0.15,   "F: thresh 0.15%"),
    ]

    results = []
    for thresh, label in configs:
        t1 = _time.time()
        stats = _run_config(days, thresh, label)
        elapsed = _time.time() - t1
        results.append(stats)
        print(f"  {label} done ({elapsed:.1f}s)")

    # ── Print comparison table ──
    print("\n" + "=" * 130)
    print("ONE-SIDE DIRECTIONAL MOMENTUM FILTER — COMPARISON")
    print("Skip the threatened side when |mom30| > threshold; enter only the safe side.")
    print("Base: Rule C3 VIX-adaptive, tiered sizing, call filter afternoon>=1.0%, put filter afternoon<-0.75% & mom30<-0.10%")
    print("=" * 130)

    hdr = f"{'Config':<28} {'P&L':>9} {'MaxDD':>9} {'WR%':>6} {'Trades':>7} {'C/P':>9} {'Sharpe':>7} {'Calmar':>7} {'Worst':>8}"
    print(hdr)
    print("-" * 130)
    for r in results:
        cp = f"{r['call_trades']}/{r['put_trades']}"
        line = (f"{r['label']:<28} "
                f"${r['total_pnl']:>8,.0f} "
                f"${r['max_dd']:>8,.0f} "
                f"{r['wr']:>5.1f}% "
                f"{r['trades']:>7d} "
                f"{cp:>9} "
                f"{r['sharpe']:>7.2f} "
                f"{r['calmar']:>7.2f} "
                f"${r['worst_day']:>7,.0f}")
        print(line)

    # ── Skip analysis table ──
    print("\n" + "=" * 110)
    print("SKIP ANALYSIS — How many trades skipped and what was their P&L?")
    print("=" * 110)
    hdr2 = f"{'Config':<28} {'Calls Skip':>11} {'Skip C P&L':>11} {'Puts Skip':>11} {'Skip P P&L':>11} {'Total Skip':>11} {'Total Skip PnL':>15}"
    print(hdr2)
    print("-" * 110)
    for r in results:
        cs = r.get("calls_skipped", 0)
        ps = r.get("puts_skipped", 0)
        cspnl = r.get("skipped_call_pnl", 0)
        pspnl = r.get("skipped_put_pnl", 0)
        line = (f"{r['label']:<28} "
                f"{cs:>11d} "
                f"${cspnl:>10,.0f} "
                f"{ps:>11d} "
                f"${pspnl:>10,.0f} "
                f"{cs+ps:>11d} "
                f"${cspnl+pspnl:>14,.0f}")
        print(line)

    # ── Delta from baseline ──
    base = results[0]
    print("\n" + "=" * 100)
    print("DELTA FROM BASELINE")
    print("=" * 100)
    hdr3 = f"{'Config':<28} {'dP&L':>10} {'dMaxDD':>10} {'dWR':>7} {'dTrades':>8} {'dSharpe':>8} {'dCalmar':>8}"
    print(hdr3)
    print("-" * 100)
    for r in results[1:]:
        dpnl = r["total_pnl"] - base["total_pnl"]
        ddd = r["max_dd"] - base["max_dd"]
        dwr = r["wr"] - base["wr"]
        dtr = r["trades"] - base["trades"]
        dsh = r["sharpe"] - base["sharpe"]
        dcal = r["calmar"] - base["calmar"]
        line = (f"{r['label']:<28} "
                f"${dpnl:>+9,.0f} "
                f"${ddd:>+9,.0f} "
                f"{dwr:>+6.1f}% "
                f"{dtr:>+8d} "
                f"{dsh:>+8.2f} "
                f"{dcal:>+8.2f}")
        print(line)

    print(f"\nTotal elapsed: {_time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
