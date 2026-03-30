"""
Validate PineScript MEDS indicator logic against:
1. TradingView 1-minute SPX data (signal generation)
2. Actual MEDS trade log from thetadata backtest (ground truth)

Compares: signal timing, direction, counts, VIX zone distribution
"""

import pandas as pd
import numpy as np
from datetime import time, datetime

# ─── Configuration (must match PineScript and meds_config.py) ───
WIDTH           = 20
QTY             = 2
MIN_NET_CREDIT  = 0.55
MIN_OTM         = 30.0
ENTRY_START     = time(9, 35)
ENTRY_END       = time(12, 45)
ENTRY_INTERVAL  = 20  # minutes
MAX_TRADES_DAY  = 10
COMMISSION      = 0.50
VIX_MAX_FILTER  = 35.0

# Dynamic SL zones
DYN_SL_VIX_LOW      = 13.0
DYN_SL_VIX_HI       = (25.0, 30.0)
DYN_SL_VIX_MID      = (13.0, 13.5)
DYN_SL_AMOUNT       = -800.0

# EOQ skip (last trading day of Mar/Jun/Sep/Dec)
ENABLE_EOQ_SKIP = True

# ─── Load TradingView 1-minute data ───
print("=" * 70)
print("MEDS PineScript Indicator Validation")
print("=" * 70)

tv1m = pd.read_csv("Tradingview_SP_SPX, 1m.csv")
tv1m.rename(columns={
    "VIX · TVC: close": "vix",
    "VIX1D · CBOE: close": "vix1d",
    "VIX9D · CBOE: close": "vix9d",
    "VVIX · CBOE: close": "vvix",
}, inplace=True)

# Parse timezone-aware timestamps — mixed offsets (-05/-04 for EST/EDT)
# Convert to UTC first, then to ET
tv1m["et_time"] = pd.to_datetime(tv1m["time"], utc=True).dt.tz_convert("America/New_York")
tv1m["date"] = tv1m["et_time"].dt.date
tv1m["time_only"] = tv1m["et_time"].dt.time
tv1m["hour"] = tv1m["et_time"].dt.hour
tv1m["minute"] = tv1m["et_time"].dt.minute

print(f"\nTradingView 1m data: {len(tv1m)} bars")
print(f"Date range: {tv1m['date'].min()} -> {tv1m['date'].max()}")
print(f"Unique trading days: {tv1m['date'].nunique()}")

# ─── Load Daily data for VIX (prior-day close) ───
tvd = pd.read_csv("Tradingview_SP_SPX, 1D.csv")
tvd.rename(columns={"VIX · TVC: close": "vix_daily"}, inplace=True)
tvd = tvd[tvd["vix_daily"].notna()].copy()
tvd["date"] = pd.to_datetime(tvd["time"]).dt.date
tvd = tvd.sort_values("date").reset_index(drop=True)

# Compute prior-day VIX change %
tvd["vix_prev"] = tvd["vix_daily"].shift(1)
tvd["vix_chg_pct"] = (tvd["vix_daily"] - tvd["vix_prev"]) / tvd["vix_prev"] * 100

# Build daily lookup: date -> {vix, vix_chg_pct, direction}
daily_lookup = {}
for _, row in tvd.iterrows():
    d = row["date"]
    vix = row["vix_daily"]
    chg = row["vix_chg_pct"]
    direction = "PUT" if (pd.notna(chg) and chg <= 0) else "CALL"
    daily_lookup[d] = {
        "vix": vix,
        "vix_chg_pct": chg,
        "direction": direction,
    }

# ─── Also load VIX from 1m data (intraday VIX for each bar) ───
# Fill forward VIX in 1m data (it's sparse)
tv1m["vix_ff"] = tv1m["vix"].ffill()

# Get daily VIX close from the last bar of each day in 1m data
daily_vix_from_1m = tv1m.groupby("date").agg(
    vix_last=("vix_ff", "last"),
    vix1d_last=("vix1d", "last"),
    vix9d_last=("vix9d", "last"),
    vvix_last=("vvix", "last"),
).to_dict("index")

# ─── Load actual MEDS trade log (ground truth) ───
tradelog = pd.read_csv("meds/logs/meft_v35_bidask_log_20260329_202932.csv")
tradelog["date"] = pd.to_datetime(tradelog["entry_date"], format="%Y%m%d").dt.date

# Filter to overlap period
tv_dates = set(tv1m["date"].unique())
tl_overlap = tradelog[tradelog["date"].isin(tv_dates)].copy()

print(f"\nMEDS trade log: {len(tradelog)} total trades, {len(tl_overlap)} in TV overlap period")
print(f"Overlap dates: {tl_overlap['date'].nunique()} trading days")

