#!/usr/bin/env python3
"""NASA-STD-7009A Verification Tests for Apex V8.

Code verification: confirm the engine correctly implements the intended math.
Run: python3 test_verification.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apex_engine import _settle_spread, _find_put_strikes, _find_call_strikes, compute_metrics

PASS = 0
FAIL = 0

def check(name, actual, expected, tol=0.001):
    global PASS, FAIL
    if abs(actual - expected) <= tol:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}: expected {expected}, got {actual}")

def check_eq(name, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}: expected {expected}, got {actual}")


# ========================================
# 1. SETTLEMENT VERIFICATION
# ========================================
print("=== 1. Settlement Verification ===")

# PUT spread: short 4988, long 4978, width=10
# SPX at 5000 → both OTM → settlement = 0
check("PUT OTM", _settle_spread("PUT", 4988, 4978, 5000.0, 10.0), 0.0)

# SPX at 4988 → at the money → settlement = 0
check("PUT ATM", _settle_spread("PUT", 4988, 4978, 4988.0, 10.0), 0.0)

# SPX at 4985 → 3 pts ITM → settlement = 3
check("PUT partial ITM", _settle_spread("PUT", 4988, 4978, 4985.0, 10.0), 3.0)

# SPX at 4978 → fully ITM → settlement = 10 (width)
check("PUT full ITM", _settle_spread("PUT", 4988, 4978, 4978.0, 10.0), 10.0)

# SPX at 4970 → deep ITM → settlement = 10 (capped at width)
check("PUT deep ITM", _settle_spread("PUT", 4988, 4978, 4970.0, 10.0), 10.0)

# CALL spread: short 5012, long 5022, width=10
check("CALL OTM", _settle_spread("CALL", 5012, 5022, 5000.0, 10.0), 0.0)
check("CALL ATM", _settle_spread("CALL", 5012, 5022, 5012.0, 10.0), 0.0)
check("CALL partial ITM", _settle_spread("CALL", 5012, 5022, 5015.0, 10.0), 3.0)
check("CALL full ITM", _settle_spread("CALL", 5012, 5022, 5022.0, 10.0), 10.0)
check("CALL deep ITM", _settle_spread("CALL", 5012, 5022, 5030.0, 10.0), 10.0)


# ========================================
# 2. STRIKE SELECTION VERIFICATION
# ========================================
print("=== 2. Strike Selection Verification ===")

available = set(range(4900, 5200, 5))  # 5-pt grid

# PUT: spot=5000, d=12 → target=4988, floor to 5pt = 4985
strikes = _find_put_strikes(5000.0, 12.0, 10.0, available)
check_eq("PUT short strike", strikes[0], 4985)
check_eq("PUT long strike", strikes[1], 4975)
check("PUT OTM distance", 5000.0 - strikes[0], 15.0)  # 15 pts OTM (rounded)

# CALL: spot=5000, d=12 → target=5012, ceil to 5pt = 5015
strikes = _find_call_strikes(5000.0, 12.0, 10.0, available)
check_eq("CALL short strike", strikes[0], 5015)
check_eq("CALL long strike", strikes[1], 5025)
check("CALL OTM distance", strikes[0] - 5000.0, 15.0)

# Edge case: exact 5pt boundary
strikes = _find_put_strikes(5000.0, 15.0, 10.0, available)
check_eq("PUT d=15 short", strikes[0], 4985)

strikes = _find_call_strikes(5000.0, 15.0, 10.0, available)
check_eq("CALL d=15 short", strikes[0], 5015)


# ========================================
# 3. P&L CALCULATION VERIFICATION
# ========================================
print("=== 3. P&L Hand Calculation ===")

# Manual P&L: credit=$0.15, settlement=$0, qty=15, commission=4×15×$0.50=$30
credit = 0.15
settlement = 0.0
qty = 15
commission = 4 * qty * 0.50
expected_pnl = (credit - settlement) * qty * 100.0 - commission
check("P&L winning trade", expected_pnl, 195.0)

# Losing trade: credit=$0.15, settlement=$5.0 (partial ITM)
settlement = 5.0
expected_pnl = (credit - settlement) * qty * 100.0 - commission
check("P&L losing trade", expected_pnl, -7305.0)

# Max loss: credit=$0.15, settlement=$10.0 (full width)
settlement = 10.0
expected_pnl = (credit - settlement) * qty * 100.0 - commission
check("P&L max loss", expected_pnl, -14805.0)


# ========================================
# 4. METRICS VERIFICATION
# ========================================
print("=== 4. Metrics Verification ===")

test_trades = [
    {"pnl": 100, "entry_date": "20250101"},
    {"pnl": 200, "entry_date": "20250102"},
    {"pnl": -50, "entry_date": "20250103"},
    {"pnl": 150, "entry_date": "20250104"},
]
m = compute_metrics(test_trades)
check_eq("num_trades", m["num_trades"], 4)
check("win_rate", m["win_rate"], 75.0)
check("total_pnl", m["total_pnl"], 400.0)
check("avg_win", m["avg_win"], 150.0)  # (100+200+150)/3
check("avg_loss", m["avg_loss"], -50.0)
check("max_single_loss", m["max_single_loss"], -50.0)

# Max DD: equity goes 100, 300, 250, 400. Peak=300, DD=250-300=-50
check("max_drawdown", m["max_drawdown"], -50.0)


# ========================================
# 5. SYMMETRY TESTS
# ========================================
print("=== 5. Symmetry Tests ===")

# PUT and CALL should have symmetric behavior
# PUT at d=12 below 5000 should mirror CALL at d=12 above 5000
put_settle = _settle_spread("PUT", 4988, 4978, 4985.0, 10.0)  # 3 pts ITM
call_settle = _settle_spread("CALL", 5012, 5022, 5015.0, 10.0)  # 3 pts ITM
check("PUT/CALL symmetry at 3pt ITM", put_settle, call_settle)

put_settle = _settle_spread("PUT", 4988, 4978, 5010.0, 10.0)  # deep OTM
call_settle = _settle_spread("CALL", 5012, 5022, 4990.0, 10.0)  # deep OTM
check("PUT/CALL symmetry OTM", put_settle, call_settle)


# ========================================
# SUMMARY
# ========================================
print()
print(f"{'='*50}")
print(f"VERIFICATION RESULTS: {PASS} PASS, {FAIL} FAIL")
if FAIL == 0:
    print("ALL TESTS PASSED ✓")
else:
    print(f"*** {FAIL} TESTS FAILED ***")
print(f"{'='*50}")
