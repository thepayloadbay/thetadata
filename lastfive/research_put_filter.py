"""Research: PUT-specific downside filters for LastFive strategy.

The current afternoon return filter (skip if >= 1.0%) protects call spreads from upside rips.
But put spread losses come from SPX DROPPING. This script researches whether a separate
downside filter for the put side improves results.

Part 1: Analyze PUT-side losses — what do afternoon return, momentum, VIX look like?
Part 2: Test PUT-SPECIFIC filters (only skip put side, call side keeps current filters).
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

# ── paths ──────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
TRADE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradelogs_lastfive.csv")

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
MIN_SHORT_DISTANCE = 3.0
SPREAD_WIDTH = 5.0
STRIKE_STEP = 5
RISK_AMOUNT = 2500.0
MIN_NET_CREDIT = 0.10
COMMISSION = 0.50
VIX_INTRADAY_MAX = 26.0
AFTERNOON_RETURN_MAX = 1.0  # existing call-side filter (skip if >= 1.0%)
START_DATE = "20220103"
END_DATE = "20260325"


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


# ── simulate one day, both sides, with optional put filter ─────────────
def _sim_day(date_str: str, spx: pd.DataFrame, qdf: pd.DataFrame,
             vix_1550: float | None, afternoon_ret: float | None,
             last_hour_ret: float | None, mom30: float | None,
             put_filter_fn=None):
    """Returns (call_trade, put_trade) dicts or None for each side."""
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

    # ── CALL SIDE (always uses global filters only) ──
    call_trade = None
    call_short = int(math.ceil((spot + MIN_SHORT_DISTANCE) / STRIKE_STEP) * STRIKE_STEP)
    call_long = call_short + int(SPREAD_WIDTH)
    if call_short not in available or call_long not in available:
        cands = sorted(s for s in available if s >= spot + MIN_SHORT_DISTANCE)
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
                call_trade = {"date": date_str, "side": "CALL", "pnl": pnl, "qty": qty,
                              "credit": credit, "settle": settle}

    # ── PUT SIDE (apply put-specific filter if provided) ──
    put_trade = None
    # Check put filter
    if put_filter_fn is not None:
        if put_filter_fn(afternoon_ret, last_hour_ret, mom30, vix_1550):
            return call_trade, None  # skip put

    put_short = int(math.floor((spot - MIN_SHORT_DISTANCE) / STRIKE_STEP) * STRIKE_STEP)
    put_long = put_short - int(SPREAD_WIDTH)
    if put_short not in available or put_long not in available:
        cands = sorted((s for s in available if s <= spot - MIN_SHORT_DISTANCE), reverse=True)
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
                put_trade = {"date": date_str, "side": "PUT", "pnl": pnl, "qty": qty,
                             "credit": credit, "settle": settle}

    return call_trade, put_trade


# ── full simulation ────────────────────────────────────────────────────
def run_simulation(put_filter_fn=None, label="baseline"):
    """Run full backtest. Returns (trades_list, stats_dict)."""
    dates = _all_dates()
    all_trades = []

    for date_str in dates:
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue

        spx = _load_spx(date_str)
        if spx is None:
            continue

        # VIX at 15:50
        vix_df = _load_vix(date_str)
        vix_1550 = _vix_at(vix_df, "15:50") if vix_df is not None else None
        if vix_1550 is not None and vix_1550 > VIX_INTRADAY_MAX:
            continue

        # Afternoon return (global filter for calls)
        afternoon_ret = _afternoon_return(spx)
        if afternoon_ret is not None and afternoon_ret >= AFTERNOON_RETURN_MAX:
            continue  # skip entire day (existing behavior)

        # Additional features for put filter
        last_hour_ret = _last_hour_return(spx)
        mom30 = _mom_30(spx)

        qdf = _load_quotes(date_str)
        if qdf is None:
            continue

        ct, pt = _sim_day(date_str, spx, qdf, vix_1550, afternoon_ret,
                          last_hour_ret, mom30, put_filter_fn)
        if ct:
            all_trades.append(ct)
        if pt:
            all_trades.append(pt)

    return _compute_stats(all_trades, label)


def _compute_stats(trades: list, label: str) -> dict:
    if not trades:
        return {"label": label, "total_pnl": 0, "max_dd": 0, "wr": 0,
                "trades": 0, "call_trades": 0, "put_trades": 0, "sharpe": 0}

    df = pd.DataFrame(trades)
    total_pnl = df["pnl"].sum()
    wins = (df["pnl"] > 0).sum()
    wr = wins / len(df) * 100

    calls = df[df["side"] == "CALL"]
    puts = df[df["side"] == "PUT"]

    # Daily P&L for Sharpe and DD
    daily = df.groupby("date")["pnl"].sum().sort_index()
    equity = 50000 + daily.cumsum()
    running_max = equity.cummax()
    dd = (equity - running_max).min()

    # Sharpe (annualized)
    if daily.std() > 0:
        sharpe = (daily.mean() / daily.std()) * np.sqrt(252)
    else:
        sharpe = 0

    return {
        "label": label,
        "total_pnl": round(total_pnl, 0),
        "max_dd": round(dd, 0),
        "wr": round(wr, 1),
        "trades": len(df),
        "call_trades": len(calls),
        "put_trades": len(puts),
        "sharpe": round(sharpe, 2),
        "avg_win": round(df[df["pnl"] > 0]["pnl"].mean(), 0) if wins > 0 else 0,
        "avg_loss": round(df[df["pnl"] <= 0]["pnl"].mean(), 0) if (df["pnl"] <= 0).sum() > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════════
#  PART 1: FEATURE ANALYSIS OF PUT LOSSES
# ══════════════════════════════════════════════════════════════════════
def analyze_put_losses():
    """Load trade log, compute features for each put trade, compare wins vs losses."""
    log = pd.read_csv(TRADE_LOG)
    puts = log[log["side"] == "PUT"].copy()
    print(f"Total PUT trades in log: {len(puts)} ({int(puts['win'].sum())}W / {int(puts['loss'].sum())}L)")
    print()

    # Compute features for each put trade date
    rows = []
    for _, trade in puts.iterrows():
        ds = str(trade["date"])
        spx = _load_spx(ds)
        if spx is None:
            continue
        vix_df = _load_vix(ds)

        af = _afternoon_return(spx)
        lhr = _last_hour_return(spx)
        m30 = _mom_30(spx)
        vix = _vix_at(vix_df, "15:50") if vix_df is not None else None

        # 5-min move (the actual entry->exit)
        move = trade["spx_at_exit"] - trade["spx_at_entry"]

        rows.append({
            "date": ds,
            "pnl": trade["pnl_earned"],
            "win": trade["win"],
            "afternoon_ret": af,
            "last_hour_ret": lhr,
            "mom_30": m30,
            "vix_1550": vix,
            "spx_move_5min": move,
        })

    df = pd.DataFrame(rows)
    wins = df[df["win"] == 1]
    losses = df[df["win"] == 0]

    print("=" * 80)
    print("FEATURE COMPARISON: PUT WINS vs PUT LOSSES")
    print("=" * 80)

    for feat in ["afternoon_ret", "last_hour_ret", "mom_30", "vix_1550", "spx_move_5min"]:
        w = wins[feat].dropna()
        l = losses[feat].dropna()
        pooled_std = np.sqrt((w.var() * (len(w)-1) + l.var() * (len(l)-1)) / (len(w) + len(l) - 2)) if (len(w) + len(l) > 2) else 1
        cohen_d = (l.mean() - w.mean()) / pooled_std if pooled_std > 0 else 0

        print(f"\n{feat}:")
        print(f"  Wins  (n={len(w):3d}): mean={w.mean():+.4f}%  median={w.median():+.4f}%  std={w.std():.4f}")
        print(f"  Losses(n={len(l):3d}): mean={l.mean():+.4f}%  median={l.median():+.4f}%  std={l.std():.4f}")
        print(f"  Cohen's d = {cohen_d:+.3f}  (>0.3 = meaningful separation)")

    print("\n" + "=" * 80)
    print("PUT LOSS DAYS — DETAILED")
    print("=" * 80)
    loss_df = df[df["win"] == 0].sort_values("pnl")
    for _, r in loss_df.iterrows():
        print(f"  {r['date']}  P&L=${r['pnl']:>+8.0f}  aft_ret={r['afternoon_ret']:+.3f}%  "
              f"last_hr={r['last_hour_ret']:+.3f}%  mom30={r['mom_30']:+.3f}%  "
              f"vix={r['vix_1550']:.1f}  5min_move={r['spx_move_5min']:+.1f}")

    # Distribution of afternoon return for losses
    print("\n" + "=" * 80)
    print("PUT LOSS afternoon_ret DISTRIBUTION")
    print("=" * 80)
    l_af = losses["afternoon_ret"].dropna()
    for thresh in [-1.5, -1.0, -0.75, -0.5, -0.3, -0.1, 0.0, 0.3, 0.5]:
        below = (l_af < thresh).sum()
        print(f"  afternoon_ret < {thresh:+.2f}%: {below}/{len(l_af)} losses ({below/len(l_af)*100:.0f}%)")

    print("\nPUT LOSS last_hour_ret DISTRIBUTION")
    l_lh = losses["last_hour_ret"].dropna()
    for thresh in [-0.5, -0.3, -0.2, -0.1, 0.0, 0.1, 0.3]:
        below = (l_lh < thresh).sum()
        print(f"  last_hour_ret < {thresh:+.2f}%: {below}/{len(l_lh)} losses ({below/len(l_lh)*100:.0f}%)")

    print("\nPUT LOSS mom_30 DISTRIBUTION")
    l_m = losses["mom_30"].dropna()
    for thresh in [-0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1]:
        below = (l_m < thresh).sum()
        print(f"  mom_30 < {thresh:+.3f}%: {below}/{len(l_m)} losses ({below/len(l_m)*100:.0f}%)")

    # Also count how many WINS would be filtered at each threshold
    print("\n" + "=" * 80)
    print("FILTER IMPACT: losses caught vs wins sacrificed")
    print("=" * 80)
    w_af = wins["afternoon_ret"].dropna()
    w_lh = wins["last_hour_ret"].dropna()
    w_m = wins["mom_30"].dropna()

    print(f"\n{'Filter':<40} {'Losses caught':>14} {'Wins lost':>10} {'Net impact':>12}")
    print("-" * 80)
    for thresh in [-1.5, -1.0, -0.75, -0.5]:
        lc = (l_af < thresh).sum()
        wl = (w_af < thresh).sum()
        print(f"afternoon_ret < {thresh:+.2f}%              {lc:>3}/{len(l_af)} ({lc/len(l_af)*100:4.0f}%)   {wl:>3}/{len(w_af)}     skip {lc+wl} days")

    for thresh in [-0.5, -0.3]:
        lc = (l_lh < thresh).sum()
        wl = (w_lh < thresh).sum()
        print(f"last_hour_ret < {thresh:+.2f}%              {lc:>3}/{len(l_lh)} ({lc/len(l_lh)*100:4.0f}%)   {wl:>3}/{len(w_lh)}     skip {lc+wl} days")

    for thresh in [-0.2, -0.1]:
        lc = (l_m < thresh).sum()
        wl = (w_m < thresh).sum()
        print(f"mom_30 < {thresh:+.3f}%                     {lc:>3}/{len(l_m)} ({lc/len(l_m)*100:4.0f}%)   {wl:>3}/{len(w_m)}     skip {lc+wl} days")

    return df


# ══════════════════════════════════════════════════════════════════════
#  PART 2: FULL BACKTEST WITH PUT-SPECIFIC FILTERS
# ══════════════════════════════════════════════════════════════════════
def run_filter_sweep():
    """Test various put-specific downside filters."""

    filters = {
        "BASELINE (no put filter)": None,
        "Put skip: aft_ret < -0.50%": lambda af, lh, m, v: af is not None and af < -0.50,
        "Put skip: aft_ret < -0.75%": lambda af, lh, m, v: af is not None and af < -0.75,
        "Put skip: aft_ret < -1.00%": lambda af, lh, m, v: af is not None and af < -1.00,
        "Put skip: aft_ret < -1.50%": lambda af, lh, m, v: af is not None and af < -1.50,
        "Put skip: last_hr < -0.30%": lambda af, lh, m, v: lh is not None and lh < -0.30,
        "Put skip: last_hr < -0.50%": lambda af, lh, m, v: lh is not None and lh < -0.50,
        "Put skip: mom_30 < -0.10%":  lambda af, lh, m, v: m is not None and m < -0.10,
        "Put skip: mom_30 < -0.20%":  lambda af, lh, m, v: m is not None and m < -0.20,
    }

    results = []
    for label, fn in filters.items():
        print(f"  Running: {label} ...", end=" ", flush=True)
        stats = run_simulation(put_filter_fn=fn, label=label)
        results.append(stats)
        print(f"done  (P&L=${stats['total_pnl']:+,.0f})")

    baseline_pnl = results[0]["total_pnl"]

    print("\n" + "=" * 120)
    print("PUT-SPECIFIC FILTER COMPARISON (call side unchanged, put side filtered)")
    print("=" * 120)
    print(f"{'Filter':<35} {'P&L':>10} {'Delta':>8} {'MaxDD':>8} {'Sharpe':>7} {'WR%':>6} "
          f"{'Trades':>7} {'Calls':>6} {'Puts':>6} {'AvgW':>7} {'AvgL':>8}")
    print("-" * 120)

    for s in results:
        delta = s["total_pnl"] - baseline_pnl
        marker = " ***" if delta > 1000 and s["max_dd"] >= results[0]["max_dd"] * 0.8 else ""
        marker = " ***" if delta > 0 and s["sharpe"] > results[0]["sharpe"] else marker
        print(f"{s['label']:<35} {s['total_pnl']:>+10,.0f} {delta:>+8,.0f} {s['max_dd']:>8,.0f} "
              f"{s['sharpe']:>7.2f} {s['wr']:>5.1f}% {s['trades']:>7} {s['call_trades']:>6} "
              f"{s['put_trades']:>6} {s['avg_win']:>+7,.0f} {s['avg_loss']:>+8,.0f}{marker}")

    print("\n*** = improved Sharpe over baseline")
    return results


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 80)
    print("PART 1: PUT LOSS FEATURE ANALYSIS")
    print("=" * 80)
    feat_df = analyze_put_losses()

    print("\n\n")
    print("=" * 80)
    print("PART 2: PUT-SPECIFIC FILTER BACKTEST SWEEP")
    print("=" * 80)
    results = run_filter_sweep()
