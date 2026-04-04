#!/usr/bin/env python3
"""SimSearch Ensemble — blend SimSearch + naive + VIX for better range prediction.

Each component captures different information:
- SimSearch: pattern matching across 30 years of similar market states
- Naive (yesterday's range): simple volatility persistence (strong baseline)
- VIX level: forward-looking implied vol (market's own range estimate)
- Realized vol: backward-looking vol clustering

The ensemble learns optimal weights via walk-forward cross-validation.

Usage:
    python simsearch_ensemble.py                    # full validation
    python simsearch_ensemble.py --date 20260325    # single prediction
"""
from __future__ import annotations

import argparse
import logging
import os
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from simsearch_config import LOGS_DIR, RUN_TS, VALIDATION_START, VALIDATION_END
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_regime import (
    predict_regime, classify_range, recommend_action,
    DANGER_THRESHOLD, RANGE_THRESHOLDS, format_regime_prediction,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TRAIN_WINDOW = 252  # 1 year of training data for walk-forward


def build_ensemble_features(features: pd.DataFrame,
                            engine: SimSearchEngine,
                            start: str, end: str,
                            top_k: int = 20) -> pd.DataFrame:
    """Build ensemble feature matrix: SimSearch pred + naive + VIX + realized vol."""
    dates = features[
        (features["date"] >= pd.Timestamp(start)) &
        (features["date"] <= pd.Timestamp(end)) &
        (features["outcome_range_pct"].notna())
    ]["date"].sort_values()

    rows = []
    for i, dt in enumerate(dates):
        ds = dt.strftime("%Y%m%d")
        query_row = features[features["date"] == dt].iloc[0]

        # SimSearch prediction
        pred = predict_regime(engine, ds, top_k=top_k)
        if "error" in pred:
            continue

        row = {
            "date": dt,
            "actual_range": pred["actual_range"],
            # Component 1: SimSearch
            "sim_range": pred["pred_range_weighted"],
            "sim_danger_prob": pred["danger_prob_weighted"],
            # Component 2: Naive (yesterday's range)
            "naive_range": query_row.get("spx_prev_range_pct", np.nan),
            # Component 3: VIX level (scaled to range units: VIX/16/sqrt(252))
            "vix_implied_range": query_row.get("vix_prev_close", np.nan) / 100 / np.sqrt(252),
            # Component 4: Realized vol
            "realized_vol_daily": query_row.get("realized_vol_20d", np.nan) / np.sqrt(252) if pd.notna(query_row.get("realized_vol_20d")) else np.nan,
            # Component 5: 2-day avg range (vol clustering)
            "avg_range_2d": np.nanmean([
                query_row.get("spx_prev_range_pct", np.nan),
                query_row.get("spx_prev2_range_pct", np.nan),
            ]),
            # Component 6: VIX momentum
            "vix_momentum": query_row.get("vix_momentum_3d", np.nan),
        }
        rows.append(row)

        if (i + 1) % 100 == 0:
            log.info("  Ensemble features: %d/%d", i + 1, len(dates))

    return pd.DataFrame(rows)


def walk_forward_ensemble(ens_df: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward validation: train Ridge on trailing window, predict next day."""
    feature_cols = ["sim_range", "naive_range", "vix_implied_range",
                    "realized_vol_daily", "avg_range_2d", "sim_danger_prob", "vix_momentum"]

    results = []
    for i in range(TRAIN_WINDOW, len(ens_df)):
        train = ens_df.iloc[max(0, i - TRAIN_WINDOW):i].copy()
        test_row = ens_df.iloc[i]

        # Drop NaN rows from training
        train_valid = train[feature_cols + ["actual_range"]].dropna()
        if len(train_valid) < 50:
            continue

        X_train = train_valid[feature_cols].values
        y_train = train_valid["actual_range"].values

        # Fit Ridge regression
        model = Ridge(alpha=1.0)
        model.fit(X_train, y_train)

        # Predict
        X_test = test_row[feature_cols].values.astype(float).reshape(1, -1)
        if np.any(np.isnan(X_test)):
            # Fill NaN with training median
            medians = train_valid[feature_cols].median().values
            X_test = np.where(np.isnan(X_test), medians, X_test)

        pred_range = float(model.predict(X_test)[0])
        pred_range = max(pred_range, 0.001)  # floor at 0.1%

        # Danger probability: blend SimSearch danger with model
        danger_prob = test_row["sim_danger_prob"]
        if pred_range >= DANGER_THRESHOLD:
            danger_prob = max(danger_prob, 0.35)

        pred_bucket = classify_range(pred_range)
        action, reason = recommend_action(pred_range, danger_prob, {})

        results.append({
            "date": test_row["date"],
            "actual_range": test_row["actual_range"],
            "pred_range_ensemble": pred_range,
            "pred_range_sim": test_row["sim_range"],
            "pred_range_naive": test_row["naive_range"],
            "pred_bucket": pred_bucket,
            "actual_bucket": classify_range(test_row["actual_range"]),
            "danger_prob": danger_prob,
            "action": action,
            "weights": dict(zip(feature_cols, model.coef_)),
        })

    return pd.DataFrame(results)


def compute_ensemble_metrics(df: pd.DataFrame) -> dict:
    """Compute metrics for ensemble vs components."""
    m = {"n_days": len(df)}

    for col, label in [("pred_range_ensemble", "ensemble"),
                        ("pred_range_sim", "simsearch"),
                        ("pred_range_naive", "naive")]:
        valid = df[[col, "actual_range"]].dropna()
        if len(valid) > 10:
            m[f"{label}_corr"] = valid[col].corr(valid["actual_range"])
            m[f"{label}_rmse"] = np.sqrt(((valid[col] - valid["actual_range"])**2).mean())
            m[f"{label}_mae"] = (valid[col] - valid["actual_range"]).abs().mean()

    # Bucket accuracy
    m["ensemble_bucket_acc"] = (df["pred_bucket"] == df["actual_bucket"]).mean()

    # Danger detection
    df["is_danger"] = df["actual_range"] >= DANGER_THRESHOLD
    df["pred_danger"] = df["danger_prob"] >= 0.25

    danger_days = df[df["is_danger"]]
    if len(danger_days) > 0:
        m["danger_recall"] = danger_days["pred_danger"].mean()
        m["danger_total"] = len(danger_days)

    flagged = df[df["pred_danger"]]
    if len(flagged) > 0:
        m["danger_precision"] = flagged["is_danger"].mean()
        m["danger_flagged"] = len(flagged)

    # Action analysis
    trade_days = df[df["action"] == "TRADE"]
    skip_days = df[df["action"] != "TRADE"]
    m["trade_pct"] = len(trade_days) / len(df)
    if len(trade_days) > 0:
        m["trade_avg_range"] = trade_days["actual_range"].mean()
        m["trade_danger_rate"] = trade_days["is_danger"].mean()
    if len(skip_days) > 0:
        m["skip_avg_range"] = skip_days["actual_range"].mean()
        m["skip_danger_rate"] = skip_days["is_danger"].mean()

    return m


def format_ensemble_report(m: dict, df: pd.DataFrame) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("SimSearch ENSEMBLE Validation Report")
    lines.append("=" * 70)
    lines.append(f"\nDays: {m['n_days']} (walk-forward, {TRAIN_WINDOW}-day training window)")
    lines.append("")

    lines.append("RANGE PREDICTION COMPARISON")
    lines.append("-" * 50)
    for label in ["ensemble", "simsearch", "naive"]:
        corr = m.get(f"{label}_corr", np.nan)
        rmse = m.get(f"{label}_rmse", np.nan)
        mae = m.get(f"{label}_mae", np.nan)
        marker = " <<<" if label == "ensemble" else ""
        lines.append(f"  {label:12s}  corr={corr:.4f}  RMSE={rmse:.4%}  MAE={mae:.4%}{marker}")

    ens_corr = m.get("ensemble_corr", 0)
    sim_corr = m.get("simsearch_corr", 0)
    naive_corr = m.get("naive_corr", 0)
    lines.append(f"\n  Ensemble vs SimSearch: {ens_corr - sim_corr:+.4f}")
    lines.append(f"  Ensemble vs Naive:     {ens_corr - naive_corr:+.4f}")

    lines.append(f"\n  Bucket accuracy: {m.get('ensemble_bucket_acc', 0):.1%}")

    lines.append("")
    lines.append("DANGER DETECTION")
    lines.append("-" * 50)
    lines.append(f"  Recall:    {m.get('danger_recall', 0):.1%} "
                 f"({m.get('danger_total', 0)} danger days)")
    lines.append(f"  Precision: {m.get('danger_precision', 0):.1%} "
                 f"({m.get('danger_flagged', 0)} flagged)")

    lines.append("")
    lines.append("TRADE vs SKIP")
    lines.append("-" * 50)
    lines.append(f"  Trade: {m.get('trade_pct', 0):.0%} of days  "
                 f"avg range={m.get('trade_avg_range', 0):.3%}  "
                 f"danger rate={m.get('trade_danger_rate', 0):.1%}")
    lines.append(f"  Skip:  {1 - m.get('trade_pct', 1):.0%} of days  "
                 f"avg range={m.get('skip_avg_range', 0):.3%}  "
                 f"danger rate={m.get('skip_danger_rate', 0):.1%}")

    # Feature weights from last model
    if len(df) > 0 and "weights" in df.columns:
        last_weights = df.iloc[-1]["weights"]
        lines.append("")
        lines.append("ENSEMBLE WEIGHTS (latest model)")
        lines.append("-" * 50)
        for feat, w in sorted(last_weights.items(), key=lambda x: abs(x[1]), reverse=True):
            lines.append(f"  {feat:25s}  {w:+.6f}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="SimSearch Ensemble")
    parser.add_argument("--start", type=str, default="2023-06-01")
    parser.add_argument("--end", type=str, default="2026-03-25")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--date", type=str, help="Single date prediction")
    args = parser.parse_args()

    features = load_features()
    engine = SimSearchEngine(features)

    if args.date:
        # Single prediction
        pred = predict_regime(engine, args.date, top_k=args.top_k)
        print(format_regime_prediction(pred))
        return

    # Build ensemble features (needs SimSearch predictions for each day)
    log.info("Building ensemble features...")
    # Need extra history for training window
    train_start = pd.Timestamp(args.start) - pd.Timedelta(days=int(TRAIN_WINDOW * 1.5))
    ens_df = build_ensemble_features(features, engine,
                                      start=train_start.strftime("%Y-%m-%d"),
                                      end=args.end, top_k=args.top_k)
    log.info("Ensemble features: %d rows", len(ens_df))

    # Walk-forward
    log.info("Running walk-forward ensemble...")
    results = walk_forward_ensemble(ens_df)

    # Filter to validation period
    results = results[results["date"] >= pd.Timestamp(args.start)]
    log.info("Validation results: %d days", len(results))

    if results.empty:
        print("No results!")
        return

    m = compute_ensemble_metrics(results)
    print(format_ensemble_report(m, results))

    out = os.path.join(LOGS_DIR, f"ensemble_{RUN_TS}.csv")
    results.drop(columns=["weights"]).to_csv(out, index=False)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
