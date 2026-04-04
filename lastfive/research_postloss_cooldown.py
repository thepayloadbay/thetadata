#!/usr/bin/env python3
"""
Research: Post-Loss Cooldown for LastFive strategy
===================================================
Tests whether reducing position size or skipping after losing days
can reduce drawdown cascades and improve risk-adjusted returns.

Config: C3 VIX-adaptive + equity compounding (5% tiered) + stacked put filter
Bidirectional, entry 15:55, exit 16:00, width $5, min credit $0.10, comm $0.50/leg

Cooldown rules tested:
  A — No cooldown (baseline)
  B — 50% size for 1 day after loss
  C — 50% size for 2 days after loss
  D — Skip next day entirely after any losing day
  E — Graduated: 1 loss -> 75%, 2 consec -> 50%, 3+ -> 25%, reset on win
  F — Severity: loss >2% eq -> 50%/2d, >1% -> 75%/1d, else normal
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "20220103"
END_DATE = "20260325"

SPREAD_WIDTH = 5.0
MIN_CREDIT = 0.10
COMMISSION = 0.50  # per leg per contract
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
    """Signed afternoon return: 13:00 to 15:54."""
    afternoon = spx_df[(spx_df["hhmm"] >= "13:00") & (spx_df["time_str"] < "15:55:00")]
    if len(afternoon) < 2:
        return None
    start = float(afternoon["close"].iloc[0])
    end = float(afternoon["close"].iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100


def compute_mom30(spx_df: pd.DataFrame) -> float | None:
    """30-min momentum: 15:25 to 15:54."""
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
        (quotes_df["right"] == right) &
        (quotes_df["strike"] == strike) &
        (quotes_df["time_str"] == time_str)
    )
    rows = quotes_df[mask]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


# ── Rule C3 adaptive distance ──────────────────────────────────────────
def rule_c3_dist(vix: float) -> float | None:
    """VIX<18 -> 0; 18-22 -> 3; 22-26 -> 5; >26 -> skip."""
    if vix < 18:
        return 0.0
    elif vix < 22:
        return 3.0
    elif vix <= 26:
        return 5.0
    else:
        return None  # skip


# ── Equity-based tiered sizing ─────────────────────────────────────────
def equity_risk(credit: float, equity: float) -> float:
    """5% tiered: cr<0.20 -> 3% eq, 0.20-0.50 -> 5% eq, >0.50 -> 7% eq."""
    if credit < 0.20:
        return equity * 0.03
    elif credit <= 0.50:
        return equity * 0.05
    else:
        return equity * 0.07


# ── Simulate one side ──────────────────────────────────────────────────
def sim_side(spot: float, spx_exit: float, dist: float,
             quotes_df: pd.DataFrame, entry_time: str,
             right: str, equity: float,
             size_mult: float = 1.0) -> dict | None:
    """Simulate a credit spread (call or put) with equity-based sizing.
    size_mult: cooldown multiplier (1.0 = full, 0.5 = half, etc.)
    """
    if right == "C":
        target = spot + dist
        short_strike = int(math.ceil(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike + int(SPREAD_WIDTH)
    else:
        target = spot - dist
        short_strike = int(math.floor(target / STRIKE_STEP) * STRIKE_STEP)
        long_strike = short_strike - int(SPREAD_WIDTH)

    available = set(quotes_df[quotes_df["right"] == right]["strike"].unique())
    if short_strike not in available or long_strike not in available:
        if right == "C":
            candidates = sorted(s for s in available if s >= spot + dist)
        else:
            candidates = sorted((s for s in available if s <= spot - dist), reverse=True)
        if not candidates:
            return None
        short_strike = int(candidates[0])
        if right == "C":
            long_strike = short_strike + int(SPREAD_WIDTH)
        else:
            long_strike = short_strike - int(SPREAD_WIDTH)
        if long_strike not in available:
            return None

    short_q = get_quote(quotes_df, right, short_strike, entry_time)
    long_q = get_quote(quotes_df, right, long_strike, entry_time)
    if short_q is None or long_q is None:
        return None

    credit = short_q["bid"] - long_q["ask"]
    if credit < MIN_CREDIT:
        return None

    # Equity-based risk with cooldown multiplier
    risk_amount = equity_risk(credit, equity) * size_mult
    max_loss_per = (SPREAD_WIDTH - credit) * 100.0
    if max_loss_per <= 0:
        return None
    qty = max(1, int(risk_amount / max_loss_per))

    # Settlement
    if right == "C":
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = SPREAD_WIDTH
        else:
            settlement = spx_exit - short_strike
    else:
        if spx_exit >= short_strike:
            settlement = 0.0
        elif spx_exit <= long_strike:
            settlement = SPREAD_WIDTH
        else:
            settlement = short_strike - spx_exit

    gross = (credit - settlement) * qty * 100.0
    comm = 4 * qty * COMMISSION
    net = gross - comm

    return {"side": right, "credit": credit, "pnl": net, "qty": qty,
            "short_strike": short_strike, "settlement": settlement}


# ── Cooldown rules ─────────────────────────────────────────────────────
class CooldownA:
    """No cooldown -- baseline."""
    name = "A_no_cooldown"
    desc = "Normal sizing every day (baseline)"

    def __init__(self):
        pass

    def get_multiplier(self):
        return 1.0

    def should_skip(self):
        return False

    def update(self, day_pnl: float, equity: float):
        pass


class CooldownB:
    """50% size for 1 day after loss."""
    name = "B_50pct_1day"
    desc = "50% size for 1 day after loss"

    def __init__(self):
        self.cooldown_days = 0

    def get_multiplier(self):
        return 0.5 if self.cooldown_days > 0 else 1.0

    def should_skip(self):
        return False

    def update(self, day_pnl: float, equity: float):
        if self.cooldown_days > 0:
            self.cooldown_days -= 1
        if day_pnl is not None and day_pnl < 0:
            self.cooldown_days = 1


class CooldownC:
    """50% size for 2 days after loss."""
    name = "C_50pct_2day"
    desc = "50% size for 2 days after loss"

    def __init__(self):
        self.cooldown_days = 0

    def get_multiplier(self):
        return 0.5 if self.cooldown_days > 0 else 1.0

    def should_skip(self):
        return False

    def update(self, day_pnl: float, equity: float):
        if self.cooldown_days > 0:
            self.cooldown_days -= 1
        if day_pnl is not None and day_pnl < 0:
            self.cooldown_days = 2


class CooldownD:
    """Skip next day entirely after any losing day."""
    name = "D_skip_1day"
    desc = "Skip next day entirely after any loss"

    def __init__(self):
        self.skip_next = False

    def get_multiplier(self):
        return 1.0

    def should_skip(self):
        return self.skip_next

    def update(self, day_pnl: float, equity: float):
        # day_pnl is None if we skipped (no trade)
        if day_pnl is None:
            self.skip_next = False
        elif day_pnl < 0:
            self.skip_next = True
        else:
            self.skip_next = False


class CooldownE:
    """Graduated: 1 loss -> 75%, 2 consec -> 50%, 3+ -> 25%. Reset on win."""
    name = "E_graduated"
    desc = "1 loss->75%, 2 consec->50%, 3+->25%, reset on win"

    def __init__(self):
        self.consec_losses = 0

    def get_multiplier(self):
        if self.consec_losses == 0:
            return 1.0
        elif self.consec_losses == 1:
            return 0.75
        elif self.consec_losses == 2:
            return 0.50
        else:
            return 0.25

    def should_skip(self):
        return False

    def update(self, day_pnl: float, equity: float):
        if day_pnl is not None and day_pnl < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0


class CooldownF:
    """Severity-based: loss >2% eq -> 50%/2d, >1% -> 75%/1d, else normal."""
    name = "F_severity"
    desc = "Loss >2% eq->50%/2d, >1%->75%/1d, else normal"

    def __init__(self):
        self.cooldown_days = 0
        self.cooldown_mult = 1.0

    def get_multiplier(self):
        return self.cooldown_mult if self.cooldown_days > 0 else 1.0

    def should_skip(self):
        return False

    def update(self, day_pnl: float, equity: float):
        if self.cooldown_days > 0:
            self.cooldown_days -= 1
        if day_pnl is not None and day_pnl < 0 and equity > 0:
            loss_pct = abs(day_pnl) / equity * 100
            if loss_pct > 2.0:
                self.cooldown_days = 2
                self.cooldown_mult = 0.50
            elif loss_pct > 1.0:
                self.cooldown_days = 1
                self.cooldown_mult = 0.75


ALL_COOLDOWNS = [CooldownA, CooldownB, CooldownC, CooldownD,
                 CooldownE, CooldownF]


# ── Run backtest with cooldown ──────────────────────────────────────────
def run_with_cooldown(day_data: list, cooldown_cls) -> dict:
    """Run full backtest with a specific cooldown rule.
    Returns stats dict.
    """
    cd = cooldown_cls()
    equity = STARTING_EQUITY
    equity_curve = [STARTING_EQUITY]
    peak = STARTING_EQUITY
    max_dd_pct = 0.0
    worst_dd_start = ""
    worst_dd_end = ""
    current_dd_start = ""
    trades = []
    day_pnls = []
    skipped_days = 0

    for dd in day_data:
        # Check if cooldown says skip
        if cd.should_skip():
            cd.update(None, equity)
            skipped_days += 1
            continue

        dist = dd["dist"]
        aft_ret = dd["afternoon_ret"]
        mom30 = dd["mom30"]
        day_pnl = 0.0
        day_traded = False
        size_mult = cd.get_multiplier()

        # CALL SIDE — skip if afternoon >= 1.0%
        skip_call = (aft_ret is not None and aft_ret >= 1.0)
        if not skip_call:
            ct = sim_side(dd["spot"], dd["spx_exit"], dist,
                          dd["opts_df"], "15:55:00", "C", equity, size_mult)
            if ct is not None:
                trades.append({"date": dd["date"], **ct})
                day_pnl += ct["pnl"]
                day_traded = True

        # PUT SIDE — skip if afternoon < -0.75% AND mom30 < -0.10%
        skip_put = (aft_ret is not None and mom30 is not None
                    and aft_ret < -0.75 and mom30 < -0.10)
        if not skip_put:
            pt = sim_side(dd["spot"], dd["spx_exit"], dist,
                          dd["opts_df"], "15:55:00", "P", equity, size_mult)
            if pt is not None:
                trades.append({"date": dd["date"], **pt})
                day_pnl += pt["pnl"]
                day_traded = True

        if day_traded:
            equity += day_pnl
            day_pnls.append(day_pnl)
            equity_curve.append(equity)

            if equity > peak:
                peak = equity
                current_dd_start = dd["date"]

            dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                worst_dd_start = current_dd_start
                worst_dd_end = dd["date"]

            cd.update(day_pnl, equity)
        else:
            cd.update(None, equity)

    # Compute stats
    n_trades = len(trades)
    if n_trades == 0:
        return {"name": cooldown_cls.name, "desc": cooldown_cls.desc,
                "trades": 0}

    trade_pnls = np.array([t["pnl"] for t in trades])
    n_wins = int((trade_pnls > 0).sum())
    wr = n_wins / n_trades * 100

    day_pnl_arr = np.array(day_pnls)
    total_pnl = equity - STARTING_EQUITY

    # Sharpe (daily)
    if len(day_pnl_arr) > 1 and day_pnl_arr.std() > 0:
        sharpe = day_pnl_arr.mean() / day_pnl_arr.std() * np.sqrt(252)
    else:
        sharpe = 0.0

    # Profit factor
    gross_profit = float(trade_pnls[trade_pnls > 0].sum())
    gross_loss = float(abs(trade_pnls[trade_pnls < 0].sum()))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max DD $
    eq_arr = np.array(equity_curve)
    peak_arr = np.maximum.accumulate(eq_arr)
    dd_dollars = (peak_arr - eq_arr).max()

    # Calmar
    n_years = len(day_pnl_arr) / 252
    if n_years > 0 and max_dd_pct > 0:
        ann_ret = total_pnl / n_years
        calmar = (ann_ret / STARTING_EQUITY * 100) / max_dd_pct
    else:
        calmar = 0.0

    # CAGR
    if n_years > 0 and equity > 0:
        cagr = (equity / STARTING_EQUITY) ** (1 / n_years) - 1
    else:
        cagr = 0.0

    # Worst drawdown period
    dd_period = f"{worst_dd_start}-{worst_dd_end}" if worst_dd_start else "N/A"

    n_calls = sum(1 for t in trades if t["side"] == "C")
    n_puts = sum(1 for t in trades if t["side"] == "P")

    return {
        "name": cooldown_cls.name,
        "desc": cooldown_cls.desc,
        "trades": n_trades,
        "calls": n_calls,
        "puts": n_puts,
        "final_equity": equity,
        "total_pnl": total_pnl,
        "max_dd_pct": max_dd_pct,
        "max_dd_dollars": dd_dollars,
        "wr": wr,
        "sharpe": sharpe,
        "calmar": calmar,
        "pf": pf,
        "cagr": cagr * 100,
        "worst_dd_period": dd_period,
        "skipped_days": skipped_days,
    }


# ── Main ────────────────────────────────────────────────────────────────
def main():
    print("=" * 110)
    print("Post-Loss Cooldown Research — Rule C3 + Equity Compounding (5% tiered)")
    print("Bidirectional, Entry 15:55, Exit 16:00, Width $5, Min Credit $0.10")
    print("Call filter: afternoon >= 1.0% (skip)")
    print("Put filter: afternoon < -0.75% AND mom30 < -0.10% (skip)")
    print(f"Starting equity: ${STARTING_EQUITY:,.0f}")
    print("=" * 110)

    # Collect all dates
    print("\nLoading trading dates...")
    all_spx = sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet")))
    all_dates = []
    for f in all_spx:
        ds = os.path.basename(f).replace(".parquet", "")
        if START_DATE <= ds <= END_DATE:
            all_dates.append(ds)
    all_dates = sorted(set(all_dates))
    print(f"  {len(all_dates)} candidate dates")

    # Pre-load day data
    print("Loading day data (SPX, VIX, options)...")
    day_data = []
    skipped = {"holiday": 0, "early_close": 0, "no_spx": 0, "no_vix": 0,
               "no_opts": 0, "vix_skip": 0}

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

        dist = rule_c3_dist(vix1550)
        if dist is None:
            skipped["vix_skip"] += 1
            continue

        aft_ret = compute_afternoon_return(spx_df)
        mom30 = compute_mom30(spx_df)

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
            "dist": dist,
            "afternoon_ret": aft_ret,
            "mom30": mom30,
            "opts_df": opts_df,
        })

    print(f"\n  Eligible trading days: {len(day_data)}")
    print(f"  Skipped: {skipped}")

    # ── Run each cooldown rule ─────────────────────────────────────────
    print("\n" + "=" * 110)
    print("Running cooldown variants...")
    print("=" * 110)

    results = []
    for cd_cls in ALL_COOLDOWNS:
        print(f"  Running {cd_cls.name}...")
        r = run_with_cooldown(day_data, cd_cls)
        results.append(r)

    # ── Comparison table ───────────────────────────────────────────────
    print("\n" + "=" * 130)
    print("COMPARISON TABLE — Post-Loss Cooldown Rules")
    print("=" * 130)
    print(f"{'Rule':<18} {'Trades':>7} {'Final Eq':>12} {'P&L':>12} {'MaxDD%':>8} "
          f"{'MaxDD$':>10} {'WR%':>7} {'Sharpe':>8} {'Calmar':>8} {'PF':>6} "
          f"{'CAGR':>7} {'Skip':>5}")
    print("-" * 130)

    baseline = results[0]  # A = no cooldown
    for r in results:
        if r["trades"] == 0:
            print(f"{r['name']:<18}  NO TRADES")
            continue
        print(
            f"{r['name']:<18} {r['trades']:>7} "
            f"${r['final_equity']:>10,.0f} ${r['total_pnl']:>10,.0f} "
            f"{r['max_dd_pct']:>7.2f}% ${r['max_dd_dollars']:>8,.0f} "
            f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} "
            f"{r['pf']:>6.2f} {r['cagr']:>6.1f}% "
            f"{r['skipped_days']:>5}"
        )

    # ── Delta vs baseline ──────────────────────────────────────────────
    print("\n" + "=" * 130)
    print("DELTA vs BASELINE (A_no_cooldown)")
    print("=" * 130)
    print(f"{'Rule':<18} {'dP&L':>12} {'dMaxDD%':>9} {'dSharpe':>9} "
          f"{'dCalmar':>9} {'dPF':>7} {'dTrades':>8} {'dWR':>7}")
    print("-" * 130)

    for r in results[1:]:
        if r["trades"] == 0:
            print(f"{r['name']:<18}  NO TRADES")
            continue
        dpnl = r["total_pnl"] - baseline["total_pnl"]
        ddd = r["max_dd_pct"] - baseline["max_dd_pct"]
        dsh = r["sharpe"] - baseline["sharpe"]
        dcal = r["calmar"] - baseline["calmar"]
        dpf = r["pf"] - baseline["pf"]
        dtr = r["trades"] - baseline["trades"]
        dwr = r["wr"] - baseline["wr"]
        print(
            f"{r['name']:<18} ${dpnl:>+10,.0f} {ddd:>+8.2f}% {dsh:>+9.2f} "
            f"{dcal:>+9.2f} {dpf:>+7.2f} {dtr:>+8d} {dwr:>+6.1f}%"
        )

    # ── Worst drawdown periods ─────────────────────────────────────────
    print("\n" + "=" * 130)
    print("WORST DRAWDOWN PERIODS")
    print("=" * 130)
    for r in results:
        if r["trades"] == 0:
            continue
        print(f"  {r['name']:<18}: MaxDD {r['max_dd_pct']:.2f}% (${r['max_dd_dollars']:,.0f})  "
              f"Period: {r['worst_dd_period']}")

    # ── Descriptions ───────────────────────────────────────────────────
    print("\n" + "=" * 130)
    print("RULE DESCRIPTIONS")
    print("=" * 130)
    for cd_cls in ALL_COOLDOWNS:
        print(f"  {cd_cls.name:<18}: {cd_cls.desc}")

    print("\n" + "=" * 130)
    print("ANALYSIS COMPLETE")
    print("=" * 130)


if __name__ == "__main__":
    main()
