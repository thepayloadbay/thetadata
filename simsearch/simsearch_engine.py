#!/usr/bin/env python3
"""SimSearch Similarity Engine — find historical days most similar to a query day.

Supports multiple similarity metrics:
- Cosine similarity on z-scored feature vectors (primary)
- Weighted cosine with feature importance
- Mahalanobis distance for correlation-aware matching

Usage:
    from simsearch_engine import SimSearchEngine
    engine = SimSearchEngine()
    results = engine.find_similar("20250310", top_k=20)
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd
from scipy.spatial.distance import cosine, mahalanobis
from sklearn.preprocessing import StandardScaler

from simsearch_config import (
    TOP_K, MIN_SIMILARITY, LOOKBACK_DAYS,
    TIER_WEIGHTS, FEATURE_WEIGHTS,
    TIER1_START, TIER2_START, TIER3_START,
)
from simsearch_features import load_features

log = logging.getLogger(__name__)


# Feature tier classification
TIER1_PREFIXES = ("spx_prev", "spx_gap", "vix_prev_", "vix_percentile",
                  "vix_zscore", "spx_above", "spx_rsi", "spx_dist", "realized_vol",
                  "vix_spx_corr", "vix_momentum",
                  "spx_streak", "range_change",
                  "tlt_prev", "gld_prev", "uup_prev", "iwm_prev",
                  "dow", "month", "is_fomc", "is_triple", "is_month", "is_quarter",
                  "days_to")
TIER2_PREFIXES = ("prev_morning_", "prev_midday_", "prev_afternoon_",
                  "prev_high_time", "prev_low_time",
                  "prev_reversal_", "prev_trend_", "prev_max_1min",
                  "prev_vol_ratio", "prev_close_location")
TIER3_PREFIXES = ("vix1d_", "vix9d_", "vvix_", "net_gex", "call_gex", "put_gex",
                  "net_vex", "gex_plus", "gex_regime", "iv_", "gamma_conc",
                  "put_call_oi", "total_oi", "call_wall", "put_wall")
OUTCOME_PREFIXES = ("outcome_",)
META_COLS = {"date", "spx_open", "spx_high", "spx_low", "spx_close", "bar_count",
             "vix_open", "vix_high", "vix_low"}


def get_feature_tier(col: str) -> int:
    """Classify a column into its feature tier."""
    if any(col.startswith(p) for p in TIER3_PREFIXES):
        return 3
    if any(col.startswith(p) for p in TIER2_PREFIXES):
        return 2
    if any(col.startswith(p) for p in TIER1_PREFIXES):
        return 1
    return 0  # meta or outcome


def get_feature_cols(df: pd.DataFrame, max_tier: int = 3) -> list[str]:
    """Get feature columns up to max_tier, excluding outcomes and meta."""
    cols = []
    for c in df.columns:
        tier = get_feature_tier(c)
        if 1 <= tier <= max_tier:
            cols.append(c)
    return cols


class SimSearchEngine:
    """Core similarity search engine."""

    def __init__(self, features: pd.DataFrame | None = None):
        if features is None:
            features = load_features()
        self.features = features.copy()
        self.features["date"] = pd.to_datetime(self.features["date"])
        self._scaler = None
        self._scaled_matrix = None
        self._feature_cols = None
        log.info("SimSearchEngine loaded: %d days, %d columns",
                 len(self.features), len(self.features.columns))

    def _prepare_matrix(self, feature_cols: list[str],
                        candidate_mask: np.ndarray) -> tuple[np.ndarray, StandardScaler]:
        """Z-score normalize features using only candidate data for fitting."""
        data = self.features.loc[candidate_mask, feature_cols].copy()

        # Replace inf with NaN, then fill NaN with column median
        data = data.replace([np.inf, -np.inf], np.nan)
        medians = data.median()
        data = data.fillna(medians)
        # Handle any remaining NaN (if entire column was NaN)
        data = data.fillna(0)

        scaler = StandardScaler()
        scaler.fit(data)

        # Transform ALL rows (including query)
        all_data = self.features[feature_cols].replace([np.inf, -np.inf], np.nan)
        all_data = all_data.fillna(medians).fillna(0)
        scaled = scaler.transform(all_data)

        return scaled, scaler

    def _compute_feature_weights(self, feature_cols: list[str]) -> np.ndarray:
        """Build weight vector from tier weights and feature-specific overrides."""
        weights = np.ones(len(feature_cols))
        for i, col in enumerate(feature_cols):
            tier = get_feature_tier(col)
            w = TIER_WEIGHTS.get(tier, 1.0)
            w *= FEATURE_WEIGHTS.get(col, 1.0)
            weights[i] = w
        return weights

    def find_similar(self, query_date: str, top_k: int = TOP_K,
                     max_tier: int | None = None,
                     method: str = "cosine",
                     min_similarity: float = MIN_SIMILARITY,
                     lookback_days: int | None = LOOKBACK_DAYS,
                     exclude_adjacent: int = 5,
                     before_only: bool = True) -> pd.DataFrame:
        """Find the top-K most similar historical days to query_date.

        Args:
            query_date: YYYYMMDD string
            top_k: number of results
            max_tier: limit to feature tier (auto-detect if None)
            method: "cosine" or "mahalanobis"
            min_similarity: minimum score to include
            lookback_days: only look at N days before query (None = all)
            exclude_adjacent: skip N days before/after query (avoid autocorrelation)
            before_only: only match days BEFORE query (for walk-forward)

        Returns:
            DataFrame with columns: date, similarity, rank, + outcome columns
        """
        query_ts = pd.Timestamp(query_date)
        query_idx = self.features[self.features["date"] == query_ts].index
        if len(query_idx) == 0:
            raise ValueError(f"Date {query_date} not found in feature cache")
        query_idx = query_idx[0]

        # Auto-detect max tier based on query date data availability
        if max_tier is None:
            query_row = self.features.iloc[query_idx]
            tier3_cols = [c for c in self.features.columns
                         if any(c.startswith(p) for p in TIER3_PREFIXES)]
            tier2_cols = [c for c in self.features.columns
                         if any(c.startswith(p) for p in TIER2_PREFIXES)]

            if tier3_cols and query_row[tier3_cols].notna().sum() > len(tier3_cols) * 0.3:
                max_tier = 3
            elif tier2_cols and query_row[tier2_cols].notna().sum() > len(tier2_cols) * 0.3:
                max_tier = 2
            else:
                max_tier = 1
            log.info("Auto-detected max_tier=%d for %s", max_tier, query_date)

        feature_cols = get_feature_cols(self.features, max_tier)
        if not feature_cols:
            raise ValueError("No feature columns found")

        # Build candidate mask
        n = len(self.features)
        candidate_mask = np.ones(n, dtype=bool)
        candidate_mask[query_idx] = False  # exclude query itself

        if before_only:
            candidate_mask[self.features["date"] >= query_ts] = False

        if exclude_adjacent > 0:
            adj_start = max(0, query_idx - exclude_adjacent)
            adj_end = min(n, query_idx + exclude_adjacent + 1)
            candidate_mask[adj_start:adj_end] = False

        if lookback_days is not None:
            cutoff = query_ts - pd.Timedelta(days=lookback_days)
            candidate_mask[self.features["date"] < cutoff] = False

        # Note: we do NOT filter out candidates missing Tier 2/3 features.
        # Pre-2022 candidates match on Tier 1 features only (NaN features
        # get median-imputed, which means they contribute 0 to similarity).
        # This lets 30 years of history participate as candidates.

        candidate_indices = np.where(candidate_mask)[0]
        if len(candidate_indices) == 0:
            log.warning("No candidates found for %s", query_date)
            return pd.DataFrame()

        log.info("Searching %d candidates with %d features (tier %d)",
                 len(candidate_indices), len(feature_cols), max_tier)

        # Prepare normalized data
        scaled, scaler = self._prepare_matrix(feature_cols, candidate_mask)
        weights = self._compute_feature_weights(feature_cols)

        # Track which features are REAL (not imputed) for each row
        raw_data = self.features[feature_cols].replace([np.inf, -np.inf], np.nan)
        has_real_data = raw_data.notna().values  # shape (n_rows, n_features)

        query_vec = scaled[query_idx] * weights
        query_real = has_real_data[query_idx]

        # Compute similarities
        scores = np.full(n, -np.inf)

        if method == "cosine":
            for idx in candidate_indices:
                cand_vec = scaled[idx] * weights
                cand_real = has_real_data[idx]
                # Only use features where BOTH query and candidate have real data
                valid = query_real & cand_real & np.isfinite(query_vec) & np.isfinite(cand_vec)
                if valid.sum() < 5:
                    continue
                sim = 1.0 - cosine(query_vec[valid], cand_vec[valid])
                scores[idx] = sim

        elif method == "lorentzian":
            # Lorentzian distance: d = sum(log(1 + |x_i - y_i|))
            # Compresses outliers — superior to Euclidean for financial data.
            # Convert to similarity: sim = 1 / (1 + d)
            for idx in candidate_indices:
                cand_vec = scaled[idx] * weights
                cand_real = has_real_data[idx]
                valid = query_real & cand_real & np.isfinite(query_vec) & np.isfinite(cand_vec)
                if valid.sum() < 5:
                    continue
                d = np.sum(np.log(1.0 + np.abs(query_vec[valid] - cand_vec[valid])))
                scores[idx] = 1.0 / (1.0 + d)

        elif method == "mahalanobis":
            # Use covariance from candidate data
            cand_data = scaled[candidate_indices] * weights
            cand_data = np.nan_to_num(cand_data, nan=0.0)
            cov = np.cov(cand_data.T)
            # Regularize
            cov += np.eye(cov.shape[0]) * 0.01
            try:
                cov_inv = np.linalg.inv(cov)
            except np.linalg.LinAlgError:
                cov_inv = np.linalg.pinv(cov)

            q = np.nan_to_num(query_vec, nan=0.0)
            for idx in candidate_indices:
                c = np.nan_to_num(scaled[idx] * weights, nan=0.0)
                dist = mahalanobis(q, c, cov_inv)
                scores[idx] = 1.0 / (1.0 + dist)  # Convert distance to similarity

        # Rank and select top-K
        valid_scores = [(i, scores[i]) for i in candidate_indices if scores[i] > min_similarity]
        valid_scores.sort(key=lambda x: x[1], reverse=True)
        top = valid_scores[:top_k]

        if not top:
            log.warning("No matches above min_similarity=%.2f", min_similarity)
            return pd.DataFrame()

        # Build result DataFrame
        result_rows = []
        for rank, (idx, score) in enumerate(top, 1):
            row = self.features.iloc[idx].to_dict()
            row["similarity"] = score
            row["rank"] = rank
            result_rows.append(row)

        result = pd.DataFrame(result_rows)

        # Add query info
        query_row = self.features.iloc[query_idx]
        result.attrs["query_date"] = query_date
        result.attrs["query_return"] = query_row.get("outcome_return", np.nan)
        result.attrs["query_direction"] = query_row.get("outcome_direction", np.nan)
        result.attrs["query_range"] = query_row.get("outcome_range_pct", np.nan)
        result.attrs["max_tier"] = max_tier
        result.attrs["n_candidates"] = len(candidate_indices)
        result.attrs["n_features"] = len(feature_cols)

        return result

    def find_similar_to_features(self, query_features: dict, top_k: int = TOP_K,
                                  max_tier: int = 3,
                                  before_date: str | None = None) -> pd.DataFrame:
        """Find similar days given a raw feature dict (for live/real-time use).

        Args:
            query_features: dict of feature_name -> value
            top_k: number of results
            max_tier: max feature tier to use
            before_date: only match days before this date (YYYYMMDD)
        """
        feature_cols = get_feature_cols(self.features, max_tier)
        available = [c for c in feature_cols if c in query_features and
                     pd.notna(query_features[c])]

        if len(available) < 5:
            raise ValueError(f"Only {len(available)} features available, need at least 5")

        # Build candidate mask
        n = len(self.features)
        candidate_mask = np.ones(n, dtype=bool)
        if before_date:
            candidate_mask[self.features["date"] >= pd.Timestamp(before_date)] = False

        candidate_indices = np.where(candidate_mask)[0]
        if len(candidate_indices) == 0:
            return pd.DataFrame()

        # Normalize using candidate stats
        data = self.features.loc[candidate_mask, available].copy()
        medians = data.median()
        means = data.mean()
        stds = data.std().replace(0, 1)

        # Build query vector (z-scored)
        query_vec = np.array([(query_features[c] - means[c]) / stds[c] for c in available])
        weights = np.array([FEATURE_WEIGHTS.get(c, 1.0) * TIER_WEIGHTS.get(get_feature_tier(c), 1.0)
                           for c in available])
        query_vec *= weights

        # Score candidates
        scores = {}
        for idx in candidate_indices:
            cand_raw = self.features.loc[self.features.index[idx], available].values.astype(float)
            cand_raw = np.where(np.isnan(cand_raw), medians[available].values, cand_raw)
            cand_vec = ((cand_raw - means.values) / stds.values) * weights
            valid = np.isfinite(query_vec) & np.isfinite(cand_vec)
            if valid.sum() < 5:
                continue
            sim = 1.0 - cosine(query_vec[valid], cand_vec[valid])
            scores[idx] = sim

        # Top-K
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        result_rows = []
        for rank, (idx, score) in enumerate(top, 1):
            row = self.features.iloc[idx].to_dict()
            row["similarity"] = score
            row["rank"] = rank
            result_rows.append(row)

        return pd.DataFrame(result_rows)
