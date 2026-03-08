"""vision/recorder.py
Drone camera recorder:
- captures video from an onboard camera source,
- writes recording.mp4 in session-XXXX folders,
- optionally extracts frames after stop.

Usage:
    python -m vision.recorder
    python -m vision.recorder --source 0
    python -m vision.recorder --source rtsp://192.168.1.1/video
"""

import argparse
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Union

import cv2
import numpy as np

from vision.frame_extractor import FrameExtractor

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_session_dir(base_dir: Path) -> Path:
    """Return the next free session-XXXX directory under base_dir."""
    existing = []
    if base_dir.exists():
        for entry in base_dir.iterdir():
            if entry.is_dir() and re.fullmatch(r"session-\d{4}", entry.name):
                existing.append(entry.name)

    if existing:
        last_num = max(int(name.split("-")[1]) for name in existing)
    else:
        last_num = -1

    session_dir = base_dir / "session-{0:04d}".format(last_num + 1)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


class DroneRecorder:
    """Record video from a camera source at fps frames per second."""

    def __init__(
        self,
        source: Union[int, str] = 0,
        fps: int = 30,
        output_dir: Union[str, Path] = "recordings",
        fourcc: str = "MJPG",
        container: str = ".avi",
        auto_extract: bool = True,
    ) -> None:
        try:
            self._source = int(source)  # type: Union[int, str]
        except (ValueError, TypeError):
            self._source = source

        self.fps = fps
        self.output_dir = Path(output_dir)
        # Normalise fourcc (must be exactly 4 chars) and container.
        self.fourcc = fourcc.strip()
        self.container = container.strip() if container.startswith(".") else f".{container.strip()}"
        self.auto_extract = auto_extract

        self._cap = None  # type: Optional[cv2.VideoCapture]
        self._writer = None  # type: Optional[cv2.VideoWriter]
        self._session_dir = None  # type: Optional[Path]
        self._video_path = None  # type: Optional[Path]
        self._recording = False
        self._frame_index = 0
        self._pending_frame = None
        self._active_capture_name = ""

    @property
    def _is_gst_pipeline(self) -> bool:
        """True when source is a GStreamer pipeline string (not a device index)."""
        return isinstance(self._source, str) and not self._source.startswith(("rtsp://", "http://", "https://"))

    def _is_numeric_source(self, source) -> bool:
        return isinstance(source, int) or (isinstance(source, str) and source.isdigit())

    def _capture_candidates_for_numeric(self, dev: int, include_csi: bool) -> List[Tuple[str, object]]:
        candidates = []  # type: List[Tuple[str, object]]
        usb_gst_mjpeg = (
            "v4l2src device=/dev/video{0} ! "
            "image/jpeg,framerate=30/1 ! jpegdec ! "
            "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1"
        ).format(dev)
        candidates.append(("gst-v4l2-usb-mjpeg[{0}]".format(dev), lambda: cv2.VideoCapture(usb_gst_mjpeg, cv2.CAP_GSTREAMER)))

        usb_gst_raw = (
            "v4l2src device=/dev/video{0} ! "
            "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1"
        ).format(dev)
        candidates.append(("gst-v4l2-usb-raw[{0}]".format(dev), lambda: cv2.VideoCapture(usb_gst_raw, cv2.CAP_GSTREAMER)))

        if include_csi:
            csi_gst = (
                "nvarguscamerasrc sensor-id={0} ! "
                "video/x-raw(memory:NVMM), width=1280, height=720, format=NV12, framerate=30/1 ! "
                "nvvidconv ! video/x-raw, format=BGRx ! "
                "videoconvert ! video/x-raw, format=BGR ! appsink drop=true max-buffers=1"
            ).format(dev)
            candidates.append(("gst-csi-nvargus[{0}]".format(dev), lambda: cv2.VideoCapture(csi_gst, cv2.CAP_GSTREAMER)))
        if hasattr(cv2, "CAP_V4L2"):
            candidates.append(("index-v4l2[{0}]".format(dev), lambda: cv2.VideoCapture(dev, cv2.CAP_V4L2)))
        candidates.append(("index-default[{0}]".format(dev), lambda: cv2.VideoCapture(dev)))
        return candidates

    def _is_green_screen_frame(self, frame) -> bool:
        if frame is None or not hasattr(frame, "shape"):
            return True
        if len(frame.shape) != 3 or int(frame.shape[2]) != 3:
            return True
        if int(frame.shape[0]) <= 0 or int(frame.shape[1]) <= 0:
            return True

        sample = frame[::8, ::8]
        if sample.size == 0:
            return True

        sample_f = sample.astype(np.float32, copy=False)
        blue = sample_f[:, :, 0]
        green = sample_f[:, :, 1]
        red = sample_f[:, :, 2]

        mean_b = float(blue.mean())
        mean_g = float(green.mean())
        mean_r = float(red.mean())
        if mean_g < 80.0:
            return False

        green_dominant = (green > red + 35.0) & (green > blue + 35.0)
        green_ratio = float(green_dominant.mean())
        green_vs_other_ratio = mean_g / max(1.0, max(mean_r, mean_b))
        return green_ratio >= 0.85 and green_vs_other_ratio >= 1.6

    def _read_valid_frame(self, cap, label: str, attempts: int = 5):
        for attempt in range(1, max(1, int(attempts)) + 1):
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            if self._is_green_screen_frame(frame):
                logger.warning(
                    "Rejected probable green/corrupt frame from %s (attempt %d/%d).",
                    label,
                    attempt,
                    attempts,
                )
                continue
            return True, frame
        return False, None

    def _capture_candidates_for_source(self, source) -> List[Tuple[str, object]]:
        if self._is_numeric_source(source):
            dev = int(source)
            return self._capture_candidates_for_numeric(dev=dev, include_csi=True)
        if isinstance(source, str):
            candidates = []  # type: List[Tuple[str, object]]
            if isinstance(source, str) and self._is_gst_pipeline:
                candidates.append(("gst-custom", lambda: cv2.VideoCapture(source, cv2.CAP_GSTREAMER)))
            candidates.append(("source-default", lambda: cv2.VideoCapture(source)))
            return candidates
        return [("source-default", lambda: cv2.VideoCapture(source))]

    def _probe_additional_indices(self, preferred_dev: int) -> List[int]:
        indices = []  # type: List[int]
        dev_dir = Path("/dev")
        if dev_dir.exists():
            for node in sorted(dev_dir.glob("video*")):
                suffix = node.name.replace("video", "")
                if suffix.isdigit():
                    idx = int(suffix)
                    if idx != preferred_dev and idx not in indices:
                        indices.append(idx)
        for idx in (0, 1, 2, 3, 4):
            if idx != preferred_dev and idx not in indices:
                indices.append(idx)
        return indices

    def _try_open_candidates(self, source, candidates: List[Tuple[str, object]], tried: List[str]):
        for name, opener in candidates:
            tried.append(name)
            cap = None
            try:
                cap = opener()
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    continue

                if self._is_numeric_source(source):
                    # Helps many USB cams avoid green frames on Jetson.
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

                ok, test_frame = self._read_valid_frame(cap, label=name, attempts=6)
                if not ok or test_frame is None:
                    cap.release()
                    continue

                logger.info("Camera opened using backend candidate: %s", name)
                self._active_capture_name = name
                return cap, test_frame
            except Exception:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                continue
        return None, None

    def _open_capture(self):
        tried = []  # type: List[str]
        candidates = self._capture_candidates_for_source(self._source)
        cap, test_frame = self._try_open_candidates(self._source, candidates, tried)
        if cap is not None and test_frame is not None:
            return cap, test_frame

        # Auto-probe alternate video nodes when numeric source fails.
        if self._is_numeric_source(self._source):
            preferred_dev = int(self._source)
            for probe_dev in self._probe_additional_indices(preferred_dev):
                logger.info("Primary camera source %s failed. Auto-probing /dev/video%s", preferred_dev, probe_dev)
                probe_candidates = self._capture_candidates_for_numeric(dev=probe_dev, include_csi=False)
                cap, test_frame = self._try_open_candidates(probe_dev, probe_candidates, tried)
                if cap is not None and test_frame is not None:
                    self._source = probe_dev
                    return cap, test_frame

        raise RuntimeError(
            "Cannot open camera: {0}. Tried: {1}. Check /dev/video* and camera permissions.".format(
                self._source,
                ", ".join(tried),
            )
        )

    def start(self) -> None:
        self._cap, test_frame = self._open_capture()

        height, width = test_frame.shape[:2] # Reliable way to get dimensions

        # Setup Writer
        self._session_dir = _next_session_dir(self.output_dir)
        self._video_path = self._session_dir / ("recording" + self.container)

        fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc)
        self._writer = cv2.VideoWriter(str(self._video_path), fourcc_code, self.fps, (width, height))
        if not self._writer.isOpened():
            raise RuntimeError("Could not open video writer for: {0}".format(self._video_path))

        # Preserve the first successfully read frame so survey detection sees
        # exactly the same first frame that is written to disk.
        self._pending_frame = test_frame
        self._frame_index = 0

        # This MUST be set to True, otherwise record_frame returns False immediately
        self._recording = True

    def record_frame(self, include_frame: bool = False):
        if not self._recording or self._cap is None or self._writer is None:
            if include_frame:
                return False, None, int(self._frame_index), _utc_now_iso()
            return False

        if self._pending_frame is not None:
            frame = self._pending_frame
            self._pending_frame = None
            ok = True
        else:
            ok, frame = self._read_valid_frame(
                self._cap,
                label=self._active_capture_name or "active-capture",
                attempts=4,
            )

        if not ok:
            if include_frame:
                return False, None, int(self._frame_index), _utc_now_iso()
            return False

        frame_ts = _utc_now_iso()
        self._writer.write(frame)
        self._frame_index += 1
        if include_frame:
            return True, frame, int(self._frame_index), frame_ts
        return True

    def stop(self) -> Path:
        self._recording = False
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._pending_frame = None
        self._active_capture_name = ""

        if self._video_path:
            logger.info("Recording stopped. Video saved to %s", self._video_path)

        if self.auto_extract and self._video_path and self._video_path.exists():
            frames_dir = self._session_dir / "frames"
            extractor = FrameExtractor(fps=self.fps)
            count = extractor.extract(self._video_path, frames_dir)
            logger.info("Extracted %d frames -> %s", count, frames_dir)

        # stop() is only called after start(), so this is safe.
        return self._video_path  # type: ignore[return-value]

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    @property
    def video_path(self) -> Optional[Path]:
        return self._video_path

    def __enter__(self) -> "DroneRecorder":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        if self._recording:
            self.stop()


