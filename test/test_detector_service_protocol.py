import base64
import json
import logging
import types
import unittest

import cv2
import numpy as np

from vision.detector_service import DetectorService


class _FakeDetector(object):
    def detect_frame(self, frame):
        _ = frame
        return [
            {
                "class_name": "target",
                "confidence": 0.88,
                "pixel_x": 50.0,
                "pixel_y": 60.0,
                "bbox_xyxy": [40.0, 45.0, 80.0, 95.0],
            }
        ]


class DetectorServiceProtocolTest(unittest.TestCase):
    def _build_service(self) -> DetectorService:
        service = DetectorService.__new__(DetectorService)
        service.host = "127.0.0.1"
        service.port = 17660
        service.logger = logging.getLogger("detector_service_test")
        service.detector = _FakeDetector()
        service.config = types.SimpleNamespace(
            vision=types.SimpleNamespace(model_path="models/test.pt"),
        )
        return service

    def test_dispatch_invalid_json(self) -> None:
        service = self._build_service()
        response = service._dispatch_line(b"not-json")
        self.assertFalse(bool(response.get("ok")))
        self.assertIn("invalid_json", str(response.get("error", "")))

    def test_infer_invalid_payload(self) -> None:
        service = self._build_service()
        response = service._dispatch_line(b"{\"op\":\"INFER\"}")
        self.assertFalse(bool(response.get("ok")))
        self.assertIn("missing image_jpeg_b64", str(response.get("error", "")))

    def test_infer_valid_payload(self) -> None:
        service = self._build_service()
        frame = np.zeros((24, 24, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        payload = {
            "op": "INFER",
            "frame_idx": 3,
            "frame_ts": "2026-01-01T00:00:00Z",
            "image_jpeg_b64": base64.b64encode(encoded.tobytes()).decode("ascii"),
        }
        response = service._dispatch_line(json.dumps(payload).encode("utf-8"))
        self.assertTrue(bool(response.get("ok")))
        detections = response.get("detections", [])
        self.assertEqual(1, len(detections))
        self.assertEqual("target", detections[0].get("class_name"))


if __name__ == "__main__":
    unittest.main()
