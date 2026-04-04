#!/usr/bin/env python3
"""SimCondor — Iron Condor strategy driven by SimSearch range prediction.

Sells 0DTE SPX iron condors on days SimSearch predicts low range.
Strike distance adapts to predicted regime. No direction signal needed.

Logic:
  1. Before open: run SimSearch to predict today's range regime
  2. CALM/NORMAL → sell iron condor with strikes at predicted_range * multiplier
  3. VOLATILE/DANGER → skip
  4. Hold to expiry (0DTE) — exit at close or stop loss

Usage:
    python simcondor.py                     # full backtest
    python simcondor.py --date 20250310     # single day
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
import time
from datetime import time as dt_time

import numpy as np
import pandas as pd

# Add parent to path for data access
sys.path.insert(0, os.path.dirname(__file__))
from simsearch_config import DATA_DIR, LOGS_DIR, RUN_TS
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_regime import predict_regime, classify_range, DANGER_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ============================================================
#  Strategy Config
# ============================================================

# Entry
ENTRY_TIME = dt_time(9, 45)          # Enter at 9:45
WIDTH = 20                            # Spread width (20pt)
QTY = 2                               # Contracts per side
MODE = "both"                          # "both" = iron condor, "put" = put spread only

# Strike distance: OTM distance = predicted_range * SPX * multiplier
STRIKE_MULTIPLIER = 1.0               # Match predicted range (aim for OTM expiry)
MIN_OTM_DISTANCE = 30                 # Minimum 30pts OTM
MAX_OTM_DISTANCE = 100                # Maximum 100pts OTM

# Exit — NO stop loss (sweep proved it hurts on CALM days)
STOP_LOSS_PER_SIDE = -99999           # Disabled: hold to expiry
MIN_CREDIT_PER_SIDE = 0.40            # Min credit per side (0.40 = best risk-adjusted at scale)

# SimSearch
SIMSEARCH_TOP_K = 20
# Which regimes to trade
TRADE_REGIMES = {"CALM"}              # Only trade CALM — NORMAL loses money

# Additional filters — calibrated from Round 2 sweep (2026-04-04)
VIX_MAX = None                         # VIX<18: -$1.5k vs baseline; not worth the filter
VIX_MIN = None                         # No min VIX
DOW_SKIP = set()                       # Skip Fri: Sharpe +0.5 but -$2.7k P&L; marginal
PREV_RANGE_MAX = 0.008                 # GAME CHANGER: PrevRange<0.8% → Sharpe 11.58, 96% WR, PF 7.14
MIN_SIMILARITY = None                  # MinSim: no improvement
DANGER_PROB_MAX = None                 # DangerProb<5%: Sharpe 5.06 but loses trades
PUT_CALL_BIAS = 0.0                    # Asymmetric: hurts both ways

# Risk
COMMISSION = 0.50                     # Per contract per leg
STARTING_EQUITY = 50000

# Backtest range
BACKTEST_START = "2022-06-01"         # 6 months warmup for SimSearch
BACKTEST_END = "2026-03-25"


# ============================================================
#  Data Loaders
# ============================================================

def load_quotes(date_str: str) -> pd.DataFrame | None:
    """Load option quotes for a date."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "option_quotes", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return None


