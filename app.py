"""
Simple RC car control server.

Reads CSV telemetry from the RP2350 over USB serial, logs it to file, and
exposes a web UI with a virtual joystick that sends pwm-a / pwm-b commands.

Features:
- Robust USB serial connection handling with error recovery
- Thread-safe operation with proper locking
- Graceful shutdown and resource cleanup
- Connection health monitoring
- Comprehensive error logging

Run:
    pip install -r requirements.txt
    python app.py
"""

import csv
import io
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports
from flask import Flask, Response, jsonify, render_template, request, send_file

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-8s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Shared state (protected by _lock) ─────────────────────────────────────────
_lock = threading.Lock()
_serial_conn: Optional[serial.Serial] = None
_log_file: Optional[io.TextIOWrapper] = None
_log_writer: Optional[csv.writer] = None
_telemetry: dict = {
    "timestamp_us": 0,
    "steer_us": 1500,
    "throttle_us": 1500,
    "speed_mps": 0.0,
    "connected": False,
    "last_update_us": 0,
    "parse_errors": 0,
    "serial_errors": 0,
}
_serial_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_connected_port: Optional[str] = None


# ── Background serial reader ───────────────────────────────────────────────────

def _is_connection_open() -> bool:
    """Thread-safe check if serial connection is open."""
    with _lock:
        return _serial_conn is not None and _serial_conn.is_open


def _parse_telemetry_line(line: str) -> Optional[dict]:
    """
    Parse telemetry line from RP2350.
    
    Supports two formats:
    1. VS Code Serial Plotter format: ">time_us:123,steer_ms:1500,throttle_ms:1500,speed_mps:0.5"
    2. Simple CSV format: "123,1500,1500,0.5"
    
    Returns dict with parsed values or None if parse fails.
    """
    line = line.strip()
    if not line:
        return None
    
    # Skip comments and responses
    if line.startswith("#") or line in ("OK", "ERR", "ERR: unknown/malformed command"):
        return None
    
    # Try parsing named format (VS Code Serial Plotter style)
    if ":" in line:
        try:
            # Strip leading '>' if present
            if line.startswith(">"):
                line = line[1:]
            
            values = {}
            for pair in line.split(","):
                if ":" not in pair:
                    return None
                key, val = pair.split(":", 1)
                key = key.strip()
                val = val.strip()
                
                # Map RP2350 field names to our format
                if "time" in key.lower():
                    values["timestamp_us"] = int(val)
                elif "steer" in key.lower():
                    values["steer_us"] = int(val)
                elif "throttle" in key.lower():
                    values["throttle_us"] = int(val)
                elif "speed" in key.lower():
                    values["speed_mps"] = float(val)
            
            # Require at least timestamp and one data field
            if "timestamp_us" in values and len(values) >= 2:
                return values
        except (ValueError, KeyError):
            return None
    
    # Try parsing simple CSV format (4 values)
    try:
        parts = line.split(",")
        if len(parts) == 4:
            ts = int(parts[0])
            st = int(parts[1])
            th = int(parts[2])
            spd = float(parts[3])
            return {
                "timestamp_us": ts,
                "steer_us": st,
                "throttle_us": th,
                "speed_mps": spd,
            }
    except ValueError:
        return None
    
    return None


def _serial_reader():
    """Read lines from serial, parse CSV telemetry, write to log."""
    logger.info("Serial reader thread started")
    consecutive_errors = 0
    max_consecutive_errors = 10
    
    while not _stop_event.is_set():
        # Check if connection is still available
        if not _is_connection_open():
            time.sleep(0.05)
            consecutive_errors = 0
            continue
        
        try:
            with _lock:
                conn = _serial_conn
            
            if conn is None or not conn.is_open:
                consecutive_errors = 0
                time.sleep(0.05)
                continue
            
            # Read with timeout to allow checking stop_event periodically
            raw = conn.readline()
            
        except (serial.SerialException, OSError) as e:
            consecutive_errors += 1
            with _lock:
                _telemetry["serial_errors"] = _telemetry.get("serial_errors", 0) + 1
            
            if consecutive_errors >= max_consecutive_errors:
                logger.error(f"Serial read failed {consecutive_errors} times: {e}")
                # Close the connection
                with _lock:
                    if _serial_conn:
                        try:
                            _serial_conn.close()
                        except Exception:
                            pass
                        _serial_conn = None
                    _telemetry["connected"] = False
                consecutive_errors = 0
            
            time.sleep(0.05)
            continue
        except Exception as e:
            logger.error(f"Unexpected error in serial reader: {e}", exc_info=True)
            time.sleep(0.05)
            continue
        
        consecutive_errors = 0
        
        if not raw:
            continue
        
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception as e:
            logger.warning(f"Failed to decode serial line: {e}")
            continue
        
        if not line:
            continue
        
        # Try to parse the line
        parsed = _parse_telemetry_line(line)
        if parsed is None:
            with _lock:
                _telemetry["parse_errors"] = _telemetry.get("parse_errors", 0) + 1
            logger.debug(f"Failed to parse telemetry: {line[:100]}")
            continue
        
        # Update telemetry and log
        try:
            with _lock:
                _telemetry.update(parsed)
                _telemetry["last_update_us"] = parsed.get("timestamp_us", 0)
                
                if _log_writer is not None and _log_file is not None:
                    row = [
                        parsed.get("timestamp_us", 0),
                        parsed.get("steer_us", 1500),
                        parsed.get("throttle_us", 1500),
                        f"{parsed.get('speed_mps', 0.0):.4f}"
                    ]
                    _log_writer.writerow(row)
                    try:
                        _log_file.flush()
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Failed to update telemetry: {e}", exc_info=True)




# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ports")
def list_ports():
    ports = [
        {"device": p.device, "description": p.description}
        for p in serial.tools.list_ports.comports()
    ]
    return jsonify(ports)


@app.route("/api/connect", methods=["POST"])
def connect():
    global _serial_conn, _log_file, _log_writer, _serial_thread, _stop_event, _connected_port
    data = request.get_json(force=True)
    port = data.get("port")
    baud = int(data.get("baud", 115200))
    if not port:
        return jsonify({"error": "port required"}), 400

    with _lock:
        if _serial_conn and _serial_conn.is_open:
            return jsonify({"error": "already connected"}), 400
        # Clean up any dead connection
        if _serial_conn:
            try:
                _serial_conn.close()
            except Exception:
                pass
            _serial_conn = None

    # Open serial
    try:
        logger.info(f"Attempting to connect to {port} at {baud} baud")
        conn = serial.Serial(port, baud, timeout=0.1)
        logger.info(f"Successfully opened serial port {port}")
    except (serial.SerialException, OSError) as e:
        logger.error(f"Failed to open serial port {port}: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error opening serial port: {e}", exc_info=True)
        return jsonify({"error": "Unexpected error"}), 500

    # Open log file
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"log_{timestamp}.csv"
        lf = open(log_path, "w", newline="")
        writer = csv.writer(lf)
        writer.writerow(["timestamp_us", "steer_us", "throttle_us", "speed_mps"])
        logger.info(f"Opened log file: {log_path}")
    except Exception as e:
        logger.error(f"Failed to open log file: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": f"Failed to open log file: {e}"}), 500

    _stop_event.clear()
    with _lock:
        _serial_conn = conn
        _log_file = lf
        _log_writer = writer
        _telemetry["connected"] = True
        _telemetry["parse_errors"] = 0
        _telemetry["serial_errors"] = 0
        _connected_port = port

    # Start background reader thread
    try:
        t = threading.Thread(target=_serial_reader, daemon=True, name="SerialReader")
        t.start()
        _serial_thread = t
        logger.info("Serial reader thread started")
    except Exception as e:
        logger.error(f"Failed to start serial reader thread: {e}")
        with _lock:
            try:
                if _serial_conn:
                    _serial_conn.close()
                if _log_file:
                    _log_file.close()
            except Exception:
                pass
            _serial_conn = None
            _log_file = None
            _log_writer = None
            _telemetry["connected"] = False
        return jsonify({"error": "Failed to start reader thread"}), 500

    return jsonify({"ok": True, "log": log_path.name})


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _serial_conn, _log_file, _log_writer, _connected_port
    logger.info("Disconnect requested")
    _stop_event.set()
    
    # Give the reader thread a brief moment to exit
    time.sleep(0.1)
    
    with _lock:
        if _serial_conn:
            try:
                _serial_conn.close()
                logger.info("Serial connection closed")
            except Exception as e:
                logger.warning(f"Error closing serial connection: {e}")
            _serial_conn = None
        
        if _log_file:
            try:
                _log_file.flush()
                _log_file.close()
                logger.info("Log file closed")
            except Exception as e:
                logger.warning(f"Error closing log file: {e}")
            _log_file = None
            _log_writer = None
        
        _telemetry["connected"] = False
        _connected_port = None
    
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
def send_command():
    data = request.get_json(force=True)
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"error": "command required"}), 400
    
    with _lock:
        conn = _serial_conn
        if conn is None or not conn.is_open:
            return jsonify({"error": "not connected"}), 400
    
    try:
        logger.debug(f"Sending command: {cmd}")
        conn.write((cmd + "\n").encode())
        return jsonify({"ok": True})
    except (serial.SerialException, OSError) as e:
        logger.error(f"Failed to send command: {e}")
        # Mark connection as broken
        with _lock:
            if _serial_conn:
                try:
                    _serial_conn.close()
                except Exception:
                    pass
                _serial_conn = None
            _telemetry["connected"] = False
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error sending command: {e}", exc_info=True)
        return jsonify({"error": "Unexpected error"}), 500



@app.route("/api/telemetry")
def telemetry():
    with _lock:
        return jsonify(dict(_telemetry))


@app.route("/api/logs")
def list_logs():
    files = sorted(LOG_DIR.glob("*.csv"), reverse=True)
    result = [
        {"name": f.name, "size": f.stat().st_size}
        for f in files
    ]
    return jsonify(result)


@app.route("/api/logs/<filename>")
def download_log(filename: str):
    # Validate filename to prevent path traversal
    safe = Path(filename).name
    if safe != filename or not safe.endswith(".csv"):
        return jsonify({"error": "invalid filename"}), 400
    path = LOG_DIR / safe
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe, mimetype="text/csv")


@app.errorhandler(500)
def handle_500_error(e):
    logger.error(f"Internal server error: {e}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(Exception)
def handle_uncaught_error(e):
    logger.error(f"Uncaught exception: {e}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


def _shutdown_handler(signum, frame):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    logger.info(f"Received signal {signum}, shutting down...")
    _stop_event.set()
    # Give threads a moment to exit
    time.sleep(0.5)
    
    with _lock:
        if _serial_conn:
            try:
                _serial_conn.close()
            except Exception:
                pass
        if _log_file:
            try:
                _log_file.close()
            except Exception:
                pass
    
    logger.info("Shutdown complete")
    sys.exit(0)


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    
    logger.info("=" * 70)
    logger.info("RC Car Control Server Starting")
    logger.info(f"Log directory: {LOG_DIR}")
    logger.info("=" * 70)
    
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        logger.error(f"Failed to start Flask app: {e}", exc_info=True)
        sys.exit(1)

