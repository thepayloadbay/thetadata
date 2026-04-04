#!/usr/bin/env python3
"""Run the best OA-confirmed config and compare against OA's $98,779.

Config: 3:52 PM, $3 OTM, Call Touch $0 / Put Touch -$1, BOTH sides, $5 width.
Uses bar OPEN for strike selection (matches OA 99.8%).
Uses bar LOW/HIGH for touch detection.
Reports yearly breakdown + overall totals.
"""
import pandas as pd, os, math, numpy as np, sys

DATA_DIR = '/Users/jeerapongwongchote/Documents/thetadata/data'
MARKET_HOLIDAYS = {"20220117","20220221","20220415","20220530","20220620","20220704","20220905","20221124","20221226","20230102","20230116","20230220","20230407","20230529","20230619","20230704","20230904","20231123","20231225","20240101","20240115","20240219","20240329","20240527","20240619","20240704","20240902","20241128","20241225","20250101","20250120","20250217","20250418","20250526","20250704","20250901","20251127","20251225","20260101","20260119","20260216","20260403","20260525","20260703","20260907","20261126","20261225"}
EARLY_CLOSE = {"20221125","20231124","20241129","20241224","20251128","20251224","20261127","20261224"}
COMMISSION = 0.50

# OA Best Config
ENTRY_HHMM = "15:52"
DIST = 3
WIDTH = 5
CALL_TOUCH = 0      # Exit when SPX reaches call strike
PUT_TOUCH = -1       # Exit when SPX goes $1 past put strike (ITM)
RISK = 1500

print("=" * 70)
print("OA BEST CONFIG BACKTEST")
print("=" * 70)
print(f"Entry: {ENTRY_HHMM} | Distance: ${DIST} OTM | Width: ${WIDTH}")
print(f"Call Touch: ${CALL_TOUCH} | Put Touch: ${PUT_TOUCH}")
print(f"Sides: BOTH | Risk: ${RISK}")
print("=" * 70)

# Load SPX data
print("\nLoading data...")
spx_cache = {}
quote_paths = {}
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
        # Check file size (>100KB = wide-range data)
        if os.path.getsize(q_path) < 100000: continue
        df = pd.read_parquet(os.path.join(spx_dir, fname))
        df['hhmm'] = df['timestamp'].str[11:16]
        spx_cache[ds] = df
        quote_paths[ds] = q_path

print(f"Loaded {len(spx_cache)} days with SPX + large quote files")

# Per-year stats
yearly = {}
all_trades = []

for ds in sorted(spx_cache.keys()):
    spx = spx_cache[ds]
    year = ds[:4]

    entry_bar = spx[spx['hhmm'] == ENTRY_HHMM]
    if entry_bar.empty: continue
    spot_open = float(entry_bar['open'].iloc[0])

    # Load quotes
    quotes = pd.read_parquet(quote_paths[ds])
    quotes['time_str'] = quotes['timestamp'].str[11:19]

    for side in ["CALL", "PUT"]:
        if side == "PUT":
            short_strike = int(math.floor((spot_open - 0.01 - DIST) / 5) * 5)
            long_strike = short_strike - WIDTH
            right = 'P'
            touch_val = PUT_TOUCH
        else:
            short_strike = int(math.ceil((spot_open + 0.01 + DIST) / 5) * 5)
            long_strike = short_strike + WIDTH
            right = 'C'
            touch_val = CALL_TOUCH

        entry_ts = f"{ENTRY_HHMM}:00"
        sq = quotes[(quotes['right']==right) & (quotes['strike']==short_strike) & (quotes['time_str']==entry_ts)]
        lq = quotes[(quotes['right']==right) & (quotes['strike']==long_strike) & (quotes['time_str']==entry_ts)]
        if sq.empty or lq.empty: continue
        credit = float(sq['bid'].iloc[0]) - float(lq['ask'].iloc[0])
        if credit < 0.05: continue

        max_loss_per = (WIDTH - credit) * 100
        qty = max(1, int(RISK / max_loss_per)) if max_loss_per > 0 else 1

        # Touch detection
        entry_min = int(ENTRY_HHMM.split(':')[0]) * 60 + int(ENTRY_HHMM.split(':')[1])
        exited = False
        exit_reason = "expire"
        exit_time = "16:00"

        for cm in range(entry_min + 1, 960):  # up to 16:00
            h, m = cm // 60, cm % 60
            hhmm = f"{h}:{m:02d}"
            ts = f"{hhmm}:00"
            bar = spx[spx['hhmm'] == hhmm]
            if bar.empty: continue

            if side == "PUT":
                triggered = float(bar['low'].iloc[0]) <= short_strike + touch_val
            else:
                triggered = float(bar['high'].iloc[0]) >= short_strike - touch_val

            if triggered:
                csq = quotes[(quotes['right']==right) & (quotes['strike']==short_strike) & (quotes['time_str']==ts)]
                clq = quotes[(quotes['right']==right) & (quotes['strike']==long_strike) & (quotes['time_str']==ts)]
                if not csq.empty and not clq.empty:
                    close_cost = float(csq['ask'].iloc[0]) - float(clq['bid'].iloc[0])
                    pnl = (credit - close_cost) * qty * 100 - 4 * qty * COMMISSION
                    exited = True
                    exit_reason = "touch"
                    exit_time = hhmm
                    break

        if not exited:
            exit_bar = spx[spx['hhmm'] == '16:00']
            if exit_bar.empty: exit_bar = spx[spx['hhmm'] == '15:59']
            spx_exit = float(exit_bar['close'].iloc[0]) if not exit_bar.empty else float(spx['close'].iloc[-1])
            if side == "PUT":
                settle = max(0, min(WIDTH, short_strike - spx_exit)) if spx_exit < short_strike else 0
            else:
                settle = max(0, min(WIDTH, spx_exit - short_strike)) if spx_exit > short_strike else 0
            pnl = (credit - settle) * qty * 100 - 4 * qty * COMMISSION

        trade = {
            'date': ds, 'year': year, 'side': side, 'short_strike': short_strike,
            'credit': credit, 'qty': qty, 'pnl': pnl, 'exit': exit_reason, 'exit_time': exit_time
        }
        all_trades.append(trade)

        if year not in yearly:
            yearly[year] = {'pnls': [], 'wins': 0, 'touches': 0, 'call_trades': 0, 'put_trades': 0}
        yearly[year]['pnls'].append(pnl)
        if pnl > 0: yearly[year]['wins'] += 1
        if exit_reason == 'touch': yearly[year]['touches'] += 1
        if side == 'CALL': yearly[year]['call_trades'] += 1
        else: yearly[year]['put_trades'] += 1

