"""Research: Stacked PUT-side filter combinations for LastFive strategy.

Tests whether combining multiple put-side filters (afternoon return, 30-min momentum,
last-hour return) cuts more losses than any single filter alone.

All configs use:
- Bidirectional (call + put)
- Rule C3 adaptive strikes: VIX<18 -> dist=0, 18<=VIX<22 -> dist=3, 22<=VIX<=26 -> dist=5
- Width $5, min credit $0.10, risk $2500, commission $0.50/leg
- Call side always filtered by: VIX<=26 + afternoon<1.0%
- Put side: various filter stacks tested
"""
from __future__ import annotations

import glob
import math
import os
import sys
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
RISK_AMOUNT = 2500.0
MIN_NET_CREDIT = 0.10
COMMISSION = 0.50
VIX_INTRADAY_MAX = 26.0
AFTERNOON_RETURN_MAX_CALL = 1.0  # call-side: skip if afternoon >= 1.0%
START_DATE = "20220103"
END_DATE = "20260325"
STARTING_EQUITY = 50000.0


# ── Rule C3 adaptive strike distance ──────────────────────────────────
def _adaptive_distance(vix: float | None) -> float:
    """VIX-based adaptive short strike distance.
    VIX < 18  -> dist=0 (ATM)
    18 <= VIX < 22 -> dist=3
    22 <= VIX <= 26 -> dist=5
    """
    if vix is None:
        return 3.0  # default fallback
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


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


