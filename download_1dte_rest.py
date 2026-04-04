#!/usr/bin/env python3
"""REST-based 1DTE option data downloader for ThetaData v3.

Downloads quotes, greeks, and OI for 1DTE SPXW options (expiry = next trading day).
Uses direct REST API — no MCP dependency, works on remote machines with --host.

Usage:
  python3 download_1dte_rest.py --year 2024 --host 192.168.0.132
  python3 download_1dte_rest.py --year 2024 --start-date 20240101 --end-date 20240630
  python3 download_1dte_rest.py --year 2024 --force
  python3 download_1dte_rest.py --year 2024 --quotes-only
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

DATA_DIR = os.path.expanduser("~/Documents/thetadata/data")
STRIKE_STEP = 5
OTM_MIN = -150
OTM_MAX = 250
MAX_WORKERS = 10

MARKET_HOLIDAYS = {
    "20180101","20180115","20180219","20180330","20180528","20180704",
    "20180903","20181122","20181225",
    "20190101","20190121","20190218","20190419","20190527","20190704",
    "20190902","20191128","20191225",
    "20200101","20200120","20200217","20200410","20200525","20200703",
    "20200907","20201126","20201225",
    "20210101","20210118","20210215","20210402","20210531","20210705",
    "20210906","20211125","20211224",
    "20220117","20220221","20220415","20220530","20220620","20220704",
    "20220905","20221124","20221226",
    "20230102","20230116","20230220","20230407","20230529","20230619",
    "20230704","20230904","20231123","20231225",
    "20240101","20240115","20240219","20240329","20240527","20240619",
    "20240704","20240902","20241128","20241225",
    "20250101","20250109","20250120","20250217","20250418","20250526",
    "20250619","20250704","20250901","20251127","20251225",
    "20260101","20260119","20260216","20260403","20260525","20260619",
    "20260703","20260907","20261126","20261225",
}


def get_trading_days(year: int) -> list[str]:
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="B")
    return [d.strftime("%Y%m%d") for d in days if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS]


def get_expiry_for_1dte(trade_date: str) -> str | None:
    """Return the next trading day (1DTE expiry)."""
    year = int(trade_date[:4])
    all_days = get_trading_days(year) + get_trading_days(year + 1)
    try:
        idx = all_days.index(trade_date)
        if idx + 1 < len(all_days):
            return all_days[idx + 1]
    except ValueError:
        pass
    return None


def fetch_json(base_url, endpoint, params):
    params["format"] = "json"
    for attempt in range(3):
        try:
            resp = requests.get(f"{base_url}{endpoint}", params=params, timeout=60)
            if resp.status_code == 200:
                text = resp.text.strip()
                if text.startswith("No data") or text.startswith("no data"):
                    return None  # no retry needed
                return resp.json()
            elif resp.status_code == 472:
                time.sleep(2 * (attempt + 1))
            else:
                time.sleep(1)
        except Exception:
            time.sleep(2)
    return None


def fetch_quotes(base_url, expiry_fmt, trade_fmt, right, strike):
    """Fetch 1-min quotes for a single contract."""
    data = fetch_json(base_url, "/v3/option/history/quote", {
        "symbol": "SPXW", "expiration": expiry_fmt,
        "strike": str(float(strike)), "right": right,
        "start_date": trade_fmt, "end_date": trade_fmt, "interval": "1m",
    })
    rows = []
    if data and data.get("response"):
        r = "P" if right == "put" else "C"
        for item in data["response"]:
            for bar in item.get("data", []):
                bid = float(bar.get("bid", 0) or 0)
                ask = float(bar.get("ask", 0) or 0)
                rows.append({
                    "right": r, "strike": strike,
                    "timestamp": bar.get("timestamp", ""),
                    "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4),
                })
    return rows


def fetch_greeks(base_url, expiry_fmt, trade_fmt, right, strike):
    """Fetch 1-min first-order greeks for a single contract."""
    data = fetch_json(base_url, "/v3/option/history/greeks/first_order", {
        "symbol": "SPXW", "expiration": expiry_fmt,
        "strike": str(float(strike)), "right": right,
        "start_date": trade_fmt, "end_date": trade_fmt, "interval": "1m",
    })
    rows = []
    if data and data.get("response"):
        r = "P" if right == "put" else "C"
        for item in data["response"]:
            for bar in item.get("data", []):
                rows.append({
                    "right": r, "strike": strike,
                    "timestamp": bar.get("timestamp", ""),
                    "bid": float(bar.get("bid", 0) or 0),
                    "ask": float(bar.get("ask", 0) or 0),
                    "delta": float(bar.get("delta", 0) or 0),
                    "theta": float(bar.get("theta", 0) or 0),
                    "vega": float(bar.get("vega", 0) or 0),
                    "rho": float(bar.get("rho", 0) or 0),
                    "iv": float(bar.get("iv", 0) or 0),
                })
    return rows


def fetch_oi(base_url, expiry_fmt, trade_fmt, right, strike):
    """Fetch open interest for a single contract."""
    data = fetch_json(base_url, "/v3/option/history/open_interest", {
        "symbol": "SPXW", "expiration": expiry_fmt,
        "strike": str(float(strike)), "right": right,
        "start_date": trade_fmt, "end_date": trade_fmt,
    })
    rows = []
    if data and data.get("response"):
        r = "P" if right == "put" else "C"
        for item in data["response"]:
            for bar in item.get("data", []):
                rows.append({
                    "right": r, "strike": strike,
                    "timestamp": bar.get("timestamp", ""),
                    "open_interest": int(bar.get("open_interest", 0) or 0),
                })
    return rows


def is_saved(data_dir, year, subdir, date_str):
    path = os.path.join(data_dir, str(year), subdir, f"{date_str}.parquet")
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_parquet(path)
        return len(df) > 0
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        return False


def download_day(base_url, date_str, year, force=False, quotes_only=False, workers=10):
    """Download 1DTE option data for one trading day."""
    expiry = get_expiry_for_1dte(date_str)
    if expiry is None:
        print(f"  {date_str}: cannot compute 1DTE expiry, skipping")
        return "skip"

    expiry_fmt = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    trade_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    # Check what's already done
    q_dir = f"option_quotes_1dte"
    g_dir = f"option_greeks_1dte"
    o_dir = f"option_open_interest_1dte"

    if not force:
        if quotes_only and is_saved(DATA_DIR, year, q_dir, date_str):
            return "skip"
        if not quotes_only and all(is_saved(DATA_DIR, year, d, date_str) for d in [q_dir, g_dir, o_dir]):
            return "skip"

    # Get SPX open price for strike range
    spx_path = os.path.join(DATA_DIR, str(year), "spx_ohlc", f"{date_str}.parquet")
    open_price = None
    if os.path.exists(spx_path):
        try:
            spx = pd.read_parquet(spx_path)
            if not spx.empty:
                open_price = float(spx["open"].iloc[0])
        except Exception:
            pass

    if open_price is None or open_price < 1000:
        # Try fetching from API
        data = fetch_json(base_url, "/v3/index/history/ohlc", {
            "symbol": "SPX", "start_date": trade_fmt, "end_date": trade_fmt, "interval": "1m",
        })
        if data and data.get("response"):
            for item in data["response"]:
                o = item.get("open") or item.get("close")
                if o and float(o) > 1000:
                    open_price = float(o)
                    break

    if open_price is None or open_price < 1000:
        print(f"  {date_str}: no SPX open, skipping")
        return "no_spx"

    base = round(open_price / STRIKE_STEP) * STRIKE_STEP

    # ── Probe: check if this expiry actually exists ──
    # Before April 2022, SPXW only had Mon/Wed/Fri expirations
    probe = fetch_json(base_url, "/v3/option/history/quote", {
        "symbol": "SPXW", "expiration": expiry_fmt,
        "strike": str(float(base)), "right": "put",
        "start_date": trade_fmt, "end_date": trade_fmt, "interval": "1m",
    })
    if probe is None or not probe.get("response"):
        print(f"  {date_str}: exp={expiry} no expiry available, skipping")
        return "no_expiry"

    put_strikes = list(range(int(base) - OTM_MAX, int(base) - OTM_MIN + 1, STRIKE_STEP))
    call_strikes = list(range(int(base) + OTM_MIN, int(base) + OTM_MAX + 1, STRIKE_STEP))
    contracts = [("put", s) for s in put_strikes] + [("call", s) for s in call_strikes]

    # ── Fetch quotes ──
    q_rows = []
    if force or not is_saved(DATA_DIR, year, q_dir, date_str):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_quotes, base_url, expiry_fmt, trade_fmt, r, s): (r, s)
                       for r, s in contracts}
            for future in as_completed(futures):
                q_rows.extend(future.result())

        if q_rows:
            out_dir = os.path.join(DATA_DIR, str(year), q_dir)
            os.makedirs(out_dir, exist_ok=True)
            df = pd.DataFrame(q_rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(os.path.join(out_dir, f"{date_str}.parquet"), index=False)

    if quotes_only:
        strikes = len(set(r["strike"] for r in q_rows)) if q_rows else 0
        print(f"  {date_str}: exp={expiry} {len(q_rows):,} quotes ({strikes} strikes)")
        return "ok" if q_rows else "no_data"

    # ── Fetch greeks ──
    g_rows = []
    if force or not is_saved(DATA_DIR, year, g_dir, date_str):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_greeks, base_url, expiry_fmt, trade_fmt, r, s): (r, s)
                       for r, s in contracts}
            for future in as_completed(futures):
                g_rows.extend(future.result())

        if g_rows:
            out_dir = os.path.join(DATA_DIR, str(year), g_dir)
            os.makedirs(out_dir, exist_ok=True)
            df = pd.DataFrame(g_rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(os.path.join(out_dir, f"{date_str}.parquet"), index=False)

    # ── Fetch OI ──
    o_rows = []
    if force or not is_saved(DATA_DIR, year, o_dir, date_str):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_oi, base_url, expiry_fmt, trade_fmt, r, s): (r, s)
                       for r, s in contracts}
            for future in as_completed(futures):
                o_rows.extend(future.result())

        if o_rows:
            out_dir = os.path.join(DATA_DIR, str(year), o_dir)
            os.makedirs(out_dir, exist_ok=True)
            df = pd.DataFrame(o_rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(os.path.join(out_dir, f"{date_str}.parquet"), index=False)

    strikes = len(set(r["strike"] for r in q_rows)) if q_rows else 0
    print(f"  {date_str}: exp={expiry} q={len(q_rows):,} g={len(g_rows):,} oi={len(o_rows):,} ({strikes} strikes)")
    return "ok" if q_rows else "no_data"


def main():
    parser = argparse.ArgumentParser(description="Download 1DTE SPXW option data via REST API")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="ThetaData Terminal host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=25503)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--quotes-only", action="store_true",
                        help="Only download quotes (skip greeks and OI)")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"

    print(f"Testing {base_url}...")
    test = fetch_json(base_url, "/v3/index/snapshot/ohlc", {"symbol": "SPX"})
    if not test:
        print(f"ERROR: Cannot reach ThetaData at {base_url}")
        sys.exit(1)
    print(f"Connected to ThetaData at {base_url}")

    days = get_trading_days(args.year)
    if args.start_date:
        days = [d for d in days if d >= args.start_date]
    if args.end_date:
        days = [d for d in days if d <= args.end_date]

    mode = "quotes only" if args.quotes_only else "quotes + greeks + OI"
    print(f"\n{'='*60}")
    print(f"  1DTE Download — {args.year}")
    print(f"  Trading days : {len(days)}")
    print(f"  Data types   : {mode}")
    print(f"  Strike range : {OTM_MIN} to +{OTM_MAX} from open")
    print(f"  Workers      : {args.workers}")
    print(f"  Output       : {DATA_DIR}/{args.year}/option_*_1dte/")
    print(f"{'='*60}\n")

    ok = skip = fail = 0
    t0 = time.time()
    for i, ds in enumerate(days):
        result = download_day(base_url, ds, args.year, args.force, args.quotes_only, args.workers)
        if result == "ok":
            ok += 1
        elif result == "skip":
            skip += 1
        else:
            fail += 1
        elapsed = time.time() - t0
        rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
        remaining = (len(days) - i - 1) / rate if rate > 0 else 0
        if (i + 1) % 5 == 0:
            print(f"  [{i+1}/{len(days)}] {ok} ok, {skip} skip, {fail} fail | "
                  f"{rate:.1f} days/min, ~{remaining:.0f} min left")

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  Done! ok={ok}, skip={skip}, fail={fail}, time={elapsed:.0f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
