#!/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
APP_LOG="${LOG_DIR}/camera_bridge_${TIMESTAMP}.log"

cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "[$(date)] Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

PYTHON="${VENV_DIR}/bin/python3"
PIP="${VENV_DIR}/bin/pip"

echo "[$(date)] Installing/checking dependencies..."
$PIP install -q -r requirements.txt

echo "[$(date)] Starting camera serial bridge..."
echo "[$(date)] Log file: ${APP_LOG}"

# Example override:
# export SERIAL_ADDRESS=/dev/ttyUSB0
$PYTHON app.py >> "$APP_LOG" 2>&1
