import csv
import json
import math
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from navigation.mavlink_controller import MavlinkController
from planning.coordinate_transform import CoordinateTransformer
from planning.tsp_solver import TSPSolver
from utils.config import AppConfig
from vision.remote_yolo_client import RemoteYoloClient, RemoteYoloError
from vision.recorder import DroneRecorder


RAW_CSV_COLUMNS = [
    "frame_idx",
    "timestamp_utc",
    "class_name",
    "confidence",
    "latitude",
    "longitude",
    "gps_fix_type",
    "pixel_x",
    "pixel_y",
    "bbox_x1",
    "bbox_y1",
    "bbox_x2",
    "bbox_y2",
]

UNIQUE_CSV_COLUMNS = [
    "target_id",
    "latitude",
    "longitude",
    "hit_count",
    "avg_confidence",
    "max_confidence",
    "first_seen_utc",
    "last_seen_utc",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_target_session_dir(base_dir: Path) -> Path:
    existing = []
    if base_dir.exists():
        for entry in base_dir.iterdir():
            if entry.is_dir() and re.fullmatch(r"session-\d{4}", entry.name):
                existing.append(entry.name)
    last_num = max([int(name.split("-")[1]) for name in existing]) if existing else -1
    path = base_dir / "session-{0:04d}".format(last_num + 1)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000.0
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat = lat2_r - lat1_r
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(1e-12, 1.0 - a)))
    return radius_m * c


class _GraphProjector:
    def __init__(self, points_xy: List[Tuple[float, float]], canvas_px: int, margin_px: int) -> None:
        self.canvas_px = canvas_px
        self.margin_px = margin_px
        if points_xy:
            xs = [p[0] for p in points_xy]
            ys = [p[1] for p in points_xy]
            self.min_x = min(xs)
            self.max_x = max(xs)
            self.min_y = min(ys)
            self.max_y = max(ys)
        else:
            self.min_x = self.max_x = 0.0
            self.min_y = self.max_y = 0.0

        self.range_x = self.max_x - self.min_x
        self.range_y = self.max_y - self.min_y
        usable = max(1, self.canvas_px - 2 * self.margin_px)
        scale_x = usable / self.range_x if self.range_x > 1e-9 else float(usable)
        scale_y = usable / self.range_y if self.range_y > 1e-9 else float(usable)
        self.scale = min(scale_x, scale_y)

    def project(self, point_xy: Tuple[float, float]) -> Tuple[int, int]:
        x, y = point_xy
        if self.range_x <= 1e-9:
            px = self.canvas_px // 2
        else:
            px = self.margin_px + int(round((x - self.min_x) * self.scale))

        if self.range_y <= 1e-9:
            py = self.canvas_px // 2
        else:
            py = self.canvas_px - self.margin_px - int(round((y - self.min_y) * self.scale))
        return px, py


