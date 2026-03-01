import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2

from planning.coordinate_transform import CoordinateTransformer
from utils.config import AppConfig

try:
    import torch
except Exception:  # pragma: no cover
    torch = None

try:
    from ultralytics import YOLO
except Exception as exc:  # pragma: no cover
    YOLO = None
    ULTRALYTICS_IMPORT_ERROR = exc
else:
    ULTRALYTICS_IMPORT_ERROR = None


class YoloTargetDetector:
    def __init__(self, config: AppConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.transformer = CoordinateTransformer(config.mapping.meters_per_pixel)
        self._model: Any = None

    def detect(self, flatmap_path: Path) -> Tuple[List[Dict[str, Any]], Path, Path]:
        if not flatmap_path.exists():
            raise FileNotFoundError(f"Map file not found: {flatmap_path}")
        if YOLO is None:
            raise RuntimeError(f"Ultralytics import failed: {ULTRALYTICS_IMPORT_ERROR}")

        image = cv2.imread(str(flatmap_path))
        if image is None:
            raise RuntimeError(f"Unable to read image from {flatmap_path}")

        height, width = image.shape[:2]
        model = self._load_model()
        device = 0 if torch is not None and torch.cuda.is_available() else "cpu"
        infer_half = bool(torch is not None and torch.cuda.is_available())

        results = model.predict(
            source=str(flatmap_path),
            conf=self.config.vision.conf_threshold,
            imgsz=self.config.vision.image_size,
            device=device,
            half=infer_half,
            verbose=False,
        )
        if not results:
            raise RuntimeError("YOLO inference did not return results.")

        result = results[0]
        class_names = result.names
        targets = []  # type: List[Dict[str, Any]]

        for idx, box in enumerate(result.boxes):
            cls_id = int(box.cls.item())
            cls_name = class_names.get(cls_id, str(cls_id))
            conf = float(box.conf.item())

            x1, y1, x2, y2 = box.xyxy[0].tolist()
            center_x = (x1 + x2) / 2.0
            center_y = (y1 + y2) / 2.0

            rel_x_m, rel_y_m = self.transformer.pixel_to_relative(center_x, center_y, width, height)

            if self.config.vision.target_class_name and cls_name != self.config.vision.target_class_name:
                continue

            target = {
                "id": idx,
                "class_name": cls_name,
                "confidence": round(conf, 4),
                "pixel_x": round(center_x, 2),
                "pixel_y": round(center_y, 2),
                "bbox_xyxy": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                "relative_x_m": round(rel_x_m, 3),
                "relative_y_m": round(rel_y_m, 3),
            }
            targets.append(target)
            self._draw_annotation(image, target)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        annotated_path = self.config.paths.detections_dir / f"annotated_{timestamp}.jpg"
        json_path = self.config.paths.detections_dir / "targets.json"

        cv2.imwrite(str(annotated_path), image)
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

    def _draw_annotation(self, image, target: Dict[str, Any]) -> None:
        x1, y1, x2, y2 = [int(v) for v in target["bbox_xyxy"]]
        center = (int(target["pixel_x"]), int(target["pixel_y"]))
        label = f'{target["class_name"]}:{target["confidence"]:.2f}'
        cv2.rectangle(image, (x1, y1), (x2, y2), (60, 220, 20), 2)
        cv2.circle(image, center, 4, (10, 10, 240), -1)
        cv2.putText(image, label, (x1, max(y1 - 10, 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    def _load_model(self):
        if self._model is None:
            model_path = Path(self.config.vision.model_path)
            if not model_path.is_absolute():
                model_path = self.config.paths.base_dir / model_path
            if not model_path.exists():
                raise FileNotFoundError(f"YOLO model path not found: {model_path}")
            self._model = YOLO(str(model_path))
            self.logger.info("Loaded YOLO model from %s", model_path)
        return self._model
