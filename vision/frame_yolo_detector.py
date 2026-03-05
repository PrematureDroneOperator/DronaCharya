from pathlib import Path
from typing import Any, Dict, List

import cv2

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


class FrameYoloDetector:
    def __init__(self, config: AppConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self._model: Any = None

    def detect_frame(self, frame) -> List[Dict[str, Any]]:
        if frame is None:
            return []
        if YOLO is None:
            raise RuntimeError("Ultralytics import failed: {0}".format(ULTRALYTICS_IMPORT_ERROR))

        model = self._load_model()
        device = 0 if torch is not None and torch.cuda.is_available() else "cpu"
        infer_half = bool(torch is not None and torch.cuda.is_available())

        # Defensive copy because the recorder thread reuses frame buffers.
        frame_for_infer = frame.copy()
        results = model.predict(
            source=frame_for_infer,
            conf=self.config.vision.conf_threshold,
            imgsz=self.config.vision.image_size,
            device=device,
            half=infer_half,
            verbose=False,
        )
        if not results:
            return []

        result = results[0]
        class_names = result.names
        detections = []  # type: List[Dict[str, Any]]

        for box in result.boxes:
            cls_id = int(box.cls.item())
            cls_name = class_names.get(cls_id, str(cls_id))
            if self.config.vision.target_class_name and cls_name != self.config.vision.target_class_name:
                continue

            conf = float(box.conf.item())
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            center_x = (x1 + x2) / 2.0
            center_y = (y1 + y2) / 2.0
            detections.append(
                {
                    "class_name": cls_name,
                    "confidence": round(conf, 4),
                    "pixel_x": round(center_x, 2),
                    "pixel_y": round(center_y, 2),
                    "bbox_xyxy": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                }
            )

        return detections

    def annotate_frame(self, frame, detections: List[Dict[str, Any]]):
        if frame is None:
            return None
        image = frame.copy()
        for detection in detections:
            x1, y1, x2, y2 = [int(v) for v in detection["bbox_xyxy"]]
            label = "{0}:{1:.2f}".format(detection["class_name"], detection["confidence"])
            cv2.rectangle(image, (x1, y1), (x2, y2), (60, 220, 20), 2)
            cv2.circle(image, (int(detection["pixel_x"]), int(detection["pixel_y"])), 4, (10, 10, 240), -1)
            cv2.putText(image, label, (x1, max(y1 - 10, 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        return image

    def _load_model(self):
        if self._model is None:
            model_path = Path(self.config.vision.model_path)
            if not model_path.is_absolute():
                model_path = self.config.paths.base_dir / model_path
            if not model_path.exists():
                raise FileNotFoundError("YOLO model path not found: {0}".format(model_path))
            self._model = YOLO(str(model_path))
            self.logger.info("Loaded frame YOLO model from %s", model_path)
        return self._model
