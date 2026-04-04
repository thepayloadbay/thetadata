#!/usr/bin/env python3
"""SimSearch Validation — walk-forward backtesting of predictive power.

Runs SimSearch on each day in the validation window, compares predictions
to actual outcomes, and reports accuracy metrics.

Usage:
    python simsearch_validate.py                         # full validation
    python simsearch_validate.py --start 2024-01-01      # custom start
    python simsearch_validate.py --tier 1                # Tier 1 only
    python simsearch_validate.py --top-k 10              # top 10 matches
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import numpy as np
import pandas as pd

from simsearch_config import (
    VALIDATION_START, VALIDATION_END, TOP_K, LOGS_DIR, RUN_TS,
)
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine
from simsearch_predict import analyze_matches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def run_validation(start: str = VALIDATION_START,
                   end: str = VALIDATION_END,
                   top_k: int = TOP_K,
                   max_tier: int | None = None,
                   method: str = "cosine") -> pd.DataFrame:
    """Walk-forward validation of SimSearch predictions.

    For each day D in [start, end]:
      1. Find top-K similar days using only data BEFORE D
      2. Record prediction vs actual outcome

    Returns DataFrame with per-day validation results.
    """
    features = load_features()
    engine = SimSearchEngine(features)

    dates = features[
        (features["date"] >= pd.Timestamp(start)) &
        (features["date"] <= pd.Timestamp(end))
    ]["date"].sort_values()

    log.info("Validation: %d days from %s to %s, top_k=%d, method=%s",
             len(dates), start, end, top_k, method)

    results = []
    t0 = time.time()

    for i, query_date in enumerate(dates):
        date_str = query_date.strftime("%Y%m%d")

        try:
            matches = engine.find_similar(
                date_str, top_k=top_k, max_tier=max_tier,
                method=method, before_only=True
            )
        except (ValueError, Exception) as e:
            log.debug("Skip %s: %s", date_str, e)
            continue

        if matches.empty:
            continue

        pred = analyze_matches(matches)

        # Get actual outcome
        query_row = features[features["date"] == query_date].iloc[0]
        actual_return = query_row.get("outcome_return", np.nan)
        actual_direction = query_row.get("outcome_direction", np.nan)
        actual_range = query_row.get("outcome_range_pct", np.nan)

        row = {
            "date": query_date,
            "actual_return": actual_return,
            "actual_direction": actual_direction,
            "actual_range": actual_range,
            "pred_return": pred.get("pred_return_weighted", np.nan),
            "pred_direction_up_pct": pred.get("pred_direction_up_pct", np.nan),
            "pred_direction": pred.get("pred_direction", ""),
            "pred_direction_confidence": pred.get("pred_direction_confidence", np.nan),
            "pred_range_mean": pred.get("pred_range_mean", np.nan),
            "pred_next_return": pred.get("pred_next_return_mean", np.nan),
            "pred_next_direction_up_pct": pred.get("pred_next_direction_up_pct", np.nan),
            "n_matches": pred.get("n_matches", 0),
            "avg_similarity": pred.get("avg_similarity", 0),
            "max_tier": pred.get("max_tier", 0),
            "top_1_similarity": matches.iloc[0]["similarity"] if len(matches) > 0 else np.nan,
            "top_1_date": matches.iloc[0]["date"] if len(matches) > 0 else pd.NaT,
        }
        results.append(row)

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            log.info("  Progress: %d/%d days (%.1f sec)", i + 1, len(dates), elapsed)

    df = pd.DataFrame(results)
    elapsed = time.time() - t0
    log.info("Validation complete: %d days in %.1f sec", len(df), elapsed)
    return df


def compute_metrics(df: pd.DataFrame) -> dict:
    """Compute validation metrics from walk-forward results."""
    if df.empty:
        return {"error": "No validation results"}

    metrics = {"n_days": len(df)}

    # Direction accuracy
    df["pred_dir_binary"] = np.where(df["pred_direction_up_pct"] > 0.5, 1, -1)
    correct = (df["pred_dir_binary"] == df["actual_direction"])
    metrics["direction_accuracy"] = correct.mean()
    metrics["direction_accuracy_n"] = correct.sum()

    # High confidence subset (>60% agreement)
    high_conf = df[df["pred_direction_confidence"] > 0.60]
    if len(high_conf) > 0:
        hc_correct = (
            np.where(high_conf["pred_direction_up_pct"] > 0.5, 1, -1) ==
            high_conf["actual_direction"]
        )
        metrics["direction_accuracy_highconf"] = hc_correct.mean()
        metrics["direction_accuracy_highconf_n"] = len(high_conf)
    else:
        metrics["direction_accuracy_highconf"] = np.nan
        metrics["direction_accuracy_highconf_n"] = 0

    # Very high confidence (>70%)
    vhc = df[df["pred_direction_confidence"] > 0.70]
    if len(vhc) > 0:
        vhc_correct = (
            np.where(vhc["pred_direction_up_pct"] > 0.5, 1, -1) ==
            vhc["actual_direction"]
        )
        metrics["direction_accuracy_vhighconf"] = vhc_correct.mean()
        metrics["direction_accuracy_vhighconf_n"] = len(vhc)

    # Return prediction correlation
    valid = df[["pred_return", "actual_return"]].dropna()
    if len(valid) > 10:
        metrics["return_correlation"] = valid["pred_return"].corr(valid["actual_return"])
    else:
        metrics["return_correlation"] = np.nan

    # Range prediction
    valid_range = df[["pred_range_mean", "actual_range"]].dropna()
    if len(valid_range) > 10:
        metrics["range_correlation"] = valid_range["pred_range_mean"].corr(valid_range["actual_range"])
        metrics["range_rmse"] = np.sqrt(((valid_range["pred_range_mean"] - valid_range["actual_range"])**2).mean())
    else:
        metrics["range_correlation"] = np.nan
        metrics["range_rmse"] = np.nan

    # Brier score (for direction probability calibration)
    # Brier = mean((pred_prob - actual_binary)^2)
    actual_up = (df["actual_direction"] > 0).astype(float)
    pred_up_prob = df["pred_direction_up_pct"].clip(0, 1)
    valid_brier = pd.notna(actual_up) & pd.notna(pred_up_prob)
    if valid_brier.sum() > 10:
        metrics["brier_score"] = ((pred_up_prob[valid_brier] - actual_up[valid_brier])**2).mean()
        # Random baseline Brier = 0.25
        metrics["brier_skill_score"] = 1 - metrics["brier_score"] / 0.25
    else:
        metrics["brier_score"] = np.nan
        metrics["brier_skill_score"] = np.nan

    # Simulated P&L: bet $100 on predicted direction
    df["sim_pnl"] = np.where(
        df["pred_dir_binary"] == df["actual_direction"],
        df["actual_return"].abs() * 10000,  # win in basis points
        -df["actual_return"].abs() * 10000   # lose in basis points
    )
    metrics["sim_pnl_total_bps"] = df["sim_pnl"].sum()
    metrics["sim_pnl_sharpe"] = (df["sim_pnl"].mean() / df["sim_pnl"].std() * np.sqrt(252)
                                  if df["sim_pnl"].std() > 0 else 0)

    # Next-day prediction accuracy
    if "pred_next_direction_up_pct" in df.columns:
        df["actual_next_direction"] = df["actual_direction"].shift(-1)
        valid_next = df[["pred_next_direction_up_pct", "actual_next_direction"]].dropna()
        if len(valid_next) > 10:
            next_pred = np.where(valid_next["pred_next_direction_up_pct"] > 0.5, 1, -1)
            metrics["next_day_direction_accuracy"] = (next_pred == valid_next["actual_next_direction"]).mean()

    # Similarity-binned accuracy
    if len(df) > 20:
        df["sim_bin"] = pd.qcut(df["avg_similarity"], 4, labels=["Q1_low", "Q2", "Q3", "Q4_high"])
        for bin_name in ["Q1_low", "Q4_high"]:
            subset = df[df["sim_bin"] == bin_name]
            if len(subset) > 0:
                acc = (np.where(subset["pred_direction_up_pct"] > 0.5, 1, -1) ==
                       subset["actual_direction"]).mean()
                metrics[f"direction_accuracy_{bin_name}"] = acc

    return metrics


def format_validation_report(metrics: dict, results_df: pd.DataFrame) -> str:
    """Format validation metrics as readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append("SimSearch Walk-Forward Validation Report")
    lines.append("=" * 70)
    lines.append("")

    lines.append(f"Days validated:  {metrics.get('n_days', 0)}")
    lines.append("")

    # Direction
    lines.append("DIRECTION PREDICTION")
    lines.append("-" * 50)
    acc = metrics.get("direction_accuracy", 0)
    lines.append(f"Overall accuracy:     {acc:.1%} ({metrics.get('direction_accuracy_n', 0)} correct)")
    lines.append(f"  (random baseline:   50.0%)")

    hc_acc = metrics.get("direction_accuracy_highconf", np.nan)
    if pd.notna(hc_acc):
        lines.append(f"High-conf (>60%):     {hc_acc:.1%} "
                     f"({metrics.get('direction_accuracy_highconf_n', 0)} days)")

    vhc_acc = metrics.get("direction_accuracy_vhighconf", np.nan)
    if pd.notna(vhc_acc):
        lines.append(f"Very-high-conf (>70%): {vhc_acc:.1%} "
                     f"({metrics.get('direction_accuracy_vhighconf_n', 0)} days)")

    # Similarity-binned
    for key in ["direction_accuracy_Q1_low", "direction_accuracy_Q4_high"]:
        val = metrics.get(key)
        if val is not None:
            label = "Low similarity" if "Q1" in key else "High similarity"
            lines.append(f"  {label}:     {val:.1%}")

    # Return prediction
    lines.append("")
    lines.append("RETURN PREDICTION")
    lines.append("-" * 50)
    rc = metrics.get("return_correlation", np.nan)
    lines.append(f"Return correlation:   {rc:.4f}" if pd.notna(rc) else "Return correlation: N/A")

    # Range prediction
    rng_corr = metrics.get("range_correlation", np.nan)
    rng_rmse = metrics.get("range_rmse", np.nan)
    lines.append(f"Range correlation:    {rng_corr:.4f}" if pd.notna(rng_corr) else "Range correlation: N/A")
    lines.append(f"Range RMSE:           {rng_rmse:.4%}" if pd.notna(rng_rmse) else "Range RMSE: N/A")

    # Calibration
    lines.append("")
    lines.append("CALIBRATION")
    lines.append("-" * 50)
    brier = metrics.get("brier_score", np.nan)
    bss = metrics.get("brier_skill_score", np.nan)
    lines.append(f"Brier score:          {brier:.4f} (lower=better, random=0.25)"
                 if pd.notna(brier) else "Brier score: N/A")
    lines.append(f"Brier skill score:    {bss:.4f} (>0 = better than random)"
                 if pd.notna(bss) else "Brier skill score: N/A")

    # Simulated P&L
    lines.append("")
    lines.append("SIMULATED P&L (directional bet)")
    lines.append("-" * 50)
    lines.append(f"Total P&L:            {metrics.get('sim_pnl_total_bps', 0):.0f} bps")
    lines.append(f"Sharpe ratio:         {metrics.get('sim_pnl_sharpe', 0):.2f}")

    # Next-day
    nda = metrics.get("next_day_direction_accuracy")
    if nda is not None:
        lines.append("")
        lines.append("NEXT-DAY PREDICTION")
        lines.append("-" * 50)
        lines.append(f"Direction accuracy:   {nda:.1%}")

    # Monthly breakdown
    if len(results_df) > 30:
        lines.append("")
        lines.append("MONTHLY BREAKDOWN")
        lines.append("-" * 50)
        results_df["month"] = results_df["date"].dt.to_period("M")
        monthly = results_df.groupby("month").apply(
            lambda g: pd.Series({
                "n": len(g),
                "accuracy": (np.where(g["pred_direction_up_pct"] > 0.5, 1, -1) ==
                            g["actual_direction"]).mean(),
                "avg_sim": g["avg_similarity"].mean(),
            })
        )
        for period, row in monthly.iterrows():
            lines.append(f"  {period}: acc={row['accuracy']:.0%} "
                        f"(n={int(row['n'])}, sim={row['avg_sim']:.3f})")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="SimSearch Validation")
    parser.add_argument("--start", type=str, default=VALIDATION_START)
    parser.add_argument("--end", type=str, default=VALIDATION_END)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--tier", type=int, default=None, help="Max feature tier")
    parser.add_argument("--method", type=str, default="cosine",
                        choices=["cosine", "lorentzian", "mahalanobis"])
    args = parser.parse_args()

    results = run_validation(
        start=args.start, end=args.end,
        top_k=args.top_k, max_tier=args.tier,
        method=args.method,
    )

    if results.empty:
        print("No validation results generated!")
        return

    metrics = compute_metrics(results)
    report = format_validation_report(metrics, results)
    print(report)

    # Save results
    out_path = os.path.join(LOGS_DIR, f"validation_{RUN_TS}.csv")
    results.to_csv(out_path, index=False)
    print(f"\nDetailed results saved: {out_path}")

    # Save metrics
    metrics_path = os.path.join(LOGS_DIR, f"validation_metrics_{RUN_TS}.txt")
    with open(metrics_path, "w") as f:
        f.write(report)
    print(f"Report saved: {metrics_path}")


if __name__ == "__main__":
    main()
