#!/bin/bash
# Waits for the 2025 download (PID 91888) to finish, then starts 2026.

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"
PID=91888

echo "[$(date)] Waiting for 2025 download (PID $PID) to finish..."

while kill -0 $PID 2>/dev/null; do
    sleep 60
done

echo "[$(date)] 2025 download complete. Starting 2026..."
cd "$PROJ"
$VENV -u download_data.py --year 2026 >> "$PROJ/logs/mini1_2026.log" 2>&1

echo "[$(date)] 2026 download finished."