class SurveySessionManager:
    def __init__(
        self,
        config: AppConfig,
        logger,
        telemetry_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._telemetry_log = telemetry_log

        self.tsp_solver = TSPSolver()
        self.transformer = CoordinateTransformer(config.mapping.meters_per_pixel)

        self._state_lock = threading.Lock()
        self._frame_queue = queue.Queue(maxsize=8)  # type: queue.Queue
        self._record_stop_event = threading.Event()
        self._detect_stop_event = threading.Event()
        self._gps_stop_event = threading.Event()

        self._record_thread = None  # type: Optional[threading.Thread]
        self._detect_thread = None  # type: Optional[threading.Thread]
        self._gps_thread = None  # type: Optional[threading.Thread]

        self._recorder = None  # type: Optional[DroneRecorder]
        self._gps_controller = None  # type: Optional[MavlinkController]
        self._detector_client = None  # type: Optional[RemoteYoloClient]

        self._running = False
        self._current_session_dir = None  # type: Optional[Path]
        self._current_recording_session_dir = None  # type: Optional[Path]
        self._current_video_path = None  # type: Optional[Path]
        self._frame_count = 0
        self._dropped_frame_count = 0
        self._gps_skipped_count = 0
        self._center_skipped_count = 0
        self._raw_detections = []  # type: List[Dict[str, Any]]
        self._latest_gps = None  # type: Optional[Dict[str, Any]]
        self._start_position = None  # type: Optional[Dict[str, float]]
        self._detector_online = False
        self._detector_error_count = 0
        self._detector_disconnect_count = 0
        self._detector_last_error = ""
        self._inference_dropped_count = 0
        self._partial_detection = False

    def get_detector_status(self) -> Dict[str, Any]:
        with self._state_lock:
            endpoint = "{0}:{1}".format(
                self.config.detector_service.host,
                int(self.config.detector_service.port),
            )
            return {
                "enabled": bool(self.config.detector_service.enabled),
                "endpoint": endpoint,
                "online": bool(self._detector_online),
                "error_count": int(self._detector_error_count),
                "disconnect_count": int(self._detector_disconnect_count),
                "last_error": str(self._detector_last_error),
                "inference_dropped_count": int(self._inference_dropped_count),
            }

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def current_session_dir(self) -> Optional[Path]:
        with self._state_lock:
            return self._current_session_dir

    def start_survey(self) -> Dict[str, Any]:
        with self._state_lock:
            if self._running:
                raise RuntimeError("Survey is already running.")

        if not bool(self.config.detector_service.enabled):
            raise RuntimeError("Detector service is disabled. Set detector_service.enabled=true before START_SURVEY.")

        detector_client = self._build_detector_client()
        try:
            ping_payload = detector_client.ping()
        except RemoteYoloError as exc:
            raise RuntimeError(
                "Detector service unavailable at {0}:{1}: {2}".format(
                    self.config.detector_service.host,
                    int(self.config.detector_service.port),
                    exc,
                )
            )

        source = self.config.camera.stream_url.strip() or self.config.camera.device_id
        recorder = DroneRecorder(
            source=source,
            fps=30,
            output_dir=str(self.config.paths.data_dir / "recordings"),
            fourcc=self.config.camera.fourcc,
            container=self.config.camera.container,
            auto_extract=True,
        )
        try:
            recorder.start()
        except Exception:
            detector_client.close()
            raise
        if recorder.session_dir is None:
            detector_client.close()
            raise RuntimeError("Recorder session path is unavailable.")

        session_dir = _next_target_session_dir(self.config.paths.target_sessions_dir)
        (session_dir / "graphs").mkdir(parents=True, exist_ok=True)

        try:
            detector_client.session_start(session_dir.name)
        except RemoteYoloError as exc:
            try:
                recorder.stop()
            except Exception:
                pass
            detector_client.close()
            raise RuntimeError(
                "Detector service disconnected before survey start ({0}:{1}): {2}".format(
                    self.config.detector_service.host,
                    int(self.config.detector_service.port),
                    exc,
                )
            )

        with self._state_lock:
            self._clear_frame_queue_locked()

        with self._state_lock:
            self._recorder = recorder
            self._detector_client = detector_client
            self._running = True
            self._current_session_dir = session_dir
            self._current_recording_session_dir = recorder.session_dir
            self._current_video_path = None
            self._frame_count = 0
            self._dropped_frame_count = 0
            self._gps_skipped_count = 0
            self._center_skipped_count = 0
            self._raw_detections = []
            self._latest_gps = None
            self._start_position = None
            self._detector_online = True
            self._detector_error_count = 0
            self._detector_disconnect_count = 0
            self._detector_last_error = ""
            self._inference_dropped_count = 0
            self._partial_detection = False

        self._record_stop_event.clear()
        self._detect_stop_event.clear()
        self._gps_stop_event.clear()

        self._write_metadata(
            session_dir,
            {
                "session_id": session_dir.name,
                "survey_started_utc": _utc_now_iso(),
                "survey_ended_utc": None,
                "recording_session": str(recorder.session_dir),
                "recording_video": "",
                "model_path": str(self._resolve_model_path()),
                "conf_threshold": float(self.config.vision.conf_threshold),
                "target_class_name": str(self.config.vision.target_class_name),
                "inference_every_n": int(self.config.survey.inference_every_n),
                "detection_interval_sec": float(self.config.survey.detection_interval_sec),
                "dedup_radius_m": float(self.config.survey.dedup_radius_m),
                "min_gps_fix_type": int(self.config.survey.min_gps_fix_type),
                "closed_cycle": True,
                "gps_skipped_count": 0,
                "center_skipped_count": 0,
                "raw_detection_count": 0,
                "unique_target_count": 0,
                "detector_mode": "remote_tcp",
                "detector_enabled": True,
                "detector_endpoint": "{0}:{1}".format(
                    self.config.detector_service.host,
                    int(self.config.detector_service.port),
                ),
                "detector_ping": ping_payload,
                "detector_error_count": 0,
                "detector_disconnect_count": 0,
                "inference_dropped_count": 0,
                "detector_online": True,
                "detector_last_error": "",
                "partial_detection_finalized": False,
                "graphs": {},
                "artifacts": {},
            },
        )

        self._record_thread = threading.Thread(target=self._record_loop, name="SurveyRecorder", daemon=True)
        self._detect_thread = threading.Thread(target=self._detect_loop, name="SurveyDetector", daemon=True)
        self._gps_thread = threading.Thread(target=self._gps_loop, name="SurveyGPS", daemon=True)
        self._detect_thread.start()
        self._gps_thread.start()
        self._record_thread.start()

        self._log("Survey started. session={0}".format(session_dir))
        return {
            "ok": True,
            "message": "Survey started. Session: {0}".format(session_dir),
            "session_dir": str(session_dir),
            "recording_session": str(recorder.session_dir),
            "detector_endpoint": "{0}:{1}".format(
                self.config.detector_service.host,
                int(self.config.detector_service.port),
            ),
        }

    def stop_survey(self) -> Dict[str, Any]:
        with self._state_lock:
            running = self._running
            session_dir = self._current_session_dir
        if not running:
            return {"ok": False, "message": "No active survey in progress."}
        if session_dir is None:
            return {"ok": False, "message": "Survey session path missing."}

        self._record_stop_event.set()
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=10.0)
        self._detect_stop_event.set()
        if self._detect_thread and self._detect_thread.is_alive():
            self._detect_thread.join(timeout=20.0)
        self._gps_stop_event.set()
        if self._gps_thread and self._gps_thread.is_alive():
            self._gps_thread.join(timeout=5.0)

        try:
            result = self._finalize_session(session_dir)
            self._log("Survey finalized. raw={0} unique={1}".format(result["raw_count"], result["unique_count"]))
            return result
        finally:
            self._close_detector_client(session_id=session_dir.name)
            with self._state_lock:
                self._running = False

    def build_route(self, session_dir: Optional[Path] = None) -> Dict[str, Any]:
        with self._state_lock:
            if self._running:
                raise RuntimeError("Cannot build route while survey is running.")
            default_session = self._current_session_dir
        target = session_dir or default_session or self.get_latest_session(require_route=False)
        if target is None:
            raise RuntimeError("No survey session available for route generation.")
        return self._finalize_session(target)

    def load_latest_route(self) -> Dict[str, Any]:
        session_dir = self.get_latest_session(require_route=True)
        if session_dir is None:
            raise RuntimeError("No completed target session route found.")
        route_path = session_dir / "route_tsp_cycle.json"
        with route_path.open("r", encoding="utf-8") as handle:
            route = json.load(handle)
        return {
            "session_dir": str(session_dir),
            "route_path": str(route_path),
            "route": route,
            "waypoints": route.get("waypoints", []),
        }

    def get_latest_session(self, require_route: bool) -> Optional[Path]:
        root = self.config.paths.target_sessions_dir
        if not root.exists():
            return None
        candidates = []
        for entry in root.iterdir():
            if not entry.is_dir() or not re.fullmatch(r"session-\d{4}", entry.name):
                continue
            if require_route and not (entry / "route_tsp_cycle.json").exists():
                continue
            candidates.append(entry)
        if not candidates:
            return None
        return sorted(candidates, key=lambda p: p.name)[-1]

    def _record_loop(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        detection_interval_sec = max(0.05, float(self.config.survey.detection_interval_sec))
        next_detection_ts = 0.0
        try:
            while not self._record_stop_event.is_set():
                ok, frame, frame_idx, frame_ts = recorder.record_frame(include_frame=True)
                if not ok:
                    break
                with self._state_lock:
                    self._frame_count = int(frame_idx)

                now_monotonic = time.monotonic()
                if now_monotonic < next_detection_ts:
                    continue
                next_detection_ts = now_monotonic + detection_interval_sec
                try:
                    self._frame_queue.put((frame_idx, frame_ts, frame), timeout=0.01)
                except queue.Full:
                    with self._state_lock:
                        self._dropped_frame_count += 1
                        self._inference_dropped_count += 1
        finally:
            video_path = None
            try:
                video_path = recorder.stop()
            except Exception as exc:
                self.logger.warning("Failed to stop recorder cleanly: %s", exc)
            with self._state_lock:
                self._current_video_path = video_path

    def _detect_loop(self) -> None:
        client = self._detector_client
        if client is None:
            return
        ratio = max(0.0, min(1.0, float(self.config.survey.center_region_ratio)))

        while True:
            if self._detect_stop_event.is_set() and self._frame_queue.empty():
                break
            try:
                frame_idx, frame_ts, frame = self._frame_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            with self._state_lock:
                detector_online = bool(self._detector_online)
            if not detector_online:
                with self._state_lock:
                    self._inference_dropped_count += 1
                continue

            try:
                detections = client.infer(frame=frame, frame_idx=int(frame_idx), frame_ts=str(frame_ts))
            except RemoteYoloError as exc:
                self._on_detector_disconnect(str(exc))
                continue
            except Exception as exc:
                with self._state_lock:
                    self._detector_error_count += 1
                    self._detector_last_error = str(exc)
                    self._inference_dropped_count += 1
                self.logger.warning("Remote detector inference failed: %s", exc)
                continue

            frame_h, frame_w = frame.shape[:2]
            fw = float(frame_w)
            fh = float(frame_h)
            margin_x = fw * (1.0 - ratio) / 2.0
            margin_y = fh * (1.0 - ratio) / 2.0
            for detection in detections:
                px = float(detection.get("pixel_x", 0.0))
                py = float(detection.get("pixel_y", 0.0))
                if px < margin_x or px > fw - margin_x or py < margin_y or py > fh - margin_y:
                    with self._state_lock:
                        self._center_skipped_count += 1
                    continue
                self._append_raw_detection(frame_idx, frame_ts, detection)

    def _gps_loop(self) -> None:
        min_fix = int(self.config.survey.min_gps_fix_type)
        while not self._gps_stop_event.is_set():
            controller = self._gps_controller
            if controller is None:
                controller = MavlinkController(
                    connection_string=self.config.mission.mavlink_connection,
                    baudrate=self.config.mission.mavlink_baudrate,
                    logger=self.logger,
                )
                try:
                    controller.connect(timeout_sec=10)
                except Exception as exc:
                    self.logger.warning("GPS monitor connect failed: %s", exc)
                    try:
                        controller.close()
                    except Exception:
                        pass
                    time.sleep(2.0)
                    continue
                with self._state_lock:
                    self._gps_controller = controller

            try:
                msg = controller.recv_match("GPS_RAW_INT", timeout=1.0)
            except Exception as exc:
                self.logger.warning("GPS monitor read failed: %s", exc)
                try:
                    controller.close()
                except Exception:
                    pass
                with self._state_lock:
                    self._gps_controller = None
                time.sleep(1.0)
                continue
            if msg is None:
                continue
            fix_type = int(getattr(msg, "fix_type", 0))
            if fix_type < min_fix:
                continue
            lat = float(getattr(msg, "lat", 0.0)) / 1e7
            lon = float(getattr(msg, "lon", 0.0)) / 1e7
            payload = {
                "latitude": lat,
                "longitude": lon,
                "altitude_m": float(getattr(msg, "alt", 0.0)) / 1000.0,
                "fix_type": fix_type,
                "timestamp_utc": _utc_now_iso(),
            }
            with self._state_lock:
                self._latest_gps = payload
                if self._start_position is None:
                    self._start_position = {"latitude": lat, "longitude": lon}

        controller = self._gps_controller
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
        with self._state_lock:
            self._gps_controller = None

    def _build_detector_client(self) -> RemoteYoloClient:
        return RemoteYoloClient(
            host=self.config.detector_service.host,
            port=int(self.config.detector_service.port),
            request_timeout_sec=float(self.config.detector_service.request_timeout_sec),
            connect_timeout_sec=float(self.config.detector_service.connect_timeout_sec),
            jpeg_quality=int(self.config.detector_service.jpeg_quality),
            logger=self.logger,
        )

    def _close_detector_client(self, session_id: str) -> None:
        with self._state_lock:
            client = self._detector_client
            self._detector_client = None
            self._detector_online = False
        if client is None:
            return
        try:
            client.session_end(session_id=session_id)
        except Exception as exc:
            self.logger.warning("Detector SESSION_END failed: %s", exc)
        finally:
            client.close()

    def _on_detector_disconnect(self, reason: str) -> None:
        reason = str(reason)
        with self._state_lock:
            was_online = bool(self._detector_online)
            self._detector_online = False
            self._detector_error_count += 1
            self._detector_last_error = reason
            self._inference_dropped_count += 1
            self._partial_detection = True
            if was_online:
                self._detector_disconnect_count += 1

        if was_online:
            self.logger.warning("Detector service disconnected during survey: %s", reason)
            self._log("Detector service disconnected. Finalization will use partial detections.")

    def _clear_frame_queue_locked(self) -> None:
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _append_raw_detection(self, frame_idx: int, frame_ts: str, detection: Dict[str, Any]) -> None:
        with self._state_lock:
            gps = dict(self._latest_gps) if self._latest_gps else None
        if gps is None:
            with self._state_lock:
                self._gps_skipped_count += 1
            return
        bbox = detection.get("bbox_xyxy", [0.0, 0.0, 0.0, 0.0])
        row = {
            "frame_idx": int(frame_idx),
            "timestamp_utc": str(frame_ts or _utc_now_iso()),
            "class_name": str(detection.get("class_name", "")),
            "confidence": float(detection.get("confidence", 0.0)),
            "latitude": round(float(gps["latitude"]), 8),
            "longitude": round(float(gps["longitude"]), 8),
            "gps_fix_type": int(gps.get("fix_type", 0)),
            "pixel_x": float(detection.get("pixel_x", 0.0)),
            "pixel_y": float(detection.get("pixel_y", 0.0)),
            "bbox_x1": float(bbox[0]),
            "bbox_y1": float(bbox[1]),
            "bbox_x2": float(bbox[2]),
            "bbox_y2": float(bbox[3]),
        }
        with self._state_lock:
            self._raw_detections.append(row)

    def _finalize_session(self, session_dir: Path) -> Dict[str, Any]:
        raw = self._load_or_current_raw(session_dir)
        raw = sorted(raw, key=lambda row: (int(row.get("frame_idx", 0)), str(row.get("timestamp_utc", ""))))
        metadata = self._load_metadata(session_dir)

        start_position = self._resolve_start_position(metadata, raw)
        unique_targets = self._cluster_targets(raw, float(self.config.survey.dedup_radius_m))
        route_payload = self._build_route_payload(session_dir.name, start_position, unique_targets)

        graphs_dir = session_dir / "graphs"
        graphs_dir.mkdir(parents=True, exist_ok=True)
        raw_graph_path = graphs_dir / "raw_points.png"
        tsp_graph_path = graphs_dir / "tsp_cycle.png"

        self._write_raw_files(session_dir, raw)
        self._write_unique_files(session_dir, unique_targets)
        self._write_route_file(session_dir, route_payload)
        self._write_raw_graph(raw_graph_path, raw, start_position)
        self._write_tsp_graph(tsp_graph_path, route_payload)

        with self._state_lock:
            is_current_session = self._current_session_dir == session_dir
            gps_skipped_count = int(self._gps_skipped_count) if is_current_session else int(metadata.get("gps_skipped_count", 0))
            center_skipped_count = int(self._center_skipped_count) if is_current_session else int(metadata.get("center_skipped_count", 0))
            frame_count = int(self._frame_count) if is_current_session else int(metadata.get("frame_count", 0))
            dropped_frame_count = (
                int(self._dropped_frame_count) if is_current_session else int(metadata.get("dropped_frame_count", 0))
            )
            inference_dropped_count = (
                int(self._inference_dropped_count) if is_current_session else int(metadata.get("inference_dropped_count", 0))
            )
            detector_error_count = (
                int(self._detector_error_count) if is_current_session else int(metadata.get("detector_error_count", 0))
            )
            detector_disconnect_count = (
                int(self._detector_disconnect_count)
                if is_current_session
                else int(metadata.get("detector_disconnect_count", 0))
            )
            detector_last_error = (
                str(self._detector_last_error) if is_current_session else str(metadata.get("detector_last_error", ""))
            )
            detector_online = bool(self._detector_online) if is_current_session else bool(metadata.get("detector_online", False))
            partial_detection = (
                bool(self._partial_detection) if is_current_session else bool(metadata.get("partial_detection_finalized", False))
            )
            recording_session = str(self._current_recording_session_dir or "") if is_current_session else str(
                metadata.get("recording_session", "")
            )
            recording_video = str(self._current_video_path or "") if is_current_session else str(
                metadata.get("recording_video", "")
            )

        recording_video_path = self._resolve_recording_video_path(recording_video, recording_session)
        detected_video_path = self._write_detected_video(session_dir, recording_video_path, raw)

        metadata.update(
            {
                "survey_ended_utc": _utc_now_iso(),
                "recording_session": metadata.get("recording_session", recording_session),
                "recording_video": str(recording_video_path) if recording_video_path else (
                    metadata.get("recording_video", recording_video) or recording_video
                ),
                "frame_count": frame_count,
                "dropped_frame_count": dropped_frame_count,
                "inference_dropped_count": inference_dropped_count,
                "gps_skipped_count": gps_skipped_count,
                "center_skipped_count": center_skipped_count,
                "center_region_ratio": float(self.config.survey.center_region_ratio),
                "detection_interval_sec": float(self.config.survey.detection_interval_sec),
                "raw_detection_count": len(raw),
                "unique_target_count": len(unique_targets),
                "start_position": start_position,
                "detector_mode": "remote_tcp",
                "detector_enabled": bool(self.config.detector_service.enabled),
                "detector_endpoint": "{0}:{1}".format(
                    self.config.detector_service.host,
                    int(self.config.detector_service.port),
                ),
                "detector_online": detector_online,
                "detector_error_count": detector_error_count,
                "detector_disconnect_count": detector_disconnect_count,
                "detector_last_error": detector_last_error,
                "partial_detection_finalized": partial_detection,
                "graphs": {
                    "raw_points": str(raw_graph_path),
                    "tsp_cycle": str(tsp_graph_path),
                },
                "artifacts": {
                    "raw_csv": str(session_dir / "raw_detections.csv"),
                    "raw_json": str(session_dir / "raw_detections.json"),
                    "unique_csv": str(session_dir / "unique_targets.csv"),
                    "unique_json": str(session_dir / "unique_targets.json"),
                    "route_json": str(session_dir / "route_tsp_cycle.json"),
                    "detected_video": str(detected_video_path) if detected_video_path else "",
                },
            }
        )
        self._write_metadata(session_dir, metadata)

        return {
            "ok": True,
            "message": "Survey session finalized.",
            "session_dir": str(session_dir),
            "recording_session": metadata.get("recording_session", ""),
            "recording_video": metadata.get("recording_video", ""),
            "raw_count": len(raw),
            "unique_count": len(unique_targets),
            "gps_skipped_count": gps_skipped_count,
            "center_skipped_count": center_skipped_count,
            "inference_dropped_count": inference_dropped_count,
            "detector_online": detector_online,
            "detector_error_count": detector_error_count,
            "detector_disconnect_count": detector_disconnect_count,
            "detector_last_error": detector_last_error,
            "partial_detection_finalized": partial_detection,
            "route_path": str(session_dir / "route_tsp_cycle.json"),
            "raw_graph": str(raw_graph_path),
            "tsp_graph": str(tsp_graph_path),
            "detected_video": str(detected_video_path) if detected_video_path else "",
            "waypoints": route_payload.get("waypoints", []),
            "route": route_payload,
        }

    def _load_or_current_raw(self, session_dir: Path) -> List[Dict[str, Any]]:
        with self._state_lock:
            current_session = self._current_session_dir
            current_raw = [dict(row) for row in self._raw_detections]
        if current_session == session_dir and current_raw:
            return current_raw

        raw_json_path = session_dir / "raw_detections.json"
        if raw_json_path.exists():
            with raw_json_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rows = payload.get("raw_detections", [])
            if isinstance(rows, list):
                return rows

        raw_csv_path = session_dir / "raw_detections.csv"
        if raw_csv_path.exists():
            rows = []  # type: List[Dict[str, Any]]
            with raw_csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    rows.append(row)
            return rows
        return []

    def _resolve_recording_video_path(self, recording_video: str, recording_session: str) -> Optional[Path]:
        candidates = []  # type: List[Path]
        if recording_video:
            candidates.append(Path(recording_video))
        if recording_session:
            session_path = Path(recording_session)
            candidates.append(session_path / ("recording" + self.config.camera.container))
            candidates.append(session_path / "recording.avi")
            candidates.append(session_path / "recording.mp4")

        for candidate in candidates:
            path = candidate if candidate.is_absolute() else (self.config.paths.base_dir / candidate)
            if path.exists() and path.is_file():
                return path
        return None

    def _write_detected_video(
        self,
        session_dir: Path,
        recording_video_path: Optional[Path],
        raw: List[Dict[str, Any]],
    ) -> Optional[Path]:
        if recording_video_path is None:
            self.logger.warning("Detected-video export skipped: recording video path unavailable.")
            return None

        extension = self.config.camera.container if str(self.config.camera.container).startswith(".") else ".avi"
        detected_video_path = session_dir / ("recording_detected" + extension)
        if detected_video_path.exists():
            return detected_video_path

        cap = cv2.VideoCapture(str(recording_video_path))
        if not cap.isOpened():
            self.logger.warning("Detected-video export skipped: cannot open %s", recording_video_path)
            return None

        writer = None
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            if fps <= 1e-6:
                fps = 30.0

            ok, first_frame = cap.read()
            if not ok or first_frame is None:
                self.logger.warning("Detected-video export skipped: empty recording %s", recording_video_path)
                return None

            frame_h, frame_w = first_frame.shape[:2]
            writer = cv2.VideoWriter(
                str(detected_video_path),
                cv2.VideoWriter_fourcc(*self.config.camera.fourcc),
                fps,
                (frame_w, frame_h),
            )
            if not writer.isOpened():
                writer.release()
                writer = cv2.VideoWriter(
                    str(detected_video_path),
                    cv2.VideoWriter_fourcc(*"MJPG"),
                    fps,
                    (frame_w, frame_h),
                )
            if not writer.isOpened():
                self.logger.warning("Detected-video export skipped: could not create writer for %s", detected_video_path)
                return None

            detections_by_frame = {}  # type: Dict[int, List[Dict[str, Any]]]
            for row in raw:
                try:
                    frame_idx = int(float(row.get("frame_idx", 0)))
                except (TypeError, ValueError):
                    continue
                if frame_idx <= 0:
                    continue

                try:
                    x1 = float(row.get("bbox_x1", 0.0))
                    y1 = float(row.get("bbox_y1", 0.0))
                    x2 = float(row.get("bbox_x2", 0.0))
                    y2 = float(row.get("bbox_y2", 0.0))
                    px = float(row.get("pixel_x", 0.0))
                    py = float(row.get("pixel_y", 0.0))
                    conf = float(row.get("confidence", 0.0))
                except (TypeError, ValueError):
                    continue

                detection = {
                    "class_name": str(row.get("class_name", "")),
                    "confidence": conf,
                    "pixel_x": px,
                    "pixel_y": py,
                    "bbox_xyxy": [x1, y1, x2, y2],
                }
                detections_by_frame.setdefault(frame_idx, []).append(detection)

            frame_idx = 1
            first_detections = detections_by_frame.get(frame_idx, [])
            first_annotated = self._annotate_frame(first_frame, first_detections) if first_detections else first_frame
            writer.write(first_annotated if first_annotated is not None else first_frame)

            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_idx += 1
                detections = detections_by_frame.get(frame_idx, [])
                annotated = self._annotate_frame(frame, detections) if detections else frame
                writer.write(annotated if annotated is not None else frame)
        finally:
            cap.release()
            if writer is not None:
                writer.release()

        if detected_video_path.exists():
            self.logger.info("Detected-video export complete: %s", detected_video_path)
            return detected_video_path
        return None

    def _resolve_start_position(self, metadata: Dict[str, Any], raw: List[Dict[str, Any]]) -> Dict[str, float]:
        saved = metadata.get("start_position", {})
        if isinstance(saved, dict) and "latitude" in saved and "longitude" in saved:
            return {
                "latitude": float(saved["latitude"]),
                "longitude": float(saved["longitude"]),
            }

        with self._state_lock:
            if self._start_position is not None:
                return {
                    "latitude": float(self._start_position["latitude"]),
                    "longitude": float(self._start_position["longitude"]),
                }

        if raw:
            first = raw[0]
            return {
                "latitude": float(first["latitude"]),
                "longitude": float(first["longitude"]),
            }

        if self.config.mission.home_latitude is not None and self.config.mission.home_longitude is not None:
            return {
                "latitude": float(self.config.mission.home_latitude),
                "longitude": float(self.config.mission.home_longitude),
            }
        return {"latitude": 0.0, "longitude": 0.0}

    def _cluster_targets(self, raw: List[Dict[str, Any]], radius_m: float) -> List[Dict[str, Any]]:
        clusters = []  # type: List[Dict[str, Any]]
        for row in raw:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            conf = float(row.get("confidence", 0.0))
            ts = str(row.get("timestamp_utc", ""))

            best_idx = None
            best_dist = None  # type: Optional[float]
            for idx, cluster in enumerate(clusters):
                distance = _haversine_meters(lat, lon, float(cluster["latitude"]), float(cluster["longitude"]))
                if distance <= radius_m and (best_dist is None or distance < best_dist):
                    best_dist = distance
                    best_idx = idx

            if best_idx is None:
                clusters.append(
                    {
                        "latitude": lat,
                        "longitude": lon,
                        "hit_count": 1,
                        "confidence_sum": conf,
                        "max_confidence": conf,
                        "first_seen_utc": ts,
                        "last_seen_utc": ts,
                    }
                )
                continue

            cluster = clusters[best_idx]
            hit_count = int(cluster["hit_count"]) + 1
            cluster["latitude"] = (float(cluster["latitude"]) * (hit_count - 1) + lat) / hit_count
            cluster["longitude"] = (float(cluster["longitude"]) * (hit_count - 1) + lon) / hit_count
            cluster["hit_count"] = hit_count
            cluster["confidence_sum"] = float(cluster["confidence_sum"]) + conf
            cluster["max_confidence"] = max(float(cluster["max_confidence"]), conf)
            cluster["last_seen_utc"] = ts

        result = []  # type: List[Dict[str, Any]]
        for idx, cluster in enumerate(clusters):
            hit_count = max(1, int(cluster["hit_count"]))
            result.append(
                {
                    "target_id": idx,
                    "latitude": round(float(cluster["latitude"]), 8),
                    "longitude": round(float(cluster["longitude"]), 8),
                    "hit_count": hit_count,
                    "avg_confidence": round(float(cluster["confidence_sum"]) / hit_count, 4),
                    "max_confidence": round(float(cluster["max_confidence"]), 4),
                    "first_seen_utc": str(cluster["first_seen_utc"]),
                    "last_seen_utc": str(cluster["last_seen_utc"]),
                }
            )
        return result

    def _build_route_payload(
        self,
        session_id: str,
        start_position: Dict[str, float],
        unique_targets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        start_lat = float(start_position["latitude"])
        start_lon = float(start_position["longitude"])

        targets_xy = []
        for target in unique_targets:
            x_m, y_m = self.transformer.gps_to_relative(
                start_lat,
                start_lon,
                float(target["latitude"]),
                float(target["longitude"]),
            )
            targets_xy.append((x_m, y_m))

        solution = self.tsp_solver.solve(targets_xy=targets_xy, start_xy=(0.0, 0.0), include_return_to_start=True)
        ordered_targets = []  # type: List[Dict[str, Any]]
        for visit_order, idx in enumerate(solution.order, start=1):
            target = dict(unique_targets[idx])
            rel_x, rel_y = targets_xy[idx]
            target["visit_order"] = visit_order
            target["relative_x_m"] = round(rel_x, 3)
            target["relative_y_m"] = round(rel_y, 3)
            ordered_targets.append(target)

        waypoints = []  # type: List[Dict[str, Any]]
        for index, target in enumerate(ordered_targets):
            waypoints.append(
                {
                    "index": index,
                    "visit_order": int(target["visit_order"]),
                    "target_id": int(target["target_id"]),
                    "latitude": float(target["latitude"]),
                    "longitude": float(target["longitude"]),
                    "altitude_m": float(self.config.mission.default_altitude_m),
                    "hover_time": float(self.config.mission.hover_time_sec),
                }
            )

        if ordered_targets:
            waypoints.append(
                {
                    "index": len(waypoints),
                    "visit_order": len(waypoints) + 1,
                    "target_id": "RETURN_START",
                    "latitude": round(start_lat, 8),
                    "longitude": round(start_lon, 8),
                    "altitude_m": float(self.config.mission.default_altitude_m),
                    "hover_time": float(self.config.mission.hover_time_sec),
                }
            )

        return {
            "session_id": session_id,
            "timestamp_utc": _utc_now_iso(),
            "closed_cycle": True,
            "total_targets": len(ordered_targets),
            "total_distance_m": round(float(solution.distance_m), 3),
            "start_position": {
                "latitude": round(start_lat, 8),
                "longitude": round(start_lon, 8),
            },
            "ordered_targets": ordered_targets,
            "waypoints": waypoints,
        }

    def _write_raw_files(self, session_dir: Path, raw: List[Dict[str, Any]]) -> None:
        csv_path = session_dir / "raw_detections.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RAW_CSV_COLUMNS)
            writer.writeheader()
            for row in raw:
                normalized = dict(row)
                for key in RAW_CSV_COLUMNS:
                    normalized.setdefault(key, "")
                writer.writerow(normalized)

        json_path = session_dir / "raw_detections.json"
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump({"timestamp_utc": _utc_now_iso(), "count": len(raw), "raw_detections": raw}, handle, indent=2)

    def _write_unique_files(self, session_dir: Path, unique_targets: List[Dict[str, Any]]) -> None:
        csv_path = session_dir / "unique_targets.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=UNIQUE_CSV_COLUMNS)
            writer.writeheader()
            for row in unique_targets:
                normalized = dict(row)
                for key in UNIQUE_CSV_COLUMNS:
                    normalized.setdefault(key, "")
                writer.writerow(normalized)

        json_path = session_dir / "unique_targets.json"
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump({"timestamp_utc": _utc_now_iso(), "count": len(unique_targets), "targets": unique_targets}, handle, indent=2)

    def _write_route_file(self, session_dir: Path, route_payload: Dict[str, Any]) -> None:
        with (session_dir / "route_tsp_cycle.json").open("w", encoding="utf-8") as handle:
            json.dump(route_payload, handle, indent=2)

    def _write_raw_graph(self, graph_path: Path, raw: List[Dict[str, Any]], start_position: Dict[str, float]) -> None:
        canvas_px = int(self.config.survey.graph_canvas_px)
        image = np.full((canvas_px, canvas_px, 3), 255, dtype=np.uint8)
        start_lat = float(start_position["latitude"])
        start_lon = float(start_position["longitude"])

        points_xy = []
        for row in raw:
            points_xy.append(
                self.transformer.gps_to_relative(start_lat, start_lon, float(row["latitude"]), float(row["longitude"]))
            )
        projector = _GraphProjector(points_xy + [(0.0, 0.0)], canvas_px, int(self.config.survey.graph_margin_px))
        start_px = projector.project((0.0, 0.0))
        cv2.circle(image, start_px, 7, (255, 0, 0), -1)
        cv2.putText(image, "START", (start_px[0] + 8, start_px[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)

        if not points_xy:
            cv2.putText(image, "No raw GPS detections", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 80), 2)
        else:
            for idx, point in enumerate(points_xy):
                px = projector.project(point)
                cv2.circle(image, px, 3, (0, 0, 255), -1)
                if idx < 25:
                    cv2.putText(image, str(idx + 1), (px[0] + 3, px[1] - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 1)

        cv2.imwrite(str(graph_path), image)

    def _write_tsp_graph(self, graph_path: Path, route_payload: Dict[str, Any]) -> None:
        canvas_px = int(self.config.survey.graph_canvas_px)
        image = np.full((canvas_px, canvas_px, 3), 255, dtype=np.uint8)
        ordered_targets = route_payload.get("ordered_targets", [])
        ordered_xy = [(float(t["relative_x_m"]), float(t["relative_y_m"])) for t in ordered_targets]
        projector = _GraphProjector(ordered_xy + [(0.0, 0.0)], canvas_px, int(self.config.survey.graph_margin_px))

        start_px = projector.project((0.0, 0.0))
        cv2.circle(image, start_px, 7, (255, 0, 0), -1)
        cv2.putText(image, "START", (start_px[0] + 8, start_px[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)

        if not ordered_xy:
            cv2.putText(image, "No unique targets for TSP", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 80), 2)
            cv2.imwrite(str(graph_path), image)
            return

        cycle_points = [(0.0, 0.0)] + ordered_xy + [(0.0, 0.0)]
        cycle_pixels = [projector.project(point) for point in cycle_points]
        for idx in range(1, len(cycle_pixels)):
            cv2.line(image, cycle_pixels[idx - 1], cycle_pixels[idx], (0, 170, 0), 2)

        for order, point in enumerate(ordered_xy, start=1):
            px = projector.project(point)
            cv2.circle(image, px, 5, (0, 0, 255), -1)
            cv2.putText(image, str(order), (px[0] + 5, px[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

        cv2.imwrite(str(graph_path), image)

    def _annotate_frame(self, frame, detections: List[Dict[str, Any]]):
        if frame is None:
            return None
        image = frame.copy()
        for detection in detections:
            try:
                x1, y1, x2, y2 = [int(float(v)) for v in detection.get("bbox_xyxy", [0.0, 0.0, 0.0, 0.0])]
                cls_name = str(detection.get("class_name", "target"))
                conf = float(detection.get("confidence", 0.0))
                px = int(float(detection.get("pixel_x", 0.0)))
                py = int(float(detection.get("pixel_y", 0.0)))
            except (TypeError, ValueError):
                continue

            label = "{0}:{1:.2f}".format(cls_name, conf)
            cv2.rectangle(image, (x1, y1), (x2, y2), (60, 220, 20), 2)
            cv2.circle(image, (px, py), 4, (10, 10, 240), -1)
            cv2.putText(image, label, (x1, max(y1 - 10, 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        return image

    def _load_metadata(self, session_dir: Path) -> Dict[str, Any]:
        path = session_dir / "metadata.json"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}

    def _write_metadata(self, session_dir: Path, payload: Dict[str, Any]) -> None:
        with (session_dir / "metadata.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def _resolve_model_path(self) -> Path:
        path = Path(self.config.vision.model_path)
        if not path.is_absolute():
            path = self.config.paths.base_dir / path
        return path

    def _log(self, message: str) -> None:
        self.logger.info(message)
        if self._telemetry_log:
            try:
                self._telemetry_log(message)
            except Exception:
                pass
