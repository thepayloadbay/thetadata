#!/usr/bin/env bash
# Full-run TP sweep — one complete marathon backtest per TP level.
# Results land in logs/run_history.json tagged by their DAILY_TP value.

set -e
cd "$(dirname "$0")"

TP_LEVELS=("600.0" "650.0" "700.0" "750.0" "800.0" "900.0" "None")

for tp in "${TP_LEVELS[@]}"; do
    echo ""
    echo "============================================================"
    echo "  DAILY_TP = $tp"
    echo "============================================================"

    # Patch DAILY_TP in the source
    if [ "$tp" = "None" ]; then
        sed -i '' "s/^DAILY_TP       = .*/DAILY_TP       = None/" metf_v35_bidask.py
    else
        sed -i '' "s/^DAILY_TP       = .*/DAILY_TP       = ${tp}/" metf_v35_bidask.py
    fi

    python3 metf_v35_bidask.py 2>&1 | grep -E "Total P&L|Win rate|Max drawdown|Sharpe|Calmar|Trades"
done

# Restore baseline TP
sed -i '' "s/^DAILY_TP       = .*/DAILY_TP       = 750.0/" metf_v35_bidask.py
echo ""
echo "Restored DAILY_TP = 750.0"
echo ""
echo "Full results in logs/run_history.json"