def record_until_stop(
    source: Union[int, str] = 0,
    fps: int = 30,
    output_dir: Union[str, Path] = "recordings",
    fourcc: str = "XVID",
    container: str = ".avi",
    show_preview: bool = True,
    stop_key: str = "q",
) -> Tuple[Path, Path]:
    """Block and record until stop_key is pressed or source ends."""
    recorder = DroneRecorder(source=source, fps=fps, output_dir=output_dir,
                             fourcc=fourcc, container=container)
    recorder.start()
    frame_delay_ms = max(1, int(1000 / max(1, fps)))

    try:
        while True:
            ok = recorder.record_frame()
            if not ok:
                logger.warning("Frame capture failed - stopping.")
                break

            if show_preview:
                # Preview is intentionally no-op in this lightweight helper.
                pass

            key = cv2.waitKey(frame_delay_ms) & 0xFF
            if key == ord(stop_key):
                logger.info("Stop key '%s' pressed - ending recording.", stop_key)
                break
    finally:
        video_path = recorder.stop()
        if show_preview:
            cv2.destroyAllWindows()

    return video_path, recorder.session_dir  # type: ignore[return-value]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drone camera recorder")
    parser.add_argument("--source", default="0", help="Camera index or RTSP URL (default: 0)")
    parser.add_argument("--fps", type=int, default=30, help="Recording FPS (default: 30)")
    parser.add_argument("--output-dir", default="recordings", help="Root output directory")
    parser.add_argument("--no-preview", action="store_true", help="Disable live preview")
    parser.add_argument("--stop-key", default="q", help="Key to stop recording (default: q)")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    video, session = record_until_stop(
        source=args.source,
        fps=args.fps,
        output_dir=args.output_dir,
        show_preview=not args.no_preview,
        stop_key=args.stop_key,
    )
    print("\nDone!\n  Video   : {}\n  Session : {}".format(video, session))


#recorder.py
