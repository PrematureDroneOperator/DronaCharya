import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2

from planning.coordinate_transform import CoordinateTransformer
from utils.config import AppConfig
from vision.frame_yolo_detector import FrameYoloDetector


class YoloTargetDetector:
    def __init__(self, config: AppConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.transformer = CoordinateTransformer(config.mapping.meters_per_pixel)
        self.frame_detector = FrameYoloDetector(config, logger)

    def detect(self, flatmap_path: Path) -> Tuple[List[Dict[str, Any]], Path, Path]:
        if not flatmap_path.exists():
            raise FileNotFoundError("Map file not found: {0}".format(flatmap_path))

        image = cv2.imread(str(flatmap_path))
        if image is None:
            raise RuntimeError("Unable to read image from {0}".format(flatmap_path))

        height, width = image.shape[:2]
        detections = self.frame_detector.detect_frame(image)

        targets = []  # type: List[Dict[str, Any]]
        for idx, detection in enumerate(detections):
            center_x = float(detection["pixel_x"])
            center_y = float(detection["pixel_y"])
            rel_x_m, rel_y_m = self.transformer.pixel_to_relative(center_x, center_y, width, height)

            target = {
                "id": idx,
                "class_name": str(detection["class_name"]),
                "confidence": round(float(detection["confidence"]), 4),
                "pixel_x": round(center_x, 2),
                "pixel_y": round(center_y, 2),
                "bbox_xyxy": [round(float(v), 2) for v in detection["bbox_xyxy"]],
                "relative_x_m": round(rel_x_m, 3),
                "relative_y_m": round(rel_y_m, 3),
            }
            targets.append(target)

        annotated = self.frame_detector.annotate_frame(image, detections)
        if annotated is None:
            annotated = image

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        annotated_path = self.config.paths.detections_dir / "annotated_{0}.jpg".format(timestamp)
        json_path = self.config.paths.detections_dir / "targets.json"

        cv2.imwrite(str(annotated_path), annotated)
        payload = {
            "timestamp_utc": timestamp,
            "map_path": str(flatmap_path),
            "annotated_path": str(annotated_path),
            "count": len(targets),
            "targets": targets,
        }

        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

        self.logger.info("Detected %s targets. Output: %s", len(targets), json_path)
        return targets, annotated_path, json_path
