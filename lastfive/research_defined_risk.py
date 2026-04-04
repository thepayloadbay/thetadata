#!/usr/bin/env python3
"""
Research: Defined-Risk Structures — Width / Ratio / Ladder Comparison
=====================================================================
SPX 0DTE strikes are on a $5 grid, so minimum spread width is $5.

Compare 5 spread structures for LastFive strategy:
  A) Baseline: 1x $5 wide at VIX-adaptive dist (current production config)
  B) Narrow max-loss: 1x $5 wide, same dist, same tier risk (sanity check / true baseline)
     Actually: tighter sizing — cap qty so max-loss-per-day never exceeds $2,000
  C) Ratio: 2x entries at $5 wide, each at 60% risk (total ~120% exposure, capped per-trade)
  D) Multi-width ladder: Entry1 dist=0/$5 wide 40% risk + Entry2 dist=5/$10 wide 60% risk
  E) Wide spread: 1x $10 wide (more credit per spread, fewer contracts, wider buffer)

All use: C3 VIX-adaptive, bidirectional, entry 15:55, exit 16:00,
         min credit $0.10, commission $0.50/leg, tiered sizing,
         call filter: afternoon>=1.0%, put filter: afternoon<-0.75% AND mom30<-0.10%
"""

import math
import os
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Paths ──
DATA = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "2022-01-03"
END_DATE = "2026-03-25"

COMMISSION = 0.50  # per leg per contract
MIN_CREDIT = 0.10
STARTING_EQUITY = 50_000
STRIKE_STEP = 5

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def trading_dates():
    dates = []
    d = datetime.strptime(START_DATE, "%Y-%m-%d")
    e = datetime.strptime(END_DATE, "%Y-%m-%d")
    while d <= e:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in HOLIDAYS and ds not in EARLY_CLOSE:
            dates.append(ds)
        d += timedelta(days=1)
    return dates


