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
from pathlib import Path
from typing import Optional, Tuple, Union

import cv2

from vision.frame_extractor import FrameExtractor

logger = logging.getLogger(__name__)


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

    @property
    def _is_gst_pipeline(self) -> bool:
        """True when source is a GStreamer pipeline string (not a device index)."""
        return isinstance(self._source, str) and not self._source.startswith(("rtsp://", "http://", "https://"))

    def start(self) -> None:
        # Use the GStreamer backend explicitly for pipeline strings so OpenCV
        # does not fall back to V4L2 raw capture (which gives green frames when
        # the camera's native pixel format isn't plain BGR).
        if self._is_gst_pipeline:
            self._cap = cv2.VideoCapture(self._source, cv2.CAP_GSTREAMER)
        else:
            self._cap = cv2.VideoCapture(self._source)

        if not self._cap.isOpened():
            raise RuntimeError(
                "Cannot open camera source: {!r}\n"
                "  Hint: if you see green frames with device_id, set stream_url to a "
                "v4l2src GStreamer pipeline instead (see config/config.yaml).".format(self._source)
            )

        # For plain device-index sources, force the camera to output MJPEG.
        # This avoids the green-frame problem when the camera's native format
        # is YUYV — OpenCV decodes MJPEG to BGR correctly without GStreamer.
        if isinstance(self._source, int):
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        self._cap.set(cv2.CAP_PROP_FPS, self.fps)

        width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
        height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)

        self._session_dir = _next_session_dir(self.output_dir)
        # Use the configured container extension so the codec and container match.
        self._video_path = self._session_dir / ("recording" + self.container)

        fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc)
        self._writer = cv2.VideoWriter(str(self._video_path), fourcc_code, self.fps, (width, height))
        if not self._writer.isOpened():
            raise RuntimeError(
                "VideoWriter failed to initialize. "
                "fourcc={!r} container={!r}\n"
                "  On Jetson try: fourcc=MJPG container=.avi".format(
                    self.fourcc, self.container
                )
            )

        self._recording = True
        logger.info(
            "Recording started -> %s  [%dx%d @ %d fps]  codec=%s src=%r",
            self._video_path, width, height, self.fps, self.fourcc, self._source,
        )

    def record_frame(self) -> bool:
        if not self._recording or self._cap is None or self._writer is None:
            return False

        ok, frame = self._cap.read()
        if not ok:
            return False

        self._writer.write(frame)
        return True

    def stop(self) -> Path:
        self._recording = False
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

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
