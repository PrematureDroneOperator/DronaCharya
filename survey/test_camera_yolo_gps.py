"""
Run a field-style survey stack test:
- capture camera frames,
- run frame YOLO detection,
- tag detections with Pixhawk GPS,
- write outputs into test/session-XXXX.

Outputs per session:
- recording.avi/.mp4        (raw camera recording from DroneRecorder)
- recording_detected.avi    (annotated YOLO + GPS overlay video)
- detections.csv
- detections.json
- metadata.json
"""

import argparse
import csv
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2

# Allow direct execution: `python survey/test_camera_yolo_gps.py`
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from navigation.mavlink_controller import MavlinkController
from utils.config import load_config
from vision.frame_yolo_detector import FrameYoloDetector
from vision.recorder import DroneRecorder


DETECTION_COLUMNS = [
    "frame_idx",
    "timestamp_utc",
    "class_name",
    "confidence",
    "pixel_x",
    "pixel_y",
    "bbox_x1",
    "bbox_y1",
    "bbox_x2",
    "bbox_y2",
    "gps_fix_type",
    "latitude",
    "longitude",
    "altitude_m",
    "gps_timestamp_utc",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GPSMonitor:
    def __init__(
        self,
        connection_string: str,
        baudrate: int,
        min_fix_type: int,
        logger: logging.Logger,
    ) -> None:
        self.connection_string = connection_string
        self.baudrate = int(baudrate)
        self.min_fix_type = int(min_fix_type)
        self.logger = logger

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._controller: Optional[MavlinkController] = None
        self._latest: Optional[Dict[str, Any]] = None
        self._connected = False
        self._updates = 0
        self._errors = 0
        self._udp_hint_logged = False

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="TestGPSMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        controller = self._controller
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
        with self._lock:
            self._controller = None
            self._connected = False

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "gps_updates": int(self._updates),
                "gps_errors": int(self._errors),
                "gps_connected": int(self._connected),
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            controller = self._controller
            if controller is None:
                controller = MavlinkController(
                    connection_string=self.connection_string,
                    baudrate=self.baudrate,
                    logger=self.logger,
                )
                try:
                    controller.connect(timeout_sec=10)
                except Exception as exc:
                    with self._lock:
                        self._errors += 1
                        self._connected = False
                    self.logger.warning("GPS monitor connect failed: %s", exc)
                    if (not self._udp_hint_logged) and str(self.connection_string).startswith("udp:127.0.0.1:"):
                        self._udp_hint_logged = True
                        self.logger.warning(
                            "MAVLink source is localhost UDP. Ensure a forwarder is publishing heartbeat to %s, "
                            "or use direct serial (e.g. /dev/ttyTHS1).",
                            self.connection_string,
                        )
                    try:
                        controller.close()
                    except Exception:
                        pass
                    time.sleep(1.5)
                    continue
                with self._lock:
                    self._controller = controller
                    self._connected = True

            try:
                msg = controller.recv_match("GPS_RAW_INT", timeout=1.0)
            except Exception as exc:
                with self._lock:
                    self._errors += 1
                    self._connected = False
                    self._controller = None
                self.logger.warning("GPS monitor read failed: %s", exc)
                try:
                    controller.close()
                except Exception:
                    pass
                time.sleep(1.0)
                continue

            if msg is None:
                continue
            fix_type = int(getattr(msg, "fix_type", 0))
            if fix_type < self.min_fix_type:
                continue

            payload = {
                "fix_type": fix_type,
                "latitude": float(getattr(msg, "lat", 0.0)) / 1e7,
                "longitude": float(getattr(msg, "lon", 0.0)) / 1e7,
                "altitude_m": float(getattr(msg, "alt", 0.0)) / 1000.0,
                "timestamp_utc": _utc_now_iso(),
            }
            with self._lock:
                self._latest = payload
                self._updates += 1
                self._connected = True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Camera + YOLO + Pixhawk GPS test recorder.")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to YAML config.")
    parser.add_argument("--output-root", type=str, default="test", help="Output root (default: test).")
    parser.add_argument("--source", type=str, default="", help="Camera source override (device index/url/pipeline).")
    parser.add_argument("--fps", type=int, default=30, help="Recording FPS (default: 30).")
    parser.add_argument("--duration-sec", type=float, default=0.0, help="Stop after N seconds (0 = until stop).")
    parser.add_argument("--max-frames", type=int, default=0, help="Stop after N frames (0 = unlimited).")
    parser.add_argument(
        "--inference-every-n",
        type=int,
        default=0,
        help="Run detection every Nth frame (0 = use config.survey.inference_every_n).",
    )
    parser.add_argument(
        "--min-gps-fix-type",
        type=int,
        default=0,
        help="Minimum GPS fix type to accept (0 = use config.survey.min_gps_fix_type).",
    )
    parser.add_argument("--preview", action="store_true", help="Show live annotated preview window.")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level.")
    parser.add_argument(
        "--mavlink-connection",
        type=str,
        default="",
        help="Override MAVLink connection string (default: mission.mavlink_connection from config).",
    )
    parser.add_argument(
        "--mavlink-baudrate",
        type=int,
        default=0,
        help="Override MAVLink baudrate (default: mission.mavlink_baudrate from config).",
    )
    parser.add_argument(
        "--mavlink-test-only",
        action="store_true",
        help="Test heartbeat/GPS and exit (no camera recording).",
    )
    parser.add_argument(
        "--mavlink-timeout-sec",
        type=float,
        default=15.0,
        help="Heartbeat wait timeout for --mavlink-test-only.",
    )
    parser.add_argument(
        "--mavlink-gps-samples",
        type=int,
        default=3,
        help="Number of GPS samples to print in --mavlink-test-only mode.",
    )
    return parser