def load_spx_bar(date_str: str, target_time: dt_time) -> float | None:
    """Load SPX price at a specific time."""
    year = date_str[:4]
    path = os.path.join(DATA_DIR, year, "spx_ohlc", f"{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # Filter valid bars
        df = df[(df["open"] > 0) & (df["close"] > 0)]
        bar = df[df["timestamp"].dt.time == target_time]
        if len(bar) > 0:
            return float(bar.iloc[0]["close"])
        # Fallback: nearest bar
        df["time"] = df["timestamp"].dt.time
        nearest = df.iloc[(df["time"].apply(lambda t: abs(
            t.hour * 60 + t.minute - target_time.hour * 60 - target_time.minute
        ))).argmin()]
        return float(nearest["close"])
    except Exception:
        return None


def get_quote(quotes: pd.DataFrame, right: str, strike: float,
              target_time: dt_time) -> dict | None:
    """Get bid/ask for a specific option at a specific time."""
    mask = (
        (quotes["right"] == right) &
        (quotes["strike"] == strike) &
        (quotes["timestamp"].dt.time == target_time)
    )
    rows = quotes[mask]
    if len(rows) == 0:
        return None
    r = rows.iloc[0]
    return {"bid": float(r["bid"]), "ask": float(r["ask"]), "mid": float(r["mid"])}


# ============================================================
#  Iron Condor Logic
# ============================================================

def find_ic_strikes(spot: float, pred_range: float, regime: str) -> dict:
    """Calculate iron condor strike distances based on predicted range.

    Returns dict with put_short, put_long, call_short, call_long strikes.
    """
    # OTM distance: predicted range * spot * multiplier
    raw_distance = pred_range * spot * STRIKE_MULTIPLIER

    # Clamp to bounds
    otm_distance = max(MIN_OTM_DISTANCE, min(MAX_OTM_DISTANCE, raw_distance))

    # Round to nearest 5
    otm_distance = round(otm_distance / 5) * 5

    # Asymmetric bias: positive PUT_CALL_BIAS pushes puts further OTM
    put_otm = otm_distance + PUT_CALL_BIAS
    call_otm = otm_distance - PUT_CALL_BIAS
    put_otm = max(MIN_OTM_DISTANCE, put_otm)
    call_otm = max(MIN_OTM_DISTANCE, call_otm)

    # Strikes
    put_short = round((spot - put_otm) / 5) * 5
    put_long = put_short - WIDTH
    call_short = round((spot + call_otm) / 5) * 5
    call_long = call_short + WIDTH

    return {
        "put_short": put_short,
        "put_long": put_long,
        "call_short": call_short,
        "call_long": call_long,
        "otm_distance": otm_distance,
    }


def price_ic(quotes: pd.DataFrame, strikes: dict,
             entry_time: dt_time) -> dict | None:
    """Price an iron condor from option quotes.

    Returns credit received per contract (as premium, multiply by 100 for $).
    """
    ps = get_quote(quotes, "P", strikes["put_short"], entry_time)
    pl = get_quote(quotes, "P", strikes["put_long"], entry_time)
    cs = get_quote(quotes, "C", strikes["call_short"], entry_time)
    cl = get_quote(quotes, "C", strikes["call_long"], entry_time)

    if any(x is None for x in [ps, pl, cs, cl]):
        return None

    # Sell short strikes at bid, buy long strikes at ask
    put_credit = ps["bid"] - pl["ask"]
    call_credit = cs["bid"] - cl["ask"]

    if put_credit < 0 or call_credit < 0:
        return None

    return {
        "put_credit": put_credit,
        "call_credit": call_credit,
        "total_credit": put_credit + call_credit,
        "put_short_bid": ps["bid"],
        "put_long_ask": pl["ask"],
        "call_short_bid": cs["bid"],
        "call_long_ask": cl["ask"],
    }


def evaluate_ic_at_expiry(quotes: pd.DataFrame, strikes: dict,
                          spot_close: float) -> dict:
    """Evaluate iron condor at expiry (close).

    0DTE options expire at close. Value = intrinsic value only.
    """
    # Put spread intrinsic at expiry
    put_short_val = max(strikes["put_short"] - spot_close, 0)
    put_long_val = max(strikes["put_long"] - spot_close, 0)
    put_spread_val = put_short_val - put_long_val  # positive = we owe

    # Call spread intrinsic at expiry
    call_short_val = max(spot_close - strikes["call_short"], 0)
    call_long_val = max(spot_close - strikes["call_long"], 0)
    call_spread_val = call_short_val - call_long_val  # positive = we owe

    return {
        "put_spread_val": put_spread_val,
        "call_spread_val": call_spread_val,
        "total_exit_val": put_spread_val + call_spread_val,
    }


def check_intraday_sl(quotes: pd.DataFrame, strikes: dict,
                       entry_credit: float, entry_time: dt_time) -> dict | None:
    """Check if stop loss was hit intraday (sampled every 30 min for speed).

    Returns exit info if SL hit, None otherwise.
    """
    # Sample every 30 minutes instead of every minute
    check_times = []
    for h in range(entry_time.hour, 16):
        for m in (0, 30):
            t = dt_time(h, m)
            if t > entry_time:
                check_times.append(t)
    check_times.append(dt_time(15, 55))

    for tm in check_times:
        ps = get_quote(quotes, "P", strikes["put_short"], tm)
        pl = get_quote(quotes, "P", strikes["put_long"], tm)
        cs = get_quote(quotes, "C", strikes["call_short"], tm)
        cl = get_quote(quotes, "C", strikes["call_long"], tm)

        if any(x is None for x in [ps, pl, cs, cl]):
            continue

        put_spread_close = max(ps["ask"] - pl["bid"], 0)
        call_spread_close = max(cs["ask"] - cl["bid"], 0)
        total_close_cost = put_spread_close + call_spread_close
        total_pnl = (entry_credit - total_close_cost) * 100 * QTY

        if total_pnl <= STOP_LOSS_PER_SIDE * 2:
            return {
                "sl_time": tm,
                "sl_pnl": total_pnl,
                "put_spread_val": put_spread_close,
                "call_spread_val": call_spread_close,
            }

    return None


# ============================================================
#  Backtest
# ============================================================

def backtest_day(date_str: str, engine: SimSearchEngine,
                 pred_cache: dict) -> dict | None:
    """Backtest a single day."""
    # Step 1: SimSearch prediction (cached)
    if date_str not in pred_cache:
        pred = predict_regime(engine, date_str, top_k=SIMSEARCH_TOP_K)
        pred_cache[date_str] = pred
    pred = pred_cache[date_str]

    if "error" in pred:
        return None

    regime = pred["pred_bucket"]
    pred_range = pred["pred_range_weighted"]

    # Step 2: Trade decision
    if regime not in TRADE_REGIMES:
        return {"date": date_str, "action": "SKIP", "reason": f"regime={regime}",
                "pred_range": pred_range, "regime": regime}

    # Step 2b: Additional filters
    dt = pd.Timestamp(date_str)
    if DOW_SKIP and dt.dayofweek in DOW_SKIP:
        return {"date": date_str, "action": "SKIP", "reason": f"dow={dt.dayofweek}",
                "pred_range": pred_range, "regime": regime}

    # Get feature row for VIX/range filters
    feat_row = engine.features[engine.features["date"] == dt]
    if len(feat_row) > 0:
        feat_row = feat_row.iloc[0]
        if VIX_MAX is not None:
            vix = feat_row.get("vix_prev_close", None)
            if vix is not None and vix > VIX_MAX:
                return {"date": date_str, "action": "SKIP", "reason": f"vix={vix:.1f}>{VIX_MAX}",
                        "pred_range": pred_range, "regime": regime}
        if VIX_MIN is not None:
            vix = feat_row.get("vix_prev_close", None)
            if vix is not None and vix < VIX_MIN:
                return {"date": date_str, "action": "SKIP", "reason": f"vix={vix:.1f}<{VIX_MIN}",
                        "pred_range": pred_range, "regime": regime}
        if PREV_RANGE_MAX is not None:
            prev_range = feat_row.get("spx_prev_range_pct", None)
            if prev_range is not None and prev_range > PREV_RANGE_MAX:
                return {"date": date_str, "action": "SKIP", "reason": f"prev_range={prev_range:.3%}",
                        "pred_range": pred_range, "regime": regime}

    if MIN_SIMILARITY is not None:
        avg_sim = pred.get("avg_similarity", 0)
        if avg_sim < MIN_SIMILARITY:
            return {"date": date_str, "action": "SKIP", "reason": f"sim={avg_sim:.3f}",
                    "pred_range": pred_range, "regime": regime}

    if DANGER_PROB_MAX is not None:
        dp = pred.get("danger_prob_weighted", 0)
        if dp > DANGER_PROB_MAX:
            return {"date": date_str, "action": "SKIP", "reason": f"danger={dp:.1%}",
                    "pred_range": pred_range, "regime": regime}

    # Step 3: Load market data
    spot = load_spx_bar(date_str, ENTRY_TIME)
    if spot is None:
        return None

    quotes = load_quotes(date_str)
    if quotes is None:
        return None

    # Step 4: Calculate strikes
    strikes = find_ic_strikes(spot, pred_range, regime)

    # Step 5: Price the iron condor
    pricing = price_ic(quotes, strikes, ENTRY_TIME)
    if pricing is None:
        return {"date": date_str, "action": "SKIP", "reason": "no_quotes",
                "pred_range": pred_range, "regime": regime}

    # Check minimum credit
    if pricing["put_credit"] < MIN_CREDIT_PER_SIDE:
        return {"date": date_str, "action": "SKIP", "reason": "put_credit_too_low",
                "pred_range": pred_range, "regime": regime}
    if pricing["call_credit"] < MIN_CREDIT_PER_SIDE:
        return {"date": date_str, "action": "SKIP", "reason": "call_credit_too_low",
                "pred_range": pred_range, "regime": regime}

    # Step 6: Check for intraday stop loss
    # Store per-side credits for SL check
    quotes.attrs["put_credit"] = pricing["put_credit"]
    quotes.attrs["call_credit"] = pricing["call_credit"]

    sl_result = check_intraday_sl(quotes, strikes, pricing["total_credit"], ENTRY_TIME)

    if sl_result is not None:
        pnl = sl_result["sl_pnl"]
        commissions = COMMISSION * 4 * QTY * 2  # 4 legs, entry + exit
        pnl -= commissions
        return {
            "date": date_str, "action": "TRADE", "outcome": "SL",
            "pred_range": pred_range, "regime": regime,
            "spot": spot, "otm_distance": strikes["otm_distance"],
            "put_short": strikes["put_short"], "call_short": strikes["call_short"],
            "credit": pricing["total_credit"],
            "put_credit": pricing["put_credit"],
            "call_credit": pricing["call_credit"],
            "exit_val": sl_result["put_spread_val"] + sl_result["call_spread_val"],
            "pnl": pnl,
            "exit_time": str(sl_result["sl_time"]),
            "actual_range": pred.get("actual_range"),
        }

    # Step 7: Evaluate at expiry
    spot_close = load_spx_bar(date_str, dt_time(15, 59))
    if spot_close is None:
        spot_close = load_spx_bar(date_str, dt_time(15, 55))
    if spot_close is None:
        return None

    expiry = evaluate_ic_at_expiry(quotes, strikes, spot_close)
    pnl = (pricing["total_credit"] - expiry["total_exit_val"]) * 100 * QTY
    commissions = COMMISSION * 4 * QTY  # 4 legs, entry only (expire worthless = no exit cost)
    if expiry["total_exit_val"] > 0:
        commissions += COMMISSION * 4 * QTY  # Need to close if ITM

    pnl -= commissions

    outcome = "WIN" if pnl > 0 else "LOSS"

    return {
        "date": date_str, "action": "TRADE", "outcome": outcome,
        "pred_range": pred_range, "regime": regime,
        "spot": spot, "spot_close": spot_close,
        "otm_distance": strikes["otm_distance"],
        "put_short": strikes["put_short"], "call_short": strikes["call_short"],
        "credit": pricing["total_credit"],
        "put_credit": pricing["put_credit"],
        "call_credit": pricing["call_credit"],
        "exit_val": expiry["total_exit_val"],
        "pnl": pnl,
        "exit_time": "15:59",
        "actual_range": pred.get("actual_range"),
    }


def run_backtest(start: str = BACKTEST_START, end: str = BACKTEST_END) -> pd.DataFrame:
    """Run full backtest."""
    features = load_features()
    engine = SimSearchEngine(features)

    # Get trading dates
    dates = features[
        (features["date"] >= pd.Timestamp(start)) &
        (features["date"] <= pd.Timestamp(end))
    ]["date"].dt.strftime("%Y%m%d").tolist()

    log.info("Backtesting %d dates from %s to %s", len(dates), start, end)

    pred_cache = {}
    results = []
    t0 = time.time()

    for i, ds in enumerate(dates):
        result = backtest_day(ds, engine, pred_cache)
        if result is not None:
            results.append(result)

        if (i + 1) % 50 == 0:
            trades = sum(1 for r in results if r.get("action") == "TRADE")
            log.info("  Progress: %d/%d dates, %d trades (%.0fs)",
                     i + 1, len(dates), trades, time.time() - t0)

    df = pd.DataFrame(results)
    log.info("Backtest complete: %d results in %.0fs", len(df), time.time() - t0)
    return df


def format_results(df: pd.DataFrame) -> str:
    """Format backtest results."""
    lines = []
    lines.append("=" * 70)
    lines.append("SimCondor — Iron Condor Strategy (SimSearch Range Prediction)")
    lines.append("=" * 70)

    trades = df[df["action"] == "TRADE"]
    skips = df[df["action"] == "SKIP"]

    lines.append(f"\nPeriod: {df['date'].min()} to {df['date'].max()}")
    lines.append(f"Total days: {len(df)}")
    lines.append(f"Traded: {len(trades)} | Skipped: {len(skips)}")

    if len(trades) == 0:
        lines.append("No trades!")
        return "\n".join(lines)

    # P&L
    total_pnl = trades["pnl"].sum()
    cum_pnl = trades["pnl"].cumsum()
    max_dd = (cum_pnl - cum_pnl.cummax()).min()
    sharpe = trades["pnl"].mean() / trades["pnl"].std() * np.sqrt(252) if trades["pnl"].std() > 0 else 0
    calmar = total_pnl / abs(max_dd) if max_dd < 0 else 999
    wr = (trades["pnl"] > 0).mean()
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    lines.append(f"\nRESULTS")
    lines.append("-" * 50)
    lines.append(f"  Total P&L:    ${total_pnl:,.0f}")
    lines.append(f"  Max DD:       ${max_dd:,.0f}")
    lines.append(f"  Sharpe:       {sharpe:.2f}")
    lines.append(f"  Calmar:       {calmar:.2f}")
    lines.append(f"  Win Rate:     {wr:.1%}")
    lines.append(f"  Trades:       {len(trades)}")
    lines.append(f"  Avg Win:      ${wins['pnl'].mean():,.0f}" if len(wins) > 0 else "")
    lines.append(f"  Avg Loss:     ${losses['pnl'].mean():,.0f}" if len(losses) > 0 else "")
    lines.append(f"  Profit Factor: {wins['pnl'].sum() / abs(losses['pnl'].sum()):.2f}"
                 if len(losses) > 0 and losses['pnl'].sum() < 0 else "")
    lines.append(f"  Best Day:     ${trades['pnl'].max():,.0f}")
    lines.append(f"  Worst Day:    ${trades['pnl'].min():,.0f}")

    # Regime breakdown
    lines.append(f"\nBY REGIME")
    lines.append("-" * 50)
    for regime in ["CALM", "NORMAL", "VOLATILE", "DANGER"]:
        r = trades[trades["regime"] == regime]
        if len(r) > 0:
            lines.append(f"  {regime:10s}: {len(r):4d} trades, "
                        f"${r['pnl'].sum():>8,.0f} P&L, "
                        f"{(r['pnl']>0).mean():.0%} WR, "
                        f"avg credit ${r['credit'].mean()*100:.0f}")
        s = skips[skips["regime"] == regime] if "regime" in skips.columns else pd.DataFrame()
        if len(s) > 0:
            lines.append(f"  {regime:10s}: {len(s):4d} skipped")

    # OTM distance stats
    lines.append(f"\nSTRIKE DISTANCES")
    lines.append("-" * 50)
    lines.append(f"  Avg OTM distance: {trades['otm_distance'].mean():.0f} pts")
    lines.append(f"  Min OTM distance: {trades['otm_distance'].min():.0f} pts")
    lines.append(f"  Max OTM distance: {trades['otm_distance'].max():.0f} pts")
    lines.append(f"  Avg credit:       ${trades['credit'].mean()*100:.0f}")

    # Worst 5 days
    lines.append(f"\nWORST 5 DAYS")
    lines.append("-" * 50)
    for _, r in trades.nsmallest(5, "pnl").iterrows():
        lines.append(f"  {r['date']}: ${r['pnl']:+,.0f}  "
                     f"regime={r['regime']}  OTM={r['otm_distance']:.0f}  "
                     f"credit=${r['credit']*100:.0f}  "
                     f"range={r.get('actual_range', 0):.2%}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="SimCondor Backtest")
    parser.add_argument("--start", type=str, default=BACKTEST_START)
    parser.add_argument("--end", type=str, default=BACKTEST_END)
    parser.add_argument("--date", type=str, help="Single date")
    args = parser.parse_args()

    if args.date:
        features = load_features()
        engine = SimSearchEngine(features)
        result = backtest_day(args.date, engine, {})
        if result:
            for k, v in result.items():
                print(f"  {k}: {v}")
        else:
            print("No result for this date")
        return

    df = run_backtest(start=args.start, end=args.end)

    if df.empty:
        print("No results!")
        return

    report = format_results(df)
    print(report)

    # Save
    out = os.path.join(LOGS_DIR, f"simcondor_trades_{RUN_TS}.csv")
    df.to_csv(out, index=False)
    print(f"\nTrades saved: {out}")

    report_path = os.path.join(LOGS_DIR, f"simcondor_report_{RUN_TS}.txt")
    with open(report_path, "w") as f:
        f.write(report)


if __name__ == "__main__":
    main()
