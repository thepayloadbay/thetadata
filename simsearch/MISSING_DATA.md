# SimSearch — Missing Data & Workarounds

## Data Gaps

### SPX Daily OHLC (pre-2013)
- **Source**: `references/spx-history.csv` from CBOE — only goes back to late 2025 (recent ~60 days)
- **Source**: `data/spx_daily_close.csv` — need to check coverage
- **Workaround**: Build daily OHLC from 1-minute parquets (2013+). For pre-2013, use VIX history + any available daily CSV
- **Impact**: Tier 1 features limited to VIX-only before 2013 unless we find another SPX daily source

### SPX Volume
- The `volume` column in `spx_ohlc/*.parquet` is always 0 (index has no volume)
- **Workaround**: Use `count` column (tick count) as volume proxy. Or skip volume features
- **Impact**: Volume profile features will use tick count, not real volume

### GEX Data Gap
- GEX computed through 2026-03-25 only (need to run `compute_gex.py` for recent days)
- **Workaround**: Run backfill before using Tier 3 features on recent dates
- **Impact**: Minor — just need to run the compute script

### VIX Term Structure (pre-2022)
- VIX1D, VIX9D, VVIX only available from 2022
- **Workaround**: For pre-2022, use VIX level + VIX change as proxy. Term structure features set to NaN
- **Impact**: Tier 3 features unavailable pre-2022. Similarity matching auto-excludes NaN features

### Options Data (pre-2022)
- No option quotes, Greeks, or OI before 2022
- **Workaround**: None possible — options features simply unavailable
- **Impact**: Pre-2022 matching uses Tier 1+2 only. This is fine — the system gracefully degrades

### Cross-Asset Data
- No bond (TLT/ZB), gold (GLD), dollar (DXY), or sector data
- **Potential source**: Yahoo Finance via yfinance (same as VIX download)
- **Impact**: Macro-driven days harder to match without cross-asset context

---

## Data Quality Notes

1. **SPX OHLC timestamps**: Format `2025-01-03T09:30:00.000` — need to parse as Eastern time
2. **VIX history dates**: Format `01/02/1990` (MM/DD/YYYY) — different from parquet dates
3. **GEX per-strike vs aggregate**: GEX parquets have both per-strike rows and aggregate rows (where `right` is empty). Filter carefully
4. **OI is start-of-day**: Open interest is measured at market open, not intraday
5. **Greeks have NaN**: Some strikes have NaN gamma/vanna — need to handle gracefully

---

## Recommended Data Additions

| Data | Source | Difficulty | Value |
|------|--------|------------|-------|
| SPX daily OHLC (pre-2013) | Yahoo Finance | Easy | Extends Tier 1 to 1990 |
| Cross-asset (TLT, GLD, DXY) | Yahoo Finance | Easy | Macro context |
| Economic calendar | Manual or API | Medium | Event-day matching |
| Dark pool DIX/GEX (SqueezeMetrics) | Paid API | Hard | Institutional flow |
