"""
Simple RC car control server.

Reads CSV telemetry from the RP2350 over USB serial, logs it to file, and
exposes a web UI with a virtual joystick that sends pwm-a / pwm-b commands.

Run:
    pip install -r requirements.txt
    python app.py
"""

import csv
import io
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import serial
import serial.tools.list_ports
from flask import Flask, Response, jsonify, render_template, request, send_file

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Shared state (protected by _lock) ─────────────────────────────────────────
_lock = threading.Lock()
_serial_conn: serial.Serial | None = None
_log_file: io.TextIOWrapper | None = None
_log_writer: csv.writer | None = None
_telemetry: dict = {
    "timestamp_us": 0,
    "steer_us": 1500,
    "throttle_us": 1500,
    "speed_mps": 0.0,
    "connected": False,
}
_serial_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ── Background serial reader ───────────────────────────────────────────────────

def _serial_reader():
    """Read lines from serial, parse CSV telemetry, write to log."""
    while not _stop_event.is_set():
        with _lock:
            conn = _serial_conn
        if conn is None or not conn.is_open:
            time.sleep(0.05)
            continue
        try:
            raw = conn.readline()
        except Exception:
            time.sleep(0.05)
            continue
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        # Skip header / comment lines
        if line.startswith("#"):
            continue
        # Skip ACK lines from commands
        if line in ("OK", "ERR"):
            continue
        parts = line.split(",")
        if len(parts) != 4:
            continue
        try:
            ts  = int(parts[0])
            st  = int(parts[1])
            th  = int(parts[2])
            spd = float(parts[3])
        except ValueError:
            continue
        row = {"timestamp_us": ts, "steer_us": st, "throttle_us": th, "speed_mps": spd}
        with _lock:
            _telemetry.update(row)
            if _log_writer is not None:
                _log_writer.writerow([ts, st, th, f"{spd:.4f}"])
                if _log_file:
                    _log_file.flush()


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
    global _serial_conn, _log_file, _log_writer, _serial_thread, _stop_event
    data = request.get_json(force=True)
    port = data.get("port")
    baud = int(data.get("baud", 115200))
    if not port:
        return jsonify({"error": "port required"}), 400

    with _lock:
        if _serial_conn and _serial_conn.is_open:
            return jsonify({"error": "already connected"}), 400

    # Open serial
    try:
        conn = serial.Serial(port, baud, timeout=0.1)
    except serial.SerialException as e:
        return jsonify({"error": str(e)}), 500

    # Open log file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"log_{timestamp}.csv"
    lf = open(log_path, "w", newline="")
    writer = csv.writer(lf)
    writer.writerow(["timestamp_us", "steer_us", "throttle_us", "speed_mps"])

    _stop_event.clear()
    with _lock:
        _serial_conn = conn
        _log_file = lf
        _log_writer = writer
        _telemetry["connected"] = True

    # Start background reader thread
    t = threading.Thread(target=_serial_reader, daemon=True)
    t.start()
    _serial_thread = t

    return jsonify({"ok": True, "log": log_path.name})


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    global _serial_conn, _log_file, _log_writer
    _stop_event.set()
    with _lock:
        if _serial_conn:
            try:
                _serial_conn.close()
            except Exception:
                pass
            _serial_conn = None
        if _log_file:
            try:
                _log_file.flush()
                _log_file.close()
            except Exception:
                pass
            _log_file = None
            _log_writer = None
        _telemetry["connected"] = False
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
        conn.write((cmd + "\n").encode())
    except serial.SerialException as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


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


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting RC car control server on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
