#!/bin/bash
# sync_data.sh — full 3-way sync across all Mac minis
#
# Strategy: Mini 1 is the hub.
#   Phase 1: Pull unique/newer files from Mini 2 and Mini 3 → Mini 1
#   Phase 2: Push all files from Mini 1 → Mini 2
#   Phase 3: Push all files from Mini 1 → Mini 3
#
# Safe to re-run: rsync only copies files that are missing or changed.
# Re-run after active downloads finish (2018, 2019, 2022) to pick up new files.

set -euo pipefail

DATA="/Users/jeerapongwongchote/Documents/thetadata/data"
MINI2="jeerapongwongchote@192.168.0.253"
MINI3="jeerapongwongchote@192.168.0.123"

RSYNC="rsync -avz --progress"

# All known years
ALL_YEARS="2015 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026"

# ── Phase 1a: Pull from Mini 2 → Mini 1 ─────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Phase 1a: Pull Mini 2 → Mini 1"
echo "════════════════════════════════════════════"
for year in $ALL_YEARS; do
    echo "--- $year ---"
    $RSYNC --ignore-existing "$MINI2:$DATA/$year/" "$DATA/$year/" 2>/dev/null || echo "  (skipped — year not on Mini 2)"
done

# ── Phase 1b: Pull from Mini 3 → Mini 1 ─────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Phase 1b: Pull Mini 3 → Mini 1"
echo "════════════════════════════════════════════"
for year in $ALL_YEARS; do
    echo "--- $year ---"
    $RSYNC --ignore-existing "$MINI3:$DATA/$year/" "$DATA/$year/" 2>/dev/null || echo "  (skipped — year not on Mini 3)"
done

# ── Phase 2: Push from Mini 1 → Mini 2 ──────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Phase 2: Push Mini 1 → Mini 2"
echo "════════════════════════════════════════════"
for year in $ALL_YEARS; do
    echo "--- $year ---"
    ssh "$MINI2" "mkdir -p $DATA/$year"
    $RSYNC --ignore-existing "$DATA/$year/" "$MINI2:$DATA/$year/"
done

# ── Phase 3: Push from Mini 1 → Mini 3 ──────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Phase 3: Push Mini 1 → Mini 3"
echo "════════════════════════════════════════════"
for year in $ALL_YEARS; do
    echo "--- $year ---"
    ssh "$MINI3" "mkdir -p $DATA/$year"
    $RSYNC --ignore-existing "$DATA/$year/" "$MINI3:$DATA/$year/"
done

echo ""
echo "════════════════════════════════════════════"
echo " Sync complete."
echo " Re-run after 2018/2019/2022 finish downloading"
echo " to pick up the remaining files."
echo "════════════════════════════════════════════"
