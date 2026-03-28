# ThetaData Infrastructure

## Mac Mini Setup
| Machine | IP | Year Downloading |
|---------|-----|-----------------|
| Mini 1 (local) | 192.168.0.132 | 2025 |
| Mini 2 | 192.168.0.253 | 2024 |
| Mini 3 | 192.168.0.123 | 2023 |

## Download Script
- Script: `download_data.py`
- Location: `/Users/jeerapongwongchote/Documents/thetadata`

## Networking Notes
- Mini 3 requires an **SSH reverse tunnel** to reach the MCP server on Mini 1
- MCP server runs on Mini 1 (192.168.0.132)

## METF Backtest Notes
- Full year backtest: 2025-03-24 to 2026-03-21 (260 trading days)
- Main script: `metf_v35_bidask.py`
- Key findings: Widen spreads to 50pt, CALL spreads outperform PUTs, avoid VIX 15-20 range