# Overall stats
all_pnls = [t['pnl'] for t in all_trades]
total_pnl = sum(all_pnls)
total_wins = sum(1 for p in all_pnls if p > 0)
total_touches = sum(1 for t in all_trades if t['exit'] == 'touch')

eq = [50000]
for p in all_pnls: eq.append(eq[-1] + p)
eq = np.array(eq)
peak = np.maximum.accumulate(eq)
max_dd = float((eq - peak).min())
sharpe = np.mean(all_pnls)/np.std(all_pnls)*math.sqrt(min(len(all_pnls),252)) if np.std(all_pnls)>0 else 0
pf = abs(sum(p for p in all_pnls if p>0)/sum(p for p in all_pnls if p<=0)) if any(p<=0 for p in all_pnls) else 999
calmar = total_pnl / abs(max_dd) if max_dd != 0 else 999

# Print yearly breakdown
print(f"\n{'Year':>4} | {'Trades':>6} | {'C/P':>7} | {'WR':>6} | {'Touch':>5} | {'P&L':>10} | {'Max DD':>9} | {'Sharpe':>7}")
print("-" * 75)

for year in sorted(yearly.keys()):
    y = yearly[year]
    n = len(y['pnls'])
    wr = y['wins']/n*100 if n else 0
    ypnl = sum(y['pnls'])
    yeq = [0]
    for p in y['pnls']: yeq.append(yeq[-1]+p)
    yeq = np.array(yeq); ypk = np.maximum.accumulate(yeq)
    ydd = float((yeq-ypk).min())
    ysh = np.mean(y['pnls'])/np.std(y['pnls'])*math.sqrt(min(n,252)) if np.std(y['pnls'])>0 else 0
    print(f"{year:>4} | {n:>6} | {y['call_trades']:>3}/{y['put_trades']:<3} | {wr:>5.1f}% | {y['touches']:>5} | ${ypnl:>9,.0f} | ${ydd:>8,.0f} | {ysh:>6.2f}")

print("-" * 75)
print(f"{'ALL':>4} | {len(all_pnls):>6} | {sum(1 for t in all_trades if t['side']=='CALL'):>3}/{sum(1 for t in all_trades if t['side']=='PUT'):<3} | {total_wins/len(all_pnls)*100:>5.1f}% | {total_touches:>5} | ${total_pnl:>9,.0f} | ${max_dd:>8,.0f} | {sharpe:>6.2f}")

print(f"\n{'='*70}")
print(f"OVERALL RESULTS")
print(f"{'='*70}")
print(f"Total P&L:      ${total_pnl:>12,.0f}")
print(f"Max Drawdown:   ${max_dd:>12,.0f}")
print(f"Sharpe Ratio:   {sharpe:>12.2f}")
print(f"Calmar Ratio:   {calmar:>12.2f}")
print(f"Profit Factor:  {pf:>12.2f}")
print(f"Win Rate:       {total_wins/len(all_pnls)*100:>11.1f}%")
print(f"Total Trades:   {len(all_pnls):>12}")
print(f"Touch Exits:    {total_touches:>12} ({total_touches/len(all_pnls)*100:.1f}%)")
print(f"Avg Win:        ${np.mean([p for p in all_pnls if p>0]):>12,.0f}")
print(f"Avg Loss:       ${np.mean([p for p in all_pnls if p<=0]):>12,.0f}")
print(f"Worst Trade:    ${min(all_pnls):>12,.0f}")

print(f"\n{'='*70}")
print(f"COMPARISON vs OA ($98,779)")
print(f"{'='*70}")
print(f"Our backtest:   ${total_pnl:>12,.0f}")
print(f"OA result:      $     98,779")
print(f"Difference:     ${total_pnl - 98779:>12,.0f}")
print(f"Coverage:       {len(spx_cache)} days with wide-range quotes")

# Top 10 worst trades
print(f"\nTOP 10 WORST TRADES:")
worst = sorted(all_trades, key=lambda t: t['pnl'])[:10]
for t in worst:
    print(f"  {t['date']} {t['side']:>4} strike={t['short_strike']} cr={t['credit']:.2f} qty={t['qty']} exit={t['exit']:>6} at {t['exit_time']} => ${t['pnl']:>8,.0f}")

# Save trade log
trade_df = pd.DataFrame(all_trades)
out_path = '/Users/jeerapongwongchote/Documents/thetadata/lastfive/oa_best_trades.csv'
trade_df.to_csv(out_path, index=False)
print(f"\nTrade log saved to {out_path}")
