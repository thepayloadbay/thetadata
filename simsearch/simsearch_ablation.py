#!/usr/bin/env python3
"""SimSearch Feature Ablation — find which features actually matter.

Tests leave-one-group-out and single-group-only to identify signal vs noise.

Usage:
    python simsearch_ablation.py
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from simsearch_features import load_features
from simsearch_engine import SimSearchEngine, get_feature_cols, get_feature_tier
from simsearch_regime import predict_regime, DANGER_THRESHOLD, classify_range
from simsearch_config import VALIDATION_START, VALIDATION_END

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


def classify_features(feature_cols: list[str]) -> dict[str, list[str]]:
    """Group features into semantic categories."""
    groups = {}
    for c in feature_cols:
        if c.startswith("spx_prev_return"):
            groups.setdefault("spx_returns", []).append(c)
        elif c.startswith("spx_prev_") and "return" not in c:
            groups.setdefault("spx_candle", []).append(c)
        elif c.startswith("spx_gap"):
            groups.setdefault("spx_gap", []).append(c)
        elif c.startswith(("spx_above", "spx_rsi", "spx_dist")):
            groups.setdefault("spx_trend", []).append(c)
        elif c.startswith(("vix_prev", "vix_percentile", "vix_zscore", "vix_spx")):
            groups.setdefault("vix_features", []).append(c)
        elif c.startswith("realized_vol"):
            groups.setdefault("realized_vol", []).append(c)
        elif c.startswith(("dow", "month", "is_", "days_to")):
            groups.setdefault("calendar", []).append(c)
        elif c.startswith("prev_"):
            groups.setdefault("intraday_shape", []).append(c)
        elif c.startswith(("vix1d", "vix9d", "vvix")):
            groups.setdefault("vix_term", []).append(c)
        elif c.startswith(("net_gex", "call_gex", "put_gex", "net_vex", "gex_")):
            groups.setdefault("gex_features", []).append(c)
        elif c.startswith(("iv_", "gamma_")):
            groups.setdefault("iv_greeks", []).append(c)
        elif c.startswith(("put_call", "total_oi", "call_wall", "put_wall")):
            groups.setdefault("oi_positioning", []).append(c)
        else:
            groups.setdefault("other", []).append(c)
    return groups


def run_regime_validation_with_mask(features: pd.DataFrame,
                                     allowed_cols: list[str],
                                     start: str = "2023-06-01",
                                     end: str = "2026-03-25",
                                     top_k: int = 20) -> dict:
    """Run regime validation using only specified feature columns."""
    # Temporarily zero out non-allowed features by setting them to NaN
    modified = features.copy()
    all_feat_cols = get_feature_cols(features, max_tier=3)
    drop_cols = [c for c in all_feat_cols if c not in allowed_cols]
    for c in drop_cols:
        modified[c] = np.nan

    engine = SimSearchEngine(modified)
    dates = modified[
        (modified["date"] >= pd.Timestamp(start)) &
        (modified["date"] <= pd.Timestamp(end)) &
        (modified["outcome_range_pct"].notna())
    ]["date"].sort_values()

    pred_ranges = []
    actual_ranges = []
    danger_flags = []
    actual_dangers = []

    for dt in dates:
        ds = dt.strftime("%Y%m%d")
        pred = predict_regime(engine, ds, top_k=top_k)
        if "error" in pred:
            continue
        if pred["actual_range"] is None:
            continue

        pred_ranges.append(pred["pred_range_weighted"])
        actual_ranges.append(pred["actual_range"])
        danger_flags.append(pred["danger_prob_weighted"] >= 0.25)
        actual_dangers.append(pred["actual_range"] >= DANGER_THRESHOLD)

    if len(pred_ranges) < 20:
        return {"range_corr": np.nan, "bucket_acc": np.nan, "danger_recall": np.nan}

    pred_r = np.array(pred_ranges)
    actual_r = np.array(actual_ranges)
    df_flags = np.array(danger_flags)
    ad_flags = np.array(actual_dangers)

    # Range correlation
    range_corr = np.corrcoef(pred_r, actual_r)[0, 1]

    # Bucket accuracy
    pred_buckets = [classify_range(r) for r in pred_r]
    actual_buckets = [classify_range(r) for r in actual_r]
    bucket_acc = sum(p == a for p, a in zip(pred_buckets, actual_buckets)) / len(pred_buckets)

    # Danger recall
    actual_danger_count = ad_flags.sum()
    if actual_danger_count > 0:
        danger_recall = (df_flags & ad_flags).sum() / actual_danger_count
    else:
        danger_recall = np.nan

    # Danger rate on trade days
    trade_mask = ~df_flags
    if trade_mask.sum() > 0:
        danger_rate_trade = ad_flags[trade_mask].mean()
    else:
        danger_rate_trade = np.nan

    return {
        "range_corr": range_corr,
        "bucket_acc": bucket_acc,
        "danger_recall": danger_recall,
        "danger_rate_trade": danger_rate_trade,
        "n_days": len(pred_ranges),
    }


def main():
    features = load_features()
    all_feat_cols = get_feature_cols(features, max_tier=3)
    groups = classify_features(all_feat_cols)

    print("=" * 80)
    print("SimSearch Feature Ablation Study")
    print("=" * 80)
    print(f"\nTotal features: {len(all_feat_cols)} in {len(groups)} groups")
    print(f"Validation: 2023-06-01 to 2026-03-25, K=20\n")

    # Baseline: all features
    print("Running baseline (all features)...")
    t0 = time.time()
    baseline = run_regime_validation_with_mask(features, all_feat_cols)
    print(f"  Baseline: corr={baseline['range_corr']:.4f}  "
          f"bucket={baseline['bucket_acc']:.1%}  "
          f"recall={baseline['danger_recall']:.1%}  "
          f"danger_trade={baseline['danger_rate_trade']:.1%}  "
          f"({time.time()-t0:.0f}s)")
    print()

    # Leave-one-group-out
    print("LEAVE-ONE-GROUP-OUT (drop each group, measure impact)")
    print("-" * 80)
    print(f"{'Group':20s} {'N':>3s} {'RangeCorr':>10s} {'BucketAcc':>10s} "
          f"{'DangerRcl':>10s} {'DngTrade':>10s} {'Impact':>10s}")
    print("-" * 80)

    logo_results = {}
    for group_name in sorted(groups.keys()):
        group_cols = groups[group_name]
        remaining = [c for c in all_feat_cols if c not in group_cols]

        t0 = time.time()
        result = run_regime_validation_with_mask(features, remaining)
        elapsed = time.time() - t0

        impact = result["range_corr"] - baseline["range_corr"]
        logo_results[group_name] = (result, impact)

        print(f"{group_name:20s} {len(group_cols):3d} "
              f"{result['range_corr']:10.4f} {result['bucket_acc']:10.1%} "
              f"{result['danger_recall']:10.1%} {result['danger_rate_trade']:10.1%} "
              f"{impact:+10.4f}  ({elapsed:.0f}s)")

    # Single-group-only
    print()
    print("SINGLE-GROUP-ONLY (use only one group, measure standalone value)")
    print("-" * 80)
    print(f"{'Group':20s} {'N':>3s} {'RangeCorr':>10s} {'BucketAcc':>10s} "
          f"{'DangerRcl':>10s} {'DngTrade':>10s}")
    print("-" * 80)

    for group_name in sorted(groups.keys()):
        group_cols = groups[group_name]
        if len(group_cols) < 2:
            # Need at least 2 features for cosine
            extended = group_cols + ["spx_gap_pct"] if "spx_gap_pct" not in group_cols else group_cols + ["realized_vol_20d"]
        else:
            extended = group_cols

        t0 = time.time()
        result = run_regime_validation_with_mask(features, extended)
        elapsed = time.time() - t0

        print(f"{group_name:20s} {len(group_cols):3d} "
              f"{result['range_corr']:10.4f} {result['bucket_acc']:10.1%} "
              f"{result['danger_recall']:10.1%} {result['danger_rate_trade']:10.1%}"
              f"  ({elapsed:.0f}s)")

    # Summary
    print()
    print("SUMMARY")
    print("=" * 80)
    print("Most harmful to remove (biggest negative impact):")
    for name, (res, impact) in sorted(logo_results.items(), key=lambda x: x[1][1]):
        if impact < -0.005:
            print(f"  {name:20s} impact={impact:+.4f}  (removing hurts)")
    print()
    print("Safe to remove (positive or near-zero impact):")
    for name, (res, impact) in sorted(logo_results.items(), key=lambda x: x[1][1], reverse=True):
        if impact > -0.002:
            print(f"  {name:20s} impact={impact:+.4f}  (removing helps or neutral)")


if __name__ == "__main__":
    main()
