#!/usr/bin/env python3
"""
Research: Equity-Based Compounding for LastFive Strategy
========================================================
Compare fixed-dollar sizing vs equity-percentage sizing modes.

Base config: FINAL (C3 VIX-adaptive + tiered credit + stacked put filter)
Starting equity: $50,000

Sizing modes:
  A: Current tiered fixed-dollar
  B: 5% of equity tiered
  C: 7% of equity tiered
  D: 10% of equity flat
  E: 25% Kelly fraction (recalc every 50 trades)
"""

import pandas as pd
import numpy as np
import math
import os
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

BASE = "/Users/jeerapongwongchote/Documents/thetadata"
DATA = os.path.join(BASE, "data")

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

START_DATE = "2022-01-03"
END_DATE = "2026-03-25"

COMMISSION_PER_LEG = 0.50
WIDTH = 5
MIN_CREDIT = 0.10
STARTING_EQUITY = 50_000


# ─── helpers ───────────────────────────────────────────────────────────────────

def trading_dates(start, end):
    dates = []
    d = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    while d <= e:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in HOLIDAYS and ds not in EARLY_CLOSE:
            dates.append(ds)
        d += timedelta(days=1)
    return dates


def load_parquet(year, subdir, date_str):
    path = os.path.join(DATA, str(year), subdir, f"{date_str}.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)
    return None


def get_bar(df, hhmm):
    mask = df["timestamp"].str.contains(f"T{hhmm}:")
    rows = df[mask]
    if len(rows) == 0:
        return None
    return rows.iloc[0]


def get_spot_at(spx_df, hhmm):
    bar = get_bar(spx_df, hhmm)
    return float(bar["close"]) if bar is not None else None


def get_vix_at(vix_df, hhmm):
    bar = get_bar(vix_df, hhmm)
    return float(bar["close"]) if bar is not None else None


def compute_afternoon_return(spx_df):
    hhmm_series = spx_df["timestamp"].str[11:16]
    afternoon = spx_df[(hhmm_series >= "13:00") & (hhmm_series <= "15:54")]
    if len(afternoon) < 2:
        return None
    first_close = float(afternoon.iloc[0]["close"])
    last_close = float(afternoon.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def compute_mom_30(spx_df):
    hhmm_series = spx_df["timestamp"].str[11:16]
    afternoon = spx_df[(hhmm_series >= "15:25") & (hhmm_series <= "15:54")]
    if len(afternoon) < 2:
        return None
    first_close = float(afternoon.iloc[0]["close"])
    last_close = float(afternoon.iloc[-1]["close"])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close * 100


def get_quote(quotes_df, right, strike, hhmm):
    mask = (quotes_df["right"] == right) & \
           (quotes_df["strike"] == strike) & \
           (quotes_df["timestamp"].str.contains(f"T{hhmm}:"))
    rows = quotes_df[mask]
    if len(rows) == 0:
        return None
    row = rows.iloc[0]
    bid = float(row["bid"])
    ask = float(row["ask"])
    mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid}


def compute_credit(quotes_df, right, short_strike, long_strike, hhmm):
    short_q = get_quote(quotes_df, right, short_strike, hhmm)
    long_q = get_quote(quotes_df, right, long_strike, hhmm)
    if short_q is None or long_q is None:
        return None, None, None
    credit = short_q["bid"] - long_q["ask"]
    return credit, short_q, long_q


def compute_settlement_pnl(quotes_df, right, short_strike, long_strike, entry_credit):
    for try_hhmm in ["16:00", "15:59", "15:58"]:
        short_q = get_quote(quotes_df, right, short_strike, try_hhmm)
        long_q = get_quote(quotes_df, right, long_strike, try_hhmm)
        if short_q is not None and long_q is not None:
            exit_debit = short_q["mid"] - long_q["mid"]
            if exit_debit < 0:
                exit_debit = 0
            pnl = entry_credit - exit_debit
            return pnl, exit_debit
    return None, None


# ─── VIX distance ────────────────────────────────────────────────────────────

def get_distance(vix):
    if vix is None:
        return None
    if vix < 18:
        return 0
    elif vix < 22:
        return 3
    elif vix <= 26:
        return 5
    else:
        return None  # skip VIX > 26


# ─── filters ─────────────────────────────────────────────────────────────────

def should_skip_call(afternoon_ret):
    if afternoon_ret is not None and afternoon_ret >= 1.0:
        return True
    return False


def should_skip_put(afternoon_ret, mom_30):
    if afternoon_ret is not None and mom_30 is not None:
        if afternoon_ret < -0.75 and mom_30 < -0.10:
            return True
    return False


# ─── sizing modes ────────────────────────────────────────────────────────────

class SizingMode:
    """Base class for position sizing."""
    def __init__(self, name):
        self.name = name

    def get_risk_amount(self, credit, equity, trade_history):
        raise NotImplementedError


class FixedTiered(SizingMode):
    """A: Current tiered fixed dollar sizing."""
    def __init__(self):
        super().__init__("A: Fixed Tiered $")

    def get_risk_amount(self, credit, equity, trade_history):
        if credit < 0.20:
            return 1500
        elif credit <= 0.50:
            return 2500
        else:
            return 3500


class EquityTiered5(SizingMode):
    """B: 5% of equity, tiered by credit."""
    def __init__(self):
        super().__init__("B: 5% Eq Tiered")

    def get_risk_amount(self, credit, equity, trade_history):
        if credit < 0.20:
            return equity * 0.03
        elif credit <= 0.50:
            return equity * 0.05
        else:
            return equity * 0.07


class EquityTiered7(SizingMode):
    """C: 7% of equity, tiered by credit."""
    def __init__(self):
        super().__init__("C: 7% Eq Tiered")

    def get_risk_amount(self, credit, equity, trade_history):
        if credit < 0.20:
            return equity * 0.04
        elif credit <= 0.50:
            return equity * 0.07
        else:
            return equity * 0.10


class EquityFlat10(SizingMode):
    """D: 10% of equity flat."""
    def __init__(self):
        super().__init__("D: 10% Eq Flat")

    def get_risk_amount(self, credit, equity, trade_history):
        return equity * 0.10


class KellyFraction(SizingMode):
    """E: 25% Kelly fraction, recalc every 50 trades."""
    def __init__(self):
        super().__init__("E: 25% Kelly")
        self.kelly_pct = 0.05  # default 5% until we have data
        self.last_recalc = 0
        self.warmup = 100
        self.recalc_interval = 50

    def _recalc_kelly(self, trade_history):
        if len(trade_history) < self.warmup:
            return
        # Check if we need to recalculate
        n = len(trade_history)
        if n < self.warmup:
            return
        # Recalc at warmup, then every recalc_interval trades
        if n == self.warmup or (n > self.warmup and (n - self.warmup) % self.recalc_interval == 0):
            wins = [t for t in trade_history if t > 0]
            losses = [t for t in trade_history if t <= 0]
            if len(wins) == 0 or len(losses) == 0:
                return
            wr = len(wins) / len(trade_history)
            avg_win = np.mean(wins)
            avg_loss = abs(np.mean(losses))
            if avg_loss == 0:
                return
            win_loss_ratio = avg_win / avg_loss
            kelly = wr - (1 - wr) / win_loss_ratio
            kelly = max(kelly, 0.01)  # floor at 1%
            self.kelly_pct = kelly * 0.25  # 25% Kelly
            self.last_recalc = n

    def get_risk_amount(self, credit, equity, trade_history):
        self._recalc_kelly(trade_history)
        return equity * self.kelly_pct


# ─── main backtest engine ───────────────────────────────────────────────────

def run_backtest(sizing_mode, dates):
    """Run backtest with given sizing mode, tracking equity curve."""
    trades = []
    trade_pnls = []  # for Kelly calculation
    equity = STARTING_EQUITY
    peak = STARTING_EQUITY
    max_dd_pct = 0
    worst_day_loss_pct = 0  # worst single-day loss as % of equity at the time

    for date_str in dates:
        year = date_str[:4]

        # Load data
        spx_df = load_parquet(year, "spx_ohlc", date_str)
        vix_df = load_parquet(year, "vix_ohlc", date_str)
        quotes_df = load_parquet(year, "option_quotes", date_str)

        if spx_df is None or vix_df is None or quotes_df is None:
            continue

        # Get VIX at 15:50
        vix_1550 = get_vix_at(vix_df, "15:50")

        # Get distance
        dist = get_distance(vix_1550)
        if dist is None:
            continue

        # Get SPX spot at 15:55
        spot = get_spot_at(spx_df, "15:55")
        if spot is None:
            spot = get_spot_at(spx_df, "15:54")
        if spot is None:
            continue

        # Compute filters
        afternoon_ret = compute_afternoon_return(spx_df)
        mom_30 = compute_mom_30(spx_df)

        equity_at_start_of_day = equity
        day_pnl = 0

        # ── CALL SIDE ──
        skip_call = should_skip_call(afternoon_ret)
        if not skip_call:
            call_short = math.ceil((spot + dist) / 5) * 5
            call_long = call_short + WIDTH

            credit_call, _, _ = compute_credit(quotes_df, "C", call_short, call_long, "15:55")
            if credit_call is not None and credit_call >= MIN_CREDIT:
                risk_amount = sizing_mode.get_risk_amount(credit_call, equity, trade_pnls)
                max_loss_per = (WIDTH - credit_call) * 100
                if max_loss_per > 0:
                    num_contracts = max(1, int(risk_amount / max_loss_per))
                else:
                    num_contracts = 1

                pnl_per, exit_debit = compute_settlement_pnl(
                    quotes_df, "C", call_short, call_long, credit_call)

                if pnl_per is not None:
                    total_pnl = pnl_per * 100 * num_contracts - 2 * COMMISSION_PER_LEG * num_contracts
                    equity += total_pnl
                    day_pnl += total_pnl
                    trade_pnls.append(total_pnl)

                    peak = max(peak, equity)
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    max_dd_pct = max(max_dd_pct, dd)

                    trades.append({
                        "date": date_str, "side": "CALL",
                        "credit": credit_call, "contracts": num_contracts,
                        "pnl_per": pnl_per, "total_pnl": total_pnl,
                        "equity": equity,
                    })

        # ── PUT SIDE ──
        skip_put = should_skip_put(afternoon_ret, mom_30)
        if not skip_put:
            put_short = math.floor((spot - dist) / 5) * 5
            put_long = put_short - WIDTH

            credit_put, _, _ = compute_credit(quotes_df, "P", put_short, put_long, "15:55")
            if credit_put is not None and credit_put >= MIN_CREDIT:
                risk_amount = sizing_mode.get_risk_amount(credit_put, equity, trade_pnls)
                max_loss_per = (WIDTH - credit_put) * 100
                if max_loss_per > 0:
                    num_contracts = max(1, int(risk_amount / max_loss_per))
                else:
                    num_contracts = 1

                pnl_per, exit_debit = compute_settlement_pnl(
                    quotes_df, "P", put_short, put_long, credit_put)

                if pnl_per is not None:
                    total_pnl = pnl_per * 100 * num_contracts - 2 * COMMISSION_PER_LEG * num_contracts
                    equity += total_pnl
                    day_pnl += total_pnl
                    trade_pnls.append(total_pnl)

                    peak = max(peak, equity)
                    dd = (peak - equity) / peak * 100 if peak > 0 else 0
                    max_dd_pct = max(max_dd_pct, dd)

                    trades.append({
                        "date": date_str, "side": "PUT",
                        "credit": credit_put, "contracts": num_contracts,
                        "pnl_per": pnl_per, "total_pnl": total_pnl,
                        "equity": equity,
                    })

        # Track worst single-day loss as % of equity
        if day_pnl < 0 and equity_at_start_of_day > 0:
            loss_pct = abs(day_pnl) / equity_at_start_of_day * 100
            worst_day_loss_pct = max(worst_day_loss_pct, loss_pct)

    return trades, equity, max_dd_pct, worst_day_loss_pct


def compute_stats(trades, final_equity, max_dd_pct, worst_day_loss_pct, label):
    if not trades:
        return {"label": label, "trades": 0}

    df = pd.DataFrame(trades)
    total_pnl = df["total_pnl"].sum()
    n_trades = len(df)
    wins = df[df["total_pnl"] > 0]
    wr = len(wins) / n_trades * 100 if n_trades > 0 else 0

    # Sharpe (daily returns)
    daily_pnl = df.groupby("date")["total_pnl"].sum()
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)
    else:
        sharpe = 0

    worst_trade = df["total_pnl"].min()

    return {
        "label": label,
        "trades": n_trades,
        "final_equity": final_equity,
        "pnl": total_pnl,
        "max_dd_pct": max_dd_pct,
        "wr": wr,
        "sharpe": sharpe,
        "worst_day_loss_pct": worst_day_loss_pct,
        "worst_trade": worst_trade,
    }


