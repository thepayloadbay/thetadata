"""
Deep-dive analysis of losing trades in the LastFive strategy.
Compares feature distributions between wins and losses to find filterable patterns.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = Path("/Users/jeerapongwongchote/Documents/thetadata/data")
TRADES_CSV = Path("/Users/jeerapongwongchote/Documents/thetadata/lastfive/tradelogs_lastfive.csv")

def load_ohlc(date_str, kind="spx_ohlc"):
    """Load 1-min OHLC for a given date. kind = 'spx_ohlc' or 'vix_ohlc'."""
    year = date_str[:4]
    p = DATA_DIR / year / kind / f"{date_str}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df['ts'] = pd.to_datetime(df['timestamp'])
    df['time'] = df['ts'].dt.strftime('%H:%M')
    return df

def get_prev_trading_date(date_str, all_dates):
    """Get previous trading date from sorted list."""
    idx = all_dates.index(date_str) if date_str in all_dates else -1
    if idx > 0:
        return all_dates[idx - 1]
    return None

def compute_vwap_approx(spx_df):
    """Approximate VWAP using volume-weighted close. If no volume, use simple mean of closes."""
    day = spx_df[spx_df['time'] <= '15:55']
    if day.empty:
        return np.nan
    if day['volume'].sum() > 0:
        return np.average(day['close'], weights=day['volume'])
    else:
        # Use typical price as proxy
        return day['close'].mean()

def main():
    trades = pd.read_csv(TRADES_CSV)
    trades['date'] = trades['date'].astype(str)

    # Collect all unique dates for prev-close lookups
    all_spx_dates = sorted(set(trades['date'].tolist()))
    # Also add surrounding dates for prev close lookups
    # Build a broader date list from data directories
    broad_dates = set()
    for year_dir in DATA_DIR.iterdir():
        ohlc_dir = year_dir / "spx_ohlc"
        if ohlc_dir.exists():
            for f in ohlc_dir.glob("*.parquet"):
                broad_dates.add(f.stem)
    broad_dates = sorted(broad_dates)

    features = []

    for i, row in trades.iterrows():
        date_str = row['date']
        dt = datetime.strptime(date_str, '%Y%m%d')

        spx = load_ohlc(date_str, "spx_ohlc")
        vix = load_ohlc(date_str, "vix_ohlc")

        feat = {
            'date': date_str,
            'win': row['win'],
            'loss': row['loss'],
            'pnl': row['pnl_earned'],
            'credit': row['credit_received'],
            'spx_at_entry': row['spx_at_entry'],
            'spx_at_exit': row['spx_at_exit'],
            'short_strike': row['short_strike'],
            'long_strike': row['long_strike'],
            'qty': row['qty'],
            'vix_level': row['vix_level'],
            'day_of_week': dt.weekday(),  # 0=Mon, 4=Fri
            'day_name': dt.strftime('%A'),
            'month': dt.month,
            'is_friday': int(dt.weekday() == 4),
        }

        # Strike distance
        feat['strike_distance'] = row['short_strike'] - row['spx_at_entry']

        if spx is not None:
            # SPX move 15:55 -> 16:00
            bar_1555 = spx[spx['time'] == '15:55']
            bar_1600 = spx[spx['time'] == '16:00']

            if not bar_1555.empty and not bar_1600.empty:
                spx_1555 = bar_1555.iloc[0]['close']
                spx_1600 = bar_1600.iloc[0]['close']
                feat['spx_move_1555_1600'] = spx_1600 - spx_1555
                feat['spx_move_1555_1600_pct'] = (spx_1600 - spx_1555) / spx_1555 * 100
            else:
                feat['spx_move_1555_1600'] = np.nan
                feat['spx_move_1555_1600_pct'] = np.nan

            # High/Low between 15:55 and 16:00
            damage_window = spx[(spx['time'] >= '15:55') & (spx['time'] <= '16:00')]
            if not damage_window.empty:
                feat['damage_high'] = damage_window['high'].max()
                feat['damage_low'] = damage_window['low'].min()
                feat['damage_range'] = feat['damage_high'] - feat['damage_low']
            else:
                feat['damage_high'] = feat['damage_low'] = feat['damage_range'] = np.nan

            # Gap from open (open vs prev close)
            open_bar = spx[spx['time'] == '09:30']
            prev_date = get_prev_trading_date(date_str, broad_dates)
            if prev_date and not open_bar.empty:
                prev_spx = load_ohlc(prev_date, "spx_ohlc")
                if prev_spx is not None:
                    prev_close_bar = prev_spx[prev_spx['time'] == '16:00']
                    if not prev_close_bar.empty:
                        prev_close = prev_close_bar.iloc[0]['close']
                        today_open = open_bar.iloc[0]['open']
                        feat['gap_from_open'] = today_open - prev_close
                        feat['gap_from_open_pct'] = (today_open - prev_close) / prev_close * 100
                    else:
                        feat['gap_from_open'] = feat['gap_from_open_pct'] = np.nan
                else:
                    feat['gap_from_open'] = feat['gap_from_open_pct'] = np.nan
            else:
                feat['gap_from_open'] = feat['gap_from_open_pct'] = np.nan

            # SPX range in last 30 min before entry (15:25-15:54) — volatility proxy
            pre_entry = spx[(spx['time'] >= '15:25') & (spx['time'] <= '15:54')]
            if not pre_entry.empty:
                feat['pre_entry_range'] = pre_entry['high'].max() - pre_entry['low'].min()
                feat['pre_entry_range_pct'] = feat['pre_entry_range'] / pre_entry['close'].mean() * 100
            else:
                feat['pre_entry_range'] = feat['pre_entry_range_pct'] = np.nan

            # VWAP distance at 15:55
            vwap_val = compute_vwap_approx(spx)
            if not np.isnan(vwap_val) and not bar_1555.empty:
                feat['vwap_distance'] = bar_1555.iloc[0]['close'] - vwap_val
                feat['vwap_distance_pct'] = feat['vwap_distance'] / vwap_val * 100
            else:
                feat['vwap_distance'] = feat['vwap_distance_pct'] = np.nan
        else:
            for k in ['spx_move_1555_1600', 'spx_move_1555_1600_pct', 'damage_high', 'damage_low',
                       'damage_range', 'gap_from_open', 'gap_from_open_pct', 'pre_entry_range',
                       'pre_entry_range_pct', 'vwap_distance', 'vwap_distance_pct']:
                feat[k] = np.nan

        # VIX rising or falling during the day
        if vix is not None:
            vix_open_bar = vix[(vix['time'] >= '09:31') & (vix['time'] <= '09:35')]
            vix_1555 = vix[vix['time'] == '15:55']
            if not vix_open_bar.empty and not vix_1555.empty:
                vix_open = vix_open_bar.iloc[0]['close']
                vix_close = vix_1555.iloc[0]['close']
                feat['vix_day_change'] = vix_close - vix_open
                feat['vix_rising'] = int(vix_close > vix_open)
            else:
                feat['vix_day_change'] = np.nan
                feat['vix_rising'] = np.nan
        else:
            feat['vix_day_change'] = feat['vix_rising'] = np.nan

        features.append(feat)

    df = pd.DataFrame(features)

    # Days since last loss (clustering)
    df = df.sort_values('date').reset_index(drop=True)
    df['days_since_last_loss'] = np.nan
    last_loss_date = None
    for i, row in df.iterrows():
        if last_loss_date is not None:
            dt_now = datetime.strptime(row['date'], '%Y%m%d')
            dt_last = datetime.strptime(last_loss_date, '%Y%m%d')
            df.at[i, 'days_since_last_loss'] = (dt_now - dt_last).days
        if row['loss'] == 1:
            last_loss_date = row['date']

    # Credit vs loss severity
    df['credit_per_contract'] = df['credit']
    df['loss_severity'] = df['pnl'].apply(lambda x: abs(x) if x < 0 else 0)

    # Move direction (positive = up move toward call spread)
    df['spx_move_direction'] = df['spx_move_1555_1600'].apply(
        lambda x: 'UP' if x > 0 else ('DOWN' if x < 0 else 'FLAT') if not np.isnan(x) else 'N/A'
    )

    # ============================
    # PRINT LOSING TRADE DETAILS
    # ============================
    losses = df[df['loss'] == 1].copy()
    wins = df[df['win'] == 1].copy()

    print("=" * 120)
    print(f"LOSING TRADE DETAILS ({len(losses)} trades)")
    print("=" * 120)

    detail_cols = ['date', 'day_name', 'is_friday', 'month', 'spx_at_entry', 'short_strike',
                   'strike_distance', 'credit', 'pnl', 'loss_severity', 'qty',
                   'spx_move_1555_1600', 'damage_range', 'spx_move_direction',
                   'gap_from_open', 'gap_from_open_pct', 'pre_entry_range', 'pre_entry_range_pct',
                   'vix_level', 'vix_day_change', 'vix_rising',
                   'vwap_distance', 'vwap_distance_pct', 'days_since_last_loss']

    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 200)
    pd.set_option('display.max_colwidth', 20)
    pd.set_option('display.float_format', lambda x: f'{x:.2f}')

    for _, row in losses.iterrows():
        print(f"\n--- {row['date']} ({row['day_name']}) ---")
        for c in detail_cols:
            if c in row:
                print(f"  {c:30s}: {row[c]}")
        print()

    # ============================
    # COMPARISON TABLE: Wins vs Losses
    # ============================
    compare_cols = [
        'strike_distance', 'credit', 'spx_move_1555_1600', 'spx_move_1555_1600_pct',
        'damage_range', 'gap_from_open', 'gap_from_open_pct',
        'pre_entry_range', 'pre_entry_range_pct',
        'vix_level', 'vix_day_change', 'vix_rising', 'is_friday',
        'vwap_distance', 'vwap_distance_pct', 'days_since_last_loss',
        'loss_severity', 'qty'
    ]

    print("\n" + "=" * 120)
    print("WIN vs LOSS COMPARISON")
    print("=" * 120)
    print(f"{'Feature':35s} | {'Win Mean':>12s} | {'Loss Mean':>12s} | {'Win Med':>12s} | {'Loss Med':>12s} | {'Effect Size':>12s} | {'Direction':>10s}")
    print("-" * 120)

    for col in compare_cols:
        w = wins[col].dropna()
        l = losses[col].dropna()
        if len(w) == 0 or len(l) == 0:
            continue
        w_mean = w.mean()
        l_mean = l.mean()
        w_med = w.median()
        l_med = l.median()

        # Cohen's d effect size
        pooled_std = np.sqrt((w.std()**2 * (len(w)-1) + l.std()**2 * (len(l)-1)) / (len(w)+len(l)-2))
        if pooled_std > 0:
            cohens_d = (l_mean - w_mean) / pooled_std
        else:
            cohens_d = 0

        direction = "LOSS HIGHER" if l_mean > w_mean else "LOSS LOWER"

        print(f"{col:35s} | {w_mean:12.4f} | {l_mean:12.4f} | {w_med:12.4f} | {l_med:12.4f} | {cohens_d:12.4f} | {direction:>10s}")

    # ============================
    # DAY OF WEEK BREAKDOWN
    # ============================
    print("\n" + "=" * 120)
    print("DAY-OF-WEEK BREAKDOWN")
    print("=" * 120)
    for day_name in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
        day_trades = df[df['day_name'] == day_name]
        day_wins = day_trades['win'].sum()
        day_losses = day_trades['loss'].sum()
        total = len(day_trades)
        wr = day_wins / total * 100 if total > 0 else 0
        print(f"  {day_name:12s}: {total:3d} trades, {day_wins:3d} wins, {day_losses:2d} losses, WR={wr:.1f}%")

    # ============================
    # MONTH BREAKDOWN
    # ============================
    print("\n" + "=" * 120)
    print("MONTH BREAKDOWN")
    print("=" * 120)
    for m in range(1, 13):
        m_trades = df[df['month'] == m]
        m_wins = m_trades['win'].sum()
        m_losses = m_trades['loss'].sum()
        total = len(m_trades)
        if total == 0:
            continue
        wr = m_wins / total * 100
        print(f"  Month {m:2d}: {total:3d} trades, {m_wins:3d} wins, {m_losses:2d} losses, WR={wr:.1f}%")

    # ============================
    # STRIKE DISTANCE ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("STRIKE DISTANCE ANALYSIS")
    print("=" * 120)
    for threshold in [3, 4, 5, 6, 7, 8, 10]:
        close_trades = df[df['strike_distance'] <= threshold]
        close_losses = close_trades['loss'].sum()
        close_wins = close_trades['win'].sum()
        far_trades = df[df['strike_distance'] > threshold]
        far_losses = far_trades['loss'].sum()
        far_wins = far_trades['win'].sum()
        print(f"  Distance <= {threshold:2d}: {len(close_trades):3d} trades, {close_losses:2d} losses, WR={close_wins/len(close_trades)*100:.1f}%  |  Distance > {threshold:2d}: {len(far_trades):3d} trades, {far_losses:2d} losses, WR={far_wins/max(len(far_trades),1)*100:.1f}%")

    # ============================
    # PRE-ENTRY RANGE (VOLATILITY) ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("PRE-ENTRY RANGE ANALYSIS (15:25-15:54 range as % of SPX)")
    print("=" * 120)
    for pct_thresh in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]:
        high_vol = df[df['pre_entry_range_pct'] >= pct_thresh]
        low_vol = df[df['pre_entry_range_pct'] < pct_thresh]
        hv_losses = high_vol['loss'].sum()
        lv_losses = low_vol['loss'].sum()
        print(f"  Range >= {pct_thresh:.2f}%: {len(high_vol):3d} trades, {hv_losses:2d} losses, WR={(high_vol['win'].sum()/max(len(high_vol),1))*100:.1f}%  |  Range < {pct_thresh:.2f}%: {len(low_vol):3d} trades, {lv_losses:2d} losses, WR={(low_vol['win'].sum()/max(len(low_vol),1))*100:.1f}%")

    # ============================
    # GAP FROM OPEN ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("GAP FROM OPEN ANALYSIS")
    print("=" * 120)
    for gap_thresh in [0.0, 0.5, 1.0]:
        gap_up = df[df['gap_from_open_pct'] > gap_thresh]
        gap_dn = df[df['gap_from_open_pct'] <= gap_thresh]
        print(f"  Gap > {gap_thresh:.1f}%: {len(gap_up):3d} trades, {gap_up['loss'].sum():2d} losses, WR={(gap_up['win'].sum()/max(len(gap_up),1))*100:.1f}%  |  Gap <= {gap_thresh:.1f}%: {len(gap_dn):3d} trades, {gap_dn['loss'].sum():2d} losses, WR={(gap_dn['win'].sum()/max(len(gap_dn),1))*100:.1f}%")

    # ============================
    # VIX RISING ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("VIX RISING/FALLING ANALYSIS")
    print("=" * 120)
    vix_up = df[df['vix_rising'] == 1]
    vix_dn = df[df['vix_rising'] == 0]
    print(f"  VIX Rising:  {len(vix_up):3d} trades, {vix_up['loss'].sum():2d} losses, WR={(vix_up['win'].sum()/max(len(vix_up),1))*100:.1f}%")
    print(f"  VIX Falling: {len(vix_dn):3d} trades, {vix_dn['loss'].sum():2d} losses, WR={(vix_dn['win'].sum()/max(len(vix_dn),1))*100:.1f}%")

    # VIX day change thresholds
    for vix_thresh in [0.5, 1.0, 1.5, 2.0]:
        big_vix = df[df['vix_day_change'] >= vix_thresh]
        small_vix = df[df['vix_day_change'] < vix_thresh]
        print(f"  VIX change >= +{vix_thresh}: {len(big_vix):3d} trades, {big_vix['loss'].sum():2d} losses  |  VIX change < +{vix_thresh}: {len(small_vix):3d} trades, {small_vix['loss'].sum():2d} losses")

    # ============================
    # VWAP DISTANCE ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("VWAP DISTANCE ANALYSIS (SPX at 15:55 vs day VWAP)")
    print("=" * 120)
    for vw_thresh in [0.0, 0.10, 0.20, 0.30]:
        above = df[df['vwap_distance_pct'] > vw_thresh]
        below = df[df['vwap_distance_pct'] <= vw_thresh]
        print(f"  Above VWAP by > {vw_thresh:.2f}%: {len(above):3d} trades, {above['loss'].sum():2d} losses, WR={(above['win'].sum()/max(len(above),1))*100:.1f}%  |  At/Below: {len(below):3d} trades, {below['loss'].sum():2d} losses, WR={(below['win'].sum()/max(len(below),1))*100:.1f}%")

    # ============================
    # DAMAGE RANGE ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("DAMAGE RANGE (15:55-16:00 high-low) ANALYSIS")
    print("=" * 120)
    for dr_thresh in [5, 8, 10, 12, 15, 20]:
        high_damage = df[df['damage_range'] >= dr_thresh]
        low_damage = df[df['damage_range'] < dr_thresh]
        print(f"  Damage range >= {dr_thresh:2d}: {len(high_damage):3d} trades, {high_damage['loss'].sum():2d} losses, WR={(high_damage['win'].sum()/max(len(high_damage),1))*100:.1f}%  |  < {dr_thresh:2d}: {len(low_damage):3d} trades, {low_damage['loss'].sum():2d} losses")

    # ============================
    # LOSS CLUSTERING ANALYSIS
    # ============================
    print("\n" + "=" * 120)
    print("LOSS CLUSTERING (days since previous loss)")
    print("=" * 120)
    loss_gaps = losses['days_since_last_loss'].dropna()
    if len(loss_gaps) > 0:
        print(f"  Mean days between losses: {loss_gaps.mean():.1f}")
        print(f"  Median days between losses: {loss_gaps.median():.1f}")
        print(f"  Min gap: {loss_gaps.min():.0f} days")
        print(f"  Losses within 7 days of another loss: {(loss_gaps <= 7).sum()}")
        print(f"  Losses within 3 days of another loss: {(loss_gaps <= 3).sum()}")

    # ============================
    # CREDIT vs LOSS SEVERITY
    # ============================
    print("\n" + "=" * 120)
    print("CREDIT RECEIVED vs LOSS SEVERITY (losses only)")
    print("=" * 120)
    for _, row in losses.iterrows():
        ratio = row['loss_severity'] / (row['credit'] * row['qty'] * 100) if row['credit'] > 0 else np.nan
        print(f"  {row['date']}: credit=${row['credit']:.2f} x {row['qty']}qty = ${row['credit']*row['qty']*100:.0f} gross credit, loss=${row['loss_severity']:.0f}, ratio={ratio:.1f}x")

    # ============================
    # COMBINED FILTER SUGGESTIONS
    # ============================
    print("\n" + "=" * 120)
    print("FILTER SUGGESTIONS (target: remove 3+ losses, lose as few wins as possible)")
    print("=" * 120)

    filters = [
        ("Friday only (skip Fridays)", lambda r: r['is_friday'] == 0),
        ("Strike dist > 5", lambda r: r['strike_distance'] > 5),
        ("Strike dist > 4", lambda r: r['strike_distance'] > 4),
        ("Pre-entry range < 0.20%", lambda r: r['pre_entry_range_pct'] < 0.20),
        ("Pre-entry range < 0.25%", lambda r: r['pre_entry_range_pct'] < 0.25),
        ("Pre-entry range < 0.30%", lambda r: r['pre_entry_range_pct'] < 0.30),
        ("VIX not rising (vix_rising=0)", lambda r: r['vix_rising'] == 0),
        ("VIX change < +1.0", lambda r: r['vix_day_change'] < 1.0),
        ("VIX change < +1.5", lambda r: r['vix_day_change'] < 1.5),
        ("Damage range < 10", lambda r: r['damage_range'] < 10),
        ("Damage range < 15", lambda r: r['damage_range'] < 15),
        ("VWAP dist > -0.20%", lambda r: r['vwap_distance_pct'] > -0.20),
        ("Gap from open > -0.50%", lambda r: r['gap_from_open_pct'] > -0.50),
        ("Not Friday + strike dist > 4", lambda r: (r['is_friday'] == 0) & (r['strike_distance'] > 4)),
        ("Not Friday + pre-entry range < 0.25%", lambda r: (r['is_friday'] == 0) & (r['pre_entry_range_pct'] < 0.25)),
        ("Strike dist > 5 + pre-entry range < 0.25%", lambda r: (r['strike_distance'] > 5) & (r['pre_entry_range_pct'] < 0.25)),
        ("VIX change < 1.5 + strike dist > 4", lambda r: (r['vix_day_change'] < 1.5) & (r['strike_distance'] > 4)),
        ("Not Friday + VIX change < 1.0", lambda r: (r['is_friday'] == 0) & (r['vix_day_change'] < 1.0)),
        ("Pre-entry range < 0.25% + VIX change < 1.5", lambda r: (r['pre_entry_range_pct'] < 0.25) & (r['vix_day_change'] < 1.5)),
    ]

    print(f"\n{'Filter':55s} | {'Trades':>7s} | {'Wins':>6s} | {'Losses':>7s} | {'WR%':>6s} | {'Wins Lost':>10s} | {'Losses Cut':>11s} | {'Net Benefit':>11s}")
    print("-" * 140)

    base_wins = int(wins['win'].sum())
    base_losses = int(losses['loss'].sum())

    for name, filt_fn in filters:
        try:
            mask = filt_fn(df)
            filtered = df[mask]
            f_wins = int(filtered['win'].sum())
            f_losses = int(filtered['loss'].sum())
            f_total = len(filtered)
            f_wr = f_wins / f_total * 100 if f_total > 0 else 0
            wins_lost = base_wins - f_wins
            losses_cut = base_losses - f_losses
            net = losses_cut - wins_lost  # positive = good trade-off
            print(f"  {name:53s} | {f_total:7d} | {f_wins:6d} | {f_losses:7d} | {f_wr:5.1f}% | {wins_lost:10d} | {losses_cut:11d} | {net:+11d}")
        except Exception as e:
            print(f"  {name:53s} | ERROR: {e}")

    # ============================
    # BEST SINGLE FILTERS (automated search)
    # ============================
    print("\n" + "=" * 120)
    print("AUTOMATED FILTER SEARCH: best single-feature thresholds")
    print("=" * 120)

    search_features = {
        'strike_distance': ('<=', np.arange(3, 12, 0.5)),
        'pre_entry_range_pct': ('>=', np.arange(0.05, 0.50, 0.02)),
        'vix_day_change': ('>=', np.arange(-1, 4, 0.25)),
        'vwap_distance_pct': ('<=', np.arange(-1, 1, 0.05)),
        'damage_range': ('>=', np.arange(3, 25, 1)),
        'gap_from_open_pct': ('<=', np.arange(-2, 2, 0.1)),
        'vix_level': ('>=', np.arange(12, 35, 1)),
    }

    best_results = []
    for feat_name, (direction, thresholds) in search_features.items():
        for thresh in thresholds:
            if direction == '<=':
                mask = df[feat_name] <= thresh
            else:
                mask = df[feat_name] >= thresh
            removed = df[~mask]
            if len(removed) == 0:
                continue
            losses_cut = int(removed['loss'].sum())
            wins_lost = int(removed['win'].sum())
            if losses_cut >= 3 and wins_lost <= losses_cut * 3:
                ratio = losses_cut / max(wins_lost, 0.5)
                remaining = df[mask]
                wr = remaining['win'].sum() / len(remaining) * 100
                best_results.append({
                    'filter': f"{feat_name} {direction} {thresh:.2f} (remove violators)",
                    'losses_cut': losses_cut,
                    'wins_lost': wins_lost,
                    'ratio': ratio,
                    'remaining': len(remaining),
                    'wr': wr,
                })

    best_results.sort(key=lambda x: (-x['losses_cut'], x['wins_lost']))
    print(f"\n{'Filter':60s} | {'LossCut':>8s} | {'WinLost':>8s} | {'Ratio':>6s} | {'Remain':>7s} | {'WR%':>6s}")
    print("-" * 110)
    for r in best_results[:20]:
        print(f"  {r['filter']:58s} | {r['losses_cut']:8d} | {r['wins_lost']:8d} | {r['ratio']:5.1f}x | {r['remaining']:7d} | {r['wr']:5.1f}%")

    print("\n\nDone.")

if __name__ == "__main__":
    main()
