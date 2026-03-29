#!/bin/bash
# run_distributed.sh — split backtest across 3 Mac Minis by date range, merge results on Mini 1
#
# Date split:
#   Mini 1 (local)        : 2022-01-03 → 2023-06-30
#   Mini 2 (192.168.0.253): 2023-07-03 → 2024-12-31
#   Mini 3 (192.168.0.123): 2025-01-02 → 2026-03-25
#
# Remote jobs run via nohup (detached) — no SSH keepalive issues.
# Completion is detected by polling for a sentinel .done file.

set -euo pipefail

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"
MINI2="jeerapongwongchote@192.168.0.253"
MINI3="jeerapongwongchote@192.168.0.123"

TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="$PROJ/logs"

MINI1_LOG="$LOG_DIR/distributed_mini1_${TS}.log"
MINI2_LOG="$LOG_DIR/distributed_mini2_${TS}.log"
MINI3_LOG="$LOG_DIR/distributed_mini3_${TS}.log"

MINI1_CSV="$LOG_DIR/distributed_trades_mini1_${TS}.csv"
MINI2_CSV="$LOG_DIR/distributed_trades_mini2_${TS}.csv"
MINI3_CSV="$LOG_DIR/distributed_trades_mini3_${TS}.csv"
MERGED_CSV="$LOG_DIR/distributed_trades_merged_${TS}.csv"

# Sentinel files written by each mini when done
MINI2_DONE="$LOG_DIR/distributed_mini2_${TS}.done"
MINI3_DONE="$LOG_DIR/distributed_mini3_${TS}.done"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  DISTRIBUTED BACKTEST — $TS"
echo "══════════════════════════════════════════════════════════"
echo "  Mini 1 (local)        : 2022-01-03 → 2023-06-30"
echo "  Mini 2 (192.168.0.253): 2023-07-03 → 2024-12-31"
echo "  Mini 3 (192.168.0.123): 2025-01-02 → 2026-03-25"
echo ""

# ── Sync latest code to Mini 2 and Mini 3 ────────────────────────────────────
echo "  [sync] Pushing latest code to Mini 2 and Mini 3..."
rsync -az "$PROJ/meds.py" "$MINI2:$PROJ/"
rsync -az "$PROJ/meds.py" "$MINI3:$PROJ/"
echo "  [sync] Done."
echo ""

# ── Launch Mini 2 via nohup (detached — survives SSH disconnect) ──────────────
echo "  [launch] Starting Mini 2 (detached)..."
ssh "$MINI2" "cd $PROJ && nohup bash -c '$VENV -u meds.py \
    --start 2023-07-03 --end 2024-12-31 \
    --out $MINI2_CSV \
    >> $MINI2_LOG 2>&1 && touch $MINI2_DONE' < /dev/null > /dev/null 2>&1 &"

# ── Launch Mini 3 via nohup (detached) ───────────────────────────────────────
echo "  [launch] Starting Mini 3 (detached)..."
ssh "$MINI3" "cd $PROJ && nohup bash -c '$VENV -u meds.py \
    --start 2025-01-02 --end 2026-03-25 \
    --out $MINI3_CSV \
    >> $MINI3_LOG 2>&1 && touch $MINI3_DONE' < /dev/null > /dev/null 2>&1 &"

# ── Run Mini 1 locally ────────────────────────────────────────────────────────
echo "  [launch] Starting Mini 1 (local)..."
"$VENV" -u "$PROJ/meds.py" \
    --start 2022-01-03 --end 2023-06-30 \
    --out "$MINI1_CSV" \
    >> "$MINI1_LOG" 2>&1 &
MINI1_PID=$!

echo ""
echo "  All 3 minis running in parallel."
echo "  Logs:"
echo "    Mini 1: $MINI1_LOG"
echo "    Mini 2: $MINI2_LOG"
echo "    Mini 3: $MINI3_LOG"
echo ""
echo "  Waiting for completion (polling every 30s)..."

# ── Wait for Mini 1 locally ───────────────────────────────────────────────────
wait $MINI1_PID && echo "  [done] Mini 1 finished." || echo "  [FAIL] Mini 1 failed — check $MINI1_LOG"

