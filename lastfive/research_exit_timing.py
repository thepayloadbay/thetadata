#!/usr/bin/env python3
"""
Research: Does exiting BEFORE settlement (15:57, 15:58, 15:59) improve results
vs holding to 16:00 expiration?

Hypothesis: The 16:00 settlement bar can have whipsaw/pin risk. Exiting 1-2 min
early at a small cost might avoid some losing trades where SPX spikes in the
final minute.
"""

import pandas as pd
import numpy as np
import os
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
DATA_DIR = Path("/Users/jeerapongwongchote/Documents/thetadata/data")
TRADE_LOG = Path("/Users/jeerapongwongchote/Documents/thetadata/lastfive/tradelogs_lastfive.csv")
SPREAD_WIDTH = 5.0
COMMISSION_PER_LEG = 0.50  # per contract per leg
EXIT_TIMES = ["15:57:00", "15:58:00", "15:59:00"]
STARTING_EQUITY = 50000.0

# ── Load trades ─────────────────────────────────────────────────────────────
trades = pd.read_csv(TRADE_LOG)
print(f"Loaded {len(trades)} trades from {trades['date'].iloc[0]} to {trades['date'].iloc[-1]}")

# ── Helper: load option quote for a strike/right/time ───────────────────────
def load_quotes_for_date(date_int):
    """Load option_quotes parquet for a given date (int like 20240102)."""
    date_str = str(date_int)
    year = date_str[:4]
    fpath = DATA_DIR / year / "option_quotes" / f"{date_str}.parquet"
    if not fpath.exists():
        return None
    return pd.read_parquet(fpath)


def get_quote(df_quotes, strike, right, time_str):
    """Get bid/ask for a specific strike, right, time from pre-loaded quotes."""
    mask = (df_quotes['strike'] == int(strike)) & \
           (df_quotes['right'] == right) & \
           (df_quotes['timestamp'].str[11:19] == time_str)
    rows = df_quotes[mask]
    if len(rows) == 0:
        return None, None
    row = rows.iloc[0]
    return row['bid'], row['ask']


# ── Compute P&L for each exit scenario ──────────────────────────────────────
results = {t: [] for t in EXIT_TIMES}
results["16:00:00 (settlement)"] = []  # baseline from trade log

skipped = {t: 0 for t in EXIT_TIMES}
quote_cache = {}

for idx, trade in trades.iterrows():
    date_int = trade['date']
    short_strike = trade['short_strike']
    long_strike = trade['long_strike']
    qty = trade['qty']
    credit = trade['credit_received']
    commission = COMMISSION_PER_LEG * 2 * qty  # 2 legs

    # Baseline: settlement at 16:00 (from trade log)
    baseline_pnl = trade['pnl_earned']
    results["16:00:00 (settlement)"].append({
        'date': date_int,
        'pnl': baseline_pnl,
        'credit': credit,
        'exit_cost': trade['spread_value_exit'],
        'qty': qty,
    })

    # Load quotes for early exit times
    if date_int not in quote_cache:
        quote_cache[date_int] = load_quotes_for_date(date_int)

    df_q = quote_cache[date_int]
    if df_q is None:
        for t in EXIT_TIMES:
            skipped[t] += 1
        continue

    for exit_time in EXIT_TIMES:
        # Buying back: pay ask for short leg, receive bid for long leg
        short_bid, short_ask = get_quote(df_q, short_strike, 'C', exit_time)
        long_bid, long_ask = get_quote(df_q, long_strike, 'C', exit_time)

        if short_ask is None or long_bid is None:
            skipped[exit_time] += 1
            # Fall back to settlement P&L if quotes missing
            results[exit_time].append({
                'date': date_int,
                'pnl': baseline_pnl,
                'credit': credit,
                'exit_cost': trade['spread_value_exit'],
                'qty': qty,
                'fallback': True,
            })
            continue

        # Cost to buy back the spread = short_ask - long_bid
        # (we pay ask to buy back short, receive bid to sell back long)
        buyback_cost = short_ask - long_bid
        # Floor at 0 — can't have negative cost (would mean free money)
        buyback_cost = max(buyback_cost, 0.0)

        pnl = (credit - buyback_cost) * qty * 100 - commission

        results[exit_time].append({
            'date': date_int,
            'pnl': pnl,
            'credit': credit,
            'exit_cost': buyback_cost,
            'qty': qty,
            'fallback': False,
        })

    # Free memory periodically
    if idx % 50 == 0 and idx > 0:
        # Keep only last 5 dates in cache
        dates_to_keep = set(trades['date'].iloc[max(0, idx-5):idx+5])
        quote_cache = {k: v for k, v in quote_cache.items() if k in dates_to_keep}

print(f"\nQuote availability:")
for t in EXIT_TIMES:
    n_fallback = sum(1 for r in results[t] if r.get('fallback', False))
    print(f"  {t}: {skipped[t]} dates missing parquet, {n_fallback} missing quotes (used settlement fallback)")

