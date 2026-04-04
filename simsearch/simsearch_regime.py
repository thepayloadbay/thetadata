#!/usr/bin/env python3
"""SimSearch Regime Predictor — predict today's range/volatility regime.

Instead of predicting direction (coin-flip), predict:
1. Expected daily range (% from high to low)
2. Range bucket: CALM / NORMAL / VOLATILE / DANGER
3. Probability of a "danger day" (range > 1.5%)
4. Recommended action: TRADE / REDUCE / SKIP

This is automatable because:
- Volatility clusters (yesterday's range predicts today's)
- VIX level strongly predicts daily range
- GEX regime correlates with range (r=-0.57)
- We don't need direction — just "how much will it move?"

Usage:
    python simsearch_regime.py                          # today's prediction
    python simsearch_regime.py --date 20250310          # specific date
    python simsearch_regime.py --validate               # walk-forward backtest
    python simsearch_regime.py --validate --as-filter   # simulate as strategy filter
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
    DATA_DIR, LOGS_DIR, RUN_TS, TOP_K,
    VALIDATION_START, VALIDATION_END,
)
from simsearch_features import load_features
from simsearch_engine import SimSearchEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ============================================================
#  Range Buckets
# ============================================================
# Based on SPX historical range distribution:
#   P25 ~= 0.55%  (calm day)
#   P50 ~= 0.85%  (normal)
#   P75 ~= 1.25%  (volatile)
#   P90 ~= 1.75%  (danger)

RANGE_THRESHOLDS = {
    "CALM":     0.007,   # < 0.7%
    "NORMAL":   0.012,   # 0.7% - 1.2%
    "VOLATILE": 0.018,   # 1.2% - 1.8%
    # > 1.8% = DANGER
}

DANGER_THRESHOLD = 0.015  # 1.5% — above this, credit spreads at serious risk


def classify_range(range_pct: float) -> str:
    """Classify a daily range percentage into a bucket."""
    if range_pct < RANGE_THRESHOLDS["CALM"]:
        return "CALM"
    elif range_pct < RANGE_THRESHOLDS["NORMAL"]:
        return "NORMAL"
    elif range_pct < RANGE_THRESHOLDS["VOLATILE"]:
        return "VOLATILE"
    else:
        return "DANGER"


def recommend_action(pred_range: float, danger_prob: float,
                     pred_bucket_dist: dict) -> tuple[str, str]:
    """Generate trading recommendation from range prediction.

    Returns:
        (action, reason) where action is TRADE / REDUCE / SKIP
    """
    calm_pct = pred_bucket_dist.get("CALM", 0) + pred_bucket_dist.get("NORMAL", 0)

    if danger_prob >= 0.40:
        return "SKIP", f"40%+ of similar days were danger days ({danger_prob:.0%})"
    elif danger_prob >= 0.25:
        return "REDUCE", f"25-40% danger probability ({danger_prob:.0%}), reduce size"
    elif pred_range >= DANGER_THRESHOLD:
        return "REDUCE", f"predicted range {pred_range:.2%} above danger threshold"
    elif calm_pct >= 0.65:
        return "TRADE", f"{calm_pct:.0%} of similar days were calm/normal"
    elif pred_range < RANGE_THRESHOLDS["NORMAL"]:
        return "TRADE", f"predicted range {pred_range:.2%} in safe zone"
    else:
        return "TRADE", f"predicted range {pred_range:.2%} — normal conditions"


# ============================================================
#  Regime Prediction
# ============================================================

def predict_regime(engine: SimSearchEngine, date_str: str,
                   top_k: int = 10) -> dict:
    """Predict today's range regime from similar historical days.

    Args:
        engine: SimSearchEngine instance
        date_str: YYYYMMDD
        top_k: number of similar days to use

    Returns:
        dict with prediction details
    """
    try:
        matches = engine.find_similar(date_str, top_k=top_k, before_only=True,
                                       min_similarity=0.0)
    except (ValueError, Exception) as e:
        return {"error": str(e), "date": date_str}

    if matches.empty or "outcome_range_pct" not in matches.columns:
        return {"error": "no matches", "date": date_str}

    ranges = matches["outcome_range_pct"].dropna()
    if len(ranges) == 0:
        return {"error": "no range data in matches", "date": date_str}

    sims = matches.loc[ranges.index, "similarity"]
    weights = sims / sims.sum()

    # Range predictions
    pred_range_mean = ranges.mean()
    pred_range_weighted = (ranges * weights).sum()
    pred_range_median = ranges.median()

    # Bucket distribution
    buckets = ranges.apply(classify_range)
    bucket_dist = buckets.value_counts(normalize=True).to_dict()
    for b in ["CALM", "NORMAL", "VOLATILE", "DANGER"]:
        bucket_dist.setdefault(b, 0.0)

    # Danger probability
    danger_prob = (ranges >= DANGER_THRESHOLD).mean()
    danger_prob_weighted = ((ranges >= DANGER_THRESHOLD).astype(float) * weights).sum()

    # Predicted bucket (most common)
    pred_bucket = buckets.mode().iloc[0] if len(buckets) > 0 else "NORMAL"

    # Action recommendation
    action, reason = recommend_action(pred_range_weighted, danger_prob_weighted, bucket_dist)

    # Get actual outcome if available
    features = engine.features
    query_ts = pd.Timestamp(date_str)
    query_row = features[features["date"] == query_ts]
    actual_range = float(query_row["outcome_range_pct"].iloc[0]) if len(query_row) > 0 and pd.notna(query_row["outcome_range_pct"].iloc[0]) else None
    actual_bucket = classify_range(actual_range) if actual_range is not None else None

    return {
        "date": date_str,
        "pred_range_mean": pred_range_mean,
        "pred_range_weighted": pred_range_weighted,
        "pred_range_median": pred_range_median,
        "pred_bucket": pred_bucket,
        "bucket_dist": bucket_dist,
        "danger_prob": danger_prob,
        "danger_prob_weighted": danger_prob_weighted,
        "action": action,
        "reason": reason,
        "n_matches": len(ranges),
        "avg_similarity": sims.mean(),
        "actual_range": actual_range,
        "actual_bucket": actual_bucket,
        "match_dates": matches.head(5)["date"].dt.strftime("%Y-%m-%d").tolist(),
        "match_ranges": matches.head(5)["outcome_range_pct"].round(4).tolist(),
        "match_sims": matches.head(5)["similarity"].round(4).tolist(),
    }


def format_regime_prediction(pred: dict) -> str:
    """Format regime prediction as readable output."""
    if "error" in pred:
        return f"Error for {pred.get('date', '?')}: {pred['error']}"

    lines = []
    lines.append(f"SimSearch Regime Prediction for {pred['date']}")
    lines.append("=" * 60)

    # Action banner
    action = pred["action"]
    emoji_map = {"TRADE": "[GO]", "REDUCE": "[CAUTION]", "SKIP": "[STOP]"}
    lines.append(f"\n  {emoji_map.get(action, '')}  ACTION: {action}")
    lines.append(f"  Reason: {pred['reason']}")

    # Range prediction
    lines.append(f"\nRANGE PREDICTION")
    lines.append("-" * 40)
    lines.append(f"  Predicted range:  {pred['pred_range_weighted']:.3%} "
                 f"(mean: {pred['pred_range_mean']:.3%}, median: {pred['pred_range_median']:.3%})")
    lines.append(f"  Predicted bucket: {pred['pred_bucket']}")
    lines.append(f"  Danger prob:      {pred['danger_prob_weighted']:.0%}")

    # Bucket distribution
    lines.append(f"\n  Bucket breakdown (top-{pred['n_matches']} matches):")
    for b in ["CALM", "NORMAL", "VOLATILE", "DANGER"]:
        pct = pred["bucket_dist"].get(b, 0)
        bar = "#" * int(pct * 30)
        lines.append(f"    {b:10s} {pct:5.0%} {bar}")

    # Actual (if available)
    if pred["actual_range"] is not None:
        lines.append(f"\nACTUAL OUTCOME")
        lines.append("-" * 40)
        lines.append(f"  Actual range:  {pred['actual_range']:.3%}")
        lines.append(f"  Actual bucket: {pred['actual_bucket']}")
        correct = pred["pred_bucket"] == pred["actual_bucket"]
        lines.append(f"  Bucket match:  {'YES' if correct else 'NO'}")

    # Top matches
    lines.append(f"\nTOP MATCHES ({pred['n_matches']} total, avg sim: {pred['avg_similarity']:.3f})")
    lines.append("-" * 40)
    for i, (dt, rng, sim) in enumerate(zip(
        pred.get("match_dates", []),
        pred.get("match_ranges", []),
        pred.get("match_sims", [])
    )):
        bucket = classify_range(rng)
        lines.append(f"  #{i+1}: {dt}  range={rng:.3%}  [{bucket}]  sim={sim:.3f}")

    return "\n".join(lines)


# ============================================================
#  Walk-Forward Validation
# ============================================================

def validate_regime(start: str = VALIDATION_START,
                    end: str = VALIDATION_END,
                    top_k: int = 10) -> pd.DataFrame:
    """Walk-forward validation of regime predictions."""
    features = load_features()
    engine = SimSearchEngine(features)

    dates = features[
        (features["date"] >= pd.Timestamp(start)) &
        (features["date"] <= pd.Timestamp(end)) &
        (features["outcome_range_pct"].notna())
    ]["date"].sort_values()

    log.info("Regime validation: %d days, top_k=%d", len(dates), top_k)

    results = []
    t0 = time.time()

    for i, dt in enumerate(dates):
        ds = dt.strftime("%Y%m%d")
        pred = predict_regime(engine, ds, top_k=top_k)

        if "error" in pred:
            continue

        results.append({
            "date": dt,
            "pred_range": pred["pred_range_weighted"],
            "pred_bucket": pred["pred_bucket"],
            "danger_prob": pred["danger_prob_weighted"],
            "action": pred["action"],
            "actual_range": pred["actual_range"],
            "actual_bucket": pred["actual_bucket"],
            "n_matches": pred["n_matches"],
            "avg_similarity": pred["avg_similarity"],
        })

        if (i + 1) % 100 == 0:
            log.info("  Progress: %d/%d (%.1fs)", i + 1, len(dates), time.time() - t0)

    df = pd.DataFrame(results)
    log.info("Validation done: %d days in %.1fs", len(df), time.time() - t0)
    return df


def compute_regime_metrics(df: pd.DataFrame) -> dict:
    """Compute regime prediction metrics."""
    if df.empty:
        return {"error": "empty"}

    m = {"n_days": len(df)}

    # Range correlation
    valid = df[["pred_range", "actual_range"]].dropna()
    if len(valid) > 10:
        m["range_correlation"] = valid["pred_range"].corr(valid["actual_range"])
        m["range_rmse"] = np.sqrt(((valid["pred_range"] - valid["actual_range"])**2).mean())
        m["range_mae"] = (valid["pred_range"] - valid["actual_range"]).abs().mean()
    else:
        m["range_correlation"] = np.nan

    # Bucket accuracy
    bucket_correct = (df["pred_bucket"] == df["actual_bucket"]).mean()
    m["bucket_accuracy"] = bucket_correct

    # Adjacent bucket accuracy (within 1 step)
    bucket_order = {"CALM": 0, "NORMAL": 1, "VOLATILE": 2, "DANGER": 3}
    df["pred_ord"] = df["pred_bucket"].map(bucket_order)
    df["actual_ord"] = df["actual_bucket"].map(bucket_order)
    m["bucket_adjacent_accuracy"] = ((df["pred_ord"] - df["actual_ord"]).abs() <= 1).mean()

    # Danger day detection
    df["is_danger"] = (df["actual_range"] >= DANGER_THRESHOLD)
    df["pred_danger"] = (df["danger_prob"] >= 0.25)  # 25% threshold
    danger_days = df[df["is_danger"]]
    safe_days = df[~df["is_danger"]]

    if len(danger_days) > 0:
        # Recall: what % of actual danger days did we flag?
        m["danger_recall"] = danger_days["pred_danger"].mean()
        m["danger_days_total"] = len(danger_days)
    else:
        m["danger_recall"] = np.nan
        m["danger_days_total"] = 0

    if df["pred_danger"].sum() > 0:
        # Precision: what % of flagged days were actually dangerous?
        m["danger_precision"] = df[df["pred_danger"]]["is_danger"].mean()
        m["danger_flagged_total"] = int(df["pred_danger"].sum())
    else:
        m["danger_precision"] = np.nan
        m["danger_flagged_total"] = 0

    # Action distribution
    m["action_trade_pct"] = (df["action"] == "TRADE").mean()
    m["action_reduce_pct"] = (df["action"] == "REDUCE").mean()
    m["action_skip_pct"] = (df["action"] == "SKIP").mean()

    # P&L proxy: avg range on TRADE vs SKIP days
    trade_days = df[df["action"] == "TRADE"]
    skip_days = df[df["action"] != "TRADE"]
    if len(trade_days) > 0:
        m["avg_range_trade_days"] = trade_days["actual_range"].mean()
    if len(skip_days) > 0:
        m["avg_range_skip_days"] = skip_days["actual_range"].mean()

    # Danger rate on TRADE vs SKIP days
    if len(trade_days) > 0:
        m["danger_rate_trade_days"] = trade_days["is_danger"].mean()
    if len(skip_days) > 0:
        m["danger_rate_skip_days"] = skip_days["is_danger"].mean()

    # Naive baseline: predict yesterday's bucket
    df["naive_bucket"] = df["actual_bucket"].shift(1)
    naive_valid = df[df["naive_bucket"].notna()]
    if len(naive_valid) > 0:
        m["naive_bucket_accuracy"] = (naive_valid["naive_bucket"] == naive_valid["actual_bucket"]).mean()

    # Naive baseline: predict yesterday's range
    df["naive_range"] = df["actual_range"].shift(1)
    naive_range_valid = df[["naive_range", "actual_range"]].dropna()
    if len(naive_range_valid) > 10:
        m["naive_range_correlation"] = naive_range_valid["naive_range"].corr(
            naive_range_valid["actual_range"])

    return m


def format_regime_report(m: dict, df: pd.DataFrame) -> str:
    """Format regime validation report."""
    lines = []
    lines.append("=" * 70)
    lines.append("SimSearch REGIME Validation Report")
    lines.append("=" * 70)
    lines.append(f"\nDays: {m['n_days']}")
    lines.append("")

    lines.append("RANGE PREDICTION")
    lines.append("-" * 50)
    rc = m.get("range_correlation", np.nan)
    lines.append(f"  Range correlation:    {rc:.4f}" if pd.notna(rc) else "  Range correlation: N/A")
    lines.append(f"  Range RMSE:           {m.get('range_rmse', np.nan):.4%}")
    lines.append(f"  Range MAE:            {m.get('range_mae', np.nan):.4%}")
    nrc = m.get("naive_range_correlation", np.nan)
    if pd.notna(nrc):
        lines.append(f"  Naive baseline corr:  {nrc:.4f} (yesterday's range)")
        improvement = rc - nrc if pd.notna(rc) else 0
        lines.append(f"  Improvement:          {improvement:+.4f}")

    lines.append("")
    lines.append("BUCKET CLASSIFICATION")
    lines.append("-" * 50)
    lines.append(f"  Exact bucket accuracy:    {m.get('bucket_accuracy', 0):.1%}")
    lines.append(f"  Adjacent (within 1 step): {m.get('bucket_adjacent_accuracy', 0):.1%}")
    nb = m.get("naive_bucket_accuracy", np.nan)
    if pd.notna(nb):
        lines.append(f"  Naive baseline:           {nb:.1%} (yesterday's bucket)")

    lines.append("")
    lines.append("DANGER DAY DETECTION")
    lines.append("-" * 50)
    lines.append(f"  Actual danger days:  {m.get('danger_days_total', 0)} "
                 f"({m.get('danger_days_total', 0)/m['n_days']:.1%} of all days)")
    lines.append(f"  Flagged days:        {m.get('danger_flagged_total', 0)}")
    dr = m.get("danger_recall", np.nan)
    lines.append(f"  Recall:              {dr:.1%} (of danger days, how many flagged)"
                 if pd.notna(dr) else "  Recall: N/A")
    dp = m.get("danger_precision", np.nan)
    lines.append(f"  Precision:           {dp:.1%} (of flagged days, how many actually dangerous)"
                 if pd.notna(dp) else "  Precision: N/A")

    lines.append("")
    lines.append("ACTION RECOMMENDATIONS")
    lines.append("-" * 50)
    lines.append(f"  TRADE:  {m.get('action_trade_pct', 0):.0%} of days")
    lines.append(f"  REDUCE: {m.get('action_reduce_pct', 0):.0%} of days")
    lines.append(f"  SKIP:   {m.get('action_skip_pct', 0):.0%} of days")

    art = m.get("avg_range_trade_days", np.nan)
    ars = m.get("avg_range_skip_days", np.nan)
    if pd.notna(art) and pd.notna(ars):
        lines.append(f"\n  Avg range on TRADE days: {art:.3%}")
        lines.append(f"  Avg range on SKIP days:  {ars:.3%}")
        lines.append(f"  Separation:              {ars - art:+.3%}")

    drt = m.get("danger_rate_trade_days", np.nan)
    drs = m.get("danger_rate_skip_days", np.nan)
    if pd.notna(drt) and pd.notna(drs):
        lines.append(f"\n  Danger rate on TRADE days: {drt:.1%}")
        lines.append(f"  Danger rate on SKIP days:  {drs:.1%}")
        if drt > 0:
            lines.append(f"  Danger reduction:          {1 - drt/((drt*m.get('action_trade_pct',1) + drs*(1-m.get('action_trade_pct',1)))):.0%}"
                         if (drt + drs) > 0 else "")

    return "\n".join(lines)


# ============================================================
#  CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="SimSearch Regime Predictor")
    parser.add_argument("--date", type=str, help="Date to predict (YYYYMMDD)")
    parser.add_argument("--validate", action="store_true", help="Walk-forward validation")
    parser.add_argument("--start", type=str, default=VALIDATION_START)
    parser.add_argument("--end", type=str, default=VALIDATION_END)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    if args.validate:
        df = validate_regime(start=args.start, end=args.end, top_k=args.top_k)
        if df.empty:
            print("No results!")
            return
        m = compute_regime_metrics(df)
        print(format_regime_report(m, df))

        out = os.path.join(LOGS_DIR, f"regime_validation_{RUN_TS}.csv")
        df.to_csv(out, index=False)
        print(f"\nSaved: {out}")

    elif args.date:
        engine = SimSearchEngine()
        pred = predict_regime(engine, args.date, top_k=args.top_k)
        print(format_regime_prediction(pred))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
