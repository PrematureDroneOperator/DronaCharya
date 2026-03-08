"""
Standalone YOLO detector service for Python 3.12 runtime.

Protocol:
- newline-delimited JSON over TCP
- request ops: PING, SESSION_START, SESSION_END, INFER
"""

import argparse
import base64
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import AppConfig, load_config
from vision.frame_yolo_detector import FrameYoloDetector


class DetectorService:
    def __init__(self, config: AppConfig, logger: logging.Logger, host: Optional[str], port: Optional[int]) -> None:
        self.config = config
        self.logger = logger
        self.host = str(host if host is not None else config.detector_service.host)
        self.port = int(port if port is not None else config.detector_service.port)

        self.detector = FrameYoloDetector(config, logger)
        # Warm-load model once at service start so per-frame latency stays low.
        self.detector._load_model()  # type: ignore[attr-defined]

    def serve_forever(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(1)
        self.logger.info("Detector service listening on %s:%s", self.host, self.port)

        try:
            while True:
                conn, addr = sock.accept()
                self.logger.info("Detector client connected: %s:%s", addr[0], addr[1])
                try:
                    self._handle_client(conn, addr)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    self.logger.info("Detector client disconnected: %s:%s", addr[0], addr[1])
        except KeyboardInterrupt:
            self.logger.info("Detector service interrupted.")
            return 0
        finally:
            sock.close()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        _ = addr
        buffer = b""
        conn.settimeout(30.0)
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk
            while True:
                nl = buffer.find(b"\n")
                if nl < 0:
                    break
                raw_line = buffer[:nl]
                buffer = buffer[nl + 1 :]
                if not raw_line:
                    continue
                response = self._dispatch_line(raw_line)
                payload = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
                conn.sendall(payload)

    def _dispatch_line(self, raw_line: bytes) -> Dict[str, Any]:
        try:
            request = json.loads(raw_line.decode("utf-8", errors="strict"))
            if not isinstance(request, dict):
                raise ValueError("Request payload must be object")
        except ValueError as exc:
            return {"ok": False, "error": "invalid_json: {0}".format(exc)}

        op = str(request.get("op", "")).upper()
        if op == "PING":
            return {
                "ok": True,
                "op": "PING",
                "status": "ready",
                "host": self.host,
                "port": self.port,
                "model_path": str(self.config.vision.model_path),
            }
        if op == "SESSION_START":
            session_id = str(request.get("session_id", ""))
            self.logger.info("Detector session start: %s", session_id)
            return {"ok": True, "op": "SESSION_START", "session_id": session_id}
        if op == "SESSION_END":
            session_id = str(request.get("session_id", ""))
            self.logger.info("Detector session end: %s", session_id)
            return {"ok": True, "op": "SESSION_END", "session_id": session_id}
        if op == "INFER":
            return self._infer(request)
        return {"ok": False, "error": "unsupported_op: {0}".format(op)}

    def _infer(self, request: Dict[str, Any]) -> Dict[str, Any]:
        frame_idx = int(request.get("frame_idx", 0))
        self.logger.info("==================================================")
        self.logger.info("Frame %d received. Running YOLO inference...", frame_idx)

        image_b64 = request.get("image_jpeg_b64", "")
        if not isinstance(image_b64, str) or not image_b64:
            self.logger.error("Frame %d missing image_jpeg_b64", frame_idx)
            return {"ok": False, "error": "missing image_jpeg_b64"}
            
        try:
            image_bytes = base64.b64decode(image_b64.encode("ascii"), validate=True)
            image_np = np.frombuffer(image_bytes, dtype=np.uint8)
            frame = cv2.imdecode(image_np, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("imdecode returned None")
                
            detections = self.detector.detect_frame(frame)
            
            self.logger.info("Frame %d result: %d targets detected.", frame_idx, len(detections))
            for i, d in enumerate(detections):
                self.logger.info("   Target %d -> Class '%s' | Confidence: %.2f | Center Pixel: (%.1f, %.1f)", 
                                 i+1, d.get("class_name"), d.get("confidence"), d.get("pixel_x"), d.get("pixel_y"))
                                 
        except Exception as exc:
            self.logger.error("Frame %d inference failed: %s", frame_idx, exc)
            return {"ok": False, "error": "infer_failed: {0}".format(exc)}

        return {
            "ok": True,
            "op": "INFER",
            "frame_idx": int(request.get("frame_idx", 0)),
            "frame_ts": str(request.get("frame_ts", "")),
            "detections": detections,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone YOLO detector service")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to YAML config")
    parser.add_argument("--host", type=str, default="", help="Override detector_service.host")
    parser.add_argument("--port", type=int, default=0, help="Override detector_service.port")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("detector_service")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    config = load_config(config_path=config_path, base_dir=PROJECT_ROOT)

    host = str(args.host).strip() if str(args.host).strip() else None
    port = int(args.port) if int(args.port) > 0 else None
    service = DetectorService(config=config, logger=logger, host=host, port=port)
    return service.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
