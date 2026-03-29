#!/bin/bash
# wait_and_merge.sh
# Restarts Mini 2 backtest (2023-07-03 → 2024-12-31), polls for completion,
# then merges day_filter_sweep CSVs from all 3 minis.

set -euo pipefail

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"
MINI2="jeerapongwongchote@192.168.0.253"
MINI3="jeerapongwongchote@192.168.0.123"
LOG_DIR="$PROJ/logs"

TS=$(date +%Y%m%d_%H%M%S)
MINI2_LOG="$LOG_DIR/distributed_mini2_restart_${TS}.log"
MINI2_DONE="$LOG_DIR/distributed_mini2_restart_${TS}.done"
MINI2_CSV_DEST="$LOG_DIR/mini2_sweep_restart_${TS}.csv"

MINI1_CSV="$LOG_DIR/metf_v35_bidask_day_filter_sweep_20260327_132803.csv"
MINI3_CSV="$LOG_DIR/mini3_sweep_20260327_132803.csv"   # pulled below
MERGED_CSV="$LOG_DIR/distributed_sweep_merged_${TS}.csv"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  WAIT-AND-MERGE — $TS"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Pull Mini 3 sweep CSV to Mini 1 ──────────────────────────────────────────
echo "  [pull] Pulling Mini 3 sweep CSV..."
rsync -az "$MINI3:$PROJ/logs/metf_v35_bidask_day_filter_sweep_20260327_132803.csv" "$MINI3_CSV"
echo "  Mini 3 CSV pulled → $MINI3_CSV"
echo ""

# ── Restart Mini 2 (kill any stale process first) ────────────────────────────
echo "  [restart] Killing any stale metf process on Mini 2..."
ssh "$MINI2" "pkill -f meds || true"
sleep 2

echo "  [restart] Launching Mini 2 (2023-07-03 → 2024-12-31, detached)..."
ssh "$MINI2" "cd $PROJ && nohup bash -c \
    '$VENV -u meds.py --start 2023-07-03 --end 2024-12-31 \
     >> $MINI2_LOG 2>&1 && touch $MINI2_DONE' \
    < /dev/null > /dev/null 2>&1 &"
echo "  Mini 2 launched. Log: $MINI2_LOG"
echo ""

# ── Poll Mini 2 for completion ────────────────────────────────────────────────
echo "  [poll] Waiting for Mini 2 to finish (checking every 60s)..."
while true; do
    sleep 60
    if ssh "$MINI2" "test -f $MINI2_DONE" 2>/dev/null; then
        echo "  [done] Mini 2 finished!"
        break
    fi
    LAST=$(ssh "$MINI2" "tail -1 $MINI2_LOG 2>/dev/null || echo '(no log yet)'" 2>/dev/null)
    echo "  [wait] $(date '+%H:%M:%S') — $LAST"
done

# ── Pull Mini 2 sweep CSV ─────────────────────────────────────────────────────
echo ""
echo "  [pull] Finding and pulling Mini 2 sweep CSV..."
MINI2_REMOTE_CSV=$(ssh "$MINI2" "ls -t $PROJ/logs/meds_day_filter_sweep_*.csv 2>/dev/null | head -1" 2>/dev/null)
if [[ -z "$MINI2_REMOTE_CSV" ]]; then
    echo "  [FAIL] No day_filter_sweep CSV found on Mini 2!"
    exit 1
fi
rsync -az "$MINI2:$MINI2_REMOTE_CSV" "$MINI2_CSV_DEST"
echo "  Mini 2 CSV pulled → $MINI2_CSV_DEST"

# ── Merge all 3 sweep CSVs ────────────────────────────────────────────────────
echo ""
echo "  [merge] Merging sweep CSVs from all 3 minis..."
MINI1_CSV="$MINI1_CSV" \
MINI2_CSV="$MINI2_CSV_DEST" \
MINI3_CSV="$MINI3_CSV" \
MERGED_CSV="$MERGED_CSV" \
"$VENV" - << 'PYEOF'
import os, sys
import pandas as pd

mini1 = os.environ["MINI1_CSV"]
mini2 = os.environ["MINI2_CSV"]
mini3 = os.environ["MINI3_CSV"]
merged = os.environ["MERGED_CSV"]

dfs = {}
for name, path in [("Mini1 (2022-06/2023)", mini1),
                   ("Mini2 (07/2023-2024)", mini2),
                   ("Mini3 (2025-2026)",    mini3)]:
    if os.path.exists(path):
        df = pd.read_csv(path)
        dfs[name] = df
        print(f"  Loaded {name}: {len(df)} rows from {os.path.basename(path)}")
    else:
        print(f"  WARNING: missing {path}")

if len(dfs) < 2:
    print("  ERROR: need at least 2 CSVs to merge")
    sys.exit(1)

all_df = pd.concat(dfs.values(), keys=dfs.keys(), names=["source"]).reset_index(level=0)

# Re-aggregate by filter_name + threshold
agg = (all_df.groupby(["filter_name", "threshold"])
       .agg(
           sources=("source", lambda x: " + ".join(sorted(set(x)))),
           days_traded=("days_traded", "sum"),
           days_filtered_out=("days_filtered_out", "sum"),
           num_trades=("num_trades", "sum"),
           total_pnl=("total_pnl", "sum"),
           max_drawdown=("max_drawdown", "sum"),
       )
       .reset_index())

# Recalculate derived metrics
# win_rate_pct: not directly summable — skip (set to NaN)
agg["win_rate_pct"] = None
agg["calmar"] = agg.apply(
    lambda r: r["total_pnl"] / abs(r["max_drawdown"]) if r["max_drawdown"] < 0 else float("inf"),
    axis=1
)
agg = agg.sort_values("total_pnl", ascending=False)
agg.to_csv(merged, index=False)

print(f"\n  Merged {len(agg)} filter combinations → {os.path.basename(merged)}")
print(f"\n{'═'*60}")
print(f"  TOP 10 BY TOTAL P&L (merged, 2022–2026)")
print(f"{'═'*60}")
print(agg[["filter_name","threshold","days_traded","num_trades","total_pnl","max_drawdown","calmar"]].head(10).to_string(index=False))
print(f"{'═'*60}")
PYEOF

echo ""
echo "  [complete] Done. Merged CSV: $MERGED_CSV"
