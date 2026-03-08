import time
from typing import Callable, Dict, List, Optional

from navigation.mavlink_controller import MavlinkController


class MissionExecutor:
    def __init__(
        self,
        controller: MavlinkController,
        logger,
        max_duration_sec: int = 900,
    ) -> None:
        self.controller = controller
        self.logger = logger
        self.max_duration_sec = max_duration_sec

    def execute(
        self,
        waypoints: List[Dict[str, float]],
        flight_speed_m_s: Optional[float] = None,
        takeoff_alt_m: float = 10.0,
        abort_checker: Optional[Callable[[], bool]] = None,
        telemetry_callback: Optional[Callable[[Dict], None]] = None,
    ) -> Dict[str, float]:
        if not waypoints:
            raise ValueError("No mission waypoints available.")

        self.controller.connect()
        self.controller.get_current_gps(timeout_sec=20)

        total_mission_items = self.controller.upload_mission(
            waypoints, 
            flight_speed_m_s=flight_speed_m_s, 
            takeoff_alt_m=takeoff_alt_m
        )
        self.controller.arm()

        # AUTO mode is expected for mission execution on most autopilots.
        self.controller.set_mode("AUTO")
        self.controller.start_mission()

        reached = -1
        mission_start = time.time()
        last_telemetry_time = 0.0

        while True:
            if abort_checker and abort_checker():
                self.controller.abort_mission()
                raise RuntimeError("Mission aborted by operator.")

            elapsed = time.time() - mission_start
            if elapsed > self.max_duration_sec:
                self.controller.abort_mission()
                raise RuntimeError("Mission timeout exceeded.")

            msg = self.controller.recv_match(
                ["MISSION_ITEM_REACHED", "GLOBAL_POSITION_INT", "STATUSTEXT"],
                timeout=0.1,
            )
            if msg is None:
                continue

            msg_type = msg.get_type()
            if msg_type == "MISSION_ITEM_REACHED":
                reached = max(reached, int(msg.seq))
                self.logger.info("Reached waypoint index %s", reached)
                if reached >= total_mission_items - 1:
                    break
            elif msg_type == "GLOBAL_POSITION_INT" and telemetry_callback:
                if time.time() - last_telemetry_time >= 1.0:
                    last_telemetry_time = time.time()
                    telemetry_callback(
                        {
                            "latitude": float(msg.lat) / 1e7,
                            "longitude": float(msg.lon) / 1e7,
                            "altitude_m": float(msg.relative_alt) / 1000.0,
                        }
                    )
            elif msg_type == "STATUSTEXT":
                self.logger.info("FCU: %s", getattr(msg, "text", ""))

        duration = time.time() - mission_start
        self.logger.info("Mission completed. duration=%.1fs", duration)
        return {"duration_sec": round(duration, 2), "waypoints_reached": int(reached + 1)}
