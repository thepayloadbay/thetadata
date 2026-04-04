#!/usr/bin/env python3
"""SimSearch Prediction & Analysis — analyze outcomes on matched days.

Given a set of similar historical days, generate predictions about:
- Direction (up/down)
- Expected range
- Expected return distribution
- Conditional probabilities (e.g., "80% of similar days closed green")
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def analyze_matches(matches: pd.DataFrame) -> dict:
    """Analyze outcomes on matched days to generate predictions.

    Args:
        matches: DataFrame from SimSearchEngine.find_similar() with outcome columns

    Returns:
        dict with prediction summary
    """
    if matches.empty:
        return {"error": "No matches to analyze"}

    query_date = matches.attrs.get("query_date", "unknown")
    n = len(matches)

    results = {
        "query_date": query_date,
        "n_matches": n,
        "avg_similarity": matches["similarity"].mean(),
        "max_tier": matches.attrs.get("max_tier", "?"),
        "n_features": matches.attrs.get("n_features", "?"),
        "n_candidates": matches.attrs.get("n_candidates", "?"),
    }

    # --- Same-day outcomes on matched days ---
    if "outcome_return" in matches.columns:
        returns = matches["outcome_return"].dropna()
        if len(returns) > 0:
            results["pred_return_mean"] = returns.mean()
            results["pred_return_median"] = returns.median()
            results["pred_return_std"] = returns.std()

            # Weighted by similarity
            weights = matches.loc[returns.index, "similarity"]
            weights = weights / weights.sum()
            results["pred_return_weighted"] = (returns * weights).sum()

    if "outcome_direction" in matches.columns:
        dirs = matches["outcome_direction"].dropna()
        if len(dirs) > 0:
            up_pct = (dirs > 0).mean()
            down_pct = (dirs < 0).mean()
            flat_pct = (dirs == 0).mean()
            results["pred_direction_up_pct"] = up_pct
            results["pred_direction_down_pct"] = down_pct
            results["pred_direction_flat_pct"] = flat_pct
            results["pred_direction"] = "UP" if up_pct > 0.55 else ("DOWN" if down_pct > 0.55 else "NEUTRAL")
            results["pred_direction_confidence"] = max(up_pct, down_pct)

            # Weighted vote
            weights = matches.loc[dirs.index, "similarity"]
            weighted_dir = (np.sign(dirs) * weights).sum() / weights.sum()
            results["pred_direction_weighted_score"] = weighted_dir

    if "outcome_range_pct" in matches.columns:
        ranges = matches["outcome_range_pct"].dropna()
        if len(ranges) > 0:
            results["pred_range_mean"] = ranges.mean()
            results["pred_range_median"] = ranges.median()
            results["pred_range_p25"] = ranges.quantile(0.25)
            results["pred_range_p75"] = ranges.quantile(0.75)

    # --- Next-day outcomes (forward prediction) ---
    if "outcome_next_return" in matches.columns:
        next_returns = matches["outcome_next_return"].dropna()
        if len(next_returns) > 0:
            results["pred_next_return_mean"] = next_returns.mean()
            results["pred_next_return_median"] = next_returns.median()

    if "outcome_next_direction" in matches.columns:
        next_dirs = matches["outcome_next_direction"].dropna()
        if len(next_dirs) > 0:
            next_up = (next_dirs > 0).mean()
            results["pred_next_direction_up_pct"] = next_up
            results["pred_next_direction"] = "UP" if next_up > 0.55 else ("DOWN" if next_up < 0.45 else "NEUTRAL")

    if "outcome_next_range_pct" in matches.columns:
        next_ranges = matches["outcome_next_range_pct"].dropna()
        if len(next_ranges) > 0:
            results["pred_next_range_mean"] = next_ranges.mean()

    # --- Risk assessment ---
    if "outcome_return" in matches.columns:
        returns = matches["outcome_return"].dropna()
        if len(returns) > 0:
            results["risk_worst_return"] = returns.min()
            results["risk_best_return"] = returns.max()
            results["risk_pct_below_neg1pct"] = (returns < -0.01).mean()
            results["risk_pct_above_1pct"] = (returns > 0.01).mean()

    # --- Top/bottom match details ---
    results["top_3_dates"] = matches.head(3)["date"].dt.strftime("%Y-%m-%d").tolist()
    results["top_3_similarities"] = matches.head(3)["similarity"].round(4).tolist()
    if "outcome_return" in matches.columns:
        results["top_3_returns"] = matches.head(3)["outcome_return"].round(5).tolist()

    return results


def format_prediction(pred: dict) -> str:
    """Format prediction dict as readable text."""
    if "error" in pred:
        return f"Error: {pred['error']}"

    lines = []
    lines.append(f"SimSearch Results for {pred['query_date']}")
    lines.append("=" * 60)
    lines.append(f"Matches: {pred['n_matches']} days (avg similarity: {pred['avg_similarity']:.4f})")
    lines.append(f"Features: {pred['n_features']} (Tier {pred['max_tier']}), Candidates: {pred['n_candidates']}")
    lines.append("")

    # Same-day prediction
    lines.append("SAME-DAY PREDICTION")
    lines.append("-" * 40)
    if "pred_direction" in pred:
        conf = pred.get("pred_direction_confidence", 0)
        lines.append(f"Direction:  {pred['pred_direction']} ({conf:.0%} confidence)")
        lines.append(f"  Up: {pred.get('pred_direction_up_pct', 0):.0%}  "
                     f"Down: {pred.get('pred_direction_down_pct', 0):.0%}  "
                     f"Flat: {pred.get('pred_direction_flat_pct', 0):.0%}")
    if "pred_return_mean" in pred:
        lines.append(f"Exp. Return: {pred['pred_return_mean']:.4%} (median: {pred['pred_return_median']:.4%})")
        lines.append(f"Weighted:    {pred.get('pred_return_weighted', 0):.4%}")
    if "pred_range_mean" in pred:
        lines.append(f"Exp. Range:  {pred['pred_range_mean']:.4%} "
                     f"(P25: {pred['pred_range_p25']:.4%}, P75: {pred['pred_range_p75']:.4%})")

    # Next-day prediction
    if "pred_next_direction" in pred:
        lines.append("")
        lines.append("NEXT-DAY PREDICTION")
        lines.append("-" * 40)
        lines.append(f"Direction:  {pred['pred_next_direction']} "
                     f"(up: {pred.get('pred_next_direction_up_pct', 0):.0%})")
        if "pred_next_return_mean" in pred:
            lines.append(f"Exp. Return: {pred['pred_next_return_mean']:.4%}")

    # Risk
    if "risk_worst_return" in pred:
        lines.append("")
        lines.append("RISK PROFILE")
        lines.append("-" * 40)
        lines.append(f"Best match return:  {pred['risk_best_return']:+.4%}")
        lines.append(f"Worst match return: {pred['risk_worst_return']:+.4%}")
        lines.append(f"Days below -1%:     {pred.get('risk_pct_below_neg1pct', 0):.0%}")
        lines.append(f"Days above +1%:     {pred.get('risk_pct_above_1pct', 0):.0%}")

    # Top matches
    lines.append("")
    lines.append("TOP MATCHES")
    lines.append("-" * 40)
    for i, (dt, sim) in enumerate(zip(pred.get("top_3_dates", []),
                                       pred.get("top_3_similarities", []))):
        ret = pred.get("top_3_returns", [None]*3)[i]
        ret_str = f"{ret:+.4%}" if ret is not None else "N/A"
        lines.append(f"  #{i+1}: {dt}  sim={sim:.4f}  return={ret_str}")

    return "\n".join(lines)
