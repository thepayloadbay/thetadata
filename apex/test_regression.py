#!/usr/bin/env python3
"""NASA-STD-7009A Regression Test Suite for Apex V8.

Compares current engine output against the gold trade log.
Any deviation indicates a code change broke the model.

Run: python3 test_regression.py
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GOLD_CSV = os.path.join(os.path.dirname(__file__), "gold_v8_trades.csv")
GOLD_JSON = os.path.join(os.path.dirname(__file__), "gold_v8_summary.json")

# Tolerance for floating point comparison
PNL_TOL = 0.01  # $0.01
CREDIT_TOL = 0.0001


def load_gold():
    """Load gold trade log and summary."""
    with open(GOLD_CSV) as f:
        trades = list(csv.DictReader(f))
    with open(GOLD_JSON) as f:
        summary = json.load(f)
    return trades, summary


def run_v8_current():
    """Run current V8 engine and return trades in same format as gold."""
    from apex_engine import (
        build_daily_indicators, _load_spx_1min, _get_spx_at_time,
        _load_option_quotes, _get_quote, _find_put_strikes, _find_call_strikes,
        _settle_spread, prior_day_ind, _load_oi, _get_nearby_oi,
    )
    import apex_config as _cfg
    from datetime import datetime

    indicators = build_daily_indicators()
    sd = "20220103"; ed = "20260325"
    dates = sorted(d for d in indicators.keys() if sd <= d <= ed)
    trades = []

    for d in dates:
        if d in _cfg.MARKET_HOLIDAYS or d in _cfg.EARLY_CLOSE_DAYS:
            continue
        prev = prior_day_ind(d)
        if prev is None:
            continue
        if abs(prev.get("day_return_pct", 0) or 0) > 1.5:
            continue
        dt = datetime.strptime(d, "%Y%m%d")
        dow = dt.weekday()

        spx_df = _load_spx_1min(d)
        if spx_df is None or "time_str" not in spx_df.columns:
            continue
        quotes_df = _load_option_quotes(d)
        if quotes_df is None:
            continue
        available = set(quotes_df.index.get_level_values("strike").unique())

        spx_exit = None
        for et in ["16:00:00", "15:59:00"]:
            bar = spx_df[spx_df["time_str"] == et]
            if not bar.empty:
                spx_exit = float(bar.iloc[0]["close"])
                break
        if spx_exit is None:
            spx_exit = float(spx_df["close"].iloc[-1])

        oi_df = _load_oi(d) if dow == 3 else None

        def get_best(entry_time, otm, direction="best", use_oi=False, oi_min=0):
            spot = _get_spx_at_time(spx_df, entry_time)
            if spot is None:
                return None
            sides = {}
            opts = ["CALL"] if direction == "CALL" else ["PUT", "CALL"]
            for opt_type in opts:
                right = "P" if opt_type == "PUT" else "C"
                strikes = (
                    _find_put_strikes(spot, otm, 10.0, available) if opt_type == "PUT"
                    else _find_call_strikes(spot, otm, 10.0, available)
                )
                if strikes is None:
                    continue
                short, long = strikes
                if use_oi and oi_df is not None:
                    nearby = _get_nearby_oi(oi_df, right, short, 10)
                    if nearby >= 0 and nearby < oi_min:
                        continue
                sq = _get_quote(quotes_df, right, short, entry_time)
                lq = _get_quote(quotes_df, right, long, entry_time)
                if not sq or not lq:
                    continue
                credit = sq["bid"] - lq["ask"]
                if credit < 0.10:
                    continue
                settle = _settle_spread(opt_type, short, long, spx_exit, 10.0)
                sides[opt_type] = {
                    "credit": round(credit, 4), "settle": round(settle, 4),
                    "short_strike": short, "long_strike": long, "spot": round(spot, 2),
                }
            if not sides:
                return None
            return max(sides.items(), key=lambda x: x[1]["credit"])

        # First entry
        if dow in {0, 1}:
            bar_1550 = spx_df[spx_df["time_str"] == "15:50:00"]
            e1 = "15:51:00" if (
                not bar_1550.empty
                and (float(bar_1550.iloc[0]["high"]) - float(bar_1550.iloc[0]["low"])) <= 5
            ) else "15:55:00"
            r1 = get_best(e1, 12.0)
        elif dow == 2:
            e1 = "15:55:00"
            r1 = get_best(e1, 12.0, direction="CALL")
        elif dow == 3:
            e1 = "15:55:00"
            r1 = get_best(e1, 15.0, use_oi=True, oi_min=1000)
        elif dow == 4:
            e1 = "15:55:00"
            r1 = get_best(e1, 10.0)
        else:
            continue

        if r1:
            opt_type, info = r1
            pnl = round((info["credit"] - info["settle"]) * 15 * 100.0 - 30, 2)
            trades.append({
                "date": d, "entry_time": e1, "entry_num": "1",
                "option_type": opt_type, "short_strike": str(info["short_strike"]),
                "long_strike": str(info["long_strike"]),
                "credit": str(info["credit"]), "settle": str(info["settle"]),
                "spot": str(info["spot"]), "spx_exit": str(round(spx_exit, 2)),
                "pnl": str(pnl),
            })

        # Second entry
        if dow == 2:
            r2 = get_best("15:57:00", 6.0, direction="CALL")
        elif dow == 3:
            r2 = None
        else:
            r2 = get_best("15:57:00", 8.0)

        if r2:
            opt_type2, info2 = r2
            pnl2 = round((info2["credit"] - info2["settle"]) * 15 * 100.0 - 30, 2)
            trades.append({
                "date": d, "entry_time": "15:57:00", "entry_num": "2",
                "option_type": opt_type2, "short_strike": str(info2["short_strike"]),
                "long_strike": str(info2["long_strike"]),
                "credit": str(info2["credit"]), "settle": str(info2["settle"]),
                "spot": str(info2["spot"]), "spx_exit": str(round(spx_exit, 2)),
                "pnl": str(pnl2),
            })

    return trades


def compare(gold_trades, current_trades):
    """Compare gold vs current trade logs. Return (pass_count, fail_count, failures)."""
    passes = 0
    fails = 0
    failures = []

    # Check trade count
    if len(gold_trades) != len(current_trades):
        fails += 1
        failures.append(f"Trade count: gold={len(gold_trades)}, current={len(current_trades)}")
    else:
        passes += 1

    # Compare each trade
    min_len = min(len(gold_trades), len(current_trades))
    for i in range(min_len):
        g = gold_trades[i]
        c = current_trades[i]

        # Date match
        if g["date"] != c["date"]:
            fails += 1
            failures.append(f"Trade {i}: date mismatch gold={g['date']} current={c['date']}")
            continue

        # Entry time
        if g["entry_time"] != c["entry_time"]:
            fails += 1
            failures.append(f"Trade {i} ({g['date']}): entry_time gold={g['entry_time']} current={c['entry_time']}")
            continue

        # Option type
        if g["option_type"] != c["option_type"]:
            fails += 1
            failures.append(f"Trade {i} ({g['date']}): type gold={g['option_type']} current={c['option_type']}")
            continue

        # Strikes
        if g["short_strike"] != c["short_strike"]:
            fails += 1
            failures.append(f"Trade {i} ({g['date']}): short gold={g['short_strike']} current={c['short_strike']}")
            continue

        # P&L
        pnl_diff = abs(float(g["pnl"]) - float(c["pnl"]))
        if pnl_diff > PNL_TOL:
            fails += 1
            failures.append(f"Trade {i} ({g['date']}): pnl gold=${g['pnl']} current=${c['pnl']} diff=${pnl_diff:.2f}")
            continue

        # Credit
        credit_diff = abs(float(g["credit"]) - float(c["credit"]))
        if credit_diff > CREDIT_TOL:
            fails += 1
            failures.append(f"Trade {i} ({g['date']}): credit gold={g['credit']} current={c['credit']}")
            continue

        passes += 1

    # Summary metrics
    gold_pnl = sum(float(t["pnl"]) for t in gold_trades)
    current_pnl = sum(float(t["pnl"]) for t in current_trades)
    if abs(gold_pnl - current_pnl) > 1.0:
        fails += 1
        failures.append(f"Total P&L: gold=${gold_pnl:,.2f} current=${current_pnl:,.2f}")
    else:
        passes += 1

    return passes, fails, failures


def main():
    print("=" * 60)
    print("APEX V8 REGRESSION TEST")
    print("Comparing current engine output to gold trade log")
    print("=" * 60)
    print()

    # Check gold files exist
    if not os.path.exists(GOLD_CSV) or not os.path.exists(GOLD_JSON):
        print("ERROR: Gold files not found. Run the gold generation script first.")
        sys.exit(1)

    # Load gold
    gold_trades, gold_summary = load_gold()
    print(f"Gold: {gold_summary['total_trades']} trades, ${gold_summary['total_pnl']:,.2f} P&L")

    # Run current engine
    print("Running current engine...")
    current_trades = run_v8_current()
    current_pnl = sum(float(t["pnl"]) for t in current_trades)
    print(f"Current: {len(current_trades)} trades, ${current_pnl:,.2f} P&L")
    print()

    # Compare
    passes, fails, failures = compare(gold_trades, current_trades)

    if failures:
        print(f"FAILURES ({fails}):")
        for f in failures[:20]:
            print(f"  {f}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
    print()

    print(f"{'=' * 60}")
    print(f"REGRESSION RESULTS: {passes} PASS, {fails} FAIL")
    if fails == 0:
        print("ALL TESTS PASSED — engine matches gold log exactly")
    else:
        print(f"*** {fails} REGRESSIONS DETECTED — investigate before deploying ***")
    print(f"{'=' * 60}")

    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
