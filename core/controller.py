import queue
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from navigation.mavlink_controller import MavlinkController
from navigation.mission_executor import MissionExecutor
from navigation.mission_session import MissionSessionRecorder
from survey.session_manager import SurveySessionManager
from telemetry.telemetry_server import TelemetryServer
from utils.config import AppConfig
from utils.logger import RingBufferLogHandler
from vision.recorder import DroneRecorder
import subprocess
import os
import sys


@dataclass
class AppState:
    connection_status: str = "UNKNOWN"
    current_mode: str = "CLI"
    mapping_progress: float = 0.0
    detected_targets_count: int = 0
    mission_state: str = "IDLE"
    recording_state: str = "IDLE"  # IDLE | RECORDING | STOPPING
    survey_state: str = "IDLE"  # IDLE | RUNNING | STOPPING
    last_recording_session: str = ""
    last_target_session: str = ""
    last_map_path: str = ""
    last_detection_path: str = ""
    last_route_path: str = ""
    last_raw_graph_path: str = ""
    last_tsp_graph_path: str = ""
    raw_detection_count: int = 0
    unique_target_count: int = 0
    last_error: str = ""
    detector_service_endpoint: str = ""
    detector_service_online: bool = False
    detector_error_count: int = 0
    detector_disconnect_count: int = 0
    inference_dropped_count: int = 0
    detector_hit_count: int = 0
    partial_detection_finalized: bool = False
    detector_last_error: str = ""
    takeoff_latitude: Optional[float] = None
    takeoff_longitude: Optional[float] = None
    last_mission_session: str = ""


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
        self.survey_manager = SurveySessionManager(
            config=config,
            logger=logger,
            telemetry_log=lambda message: self.telemetry_server.send_log(message),
        )

        self._ordered_waypoints = []  # type: List[Dict[str, Any]]
        self._abort_event = threading.Event()

        # Standalone recording (video-only mode)
        self._recorder = None  # type: Optional[DroneRecorder]
        self._rec_thread = None  # type: Optional[threading.Thread]
        self._rec_stop_event = threading.Event()

        # Mission session (GPS path recording)
        self._mission_session = None  # type: Optional[MissionSessionRecorder]

        self._command_queue = queue.Queue()  # type: queue.Queue
        self._worker_stop = threading.Event()
        self._worker_thread = threading.Thread(target=self._command_worker, name="CommandWorker", daemon=True)
        
        # Test GPS Tracker state
        self._gps_test_process = None

    def start(self, mode: str) -> None:
        normalized_mode = mode.upper()
        self._set_state(current_mode=normalized_mode, mission_state="READY", last_error="")
        self._sync_detector_status()
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
        if self._rec_thread and self._rec_thread.is_alive():
            self._rec_stop_event.set()
            self._rec_thread.join(timeout=5.0)
        if self.survey_manager.is_running:
            try:
                self.survey_manager.stop_survey()
            except Exception:
                pass
        self._stop_gps_test()
        self.telemetry_server.stop()
        self.logger.info("dronAcharya stopped.")

    def submit_command(
        self, command: str, source: str = "local", wait: bool = True, timeout: float = 3600.0
    ) -> Dict[str, Any]:
        norm_cmd = self._normalize_command(command)
        if norm_cmd == "ABORT":
            self.logger.warning("Priority interrupt: %s from %s", norm_cmd, source)
            return self._abort_mission()

        event = threading.Event() if wait else None
        request = CommandRequest(command=command, source=source, wait_event=event)
        self._command_queue.put(request)

        if wait and event:
            completed = event.wait(timeout=timeout)
            if not completed:
                return {"ok": False, "message": "Command '{0}' timed out after {1:.0f}s".format(command, timeout)}
            return request.response
        return {"ok": True, "message": "Command '{0}' queued".format(command)}

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
        source = "gcs:{0}:{1}".format(source_addr[0], source_addr[1])
        self.submit_command(command=command, source=source, wait=False)

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
                self.telemetry_server.send_log("{0} failed: {1}".format(command, exc), level="ERROR")
                result = {"ok": False, "message": str(exc)}

            request.response.update(result)
            if request.wait_event:
                request.wait_event.set()

    def _normalize_command(self, command: str) -> str:
        canonical = command.strip().upper()
        aliases = {
            "MAP": "START_SURVEY",
            "DETECT": "BUILD_ROUTE",
            "PLAN": "BUILD_ROUTE",
            "STATUS": "STATUS_REQUEST",
            "START_MAPPING": "START_SURVEY",
            "RUN_DETECTION": "BUILD_ROUTE",
            "PLAN_ROUTE": "BUILD_ROUTE",
        }
        return aliases.get(canonical, canonical)

    def _execute_command(self, command: str, source: str) -> Dict[str, Any]:
        self.logger.info("Executing command '%s' (source=%s)", command, source)

        if command == "START_SURVEY":
            return self._start_survey()
        if command == "STOP_SURVEY":
            return self._stop_survey()
        if command == "BUILD_ROUTE":
            return self._build_route()
        if command == "BUILD_ROUTE_RAW":
            return self._build_route_raw()
        if command == "START_RECORDING":
            return self._start_recording_local()
        if command == "STOP_RECORDING":
            return self._stop_recording_local()
        if command == "START_MISSION":
            return self._start_mission()
        if command == "BUILD_MISSION":
            return self._build_mission()
        if command == "ABORT":
            return self._abort_mission()
        if command == "START_GPS_TEST":
            return self._start_gps_test()
        if command == "STOP_GPS_TEST":
            return self._stop_gps_test()
        if command == "STATUS_REQUEST":
            self._sync_detector_status()
            status = self.get_status_snapshot()
            self._send_status()
            return {"ok": True, "message": "Status sent", "status": status}
        return {"ok": False, "message": "Unsupported command: {0}".format(command)}

    def _build_mission(self) -> Dict[str, Any]:
        try:
            restored = self._load_selected_route()
            waypoints = restored.get("waypoints", [])
            if not waypoints:
                return {"ok": False, "message": "No waypoints in latest route."}

            route = restored.get("route", {})
            flight_speed = float(self.config.mission.flight_speed_m_s)
            altitude = self._mission_takeoff_altitude(waypoints)
            target_count = int(route.get("total_targets", len(waypoints)))

            lines = [
                "==================================",
                f"  MISSION BUILD PREVIEW ({target_count} targets)",
                "==================================",
                f"0: TAKEOFF to {altitude}m (Relative)",
                f"1: CHANGE_SPEED to {flight_speed} m/s"
            ]

            seq = 2
            for wp in waypoints:
                lat = float(wp.get("latitude", 0.0))
                lon = float(wp.get("longitude", 0.0))
                hover = float(wp.get("hover_time", self.config.mission.hover_time_sec))
                waypoint_altitude = float(wp.get("altitude_m", altitude))
                lines.append(f"{seq}: WAYPOINT -> Lat: {lat:.6f}, Lon: {lon:.6f} | Alt: {waypoint_altitude}m")
                seq += 1
                lines.append(f"{seq}: HOVER -> wait {hover} sec")
                seq += 1

            lines.append(f"{seq}: RETURN TO LAUNCH (RTL) & LAND")
            lines.append("==================================")

            mission_transcript = "\n".join(lines)
            self.telemetry_server.send_log(f"\n{mission_transcript}")

            return {"ok": True, "message": "Mission built and sent to logs."}
        except Exception as exc:
            self.logger.error("Failed to build mission: %s", exc)
            return {"ok": False, "message": f"Failed to build mission preview: {exc}"}

    def _selected_route_path(self) -> Optional[Path]:
        with self.state_lock:
            raw_path = str(self.state.last_route_path or "").strip()
        if not raw_path:
            return None
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.config.paths.base_dir / candidate
        return candidate if candidate.exists() else None

    def _prepare_mission_waypoints(self, waypoints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        altitude = float(self.config.mission.default_altitude_m)
        hover_time = float(self.config.mission.hover_time_sec)

        prepared = []  # type: List[Dict[str, Any]]
        for index, waypoint in enumerate(waypoints):
            item = dict(waypoint)
            item["index"] = index
            item["altitude_m"] = altitude
            item["hover_time"] = hover_time
            prepared.append(item)
        return prepared

    def _mission_takeoff_altitude(self, waypoints: List[Dict[str, Any]]) -> float:
        if waypoints:
            return float(waypoints[0].get("altitude_m", self.config.mission.default_altitude_m))
        return float(self.config.mission.default_altitude_m)

    def _load_selected_route(self) -> Dict[str, Any]:
        route_path = self._selected_route_path()
        if route_path is not None:
            restored = self.survey_manager.load_route(route_path)
        else:
            restored = self.survey_manager.load_latest_route()

        raw_waypoints = restored.get("waypoints", [])
        if not raw_waypoints:
            raise RuntimeError("Latest session route has no waypoints.")

        prepared_waypoints = self._prepare_mission_waypoints(raw_waypoints)
        route = dict(restored.get("route", {}))
        route["waypoints"] = prepared_waypoints

        start_position = route.get("start_position", {})
        self._ordered_waypoints = prepared_waypoints
        self._set_state(
            last_target_session=restored.get("session_dir", ""),
            last_route_path=restored.get("route_path", ""),
            takeoff_latitude=float(start_position.get("latitude", 0.0)) if start_position else None,
            takeoff_longitude=float(start_position.get("longitude", 0.0)) if start_position else None,
        )

        return {
            **restored,
            "route": route,
            "waypoints": prepared_waypoints,
        }


    def _start_survey(self) -> Dict[str, Any]:
        if self._rec_thread and self._rec_thread.is_alive():
            return {"ok": False, "message": "Standalone recording is running. Stop recording before survey."}
        self.telemetry_server.send_log("Survey start requested.")
        try:
            result = self.survey_manager.start_survey()
        except Exception:
            self._set_state(mission_state="READY", survey_state="IDLE", recording_state="IDLE")
            self._sync_detector_status()
            raise
        self._sync_detector_status()
        self._set_state(
            mission_state="SURVEY_RUNNING",
            survey_state="RUNNING",
            recording_state="RECORDING",
            last_error="",
            partial_detection_finalized=False,
        )
        self._set_state(
            last_target_session=result.get("session_dir", ""),
            last_recording_session=result.get("recording_session", ""),
        )
        self._send_status()
        return result

    def _stop_survey(self) -> Dict[str, Any]:
        self._set_state(survey_state="STOPPING", recording_state="STOPPING", mission_state="SURVEY_STOPPING", last_error="")
        self.telemetry_server.send_log("Survey stop requested.")

        result = self.survey_manager.stop_survey()
        if not result.get("ok"):
            self._set_state(survey_state="IDLE", recording_state="IDLE")
            self._sync_detector_status()
            self._send_status()
            return result

        route = result.get("route", {})
        self._ordered_waypoints = route.get("waypoints", [])
        start_position = route.get("start_position", {})
        start_lat = float(start_position.get("latitude", 0.0)) if start_position else None
        start_lon = float(start_position.get("longitude", 0.0)) if start_position else None

        self._set_state(
            mission_state="ROUTE_READY",
            survey_state="IDLE",
            recording_state="IDLE",
            last_target_session=result.get("session_dir", ""),
            last_recording_session=result.get("recording_session", ""),
            raw_detection_count=int(result.get("raw_count", 0)),
            unique_target_count=int(result.get("unique_count", 0)),
            detected_targets_count=int(result.get("unique_count", 0)),
            inference_dropped_count=int(result.get("inference_dropped_count", 0)),
            detector_hit_count=int(result.get("detector_hit_count", 0)),
            detector_service_online=bool(result.get("detector_online", False)),
            detector_error_count=int(result.get("detector_error_count", 0)),
            detector_disconnect_count=int(result.get("detector_disconnect_count", 0)),
            detector_last_error=str(result.get("detector_last_error", "")),
            partial_detection_finalized=bool(result.get("partial_detection_finalized", False)),
            last_route_path=result.get("route_path", ""),
            last_raw_graph_path=result.get("raw_graph", ""),
            last_tsp_graph_path=result.get("tsp_graph", ""),
            takeoff_latitude=start_lat,
            takeoff_longitude=start_lon,
        )
        self._sync_detector_status()
        self._send_status()
        self.telemetry_server.send_log(
            "Survey complete: raw={0}, unique={1}, route={2}".format(
                result.get("raw_count", 0),
                result.get("unique_count", 0),
                result.get("route_path", ""),
            )
        )
        if bool(result.get("partial_detection_finalized", False)):
            self.telemetry_server.send_log(
                "Survey finalized with partial detections due to detector service disconnect.",
                level="WARNING",
            )
        return result

    def _build_route(self) -> Dict[str, Any]:
        self._set_state(mission_state="BUILDING_ROUTE", last_error="")
        self.telemetry_server.send_log("Route build requested.")

        result = self.survey_manager.build_route()
        route = result.get("route", {})
        self._ordered_waypoints = route.get("waypoints", [])
        start_position = route.get("start_position", {})

        self._set_state(
            mission_state="ROUTE_READY",
            survey_state="IDLE",
            detected_targets_count=int(result.get("unique_count", 0)),
            raw_detection_count=int(result.get("raw_count", 0)),
            unique_target_count=int(result.get("unique_count", 0)),
            inference_dropped_count=int(result.get("inference_dropped_count", 0)),
            detector_hit_count=int(result.get("detector_hit_count", 0)),
            detector_service_online=bool(result.get("detector_online", False)),
            detector_error_count=int(result.get("detector_error_count", 0)),
            detector_disconnect_count=int(result.get("detector_disconnect_count", 0)),
            detector_last_error=str(result.get("detector_last_error", "")),
            partial_detection_finalized=bool(result.get("partial_detection_finalized", False)),
            last_target_session=result.get("session_dir", ""),
            last_route_path=result.get("route_path", ""),
            last_raw_graph_path=result.get("raw_graph", ""),
            last_tsp_graph_path=result.get("tsp_graph", ""),
            takeoff_latitude=float(start_position.get("latitude", 0.0)) if start_position else None,
            takeoff_longitude=float(start_position.get("longitude", 0.0)) if start_position else None,
        )
        self._sync_detector_status()
        self._send_status()
        self.telemetry_server.send_log("Route ready: {0}".format(result.get("route_path", "")))
        return result

    def _build_route_raw(self) -> Dict[str, Any]:
        self._set_state(mission_state="BUILDING_ROUTE", last_error="")
        self.telemetry_server.send_log("Raw-order route build requested (no TSP).")

        result = self.survey_manager.build_route_raw()
        route = result.get("route", {})
        self._ordered_waypoints = route.get("waypoints", [])
        start_position = route.get("start_position", {})

        self._set_state(
            mission_state="ROUTE_READY",
            survey_state="IDLE",
            detected_targets_count=int(result.get("unique_count", 0)),
            raw_detection_count=int(result.get("raw_count", 0)),
            unique_target_count=int(result.get("unique_count", 0)),
            last_target_session=result.get("session_dir", ""),
            last_route_path=result.get("route_path", ""),
            takeoff_latitude=float(start_position.get("latitude", 0.0)) if start_position else None,
            takeoff_longitude=float(start_position.get("longitude", 0.0)) if start_position else None,
        )
        self._sync_detector_status()
        self._send_status()
        self.telemetry_server.send_log(
            "Raw-order route ready ({0} targets): {1}".format(
                result.get("unique_count", 0), result.get("route_path", "")
            )
        )
        return result

    def _start_recording_local(self) -> Dict[str, Any]:
        if self.survey_manager.is_running:
            return {"ok": False, "message": "Survey is running. Stop survey before standalone recording."}
        if self._rec_thread and self._rec_thread.is_alive():
            return {"ok": False, "message": "Recording already in progress."}

        source = self.config.camera.stream_url.strip() or self.config.camera.device_id
        output_dir = self.config.paths.data_dir / "recordings"

        self._rec_stop_event.clear()
        self._recorder = DroneRecorder(
            source=source,
            fps=30,
            output_dir=str(output_dir),
            fourcc=self.config.camera.fourcc,
            container=self.config.camera.container,
            auto_extract=True,
        )
        try:
            self._recorder.start()
        except RuntimeError as exc:
            self._set_state(last_error=str(exc))
            self._send_status()
            return {"ok": False, "message": "Could not open camera: {0}".format(exc)}

        session_dir = str(self._recorder.session_dir)
        self._set_state(recording_state="RECORDING", last_recording_session=session_dir, mission_state="RECORDING_ONLY")
        self.telemetry_server.send_log("Recording started -> {0}".format(session_dir))
        self._send_status()

        self._rec_thread = threading.Thread(target=self._recording_loop, name="DroneRecorder", daemon=True)
        self._rec_thread.start()
        return {"ok": True, "message": "Recording started. Session: {0}".format(session_dir), "session_dir": session_dir}

    def _recording_loop(self) -> None:
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
        self._set_state(recording_state="IDLE", mission_state="READY")
        self.telemetry_server.send_log(
            "Recording saved -> {0} | {1} frames extracted to {2}".format(video_path, frame_count, frames_dir)
        )
        self._send_status()

    def _stop_recording_local(self) -> Dict[str, Any]:
        if not (self._rec_thread and self._rec_thread.is_alive()):
            return {"ok": False, "message": "No recording in progress."}
        self._rec_stop_event.set()
        self._set_state(recording_state="STOPPING")
        self._send_status()
        return {"ok": True, "message": "Stop signal sent. Extracting frames..."}

    def _start_mission(self) -> Dict[str, Any]:
        restored = self._load_selected_route()
        self.telemetry_server.send_log("Loaded mission route -> {0}".format(restored.get("route_path", "")))

        self._abort_event.clear()
        self._set_state(mission_state="MISSION_RUNNING", last_error="")
        self.telemetry_server.send_log("Mission started.")

        # Create the mission session recorder for this run
        self._mission_session = MissionSessionRecorder(
            mission_sessions_dir=self.config.paths.mission_sessions_dir,
            waypoints=list(self._ordered_waypoints),
            logger=self.logger,
        )
        self.telemetry_server.send_log(
            "Mission session recording started -> {0}".format(self._mission_session.session_dir)
        )

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
                flight_speed_m_s=float(self.config.mission.flight_speed_m_s),
                takeoff_alt_m=self._mission_takeoff_altitude(self._ordered_waypoints),
                abort_checker=lambda: self._abort_event.is_set(),
                telemetry_callback=self._on_mission_telemetry,
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
            # Always finalize the session even on abort / error
            if self._mission_session is not None:
                try:
                    session_dir = self._mission_session.finalize()
                    self._set_state(last_mission_session=str(session_dir))
                    self.telemetry_server.send_log(
                        "Mission session saved -> {0}".format(session_dir)
                    )
                except Exception as exc:
                    self.logger.warning("Mission session finalize failed: %s", exc)
                finally:
                    self._mission_session = None

    def _abort_mission(self) -> Dict[str, Any]:
        self._abort_event.set()
        self._set_state(mission_state="ABORT_REQUESTED")
        self.telemetry_server.send_log("Abort requested by operator.", level="WARNING")
        self._send_status()
        return {"ok": True, "message": "Abort requested."}

    def _on_mission_telemetry(self, payload: Dict[str, Any]) -> None:
        """Called once per second with live GPS from the drone during mission execution."""
        # Forward to GCS as usual
        self.telemetry_server.send_event("TELEMETRY", payload)
        # Record the point in the mission session
        if self._mission_session is not None:
            try:
                self._mission_session.record_point(
                    latitude=float(payload.get("latitude", 0.0)),
                    longitude=float(payload.get("longitude", 0.0)),
                    altitude_m=float(payload.get("altitude_m", 0.0)),
                )
            except Exception as exc:
                self.logger.warning("Mission session record_point failed: %s", exc)

    def _send_live_telemetry(self, payload: Dict[str, Any]) -> None:
        self.telemetry_server.send_event("TELEMETRY", payload)

    def _set_state(self, **updates: Any) -> None:
        with self.state_lock:
            for key, value in updates.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)

    def _send_status(self) -> None:
        self._sync_detector_status()
        self.telemetry_server.send_status(self.get_status_snapshot())

    def _sync_detector_status(self) -> None:
        status = self.survey_manager.get_detector_status()
        self._set_state(
            detector_service_endpoint=str(status.get("endpoint", "")),
            detector_service_online=bool(status.get("online", False)),
            detector_error_count=int(status.get("error_count", 0)),
            detector_disconnect_count=int(status.get("disconnect_count", 0)),
            inference_dropped_count=int(status.get("inference_dropped_count", 0)),
            detector_last_error=str(status.get("last_error", "")),
        )

    def _start_gps_test(self) -> Dict[str, Any]:
        if self._gps_test_process is not None and self._gps_test_process.poll() is None:
            return {"ok": False, "message": "GPS logging test is already running."}
            
        script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_gps_logs.py")
        try:
            self._gps_test_process = subprocess.Popen(
                [sys.executable, script_path],
                close_fds=True
            )
            self.telemetry_server.send_log("Started continuous GPS logging test.", level="INFO")
            return {"ok": True, "message": "GPS test started"}
        except Exception as e:
            self.logger.error(f"Failed to start GPS test: {e}")
            return {"ok": False, "message": f"Failed to start GPS test: {e}"}

    def _stop_gps_test(self) -> Dict[str, Any]:
        if self._gps_test_process is None or self._gps_test_process.poll() is not None:
            return {"ok": False, "message": "No GPS logging test is running."}
            
        try:
            self.logger.warning("Force killing GPS test process...")
            self._gps_test_process.kill()
            self._gps_test_process.wait(timeout=2.0)
            self._gps_test_process = None
            self.telemetry_server.send_log("Force killed continuous GPS logging test.", level="WARNING")
            return {"ok": True, "message": "GPS test force stopped"}
        except subprocess.TimeoutExpired:
            self._gps_test_process = None
            self.telemetry_server.send_log("GPS test process kill timed out, dropping reference.", level="ERROR")
            return {"ok": True, "message": "GPS test dropped after kill timeout"}
        except Exception as e:
            self.logger.error(f"Failed to stop GPS test: {e}")
            return {"ok": False, "message": f"Failed to stop GPS test: {e}"}
