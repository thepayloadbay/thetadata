# Apex — Missing Data Log

## Summary

**No actionable missing data.** The 40 "missing" quote days in early 2022 are Tuesdays and Thursdays before Cboe launched daily SPX 0DTE expirations on **May 11, 2022**. There were simply no 0DTE options to trade on those days.

- **2022 Jan-May Tue/Thu**: No 0DTE existed. Not downloadable. Not missing.
- **2022 holidays** (MLK, Presidents, Good Friday, etc.): Market closed. Correct.
- **2023-2024**: 100% complete.
- **2025**: 100% complete (20251225 = Christmas, market closed).
- **2026**: Data through ~Mar 25 (partial year, ongoing).

## Backtest Integrity
The backtest is accurate. No trades are missed due to data gaps. The engine correctly skips days with no option quotes, which aligns with the historical reality that Tue/Thu 0DTE didn't exist before May 2022.
