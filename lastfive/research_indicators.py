#!/usr/bin/env python3
"""Research indicators to filter losing LastFive trades.

Builds a feature-rich dataset for every trade at 15:55/dist=3, then
analyzes which features distinguish winning days from losing days.

Features computed (all available before 15:55 — no look-ahead):
  - VIX close (T-1, prior day)
  - VIX intraday at 15:50 (same-day, already happened)
  - SPX day range (high - low) up to 15:54
  - SPX intraday return (open to 15:54)
  - SPX momentum: last 5/10/15 min return before entry
  - SPX distance from session high/low
  - Day of week
  - SPX EMA trend at 15:50 (using 1-min bars)
  - Prior-day SPX return
  - VIX change (T-1 to T intraday at 15:50)
  - Intraday range percentile (how wide is today vs recent days)
"""
from __future__ import annotations

import glob
import math
import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

MARKET_HOLIDAYS = {
    "20220117", "20220221", "20220415", "20220530", "20220620", "20220704",
    "20220905", "20221124", "20221226",
    "20230102", "20230116", "20230220", "20230407", "20230529", "20230619",
    "20230704", "20230904", "20231123", "20231225",
    "20240101", "20240115", "20240219", "20240329", "20240527", "20240619",
    "20240704", "20240902", "20241128", "20241225",
    "20250101", "20250120", "20250217", "20250418", "20250526", "20250704",
    "20250901", "20251127", "20251225",
    "20260101", "20260119", "20260216", "20260403", "20260525", "20260703",
    "20260907", "20261126", "20261225",
}
EARLY_CLOSE_DAYS = {
    "20221125", "20231124", "20241129", "20241224",
    "20251128", "20251224", "20261127", "20261224",
}

SPREAD_WIDTH = 5.0
RISK_AMOUNT = 2500.0
COMMISSION = 0.50
MIN_SHORT_DISTANCE = 3
ENTRY_TIME = "15:55:00"


