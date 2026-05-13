#!/bin/env bash

# Robust startup with auto-restart for the camera serial bridge.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
APP_LOG="${LOG_DIR}/camera_bridge_robust_${TIMESTAMP}.log"

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

echo "[$(date)] Starting robust camera bridge (auto-restart)..."
echo "[$(date)] Log file: ${APP_LOG}"

RESTART_DELAY=3
RESTART_COUNT=0
MAX_CONSECUTIVE_FAILURES=20

while true; do
    set +e
    $PYTHON app.py >> "$APP_LOG" 2>&1
    EXIT_CODE=$?
    set -e

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date)] Bridge exited cleanly"
        exit 0
    fi

    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ $RESTART_COUNT -gt $MAX_CONSECUTIVE_FAILURES ]; then
        echo "[$(date)] Too many consecutive failures (${RESTART_COUNT}), giving up"
        exit 1
    fi

    echo "[$(date)] Bridge crashed (exit ${EXIT_CODE}), restart #${RESTART_COUNT} in ${RESTART_DELAY}s"
    sleep $RESTART_DELAY
done
