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
        self._target_filter_checked = False
        self._logged_runtime = False

    def detect_frame(self, frame) -> List[Dict[str, Any]]:
        if frame is None:
            return []
        if YOLO is None:
            raise RuntimeError("Ultralytics import failed: {0}".format(ULTRALYTICS_IMPORT_ERROR))

        model = self._load_model()
        device = 0 if torch is not None and torch.cuda.is_available() else "cpu"
        infer_half = bool(torch is not None and torch.cuda.is_available())

        if not self._logged_runtime:
            self.logger.info("Frame YOLO inference runtime: device=%s half=%s", device, infer_half)
            self._logged_runtime = True

        # Defensive copy because the recorder thread reuses frame buffers.
        frame_for_infer = frame.copy()
        try:
            results = model.predict(
                source=frame_for_infer,
                conf=self.config.vision.conf_threshold,
                imgsz=self.config.vision.image_size,
                device=device,
                half=infer_half,
                verbose=False,
            )
        except Exception:
            if device == "cpu":
                raise
            self.logger.warning("Frame YOLO GPU inference failed. Falling back to CPU for this frame.", exc_info=True)
            results = model.predict(
                source=frame_for_infer,
                conf=self.config.vision.conf_threshold,
                imgsz=self.config.vision.image_size,
                device="cpu",
                half=False,
                verbose=False,
            )
        if not results:
            return []

        result = results[0]
        class_names = result.names
        if not self._target_filter_checked:
            self._target_filter_checked = True
            target_class = str(self.config.vision.target_class_name or "").strip()
            if isinstance(class_names, dict):
                available_names = set(class_names.values())
            elif isinstance(class_names, (list, tuple)):
                available_names = set(str(name) for name in class_names)
            else:
                available_names = set()
            if target_class and target_class not in available_names:
                self.logger.warning(
                    "Configured target_class_name '%s' not found in model classes: %s",
                    target_class,
                    sorted(available_names),
                )

        if isinstance(class_names, dict):
            resolve_class_name = lambda cls_id: class_names.get(cls_id, str(cls_id))
        elif isinstance(class_names, (list, tuple)):
            resolve_class_name = lambda cls_id: str(class_names[cls_id]) if 0 <= cls_id < len(class_names) else str(cls_id)
        else:
            resolve_class_name = lambda cls_id: str(cls_id)
        detections = []  # type: List[Dict[str, Any]]

        for box in result.boxes:
            cls_id = int(box.cls.item())
            cls_name = resolve_class_name(cls_id)
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
