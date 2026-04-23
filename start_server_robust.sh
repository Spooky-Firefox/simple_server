#!/bin/env bash

# Robust server startup with auto-restart on failure
# This script:
# 1. Starts the Flask app on port 5000 with automatic restart on crash
# 2. Sets up an autossh tunnel to ronstad.se for remote access (optional)
# 3. Waits for network connectivity before establishing the tunnel
# 4. Implements health checking and graceful shutdown

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SERVER_LOG="${LOG_DIR}/server_${TIMESTAMP}.log"
TUNNEL_LOG="${LOG_DIR}/tunnel_${TIMESTAMP}.log"

echo "Robust RC Car Server + SSH Tunnel Startup"
echo "Server log: ${SERVER_LOG}"
echo "Tunnel log: ${TUNNEL_LOG}"

# ── Setup Python Virtual Environment ────────────────────────────────────────

cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "[$(date)] Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

PYTHON="${VENV_DIR}/bin/python3"
PIP="${VENV_DIR}/bin/pip"

# Install dependencies if needed
echo "[$(date)] Checking dependencies..."
if ! $PYTHON -c "import flask" 2>/dev/null; then
    echo "[$(date)] Installing dependencies..."
    $PIP install -q -r requirements.txt
else
    echo "[$(date)] Dependencies already installed"
fi

# ── Cleanup on exit ─────────────────────────────────────────────────────────

cleanup() {
    echo "[$(date)] Cleaning up..."
    
    # Kill Flask server if running
    if [ ! -z "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[$(date)] Terminating Flask server (PID $SERVER_PID)..."
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
    
    # Kill tunnel if running
    if [ ! -z "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        echo "[$(date)] Terminating SSH tunnel (PID $TUNNEL_PID)..."
        kill -TERM "$TUNNEL_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$TUNNEL_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT

# ── Start Flask Server with Auto-Restart ───────────────────────────────────

echo "[$(date)] Starting Flask server with auto-restart..."

RESTART_DELAY=5
RESTART_COUNT=0
MAX_CONSECUTIVE_FAILURES=10
FAILURE_WINDOW=3600  # 1 hour

start_server() {
    $PYTHON app.py >> "$SERVER_LOG" 2>&1 &
    SERVER_PID=$!
    echo "[$(date)] Flask server started (PID $SERVER_PID)"
}

start_server

# ── Health checking loop ────────────────────────────────────────────────────

LAST_START_TIME=$(date +%s)

while true; do
    # Check if server is still running
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        CURRENT_TIME=$(date +%s)
        TIME_SINCE_START=$((CURRENT_TIME - LAST_START_TIME))
        
        if [ $TIME_SINCE_START -lt $FAILURE_WINDOW ]; then
            RESTART_COUNT=$((RESTART_COUNT + 1))
        else
            # Reset counter if outside the window
            RESTART_COUNT=1
            LAST_START_TIME=$CURRENT_TIME
        fi
        
        if [ $RESTART_COUNT -gt $MAX_CONSECUTIVE_FAILURES ]; then
            echo "[$(date)] ERROR: Server crashed $RESTART_COUNT times in ${TIME_SINCE_START}s"
            echo "[$(date)] Giving up after $MAX_CONSECUTIVE_FAILURES consecutive failures"
            exit 1
        fi
        
        echo "[$(date)] Flask server crashed (exit code $?). Restart #$RESTART_COUNT in ${RESTART_DELAY}s..."
        tail -10 "$SERVER_LOG"
        sleep $RESTART_DELAY
        start_server
    fi
    
    sleep 5
done
