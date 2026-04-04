#!/usr/bin/env python3
"""SimSearch — Historical Day Similarity Engine for SPX.

Find the most similar historical days to any given trading day,
analyze what unfolded on those days, and generate predictions.

Usage:
    # Build feature cache (first time)
    python simsearch.py --build

    # Query: find days similar to March 10, 2025
    python simsearch.py --date 20250310

    # Query with options
    python simsearch.py --date 20250310 --top-k 30 --tier 3 --method cosine

    # Run walk-forward validation
    python simsearch.py --validate

    # Validate specific range
    python simsearch.py --validate --start 2024-01-01 --end 2025-12-31

    # Show feature summary for a date
    python simsearch.py --inspect 20250310
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np
import pandas as pd

from simsearch_config import (
    FEATURE_CACHE, TOP_K, VALIDATION_START, VALIDATION_END, LOGS_DIR, RUN_TS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def cmd_build(args):
    """Build the feature cache."""
    from simsearch_features import build_all_features
    build_all_features(max_tier=args.tier, force=args.force)


def cmd_query(args):
    """Find similar days for a query date."""
    from simsearch_engine import SimSearchEngine
    from simsearch_predict import analyze_matches, format_prediction

    engine = SimSearchEngine()
    matches = engine.find_similar(
        args.date, top_k=args.top_k,
        max_tier=args.tier if args.tier else None,
        method=args.method,
    )

    if matches.empty:
        print(f"No similar days found for {args.date}")
        return

    pred = analyze_matches(matches)
    print(format_prediction(pred))

    # Detailed match table
    if args.verbose:
        print("\n\nDETAILED MATCHES")
        print("=" * 90)
        display_cols = ["date", "similarity", "outcome_return", "outcome_direction",
                        "outcome_range_pct", "vix_close", "spx_range_pct"]
        display_cols = [c for c in display_cols if c in matches.columns]
        print(matches[display_cols].to_string(index=False, float_format="%.4f"))

    # Save matches
    if args.save:
        out = os.path.join(LOGS_DIR, f"matches_{args.date}_{RUN_TS}.csv")
        matches.to_csv(out, index=False)
        print(f"\nMatches saved: {out}")


def cmd_validate(args):
    """Run walk-forward validation."""
    from simsearch_validate import run_validation, compute_metrics, format_validation_report

    results = run_validation(
        start=args.start or VALIDATION_START,
        end=args.end or VALIDATION_END,
        top_k=args.top_k,
        max_tier=args.tier if args.tier else None,
        method=args.method,
    )

    if results.empty:
        print("No validation results!")
        return

    metrics = compute_metrics(results)
    report = format_validation_report(metrics, results)
    print(report)

    # Save
    out = os.path.join(LOGS_DIR, f"validation_{RUN_TS}.csv")
    results.to_csv(out, index=False)
    print(f"\nResults saved: {out}")


def cmd_inspect(args):
    """Show feature vector for a specific date."""
    from simsearch_features import load_features
    from simsearch_engine import get_feature_tier

    features = load_features()
    query_ts = pd.Timestamp(args.inspect)
    row = features[features["date"] == query_ts]

    if row.empty:
        print(f"Date {args.inspect} not found in feature cache")
        return

    row = row.iloc[0]
    print(f"Feature Vector for {args.inspect}")
    print("=" * 60)

    for tier in [1, 2, 3]:
        tier_name = {1: "Price & Volatility", 2: "Intraday Shape", 3: "Options & Positioning"}
        print(f"\n--- Tier {tier}: {tier_name[tier]} ---")
        for col in sorted(features.columns):
            if get_feature_tier(col) == tier:
                val = row[col]
                if pd.isna(val):
                    print(f"  {col:40s}  NaN")
                elif isinstance(val, float):
                    print(f"  {col:40s}  {val:.6f}")
                else:
                    print(f"  {col:40s}  {val}")

    # Outcomes
    print(f"\n--- Outcomes ---")
    for col in sorted(features.columns):
        if col.startswith("outcome_"):
            val = row[col]
            if pd.notna(val):
                print(f"  {col:40s}  {val:.6f}")


def main():
    parser = argparse.ArgumentParser(
        description="SimSearch — Historical Day Similarity Engine for SPX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--build", action="store_true", help="Build feature cache")
    parser.add_argument("--date", type=str, help="Query date (YYYYMMDD)")
    parser.add_argument("--validate", action="store_true", help="Run walk-forward validation")
    parser.add_argument("--inspect", type=str, help="Inspect features for a date")

    # Common options
    parser.add_argument("--top-k", type=int, default=TOP_K, help=f"Number of matches (default: {TOP_K})")
    parser.add_argument("--tier", type=int, default=None, help="Max feature tier (1/2/3, auto if omitted)")
    parser.add_argument("--method", type=str, default="cosine",
                        choices=["cosine", "lorentzian", "mahalanobis"])
    parser.add_argument("--force", action="store_true", help="Force rebuild cache")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--save", "-s", action="store_true", help="Save results to file")

    # Validation range
    parser.add_argument("--start", type=str, help="Validation start date")
    parser.add_argument("--end", type=str, help="Validation end date")

    args = parser.parse_args()

    if args.build:
        cmd_build(args)
    elif args.date:
        cmd_query(args)
    elif args.validate:
        cmd_validate(args)
    elif args.inspect:
        cmd_inspect(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