# ─── VIX Zone classifier ───
def vix_zone(v):
    if v is None or pd.isna(v):
        return "unknown"
    if v < 13:   return "< 13"
    if v < 15:   return "13-15"
    if v < 20:   return "15-20"
    if v < 25:   return "20-25"
    if v < 30:   return "25-30"
    if v < 35:   return "30-35"
    return "> 35"

def is_danger_zone(v):
    if v is None or pd.isna(v):
        return False
    return (v < DYN_SL_VIX_LOW or
            (v >= DYN_SL_VIX_HI[0] and v <= DYN_SL_VIX_HI[1]) or
            (v >= DYN_SL_VIX_MID[0] and v < DYN_SL_VIX_MID[1]))

def is_eoq_day(d):
    """Approximate: last trading day of quarter month."""
    if not ENABLE_EOQ_SKIP:
        return False
    if d.month not in (3, 6, 9, 12):
        return False
    # Check if next 3 calendar days include a new month
    for offset in range(1, 4):
        nd = d + pd.Timedelta(days=offset)
        if nd.month != d.month:
            return True
    return False

# ─── Simulate PineScript Signal Logic ───
print("\n" + "=" * 70)
print("Simulating PineScript entry signals on TradingView 1m data...")
print("=" * 70)

signals = []
days_grouped = tv1m.groupby("date")

for date, day_bars in days_grouped:
    # Get daily VIX info
    dl = daily_lookup.get(date)
    if dl is None:
        continue

    vix_today = dl["vix"]
    vix_chg = dl["vix_chg_pct"]
    direction = dl["direction"]

    # Skip filters
    if vix_today > VIX_MAX_FILTER:
        continue
    if is_eoq_day(date):
        continue

    danger = is_danger_zone(vix_today)
    zone = vix_zone(vix_today)

    daily_trades = 0

    for _, bar in day_bars.iterrows():
        t = bar["time_only"]
        h = bar["hour"]
        m = bar["minute"]

        # Entry window
        if t < ENTRY_START or t > ENTRY_END:
            continue

        # On interval: MEDS engine uses dt.minute % interval == 0
        # So entries fire at :00, :20, :40 — first valid bar in window is 9:40
        if m % ENTRY_INTERVAL != 0:
            continue

        if daily_trades >= MAX_TRADES_DAY:
            continue

        daily_trades += 1

        # Compute approximate strikes
        spot = bar["close"]
        if direction == "PUT":
            short_strike = int(round((spot - MIN_OTM) / 5.0) * 5)
            long_strike = short_strike - WIDTH
        else:
            short_strike = int(round((spot + MIN_OTM) / 5.0) * 5)
            long_strike = short_strike + WIDTH

        signals.append({
            "date": date,
            "time": t,
            "time_str": t.strftime("%H:%M:%S"),
            "direction": direction,
            "spot": spot,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "vix": vix_today,
            "vix_chg_pct": vix_chg,
            "vix_zone": zone,
            "danger_zone": danger,
            "vix1d": bar.get("vix1d"),
            "vix9d": bar.get("vix9d"),
            "vvix": bar.get("vvix"),
        })

signals_df = pd.DataFrame(signals)
print(f"\nPineScript signals generated: {len(signals_df)}")
print(f"Trading days with signals: {signals_df['date'].nunique()}")

# ─── Compare with Actual MEDS Trade Log ───
print("\n" + "=" * 70)
print("COMPARISON: PineScript Signals vs MEDS Trade Log")
print("=" * 70)

# Aggregate by date for comparison
ps_daily = signals_df.groupby("date").agg(
    ps_trades=("direction", "size"),
    ps_direction=("direction", "first"),
    ps_vix=("vix", "first"),
).reset_index()

tl_daily = tl_overlap.groupby("date").agg(
    tl_trades=("option_type", "size"),
    tl_direction=("option_type", "first"),
    tl_vix=("vix_level", "first"),
    tl_pnl=("pnl_earned", "sum"),
    tl_wins=("win", "sum"),
    tl_losses=("loss", "sum"),
).reset_index()

merged = pd.merge(ps_daily, tl_daily, on="date", how="outer", indicator=True)

print(f"\n{'Metric':<35} {'PineScript':>12} {'MEDS Log':>12} {'Delta':>10}")
print("-" * 70)

# Total signals/trades
ps_total = len(signals_df)
tl_total = len(tl_overlap)
print(f"{'Total entries':<35} {ps_total:>12} {tl_total:>12} {ps_total - tl_total:>+10}")

# Trading days
ps_days = signals_df["date"].nunique()
tl_days = tl_overlap["date"].nunique()
print(f"{'Trading days':<35} {ps_days:>12} {tl_days:>12} {ps_days - tl_days:>+10}")

