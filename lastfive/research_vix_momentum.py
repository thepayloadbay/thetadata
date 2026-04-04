"""Research: VIX Momentum Regime — adapt strike distance based on VIX direction.

Hypothesis: When VIX is falling for multiple days, the market is calming down
and it's safer to sell closer strikes (more credit). When VIX is rising, need
more distance for protection.

Configs tested:
A) Current Rule C3 (no momentum): VIX<18->d0, 18-22->d3, 22-26->d5
B) Momentum boost (3-day): falling 3+ days -> shift closer; rising 3+ days -> shift wider
C) Momentum boost aggressive (2-day): same logic but 2-day threshold
D) VIX intraday momentum: use same-day VIX change 9:30->15:50 (no look-ahead)
E) Combined: multi-day (B) + intraday (D) must agree to shift
"""
from __future__ import annotations

import glob
import math
import os
from collections import defaultdict

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
ANNUAL_RF = 0.05

# Tiered sizing
TIER_LOW_CREDIT = 0.20
TIER_HIGH_CREDIT = 0.50
RISK_LOW = 1500.0
RISK_MID = 2500.0
RISK_HIGH = 3500.0

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


def load_vix_1min(date_str: str) -> pd.DataFrame | None:
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
    """Signed afternoon return 13:00 -> 15:54."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """30-min momentum 15:25 -> 15:54."""
    bars = spx_df[(spx_df["hhmm"] >= "15:25") & (spx_df["time_str"] < "15:55:00")]
    if len(bars) < 2:
        return None
    start = float(bars["close"].iloc[0])
    end = float(bars["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


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


def get_tiered_risk(credit: float) -> float:
    if credit < TIER_LOW_CREDIT:
        return RISK_LOW
    elif credit > TIER_HIGH_CREDIT:
        return RISK_HIGH
    return RISK_MID


# ── Build VIX daily close series for multi-day momentum ────────────────
def build_vix_daily() -> dict[str, float]:
    """Return dict[date_str -> vix_close] from daily VIX parquets."""
    vix_daily = {}
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix_close" in vdf.columns:
                ds = str(vdf["date"].iloc[0])
                vix_daily[ds] = float(vdf["vix_close"].iloc[0])
        except Exception:
            pass
    # Also try vix_history.csv
    vix_hist_path = os.path.join(DATA_DIR, "vix_history.csv")
    if os.path.exists(vix_hist_path):
        vh = pd.read_csv(vix_hist_path, parse_dates=["DATE"])
        for _, row in vh.iterrows():
            ds = row["DATE"].strftime("%Y%m%d")
            vix_daily[ds] = float(row["CLOSE"])
    return vix_daily


def compute_consecutive_vix_days(vix_daily: dict[str, float]) -> dict[str, int]:
    """For each date, compute consecutive falling/rising days.

    Positive = consecutive falling days (good, VIX dropping).
    Negative = consecutive rising days (bad, VIX rising).
    """
    sorted_dates = sorted(vix_daily.keys())
    consec = {}
    streak = 0  # positive=falling, negative=rising

    for i, ds in enumerate(sorted_dates):
        if i == 0:
            consec[ds] = 0
            continue
        prev_ds = sorted_dates[i - 1]
        today_vix = vix_daily[ds]
        prev_vix = vix_daily[prev_ds]
        if today_vix < prev_vix:
            # VIX fell today
            if streak > 0:
                streak += 1
            else:
                streak = 1
        elif today_vix > prev_vix:
            # VIX rose today
            if streak < 0:
                streak -= 1
            else:
                streak = -1
        else:
            streak = 0
        consec[ds] = streak

    return consec


# ── Distance rules ─────────────────────────────────────────────────────
# Base C3 distance from VIX level
DIST_TIERS = [0.0, 3.0, 5.0]  # d0, d3, d5


def base_c3_dist(vix: float) -> float:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    else:
        return 5.0


def base_c3_tier_idx(vix: float) -> int:
    """Return tier index: 0=close(d0), 1=mid(d3), 2=wide(d5)."""
    if vix < 18:
        return 0
    elif vix < 22:
        return 1
    else:
        return 2


def shift_tier(tier_idx: int, shift: int) -> int | None:
    """Shift tier by amount. Return None if out of range (skip day)."""
    new = tier_idx + shift
    if new < 0:
        return 0  # can't go closer than d0
    if new > 2:
        return None  # wider than d5 -> skip
    return new


# Rule A: no momentum
def rule_a(vix1550: float, **kwargs) -> float | None:
    return base_c3_dist(vix1550)


# Rule B: 3-day momentum
def rule_b(vix1550: float, consec_days: int = 0, **kwargs) -> float | None:
    tier = base_c3_tier_idx(vix1550)
    if consec_days >= 3:
        # VIX falling 3+ days -> shift closer
        tier = shift_tier(tier, -1)
    elif consec_days <= -3:
        # VIX rising 3+ days -> shift wider
        tier = shift_tier(tier, +1)
    if tier is None:
        return None  # skip
    return DIST_TIERS[tier]


# Rule C: 2-day momentum (aggressive)
def rule_c_mom(vix1550: float, consec_days: int = 0, **kwargs) -> float | None:
    tier = base_c3_tier_idx(vix1550)
    if consec_days >= 2:
        tier = shift_tier(tier, -1)
    elif consec_days <= -2:
        tier = shift_tier(tier, +1)
    if tier is None:
        return None
    return DIST_TIERS[tier]


# Rule D: intraday VIX momentum
def rule_d(vix1550: float, vix_intraday_change: float = 0.0, **kwargs) -> float | None:
    tier = base_c3_tier_idx(vix1550)
    if vix_intraday_change < -1.0:
        # VIX dropped > 1pt intraday -> shift closer
        tier = shift_tier(tier, -1)
    elif vix_intraday_change > 1.0:
        # VIX rose > 1pt intraday -> shift wider
        tier = shift_tier(tier, +1)
    if tier is None:
        return None
    return DIST_TIERS[tier]


# Rule E: combined (B + D must agree)
def rule_e(vix1550: float, consec_days: int = 0, vix_intraday_change: float = 0.0, **kwargs) -> float | None:
    tier = base_c3_tier_idx(vix1550)
    multiday_closer = consec_days >= 3
    multiday_wider = consec_days <= -3
    intraday_closer = vix_intraday_change < -1.0
    intraday_wider = vix_intraday_change > 1.0

    if multiday_closer and intraday_closer:
        tier = shift_tier(tier, -1)
    elif multiday_wider and intraday_wider:
        tier = shift_tier(tier, +1)
    # If they disagree or neither triggers, stay at base
    if tier is None:
        return None
    return DIST_TIERS[tier]


RULES = {
    "A_baseline": rule_a,
    "B_mom3day":  rule_b,
    "C_mom2day":  rule_c_mom,
    "D_intraday": rule_d,
    "E_combined": rule_e,
}


# ── Simulate one side ──────────────────────────────────────────────────
def sim_call(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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

    risk = get_tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

    if spx_exit <= short_strike:
        settlement = 0.0
    elif spx_exit >= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = spx_exit - short_strike

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "CALL", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike}


def sim_put(spot: float, spx_exit: float, dist: float,
            quotes_df: pd.DataFrame, entry_time: str) -> dict | None:
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

    risk = get_tiered_risk(credit)
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

    if spx_exit >= short_strike:
        settlement = 0.0
    elif spx_exit <= long_strike:
        settlement = SPREAD_WIDTH
    else:
        settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": "PUT", "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike}


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("VIX Momentum Regime Research — Strike Distance Adaptation")
    print("=" * 110)

    # Build VIX daily series and consecutive-day tracker
    print("\nLoading VIX daily closes...")
    vix_daily = build_vix_daily()
    print(f"  {len(vix_daily)} VIX daily records")
    consec_days = compute_consecutive_vix_days(vix_daily)

    # Collect all candidate dates
    print("Loading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = sorted(set(
        os.path.basename(f).replace(".parquet", "")
        for f in all_spx
        if START_DATE <= os.path.basename(f).replace(".parquet", "") <= END_DATE
    ))
    print(f"  {len(all_dates)} candidate dates")

    # Pre-collect day data
    print("Loading day data (SPX, VIX intraday, options)...")
    day_data = []
    skipped = defaultdict(int)

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

        vix_df = load_vix_1min(ds)
        if vix_df is None:
            skipped["no_vix_1min"] += 1
            continue

        vix1550 = get_price_at(vix_df, "15:50")
        if vix1550 is None or vix1550 <= 0:
            skipped["no_vix1550"] += 1
            continue

        # VIX > 26 filter
        if vix1550 > 26:
            skipped["vix_gt26"] += 1
            continue

        # Afternoon return (signed)
        aft_ret = compute_afternoon_return(spx_df)

        # Call filter: skip calls if afternoon >= 1.0%
        skip_calls = aft_ret is not None and aft_ret >= 1.0

        # Put filter: skip puts if afternoon < -0.75% AND mom30 < -0.10%
        mom30 = compute_mom30(spx_df)
        skip_puts = (aft_ret is not None and aft_ret < -0.75
                     and mom30 is not None and mom30 < -0.10)

        # Spot at 15:55
        spot = get_price_at(spx_df, "15:55")
        if spot is None:
            skipped["no_spot"] += 1
            continue

        # SPX at 16:00
        spx_exit = get_price_at(spx_df, "16:00")
        if spx_exit is None:
            spx_exit = get_price_at(spx_df, "15:59")
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        opts_df = load_options(ds)
        if opts_df is None:
            skipped["no_opts"] += 1
            continue

        # VIX intraday change: 9:31 -> 15:50 (no look-ahead; 9:30 bar often 0)
        vix_930 = get_price_at(vix_df, "09:31")
        if vix_930 is None:
            vix_930 = get_price_at(vix_df, "09:32")
        vix_intraday_change = (vix1550 - vix_930) if vix_930 is not None else 0.0

        # Multi-day consecutive direction (using prior close, no look-ahead)
        # We use the streak as of the PREVIOUS trading day's close
        # because today's VIX close isn't known yet at 15:55
        # Actually, consec_days[ds] is computed from daily closes including today
        # To avoid look-ahead, we need yesterday's streak
        # But VIX daily close IS today's close -- look-ahead!
        # Safe approach: use streak up to yesterday
        sorted_vix_dates = sorted(vix_daily.keys())
        ds_idx = None
        for j, vd in enumerate(sorted_vix_dates):
            if vd == ds:
                ds_idx = j
                break
        if ds_idx is not None and ds_idx > 0:
            prev_date = sorted_vix_dates[ds_idx - 1]
            cd = consec_days.get(prev_date, 0)
        else:
            cd = 0

        day_data.append({
            "date": ds,
            "spot": spot,
            "spx_exit": spx_exit,
            "vix1550": vix1550,
            "opts_df": opts_df,
            "skip_calls": skip_calls,
            "skip_puts": skip_puts,
            "consec_days": cd,
            "vix_intraday_change": vix_intraday_change,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {dict(skipped)}")

    # Pre-compute sorted VIX dates list for faster lookups -- already done above

    # ── VIX momentum distribution ──────────────────────────────────────
    cd_vals = [dd["consec_days"] for dd in day_data]
    vid_vals = [dd["vix_intraday_change"] for dd in day_data]
    print(f"\n  Multi-day VIX streak distribution (prior day):")
    for streak_val in [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]:
        if streak_val <= -5:
            cnt = sum(1 for v in cd_vals if v <= streak_val)
            label = f"<={streak_val}"
        elif streak_val >= 5:
            cnt = sum(1 for v in cd_vals if v >= streak_val)
            label = f">={streak_val}"
        else:
            cnt = sum(1 for v in cd_vals if v == streak_val)
            label = f"  {streak_val:+d}"
        print(f"    {label}: {cnt} days")

    print(f"\n  VIX intraday change (9:31->15:50) distribution:")
    for lo, hi, label in [(-99, -2, "<-2pt"), (-2, -1, "-2 to -1pt"), (-1, 0, "-1 to 0pt"),
                           (0, 1, "0 to +1pt"), (1, 2, "+1 to +2pt"), (2, 99, ">+2pt")]:
        cnt = sum(1 for v in vid_vals if lo <= v < hi)
        print(f"    {label}: {cnt} days")

    # ── Run each rule ──────────────────────────────────────────────────
    results = {}
    for rule_name, rule_fn in RULES.items():
        all_day_pnls = []
        all_trades = []
        n_skipped_by_rule = 0

        for dd in day_data:
            dist = rule_fn(
                vix1550=dd["vix1550"],
                consec_days=dd["consec_days"],
                vix_intraday_change=dd["vix_intraday_change"],
            )
            if dist is None:
                n_skipped_by_rule += 1
                continue

            day_pnl = 0.0
            day_traded = False

            # Call side
            if not dd["skip_calls"]:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct is not None:
                    all_trades.append(ct)
                    day_pnl += ct["pnl"]
                    day_traded = True

            # Put side
            if not dd["skip_puts"]:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt is not None:
                    all_trades.append(pt)
                    day_pnl += pt["pnl"]
                    day_traded = True

            if day_traded:
                all_day_pnls.append(day_pnl)

        n_trades = len(all_trades)
        n_calls = sum(1 for t in all_trades if t["side"] == "CALL")
        n_puts = sum(1 for t in all_trades if t["side"] == "PUT")

        if n_trades == 0:
            results[rule_name] = None
            continue

        day_pnl_arr = np.array(all_day_pnls)
        total_pnl = day_pnl_arr.sum()

        equity = np.cumsum(day_pnl_arr) + STARTING_EQUITY
        peak = np.maximum.accumulate(equity)
        dd_arr = (equity - peak) / peak * 100  # percent DD
        max_dd_pct = dd_arr.min()
        max_dd_dollar = (equity - peak).min()

        n_wins = sum(1 for t in all_trades if t["pnl"] > 0)
        wr = n_wins / n_trades * 100

        # Sharpe (daily)
        daily_rf = ANNUAL_RF / 252
        excess = day_pnl_arr - daily_rf * STARTING_EQUITY
        sharpe = excess.mean() / day_pnl_arr.std() * np.sqrt(252) if day_pnl_arr.std() > 0 else 0

        # Calmar
        years = len(day_pnl_arr) / 252
        annual_ret = total_pnl / years if years > 0 else 0
        calmar = annual_ret / abs(max_dd_dollar) if max_dd_dollar != 0 else 0

        results[rule_name] = {
            "trades": n_trades,
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_pnl": total_pnl,
            "max_dd": max_dd_dollar,
            "max_dd_pct": max_dd_pct,
            "wr": wr,
            "sharpe": sharpe,
            "calmar": calmar,
            "skipped_by_rule": n_skipped_by_rule,
        }

    # ── Print comparison table ──────────────────────────────────────────
    print("\n" + "=" * 130)
    print("COMPARISON TABLE — VIX Momentum Regime (Bidirectional, Tiered Sizing, Full Filters)")
    print("=" * 130)
    print(f"{'Config':<14} {'Trades':>7} {'C/P':>9} {'Total P&L':>12} {'Max DD$':>10} "
          f"{'Max DD%':>8} {'WR%':>6} {'Sharpe':>7} {'Calmar':>7} {'Skipped':>8}")
    print("-" * 130)

    for rule_name in RULES:
        r = results.get(rule_name)
        if r is None:
            print(f"{rule_name:<14}  NO TRADES")
            continue
        print(
            f"{rule_name:<14} {r['trades']:>7} "
            f"{r['n_calls']:>4}/{r['n_puts']:<4} "
            f"${r['total_pnl']:>10,.0f} ${r['max_dd']:>8,.0f} "
            f"{r['max_dd_pct']:>7.2f}% "
            f"{r['wr']:>5.1f}% "
            f"{r['sharpe']:>7.2f} {r['calmar']:>7.2f} "
            f"{r['skipped_by_rule']:>8}"
        )
    print("=" * 130)

    # ── Delta vs baseline ──────────────────────────────────────────────
    baseline = results.get("A_baseline")
    if baseline:
        print("\nDelta vs A_baseline:")
        print("-" * 90)
        for rule_name in RULES:
            if rule_name == "A_baseline":
                continue
            r = results.get(rule_name)
            if r is None:
                continue
            dpnl = r["total_pnl"] - baseline["total_pnl"]
            ddd = r["max_dd"] - baseline["max_dd"]
            dsharpe = r["sharpe"] - baseline["sharpe"]
            dcalmar = r["calmar"] - baseline["calmar"]
            dtrades = r["trades"] - baseline["trades"]
            print(f"  {rule_name:<14}: P&L {dpnl:>+10,.0f}  |  DD {ddd:>+8,.0f}  |  "
                  f"Sharpe {dsharpe:>+.2f}  |  Calmar {dcalmar:>+.2f}  |  Trades {dtrades:>+4}")

    # ── Distance shift distribution per rule ───────────────────────────
    print("\nDistance tier shift distribution per rule:")
    print("-" * 90)
    for rule_name, rule_fn in RULES.items():
        if rule_name == "A_baseline":
            continue
        shift_counts = {"closer": 0, "same": 0, "wider": 0, "skip": 0}
        for dd in day_data:
            base_dist = base_c3_dist(dd["vix1550"])
            new_dist = rule_fn(
                vix1550=dd["vix1550"],
                consec_days=dd["consec_days"],
                vix_intraday_change=dd["vix_intraday_change"],
            )
            if new_dist is None:
                shift_counts["skip"] += 1
            elif new_dist < base_dist:
                shift_counts["closer"] += 1
            elif new_dist > base_dist:
                shift_counts["wider"] += 1
            else:
                shift_counts["same"] += 1
        total = sum(shift_counts.values())
        print(f"  {rule_name:<14}: closer={shift_counts['closer']:>4} ({shift_counts['closer']/total*100:.1f}%), "
              f"same={shift_counts['same']:>4} ({shift_counts['same']/total*100:.1f}%), "
              f"wider={shift_counts['wider']:>4} ({shift_counts['wider']/total*100:.1f}%), "
              f"skip={shift_counts['skip']:>4} ({shift_counts['skip']/total*100:.1f}%)")

    # ── Year-by-year ───────────────────────────────────────────────────
    print("\nYear-by-year P&L:")
    print("-" * 100)
    years = sorted(set(dd["date"][:4] for dd in day_data))
    header = f"{'Config':<14}"
    for yr in years:
        header += f" {yr:>12}"
    print(header)
    print("-" * 100)

    for rule_name, rule_fn in RULES.items():
        yr_pnl = {yr: 0.0 for yr in years}
        for dd in day_data:
            dist = rule_fn(
                vix1550=dd["vix1550"],
                consec_days=dd["consec_days"],
                vix_intraday_change=dd["vix_intraday_change"],
            )
            if dist is None:
                continue
            day_pnl = 0.0
            if not dd["skip_calls"]:
                ct = sim_call(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if ct:
                    day_pnl += ct["pnl"]
            if not dd["skip_puts"]:
                pt = sim_put(dd["spot"], dd["spx_exit"], dist, dd["opts_df"], "15:55:00")
                if pt:
                    day_pnl += pt["pnl"]
            yr_pnl[dd["date"][:4]] += day_pnl

        row = f"{rule_name:<14}"
        for yr in years:
            row += f" ${yr_pnl[yr]:>10,.0f}"
        print(row)
    print("-" * 100)

    # ── Rule descriptions ─────────────────────────────────────────────
    print("\nRule definitions:")
    print("  A_baseline : Rule C3 VIX-adaptive, no momentum adjustment")
    print("               VIX<18->d0, 18-22->d3, 22-26->d5")
    print("  B_mom3day  : If VIX falling 3+ consecutive days -> shift one tier closer")
    print("               If VIX rising 3+ consecutive days -> shift one tier wider (d5->skip)")
    print("  C_mom2day  : Same as B but 2-day threshold (more aggressive)")
    print("  D_intraday : VIX intraday change 9:31->15:50: dropped >1pt -> closer, rose >1pt -> wider")
    print("  E_combined : B + D must BOTH agree to shift")

    print("\nDone.")


if __name__ == "__main__":
    main()