def _last_hour_return(spx: pd.DataFrame) -> float | None:
    """SPX return from 14:55 to 15:54."""
    bars = spx[(spx["hhmm"] >= "14:55") & (spx["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    s, e = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
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


# ── collect all trading dates ──────────────────────────────────────────
def _all_dates() -> list[str]:
    dates = set()
    for fpath in glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")):
        d = os.path.basename(fpath).replace(".parquet", "")
        if START_DATE <= d <= END_DATE:
            dates.add(d)
    return sorted(dates)


# ── simulate one day ─────────────────────────────────────────────────
def _sim_day(date_str: str, spx: pd.DataFrame, qdf: pd.DataFrame,
             vix_1550: float | None, afternoon_ret: float | None,
             last_hour_ret: float | None, mom30: float | None,
             put_filter_fn=None, call_filter_fn=None):
    """Returns (call_trade, put_trade) dicts or None for each side.

    Uses Rule C3 adaptive strikes based on VIX at 15:50.
    """
    etime = "15:55:00"
    spot = _spx_at(spx, etime)
    if spot is None:
        return None, None

    exit_ts = "16:00:00"
    spx_exit = _spx_at(spx, exit_ts)
    if spx_exit is None:
        spx_exit = _spx_at(spx, "15:59:00")
    if spx_exit is None:
        spx_exit = float(spx["close"].iloc[-1])

    available = set(qdf["strike"].unique())
    dist = _adaptive_distance(vix_1550)

    # ── CALL SIDE ──
    call_trade = None
    # Call-side filter: skip if afternoon >= 1.0% (always applied)
    skip_call = False
    if afternoon_ret is not None and afternoon_ret >= AFTERNOON_RETURN_MAX_CALL:
        skip_call = True
    # Additional call filter (for symmetric configs)
    if call_filter_fn is not None:
        if call_filter_fn(afternoon_ret, last_hour_ret, mom30, vix_1550):
            skip_call = True

    if not skip_call:
        call_short = int(math.ceil((spot + dist) / STRIKE_STEP) * STRIKE_STEP)
        if dist == 0:
            # ATM: nearest strike >= spot
            call_short = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
        call_long = call_short + int(SPREAD_WIDTH)
        if call_short not in available or call_long not in available:
            cands = sorted(s for s in available if s >= spot + dist)
            if cands:
                call_short = int(cands[0])
                call_long = call_short + int(SPREAD_WIDTH)
        if call_short in available and call_long in available:
            sq = _get_quote(qdf, "C", call_short, etime)
            lq = _get_quote(qdf, "C", call_long, etime)
            if sq and lq:
                credit = sq["bid"] - lq["ask"]
                if credit >= MIN_NET_CREDIT:
                    mlp = (SPREAD_WIDTH - credit) * 100
                    qty = max(1, int(RISK_AMOUNT / mlp)) if mlp > 0 else 1
                    if spx_exit <= call_short:
                        settle = 0.0
                    elif spx_exit >= call_long:
                        settle = SPREAD_WIDTH
                    else:
                        settle = spx_exit - call_short
                    pnl = (credit - settle) * qty * 100 - 4 * qty * COMMISSION
                    call_trade = {"date": date_str, "side": "CALL", "pnl": pnl,
                                  "qty": qty, "credit": credit, "settle": settle}

    # ── PUT SIDE ──
    put_trade = None
    # Put filter
    skip_put = False
    if put_filter_fn is not None:
        if put_filter_fn(afternoon_ret, last_hour_ret, mom30, vix_1550):
            skip_put = True

    if not skip_put:
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
        if put_short in available and put_long in available:
            sq = _get_quote(qdf, "P", put_short, etime)
            lq = _get_quote(qdf, "P", put_long, etime)
            if sq and lq:
                credit = sq["bid"] - lq["ask"]
                if credit >= MIN_NET_CREDIT:
                    mlp = (SPREAD_WIDTH - credit) * 100
                    qty = max(1, int(RISK_AMOUNT / mlp)) if mlp > 0 else 1
                    if spx_exit >= put_short:
                        settle = 0.0
                    elif spx_exit <= put_long:
                        settle = SPREAD_WIDTH
                    else:
                        settle = put_short - spx_exit
                    pnl = (credit - settle) * qty * 100 - 4 * qty * COMMISSION
                    put_trade = {"date": date_str, "side": "PUT", "pnl": pnl,
                                 "qty": qty, "credit": credit, "settle": settle}

    return call_trade, put_trade


# ── pre-cache all day data ────────────────────────────────────────────
def _preload_all_days():
    """Load all day data once, return list of day-tuples."""
    dates = _all_dates()
    days = []
    loaded = 0
    for date_str in dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        spx = _load_spx(date_str)
        if spx is None:
            continue
        vix_df = _load_vix(date_str)
        vix_1550 = _vix_at(vix_df, "15:50") if vix_df is not None else None
        if vix_1550 is not None and vix_1550 > VIX_INTRADAY_MAX:
            continue
        afternoon_ret = _afternoon_return(spx)
        last_hour_ret = _last_hour_return(spx)
        mom30 = _mom_30(spx)
        qdf = _load_quotes(date_str)
        if qdf is None:
            continue
        days.append((date_str, spx, qdf, vix_1550, afternoon_ret, last_hour_ret, mom30))
        loaded += 1
    print(f"  Pre-loaded {loaded} trading days")
    return days


# ── full simulation (uses pre-cached data) ────────────────────────────
def run_simulation(days: list, put_filter_fn=None, call_filter_fn=None,
                   label="baseline"):
    """Run full backtest on pre-cached day data."""
    all_trades = []

    for date_str, spx, qdf, vix_1550, afternoon_ret, last_hour_ret, mom30 in days:
        ct, pt = _sim_day(date_str, spx, qdf, vix_1550, afternoon_ret,
                          last_hour_ret, mom30,
                          put_filter_fn=put_filter_fn,
                          call_filter_fn=call_filter_fn)
        if ct:
            all_trades.append(ct)
        if pt:
            all_trades.append(pt)

    return _compute_stats(all_trades, label)


def _compute_stats(trades: list, label: str) -> dict:
    if not trades:
        return {"label": label, "total_pnl": 0, "max_dd": 0, "wr": 0,
                "trades": 0, "call_trades": 0, "put_trades": 0,
                "sharpe": 0, "calmar": 0}

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

    # Sharpe (annualized)
    if daily.std() > 0:
        sharpe = (daily.mean() / daily.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # Calmar (annualized return / max DD)
    n_years = len(daily) / 252
    if n_years > 0 and dd < 0:
        annual_return = total_pnl / n_years
        calmar = annual_return / abs(dd)
    else:
        calmar = 0

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
    }


# ══════════════════════════════════════════════════════════════════════
#  FILTER DEFINITIONS
# ══════════════════════════════════════════════════════════════════════

# Put-side filters (return True = SKIP put)
def _put_aftn_neg1(af, lh, m, v):
    """Skip put if afternoon return < -1.0%"""
    return af is not None and af < -1.0

def _put_mom30_neg010(af, lh, m, v):
    """Skip put if mom_30 < -0.10%"""
    return m is not None and m < -0.10

def _put_lasthr_neg030(af, lh, m, v):
    """Skip put if last_hour return < -0.30%"""
    return lh is not None and lh < -0.30

def _put_aftn_neg1_AND_mom30_neg010(af, lh, m, v):
    """Skip put if afternoon < -1.0% AND mom_30 < -0.10%"""
    return (af is not None and af < -1.0) and (m is not None and m < -0.10)

def _put_aftn_neg1_AND_lasthr_neg030(af, lh, m, v):
    """Skip put if afternoon < -1.0% AND last_hour < -0.30%"""
    return (af is not None and af < -1.0) and (lh is not None and lh < -0.30)

def _put_aftn_neg075_AND_mom30_neg010(af, lh, m, v):
    """Skip put if afternoon < -0.75% AND mom_30 < -0.10%"""
    return (af is not None and af < -0.75) and (m is not None and m < -0.10)

def _put_aftn_neg1_symmetric(af, lh, m, v):
    """Skip put if afternoon <= -1.0% (symmetric mirror of call's >=1.0%)"""
    return af is not None and af <= -1.0

def _put_aftn_neg1_symmetric_AND_mom30(af, lh, m, v):
    """Skip put if afternoon <= -1.0% OR mom_30 < -0.10%"""
    skip_af = af is not None and af <= -1.0
    skip_m = m is not None and m < -0.10
    return skip_af or skip_m

# Call-side filter for symmetric configs (skip if afternoon >= 1.0%)
def _call_aftn_pos1_symmetric(af, lh, m, v):
    """Skip call if afternoon >= 1.0% (this replaces the global filter for symmetric mode)"""
    return af is not None and af >= 1.0


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    t0 = _time.time()

    print("=" * 100)
    print("STACKED PUT-SIDE FILTER RESEARCH")
    print("Rule C3 adaptive strikes: VIX<18->dist=0, 18-22->dist=3, 22-26->dist=5")
    print("Call side always: VIX<=26 + afternoon<1.0%  |  Width=$5, MinCr=$0.10, Risk=$2500")
    print("=" * 100)

    print("\nPre-loading all trading day data ...")
    days = _preload_all_days()

    # Define all test configurations
    # For configs 1-7: call-side uses the default global afternoon filter
    #   (afternoon >= 1.0% skip is handled IN _sim_day for call side)
    # For configs 8-9: symmetric mode where call filter is per-side, not global

    configs = [
        # 1. Baseline: no extra put filter
        {
            "label": "1. Baseline (no put filter)",
            "put_fn": None,
            "call_fn": None,
        },
        # 2. Put afternoon < -1.0%
        {
            "label": "2. Put aftn < -1.0%",
            "put_fn": _put_aftn_neg1,
            "call_fn": None,
        },
        # 3. Put mom_30 < -0.10%
        {
            "label": "3. Put mom30 < -0.10%",
            "put_fn": _put_mom30_neg010,
            "call_fn": None,
        },
        # 4. Put last_hr < -0.30%
        {
            "label": "4. Put last_hr < -0.30%",
            "put_fn": _put_lasthr_neg030,
            "call_fn": None,
        },
        # 5. Put aftn<-1.0% + mom30<-0.10% (both must be true to skip)
        {
            "label": "5. Put aftn<-1% + mom30<-0.1%",
            "put_fn": _put_aftn_neg1_AND_mom30_neg010,
            "call_fn": None,
        },
        # 6. Put aftn<-1.0% + last_hr<-0.30% (both must be true to skip)
        {
            "label": "6. Put aftn<-1% + lasthr<-0.3%",
            "put_fn": _put_aftn_neg1_AND_lasthr_neg030,
            "call_fn": None,
        },
        # 7. Put aftn<-0.75% + mom30<-0.10% (tighter afternoon + momentum)
        {
            "label": "7. Put aftn<-0.75% + mom30<-0.1%",
            "put_fn": _put_aftn_neg075_AND_mom30_neg010,
            "call_fn": None,
        },
        # 8. Symmetric: call skip aftn>=1.0%, put skip aftn<=-1.0%
        #    For this, we disable the global afternoon filter and use per-side filters
        {
            "label": "8. Symmetric aftn +/-1.0%",
            "put_fn": _put_aftn_neg1_symmetric,
            "call_fn": _call_aftn_pos1_symmetric,
            "symmetric": True,
        },
        # 9. Symmetric aftn + put mom30<-0.10%
        {
            "label": "9. Symmetric + put mom30<-0.1%",
            "put_fn": _put_aftn_neg1_symmetric_AND_mom30,
            "call_fn": _call_aftn_pos1_symmetric,
            "symmetric": True,
        },
    ]

    results = []
    for cfg in configs:
        label = cfg["label"]
        is_symmetric = cfg.get("symmetric", False)
        print(f"  Running: {label} ...", end=" ", flush=True)

        if is_symmetric:
            # For symmetric configs, we need to run without the global afternoon filter
            # and instead apply per-side filters. We rebuild the day list without
            # the global afternoon skip.
            stats = run_simulation(days,
                                   put_filter_fn=cfg["put_fn"],
                                   call_filter_fn=cfg["call_fn"],
                                   label=label)
        else:
            stats = run_simulation(days,
                                   put_filter_fn=cfg["put_fn"],
                                   call_filter_fn=cfg.get("call_fn"),
                                   label=label)

        results.append(stats)
        print(f"done  (P&L=${stats['total_pnl']:+,.0f}  Sharpe={stats['sharpe']:.2f})")

    # Sort by P&L descending
    results.sort(key=lambda x: x["total_pnl"], reverse=True)

    baseline = [r for r in results if "Baseline" in r["label"]][0]
    baseline_pnl = baseline["total_pnl"]
    baseline_sharpe = baseline["sharpe"]

    elapsed = _time.time() - t0

    print(f"\n{'=' * 130}")
    print(f"STACKED PUT-SIDE FILTER COMPARISON  (sorted by P&L)")
    print(f"Rule C3 adaptive strikes | Call: VIX<=26 + aftn<1.0% | {len(days)} trading days | {elapsed:.0f}s")
    print(f"{'=' * 130}")
    print(f"{'#':<3} {'Config':<38} {'P&L':>10} {'vs Base':>9} {'MaxDD':>8} {'WR%':>6} "
          f"{'Trades':>7} {'C/P':>9} {'Sharpe':>7} {'Calmar':>7}")
    print(f"{'-' * 130}")

    for s in results:
        delta = s["total_pnl"] - baseline_pnl
        better_sharpe = s["sharpe"] > baseline_sharpe
        better_pnl = s["total_pnl"] > baseline_pnl
        marker = ""
        if better_sharpe and better_pnl:
            marker = " <-- BEST"
        elif better_sharpe:
            marker = " *Sharpe"
        elif better_pnl:
            marker = " *P&L"

        rank = [r["label"] for r in results].index(s["label"]) + 1
        cp = f"{s['call_trades']}/{s['put_trades']}"
        print(f"{rank:<3} {s['label']:<38} ${s['total_pnl']:>+9,.0f} ${delta:>+8,.0f} "
              f"${s['max_dd']:>7,.0f} {s['wr']:>5.1f}% {s['trades']:>7} {cp:>9} "
              f"{s['sharpe']:>7.2f} {s['calmar']:>7.2f}{marker}")

    print(f"\nBaseline: {baseline['label']}  P&L=${baseline_pnl:+,.0f}  Sharpe={baseline_sharpe:.2f}")

    # Print analysis summary
    print(f"\n{'=' * 100}")
    print("ANALYSIS SUMMARY")
    print(f"{'=' * 100}")

    # Best by Sharpe
    best_sharpe = max(results, key=lambda x: x["sharpe"])
    print(f"Best Sharpe : {best_sharpe['label']}  Sharpe={best_sharpe['sharpe']:.2f}  "
          f"P&L=${best_sharpe['total_pnl']:+,.0f}  DD=${best_sharpe['max_dd']:,.0f}")

    # Best by P&L
    best_pnl = max(results, key=lambda x: x["total_pnl"])
    print(f"Best P&L    : {best_pnl['label']}  P&L=${best_pnl['total_pnl']:+,.0f}  "
          f"Sharpe={best_pnl['sharpe']:.2f}  DD=${best_pnl['max_dd']:,.0f}")

    # Best by Calmar
    best_calmar = max(results, key=lambda x: x["calmar"])
    print(f"Best Calmar : {best_calmar['label']}  Calmar={best_calmar['calmar']:.2f}  "
          f"P&L=${best_calmar['total_pnl']:+,.0f}  DD=${best_calmar['max_dd']:,.0f}")

    # Best by DD (least negative)
    best_dd = max(results, key=lambda x: x["max_dd"])
    print(f"Best DD     : {best_dd['label']}  DD=${best_dd['max_dd']:,.0f}  "
          f"P&L=${best_dd['total_pnl']:+,.0f}  Sharpe={best_dd['sharpe']:.2f}")

    # Stacking effectiveness
    print(f"\nDo stacked filters beat singles?")
    singles = [r for r in results if r["label"].startswith(("2.", "3.", "4."))]
    stacks = [r for r in results if r["label"].startswith(("5.", "6.", "7."))]
    if singles and stacks:
        best_single_sharpe = max(singles, key=lambda x: x["sharpe"])
        best_stack_sharpe = max(stacks, key=lambda x: x["sharpe"])
        print(f"  Best single-filter Sharpe: {best_single_sharpe['label']}  "
              f"Sharpe={best_single_sharpe['sharpe']:.2f}")
        print(f"  Best stacked-filter Sharpe: {best_stack_sharpe['label']}  "
              f"Sharpe={best_stack_sharpe['sharpe']:.2f}")
        if best_stack_sharpe["sharpe"] > best_single_sharpe["sharpe"]:
            print(f"  --> Stacking IMPROVES Sharpe by "
                  f"{best_stack_sharpe['sharpe'] - best_single_sharpe['sharpe']:+.2f}")
        else:
            print(f"  --> Stacking does NOT improve Sharpe vs best single filter")

    print()