def build_trade_dataset():
    """Build feature-rich dataset of all trades."""
    print("Loading SPX data...")
    spx_cache = {}
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "spx_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        if date_str < "20220103" or date_str > "20260325":
            continue
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        try:
            df = pd.read_parquet(fpath)
            if not df.empty:
                df["time_str"] = df["timestamp"].str[11:19]
                df["hhmm"] = df["timestamp"].str[11:16]
                spx_cache[date_str] = df
        except Exception:
            pass

    print("Loading option quotes...")
    quotes_cache = {}
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "option_quotes", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        if date_str < "20220103" or date_str > "20260325":
            continue
        if date_str in MARKET_HOLIDAYS or date_str in EARLY_CLOSE_DAYS:
            continue
        try:
            df = pd.read_parquet(fpath)
            if not df.empty:
                df["time_str"] = df["timestamp"].str[11:19]
                quotes_cache[date_str] = df
        except Exception:
            pass

    print("Loading VIX data...")
    # VIX daily
    vix_daily = {}
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix", "*.parquet"))):
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty and "vix_close" in vdf.columns:
                vix_daily[str(vdf["date"].iloc[0])] = float(vdf["vix_close"].iloc[0])
        except Exception:
            pass

    # VIX intraday
    vix_intraday = {}
    for fpath in sorted(glob.glob(os.path.join(DATA_DIR, "*", "vix_ohlc", "*.parquet"))):
        date_str = os.path.basename(fpath).replace(".parquet", "")
        try:
            vdf = pd.read_parquet(fpath)
            if not vdf.empty:
                vdf["time_str"] = vdf["timestamp"].str[11:19]
                vdf["hhmm"] = vdf["timestamp"].str[11:16]
                vix_intraday[date_str] = vdf
        except Exception:
            pass

    trade_dates = sorted(set(spx_cache.keys()) & set(quotes_cache.keys()))
    # Build sorted date list for prior-day lookups
    all_spx_dates = sorted(spx_cache.keys())
    date_idx = {d: i for i, d in enumerate(all_spx_dates)}

    print(f"Building features for {len(trade_dates)} dates...")
    rows = []

    for date_str in trade_dates:
        spx_df = spx_cache[date_str]
        quotes_df = quotes_cache[date_str]

        # SPX at entry
        bar = spx_df[spx_df["time_str"] == ENTRY_TIME]
        if bar.empty:
            continue
        spot = float(bar["close"].iloc[0])

        # Strike selection
        target = spot + MIN_SHORT_DISTANCE
        short_strike = int(math.ceil(target / 5) * 5)
        long_strike = short_strike + int(SPREAD_WIDTH)

        # Quotes
        entry_calls = quotes_df[(quotes_df["right"] == "C") & (quotes_df["time_str"] == ENTRY_TIME)]
        short_q = entry_calls[entry_calls["strike"] == short_strike]
        long_q = entry_calls[entry_calls["strike"] == long_strike]
        if short_q.empty or long_q.empty:
            continue
        credit = float(short_q["bid"].iloc[0]) - float(long_q["ask"].iloc[0])
        if credit <= 0:
            continue

        # Qty
        max_loss_per = (SPREAD_WIDTH - credit) * 100.0
        qty = max(1, int(RISK_AMOUNT / max_loss_per)) if max_loss_per > 0 else 1

        # SPX at exit
        exit_bar = spx_df[spx_df["time_str"] == "16:00:00"]
        if exit_bar.empty:
            exit_bar = spx_df[spx_df["time_str"] == "15:59:00"]
        spx_exit = float(exit_bar["close"].iloc[0]) if not exit_bar.empty else float(spx_df["close"].iloc[-1])

        # Settlement + P&L
        if spx_exit <= short_strike:
            settlement = 0.0
        elif spx_exit >= long_strike:
            settlement = SPREAD_WIDTH
        else:
            settlement = spx_exit - short_strike
        net_pnl = (credit - settlement) * qty * 100.0 - 4 * qty * COMMISSION
        win = 1 if net_pnl > 0 else 0

        # ========== FEATURES (all available before 15:55) ==========

        # Bars up to 15:54
        pre_entry = spx_df[spx_df["time_str"] < ENTRY_TIME]
        if pre_entry.empty:
            continue

        day_open = float(spx_df[spx_df["hhmm"] == "09:30"]["open"].iloc[0]) if not spx_df[spx_df["hhmm"] == "09:30"].empty else float(pre_entry["open"].iloc[0])
        session_high = float(pre_entry["high"].max())
        session_low = float(pre_entry["low"].min())
        last_close = float(pre_entry["close"].iloc[-1])  # 15:54 close

        # Day range
        day_range = session_high - session_low
        day_range_pct = day_range / day_open * 100 if day_open else 0

        # Intraday return (open to 15:54)
        intraday_return_pct = (last_close - day_open) / day_open * 100 if day_open else 0

        # Distance from session high/low
        dist_from_high = (last_close - session_high) / day_open * 100 if day_open else 0  # negative = below high
        dist_from_low = (last_close - session_low) / day_open * 100 if day_open else 0    # positive = above low

        # Momentum: last N minutes return
        def last_n_min_return(n):
            recent = pre_entry.tail(n)
            if len(recent) < n:
                return None
            return (float(recent["close"].iloc[-1]) - float(recent["close"].iloc[0])) / float(recent["close"].iloc[0]) * 100

        mom_5 = last_n_min_return(5)
        mom_10 = last_n_min_return(10)
        mom_15 = last_n_min_return(15)
        mom_30 = last_n_min_return(30)
        mom_60 = last_n_min_return(60)

        # Is SPX near session high? (within 0.05%)
        near_high = 1 if (session_high - last_close) / day_open * 100 < 0.05 else 0

        # Day of week (0=Mon, 4=Fri)
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y%m%d")
        dow = dt.weekday()

        # VIX (prior day close for no look-ahead)
        idx = date_idx.get(date_str, 0)
        prev_date = all_spx_dates[idx - 1] if idx > 0 else None
        vix_prev_close = vix_daily.get(prev_date) if prev_date else None

        # VIX intraday at 15:50 (same day, already happened)
        vix_1550 = None
        if date_str in vix_intraday:
            vix_df = vix_intraday[date_str]
            vix_bar = vix_df[vix_df["hhmm"] == "15:50"]
            if not vix_bar.empty:
                vix_1550 = float(vix_bar["close"].iloc[0])

        # VIX intraday change (open to 15:50)
        vix_intraday_chg = None
        if date_str in vix_intraday and vix_1550 is not None:
            vix_df = vix_intraday[date_str]
            vix_open_bar = vix_df[vix_df["hhmm"] == "09:30"]
            if not vix_open_bar.empty:
                vix_open = float(vix_open_bar["close"].iloc[0])
                if vix_open > 0:
                    vix_intraday_chg = (vix_1550 - vix_open) / vix_open * 100

        # Prior-day SPX return
        prior_day_return = None
        if prev_date and prev_date in spx_cache:
            prev_df = spx_cache[prev_date]
            if not prev_df.empty:
                prev_open = float(prev_df["open"].iloc[0])
                prev_close = float(prev_df["close"].iloc[-1])
                if prev_open > 0:
                    prior_day_return = (prev_close - prev_open) / prev_open * 100

        # SPX EMA at 15:54 (fast=14, slow=50 on 1-min bars)
        closes_list = pre_entry["close"].tolist()
        def ema(data, window):
            if len(data) < window:
                return None
            alpha = 2 / (window + 1)
            e = data[0]
            for p in data[1:]:
                e = p * alpha + e * (1 - alpha)
            return e

        ema_14 = ema(closes_list, 14)
        ema_50 = ema(closes_list, 50)
        ema_bullish = 1 if (ema_14 and ema_50 and ema_14 > ema_50) else 0

        # Afternoon trend (return from 13:00 to 15:54)
        afternoon = pre_entry[pre_entry["hhmm"] >= "13:00"]
        afternoon_return = None
        if len(afternoon) >= 2:
            afternoon_return = (float(afternoon["close"].iloc[-1]) - float(afternoon["close"].iloc[0])) / float(afternoon["close"].iloc[0]) * 100

        # Last-hour trend (14:55 to 15:54)
        last_hour = pre_entry[pre_entry["hhmm"] >= "14:55"]
        last_hour_return = None
        if len(last_hour) >= 2:
            last_hour_return = (float(last_hour["close"].iloc[-1]) - float(last_hour["close"].iloc[0])) / float(last_hour["close"].iloc[0]) * 100

        rows.append({
            "date": date_str,
            "spot": round(spot, 2),
            "spx_exit": round(spx_exit, 2),
            "short_strike": short_strike,
            "credit": round(credit, 4),
            "qty": qty,
            "pnl": round(net_pnl, 2),
            "win": win,
            # Features
            "vix_prev_close": round(vix_prev_close, 2) if vix_prev_close else None,
            "vix_1550": round(vix_1550, 2) if vix_1550 else None,
            "vix_intraday_chg": round(vix_intraday_chg, 2) if vix_intraday_chg is not None else None,
            "day_range_pct": round(day_range_pct, 3),
            "intraday_return_pct": round(intraday_return_pct, 3),
            "dist_from_high_pct": round(dist_from_high, 3),
            "dist_from_low_pct": round(dist_from_low, 3),
            "mom_5": round(mom_5, 4) if mom_5 is not None else None,
            "mom_10": round(mom_10, 4) if mom_10 is not None else None,
            "mom_15": round(mom_15, 4) if mom_15 is not None else None,
            "mom_30": round(mom_30, 4) if mom_30 is not None else None,
            "mom_60": round(mom_60, 4) if mom_60 is not None else None,
            "near_high": near_high,
            "dow": dow,
            "prior_day_return": round(prior_day_return, 3) if prior_day_return is not None else None,
            "ema_bullish": ema_bullish,
            "afternoon_return": round(afternoon_return, 4) if afternoon_return is not None else None,
            "last_hour_return": round(last_hour_return, 4) if last_hour_return is not None else None,
        })

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(os.path.dirname(__file__), "trade_features.csv"), index=False)
    print(f"\nDataset: {len(df)} trades, {df['win'].sum()} wins, {len(df) - df['win'].sum()} losses")
    return df