# Avg trades per day
ps_avg = ps_total / ps_days if ps_days > 0 else 0
tl_avg = tl_total / tl_days if tl_days > 0 else 0
print(f"{'Avg entries/day':<35} {ps_avg:>12.1f} {tl_avg:>12.1f} {ps_avg - tl_avg:>+10.1f}")

# Direction split
ps_put = (signals_df["direction"] == "PUT").sum()
ps_call = (signals_df["direction"] == "CALL").sum()
tl_put = (tl_overlap["option_type"] == "PUT").sum()
tl_call = (tl_overlap["option_type"] == "CALL").sum()
print(f"{'PUT signals':<35} {ps_put:>12} {tl_put:>12} {ps_put - tl_put:>+10}")
print(f"{'CALL signals':<35} {ps_call:>12} {tl_call:>12} {ps_call - tl_call:>+10}")

# Direction agreement per day
both = merged[merged["_merge"] == "both"]
dir_match = (both["ps_direction"] == both["tl_direction"]).sum()
print(f"{'Direction match (days)':<35} {dir_match:>12} {'/ ' + str(len(both)):>12}")

# ─── Entry Time Comparison ───
print(f"\n--- Entry Time Distribution ---")
ps_times = signals_df.groupby("time_str").size()
tl_times = tl_overlap.groupby("entry_time").size()

all_times = sorted(set(ps_times.index) | set(tl_times.index))
print(f"\n{'Entry Time':<15} {'PineScript':>12} {'MEDS Log':>12} {'Match':>8}")
print("-" * 50)
for t in all_times:
    ps_c = ps_times.get(t, 0)
    tl_c = tl_times.get(t, 0)
    match = "OK" if ps_c > 0 and tl_c > 0 else "MISS"
    print(f"{t:<15} {ps_c:>12} {tl_c:>12} {match:>8}")

# ─── Note about entry time offset ───
# MEDS uses 9:40 as first entry (ENTRY_START=9:35 + first 20-min interval with data)
# PineScript fires at 9:35 if on-interval. Check if MEDS actually starts at 9:40.
print(f"\nNote: MEDS first entry time = {tl_overlap['entry_time'].min()}")
print(f"      PineScript first entry time = {signals_df['time_str'].min()}")

# ─── VIX Zone Distribution ───
print(f"\n--- VIX Zone Distribution ---")
ps_zones = signals_df.groupby("vix_zone").size()
tl_overlap["vix_zone"] = tl_overlap["vix_level"].apply(vix_zone)
tl_zones = tl_overlap.groupby("vix_zone").size()

# Known backtest WR by zone
known_wr = {"< 13": 86.7, "13-15": 93.0, "15-20": 97.8, "20-25": 99.2, "25-30": 72.1, "30-35": 98.8}

# Actual WR in overlap period from trade log
tl_wr_by_zone = tl_overlap.groupby("vix_zone")["win"].mean() * 100

zone_order = ["< 13", "13-15", "15-20", "20-25", "25-30", "30-35", "> 35"]
print(f"\n{'Zone':<10} {'PS Signals':>12} {'TL Trades':>12} {'TL WR%':>10} {'Backtest WR%':>14}")
print("-" * 62)
for z in zone_order:
    ps_z = ps_zones.get(z, 0)
    tl_z = tl_zones.get(z, 0)
    wr = tl_wr_by_zone.get(z, None)
    wr_str = f"{wr:.1f}%" if wr is not None else "N/A"
    bt_wr = known_wr.get(z, None)
    bt_str = f"{bt_wr:.1f}%" if bt_wr is not None else "N/A"
    print(f"{z:<10} {ps_z:>12} {tl_z:>12} {wr_str:>10} {bt_str:>14}")

# ─── Day-by-day Detail ───
print(f"\n--- Day-by-Day Comparison (overlap period) ---")
print(f"\n{'Date':<12} {'PS Dir':>8} {'TL Dir':>8} {'DirOK':>6} {'PS#':>5} {'TL#':>5} {'#OK':>5} {'TL PnL':>10} {'VIX':>8} {'Zone':>8} {'Danger':>7}")
print("-" * 95)

day_match_count = 0
trade_match_count = 0

