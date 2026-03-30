#!/usr/bin/env python3
"""
download_data.py — Download and cache ThetaData for SPXW 0DTE backtesting.

Saves one Parquet file per day per data type under data/{YEAR}/:

  spx_ohlc/YYYYMMDD.parquet          SPX 1-min OHLC bars
  vix/YYYYMMDD.parquet                VIX daily close
  vix_ohlc/YYYYMMDD.parquet           VIX 1-min OHLC bars (intraday)
  option_quotes/YYYYMMDD.parquet      SPXW bid/ask 1-min for all OTM strikes (PUT + CALL)
  option_greeks/YYYYMMDD.parquet      First-order greeks 1-min (delta, theta, vega, rho, IV)
  option_open_interest/YYYYMMDD.parquet  Open interest per strike (PUT + CALL)

Usage:
  python download_data.py           # download DOWNLOAD_YEAR, skip already-saved days
  python download_data.py --force   # re-download all days (overwrite existing files)
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd
from mcp import ClientSession
from mcp.client.sse import sse_client

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
DOWNLOAD_YEAR  = 2025          # overridden by --year argument at runtime
MCP_URL        = "http://127.0.0.1:25503/mcp/sse"  # overridden by --mcp-url argument
DATA_DIR       = "data"

# Strike range to download: OTM_MIN to OTM_MAX points from the day's open price.
# OTM_MAX=250 covers all realistic short strikes (up to ~140 OTM) plus
# the long leg at maximum spread width (50), with a comfortable buffer.
OTM_MIN        = 5
OTM_MAX        = 250
STRIKE_STEP    = 5

# Max concurrent API calls per day (tune down if you hit rate limits)
CONCURRENCY    = 10

# Rough SPX level per year — used as the starting strike when inferring
# the open price via put-call parity (fallback for years where index OHLC
# requires a Professional subscription).
SPX_APPROX_BY_YEAR = {
    2018: 2700, 2019: 2950, 2020: 3100, 2021: 3750,
}

# Load daily SPX close lookup (from yfinance) for more accurate strike inference
_SPX_DAILY_CLOSE = {}
_spx_close_path = os.path.join(DATA_DIR, "spx_daily_close.csv")
if os.path.exists(_spx_close_path):
    import csv
    with open(_spx_close_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            _SPX_DAILY_CLOSE[row["Date"]] = float(row["close"])


def _get_approx_spx(date_str: str) -> int | None:
    """Get approximate SPX level for a date, using daily close lookup or yearly fallback."""
    # Try exact date first, then prior days (weekends/holidays)
    import datetime
    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
    for offset in range(0, 5):
        d = (dt - datetime.timedelta(days=offset)).strftime("%Y%m%d")
        if d in _SPX_DAILY_CLOSE:
            return round(_SPX_DAILY_CLOSE[d] / STRIKE_STEP) * STRIKE_STEP
    # Fallback to yearly approximation
    year = int(date_str[:4])
    approx = SPX_APPROX_BY_YEAR.get(year)
    if approx is not None:
        return round(approx / STRIKE_STEP) * STRIKE_STEP
    return None

MARKET_HOLIDAYS = {
    # 2023
    "20230102","20230116","20230220","20230407","20230529","20230619",
    "20230704","20230904","20231123","20231225",
    # 2024
    "20240101","20240115","20240219","20240329","20240527","20240619",
    "20240704","20240902","20241128","20241225",
    # 2025
    "20250101","20250109",  # 20250109 = National Day of Mourning (Carter)
    "20250120","20250217","20250418","20250526","20250619",
    "20250704","20250901","20251127","20251225",
    # 2026
    "20260101","20260119","20260216","20260403","20260525","20260619",
    "20260703","20260907","20261126","20261225",
}

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def day_path(subdir: str, date_str: str) -> str:
    folder = os.path.join(DATA_DIR, str(DOWNLOAD_YEAR), subdir)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{date_str}.parquet")


def is_saved(subdir: str, date_str: str) -> bool:
    """Return True only if the Parquet file exists AND can be read with >0 rows."""
    path = day_path(subdir, date_str)
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_parquet(path)
        if len(df) == 0:
            os.remove(path)
            return False
        return True
    except Exception:
        # Corrupted file — remove so it gets re-downloaded
        try:
            os.remove(path)
        except OSError:
            pass
        return False


CALL_TIMEOUT = 60  # seconds per individual MCP call before treating as hung
DAY_TIMEOUT  = 900  # seconds (15 min) max per trading day before skipping


async def call_tool(session, name: str, params: dict):
    """Call an MCP tool and return parsed JSON, or None on error."""
    result = await asyncio.wait_for(session.call_tool(name, params), timeout=CALL_TIMEOUT)
    if not result.content:
        return None
    raw = result.content[0].text
    if result.isError:
        return {"_error": raw}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # ThetaData sometimes truncates large responses — try to repair closing chars.
        # Responses vary in nesting depth: index OHLC is {"response":[...]} (depth 1),
        # option quotes/greeks are {"response":[{"contract":...,"data":[...]}]} (depth 2).
        for suffix in ("]}", "]}]}", "\n]}", "\n\t]\n}", "\n\t\t]\n\t}\n\t]\n}", "]", "}"):
            try:
                return json.loads(raw + suffix)
            except json.JSONDecodeError:
                continue
        return {"_error": f"parse_failed: {raw[:200]}"}


async def call_with_retry(session, name: str, params: dict, retries: int = 5):
    for attempt in range(retries):
        try:
            data = await call_tool(session, name, params)
            if data is not None and "_error" not in data:
                return data
        except asyncio.TimeoutError:
            data = {"_error": "timeout"}
        except Exception as exc:
            # Network-level error — treat like a failed attempt
            data = {"_error": str(exc)}
        if isinstance(data, dict) and "_error" in data:
            err_msg = data["_error"]
            # Terminal errors — no point retrying
            if "no data found" in err_msg.lower():
                return None
            if "professional subscription" in err_msg.lower():
                return None
            # Re-raise connection/timeout errors so the outer reconnect loop catches them
            if attempt == retries - 1 and any(k in err_msg.lower() for k in ("timeout", "connection", "eof", "broken pipe", "reset")):
                raise ConnectionError(err_msg)
        if attempt < retries - 1:
            wait = min(2 ** attempt, 30)  # exponential backoff, cap at 30s
            print(f"  [retry {attempt+1}/{retries}] {data.get('_error','?')}")
            await asyncio.sleep(wait)
    return None


# ─────────────────────────────────────────────
#  PARSERS
# ─────────────────────────────────────────────
def parse_ohlc(data) -> pd.DataFrame | None:
    """Parse index_history_ohlc response → DataFrame."""
    if data is None:
        return None
    rows = data if isinstance(data, list) else data.get("response", [])
    if not rows:
        return None
    records = []
    for item in rows:
        if isinstance(item, dict) and "data" in item:
            for bar in item["data"]:
                records.append(bar)
        elif isinstance(item, dict) and "timestamp" in item:
            records.append(item)
        else:
            records.append(item)
    return pd.DataFrame(records) if records else None


def parse_quote_response(data, right: str, strike: int) -> list[dict]:
    """Parse option_history_quote response → list of flat dicts."""
    if data is None:
        return []
    items = data if isinstance(data, list) else data.get("response", [])
    if not items:
        return []
    rows = []
    for item in items:
        bars = item.get("data", []) if isinstance(item, dict) else []
        for bar in bars:
            ts = bar.get("timestamp", "")
            rows.append({
                "right":     right,
                "strike":    strike,
                "timestamp": ts,
                "bid":       float(bar.get("bid", 0.0)),
                "ask":       float(bar.get("ask", 0.0)),
                "mid":       round((float(bar.get("bid", 0.0)) + float(bar.get("ask", 0.0))) / 2, 4),
            })
    return rows


def parse_greeks_response(data, right: str, strike: int) -> list[dict]:
    """Parse option_history_greeks_first_order response → list of flat dicts."""
    if data is None:
        return []
    items = data if isinstance(data, list) else data.get("response", [])
    if not items:
        return []
    rows = []
    for item in items:
        bars = item.get("data", []) if isinstance(item, dict) else []
        for bar in bars:
            rows.append({
                "right":     right,
                "strike":    strike,
                "timestamp": bar.get("timestamp", ""),
                "bid":       float(bar.get("bid", 0.0)),
                "ask":       float(bar.get("ask", 0.0)),
                "delta":     float(bar.get("delta", 0.0)),
                "theta":     float(bar.get("theta", 0.0)),
                "vega":      float(bar.get("vega", 0.0)),
                "rho":       float(bar.get("rho", 0.0)),
                "iv":        float(bar.get("iv", 0.0)),
            })
    return rows


# ─────────────────────────────────────────────
#  SPX OPEN PRICE INFERENCE (put-call parity)
# ─────────────────────────────────────────────
async def infer_spx_open_from_options(session, date_str: str) -> float | None:
    """
    Infer SPX open price via put-call parity when index OHLC requires Pro plan.
    Uses the first bar of a call+put at a known approximate strike:
        underlying ≈ strike + call_mid - put_mid
    Returns None if option quotes are unavailable.
    """
    approx_strike = _get_approx_spx(date_str)
    if approx_strike is None:
        return None

    def first_mid(data):
        rows = data if isinstance(data, list) else (data or {}).get("response", [])
        for contract in rows:
            if not isinstance(contract, dict):
                continue
            for bar in contract.get("data", []):
                bid, ask = bar.get("bid", 0), bar.get("ask", 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
        return None

    call_data = await call_with_retry(session, "option_history_quote", {
        "symbol": "SPXW", "expiration": date_str, "date": date_str,
        "strike": approx_strike, "right": "C", "interval": "1m",
    })
    put_data = await call_with_retry(session, "option_history_quote", {
        "symbol": "SPXW", "expiration": date_str, "date": date_str,
        "strike": approx_strike, "right": "P", "interval": "1m",
    })
    call_mid = first_mid(call_data)
    put_mid = first_mid(put_data)
    if call_mid is None or put_mid is None:
        return None
    return approx_strike + call_mid - put_mid


# ─────────────────────────────────────────────
#  PER-DAY DOWNLOAD
# ─────────────────────────────────────────────
async def download_day(
    session,
    date_str: str,
    force: bool,
    greeks_available: bool,
) -> tuple[str, bool]:
    """
    Download all data types for one trading day.
    Returns (date_str, greeks_still_available).
    """
    expiry = date_str  # 0DTE: expiry == trade date

    # ── 1. SPX 1-min OHLC ──
    open_price = None
    if force or not is_saved("spx_ohlc", date_str):
        data = await call_with_retry(session, "index_history_ohlc", {
            "symbol": "SPX", "start_date": date_str,
            "end_date": date_str, "interval": "1m",
        })
        df = parse_ohlc(data)
        if df is not None and not df.empty:
            df.to_parquet(day_path("spx_ohlc", date_str), index=False)
            print(f"  [spx_ohlc]  {len(df)} bars")
        else:
            # Index OHLC may require Pro for older years — try put-call parity fallback
            inferred = await infer_spx_open_from_options(session, date_str)
            if inferred is not None:
                open_price = inferred
                print(f"  [spx_ohlc]  NO DATA (inferred open {open_price:.0f} via put-call parity)")
            else:
                print(f"  [spx_ohlc]  NO DATA")
                return date_str, greeks_available

    # Get open price from saved SPX data to determine strike range (if not already inferred)
    if open_price is None:
        try:
            spx_df = pd.read_parquet(day_path("spx_ohlc", date_str))
            # find open price — first bar's open or close
            open_col = next((c for c in ["open", "Open", "close", "Close"] if c in spx_df.columns), None)
            if open_col:
                open_price = float(spx_df[open_col].iloc[0])
            else:
                print(f"  [strikes]   Cannot determine open price — skipping options")
                return date_str, greeks_available
        except Exception as e:
            print(f"  [spx_ohlc]  read error: {e}")
            return date_str, greeks_available

    # ── 2. VIX ──
    if force or not is_saved("vix", date_str):
        data = await call_with_retry(session, "index_history_eod", {
            "symbol": "VIX", "start_date": date_str, "end_date": date_str,
        })
        if data is not None:
            closes = re.findall(r'"close":\s*([\d\.]+)', json.dumps(data))
            vix_close = float(closes[-1]) if closes else None
            if vix_close is not None:
                df = pd.DataFrame([{"date": date_str, "vix_close": vix_close}])
                df.to_parquet(day_path("vix", date_str), index=False)
                print(f"  [vix]       {vix_close:.2f}")
            else:
                print(f"  [vix]       NO DATA")

    # ── 3. VIX 1-min OHLC (intraday) ──
    if force or not is_saved("vix_ohlc", date_str):
        data = await call_with_retry(session, "index_history_ohlc", {
            "symbol": "VIX", "start_date": date_str,
            "end_date": date_str, "interval": "1m",
        })
        df = parse_ohlc(data)
        if df is not None and not df.empty:
            df.to_parquet(day_path("vix_ohlc", date_str), index=False)
            print(f"  [vix_ohlc]  {len(df)} bars")
        else:
            print(f"  [vix_ohlc]  NO DATA")

    # Determine which strikes to download (OTM range from open price)
    base          = round(open_price / STRIKE_STEP) * STRIKE_STEP
    put_strikes   = list(range(int(base) - OTM_MAX, int(base) - OTM_MIN + 1, STRIKE_STEP))
    call_strikes  = list(range(int(base) + OTM_MIN, int(base) + OTM_MAX + 1, STRIKE_STEP))
    all_contracts = [("P", s) for s in put_strikes] + [("C", s) for s in call_strikes]

    sem = asyncio.Semaphore(CONCURRENCY)

    # ── 4. Option quotes (bid/ask 1-min) ──
    if force or not is_saved("option_quotes", date_str):
        async def fetch_quote(right, strike):
            async with sem:
                data = await call_with_retry(session, "option_history_quote", {
                    "symbol": "SPXW", "expiration": expiry,
                    "strike": str(strike), "right": right,
                    "date": date_str, "interval": "1m",
                })
                return parse_quote_response(data, right, strike)

        tasks   = [fetch_quote(r, s) for r, s in all_contracts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rows    = [row for batch in results if isinstance(batch, list) for row in batch]

        if rows:
            df = pd.DataFrame(rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(day_path("option_quotes", date_str), index=False)
            print(f"  [quotes]    {len(df):,} rows ({len(all_contracts)} contracts)")
        else:
            print(f"  [quotes]    NO DATA")

    # ── 5. First-order greeks (delta, theta, vega, rho, IV — standard plan) ──
    if greeks_available and (force or not is_saved("option_greeks", date_str)):
        async def fetch_greeks(right, strike):
            async with sem:
                data = await call_with_retry(session, "option_history_greeks_first_order", {
                    "symbol": "SPXW", "expiration": expiry,
                    "strike": str(strike), "right": right,
                    "date": date_str, "interval": "1m",
                })
                return parse_greeks_response(data, right, strike)

        tasks   = [fetch_greeks(r, s) for r, s in all_contracts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rows    = [row for batch in results if isinstance(batch, list) for row in batch]

        if rows:
            df = pd.DataFrame(rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(day_path("option_greeks", date_str), index=False)
            print(f"  [greeks]    {len(df):,} rows")
        else:
            print(f"  [greeks]    NO DATA")

    # ── 6. Open interest per strike ──
    if force or not is_saved("option_open_interest", date_str):
        async def fetch_oi(right, strike):
            async with sem:
                data = await call_with_retry(session, "option_history_open_interest", {
                    "symbol": "SPXW", "expiration": expiry,
                    "strike": str(strike), "right": right,
                    "date": date_str,
                })
                if data is None:
                    return []
                items = data if isinstance(data, list) else data.get("response", [])
                rows = []
                for item in items:
                    for bar in (item.get("data", []) if isinstance(item, dict) else []):
                        rows.append({
                            "right":         right,
                            "strike":        strike,
                            "timestamp":     bar.get("timestamp", ""),
                            "open_interest": int(bar.get("open_interest", 0)),
                        })
                return rows

        tasks   = [fetch_oi(r, s) for r, s in all_contracts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rows    = [row for batch in results if isinstance(batch, list) for row in batch]

        if rows:
            df = pd.DataFrame(rows)
            df["strike"] = df["strike"].astype("int32")
            df.to_parquet(day_path("option_open_interest", date_str), index=False)
            print(f"  [open_int]  {len(df):,} rows")
        else:
            print(f"  [open_int]  NO DATA")

    return date_str, greeks_available


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def download_spx_ohlc_seed(session, seed_days: int, force: bool):
    """Download the last `seed_days` trading days of SPX OHLC from the prior year.

    Stored in data/{YEAR-1}/spx_ohlc/ — same structure as the main download so
    is_saved() handles resume correctly and no merging is needed.
    Only SPX OHLC is fetched (all that's needed for EMA seeding).
    """
    prior_year = DOWNLOAD_YEAR - 1
    # Temporarily override DOWNLOAD_YEAR for day_path() by patching the folder directly
    folder = os.path.join(DATA_DIR, str(prior_year), "spx_ohlc")
    os.makedirs(folder, exist_ok=True)

    def seed_path(date_str):
        return os.path.join(folder, f"{date_str}.parquet")

    def seed_saved(date_str):
        path = seed_path(date_str)
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

    prior_days = pd.date_range(
        f"{prior_year}-01-01", f"{prior_year}-12-31", freq="B"
    )
    seed_date_list = [d.strftime("%Y%m%d") for d in prior_days][-seed_days:]

    needed = [d for d in seed_date_list if force or not seed_saved(d)]
    if not needed:
        print(f"  [seed]  All {seed_days} prior-year bars already saved — skipping")
        return

    print(f"  [seed]  Downloading {len(needed)} day(s) of prior-year SPX OHLC "
          f"({seed_date_list[0]}–{seed_date_list[-1]})")

    for date_str in needed:
        data = await call_with_retry(session, "index_history_ohlc", {
            "symbol": "SPX", "start_date": date_str,
            "end_date": date_str, "interval": "1m",
        })
        df = parse_ohlc(data)
        if df is not None and not df.empty:
            df.to_parquet(seed_path(date_str), index=False)
            print(f"  [seed]    {date_str} — {len(df)} bars")
        else:
            print(f"  [seed]    {date_str} — NO DATA")


async def main(
    force: bool,
    max_days: int | None = None,
    seed_days: int = 5,
    start_date: str | None = None,
    end_date: str | None = None,
    year: int | None = None,
    mcp_url: str | None = None,
):
    global DOWNLOAD_YEAR, MCP_URL
    if year is not None:
        DOWNLOAD_YEAR = year
    if mcp_url is not None:
        MCP_URL = mcp_url

    date_list = pd.date_range(
        f"{DOWNLOAD_YEAR}-01-01", f"{DOWNLOAD_YEAR}-12-31", freq="B"
    )
    all_trading_days = [
        d.strftime("%Y%m%d") for d in date_list
        if d.strftime("%Y%m%d") not in MARKET_HOLIDAYS
    ]

    # Filter to requested date range (for splitting work across multiple machines)
    if start_date:
        all_trading_days = [d for d in all_trading_days if d >= start_date]
    if end_date:
        all_trading_days = [d for d in all_trading_days if d <= end_date]

    range_label = f"{all_trading_days[0]}–{all_trading_days[-1]}" if all_trading_days else "none"
    print(f"{'='*60}")
    print(f"  ThetaData Download — {DOWNLOAD_YEAR}")
    print(f"  Date range   : {range_label}")
    print(f"  Trading days : {len(all_trading_days)}")
    print(f"  Strike range : ±{OTM_MIN}–{OTM_MAX} OTM (step {STRIKE_STEP})")
    print(f"  Output dir   : {os.path.abspath(DATA_DIR)}/{DOWNLOAD_YEAR}/")
    print(f"  Seed days    : {seed_days} (prior-year SPX OHLC for EMA)")
    print(f"  Force        : {force}")
    print(f"{'='*60}")

    types_needed     = ["spx_ohlc", "vix", "vix_ohlc", "option_quotes", "option_greeks", "option_open_interest"]
    greeks_available = True
    completed        = 0
    skipped          = 0

    # Build the list of days that still need work (supports resuming after a crash)
    remaining = [
        (i, d) for i, d in enumerate(all_trading_days)
        if force or not all(is_saved(t, d) for t in types_needed)
    ]
    skipped = len(all_trading_days) - len(remaining)
    if max_days is not None:
        remaining = remaining[:max_days]

    MAX_RECONNECTS = 10
    reconnect_wait = 5  # seconds, doubles each attempt

    for reconnect in range(MAX_RECONNECTS):
        if not remaining:
            break
        try:
            async with sse_client(MCP_URL) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    reconnect_wait = 5  # reset backoff on successful connect

                    # Download prior-year seed bars once (skipped if already saved)
                    if seed_days > 0:
                        await download_spx_ohlc_seed(session, seed_days, force)
                        seed_days = 0  # don't repeat on reconnect

                    while remaining:
                        i, date_str = remaining[0]
                        print(f"\nDay {i+1:>3}/{len(all_trading_days)} | {date_str}")
                        try:
                            _, greeks_available = await asyncio.wait_for(
                                download_day(session, date_str, force, greeks_available),
                                timeout=DAY_TIMEOUT,
                            )
                            completed += 1
                            remaining.pop(0)
                        except asyncio.TimeoutError:
                            print(f"  [!] Day {date_str} timed out after {DAY_TIMEOUT}s — skipping")
                            remaining.pop(0)
                            raise ConnectionError("day timeout — reconnecting")
                        except ConnectionError:
                            # Bubble up to trigger reconnect
                            raise
                        except Exception as e:
                            print(f"  ERROR: {e}")
                            remaining.pop(0)  # skip this day, don't loop forever

        except (ConnectionError, OSError, EOFError, Exception) as exc:
            if not remaining:
                break
            print(f"\n  [!] Connection lost: {exc}")
            if reconnect < MAX_RECONNECTS - 1:
                print(f"  [!] Reconnecting in {reconnect_wait}s "
                      f"(attempt {reconnect+1}/{MAX_RECONNECTS}) ...")
                await asyncio.sleep(reconnect_wait)
                reconnect_wait = min(reconnect_wait * 2, 120)
            else:
                print(f"  [!] Max reconnect attempts reached — giving up.")
                break

    print(f"\n{'='*60}")
    print(f"  Done — {completed} days downloaded, {skipped} skipped (already saved)")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download ThetaData to Parquet cache")
    parser.add_argument("--force", action="store_true", help="Re-download all days")
    parser.add_argument("--days", type=int, default=None, help="Limit to N days (for testing)")
    parser.add_argument("--seed-days", type=int, default=5,
                        help="Prior-year SPX OHLC days to download for EMA seeding (default 5, 0 to skip)")
    parser.add_argument("--year", type=int, default=None,
                        help="Year to download (default: DOWNLOAD_YEAR in config)")
    parser.add_argument("--start-date", type=str, default=None,
                        help="First date to download, YYYYMMDD (for splitting within a year)")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Last date to download, YYYYMMDD (for splitting within a year)")
    parser.add_argument("--mcp-url", type=str, default=None,
                        help="MCP server URL (default: http://127.0.0.1:25503/mcp/sse)")
    args = parser.parse_args()
    asyncio.run(main(
        force=args.force,
        max_days=args.days,
        seed_days=args.seed_days,
        start_date=args.start_date,
        end_date=args.end_date,
        year=args.year,
        mcp_url=args.mcp_url,
    ))
