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
from typing import Optional, Tuple, Union

import cv2

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

    @property
    def _is_gst_pipeline(self) -> bool:
        """True when source is a GStreamer pipeline string (not a device index)."""
        return isinstance(self._source, str) and not self._source.startswith(("rtsp://", "http://", "https://"))
    def start(self) -> None:
        if isinstance(self._source, int) or (isinstance(self._source, str) and self._source.isdigit()):
            dev = int(self._source)
            # Hardware-accelerated pipeline for Jetson CSI cameras
            gst_str = (
                f"nvarguscamerasrc sensor-id={dev} ! "
                "video/x-raw(memory:NVMM), width=1280, height=720, format=NV12, framerate=60/1 ! "
                "nvvidconv ! video/x-raw, format=BGRx ! "
                "videoconvert ! video/x-raw, format=BGR ! "
                "appsink drop=true max-buffers=1"
            )
            self._cap = cv2.VideoCapture(gst_str, cv2.CAP_GSTREAMER)
        else:
            self._cap = cv2.VideoCapture(self._source)

        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera: {self._source}")

        # IMPORTANT: Grab a test frame to get ACTUAL dimensions
        ok, test_frame = self._cap.read()
        if not ok:
            raise RuntimeError("Opened camera but could not read a frame.")

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
            ok, frame = self._cap.read()

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
