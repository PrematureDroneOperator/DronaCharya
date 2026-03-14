"""mission_session.py — Records the drone's actual GPS flight path during mission execution.

On START_MISSION the controller creates a MissionSessionRecorder, feeds GPS points to
record_point() as they arrive from the MAVLink GLOBAL_POSITION_INT stream, then calls
finalize() when the mission ends. This writes three artefacts to
data/mission_sessions/session-XXXX/:

  flight_log.csv       — latitude, longitude, altitude_m, timestamp_utc per point
  path_graph.png       — plain-white-background plot of the path (same style as TSP graph)
  metadata.json        — session id, waypoints, start/end times, duration
"""

import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLIGHT_LOG_COLUMNS = ["seq", "latitude", "longitude", "altitude_m", "timestamp_utc"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_mission_session_dir(base_dir: Path) -> Path:
    """Return the next session-XXXX directory under base_dir, creating it."""
    existing = []
    if base_dir.exists():
        for entry in base_dir.iterdir():
            if entry.is_dir() and re.fullmatch(r"session-\d{4}", entry.name):
                existing.append(entry.name)
    last_num = max([int(name.split("-")[1]) for name in existing]) if existing else -1
    path = base_dir / "session-{0:04d}".format(last_num + 1)
    path.mkdir(parents=True, exist_ok=True)
    return path


class _GraphProjector:
    """Project (x, y) relative-metre coordinates onto a fixed-size canvas."""

    def __init__(self, points_xy: List[Tuple[float, float]], canvas_px: int, margin_px: int) -> None:
        self.canvas_px = canvas_px
        self.margin_px = margin_px

        if points_xy:
            xs = [p[0] for p in points_xy]
            ys = [p[1] for p in points_xy]
            self.min_x, self.max_x = min(xs), max(xs)
            self.min_y, self.max_y = min(ys), max(ys)
        else:
            self.min_x = self.max_x = 0.0
            self.min_y = self.max_y = 0.0

        self.range_x = self.max_x - self.min_x
        self.range_y = self.max_y - self.min_y
        usable = max(1, canvas_px - 2 * margin_px)
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


# ---------------------------------------------------------------------------
# Haversine helper — flat-earth relative metres from a reference point
# ---------------------------------------------------------------------------

_EARTH_R_M = 6_371_000.0


def _gps_to_relative(ref_lat: float, ref_lon: float, lat: float, lon: float) -> Tuple[float, float]:
    """Convert GPS coordinates to approximate (x_m, y_m) offset from a reference."""
    dlat = lat - ref_lat
    dlon = lon - ref_lon
    x_m = dlon * (_EARTH_R_M * abs(3.141592653589793 / 180.0) * abs(
        0.017453292519943295 * _EARTH_R_M  # cos(ref_lat) approximation
    ))
    # Simpler: just use haversine-ish direct scaling
    import math
    x_m = dlon * _EARTH_R_M * math.cos(math.radians(ref_lat)) * math.pi / 180.0
    y_m = dlat * _EARTH_R_M * math.pi / 180.0
    return x_m, y_m


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MissionSessionRecorder:
    """Records the GPS flight path during a mission and saves it to disk on finalize()."""

    CANVAS_PX = 1200
    MARGIN_PX = 60

    def __init__(self, mission_sessions_dir: Path, waypoints: List[Dict[str, Any]], logger) -> None:
        self._session_dir = _next_mission_session_dir(mission_sessions_dir)
        self._waypoints = waypoints
        self.logger = logger

        self._points: List[Dict[str, Any]] = []   # accumulated GPS rows
        self._start_time = time.time()
        self._start_iso = _utc_now_iso()
        self._ref_lat: Optional[float] = None
        self._ref_lon: Optional[float] = None

        self.logger.info("MissionSessionRecorder: session=%s", self._session_dir)

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def record_point(self, latitude: float, longitude: float, altitude_m: float, timestamp_utc: Optional[str] = None) -> None:
        """Append one GPS observation. Thread-safe (GIL is sufficient for simple appends)."""
        seq = len(self._points)
        # Use first point as reference origin for the graph
        if self._ref_lat is None:
            self._ref_lat = latitude
            self._ref_lon = longitude

        self._points.append(
            {
                "seq": seq,
                "latitude": round(latitude, 8),
                "longitude": round(longitude, 8),
                "altitude_m": round(altitude_m, 3),
                "timestamp_utc": timestamp_utc or _utc_now_iso(),
            }
        )

    def finalize(self) -> Path:
        """Write flight_log.csv, path_graph.png, and metadata.json.  Returns session dir."""
        end_iso = _utc_now_iso()
        duration_sec = round(time.time() - self._start_time, 2)

        self._write_flight_log()
        self._write_path_graph()
        self._write_metadata(end_iso, duration_sec)

        self.logger.info(
            "MissionSessionRecorder: finalized %d points -> %s",
            len(self._points),
            self._session_dir,
        )
        return self._session_dir

    # ------------------------------------------------------------------
    # Private write helpers
    # ------------------------------------------------------------------

    def _write_flight_log(self) -> None:
        csv_path = self._session_dir / "flight_log.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FLIGHT_LOG_COLUMNS)
            writer.writeheader()
            for row in self._points:
                writer.writerow({k: row.get(k, "") for k in FLIGHT_LOG_COLUMNS})
        self.logger.info("MissionSessionRecorder: wrote %s", csv_path)

    def _write_path_graph(self) -> None:
        graph_path = self._session_dir / "path_graph.png"
        canvas = self.CANVAS_PX
        margin = self.MARGIN_PX
        image = np.full((canvas, canvas, 3), 255, dtype=np.uint8)

        ref_lat = self._ref_lat or 0.0
        ref_lon = self._ref_lon or 0.0

        # Convert all recorded GPS points to relative XY metres
        points_xy: List[Tuple[float, float]] = []
        for row in self._points:
            xy = _gps_to_relative(ref_lat, ref_lon, float(row["latitude"]), float(row["longitude"]))
            points_xy.append(xy)

        # Also include waypoints as reference anchors so the graph is nicely scaled
        waypoint_xy: List[Tuple[float, float]] = []
        for wp in self._waypoints:
            if "latitude" in wp and "longitude" in wp:
                xy = _gps_to_relative(ref_lat, ref_lon, float(wp["latitude"]), float(wp["longitude"]))
                waypoint_xy.append(xy)

        all_xy = points_xy + waypoint_xy + [(0.0, 0.0)]
        projector = _GraphProjector(all_xy, canvas, margin)

        # --- Draw planned waypoints (light grey X markers) ---
        for wp_xy in waypoint_xy:
            px = projector.project(wp_xy)
            size = 6
            cv2.line(image, (px[0] - size, px[1] - size), (px[0] + size, px[1] + size), (180, 180, 180), 2)
            cv2.line(image, (px[0] + size, px[1] - size), (px[0] - size, px[1] + size), (180, 180, 180), 2)

        # --- Draw actual flight path as connected line ---
        if len(points_xy) >= 2:
            pixels = [projector.project(p) for p in points_xy]
            for i in range(1, len(pixels)):
                cv2.line(image, pixels[i - 1], pixels[i], (0, 170, 0), 2)

        # --- Draw each GPS sample point ---
        pixels_all = [projector.project(p) for p in points_xy]
        for i, px in enumerate(pixels_all):
            cv2.circle(image, px, 3, (0, 100, 220), -1)

        # --- Draw START marker ---
        start_px = projector.project((0.0, 0.0))
        cv2.circle(image, start_px, 8, (255, 80, 0), -1)
        cv2.putText(image, "START", (start_px[0] + 10, start_px[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 60), 1)

        # --- Legend / title ---
        cv2.putText(image, f"Mission Flight Path  ({len(self._points)} GPS samples)",
                    (margin, margin - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 2)
        cv2.putText(image, "Blue = actual path   Grey X = planned waypoints",
                    (margin, margin), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 100, 100), 1)

        if not points_xy:
            cv2.putText(image, "No GPS telemetry recorded", (canvas // 4, canvas // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 80), 2)

        cv2.imwrite(str(graph_path), image)
        self.logger.info("MissionSessionRecorder: wrote %s", graph_path)

    def _write_metadata(self, end_iso: str, duration_sec: float) -> None:
        meta_path = self._session_dir / "metadata.json"
        meta = {
            "session_id": self._session_dir.name,
            "mission_started_utc": self._start_iso,
            "mission_ended_utc": end_iso,
            "duration_sec": duration_sec,
            "gps_samples": len(self._points),
            "waypoints": self._waypoints,
            "artifacts": {
                "flight_log_csv": str(self._session_dir / "flight_log.csv"),
                "path_graph_png": str(self._session_dir / "path_graph.png"),
            },
        }
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        self.logger.info("MissionSessionRecorder: wrote %s", meta_path)