# ─── MAIN ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_dates = trading_dates(START_DATE, END_DATE)
    print(f"Trading dates: {len(all_dates)}  ({all_dates[0]} to {all_dates[-1]})")
    print(f"Starting equity: ${STARTING_EQUITY:,}")
    print()

    sizing_modes = [
        FixedTiered(),
        EquityTiered5(),
        EquityTiered7(),
        EquityFlat10(),
        KellyFraction(),
    ]

    results = []
    for sm in sizing_modes:
        print(f"Running {sm.name}...")
        trades, final_eq, max_dd, worst_day = run_backtest(sm, all_dates)
        stats = compute_stats(trades, final_eq, max_dd, worst_day, sm.name)
        results.append(stats)
        print(f"  -> Final equity: ${final_eq:,.0f}  |  P&L: ${stats['pnl']:,.0f}  |  MaxDD: {max_dd:.2f}%")

    print()
    print("=" * 110)
    print("COMPOUNDING COMPARISON TABLE")
    print("=" * 110)
    print(f"  {'Mode':<20} {'Final Eq':>12} {'Total P&L':>12} {'MaxDD%':>8} {'WR':>7} "
          f"{'Trades':>7} {'Sharpe':>8} {'WorstDay%':>10} {'WorstTrade':>12}")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*8} {'-'*7} "
          f"{'-'*7} {'-'*8} {'-'*10} {'-'*12}")
    for s in results:
        if s["trades"] == 0:
            print(f"  {s['label']:<20} NO TRADES")
            continue
        print(f"  {s['label']:<20} ${s['final_equity']:>11,.0f} ${s['pnl']:>11,.0f} "
              f"{s['max_dd_pct']:>7.2f}% {s['wr']:>6.1f}% "
              f"{s['trades']:>7} {s['sharpe']:>8.2f} "
              f"{s['worst_day_loss_pct']:>9.2f}% ${s['worst_trade']:>11,.0f}")

    print()
    print("=" * 110)

    # Also print Kelly details
    for sm in sizing_modes:
        if isinstance(sm, KellyFraction):
            print(f"\nKelly details: final kelly_pct = {sm.kelly_pct:.4f} ({sm.kelly_pct*100:.2f}% of equity per trade)")
            print(f"  Full Kelly would be {sm.kelly_pct/0.25:.4f} ({sm.kelly_pct/0.25*100:.2f}%)")

    print("\nDone.")
