#!/usr/bin/env python3
"""Sweep OA-compatible settings: touch, distance, entry time, width, sides.
Uses bar OPEN for strike selection (matches OA 99.8%) and bar LOW/HIGH for touch detection.
Saves results to sweep_oa_results.csv.
"""
import pandas as pd, os, math, numpy as np, csv, json

DATA_DIR = '/Users/jeerapongwongchote/Documents/thetadata/data'
MARKET_HOLIDAYS = {"20220117","20220221","20220415","20220530","20220620","20220704","20220905","20221124","20221226","20230102","20230116","20230220","20230407","20230529","20230619","20230704","20230904","20231123","20231225","20240101","20240115","20240219","20240329","20240527","20240619","20240704","20240902","20241128","20241225","20250101","20250120","20250217","20250418","20250526","20250704","20250901","20251127","20251225","20260101","20260119","20260216","20260403","20260525","20260703","20260907","20261126","20261225"}
EARLY_CLOSE = {"20221125","20231124","20241129","20241224","20251128","20251224","20261127","20261224"}
COMMISSION = 0.50

# Pre-load — only SPX OHLC (small), quotes loaded per-day lazily
print("Loading SPX OHLC data...")
spx_cache = {}
for year in range(2022, 2027):
    spx_dir = os.path.join(DATA_DIR, str(year), "spx_ohlc")
    if not os.path.exists(spx_dir): continue
    for fname in sorted(os.listdir(spx_dir)):
        if not fname.endswith('.parquet'): continue
        ds = fname.replace('.parquet','')
        if ds < "20220103" or ds > "20260325": continue
        if ds in MARKET_HOLIDAYS or ds in EARLY_CLOSE: continue
        q_path = os.path.join(DATA_DIR, ds[:4], "option_quotes", fname)
        if not os.path.exists(q_path): continue
        df = pd.read_parquet(os.path.join(spx_dir, fname))
        df['hhmm'] = df['timestamp'].str[11:16]
        spx_cache[ds] = df

print(f"Loaded {len(spx_cache)} days of SPX data")

# Lazy quote loading
_quote_cache = {}
def get_quotes(ds):
    if ds not in _quote_cache:
        path = os.path.join(DATA_DIR, ds[:4], "option_quotes", f"{ds}.parquet")
        df = pd.read_parquet(path)
        df['time_str'] = df['timestamp'].str[11:19]
        _quote_cache[ds] = df
        # Keep cache manageable
        if len(_quote_cache) > 50:
            oldest = list(_quote_cache.keys())[0]
            del _quote_cache[oldest]
    return _quote_cache[ds]

def run_sim(entry_hhmm="15:54", dist=0, width=5, touch=0, sides=["PUT"], risk=1500):
    pnls = []
    for ds, spx in spx_cache.items():
        entry_bar = spx[spx['hhmm'] == entry_hhmm]
        if entry_bar.empty: continue
        spot_open = float(entry_bar['open'].iloc[0])

        quotes = get_quotes(ds)

        for side in sides:
            if side == "PUT":
                short_strike = int(math.floor((spot_open - 0.01 - dist) / 5) * 5)
                long_strike = short_strike - int(width)
                right = 'P'
            else:
                short_strike = int(math.ceil((spot_open + 0.01 + dist) / 5) * 5)
                long_strike = short_strike + int(width)
                right = 'C'

            entry_ts = f"{entry_hhmm}:00"
            sq = quotes[(quotes['right']==right) & (quotes['strike']==short_strike) & (quotes['time_str']==entry_ts)]
            lq = quotes[(quotes['right']==right) & (quotes['strike']==long_strike) & (quotes['time_str']==entry_ts)]
            if sq.empty or lq.empty: continue
            credit = float(sq['bid'].iloc[0]) - float(lq['ask'].iloc[0])
            if credit < 0.05: continue

            max_loss_per = (width - credit) * 100
            qty = max(1, int(risk / max_loss_per)) if max_loss_per > 0 else 1

            entry_min = int(entry_hhmm.split(':')[0]) * 60 + int(entry_hhmm.split(':')[1])
            exited = False
            if touch is not None:
                for cm in range(entry_min + 1, 960):
                    h, m = cm // 60, cm % 60
                    hhmm = f"{h}:{m:02d}"
                    ts = f"{hhmm}:00"
                    bar = spx[spx['hhmm'] == hhmm]
                    if bar.empty: continue
                    if side == "PUT":
                        triggered = float(bar['low'].iloc[0]) <= short_strike + touch
                    else:
                        triggered = float(bar['high'].iloc[0]) >= short_strike - touch
                    if triggered:
                        csq = quotes[(quotes['right']==right) & (quotes['strike']==short_strike) & (quotes['time_str']==ts)]
                        clq = quotes[(quotes['right']==right) & (quotes['strike']==long_strike) & (quotes['time_str']==ts)]
                        if not csq.empty and not clq.empty:
                            close_cost = float(csq['ask'].iloc[0]) - float(clq['bid'].iloc[0])
                            pnls.append((credit - close_cost) * qty * 100 - 4 * qty * COMMISSION)
                            exited = True; break

            if not exited:
                exit_bar = spx[spx['hhmm'] == '16:00']
                if exit_bar.empty: exit_bar = spx[spx['hhmm'] == '15:59']
                spx_exit = float(exit_bar['close'].iloc[0]) if not exit_bar.empty else float(spx['close'].iloc[-1])
                if side == "PUT":
                    settle = max(0, min(width, short_strike - spx_exit)) if spx_exit < short_strike else 0
                else:
                    settle = max(0, min(width, spx_exit - short_strike)) if spx_exit > short_strike else 0
                pnls.append((credit - settle) * qty * 100 - 4 * qty * COMMISSION)

    if not pnls: return None
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    eq = [50000]
    for p in pnls: eq.append(eq[-1]+p)
    eq = np.array(eq); peak = np.maximum.accumulate(eq)
    dd = float((eq-peak).min())
    sharpe = np.mean(pnls)/np.std(pnls)*math.sqrt(min(len(pnls),252)) if np.std(pnls)>0 else 0
    pf = abs(sum(p for p in pnls if p>0)/sum(p for p in pnls if p<=0)) if any(p<=0 for p in pnls) else 999
    return {'pnl': round(total), 'dd': round(dd), 'wr': round(wins/len(pnls)*100,1), 'n': len(pnls), 'sharpe': round(sharpe,2), 'pf': round(pf,2), 'worst': round(min(pnls))}

