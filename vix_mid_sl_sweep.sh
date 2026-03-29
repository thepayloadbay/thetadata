#!/bin/bash
# vix_mid_sl_sweep.sh
# Sweeps DYNAMIC_SL_VIX_MID upper bound (lower bound fixed at 13.0)
# Runs all combos in parallel, then prints a summary table.

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"
TS=$(date +%Y%m%d_%H%M%S)
RESULTS="$PROJ/logs/vix_mid_sl_sweep_${TS}.csv"

# (low, high) pairs to test — None means disable the mid range
COMBOS=(
    "none"       # baseline: no VIX mid SL
    "13.0,13.5"
    "13.0,14.0"
    "13.0,14.5"
    "13.0,15.0"
    "13.0,15.5"
    "13.0,16.0"
    "13.0,16.5"
    "13.0,17.0"
    "13.0,18.0"
    "13.0,19.0"
    "13.0,20.0"
)

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  VIX MID SL SWEEP — $TS"
echo "══════════════════════════════════════════════════════════"
echo "  Running ${#COMBOS[@]} combos in parallel..."
echo ""

PIDS=()
LOGS=()

for combo in "${COMBOS[@]}"; do
    if [[ "$combo" == "none" ]]; then
        label="none"
        LOG="$PROJ/logs/vix_mid_sweep_none_${TS}.log"
        $VENV -u "$PROJ/meds.py" >> "$LOG" 2>&1 &
    else
        low=$(echo $combo | cut -d, -f1)
        high=$(echo $combo | cut -d, -f2)
        label="${low}-${high}"
        LOG="$PROJ/logs/vix_mid_sweep_${label}_${TS}.log"
        $VENV -u "$PROJ/meds.py" \
            --sl-vix-mid-low "$low" --sl-vix-mid-high "$high" >> "$LOG" 2>&1 &
    fi
    PIDS+=($!)
    LOGS+=("$LOG")
    echo "  Launched combo=$label PID=$!"
done

echo ""
echo "  Waiting for all to finish..."
for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
done
echo "  All done."
echo ""

# Parse results
echo "vix_mid_range,total_pnl,win_rate,max_drawdown,sharpe,sortino" > "$RESULTS"

IDX=0
for combo in "${COMBOS[@]}"; do
    LOG="${LOGS[$IDX]}"
    label="$combo"
    pnl=$(grep    "Total P&L"    "$LOG" | grep -oP '\$[\-0-9,]+' | tr -d '$,' | head -1)
    wr=$(grep     "Win rate"     "$LOG" | grep -oP '[0-9]+\.[0-9]+%' | tr -d '%' | head -1)
    dd=$(grep     "Max drawdown" "$LOG" | grep -oP '\$[\-0-9,]+' | tr -d '$,' | head -1)
    sharpe=$(grep "Sharpe ratio" "$LOG" | grep -oP '[0-9]+\.[0-9]+' | head -1)
    sortino=$(grep "Sortino ratio" "$LOG" | grep -oP '[0-9]+\.[0-9]+' | head -1)
    echo "${label},${pnl},${wr},${dd},${sharpe},${sortino}" >> "$RESULTS"
    IDX=$((IDX+1))
done

echo "══════════════════════════════════════════════════════════"
echo "  VIX MID RANGE SWEEP RESULTS"
echo "══════════════════════════════════════════════════════════"
printf "%-16s %12s %10s %14s %8s %8s\n" "VIX_MID_RANGE" "TOTAL_PNL" "WIN_RATE" "MAX_DD" "SHARPE" "SORTINO"
echo "──────────────────────────────────────────────────────────"
while IFS=, read -r label pnl wr dd sharpe sortino; do
    [[ "$label" == "vix_mid_range" ]] && continue
    printf "%-16s %12s %9s%% %14s %8s %8s\n" "$label" "\$$pnl" "$wr" "\$$dd" "$sharpe" "$sortino"
done < "$RESULTS"
echo "══════════════════════════════════════════════════════════"
echo "  Full results: $RESULTS"
