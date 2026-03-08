import time
from typing import Any, Dict, List, Set, Union

from pymavlink import mavutil


class MavlinkController:
    def __init__(self, connection_string: str, baudrate: int, logger) -> None:
        self.connection_string = connection_string
        self.baudrate = baudrate
        self.logger = logger
        self.master: Any = None

    def connect(self, timeout_sec: int = 15, abort_checker: Any = None) -> None:
        if self.master is not None:
            return

        self.logger.info("Connecting MAVLink on %s", self.connection_string)
        self.master = mavutil.mavlink_connection(self.connection_string, baud=self.baudrate)
        
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            if abort_checker and abort_checker():
                self.master.close()
                self.master = None
                raise RuntimeError("MAVLink connection aborted.")
            heartbeat = self.master.wait_heartbeat(timeout=1.0)
            if heartbeat is not None:
                self.logger.info(
                    "MAVLink connected: system=%s component=%s", self.master.target_system, self.master.target_component
                )
                return
                
        self.master.close()
        self.master = None
        raise RuntimeError("MAVLink heartbeat timeout.")

    def close(self) -> None:
        if self.master is not None:
            try:
                self.master.close()
            finally:
                self.master = None

    def recv_match(self, types: Union[List[str], str], timeout: float = 1.0):
        self._require_connection()
        return self.master.recv_match(type=types, blocking=True, timeout=timeout)

    def get_current_gps(self, timeout_sec: int = 20) -> Dict[str, float]:
        self._require_connection()
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            msg = self.master.recv_match(type="GPS_RAW_INT", blocking=True, timeout=1.0)
            if not msg:
                continue
            if int(msg.fix_type) >= 3:
                return {
                    "latitude": float(msg.lat) / 1e7,
                    "longitude": float(msg.lon) / 1e7,
                    "altitude_m": float(msg.alt) / 1000.0,
                }
        raise RuntimeError("GPS lock unavailable (fix_type < 3).")

    def arm(self) -> None:
        self._require_connection()
        self.logger.info("Arming vehicle.")
        self.master.mav.command_long_send(
            self.master.target_system,
            self.master.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def set_mode(self, mode: str) -> None:
        self._require_connection()
        mapping = self.master.mode_mapping()
        if mode not in mapping:
            raise RuntimeError(f"Mode '{mode}' not supported by autopilot.")
        self.master.set_mode(mapping[mode])
        self.logger.info("Set flight mode to %s", mode)

    def upload_mission(self, waypoints: List[Dict[str, float]]) -> None:
        self._require_connection()
        if not waypoints:
            raise ValueError("No waypoints provided.")

        self.logger.info("Uploading mission with %s waypoints.", len(waypoints))
        self.master.waypoint_clear_all_send()
        time.sleep(0.5)
        self.master.waypoint_count_send(len(waypoints))

        served = set()  # type: Set[int]
        upload_deadline = time.time() + max(30, len(waypoints) * 8)

        while len(served) < len(waypoints) and time.time() < upload_deadline:
            request = self.master.recv_match(
                type=["MISSION_REQUEST_INT", "MISSION_REQUEST"],
                blocking=True,
                timeout=5.0,
            )
            if request is None:
                continue
            seq = int(request.seq)
            if seq in served or seq >= len(waypoints):
                continue

            waypoint = waypoints[seq]
            self.master.mav.mission_item_int_send(
                self.master.target_system,
                self.master.target_component,
                seq,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                0,
                1 if seq == len(waypoints) - 1 else 0,
                float(waypoint.get("hover_time", 5)),
                0,
                0,
                0,
                int(float(waypoint["latitude"]) * 1e7),
                int(float(waypoint["longitude"]) * 1e7),
                float(waypoint["altitude_m"]),
            )
            served.add(seq)

        if len(served) != len(waypoints):
            raise RuntimeError("Mission upload incomplete due to MAVLink timeout.")

        ack = self.master.recv_match(type="MISSION_ACK", blocking=True, timeout=10.0)
        if ack is None or int(ack.type) != mavutil.mavlink.MAV_MISSION_ACCEPTED:
            ack_type = None if ack is None else int(ack.type)
            raise RuntimeError(f"Mission rejected by autopilot (ack={ack_type}).")
        self.logger.info("Mission upload successful.")

    def start_mission(self) -> None:
        self._require_connection()
        self.master.mav.command_long_send(
            self.master.target_system,
            self.master.target_component,
            mavutil.mavlink.MAV_CMD_MISSION_START,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        self.logger.info("Mission start command sent.")

    def abort_mission(self) -> None:
        self._require_connection()
        for fallback_mode in ("LOITER", "BRAKE", "POSHOLD", "HOLD"):
            try:
                self.set_mode(fallback_mode)
                self.logger.warning("Mission aborted. Switched to %s mode.", fallback_mode)
                return
            except Exception:
                continue
        raise RuntimeError("Mission abort failed. No fallback mode available.")

    def _require_connection(self) -> None:
        if self.master is None:
            raise RuntimeError("MAVLink controller is not connected.")
