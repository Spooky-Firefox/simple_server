#!/usr/bin/env python3
"""
Camera-only controller bridge.

State machine:
1) SEARCH_GREEN: detect green circles and repeatedly request automatic mode.
2) AUTO_ANGLE: after controller ACK, send camera-derived angle updates.

Configuration is done with environment variables so serial address and commands are
fixed by deployment without editing code.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import serial

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    # Serial
    serial_address: str = os.getenv("SERIAL_ADDRESS", "/dev/ttyACM0")
    baud_rate: int = int(os.getenv("SERIAL_BAUD", "115200"))
    serial_timeout_s: float = float(os.getenv("SERIAL_TIMEOUT_S", "0.1"))

    # Camera
    camera_index: int = int(os.getenv("CAMERA_INDEX", "0"))
    camera_width: int = int(os.getenv("CAMERA_WIDTH", "640"))
    camera_height: int = int(os.getenv("CAMERA_HEIGHT", "480"))
    camera_fov_deg: float = float(os.getenv("CAMERA_FOV_DEG", "70.0"))

    # Protocol
    auto_request_cmd: str = os.getenv("AUTO_REQUEST_CMD", "mode automatic")
    auto_ack_token: str = os.getenv("AUTO_ACK_TOKEN", "automatic")
    angle_cmd_format: str = os.getenv("ANGLE_CMD_FORMAT", "camera-angle:{angle:.2f}")

    # Processing thresholds (ported style from usb_cam_opencv)
    classify_s_min: int = int(os.getenv("CLASSIFY_S_MIN_PREFILTER", "50"))
    classify_bg_sat_min: int = int(os.getenv("CLASSIFY_BG_SAT_MIN", "30"))
    classify_bg_circle_margin: int = int(os.getenv("CLASSIFY_BG_CIRCLE_MARGIN", "12"))
    classify_color_threshold: float = float(os.getenv("CLASSIFY_COLOR_THRESHOLD", "0.35"))
    decide_min_confidence: float = float(os.getenv("DECIDE_MIN_CONFIDENCE", "0.85"))

    # Rate controls
    auto_request_min_interval_s: float = float(os.getenv("AUTO_REQUEST_MIN_INTERVAL_S", "0.2"))
    angle_send_min_interval_s: float = float(os.getenv("ANGLE_SEND_MIN_INTERVAL_S", "0.05"))


@dataclass
class CircleDetection:
    x: int
    y: int
    radius: int
    color: str
    confidence: float


class CircleClassifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def detect(self, frame: np.ndarray) -> list[CircleDetection]:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        sat_mask = cv2.inRange(hsv, (0, self.cfg.classify_s_min, 0), (180, 255, 255))
        red_low_mask = cv2.inRange(hsv, (0, 80, 30), (10, 255, 255))
        red_high_mask = cv2.inRange(hsv, (170, 80, 30), (180, 255, 255))
        green_mask = cv2.inRange(hsv, (35, 60, 30), (85, 255, 255))

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_contrast = self.clahe.apply(gray)

        circles = cv2.HoughCircles(
            gray_contrast,
            cv2.HOUGH_GRADIENT,
            dp=1.0,
            minDist=40,
            param1=120,
            param2=28,
            minRadius=8,
            maxRadius=250,
        )
        if circles is None:
            return []

        detections: list[CircleDetection] = []
        h, w = frame.shape[:2]

        for c in np.round(circles[0, :]).astype(int):
            cx, cy, radius = int(c[0]), int(c[1]), int(c[2])
            if radius <= 0:
                continue

            circle_mask = np.zeros((h, w), dtype=np.uint8)
            cv2.circle(circle_mask, (cx, cy), radius, 255, -1)

            sat_count = cv2.countNonZero(cv2.bitwise_and(sat_mask, circle_mask))
            if sat_count < 10:
                detections.append(CircleDetection(cx, cy, radius, "unknown", 0.0))
                continue

            red_count = cv2.countNonZero(cv2.bitwise_and(red_low_mask, circle_mask))
            red_count += cv2.countNonZero(cv2.bitwise_and(red_high_mask, circle_mask))
            green_count = cv2.countNonZero(cv2.bitwise_and(green_mask, circle_mask))

            red_frac = float(red_count) / float(sat_count)
            green_frac = float(green_count) / float(sat_count)

            x0 = max(cx - radius, 0)
            y0 = max(cy - radius, 0)
            x1 = min(cx + radius, w - 1)
            y1 = min(cy + radius, h - 1)

            bg_mask = np.zeros((h, w), dtype=np.uint8)
            cv2.rectangle(bg_mask, (x0, y0), (x1, y1), 255, -1)

            exclude_mask = np.zeros((h, w), dtype=np.uint8)
            cv2.circle(exclude_mask, (cx, cy), radius + self.cfg.classify_bg_circle_margin, 255, -1)

            bg_stencil = cv2.bitwise_and(bg_mask, cv2.bitwise_not(exclude_mask))
            bg_sat = cv2.countNonZero(cv2.bitwise_and(sat_mask, bg_stencil))

            if bg_sat < self.cfg.classify_bg_sat_min:
                bg_red_frac, bg_green_frac = 0.0, 0.0
            else:
                bg_red = cv2.countNonZero(cv2.bitwise_and(red_low_mask, bg_stencil))
                bg_red += cv2.countNonZero(cv2.bitwise_and(red_high_mask, bg_stencil))
                bg_green = cv2.countNonZero(cv2.bitwise_and(green_mask, bg_stencil))
                bg_red_frac = float(bg_red) / float(bg_sat)
                bg_green_frac = float(bg_green) / float(bg_sat)

            adj_red = red_frac * (1.0 - bg_red_frac)
            adj_green = green_frac * (1.0 - bg_green_frac)

            if adj_red >= adj_green and adj_red >= self.cfg.classify_color_threshold:
                color = "red"
                confidence = adj_red
            elif adj_green > adj_red and adj_green >= self.cfg.classify_color_threshold:
                color = "green"
                confidence = adj_green
            else:
                color = "unknown"
                confidence = max(adj_red, adj_green)

            detections.append(CircleDetection(cx, cy, radius, color, confidence))

        return detections


class ControllerBridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_event = threading.Event()
        self._auto_confirmed = threading.Event()
        self._serial: Optional[serial.Serial] = None
        self._serial_lock = threading.Lock()
        self._serial_reader_thread: Optional[threading.Thread] = None
        self._last_auto_request_t = 0.0
        self._last_angle_send_t = 0.0

    def run(self) -> int:
        try:
            self._open_serial()
            self._start_serial_reader()
            return self._run_camera_loop()
        finally:
            self._shutdown()

    def _open_serial(self) -> None:
        logger.info("Opening serial: address=%s baud=%d", self.cfg.serial_address, self.cfg.baud_rate)
        self._serial = serial.Serial(
            self.cfg.serial_address,
            self.cfg.baud_rate,
            timeout=self.cfg.serial_timeout_s,
        )
        logger.info("Serial port opened")

    def _start_serial_reader(self) -> None:
        t = threading.Thread(target=self._serial_reader, daemon=True, name="SerialReader")
        t.start()
        self._serial_reader_thread = t

    def _serial_reader(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self._serial_lock:
                    conn = self._serial
                if conn is None or not conn.is_open:
                    time.sleep(0.05)
                    continue

                raw = conn.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                logger.info("CTRL< %s", line)

                if self.cfg.auto_ack_token.lower() in line.lower():
                    if not self._auto_confirmed.is_set():
                        logger.info("Automatic mode confirmed by controller")
                    self._auto_confirmed.set()

            except Exception as exc:
                logger.warning("Serial reader error: %s", exc)
                time.sleep(0.05)

    def _send_serial(self, command: str) -> bool:
        with self._serial_lock:
            conn = self._serial
            if conn is None or not conn.is_open:
                logger.error("Cannot send, serial is not open")
                return False
            try:
                conn.write((command + "\n").encode("utf-8"))
                conn.flush()
                logger.info("CTRL> %s", command)
                return True
            except Exception as exc:
                logger.error("Failed to send serial command '%s': %s", command, exc)
                return False

    def _pick_green_target(self, detections: list[CircleDetection]) -> Optional[CircleDetection]:
        greens = [
            d
            for d in detections
            if d.color == "green" and d.confidence >= self.cfg.decide_min_confidence
        ]
        if not greens:
            return None
        return max(greens, key=lambda d: d.confidence)

    def _pixel_to_angle_deg(self, x: int, frame_width: int) -> float:
        center = frame_width / 2.0
        x_norm = (x - center) / center if center > 0 else 0.0
        return float(x_norm * (self.cfg.camera_fov_deg / 2.0))

    def _run_camera_loop(self) -> int:
        classifier = CircleClassifier(self.cfg)

        cap = cv2.VideoCapture(self.cfg.camera_index)
        if not cap.isOpened():
            logger.error("Failed to open camera index %d", self.cfg.camera_index)
            return 1

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)

        logger.info("Camera opened: index=%d, target=%dx%d", self.cfg.camera_index, self.cfg.camera_width, self.cfg.camera_height)
        logger.info("Mode: SEARCH_GREEN")

        while not self.stop_event.is_set():
            ok, frame = cap.read()
            if not ok:
                logger.warning("Camera frame read failed")
                time.sleep(0.02)
                continue

            detections = classifier.detect(frame)
            target = self._pick_green_target(detections)
            now = time.time()

            if not self._auto_confirmed.is_set():
                if target is not None and (now - self._last_auto_request_t) >= self.cfg.auto_request_min_interval_s:
                    self._send_serial(self.cfg.auto_request_cmd)
                    self._last_auto_request_t = now
                continue

            if target is None:
                continue

            if (now - self._last_angle_send_t) < self.cfg.angle_send_min_interval_s:
                continue

            angle = self._pixel_to_angle_deg(target.x, frame.shape[1])
            cmd = self.cfg.angle_cmd_format.format(angle=angle)
            if self._send_serial(cmd):
                self._last_angle_send_t = now

        cap.release()
        return 0

    def _shutdown(self) -> None:
        self.stop_event.set()
        with self._serial_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None


def main() -> int:
    cfg = Config()
    bridge = ControllerBridge(cfg)

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %s, stopping", signum)
        bridge.stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 70)
    logger.info("Camera Serial Bridge starting")
    logger.info("Serial address: %s", cfg.serial_address)
    logger.info("Auto request cmd: %s", cfg.auto_request_cmd)
    logger.info("Auto ACK token: %s", cfg.auto_ack_token)
    logger.info("Angle command format: %s", cfg.angle_cmd_format)
    logger.info("=" * 70)

    try:
        return bridge.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
