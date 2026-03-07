import logging
import unittest
from pathlib import Path

import numpy as np

from survey.session_manager import SurveySessionManager
from utils.config import load_config
from vision.remote_yolo_client import RemoteYoloError


class _FakeDetectorClient(object):
    def __init__(self) -> None:
        self._calls = 0

    def infer(self, frame, frame_idx, frame_ts):
        _ = frame
        _ = frame_idx
        _ = frame_ts
        self._calls += 1
        if self._calls == 1:
            return [
                {
                    "class_name": "targetrotation",
                    "confidence": 0.9,
                    "pixel_x": 160.0,
                    "pixel_y": 120.0,
                    "bbox_xyxy": [120.0, 80.0, 200.0, 160.0],
                }
            ]
        raise RemoteYoloError("detector disconnected")


class SurveyRemoteDetectorTest(unittest.TestCase):
    def _load_test_config(self):
        base_dir = Path(__file__).resolve().parents[1]
        config = load_config(config_path=base_dir / "config" / "config.yaml", base_dir=base_dir)
        config.detector_service.enabled = True
        config.detector_service.host = "127.0.0.1"
        config.detector_service.port = 19999
        config.detector_service.connect_timeout_sec = 0.2
        config.detector_service.request_timeout_sec = 0.2
        return config

    def test_start_survey_fails_fast_when_detector_offline(self) -> None:
        config = self._load_test_config()
        manager = SurveySessionManager(config=config, logger=logging.getLogger("survey_test"))
        with self.assertRaises(RuntimeError):
            manager.start_survey()

    def test_detect_loop_marks_partial_on_disconnect(self) -> None:
        config = self._load_test_config()
        manager = SurveySessionManager(config=config, logger=logging.getLogger("survey_test"))

        manager._detector_client = _FakeDetectorClient()
        manager._detector_online = True
        manager._latest_gps = {
            "latitude": 12.9715987,
            "longitude": 77.5945627,
            "fix_type": 3,
            "timestamp_utc": "2026-01-01T00:00:00Z",
        }

        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        manager._frame_queue.put((1, "2026-01-01T00:00:00Z", frame))
        manager._frame_queue.put((2, "2026-01-01T00:00:01Z", frame))
        manager._detect_stop_event.set()
        manager._detect_loop()

        self.assertEqual(1, len(manager._raw_detections))
        self.assertTrue(bool(manager._partial_detection))
        self.assertEqual(1, int(manager._detector_disconnect_count))
        self.assertFalse(bool(manager._detector_online))


if __name__ == "__main__":
    unittest.main()