def analyze_features(df: pd.DataFrame):
    """Compare feature distributions between wins and losses."""
    wins = df[df["win"] == 1]
    losses = df[df["win"] == 0]

    print(f"\n{'='*80}")
    print(f"FEATURE ANALYSIS: {len(wins)} wins vs {len(losses)} losses")
    print(f"{'='*80}")

    features = [
        "vix_prev_close", "vix_1550", "vix_intraday_chg",
        "day_range_pct", "intraday_return_pct",
        "dist_from_high_pct", "dist_from_low_pct",
        "mom_5", "mom_10", "mom_15", "mom_30", "mom_60",
        "near_high", "dow", "prior_day_return", "ema_bullish",
        "afternoon_return", "last_hour_return", "credit",
    ]

    print(f"\n{'Feature':<22} | {'Win Mean':>10} | {'Loss Mean':>10} | {'Win Med':>10} | {'Loss Med':>10} | {'Separation':>10}")
    print("-" * 90)

    separations = {}
    for feat in features:
        if feat not in df.columns:
            continue
        w = wins[feat].dropna()
        l = losses[feat].dropna()
        if len(w) == 0 or len(l) == 0:
            continue

        w_mean = w.mean()
        l_mean = l.mean()
        w_med = w.median()
        l_med = l.median()

        # Separation score: (mean_loss - mean_win) / pooled_std
        pooled_std = pd.concat([w, l]).std()
        sep = abs(l_mean - w_mean) / pooled_std if pooled_std > 0 else 0
        separations[feat] = sep

        print(f"{feat:<22} | {w_mean:>10.4f} | {l_mean:>10.4f} | {w_med:>10.4f} | {l_med:>10.4f} | {sep:>10.3f}")

    # Day of week breakdown
    print(f"\n{'='*60}")
    print("DAY OF WEEK BREAKDOWN")
    print(f"{'='*60}")
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for d in range(5):
        day_trades = df[df["dow"] == d]
        n = len(day_trades)
        w = day_trades["win"].sum()
        pnl = day_trades["pnl"].sum()
        wr = w / n * 100 if n else 0
        print(f"  {dow_names[d]}: {n:>4} trades | WR={wr:>5.1f}% | P&L=${pnl:>8,.0f}")

    # Top separating features
    print(f"\n{'='*60}")
    print("TOP SEPARATING FEATURES (by effect size)")
    print(f"{'='*60}")
    for feat, sep in sorted(separations.items(), key=lambda x: -x[1])[:10]:
        w_mean = wins[feat].dropna().mean()
        l_mean = losses[feat].dropna().mean()
        direction = "HIGHER on losses" if l_mean > w_mean else "LOWER on losses"
        print(f"  {sep:.3f}  {feat:<22} — {direction} (win={w_mean:.4f}, loss={l_mean:.4f})")

    # Threshold analysis for top features
    print(f"\n{'='*60}")
    print("THRESHOLD ANALYSIS — Best single-feature filters")
    print(f"{'='*60}")

    best_filters = []
    for feat in features:
        if feat not in df.columns or feat == "dow" or feat == "near_high":
            continue
        vals = df[feat].dropna()
        if len(vals) < 20:
            continue

        # Try various percentile thresholds
        for direction in ["skip_above", "skip_below"]:
            for pct in [10, 20, 25, 30, 40, 50]:
                if direction == "skip_above":
                    thresh = vals.quantile(pct / 100)
                    kept = df[df[feat] <= thresh]
                    skipped = df[df[feat] > thresh]
                else:
                    thresh = vals.quantile(1 - pct / 100)
                    kept = df[df[feat] >= thresh]
                    skipped = df[df[feat] < thresh]

                if len(kept) < 20 or len(skipped) < 5:
                    continue

                kept_wr = kept["win"].mean() * 100
                kept_pnl = kept["pnl"].sum()
                base_pnl = df["pnl"].sum()
                skipped_losses = len(skipped) - skipped["win"].sum()
                skipped_wins = skipped["win"].sum()

                # Only interesting if it improves P&L and WR
                if kept_pnl > base_pnl and kept_wr > df["win"].mean() * 100:
                    best_filters.append({
                        "feature": feat,
                        "direction": direction,
                        "percentile": pct,
                        "threshold": round(thresh, 4),
                        "kept_trades": len(kept),
                        "kept_wr": round(kept_wr, 1),
                        "kept_pnl": round(kept_pnl),
                        "skipped_losses": int(skipped_losses),
                        "skipped_wins": int(skipped_wins),
                        "pnl_improvement": round(kept_pnl - base_pnl),
                    })

    best_filters.sort(key=lambda x: -x["pnl_improvement"])
    print(f"\n{'Feature':<22} | {'Filter':<14} | {'Thresh':>8} | {'Kept':>5} | {'WR':>6} | {'P&L':>9} | {'Δ P&L':>8} | {'Skip L/W':>8}")
    print("-" * 105)
    seen = set()
    for f in best_filters[:25]:
        key = (f["feature"], f["direction"])
        if key in seen:
            continue
        seen.add(key)
        print(f"{f['feature']:<22} | {f['direction']:<14} | {f['threshold']:>8.4f} | {f['kept_trades']:>5} | {f['kept_wr']:>5.1f}% | ${f['kept_pnl']:>8,} | ${f['pnl_improvement']:>7,} | {f['skipped_losses']}L/{f['skipped_wins']}W")

    # Loss day deep dive
    print(f"\n{'='*60}")
    print("LOSING TRADES — FULL DETAIL")
    print(f"{'='*60}")
    loss_df = df[df["win"] == 0].sort_values("pnl")
    for _, row in loss_df.iterrows():
        d = row["date"]
        print(f"\n  {d[:4]}-{d[4:6]}-{d[6:]} | SPX={row['spot']:.0f}->{row['spx_exit']:.0f} (+{row['spx_exit']-row['spot']:.1f}) | "
              f"C{row['short_strike']} | cr=${row['credit']:.2f} | P&L=${row['pnl']:,.0f}")
        print(f"    VIX(T-1)={row.get('vix_prev_close','?')} | VIX(15:50)={row.get('vix_1550','?')} | "
              f"Range={row['day_range_pct']:.2f}% | IntraRet={row['intraday_return_pct']:.3f}%")
        print(f"    Mom5={row.get('mom_5','?')} | Mom10={row.get('mom_10','?')} | Mom30={row.get('mom_30','?')} | "
              f"Afternoon={row.get('afternoon_return','?')} | LastHr={row.get('last_hour_return','?')}")
        print(f"    NearHigh={row['near_high']} | EMA_bull={row['ema_bullish']} | DOW={row['dow']}")


if __name__ == "__main__":
    df = build_trade_dataset()
    analyze_features(df)
