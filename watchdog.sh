#!/bin/bash
# watchdog.sh — monitors download_data.py on all 3 minis, restarts if dead or stuck.
# Usage (run from project dir): nohup ./watchdog.sh >> logs/watchdog.log 2>&1 &

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"
STALE_MINUTES=15   # restart if log hasn't updated in this many minutes
CHECK_INTERVAL=60  # seconds between checks

MINI1_HOST="local"
MINI2_HOST="192.168.0.253"
MINI3_HOST="192.168.0.123"

# Year chains per mini — get_next_year returns next year or empty string if done
get_next_year() {
    local mini=$1 current=$2
    case "$mini:$current" in
        mini1:2026) echo "2019" ;;
        mini1:2019) echo "2015" ;;
        mini1:2015) echo "2014" ;;
        mini1:2025) echo "2023" ;;
        mini1:2023) echo "2024" ;;
        mini2:2024) echo "2022" ;;
        mini2:2022) echo "2018" ;;
        mini2:2018) echo "2016" ;;
        mini3:2023) echo "2021" ;;
        mini3:2021) echo "2020" ;;
        mini3:2020) echo "2017" ;;
        *) echo "" ;;
    esac
}

# State files — track which year each mini is currently on
MINI1_STATE="$PROJ/logs/mini1_state.txt"
MINI2_STATE="$PROJ/logs/mini2_state.txt"
MINI3_STATE="$PROJ/logs/mini3_state.txt"

# Initialize state files if not present
[ -f "$MINI1_STATE" ] || echo "2026" > "$MINI1_STATE"
[ -f "$MINI2_STATE" ] || echo "2024" > "$MINI2_STATE"
[ -f "$MINI3_STATE" ] || echo "2023" > "$MINI3_STATE"

is_year_complete() {
    local log=$1
    grep -q "^  Done —" "$log" 2>/dev/null
}

is_log_stale() {
    local log=$1
    [ ! -f "$log" ] && return 0
    local modified=$(date -r "$log" +%s 2>/dev/null || echo 0)
    local now=$(date +%s)
    local age_minutes=$(( (now - modified) / 60 ))
    [ $age_minutes -ge $STALE_MINUTES ]
}

restart_local() {
    local year=$1 log=$2
    echo "[$(date)] [mini1] Restarting year $year..."
    pkill -f "download_data.py --year $year" 2>/dev/null
    sleep 2
    cd "$PROJ"
    nohup $VENV -u download_data.py --year $year >> "$log" 2>&1 &
    echo "[$(date)] [mini1] Restarted (PID $!)"
}

restart_remote() {
    local label=$1 host=$2 year=$3 log=$4
    echo "[$(date)] [$label] Restarting year $year via SSH tunnel..."
    # Kill any orphaned SSH tunnel processes on Mini 1 for this host
    pkill -f "ssh.*$host.*download_data" 2>/dev/null
    sleep 1
    # Kill any existing process on the remote
    ssh -o ConnectTimeout=5 jeerapongwongchote@$host \
        "pkill -f 'download_data.py --year $year'" 2>/dev/null
    sleep 2
    # Run python in the foreground on the remote — output pipes back through SSH
    # to Mini 1's log. The tunnel stays alive as long as the python process runs.
    # The & is on Mini 1's side, backgrounding the whole SSH command.
    ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=20 \
        -R 25503:127.0.0.1:25503 jeerapongwongchote@$host \
        "cd $PROJ && $VENV -u download_data.py --year $year --mcp-url http://127.0.0.1:25503/mcp/sse" \
        >> "$log" 2>&1 &
    echo "[$(date)] [$label] Restart command sent (tunnel PID $!)"
}

