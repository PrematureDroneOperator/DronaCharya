from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2

from mapping.stitching import TerrainStitcher
from utils.config import AppConfig


class TerrainMapper:
    def __init__(self, config: AppConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.stitcher = TerrainStitcher(max_dimension=config.mapping.max_dimension)

    def run_mapping(self, progress_callback: Callable[[float], None] | None = None) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        session_dir = self.config.paths.maps_dir / f"session_{timestamp}"
        frames_dir = session_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        camera = self._open_camera()
        frames = []
        capture_count = self.config.camera.capture_count

        try:
            for index in range(capture_count):
                ok, frame = camera.read()
                if not ok:
                    self.logger.warning("Frame capture failed at index %s.", index)
                    continue

                frame_path = frames_dir / f"frame_{index:04d}.jpg"
                cv2.imwrite(str(frame_path), frame)
                frames.append(frame)

                progress = (index + 1) / capture_count
                if progress_callback:
                    progress_callback(progress)
                time.sleep(self.config.camera.capture_interval_sec)
        finally:
            camera.release()

        if len(frames) < 2:
            raise RuntimeError("Insufficient frames captured for terrain map generation.")

        self.logger.info("Captured %s frames. Starting stitching.", len(frames))
        stitched = self.stitcher.stitch(frames)
        map_path = self.config.paths.maps_dir / f"flatmap_{timestamp}.jpg"
        cv2.imwrite(str(map_path), stitched)

        metadata = {
            "timestamp_utc": timestamp,
            "capture_count": len(frames),
            "flatmap_path": str(map_path),
            "session_frames_dir": str(frames_dir),
        }
        metadata_path = session_dir / "metadata.json"
        with metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2)

        self.logger.info("Terrain map saved to %s", map_path)
        return map_path

    def _open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.config.camera.device_id)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.camera.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.camera.frame_height)

        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera device {self.config.camera.device_id}.")
        return cap
