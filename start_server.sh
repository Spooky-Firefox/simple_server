#!/bin/env bash

# Simple server startup with autossh tunnel to ronstad.se
# This script:
# 1. Starts the Flask app on port 5000
# 2. Sets up an autossh tunnel to ronstad.se for remote access
# 3. Waits for network connectivity before establishing the tunnel

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SERVER_LOG="${LOG_DIR}/server_${TIMESTAMP}.log"
TUNNEL_LOG="${LOG_DIR}/tunnel_${TIMESTAMP}.log"

echo "Simple Server + SSH Tunnel Startup"
echo "Server log: ${SERVER_LOG}"
echo "Tunnel log: ${TUNNEL_LOG}"

# ── Start Flask Server ──────────────────────────────────────────────────────

echo "[$(date)] Starting Flask server..."
cd "$SCRIPT_DIR"

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -q -r requirements.txt
fi

# Start server in background
python3 app.py >> "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "[$(date)] Flask server started (PID: $SERVER_PID)"

# Wait for server to be ready
echo "[$(date)] Waiting for Flask server to be ready..."
until curl -s http://localhost:5000/ >/dev/null 2>&1; do
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "[$(date)] ERROR: Flask server crashed!"
        cat "$SERVER_LOG"
        exit 1
    fi
    echo "[$(date)] Server not ready yet, waiting..."
    sleep 2
done
echo "[$(date)] Flask server is ready!"

# ── Wait for network and DNS ────────────────────────────────────────────────

echo "[$(date)] Waiting for DNS to be ready..."
until host ronstad.se >/dev/null 2>&1 || getent hosts ronstad.se >/dev/null 2>&1; do
    echo "[$(date)] DNS not ready yet"
    sleep 5
done
echo "[$(date)] DNS is ready"

# Wait for default route
until ip route show default | grep -q .; do
    echo "[$(date)] Waiting for default route"
    sleep 3
done
echo "[$(date)] Network route ready"

# ── Setup SSH Tunnel ────────────────────────────────────────────────────────

echo "[$(date)] Setting up autossh tunnel to ronstad.se..."

while true; do
    if ping -c 1 -w 5 "ronstad.se" >/dev/null 2>&1; then
        if ! pgrep -af "autossh.*5000" >/dev/null 2>&1; then
            echo "[$(date)] Starting autossh tunnel (will retry in 20s if needed)"
            sleep 20
            
            # Wait for SSH to be ready
            until ssh -o BatchMode=yes -o ConnectTimeout=5 olle@ronstad.se "exit" 2>/dev/null; do
                echo "[$(date)] Waiting for SSH connectivity to ronstad.se"
                sleep 5
            done

            # Start autossh tunnel: forward ronstad.se:5000 to localhost:5000
            autossh -M 0 -fN \
                -o ServerAliveInterval=30 \
                -o ServerAliveCountMax=3 \
                -o ExitOnForwardFailure=yes \
                -R 127.0.0.1:5000:127.0.0.1:5000 olle@ronstad.se >> "$TUNNEL_LOG" 2>&1
            
            echo "[$(date)] Autossh tunnel established"
        fi
    else
        echo "[$(date)] Cannot reach ronstad.se, will retry"
    fi
    
    sleep 5
done &
TUNNEL_PID=$!
echo "[$(date)] Tunnel monitor started (PID: $TUNNEL_PID)"

# ── Cleanup on exit ─────────────────────────────────────────────────────────

trap "echo 'Shutting down...'; kill $SERVER_PID $TUNNEL_PID 2>/dev/null; exit 0" SIGINT SIGTERM

echo "[$(date)] Services running. Press Ctrl+C to stop."
wait
