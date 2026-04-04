"""Research: Spread Width Comparison ($5, $10, $15, $20) for LastFive strategy.

Tests whether wider spreads improve risk-adjusted returns.
Wider spreads collect more credit but risk more per contract.
"""

import math
import os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ── Config ──
DATA_DIR = "/Users/jeerapongwongchote/Documents/thetadata/data"
START_DATE = "2022-01-03"
END_DATE = "2026-03-25"
WIDTHS = [5, 10, 15, 20]

MIN_SHORT_DISTANCE = 3.0
RISK_AMOUNT = 2500.0
MIN_CREDIT = 0.10
COMMISSION_PER_LEG = 0.50
VIX_MAX = 26.0
AFTERNOON_RETURN_MAX = 1.0  # percent

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


def get_trading_dates():
    dates = pd.bdate_range(START_DATE, END_DATE)
    out = []
    for d in dates:
        ds = d.strftime("%Y%m%d")
        if ds not in MARKET_HOLIDAYS and ds not in EARLY_CLOSE_DAYS:
            out.append(ds)
    return out


def load_parquet(year, subdir, date_str):
    path = os.path.join(DATA_DIR, str(year), subdir, f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def run():
    dates = get_trading_dates()
    print(f"Trading dates: {len(dates)}")

    # Results per width
    trades_by_width = {w: [] for w in WIDTHS}

    for date_str in dates:
        year = int(date_str[:4])

        # Load SPX
        spx = load_parquet(year, "spx_ohlc", date_str)
        if spx is None:
            continue

        # Parse timestamps
        spx["ts"] = spx["timestamp"].str[11:19]  # HH:MM:SS

        # SPX at 15:55 (entry spot)
        row_1555 = spx[spx["ts"] == "15:55:00"]
        if row_1555.empty:
            continue
        spot = row_1555.iloc[0]["close"]

        # SPX at exit (15:59 bar close = 16:00 settlement proxy)
        row_exit = spx[spx["ts"] == "15:59:00"]
        if row_exit.empty:
            # Try 15:58
            row_exit = spx[spx["ts"] == "15:58:00"]
            if row_exit.empty:
                continue
        spx_exit = row_exit.iloc[0]["close"]

        # ── Filters ──
        # VIX at 15:50
        vix = load_parquet(year, "vix_ohlc", date_str)
        if vix is None:
            continue
        vix["ts"] = vix["timestamp"].str[11:19]
        row_vix = vix[vix["ts"] == "15:50:00"]
        if row_vix.empty:
            continue
        vix_level = row_vix.iloc[0]["close"]
        if vix_level > VIX_MAX:
            continue

        # Afternoon return: 13:00 -> 15:54
        row_1300 = spx[spx["ts"] == "13:00:00"]
        row_1554 = spx[spx["ts"] == "15:54:00"]
        if row_1300.empty or row_1554.empty:
            continue
        spx_1300 = row_1300.iloc[0]["close"]
        spx_1554 = row_1554.iloc[0]["close"]
        afternoon_ret = (spx_1554 - spx_1300) / spx_1300 * 100
        if afternoon_ret >= AFTERNOON_RETURN_MAX:
            continue

        # Load option quotes
        opts = load_parquet(year, "option_quotes", date_str)
        if opts is None:
            continue
        opts["ts"] = opts["timestamp"].str[11:19]

        # Filter to calls at 15:55
        calls_1555 = opts[(opts["right"] == "C") & (opts["ts"] == "15:55:00")]
        if calls_1555.empty:
            continue

        # Short strike: ceil((spot + 3) / 5) * 5
        short_strike = math.ceil((spot + MIN_SHORT_DISTANCE) / 5) * 5

        # Get short call quote
        short_row = calls_1555[calls_1555["strike"] == short_strike]
        if short_row.empty:
            continue
        short_bid = short_row.iloc[0]["bid"]

        # For each width, find long call and compute trade
        for width in WIDTHS:
            long_strike = short_strike + width
            long_row = calls_1555[calls_1555["strike"] == long_strike]
            if long_row.empty:
                continue
            long_ask = long_row.iloc[0]["ask"]

            credit = short_bid - long_ask
            if credit < MIN_CREDIT:
                continue

            max_loss_per_contract = (width - credit) * 100
            if max_loss_per_contract <= 0:
                continue
            qty = int(RISK_AMOUNT / max_loss_per_contract)
            if qty < 1:
                continue

            commission = 4 * qty * COMMISSION_PER_LEG

            # Settlement
            if spx_exit <= short_strike:
                spread_val = 0.0
            elif spx_exit >= long_strike:
                spread_val = width
            else:
                spread_val = spx_exit - short_strike

            pnl = (credit - spread_val) * qty * 100 - commission

            trades_by_width[width].append({
                "date": date_str,
                "spot": spot,
                "spx_exit": spx_exit,
                "short_strike": short_strike,
                "long_strike": long_strike,
                "credit": credit,
                "qty": qty,
                "spread_val": spread_val,
                "pnl": pnl,
                "commission": commission,
            })

    # ── Compute metrics ──
    print()
    print("=" * 90)
    print(f"{'Width':>6} | {'Trades':>6} | {'Total P&L':>10} | {'Max DD':>10} | {'WR':>6} | {'Sharpe':>7} | {'Avg Credit':>10} | {'PF':>6}")
    print("-" * 90)

    for width in WIDTHS:
        tlist = trades_by_width[width]
        if not tlist:
            print(f"${width:>4}  |      0 |        N/A |        N/A |    N/A |     N/A |        N/A |    N/A")
            continue

        df = pd.DataFrame(tlist)
        pnls = df["pnl"].values
        total_pnl = pnls.sum()
        n_trades = len(pnls)

        # Win rate
        wins = (pnls > 0).sum()
        wr = wins / n_trades * 100

        # Max drawdown
        equity = np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        dd = equity - peak
        max_dd = dd.min()

        # Sharpe (trade-level, annualized assuming ~252 trades/year)
        if pnls.std() > 0:
            sharpe = (pnls.mean() / pnls.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # Avg credit
        avg_credit = df["credit"].mean()

        # Profit factor
        gross_profit = pnls[pnls > 0].sum()
        gross_loss = abs(pnls[pnls < 0].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        print(f"${width:>4}  | {n_trades:>5} | ${total_pnl:>9,.0f} | ${max_dd:>9,.0f} | {wr:>5.1f}% | {sharpe:>7.2f} | ${avg_credit:>9.2f} | {pf:>5.2f}")

    print("=" * 90)


if __name__ == "__main__":
    run()