check_remote() {
    local label=$1 host=$2 year=$3 log=$4 next_year=$5 state_file=$6
    local running
    # Try twice before declaring dead — guards against transient SSH timeouts
    running=$(ssh -o ConnectTimeout=5 jeerapongwongchote@$host \
        "pgrep -f 'download_data.py --year $year'" 2>/dev/null)
    if [ -z "$running" ]; then
        sleep 5
        running=$(ssh -o ConnectTimeout=5 jeerapongwongchote@$host \
            "pgrep -f 'download_data.py --year $year'" 2>/dev/null)
    fi
    if [ -z "$running" ]; then
        if [ -n "$next_year" ] && is_year_complete "$log"; then
            echo "[$(date)] [$label] Year $year complete — advancing to $next_year"
            echo "$next_year" > "$state_file"
            local next_log="$PROJ/logs/${label}_${next_year}.log"
            restart_remote "$label" "$host" "$next_year" "$next_log"
        elif is_year_complete "$log"; then
            echo "[$(date)] [$label] Year $year complete — all years done, nothing to do"
        else
            echo "[$(date)] [$label] Process dead — restarting year $year"
            restart_remote "$label" "$host" "$year" "$log"
        fi
    elif is_log_stale "$log"; then
        echo "[$(date)] [$label] Log stale (>${STALE_MINUTES}min) — restarting"
        restart_remote "$label" "$host" "$year" "$log"
    else
        echo "[$(date)] [$label] OK (PID $running)"
    fi
}

echo "[$(date)] Watchdog started (stale=${STALE_MINUTES}min, interval=${CHECK_INTERVAL}s)"

while true; do
    # ── Mini 1 (local) ──
    MINI1_CUR_YEAR=$(cat "$MINI1_STATE")
    MINI1_CUR_LOG="$PROJ/logs/mini1_${MINI1_CUR_YEAR}.log"
    MINI1_NEXT=$(get_next_year "mini1" "$MINI1_CUR_YEAR")
    active_year=$(pgrep -a -f "download_data.py --year" 2>/dev/null | grep -o '\-\-year [0-9]*' | awk '{print $2}' | sort -n | tail -1)
    if [ -z "$active_year" ]; then
        if is_year_complete "$MINI1_CUR_LOG" && [ -n "$MINI1_NEXT" ]; then
            echo "[$(date)] [mini1] Year $MINI1_CUR_YEAR complete — advancing to $MINI1_NEXT"
            echo "$MINI1_NEXT" > "$MINI1_STATE"
            restart_local "$MINI1_NEXT" "$PROJ/logs/mini1_${MINI1_NEXT}.log"
        elif is_year_complete "$MINI1_CUR_LOG"; then
            echo "[$(date)] [mini1] Year $MINI1_CUR_YEAR complete — all years done, nothing to do"
        else
            echo "[$(date)] [mini1] Process dead — restarting year $MINI1_CUR_YEAR"
            restart_local "$MINI1_CUR_YEAR" "$MINI1_CUR_LOG"
        fi
    elif is_log_stale "$MINI1_CUR_LOG"; then
        echo "[$(date)] [mini1] Log stale (>${STALE_MINUTES}min) — restarting"
        restart_local "$MINI1_CUR_YEAR" "$MINI1_CUR_LOG"
    else
        echo "[$(date)] [mini1] OK (year $active_year)"
    fi

    # ── Mini 2 ──
    MINI2_CUR_YEAR=$(cat "$MINI2_STATE")
    MINI2_CUR_LOG="$PROJ/logs/mini2_${MINI2_CUR_YEAR}.log"
    MINI2_CUR_NEXT=$(get_next_year "mini2" "$MINI2_CUR_YEAR")
    check_remote "mini2" "$MINI2_HOST" "$MINI2_CUR_YEAR" "$MINI2_CUR_LOG" "$MINI2_CUR_NEXT" "$MINI2_STATE"

    # ── Mini 3 ──
    MINI3_CUR_YEAR=$(cat "$MINI3_STATE")
    MINI3_CUR_LOG="$PROJ/logs/mini3_${MINI3_CUR_YEAR}.log"
    MINI3_CUR_NEXT=$(get_next_year "mini3" "$MINI3_CUR_YEAR")
    check_remote "mini3" "$MINI3_HOST" "$MINI3_CUR_YEAR" "$MINI3_CUR_LOG" "$MINI3_CUR_NEXT" "$MINI3_STATE"

    sleep $CHECK_INTERVAL
done
