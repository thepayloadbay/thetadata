#!/usr/bin/env bash
# Full-run day filter marathon — one complete backtest per filter candidate.
# Candidates from filter sweep (top by P&L/day vs baseline, >500 trading days):
#   vix_max=20          669 days  +11.5%/day
#   above_sma200=True   852 days   +8.7%/day
#   dAdx_min=25         631 days   +7.6%/day
#   dRangePct_max=1.5   766 days   +6.2%/day
#   dExpMovePct_max=1.5 839 days   +6.2%/day
# Results land in logs/run_history.json.

set -e
cd "$(dirname "$0")"

patch() { sed -i '' "s/^$1\b.*/$1 = $2/" metf_v35_bidask.py; }
reset() { sed -i '' "s/^$1\b.*/$1 = None/" metf_v35_bidask.py; }

# Disable sweep mode for marathon runs
patch RUN_DAY_FILTER_SWEEP False

run_filter() {
    local label="$1"
    local var="$2"
    local val="$3"

    echo ""
    echo "============================================================"
    echo "  FILTER: $label"
    echo "============================================================"
    patch "$var" "$val"
    python3 metf_v35_bidask.py 2>&1 | grep -E "Total P&L|Win rate|Max drawdown|Sharpe ratio|Calmar|Total trades"
    reset "$var"
}

# ── Baseline (no filter) ──
echo ""
echo "============================================================"
echo "  BASELINE (no day filter)"
echo "============================================================"
python3 metf_v35_bidask.py 2>&1 | grep -E "Total P&L|Win rate|Max drawdown|Sharpe ratio|Calmar|Total trades"

# ── Candidates ──
run_filter "vix_max=20"           "VIX_MAX_FILTER"         "20"
run_filter "above_sma200=True"    "DAY_FILTER_ABOVE_SMA200" "True"
run_filter "dAdx_min=25"          "DAY_FILTER_ADX_MIN"      "25"
run_filter "dRangePct_max=1.5"    "DAY_FILTER_RANGE_MAX"    "1.5"
run_filter "dExpMovePct_max=1.5"  "DAY_FILTER_EXP_MOVE_MAX" "1.5"

# ── Restore ──
patch RUN_DAY_FILTER_SWEEP True

echo ""
echo "All filters restored to None. RUN_DAY_FILTER_SWEEP restored to True."
echo "Full results in logs/run_history.json"
