# CLAUDE.md — SimSearch Project

model: opus

## Project Goal

**SimSearch** — Historical Day Similarity Engine for SPX. Given today's market conditions, find the N most similar historical days and analyze what unfolded on those days to predict today's price action.

The core insight: markets are driven by human behavior, and humans repeat patterns. Days with similar volatility regimes, options positioning, price structure, and momentum tend to produce similar outcomes.

---

## Architecture

```
simsearch/
├── simsearch.py              # Main CLI entry point
├── simsearch_config.py       # All parameters and paths
├── simsearch_features.py     # Feature extraction (daily vectors)
├── simsearch_engine.py       # Similarity matching engine
├── simsearch_predict.py      # Outcome analysis & prediction
├── simsearch_validate.py     # Walk-forward backtesting
├── simsearch_reporting.py    # Display & formatting
├── CLAUDE.md                 # This file
├── TODO.md                   # Research backlog
├── RESEARCH.md               # Findings and results
├── MISSING_DATA.md           # Data gaps and workarounds
└── logs/                     # Output files
```

---

## Data Sources & Coverage

| Data | Coverage | Features Extracted |
|------|----------|-------------------|
| SPX daily OHLC | 1990-2026 | Returns, range, gaps, trend |
| VIX daily OHLC | 1990-2026 | Level, change, term structure proxy |
| SPX 1-minute OHLC | 2013-2026 | Intraday shape, volume profile, EMA |
| VIX 1-minute OHLC | 2022-2026 | Intraday vol dynamics |
| SPXW Greeks (full) | 2022-2026 | IV skew, gamma surface, vanna |
| GEX/VEX computed | 2022-2026 | Dealer positioning, regime |
| Open Interest | 2022-2026 | Put/call OI ratio, concentration |
| VIX1D/VIX9D/VVIX | 2022-2026 | Term structure, vol-of-vol |

### Feature Tiers

- **Tier 1 (1990+):** SPX returns, range, gaps, VIX level/change, day-of-week, month
- **Tier 2 (2013+):** Intraday SPX shape (morning range, afternoon drift, volume profile)
- **Tier 3 (2022+):** Options Greeks, GEX regime, IV skew, term structure, VVIX

Similarity matching uses only features available for both the query day and candidate day.

---

## Similarity Metrics

1. **Cosine similarity** on z-scored feature vectors (primary)
2. **Mahalanobis distance** for correlation-aware matching
3. **DTW (Dynamic Time Warping)** on intraday price curves (optional, expensive)

---

## Validation Approach

- **Walk-forward**: for each day D, find top-K similar days using only data before D
- **No look-ahead**: feature extraction uses only data available at time of query
- **Metrics**: directional accuracy, range prediction RMSE, P&L if used as a signal
- **Baseline**: random day selection (should be ~50% directional accuracy)

---

## Environment

- Python 3, pandas, numpy, scipy, scikit-learn
- Data root: `../data/` (parquet files)
- References: `../references/spx-history.csv`, `../data/vix_history.csv`

---

## Behavior Rules

- Show results immediately after any computation finishes
- No look-ahead bias — all features computed from data available before the query time
- Feature vectors must be z-scored before similarity computation
- Always report confidence intervals alongside point predictions