def load_pq(year, subdir, date_str):
    path = os.path.join(DATA, str(year), subdir, f"{date_str}.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)
    return None


def get_close_at(df, hhmm):
    mask = df["_hhmm"] == hhmm
    rows = df[mask]
    return float(rows.iloc[0]["close"]) if len(rows) > 0 else None


def afternoon_return(spx_df):
    af = spx_df[(spx_df["_hhmm"] >= "13:00") & (spx_df["_hhmm"] <= "15:54")]
    if len(af) < 2:
        return None
    first = float(af.iloc[0]["close"])
    last = float(af.iloc[-1]["close"])
    return (last - first) / first * 100 if first > 0 else None


def mom30(spx_df):
    af = spx_df[(spx_df["_hhmm"] >= "15:25") & (spx_df["_hhmm"] <= "15:54")]
    if len(af) < 2:
        return None
    first = float(af.iloc[0]["close"])
    last = float(af.iloc[-1]["close"])
    return (last - first) / first * 100 if first > 0 else None


def get_quote(quotes_df, right, strike, hhmm="15:55"):
    mask = (quotes_df["right"] == right) & (quotes_df["strike"] == strike) & (quotes_df["_hhmm"] == hhmm)
    rows = quotes_df[mask]
    if len(rows) == 0:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


def get_exit_mid(quotes_df, right, strike):
    """Get settlement mid at 16:00, fallback 15:59, 15:58."""
    for hhmm in ["16:00", "15:59", "15:58"]:
        q = get_quote(quotes_df, right, strike, hhmm)
        if q is not None:
            return q["mid"]
    return None


def vix_adaptive_dist(vix):
    if vix is None:
        return None
    if vix < 18:
        return 0
    elif vix < 22:
        return 3
    elif vix <= 26:
        return 5
    else:
        return None


def tiered_risk(credit):
    if credit < 0.20:
        return 1500
    elif credit <= 0.50:
        return 2500
    else:
        return 3500


def spread_pnl(quotes_df, right, short_strike, long_strike, width, credit, qty):
    """Compute P&L for a spread at settlement using mid prices."""
    short_mid = get_exit_mid(quotes_df, right, short_strike)
    long_mid = get_exit_mid(quotes_df, right, long_strike)
    if short_mid is None or long_mid is None:
        return None

    exit_value = short_mid - long_mid
    exit_value = max(0, min(width, exit_value))

    pnl_per = credit - exit_value
    total = pnl_per * 100 * qty - 2 * COMMISSION * qty
    return total


# ── Trade configs ────────────────────────────────────────────────────────────

def make_spread(quotes_df, right, short_strike, width):
    """Build a single spread: short at short_strike, long at short+width (call) or short-width (put).
    Returns (credit, long_strike) or (None, None)."""
    if right == "C":
        long_strike = short_strike + width
    else:
        long_strike = short_strike - width

    q_s = get_quote(quotes_df, right, short_strike)
    q_l = get_quote(quotes_df, right, long_strike)
    if q_s is None or q_l is None:
        return None, None, None
    credit = q_s["bid"] - q_l["ask"]
    return credit, long_strike, None


def config_A_trades(quotes_df, right, short_strike, spot, dist):
    """A: Baseline $5 wide, standard tiered sizing."""
    width = 5
    credit, long_strike, _ = make_spread(quotes_df, right, short_strike, width)
    if credit is None or credit < MIN_CREDIT:
        return []

    risk = tiered_risk(credit)
    max_loss = (width - credit) * 100
    qty = max(1, int(risk / max_loss)) if max_loss > 0 else 1

    pnl = spread_pnl(quotes_df, right, short_strike, long_strike, width, credit, qty)
    if pnl is None:
        return []
    return [{"pnl": pnl, "credit": credit, "qty": qty, "width": width,
             "short": short_strike, "long": long_strike, "n_legs": 2}]


def config_B_trades(quotes_df, right, short_strike, spot, dist):
    """B: $5 wide, but cap max loss at $2,000 per side per day (risk-capped).
    This reduces qty on high-risk trades."""
    width = 5
    MAX_LOSS_CAP = 2000

    credit, long_strike, _ = make_spread(quotes_df, right, short_strike, width)
    if credit is None or credit < MIN_CREDIT:
        return []

    risk = tiered_risk(credit)
    max_loss = (width - credit) * 100
    qty_tier = max(1, int(risk / max_loss)) if max_loss > 0 else 1
    # Cap: ensure qty * max_loss_per <= MAX_LOSS_CAP
    qty_cap = max(1, int(MAX_LOSS_CAP / max_loss)) if max_loss > 0 else 1
    qty = min(qty_tier, qty_cap)

    pnl = spread_pnl(quotes_df, right, short_strike, long_strike, width, credit, qty)
    if pnl is None:
        return []
    return [{"pnl": pnl, "credit": credit, "qty": qty, "width": width,
             "short": short_strike, "long": long_strike, "n_legs": 2}]


def config_C_trades(quotes_df, right, short_strike, spot, dist):
    """C: Ratio — 2 entries at $5 wide, each at 60% of tier risk.
    Total exposure ~120% of baseline, but each position is individually smaller."""
    width = 5
    credit, long_strike, _ = make_spread(quotes_df, right, short_strike, width)
    if credit is None or credit < MIN_CREDIT:
        return []

    risk = tiered_risk(credit)
    risk_each = risk * 0.60
    max_loss = (width - credit) * 100
    qty_each = max(1, int(risk_each / max_loss)) if max_loss > 0 else 1
    # Enter 2 identical spreads
    total_qty = qty_each * 2

    pnl = spread_pnl(quotes_df, right, short_strike, long_strike, width, credit, total_qty)
    if pnl is None:
        return []
    return [{"pnl": pnl, "credit": credit, "qty": total_qty, "width": width,
             "short": short_strike, "long": long_strike, "n_legs": 4}]


def config_D_trades(quotes_df, right, spot, dist):
    """D: Multi-width ladder — split into 2 entries per side.
    Entry 1: dist=0, $5 wide, 40% of tier risk (close-in, high credit)
    Entry 2: dist=5 (or 10), $10 wide, 60% of tier risk (far out, safer, wider)
    """
    results = []

    # Entry 1: ATM, $5 wide, 40% risk
    width1 = 5
    if right == "C":
        short1 = int(math.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
        long1 = short1 + width1
    else:
        short1 = int(math.floor(spot / STRIKE_STEP) * STRIKE_STEP)
        long1 = short1 - width1

    q_s1 = get_quote(quotes_df, right, short1)
    q_l1 = get_quote(quotes_df, right, long1)
    if q_s1 is not None and q_l1 is not None:
        credit1 = q_s1["bid"] - q_l1["ask"]
        if credit1 >= MIN_CREDIT:
            risk1 = tiered_risk(credit1) * 0.40
            ml1 = (width1 - credit1) * 100
            qty1 = max(1, int(risk1 / ml1)) if ml1 > 0 else 1
            pnl1 = spread_pnl(quotes_df, right, short1, long1, width1, credit1, qty1)
            if pnl1 is not None:
                results.append({"pnl": pnl1, "credit": credit1, "qty": qty1, "width": width1,
                                "short": short1, "long": long1, "n_legs": 2})

    # Entry 2: dist=5, $10 wide, 60% risk
    dist2 = 5
    width2 = 10
    if right == "C":
        short2 = int(math.ceil((spot + dist2) / STRIKE_STEP) * STRIKE_STEP)
        long2 = short2 + width2
    else:
        short2 = int(math.floor((spot - dist2) / STRIKE_STEP) * STRIKE_STEP)
        long2 = short2 - width2

    q_s2 = get_quote(quotes_df, right, short2)
    q_l2 = get_quote(quotes_df, right, long2)
    if q_s2 is not None and q_l2 is not None:
        credit2 = q_s2["bid"] - q_l2["ask"]
        if credit2 >= MIN_CREDIT:
            risk2 = tiered_risk(credit2) * 0.60
            ml2 = (width2 - credit2) * 100
            qty2 = max(1, int(risk2 / ml2)) if ml2 > 0 else 1
            pnl2 = spread_pnl(quotes_df, right, short2, long2, width2, credit2, qty2)
            if pnl2 is not None:
                results.append({"pnl": pnl2, "credit": credit2, "qty": qty2, "width": width2,
                                "short": short2, "long": long2, "n_legs": 2})

    return results


def config_E_trades(quotes_df, right, short_strike, spot, dist):
    """E: $10 wide spread — more credit, fewer contracts, lower max qty.
    Wider buffer means less chance of full loss. Tiered sizing adjusts."""
    width = 10
    if right == "C":
        long_strike = short_strike + width
    else:
        long_strike = short_strike - width

    q_s = get_quote(quotes_df, right, short_strike)
    q_l = get_quote(quotes_df, right, long_strike)
    if q_s is None or q_l is None:
        return []

    credit = q_s["bid"] - q_l["ask"]
    if credit < MIN_CREDIT:
        return []

    risk = tiered_risk(credit)
    max_loss = (width - credit) * 100
    qty = max(1, int(risk / max_loss)) if max_loss > 0 else 1

    pnl = spread_pnl(quotes_df, right, short_strike, long_strike, width, credit, qty)
    if pnl is None:
        return []
    return [{"pnl": pnl, "credit": credit, "qty": qty, "width": width,
             "short": short_strike, "long": long_strike, "n_legs": 2}]


# ── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(config_key, dates):
    trades = []
    equity = STARTING_EQUITY
    peak = STARTING_EQUITY
    max_dd_dollar = 0

    for date_str in dates:
        year = date_str[:4]

        spx_df = load_pq(year, "spx_ohlc", date_str)
        vix_df = load_pq(year, "vix_ohlc", date_str)
        quotes_df = load_pq(year, "option_quotes", date_str)
        if spx_df is None or vix_df is None or quotes_df is None:
            continue

        spx_df = spx_df.copy()
        spx_df["_hhmm"] = spx_df["timestamp"].str[11:16]
        vix_df = vix_df.copy()
        vix_df["_hhmm"] = vix_df["timestamp"].str[11:16]
        quotes_df = quotes_df.copy()
        quotes_df["_hhmm"] = quotes_df["timestamp"].str[11:16]

        # VIX at 15:50
        vix_1550 = get_close_at(vix_df, "15:50")
        dist = vix_adaptive_dist(vix_1550)
        if dist is None:
            continue

        # SPX at 15:55
        spot = get_close_at(spx_df, "15:55")
        if spot is None:
            spot = get_close_at(spx_df, "15:54")
        if spot is None:
            continue

        aftn = afternoon_return(spx_df)
        m30 = mom30(spx_df)

        # ── CALL SIDE ──
        skip_call = (aftn is not None and aftn >= 1.0)
        if not skip_call:
            call_short = int(math.ceil((spot + dist) / STRIKE_STEP) * STRIKE_STEP)

            if config_key == "A":
                legs = config_A_trades(quotes_df, "C", call_short, spot, dist)
            elif config_key == "B":
                legs = config_B_trades(quotes_df, "C", call_short, spot, dist)
            elif config_key == "C":
                legs = config_C_trades(quotes_df, "C", call_short, spot, dist)
            elif config_key == "D":
                legs = config_D_trades(quotes_df, "C", spot, dist)
            elif config_key == "E":
                legs = config_E_trades(quotes_df, "C", call_short, spot, dist)
            else:
                legs = []

            for leg in legs:
                equity += leg["pnl"]
                peak = max(peak, equity)
                dd = peak - equity
                max_dd_dollar = max(max_dd_dollar, dd)
                trades.append({
                    "date": date_str, "side": "CALL", "spot": spot,
                    "pnl": leg["pnl"], "credit": leg["credit"], "qty": leg["qty"],
                    "width": leg["width"], "n_legs": leg["n_legs"],
                    "short": leg["short"], "long": leg["long"],
                    "vix": vix_1550, "equity": equity,
                })

        # ── PUT SIDE ──
        skip_put = False
        if aftn is not None and m30 is not None:
            if aftn < -0.75 and m30 < -0.10:
                skip_put = True

        if not skip_put:
            put_short = int(math.floor((spot - dist) / STRIKE_STEP) * STRIKE_STEP)

            if config_key == "A":
                legs = config_A_trades(quotes_df, "P", put_short, spot, dist)
            elif config_key == "B":
                legs = config_B_trades(quotes_df, "P", put_short, spot, dist)
            elif config_key == "C":
                legs = config_C_trades(quotes_df, "P", put_short, spot, dist)
            elif config_key == "D":
                legs = config_D_trades(quotes_df, "P", spot, dist)
            elif config_key == "E":
                legs = config_E_trades(quotes_df, "P", put_short, spot, dist)
            else:
                legs = []

            for leg in legs:
                equity += leg["pnl"]
                peak = max(peak, equity)
                dd = peak - equity
                max_dd_dollar = max(max_dd_dollar, dd)
                trades.append({
                    "date": date_str, "side": "PUT", "spot": spot,
                    "pnl": leg["pnl"], "credit": leg["credit"], "qty": leg["qty"],
                    "width": leg["width"], "n_legs": leg["n_legs"],
                    "short": leg["short"], "long": leg["long"],
                    "vix": vix_1550, "equity": equity,
                })

    return trades, equity, peak, max_dd_dollar


# ── Stats ────────────────────────────────────────────────────────────────────

def compute_stats(trades, max_dd_dollar):
    if not trades:
        return None

    df = pd.DataFrame(trades)
    pnls = df["pnl"].values
    total_pnl = pnls.sum()
    n = len(pnls)
    wins = (pnls > 0).sum()
    wr = wins / n * 100

    n_call = len(df[df["side"] == "CALL"])
    n_put = len(df[df["side"] == "PUT"])

    daily = df.groupby("date")["pnl"].sum()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0

    eq_series = STARTING_EQUITY + np.cumsum(pnls)
    peak_series = np.maximum.accumulate(eq_series)
    dd_pct_series = (peak_series - eq_series) / peak_series * 100
    max_dd_pct = dd_pct_series.max()

    n_days = len(daily)
    ann_ret_pct = total_pnl / STARTING_EQUITY / max(n_days / 252, 0.01) * 100
    calmar = ann_ret_pct / max_dd_pct if max_dd_pct > 0 else 0

    worst_day_pnl = daily.min()
    worst_day_date = daily.idxmin()
    worst_trade = pnls.min()

    gp = pnls[pnls > 0].sum()
    gl = abs(pnls[pnls < 0].sum())
    pf = gp / gl if gl > 0 else float("inf")

    avg_credit = df["credit"].mean()

    return {
        "pnl": total_pnl, "trades": n, "calls": n_call, "puts": n_put,
        "wr": wr, "sharpe": sharpe, "max_dd_pct": max_dd_pct,
        "max_dd_dollar": max_dd_dollar, "calmar": calmar, "pf": pf,
        "worst_day": f"{worst_day_date} (${worst_day_pnl:,.0f})",
        "worst_trade": worst_trade, "avg_credit": avg_credit,
        "final_eq": STARTING_EQUITY + total_pnl,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dates = trading_dates()
    print(f"Trading dates: {len(dates)} ({dates[0]} to {dates[-1]})")
    print()

    configs = {
        "A": "Baseline $5 wide (production)",
        "B": "Risk-capped $5w (max $2k loss/side)",
        "C": "Ratio 2x $5w (60% risk each)",
        "D": "Ladder: ATM/$5w + OTM5/$10w",
        "E": "Wide $10 spread",
    }

    results = {}
    for key in ["A", "B", "C", "D", "E"]:
        label = configs[key]
        print(f"Running Config {key}: {label}...")
        trades, final_eq, peak, max_dd_dollar = run_backtest(key, dates)
        stats = compute_stats(trades, max_dd_dollar)
        if stats:
            stats["label"] = f"{key}: {label}"
            results[key] = stats
            print(f"  -> {stats['trades']} trades, P&L=${stats['pnl']:,.0f}, "
                  f"Sharpe={stats['sharpe']:.2f}, DD={stats['max_dd_pct']:.2f}%")
        else:
            print(f"  -> NO TRADES")
        print()

    # ── Comparison table ──
    print("=" * 130)
    print("DEFINED-RISK STRUCTURE COMPARISON — LastFive (2022-01-03 to 2026-03-25)")
    print("=" * 130)
    hdr = (f"  {'Config':<38} {'P&L':>10} {'Trades':>7} {'WR':>6} {'MaxDD%':>7} "
           f"{'MaxDD$':>9} {'Sharpe':>7} {'Calmar':>7} {'PF':>6} {'AvgCr':>7} {'WorstTrd':>10}")
    print(hdr)
    print("  " + "-" * 126)

    for key in ["A", "B", "C", "D", "E"]:
        s = results.get(key)
        if s is None:
            print(f"  Config {key}: NO TRADES")
            continue
        print(f"  {s['label']:<38} ${s['pnl']:>9,.0f} {s['trades']:>7} {s['wr']:>5.1f}% "
              f"{s['max_dd_pct']:>6.2f}% ${s['max_dd_dollar']:>8,.0f} {s['sharpe']:>7.2f} "
              f"{s['calmar']:>7.2f} {s['pf']:>6.2f} ${s['avg_credit']:>6.3f} ${s['worst_trade']:>9,.0f}")

    print()
    print("  Worst day per config:")
    for key in ["A", "B", "C", "D", "E"]:
        s = results.get(key)
        if s:
            print(f"    {key}: {s['worst_day']}")

    print()
    print("  Call/Put breakdown:")
    print(f"  {'Config':<38} {'Calls':>7} {'Puts':>7}")
    print("  " + "-" * 53)
    for key in ["A", "B", "C", "D", "E"]:
        s = results.get(key)
        if s:
            print(f"  {s['label']:<38} {s['calls']:>7} {s['puts']:>7}")

    print()
    print("=" * 130)
    print("NOTES:")
    print("  - SPX 0DTE strikes are $5 apart, so minimum spread width is $5")
    print("  - A = current production config (baseline for comparison)")
    print("  - B = same as A but caps max loss per side at $2,000 (reduces qty on big-risk trades)")
    print("  - C = enters 2x the same spread at 60% risk each (~120% total exposure)")
    print("  - D = splits into close-in ATM/$5 (40%) + far OTM/$10 (60%) ladder")
    print("  - E = $10 wide spread (more credit but fewer contracts)")
    print("=" * 130)
