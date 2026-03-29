#!/usr/bin/env bash
# Full-run day filter marathon — one complete backtest per filter candidate.
# Candidates from filter sweep (top by Calmar vs baseline, >290 trading days):
#   vix_min=20              359 days  Calmar 739  (sweep)
#   dExpMovePct_min=1.25    365 days  Calmar 755  (sweep)
#   dRangePct_min=1.5       290 days  Calmar 559  (sweep)
#   dCci_max=0              299 days  Calmar 523  (sweep)
#   dIvRank_min=20          466 days  Calmar 114  (sweep)
#   dAdx_min=40             322 days  Calmar  31  (sweep)
#   dRsi_max=60             714 days  Calmar  47  (sweep)
# Results land in logs/run_history.json.

set -e
cd "$(dirname "$0")"

patch() { sed -i '' "s/^$1[[:space:]]*=.*/$1 = $2/" meds.py; }
reset() { sed -i '' "s/^$1[[:space:]]*=.*/$1 = None/" meds.py; }

# Disable sweep mode for marathon runs
sed -i '' "s/^RUN_DAY_FILTER_SWEEP = .*/RUN_DAY_FILTER_SWEEP = False/" meds.py

run_filter() {
    local label="$1"
    local var="$2"
    local val="$3"

    echo ""
    echo "============================================================"
    echo "  FILTER: $label"
    echo "============================================================"
    patch "$var" "$val"
    python3 meds.py --marathon 2>&1 | grep -E "Total P&L|Win rate|Max drawdown|Sharpe ratio|Calmar|Total trades"
    reset "$var"
}

# ── Baseline (no filter) ──
echo ""
echo "============================================================"
echo "  BASELINE (no day filter)"
echo "============================================================"
python3 meds.py --marathon 2>&1 | grep -E "Total P&L|Win rate|Max drawdown|Sharpe ratio|Calmar|Total trades"

# ── Candidates ──
run_filter "vix_min=20"              "VIX_MIN_FILTER"          "20.0"
run_filter "dExpMovePct_min=1.25"    "DAY_FILTER_EXP_MOVE_MIN" "1.25"
run_filter "dRangePct_min=1.5"       "DAY_FILTER_RANGE_MIN"    "1.5"
run_filter "dCci_max=0"              "DAY_FILTER_CCI_MAX"      "0"
run_filter "dIvRank_min=20"          "DAY_FILTER_IVR_MIN"      "20"
run_filter "dAdx_min=40"             "DAY_FILTER_ADX_MIN"      "40"
run_filter "dRsi_max=60"             "DAY_FILTER_RSI_MAX"      "60"

# ── Restore ──
sed -i '' "s/^RUN_DAY_FILTER_SWEEP = .*/RUN_DAY_FILTER_SWEEP = True/" meds.py

echo ""
echo "All filters restored to None. RUN_DAY_FILTER_SWEEP restored to True."
echo "Full results in logs/run_history.json"