# ── Poll Mini 2 and Mini 3 for completion ─────────────────────────────────────
MINI2_OK=0; MINI3_OK=0
while [[ $MINI2_OK -eq 0 || $MINI3_OK -eq 0 ]]; do
    sleep 30
    if [[ $MINI2_OK -eq 0 ]]; then
        if ssh "$MINI2" "test -f $MINI2_DONE" 2>/dev/null; then
            echo "  [done] Mini 2 finished."
            MINI2_OK=1
        else
            LAST=$(ssh "$MINI2" "tail -1 $MINI2_LOG 2>/dev/null || echo '(no log yet)'" 2>/dev/null)
            echo "  [wait] Mini 2 still running... $LAST"
        fi
    fi
    if [[ $MINI3_OK -eq 0 ]]; then
        if ssh "$MINI3" "test -f $MINI3_DONE" 2>/dev/null; then
            echo "  [done] Mini 3 finished."
            MINI3_OK=1
        else
            LAST=$(ssh "$MINI3" "tail -1 $MINI3_LOG 2>/dev/null || echo '(no log yet)'" 2>/dev/null)
            echo "  [wait] Mini 3 still running... $LAST"
        fi
    fi
done

# ── Pull CSVs from Mini 2 and Mini 3 back to Mini 1 ──────────────────────────
echo ""
echo "  [pull] Pulling trade CSVs from Mini 2 and Mini 3..."
rsync -az "$MINI2:$MINI2_CSV" "$MINI2_CSV" && echo "  Mini 2 CSV pulled." || echo "  [FAIL] Could not pull Mini 2 CSV"
rsync -az "$MINI3:$MINI3_CSV" "$MINI3_CSV" && echo "  Mini 3 CSV pulled." || echo "  [FAIL] Could not pull Mini 3 CSV"

# ── Merge all 3 CSVs ─────────────────────────────────────────────────────────
echo ""
echo "  [merge] Merging trade logs..."
LOG_DIR="$LOG_DIR" TS="$TS" "$VENV" - << 'PYEOF'
import sys, os, pandas as pd

log_dir = os.environ.get("LOG_DIR")
ts      = os.environ.get("TS")

paths = [
    f"{log_dir}/distributed_trades_mini1_{ts}.csv",
    f"{log_dir}/distributed_trades_mini2_{ts}.csv",
    f"{log_dir}/distributed_trades_mini3_{ts}.csv",
]
merged = f"{log_dir}/distributed_trades_merged_{ts}.csv"

dfs = []
for path in paths:
    if os.path.exists(path):
        df = pd.read_csv(path)
        dfs.append(df)
        print(f"  Loaded {len(df):,} trades from {os.path.basename(path)}")
    else:
        print(f"  WARNING: missing {path}")

if not dfs:
    print("  ERROR: no CSV files found to merge")
    sys.exit(1)

combined = pd.concat(dfs, ignore_index=True)
combined = combined.sort_values("entry_date").reset_index(drop=True)
combined.to_csv(merged, index=False)
print(f"\n  Merged {len(combined):,} trades → {os.path.basename(merged)}")

# Metrics
total_pnl = combined["pnl_earned"].sum()
wins      = combined["win"].sum()
losses    = combined["loss"].sum()
wr        = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

day_pnl  = combined.groupby("entry_date")["pnl_earned"].sum()
cum      = day_pnl.cumsum()
drawdown = (cum - cum.cummax()).min()
calmar   = total_pnl / abs(drawdown) if drawdown < 0 else float("inf")

print(f"\n{'═'*50}")
print(f"  MERGED BACKTEST RESULTS")
print(f"{'═'*50}")
print(f"  Total trades : {len(combined):,}")
print(f"  Win rate     : {wr:.1f}%")
print(f"  Total P&L    : ${total_pnl:,.0f}")
print(f"  Max drawdown : ${drawdown:,.0f}")
print(f"  Calmar       : {calmar:.2f}")
print(f"{'═'*50}")
PYEOF

echo ""
echo "  [complete] Distributed backtest done — $TS"
echo "  Merged CSV: $MERGED_CSV"
