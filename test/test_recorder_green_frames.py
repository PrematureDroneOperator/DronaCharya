import unittest

import numpy as np

from vision.recorder import DroneRecorder


class _FakeCapture(object):
    def __init__(self, frames, opened: bool = True) -> None:
        self._frames = list(frames)
        self._opened = bool(opened)
        self.released = False
        self.set_calls = []

    def isOpened(self) -> bool:
        return self._opened

    def read(self):
        if not self._frames:
            return False, None
        return self._frames.pop(0)

    def release(self) -> None:
        self.released = True

    def set(self, prop, value) -> bool:
        self.set_calls.append((prop, value))
        return True


class _FakeWriter(object):
    def __init__(self) -> None:
        self.frames = []

    def write(self, frame) -> None:
        self.frames.append(frame.copy())


class RecorderGreenFrameTest(unittest.TestCase):
    def test_green_frame_heuristic_flags_corrupt_frame(self) -> None:
        recorder = DroneRecorder(source=0, auto_extract=False)

        green = np.zeros((240, 320, 3), dtype=np.uint8)
        green[:, :, 1] = 255
        self.assertTrue(recorder._is_green_screen_frame(green))

        normal = np.zeros((240, 320, 3), dtype=np.uint8)
        normal[:, :, 0] = 90
        normal[:, :, 1] = 105
        normal[:, :, 2] = 130
        self.assertFalse(recorder._is_green_screen_frame(normal))

    def test_try_open_candidates_skips_green_backend(self) -> None:
        recorder = DroneRecorder(source=0, auto_extract=False)
        green = np.zeros((120, 160, 3), dtype=np.uint8)
        green[:, :, 1] = 255
        good = np.full((120, 160, 3), 120, dtype=np.uint8)

        bad_cap = _FakeCapture([(True, green)] * 6)
        good_cap = _FakeCapture([(True, good)])
        tried = []

        cap, frame = recorder._try_open_candidates(
            source=0,
            candidates=[
                ("bad-green", lambda: bad_cap),
                ("good-bgr", lambda: good_cap),
            ],
            tried=tried,
        )

        self.assertIs(cap, good_cap)
        self.assertIsNotNone(frame)
        self.assertEqual(["bad-green", "good-bgr"], tried)
        self.assertTrue(bad_cap.released)
        self.assertEqual("good-bgr", recorder._active_capture_name)

    def test_record_frame_retries_until_non_green_frame(self) -> None:
        recorder = DroneRecorder(source=0, auto_extract=False)
        recorder._recording = True
        recorder._cap = _FakeCapture(
            [
                (True, np.dstack([np.zeros((32, 32), dtype=np.uint8), np.full((32, 32), 255, dtype=np.uint8), np.zeros((32, 32), dtype=np.uint8)])),
                (True, np.full((32, 32, 3), 80, dtype=np.uint8)),
            ]
        )
        recorder._writer = _FakeWriter()
        recorder._active_capture_name = "test-cap"

        ok, frame, frame_idx, _ = recorder.record_frame(include_frame=True)

        self.assertTrue(ok)
        self.assertEqual(1, frame_idx)
        self.assertIsNotNone(frame)
        self.assertFalse(recorder._is_green_screen_frame(frame))
        self.assertEqual(1, len(recorder._writer.frames))
        self.assertFalse(recorder._is_green_screen_frame(recorder._writer.frames[0]))


if __name__ == "__main__":
    unittest.main()