def _open_video_writer(path: Path, fourcc: str, fps: float, width: int, height: int) -> cv2.VideoWriter:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), fps, (width, height))
    if writer.isOpened():
        return writer
    writer.release()

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
    if writer.isOpened():
        return writer
    writer.release()
    raise RuntimeError("Could not create detected video writer at {0}".format(path))


def _draw_overlay(
    frame,
    frame_idx: int,
    detections_count: int,
    gps_payload: Optional[Dict[str, Any]],
    gps_connected: bool,
) -> None:
    lines = [
        "frame={0} detections={1}".format(frame_idx, detections_count),
        "gps_link={0}".format("UP" if gps_connected else "DOWN"),
    ]
    if gps_payload:
        lines.append(
            "fix={0} lat={1:.7f} lon={2:.7f} alt={3:.1f}m".format(
                int(gps_payload.get("fix_type", 0)),
                float(gps_payload.get("latitude", 0.0)),
                float(gps_payload.get("longitude", 0.0)),
                float(gps_payload.get("altitude_m", 0.0)),
            )
        )
    else:
        lines.append("fix=<waiting>")

    y0 = 24
    for idx, line in enumerate(lines):
        y = y0 + idx * 24
        cv2.putText(frame, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (20, 20, 20), 3, cv2.LINE_AA)
        cv2.putText(frame, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (250, 250, 250), 1, cv2.LINE_AA)