# Run all sweeps
all_results = []
header = f"{'Config':<55} | {'P&L':>10} | {'DD':>8} | {'WR':>6} | {'#':>5} | {'Sharpe':>7} | {'PF':>6}"
divider = "-" * 110

configs = []
for et in ["15:52","15:53","15:54","15:55"]:
    for dist in [0, 1, 2, 3, 5]:
        for touch in [None, -0.5, 0, 0.5, 1.0, 2.0]:
            for sides in [["PUT"], ["CALL","PUT"]]:
                for w in [5, 10]:
                    side_label = "BOTH" if len(sides)==2 else "PUT"
                    t_label = "None" if touch is None else f"${touch:+.1f}"
                    configs.append((f"{side_label}, {et}, d={dist}, t={t_label}, w={w}", et, dist, w, touch, sides))

print(f"Running {len(configs)} combinations...")
for i, (label, et, dist, w, touch, sides) in enumerate(configs):
    if i % 20 == 0: print(f"  {i}/{len(configs)}...")
    m = run_sim(entry_hhmm=et, dist=dist, width=w, touch=touch if touch is not None else 999, sides=sides)
    if m:
        all_results.append({'label': label, 'entry': et, 'dist': dist, 'width': w,
                           'touch': touch, 'sides': "BOTH" if len(sides)==2 else "PUT", **m})

print(f"\nCompleted {len(all_results)} combinations")

# Save to CSV
out_path = '/Users/jeerapongwongchote/Documents/thetadata/lastfive/sweep_oa_results.csv'
with open(out_path, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['label','entry','dist','width','touch','sides','pnl','dd','wr','n','sharpe','pf','worst'])
    w.writeheader()
    w.writerows(all_results)
print(f"Saved to {out_path}")

# Print rankings
for rank_by, rank_key in [("P&L", "pnl"), ("SHARPE", "sharpe"), ("CALMAR (P&L/|DD|)", lambda x: x['pnl']/abs(x['dd']) if x['dd']!=0 else 0)]:
    print(f"\n{'='*110}")
    print(f"TOP 15 BY {rank_by}:")
    print(f"{'='*110}")
    print(header); print(divider)
    if callable(rank_key):
        ranked = sorted(all_results, key=rank_key, reverse=True)[:15]
    else:
        ranked = sorted(all_results, key=lambda x: -x[rank_key])[:15]
    for r in ranked:
        print(f"{r['label']:<55} | ${r['pnl']:>9,} | ${r['dd']:>7,} | {r['wr']:>5.1f}% | {r['n']:>5} | {r['sharpe']:>6.2f} | {r['pf']:>5.2f}")

print("\nDone!")
