#!/usr/bin/env python3
"""Credit Decay Model for 0DTE SPX Options — Last 10 Minutes.

Models how OTM option credit decays minute-by-minute from 15:50 to 15:59,
as a function of time-to-expiry and SPX price movement.

Usage:
    python3 credit_decay_model.py              # Fit model and print coefficients
    python3 credit_decay_model.py --predict    # Predict credit given inputs
"""
from __future__ import annotations
import os, glob, sys
import numpy as np
import pandas as pd
from datetime import datetime

# Add parent for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apex_config as _cfg
from apex_engine import (
    build_daily_indicators, _load_spx_1min, _get_spx_at_time,
    _load_option_quotes, _get_quote, _find_put_strikes, _find_call_strikes,
    prior_day_ind,
)

DATA_DIR = _cfg.DATA_DIR


def collect_data() -> pd.DataFrame:
    """Collect minute-by-minute credit data for OTM options in last 10 minutes."""
    indicators = build_daily_indicators()
    all_dates = sorted(d for d in indicators.keys() if "20220103" <= d <= "20260325")

    rows = []
    for d in all_dates:
        if d in _cfg.MARKET_HOLIDAYS or d in _cfg.EARLY_CLOSE_DAYS:
            continue

        spx_df = _load_spx_1min(d)
        if spx_df is None or "time_str" not in spx_df.columns:
            continue
        quotes_df = _load_option_quotes(d)
        if quotes_df is None:
            continue
        available = set(quotes_df.index.get_level_values("strike").unique())

        # Reference spot at 15:50 for consistent strike selection
        spot_ref = _get_spx_at_time(spx_df, "15:50:00")
        if spot_ref is None:
            continue

        prev = prior_day_ind(d)
        vix = prev.get("vix_close") if prev else None

        for opt_type in ["PUT", "CALL"]:
            right = "P" if opt_type == "PUT" else "C"
            if opt_type == "PUT":
                strikes = _find_put_strikes(spot_ref, 12.0, 10.0, available)
            else:
                strikes = _find_call_strikes(spot_ref, 12.0, 10.0, available)
            if strikes is None:
                continue
            short, long = strikes

            for minute in range(50, 60):
                time_str = f"15:{minute:02d}:00"
                sq = _get_quote(quotes_df, right, short, time_str)
                spot_now = _get_spx_at_time(spx_df, time_str)
                if not sq or spot_now is None:
                    continue

                ttx = 60 - minute  # minutes to expiry (16:00)
                otm_dist = abs(spot_now - short)
                spx_move_from_ref = spot_now - spot_ref  # signed

                rows.append({
                    "date": d,
                    "minute": minute,
                    "opt_type": opt_type,
                    "ttx": ttx,  # minutes to expiry
                    "bid": sq["bid"],
                    "ask": sq["ask"],
                    "mid": sq["mid"],
                    "otm_dist": otm_dist,
                    "spot": spot_now,
                    "spot_ref": spot_ref,
                    "spx_move": spx_move_from_ref,
                    "short_strike": short,
                    "vix": vix,
                })

    return pd.DataFrame(rows)


def fit_model(df: pd.DataFrame):
    """Fit a linear regression: bid = f(ttx, otm_dist, spx_move, vix)."""
    from sklearn.linear_model import LinearRegression

    # Features
    df = df.dropna(subset=["bid", "ttx", "otm_dist", "vix"]).copy()
    df["ttx_sqrt"] = np.sqrt(df["ttx"])  # theta decays as sqrt(time)
    df["otm_pct"] = df["otm_dist"] / df["spot"] * 100
    df["is_put"] = (df["opt_type"] == "PUT").astype(int)
    # For puts: negative SPX move = getting closer to ITM
    df["move_toward_strike"] = np.where(
        df["is_put"] == 1, -df["spx_move"], df["spx_move"]
    )

    features = ["ttx_sqrt", "otm_dist", "move_toward_strike", "vix"]
    X = df[features].values
    y = df["bid"].values

    model = LinearRegression()
    model.fit(X, y)

    print("=" * 60)
    print("CREDIT DECAY MODEL: bid = f(sqrt(ttx), otm, move, vix)")
    print("=" * 60)
    print(f"R² = {model.score(X, y):.4f}")
    print(f"Intercept: {model.intercept_:.6f}")
    print()
    print(f"{'Feature':<25} {'Coefficient':>12} {'Interpretation'}")
    print("-" * 60)
    for feat, coef in zip(features, model.coef_):
        if feat == "ttx_sqrt":
            interp = f"+1 min TTX = +${coef * (np.sqrt(6) - np.sqrt(5)):.4f} bid"
        elif feat == "otm_dist":
            interp = f"+1 pt OTM = ${coef:+.4f} bid"
        elif feat == "move_toward_strike":
            interp = f"+1 pt toward strike = ${coef:+.4f} bid"
        elif feat == "vix":
            interp = f"+1 VIX pt = ${coef:+.4f} bid"
        else:
            interp = ""
        print(f"{feat:<25} {coef:>12.6f} {interp}")

    # Predict at key scenarios
    print()
    print("=" * 60)
    print("MODEL PREDICTIONS")
    print("=" * 60)
    print()
    scenarios = [
        ("15:55 calm (VIX=20, d=12, no move)", [np.sqrt(5), 12, 0, 20]),
        ("15:55 calm (VIX=30, d=12, no move)", [np.sqrt(5), 12, 0, 30]),
        ("15:55 3pt toward strike", [np.sqrt(5), 12, 3, 20]),
        ("15:55 5pt toward strike", [np.sqrt(5), 12, 5, 20]),
        ("15:57 calm (VIX=20, d=8, no move)", [np.sqrt(3), 8, 0, 20]),
        ("15:57 3pt toward strike", [np.sqrt(3), 8, 3, 20]),
        ("15:58 calm (VIX=20, d=7)", [np.sqrt(2), 7, 0, 20]),
        ("15:59 calm (VIX=20, d=12)", [np.sqrt(1), 12, 0, 20]),
    ]
    for label, features_val in scenarios:
        pred = model.predict([features_val])[0]
        print(f"  {label:<45} → bid = ${max(0, pred):.3f}")

    return model


def main():
    print("Collecting data...")
    df = collect_data()
    print(f"Collected {len(df)} data points from {df['date'].nunique()} days")
    print()

    model = fit_model(df)

    # Summary stats
    print()
    print("=" * 60)
    print("DECAY CURVE (avg bid by minute)")
    print("=" * 60)
    for minute in range(50, 60):
        chunk = df[df["minute"] == minute]
        print(
            f"  15:{minute:02d} → bid=${chunk['bid'].mean():.3f} "
            f"(median=${chunk['bid'].median():.3f}, "
            f"P75=${chunk['bid'].quantile(0.75):.3f})"
        )


if __name__ == "__main__":
    main()