for _, row in merged.sort_values("date").iterrows():
    d = row["date"]
    ps_dir = row.get("ps_direction", "—")
    tl_dir = row.get("tl_direction", "—")
    ps_n = int(row.get("ps_trades", 0)) if pd.notna(row.get("ps_trades")) else 0
    tl_n = int(row.get("tl_trades", 0)) if pd.notna(row.get("tl_trades")) else 0
    tl_pnl = row.get("tl_pnl", 0) if pd.notna(row.get("tl_pnl")) else 0
    vix = row.get("ps_vix") or row.get("tl_vix") or 0
    zone = vix_zone(vix) if vix else "?"
    danger = "YES" if is_danger_zone(vix) else ""

    dir_ok = "OK" if ps_dir == tl_dir else "MISS"
    n_ok = "OK" if ps_n == tl_n else f"{ps_n-tl_n:+d}"

    if ps_dir == tl_dir:
        day_match_count += 1
    if ps_n == tl_n:
        trade_match_count += 1

    print(f"{str(d):<12} {str(ps_dir):>8} {str(tl_dir):>8} {dir_ok:>6} {ps_n:>5} {tl_n:>5} {n_ok:>5} {tl_pnl:>10.0f} {vix:>8.2f} {zone:>8} {danger:>7}")

total_both = len(merged[merged["_merge"] == "both"])
print(f"\nDirection match rate: {day_match_count}/{total_both} = {day_match_count/total_both*100:.1f}%")
print(f"Trade count match rate: {trade_match_count}/{total_both} = {trade_match_count/total_both*100:.1f}%")

# ─── Danger Zone / SL Analysis ───
print(f"\n--- Danger Zone Analysis ---")
ps_danger = signals_df[signals_df["danger_zone"]]
tl_danger = tl_overlap[tl_overlap["vix_level"].apply(is_danger_zone)]
print(f"PineScript danger zone signals: {len(ps_danger)}")
print(f"MEDS danger zone trades: {len(tl_danger)}")
if len(tl_danger) > 0:
    print(f"MEDS danger zone WR: {tl_danger['win'].mean()*100:.1f}%")
    print(f"MEDS danger zone PnL: ${tl_danger['pnl_earned'].sum():.0f}")

# ─── P&L Summary (from trade log for the overlap period) ───
print(f"\n--- MEDS P&L Summary (overlap period) ---")
print(f"Total P&L: ${tl_overlap['pnl_earned'].sum():,.0f}")
print(f"Win Rate: {tl_overlap['win'].mean()*100:.1f}%")
print(f"Avg Win:  ${tl_overlap[tl_overlap['win']==1]['pnl_earned'].mean():.2f}")
avg_loss = tl_overlap[tl_overlap['loss']==1]['pnl_earned'].mean()
print(f"Avg Loss: ${avg_loss:.2f}" if pd.notna(avg_loss) else "Avg Loss: N/A")
print(f"Trades:   {len(tl_overlap)}")
print(f"Days:     {tl_overlap['date'].nunique()}")

# ─── KEY DISCREPANCIES TO FLAG ───
print(f"\n" + "=" * 70)
print("KEY FINDINGS & DISCREPANCIES")
print("=" * 70)

# 1. Entry time offset
ps_first = signals_df["time_str"].min()
tl_first = tl_overlap["entry_time"].min()
if ps_first != tl_first:
    print(f"\n[!] ENTRY TIME OFFSET: PineScript starts at {ps_first}, MEDS at {tl_first}")
    print(f"    MEDS uses ENTRY_START=9:35 but first valid 20-min interval bar is 9:40")
    print(f"    PineScript fires at 9:35 because 0 % 20 == 0")
    print(f"    -> FIX: Change PineScript entry start to 9:40 or adjust interval logic")

# 2. Direction mismatches
mismatches = both[both["ps_direction"] != both["tl_direction"]]
if len(mismatches) > 0:
    print(f"\n[!] DIRECTION MISMATCHES: {len(mismatches)} days")
    for _, row in mismatches.iterrows():
        print(f"    {row['date']}: PS={row['ps_direction']}, TL={row['tl_direction']} (VIX chg: TradingView daily vs thetadata may differ)")

# 3. Trade count differences
count_diff = both[both["ps_trades"] != both["tl_trades"]]
if len(count_diff) > 0:
    print(f"\n[!] TRADE COUNT MISMATCHES: {len(count_diff)} days")
    print(f"    Root causes: (1) PineScript can't check option chain availability,")
    print(f"    (2) MEDS skips bars when no spread meets MIN_CREDIT threshold,")
    print(f"    (3) SL batch closures reduce actual trade count")
    for _, row in count_diff.head(10).iterrows():
        print(f"    {row['date']}: PS={int(row['ps_trades'])}, TL={int(row['tl_trades'])}")

# 4. PineScript limitation note
print(f"\n[i] INHERENT PINESCRIPT LIMITATIONS:")
print(f"    - Cannot access option chain data (no actual credit/strike verification)")
print(f"    - Signals are ENTRY TIMING only — actual execution depends on live option chain")
print(f"    - P&L tracking requires option quotes unavailable in PineScript")
print(f"    - SL batch closure logic is simulated (no real MTM from option prices)")
print(f"    - PineScript signals should fire TradingView alerts -> your broker/bot executes")
