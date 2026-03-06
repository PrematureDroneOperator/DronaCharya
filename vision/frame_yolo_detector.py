from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np

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
        self._backend = ""
        self._target_filter_checked = False
        self._logged_runtime = False
        self._onnx_class_assumption_logged = False

    def detect_frame(self, frame) -> List[Dict[str, Any]]:
        if frame is None:
            return []

        model = self._load_model()
        if self._backend == "ultralytics":
            return self._detect_with_ultralytics(model, frame)
        if self._backend == "opencv_onnx":
            return self._detect_with_onnx(model, frame)
        raise RuntimeError("Unsupported detector backend: {0}".format(self._backend))

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

    def _detect_with_ultralytics(self, model, frame) -> List[Dict[str, Any]]:
        device = 0 if torch is not None and torch.cuda.is_available() else "cpu"
        infer_half = bool(torch is not None and torch.cuda.is_available())

        if not self._logged_runtime:
            self.logger.info("Frame YOLO inference runtime: backend=ultralytics device=%s half=%s", device, infer_half)
            self._logged_runtime = True

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
        self._warn_target_class_missing_once(class_names)

        detections = []  # type: List[Dict[str, Any]]
        for box in result.boxes:
            cls_id = int(box.cls.item())
            cls_name = self._resolve_class_name(cls_id, class_names)
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

    def _detect_with_onnx(self, net, frame) -> List[Dict[str, Any]]:
        input_size = int(self.config.vision.image_size)
        frame_h, frame_w = frame.shape[:2]
        conf_threshold = float(self.config.vision.conf_threshold)

        if not self._logged_runtime:
            self.logger.info("Frame YOLO inference runtime: backend=opencv_onnx device=cpu")
            self._logged_runtime = True

        blob = cv2.dnn.blobFromImage(frame, scalefactor=1.0 / 255.0, size=(input_size, input_size), swapRB=True, crop=False)
        net.setInput(blob)
        outputs = net.forward()

        pred = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
        pred = np.array(pred)
        if pred.ndim == 3:
            pred = pred[0]
        if pred.ndim != 2:
            return []

        if pred.shape[0] <= 256 and pred.shape[0] < pred.shape[1]:
            pred = pred.T
        if pred.shape[1] < 6:
            return []

        scale_x = float(frame_w) / float(input_size)
        scale_y = float(frame_h) / float(input_size)

        boxes = []  # type: List[List[int]]
        scores = []  # type: List[float]
        class_names = []  # type: List[str]

        for row in pred:
            cls_scores = row[4:]
            if cls_scores.size == 0:
                continue

            cls_id = int(np.argmax(cls_scores))
            conf = float(cls_scores[cls_id])
            if conf < conf_threshold:
                continue

            cls_name = self._resolve_onnx_class_name(cls_id)
            if self.config.vision.target_class_name and cls_name != self.config.vision.target_class_name:
                continue

            cx, cy, bw, bh = [float(v) for v in row[:4]]
            x1 = max(0.0, (cx - bw / 2.0) * scale_x)
            y1 = max(0.0, (cy - bh / 2.0) * scale_y)
            x2 = min(float(frame_w - 1), (cx + bw / 2.0) * scale_x)
            y2 = min(float(frame_h - 1), (cy + bh / 2.0) * scale_y)
            if x2 <= x1 or y2 <= y1:
                continue

            box_w = max(1, int(round(x2 - x1)))
            box_h = max(1, int(round(y2 - y1)))
            boxes.append([int(round(x1)), int(round(y1)), box_w, box_h])
            scores.append(conf)
            class_names.append(cls_name)

        if not boxes:
            return []

        indices = cv2.dnn.NMSBoxes(boxes, scores, conf_threshold, 0.45)
        if indices is None or len(indices) == 0:
            return []

        flat_indices = self._flatten_indices(indices)
        detections = []  # type: List[Dict[str, Any]]
        for idx in flat_indices:
            x, y, w, h = boxes[idx]
            x2 = x + w
            y2 = y + h
            center_x = x + (w / 2.0)
            center_y = y + (h / 2.0)
            detections.append(
                {
                    "class_name": class_names[idx],
                    "confidence": round(float(scores[idx]), 4),
                    "pixel_x": round(float(center_x), 2),
                    "pixel_y": round(float(center_y), 2),
                    "bbox_xyxy": [round(float(x), 2), round(float(y), 2), round(float(x2), 2), round(float(y2), 2)],
                }
            )
        return detections

    def _load_model(self):
        if self._model is not None:
            return self._model

        model_path = Path(self.config.vision.model_path)
        if not model_path.is_absolute():
            model_path = self.config.paths.base_dir / model_path

        if not model_path.exists():
            raise FileNotFoundError("YOLO model path not found: {0}".format(model_path))

        if YOLO is not None and model_path.suffix.lower() != ".onnx":
            self._model = YOLO(str(model_path))
            self._backend = "ultralytics"
            self.logger.info("Loaded frame YOLO model from %s (backend=%s)", model_path, self._backend)
            return self._model

        onnx_path = model_path
        if onnx_path.suffix.lower() != ".onnx":
            candidate = onnx_path.with_suffix(".onnx")
            if candidate.exists():
                onnx_path = candidate
                self.logger.info("Ultralytics unavailable; using ONNX sibling model: %s", onnx_path)
            else:
                raise RuntimeError(
                    "Ultralytics import failed: {0}. Provide an ONNX model and set vision.model_path to .onnx".format(
                        ULTRALYTICS_IMPORT_ERROR
                    )
                )

        self._model = cv2.dnn.readNetFromONNX(str(onnx_path))
        self._backend = "opencv_onnx"
        self.logger.info("Loaded frame YOLO model from %s (backend=%s)", onnx_path, self._backend)
        return self._model

    def _resolve_class_name(self, cls_id: int, class_names: Any) -> str:
        if isinstance(class_names, dict):
            return str(class_names.get(cls_id, str(cls_id)))
        if isinstance(class_names, (list, tuple)):
            return str(class_names[cls_id]) if 0 <= cls_id < len(class_names) else str(cls_id)
        return str(cls_id)

    def _resolve_onnx_class_name(self, cls_id: int) -> str:
        target_name = str(self.config.vision.target_class_name or "").strip()
        if target_name:
            # ONNX export through OpenCV DNN does not carry class-name metadata.
            # For single-class custom models, class id 0 corresponds to target_name.
            if cls_id == 0:
                return target_name
            if not self._onnx_class_assumption_logged:
                self.logger.warning(
                    "ONNX fallback has no class names; assuming class id 0 is '%s'. Other class IDs will be numeric.",
                    target_name,
                )
                self._onnx_class_assumption_logged = True
        return str(cls_id)

    def _warn_target_class_missing_once(self, class_names: Any) -> None:
        if self._target_filter_checked:
            return
        self._target_filter_checked = True

        target_class = str(self.config.vision.target_class_name or "").strip()
        if not target_class:
            return

        if isinstance(class_names, dict):
            available_names = set(str(name) for name in class_names.values())
        elif isinstance(class_names, (list, tuple)):
            available_names = set(str(name) for name in class_names)
        else:
            available_names = set()

        if available_names and target_class not in available_names:
            self.logger.warning(
                "Configured target_class_name '%s' not found in model classes: %s",
                target_class,
                sorted(available_names),
            )

    def _flatten_indices(self, indices: Any) -> List[int]:
        if isinstance(indices, np.ndarray):
            return [int(v) for v in indices.flatten().tolist()]

        flattened = []  # type: List[int]
        for item in indices:
            if isinstance(item, np.ndarray):
                flattened.append(int(item.flatten()[0]))
            elif isinstance(item, (list, tuple)):
                flattened.append(int(item[0]))
            else:
                flattened.append(int(item))
        return flattened
