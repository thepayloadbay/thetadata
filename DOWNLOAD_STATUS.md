# ThetaData Download — 3 Mac Mini Setup

## What's running
Downloading SPXW 0DTE options data (quotes, greeks, OI, SPX/VIX OHLC) using download_data.py across 3 Mac minis in parallel.

## Machine assignments
| Machine | IP | Year | Days | Log |
|---|---|---|---|---|
| Mini 1 (local) | 192.168.0.132 | 2025 | 252 | logs/mini1_2025.log |
| Mini 2 | 192.168.0.253 | 2024 | 253 | logs/mini2_2024.log |
| Mini 3 | 192.168.0.123 | 2023 | 251 | logs/mini3_2023.log |

## Check progress (run on Mini 1)
```bash
cd /Users/jeerapongwongchote/Documents/thetadata
grep "^Day" logs/mini1_2025.log | tail -1
grep "^Day" logs/mini2_2024.log | tail -1
grep "^Day" logs/mini3_2023.log | tail -1
```

## Restart commands (run on Mini 1 if any process dies)
```bash
PROJ="/Users/jeerapongwongchote/Documents/thetadata"
VENV="$PROJ/.venv/bin/python3"

# Mini 1 — 2025
$VENV -u download_data.py --year 2025 >> $PROJ/logs/mini1_2025.log 2>&1 &

# Mini 2 — 2024
ssh jeerapongwongchote@192.168.0.253 "cd $PROJ && $VENV -u download_data.py --year 2024 --mcp-url http://192.168.0.132:25503/mcp/sse" >> $PROJ/logs/mini2_2024.log 2>&1 &

# Mini 3 — 2023 (needs SSH reverse tunnel — asyncio can't connect to remote IPs directly)
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -R 25503:127.0.0.1:25503 jeerapongwongchote@192.168.0.123 "cd $PROJ && $VENV -u download_data.py --year 2023 --mcp-url http://127.0.0.1:25503/mcp/sse" >> $PROJ/logs/mini3_2023.log 2>&1 &
```

## Key facts
- ThetaData MCP server runs only on Mini 1 (port 25503)
- Mini 3 requires SSH reverse tunnel — its Python asyncio can't reach remote IPs
- Script auto-resumes — skips days already saved as valid Parquet files
- All machines share same .venv path: /Users/jeerapongwongchote/Documents/thetadata/.venv
- SSH username on all machines: jeerapongwongchote
