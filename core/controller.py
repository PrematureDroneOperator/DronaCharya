import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mapping.mapper import TerrainMapper
from navigation.mavlink_controller import MavlinkController
from navigation.mission_executor import MissionExecutor
from planning.coordinate_transform import CoordinateTransformer
from planning.tsp_solver import TSPSolver
from telemetry.telemetry_server import TelemetryServer
from utils.config import AppConfig
from utils.logger import RingBufferLogHandler
from vision.recorder import DroneRecorder
from vision.yolo_detector import YoloTargetDetector


@dataclass
class AppState:
    connection_status: str = "UNKNOWN"
    current_mode: str = "CLI"
    mapping_progress: float = 0.0
    detected_targets_count: int = 0
    mission_state: str = "IDLE"
    recording_state: str = "IDLE"           # IDLE | RECORDING | STOPPED
    last_recording_session: str = ""        # path of last session-XXXX dir
    last_map_path: str = ""
    last_detection_path: str = ""
    last_route_path: str = ""
    last_error: str = ""
    takeoff_latitude: Optional[float] = None
    takeoff_longitude: Optional[float] = None


@dataclass
class CommandRequest:
    command: str
    source: str
    wait_event: Optional[threading.Event] = None
    response: Dict[str, Any] = field(default_factory=dict)


