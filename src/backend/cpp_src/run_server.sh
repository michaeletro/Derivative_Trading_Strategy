#!/usr/bin/env bash
set -euo pipefail

# Resolve project root relative to this script
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default environment values (override by exporting before running)
export DB_PATH="${DB_PATH:-$ROOT_DIR/../data_files/quant_data.db}"
export DB_CREATE_IF_MISSING="${DB_CREATE_IF_MISSING:-true}"
export DB_RESTORE_FROM_CSV="${DB_RESTORE_FROM_CSV:-true}"
export DB_FAIL_FAST="${DB_FAIL_FAST:-true}"
export ASSET_CSV="${ASSET_CSV:-$ROOT_DIR/../data_files/backup_data.csv}"
export STOCK_CSV="${STOCK_CSV:-$ROOT_DIR/../data_files/stock_backup.csv}"
export OPTION_CSV="${OPTION_CSV:-$ROOT_DIR/../data_files/option_backup.csv}"

# IB WebSocket defaults
export ENABLE_IB_WS="${ENABLE_IB_WS:-true}"
export IB_WS_URL="${IB_WS_URL:-wss://localhost:5000/v1/portal/ibwss}"

# Run server
exec "$ROOT_DIR/bin/server"