# ── Compute metrics ─────────────────────────────────────────────────────────
def compute_metrics(trade_list):
    pnls = [t['pnl'] for t in trade_list]
    pnls = np.array(pnls)
    total_pnl = pnls.sum()
    n_trades = len(pnls)
    wins = (pnls > 0).sum()
    losses = (pnls <= 0).sum()
    win_rate = wins / n_trades * 100 if n_trades > 0 else 0

    # Equity curve and max drawdown
    equity = STARTING_EQUITY + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    max_dd = dd.min()

    # Average win / average loss
    avg_win = pnls[pnls > 0].mean() if wins > 0 else 0
    avg_loss = pnls[pnls <= 0].mean() if losses > 0 else 0

    # Sharpe (annualized, ~252 trading days)
    if pnls.std() > 0:
        sharpe = (pnls.mean() / pnls.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # Calmar
    if max_dd < 0:
        calmar = (total_pnl / abs(max_dd))
    else:
        calmar = float('inf')

    return {
        'Total P&L': f"${total_pnl:,.0f}",
        'Total P&L raw': total_pnl,
        'Trades': n_trades,
        'Wins': wins,
        'Win Rate': f"{win_rate:.1f}%",
        'Avg Win': f"${avg_win:,.0f}",
        'Avg Loss': f"${avg_loss:,.0f}",
        'Max DD': f"${max_dd:,.0f}",
        'Max DD raw': max_dd,
        'Sharpe': f"{sharpe:.2f}",
        'Calmar': f"{calmar:.2f}",
    }


# ── Print comparison ────────────────────────────────────────────────────────
all_scenarios = ["16:00:00 (settlement)"] + EXIT_TIMES
metrics = {}
for scenario in all_scenarios:
    metrics[scenario] = compute_metrics(results[scenario])

print("\n" + "=" * 90)
print("EXIT TIMING COMPARISON: Early Exit vs Settlement")
print("=" * 90)

header_labels = {
    "16:00:00 (settlement)": "16:00 Settlement (baseline)",
    "15:57:00": "15:57 Exit",
    "15:58:00": "15:58 Exit",
    "15:59:00": "15:59 Exit",
}

fields = ['Total P&L', 'Trades', 'Wins', 'Win Rate', 'Avg Win', 'Avg Loss', 'Max DD', 'Sharpe', 'Calmar']

# Print as table
col_width = 22
print(f"\n{'Metric':<16}", end="")
for s in all_scenarios:
    print(f"{header_labels[s]:>{col_width}}", end="")
print()
print("-" * (16 + col_width * len(all_scenarios)))

for field in fields:
    print(f"{field:<16}", end="")
    for s in all_scenarios:
        print(f"{str(metrics[s][field]):>{col_width}}", end="")
    print()

# ── Delta analysis: which trades changed? ───────────────────────────────────
print("\n" + "=" * 90)
print("TRADE-BY-TRADE DELTA: Where early exit HELPED or HURT vs settlement")
print("=" * 90)

for exit_time in EXIT_TIMES:
    early = results[exit_time]
    baseline = results["16:00:00 (settlement)"]

    deltas = []
    for i in range(len(baseline)):
        delta = early[i]['pnl'] - baseline[i]['pnl']
        if abs(delta) > 0.01:  # non-trivial difference
            deltas.append({
                'date': baseline[i]['date'],
                'baseline_pnl': baseline[i]['pnl'],
                'early_pnl': early[i]['pnl'],
                'delta': delta,
                'credit': baseline[i]['credit'],
                'exit_cost_baseline': baseline[i]['exit_cost'],
                'exit_cost_early': early[i]['exit_cost'],
            })

    helped = [d for d in deltas if d['delta'] > 0]
    hurt = [d for d in deltas if d['delta'] < 0]
    helped_total = sum(d['delta'] for d in helped)
    hurt_total = sum(d['delta'] for d in hurt)

    print(f"\n--- {exit_time} vs Settlement ---")
    print(f"  Trades where early exit HELPED: {len(helped)}  (total saved: ${helped_total:,.0f})")
    print(f"  Trades where early exit HURT:   {len(hurt)}  (total cost: ${hurt_total:,.0f})")
    print(f"  Net effect: ${helped_total + hurt_total:,.0f}")

    # Show top 5 biggest helps and hurts
    if helped:
        print(f"\n  Top 5 biggest saves (early exit helped):")
        for d in sorted(helped, key=lambda x: -x['delta'])[:5]:
            print(f"    {d['date']}: baseline=${d['baseline_pnl']:,.0f} -> early=${d['early_pnl']:,.0f}  "
                  f"(saved ${d['delta']:,.0f})  credit={d['credit']:.2f}  "
                  f"exit_cost: {d['exit_cost_baseline']:.2f} -> {d['exit_cost_early']:.2f}")

    if hurt:
        print(f"\n  Top 5 biggest costs (early exit hurt):")
        for d in sorted(hurt, key=lambda x: x['delta'])[:5]:
            print(f"    {d['date']}: baseline=${d['baseline_pnl']:,.0f} -> early=${d['early_pnl']:,.0f}  "
                  f"(cost ${d['delta']:,.0f})  credit={d['credit']:.2f}  "
                  f"exit_cost: {d['exit_cost_baseline']:.2f} -> {d['exit_cost_early']:.2f}")

# ── Losing trades only analysis ─────────────────────────────────────────────
print("\n" + "=" * 90)
print("FOCUS: Settlement LOSERS — Did early exit save them?")
print("=" * 90)

baseline_trades = results["16:00:00 (settlement)"]
losers_idx = [i for i, t in enumerate(baseline_trades) if t['pnl'] <= 0]
print(f"\nBaseline has {len(losers_idx)} losing trades out of {len(baseline_trades)} total")

for exit_time in EXIT_TIMES:
    early = results[exit_time]
    saved = 0
    still_lost = 0
    saved_amount = 0
    for i in losers_idx:
        if early[i]['pnl'] > 0:
            saved += 1
            saved_amount += early[i]['pnl'] - baseline_trades[i]['pnl']
        else:
            still_lost += 1

    print(f"\n  {exit_time}: Converted {saved}/{len(losers_idx)} losers to winners (saved ${saved_amount:,.0f})")
    print(f"           Still lost: {still_lost}/{len(losers_idx)}")

print("\nDone.")