def _run_mavlink_probe(
    connection_string: str,
    baudrate: int,
    min_fix_type: int,
    timeout_sec: float,
    gps_samples: int,
    logger: logging.Logger,
) -> int:
    logger.info("MAVLink probe: connection=%s baud=%s min_fix_type=%s", connection_string, baudrate, min_fix_type)
    controller = MavlinkController(connection_string=connection_string, baudrate=int(baudrate), logger=logger)
    heartbeat_timeout = max(1, int(round(float(timeout_sec))))
    try:
        controller.connect(timeout_sec=heartbeat_timeout)
    except Exception as exc:
        logger.error("MAVLink heartbeat test failed: %s", exc)
        if str(connection_string).startswith("udp:127.0.0.1:"):
            logger.error(
                "No local UDP heartbeat source on %s. Start a MAVLink forwarder or switch to serial /dev/tty*.",
                connection_string,
            )
        return 2

    logger.info("Heartbeat OK.")

    collected = 0
    deadline = time.time() + max(5.0, float(timeout_sec))
    try:
        while collected < max(1, int(gps_samples)) and time.time() < deadline:
            msg = controller.recv_match("GPS_RAW_INT", timeout=1.0)
            if msg is None:
                continue
            fix_type = int(getattr(msg, "fix_type", 0))
            lat = float(getattr(msg, "lat", 0.0)) / 1e7
            lon = float(getattr(msg, "lon", 0.0)) / 1e7
            alt_m = float(getattr(msg, "alt", 0.0)) / 1000.0
            logger.info("GPS sample %s: fix=%s lat=%.7f lon=%.7f alt=%.2fm", collected + 1, fix_type, lat, lon, alt_m)
            if fix_type >= int(min_fix_type):
                collected += 1

        if collected == 0:
            logger.warning("No GPS samples with fix_type >= %s received before timeout.", int(min_fix_type))
            return 3
        logger.info("MAVLink probe passed with %s GPS samples.", collected)
        return 0
    finally:
        try:
            controller.close()
        except Exception:
            pass


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("test_camera_yolo_gps")

    base_dir = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / config_path
    config = load_config(config_path=config_path, base_dir=base_dir)

    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = base_dir / output_root
    output_root.mkdir(parents=True, exist_ok=True)

    source = str(args.source).strip() or config.camera.stream_url.strip() or str(config.camera.device_id)
    inference_every_n = max(1, int(args.inference_every_n) if int(args.inference_every_n) > 0 else int(config.survey.inference_every_n))
    min_fix_type = int(args.min_gps_fix_type) if int(args.min_gps_fix_type) > 0 else int(config.survey.min_gps_fix_type)
    mavlink_connection = str(args.mavlink_connection).strip() or str(config.mission.mavlink_connection)
    mavlink_baudrate = int(args.mavlink_baudrate) if int(args.mavlink_baudrate) > 0 else int(config.mission.mavlink_baudrate)

    if args.mavlink_test_only:
        return _run_mavlink_probe(
            connection_string=mavlink_connection,
            baudrate=mavlink_baudrate,
            min_fix_type=min_fix_type,
            timeout_sec=float(args.mavlink_timeout_sec),
            gps_samples=int(args.mavlink_gps_samples),
            logger=logger,
        )

    detector = FrameYoloDetector(config, logger)
    gps_monitor = GPSMonitor(
        connection_string=mavlink_connection,
        baudrate=mavlink_baudrate,
        min_fix_type=min_fix_type,
        logger=logger,
    )
    recorder = DroneRecorder(
        source=source,
        fps=int(args.fps),
        output_dir=str(output_root),
        fourcc=config.camera.fourcc,
        container=config.camera.container,
        auto_extract=False,
    )

    started_utc = _utc_now_iso()
    raw_video_path = ""
    detected_video_path = ""
    detections_rows: List[Dict[str, Any]] = []
    frames_processed = 0
    total_model_detections = 0
    detection_failures = 0
    writer: Optional[cv2.VideoWriter] = None
    recorder_started = False

    try:
        recorder.start()
        recorder_started = True
        if recorder.session_dir is None:
            raise RuntimeError("Recorder did not create a session directory.")
        session_dir = recorder.session_dir
        logger.info("Test session started: %s", session_dir)

        container = config.camera.container if str(config.camera.container).startswith(".") else ".avi"
        detected_path = session_dir / ("recording_detected" + container)
        detections_csv_path = session_dir / "detections.csv"
        detections_json_path = session_dir / "detections.json"
        metadata_path = session_dir / "metadata.json"

        gps_monitor.start()
        start_t = time.time()

        with detections_csv_path.open("w", encoding="utf-8", newline="") as handle:
            csv_writer = csv.DictWriter(handle, fieldnames=DETECTION_COLUMNS)
            csv_writer.writeheader()

            while True:
                if args.duration_sec > 0 and (time.time() - start_t) >= float(args.duration_sec):
                    logger.info("Reached duration limit (%.1fs).", float(args.duration_sec))
                    break

                ok, frame, frame_idx, frame_ts = recorder.record_frame(include_frame=True)
                if not ok:
                    logger.info("Camera source ended or frame read failed.")
                    break

                frames_processed = int(frame_idx)
                detections = []
                if frame_idx % inference_every_n == 0:
                    try:
                        detections = detector.detect_frame(frame)
                    except Exception as exc:
                        detection_failures += 1
                        logger.warning("Frame detection failed at frame %s: %s", frame_idx, exc)
                        detections = []

                gps_payload = gps_monitor.latest()
                gps_stats = gps_monitor.stats()
                gps_connected = bool(gps_stats.get("gps_connected", 0))

                for detection in detections:
                    bbox = detection.get("bbox_xyxy", [0.0, 0.0, 0.0, 0.0])
                    row = {
                        "frame_idx": int(frame_idx),
                        "timestamp_utc": str(frame_ts),
                        "class_name": str(detection.get("class_name", "")),
                        "confidence": float(detection.get("confidence", 0.0)),
                        "pixel_x": float(detection.get("pixel_x", 0.0)),
                        "pixel_y": float(detection.get("pixel_y", 0.0)),
                        "bbox_x1": float(bbox[0]),
                        "bbox_y1": float(bbox[1]),
                        "bbox_x2": float(bbox[2]),
                        "bbox_y2": float(bbox[3]),
                        "gps_fix_type": int(gps_payload.get("fix_type", 0)) if gps_payload else "",
                        "latitude": float(gps_payload.get("latitude", 0.0)) if gps_payload else "",
                        "longitude": float(gps_payload.get("longitude", 0.0)) if gps_payload else "",
                        "altitude_m": float(gps_payload.get("altitude_m", 0.0)) if gps_payload else "",
                        "gps_timestamp_utc": str(gps_payload.get("timestamp_utc", "")) if gps_payload else "",
                    }
                    csv_writer.writerow(row)
                    detections_rows.append(row)
                    total_model_detections += 1

                annotated = detector.annotate_frame(frame, detections)
                if annotated is None:
                    annotated = frame.copy()
                _draw_overlay(annotated, int(frame_idx), len(detections), gps_payload, gps_connected)

                if writer is None:
                    height, width = annotated.shape[:2]
                    writer = _open_video_writer(detected_path, config.camera.fourcc, float(args.fps), width, height)
                    detected_video_path = str(detected_path)
                writer.write(annotated)

                if args.preview:
                    cv2.imshow("YOLO + GPS Test", annotated)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), ord("Q"), 27):
                        logger.info("Preview stop key pressed.")
                        break

                if args.max_frames > 0 and int(frame_idx) >= int(args.max_frames):
                    logger.info("Reached max frame limit (%s).", int(args.max_frames))
                    break

        with detections_json_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {"timestamp_utc": _utc_now_iso(), "count": len(detections_rows), "detections": detections_rows},
                handle,
                indent=2,
            )

        gps_stats = gps_monitor.stats()
        metadata = {
            "session_id": session_dir.name,
            "started_utc": started_utc,
            "ended_utc": _utc_now_iso(),
            "source": source,
            "inference_every_n": int(inference_every_n),
            "min_gps_fix_type": int(min_fix_type),
            "model_path": str(config.vision.model_path),
            "conf_threshold": float(config.vision.conf_threshold),
            "target_class_name": str(config.vision.target_class_name),
            "frames_processed": int(frames_processed),
            "total_detections": int(total_model_detections),
            "detection_failures": int(detection_failures),
            "gps_updates": int(gps_stats.get("gps_updates", 0)),
            "gps_errors": int(gps_stats.get("gps_errors", 0)),
            "gps_connected": bool(gps_stats.get("gps_connected", 0)),
            "artifacts": {
                "raw_video": str(session_dir / ("recording" + container)),
                "detected_video": str(detected_path) if detected_video_path else "",
                "detections_csv": str(detections_csv_path),
                "detections_json": str(detections_json_path),
            },
        }
        with metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2)

        logger.info(
            "Test complete. session=%s frames=%s detections=%s gps_updates=%s",
            session_dir,
            frames_processed,
            total_model_detections,
            gps_stats.get("gps_updates", 0),
        )
        logger.info("Artifacts: %s", metadata["artifacts"])

    finally:
        gps_monitor.stop()
        if writer is not None:
            writer.release()
        if args.preview:
            cv2.destroyAllWindows()
        if recorder_started:
            try:
                raw_video = recorder.stop()
                raw_video_path = str(raw_video)
            except Exception:
                pass
        if raw_video_path:
            logger.info("Raw recording saved: %s", raw_video_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