class DroneAcharyaController:
    def __init__(self, config: AppConfig, logger, log_handler: RingBufferLogHandler) -> None:
        self.config = config
        self.logger = logger
        self.log_handler = log_handler

        self.state = AppState()
        self.state_lock = threading.Lock()

        self.telemetry_server = TelemetryServer(config.telemetry, logger)
        self.mapper = TerrainMapper(config, logger)
        self.detector = YoloTargetDetector(config, logger)
        self.tsp_solver = TSPSolver()
        self.transformer = CoordinateTransformer(config.mapping.meters_per_pixel)

        self._targets = []  # type: List[Dict[str, Any]]
        self._ordered_waypoints = []  # type: List[Dict[str, Any]]
        self._abort_event = threading.Event()

        # Recording
        self._recorder = None  # type: Optional[DroneRecorder]
        self._rec_thread = None  # type: Optional[threading.Thread]
        self._rec_stop_event = threading.Event()

        self._command_queue = queue.Queue()  # type: queue.Queue
        self._worker_stop = threading.Event()
        self._worker_thread = threading.Thread(target=self._command_worker, name="CommandWorker", daemon=True)

    def start(self, mode: str) -> None:
        normalized_mode = mode.upper()
        self._set_state(current_mode=normalized_mode, mission_state="READY", last_error="")
        self._worker_stop.clear()
        self._worker_thread.start()
        self.telemetry_server.start(self._on_remote_command)
        self._send_status()
        self.logger.info("dronAcharya started in %s mode.", normalized_mode)

    def stop(self) -> None:
        self._worker_stop.set()
        self._command_queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
        self.telemetry_server.stop()
        self.logger.info("dronAcharya stopped.")

    def submit_command(
        self, command: str, source: str = "local", wait: bool = True, timeout: float = 3600.0
    ) -> Dict[str, Any]:
        event = threading.Event() if wait else None
        request = CommandRequest(command=command, source=source, wait_event=event)
        self._command_queue.put(request)

        if wait and event:
            completed = event.wait(timeout=timeout)
            if not completed:
                return {"ok": False, "message": f"Command '{command}' timed out after {timeout:.0f}s"}
            return request.response
        return {"ok": True, "message": f"Command '{command}' queued"}

    def get_status_snapshot(self) -> Dict[str, Any]:
        with self.state_lock:
            snapshot = asdict(self.state)
        snapshot["queue_depth"] = self._command_queue.qsize()
        return snapshot

    def get_recent_logs(self, offset: int = 0) -> Tuple[int, List[str]]:
        records = self.log_handler.snapshot()
        if offset < 0:
            offset = 0
        return len(records), records[offset:]

    def _on_remote_command(self, command: str, source_addr: Tuple[str, int]) -> None:
        self.submit_command(command=command, source=f"gcs:{source_addr[0]}:{source_addr[1]}", wait=False)

    def _command_worker(self) -> None:
        while not self._worker_stop.is_set():
            try:
                request = self._command_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if request is None:
                continue

            command = self._normalize_command(request.command)
            try:
                result = self._execute_command(command, source=request.source)
            except Exception as exc:
                self.logger.exception("Command '%s' failed", command)
                self._set_state(last_error=str(exc), mission_state="ERROR")
                self._send_status()
                self.telemetry_server.send_log(f"{command} failed: {exc}", level="ERROR")
                result = {"ok": False, "message": str(exc)}

            request.response.update(result)
            if request.wait_event:
                request.wait_event.set()

    def _normalize_command(self, command: str) -> str:
        canonical = command.strip().upper()
        aliases = {
            "MAP": "START_MAPPING",
            "DETECT": "RUN_DETECTION",
            "PLAN": "PLAN_ROUTE",
            "STATUS": "STATUS_REQUEST",
        }
        return aliases.get(canonical, canonical)

    def _execute_command(self, command: str, source: str) -> Dict[str, Any]:
        self.logger.info("Executing command '%s' (source=%s)", command, source)

        if command == "START_MAPPING":
            return self._run_mapping()
        if command == "RUN_DETECTION":
            return self._run_detection()
        if command == "PLAN_ROUTE":
            return self._run_planning()
        if command == "START_MISSION":
            return self._start_mission()
        if command == "ABORT":
            return self._abort_mission()
        if command == "STATUS_REQUEST":
            status = self.get_status_snapshot()
            self._send_status()
            return {"ok": True, "message": "Status sent", "status": status}
        if command == "START_RECORDING":
            return self._start_recording_local()
        if command == "STOP_RECORDING":
            return self._stop_recording_local()
        return {"ok": False, "message": f"Unsupported command: {command}"}

    def _run_mapping(self) -> Dict[str, Any]:
        self._set_state(mission_state="MAPPING", mapping_progress=0.0, last_error="")
        self.telemetry_server.send_log("Mapping started.")

        map_path = self.mapper.run_mapping(progress_callback=self._mapping_progress_update)

        self._set_state(
            mission_state="MAPPED",
            mapping_progress=100.0,
            last_map_path=str(map_path),
        )
        self._send_status()
        self.telemetry_server.send_log(f"Mapping complete: {map_path}")
        return {"ok": True, "message": f"Map generated at {map_path}", "map_path": str(map_path)}

    def _run_detection(self) -> Dict[str, Any]:
        snapshot = self.get_status_snapshot()
        if not snapshot["last_map_path"]:
            raise RuntimeError("Detection requires an existing map. Run 'map' first.")

        map_path = Path(snapshot["last_map_path"])
        self._set_state(mission_state="DETECTING", last_error="")
        self.telemetry_server.send_log("Detection started.")

        targets, annotated_path, json_path = self.detector.detect(map_path)
        self._targets = targets

        self._set_state(
            mission_state="DETECTION_DONE",
            detected_targets_count=len(targets),
            last_detection_path=str(annotated_path),
        )
        self._send_status()
        self.telemetry_server.send_log(f"Detection complete: {len(targets)} targets")
        return {
            "ok": True,
            "message": f"Detection complete. targets={len(targets)}",
            "targets_json": str(json_path),
            "annotated_map": str(annotated_path),
            "count": len(targets),
        }

    def _run_planning(self) -> Dict[str, Any]:
        if not self._targets:
            raise RuntimeError("Route planning requires detections. Run 'detect' first.")

        self._set_state(mission_state="PLANNING", last_error="")
        self.telemetry_server.send_log("Route planning started.")

        target_points = [(float(t["relative_x_m"]), float(t["relative_y_m"])) for t in self._targets]
        solution = self.tsp_solver.solve(target_points, start_xy=(0.0, 0.0), include_return_to_start=False)
        ordered_targets = [self._targets[idx] for idx in solution.order]

        takeoff = self._resolve_takeoff_gps()
        waypoints = []
        for index, target in enumerate(ordered_targets):
            latitude, longitude = self.transformer.relative_to_gps(
                takeoff["latitude"],
                takeoff["longitude"],
                float(target["relative_x_m"]),
                float(target["relative_y_m"]),
            )
            waypoints.append(
                {
                    "index": index,
                    "target_id": target["id"],
                    "latitude": round(latitude, 8),
                    "longitude": round(longitude, 8),
                    "relative_offset": {
                        "x_m": float(target["relative_x_m"]),
                        "y_m": float(target["relative_y_m"]),
                    },
                    "altitude_m": float(self.config.mission.default_altitude_m),
                    "hover_time": float(self.config.mission.hover_time_sec),
                }
            )

        self._ordered_waypoints = waypoints
        route_payload = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "start_position": {
                "latitude": takeoff["latitude"],
                "longitude": takeoff["longitude"],
            },
            "total_targets": len(ordered_targets),
            "distance_m": round(solution.distance_m, 3),
            "waypoints": waypoints,
        }
        route_path = self.config.paths.routes_dir / "mission_route.json"
        with route_path.open("w", encoding="utf-8") as handle:
            json.dump(route_payload, handle, indent=2)

        self._set_state(
            mission_state="ROUTE_READY",
            last_route_path=str(route_path),
            takeoff_latitude=float(takeoff["latitude"]),
            takeoff_longitude=float(takeoff["longitude"]),
            connection_status="CONNECTED",
        )
        self._send_status()
        self.telemetry_server.send_log(f"Route ready: {route_path}")
        return {
            "ok": True,
            "message": f"Route planned with {len(waypoints)} waypoints",
            "route_path": str(route_path),
            "distance_m": solution.distance_m,
        }

    def _start_mission(self) -> Dict[str, Any]:
        if not self._ordered_waypoints:
            raise RuntimeError("Mission route not available. Run 'plan' first.")

        self._abort_event.clear()
        self._set_state(mission_state="MISSION_RUNNING", last_error="")
        self.telemetry_server.send_log("Mission started.")

        mav = MavlinkController(
            connection_string=self.config.mission.mavlink_connection,
            baudrate=self.config.mission.mavlink_baudrate,
            logger=self.logger,
        )
        executor = MissionExecutor(
            controller=mav,
            logger=self.logger,
            max_duration_sec=self.config.mission.max_mission_duration_sec,
        )

        try:
            result = executor.execute(
                self._ordered_waypoints,
                abort_checker=lambda: self._abort_event.is_set(),
                telemetry_callback=self._send_live_telemetry,
            )
            self._set_state(mission_state="MISSION_COMPLETE", connection_status="CONNECTED")
            self.telemetry_server.send_log("Mission completed.")
            self._send_status()
            return {"ok": True, "message": "Mission completed", "result": result}
        finally:
            try:
                mav.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Recording (runs locally on the drone / Jetson Nano)
    # ------------------------------------------------------------------

    def _start_recording_local(self) -> Dict[str, Any]:
        """Start capturing from the onboard camera and save locally."""
        if self._rec_thread and self._rec_thread.is_alive():
            return {"ok": False, "message": "Recording already in progress."}

        source = self.config.camera.stream_url.strip() or self.config.camera.device_id
        output_dir = self.config.paths.data_dir / "recordings"

        self._rec_stop_event.clear()
        self._recorder = DroneRecorder(
            source=source,
            fps=30,
            output_dir=str(output_dir),
            auto_extract=True,
        )
        try:
            self._recorder.start()
        except RuntimeError as exc:
            self._set_state(last_error=str(exc))
            self._send_status()
            return {"ok": False, "message": f"Could not open camera: {exc}"}

        session_dir = str(self._recorder.session_dir)
        self._set_state(recording_state="RECORDING", last_recording_session=session_dir)
        self.telemetry_server.send_log("Recording started -> {}".format(session_dir))
        self._send_status()

        self._rec_thread = threading.Thread(
            target=self._recording_loop, name="DroneRecorder", daemon=True
        )
        self._rec_thread.start()
        return {"ok": True, "message": f"Recording started. Session: {session_dir}"}

    def _recording_loop(self) -> None:
        """Background thread: pump frames until stop is requested."""
        assert self._recorder is not None
        while not self._rec_stop_event.is_set():
            ok = self._recorder.record_frame()
            if not ok:
                self.telemetry_server.send_log("Camera source exhausted - recording stopped automatically.", level="WARNING")
                break
        video_path = self._recorder.stop()
        session_dir = self._recorder.session_dir
        frames_dir = session_dir / "frames" if session_dir else None
        frame_count = len(list(frames_dir.glob("*.jpg"))) if frames_dir and frames_dir.exists() else 0
        self._set_state(recording_state="IDLE")
        self.telemetry_server.send_log(
            "Recording saved -> {} | {} frames extracted to {}".format(video_path, frame_count, frames_dir)
        )
        self._send_status()

    def _stop_recording_local(self) -> Dict[str, Any]:
        """Signal the recording loop to stop."""
        if not (self._rec_thread and self._rec_thread.is_alive()):
            return {"ok": False, "message": "No recording in progress."}
        self._rec_stop_event.set()
        self._set_state(recording_state="STOPPING")
        self._send_status()
        return {"ok": True, "message": "Stop signal sent. Extracting frames..."}

    def _abort_mission(self) -> Dict[str, Any]:
        self._abort_event.set()
        self._set_state(mission_state="ABORT_REQUESTED")
        self.telemetry_server.send_log("Abort requested by operator.", level="WARNING")
        self._send_status()
        return {"ok": True, "message": "Abort requested."}

    def _mapping_progress_update(self, progress_fraction: float) -> None:
        percentage = max(0.0, min(100.0, progress_fraction * 100.0))
        self._set_state(mapping_progress=round(percentage, 1))
        self._send_status()

    def _resolve_takeoff_gps(self) -> Dict[str, float]:
        with self.state_lock:
            if self.state.takeoff_latitude is not None and self.state.takeoff_longitude is not None:
                return {
                    "latitude": float(self.state.takeoff_latitude),
                    "longitude": float(self.state.takeoff_longitude),
                }

        if self.config.mission.home_latitude is not None and self.config.mission.home_longitude is not None:
            self.logger.info("Using configured home GPS as takeoff reference.")
            return {
                "latitude": float(self.config.mission.home_latitude),
                "longitude": float(self.config.mission.home_longitude),
            }

        self.logger.info("Fetching takeoff GPS from MAVLink.")
        mav = MavlinkController(
            connection_string=self.config.mission.mavlink_connection,
            baudrate=self.config.mission.mavlink_baudrate,
            logger=self.logger,
        )
        try:
            mav.connect()
            gps = mav.get_current_gps(timeout_sec=20)
            return {
                "latitude": float(gps["latitude"]),
                "longitude": float(gps["longitude"]),
            }
        except Exception:
            self._set_state(connection_status="DISCONNECTED")
            raise
        finally:
            try:
                mav.close()
            except Exception:
                pass

    def _send_live_telemetry(self, payload: Dict[str, Any]) -> None:
        self.telemetry_server.send_event("TELEMETRY", payload)

    def _set_state(self, **updates: Any) -> None:
        with self.state_lock:
            for key, value in updates.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)

    def _send_status(self) -> None:
        self.telemetry_server.send_status(self.get_status_snapshot())


