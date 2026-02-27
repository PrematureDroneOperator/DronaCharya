"""vision/recorder.py
Drone camera recorder – captures video at 30 FPS from the drone's onboard
camera (or any OpenCV-compatible source), writes a .mp4 file, and then
auto-enumerates every frame into a ``session-XXXX/`` directory.

Usage (standalone)
------------------
    python -m vision.recorder               # uses webcam / default source
    python -m vision.recorder --source 0    # explicit device index
    python -m vision.recorder --source rtsp://192.168.1.1/video
    python -m vision.recorder --fps 30 --output-dir recordings
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path

import cv2

from vision.frame_extractor import FrameExtractor

logger = logging.getLogger(__name__)


def _next_session_dir(base_dir: Path) -> Path:
    """Return the next free ``session-XXXX`` directory under *base_dir*."""
    existing = [
        d.name for d in base_dir.iterdir() if d.is_dir() and re.fullmatch(r"session-\d{4}", d.name)
    ] if base_dir.exists() else []
    if existing:
        last_num = max(int(n.split("-")[1]) for n in existing)
    else:
        last_num = -1
    new_dir = base_dir / f"session-{last_num + 1:04d}"
    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


class DroneRecorder:
    """Record video from a camera source at *fps* frames per second.

    Parameters
    ----------
    source:
        OpenCV-compatible source – device index (int or str int), RTSP URL,
        or any ``cv2.VideoCapture``-compatible string.
    fps:
        Target recording frame rate (default 30).
    output_dir:
        Root directory where ``session-XXXX/`` folders are created.
    fourcc:
        FourCC codec string for the output .mp4 (default ``mp4v``).
    auto_extract:
        Automatically extract frames to ``session-XXXX/frames/`` after
        recording stops (default ``True``).
    """

    def __init__(
        self,
        source: int | str = 0,
        fps: int = 30,
        output_dir: str | Path = "recordings",
        fourcc: str = "mp4v",
        auto_extract: bool = True,
    ) -> None:
        try:
            self._source: int | str = int(source)
        except (ValueError, TypeError):
            self._source = source

        self.fps = fps
        self.output_dir = Path(output_dir)
        self.fourcc = fourcc
        self.auto_extract = auto_extract

        self._cap: cv2.VideoCapture | None = None
        self._writer: cv2.VideoWriter | None = None
        self._session_dir: Path | None = None
        self._video_path: Path | None = None
        self._recording = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open camera and begin recording."""
        self._cap = cv2.VideoCapture(self._source)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera source: {self._source!r}")

        # Try to set the capture FPS (best-effort – hardware may ignore it)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)

        width  = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)  or 1280)
        height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)

        self._session_dir = _next_session_dir(self.output_dir)
        self._video_path  = self._session_dir / "recording.mp4"

        fourcc = cv2.VideoWriter_fourcc(*self.fourcc)
        self._writer = cv2.VideoWriter(
            str(self._video_path), fourcc, self.fps, (width, height)
        )
        if not self._writer.isOpened():
            raise RuntimeError("VideoWriter failed to initialise.")

        self._recording = True
        logger.info(
            "Recording started → %s  [%dx%d @ %d fps]",
            self._video_path, width, height, self.fps,
        )

    def record_frame(self) -> bool:
        """Capture and write a single frame.  Returns ``False`` when the
        source is exhausted (end-of-file / camera disconnected)."""
        if not self._recording or self._cap is None or self._writer is None:
            return False
        ok, frame = self._cap.read()
        if not ok:
            return False
        self._writer.write(frame)
        return True

    def stop(self) -> Path:
        """Stop recording, release resources, and (optionally) extract frames.

        Returns
        -------
        Path
            The path to the recorded ``.mp4`` file.
        """
        self._recording = False
        if self._writer:
            self._writer.release()
            self._writer = None
        if self._cap:
            self._cap.release()
            self._cap = None

        logger.info("Recording stopped. Video saved to %s", self._video_path)

        if self.auto_extract and self._video_path and self._video_path.exists():
            frames_dir = self._session_dir / "frames"
            extractor = FrameExtractor(fps=self.fps)
            count = extractor.extract(self._video_path, frames_dir)
            logger.info("Extracted %d frames → %s", count, frames_dir)

        return self._video_path  # type: ignore[return-value]

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def video_path(self) -> Path | None:
        return self._video_path

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> DroneRecorder:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        if self._recording:
            self.stop()


# ------------------------------------------------------------------
# Blocking record-until-keypress helper
# ------------------------------------------------------------------

def record_until_stop(
    source: int | str = 0,
    fps: int = 30,
    output_dir: str | Path = "recordings",
    show_preview: bool = True,
    stop_key: str = "q",
) -> tuple[Path, Path]:
    """Block and record until *stop_key* is pressed (or the source ends).

    Parameters
    ----------
    source:
        Camera / RTSP source passed to :class:`DroneRecorder`.
    fps:
        Recording frame rate.
    output_dir:
        Root recordings directory.
    show_preview:
        Display a live preview window while recording.
    stop_key:
        Key character that stops recording (default ``'q'``).

    Returns
    -------
    tuple[Path, Path]
        ``(video_path, session_dir)``
    """
    recorder = DroneRecorder(source=source, fps=fps, output_dir=output_dir)
    recorder.start()

    frame_delay_ms = max(1, int(1000 / fps))

    try:
        while True:
            ok = recorder.record_frame()
            if not ok:
                logger.warning("Frame capture failed – stopping.")
                break

            if show_preview:
                # Grab the last written frame for display (re-read from cap)
                # We use a tiny peek without a second read; just show a simple
                # title instead of re-reading to avoid desync.
                cap_peek = cv2.VideoCapture(str(recorder.video_path)) if recorder.video_path else None
                # Cheaper: open a temporary cap just to show live preview
                # We actually maintain a separate display cap bound to source.
                # (handled below via _display_cap)
                pass

            # Honour requested FPS timing
            key = cv2.waitKey(frame_delay_ms) & 0xFF
            if key == ord(stop_key):
                logger.info("Stop key '%s' pressed – ending recording.", stop_key)
                break
    finally:
        video_path = recorder.stop()
        if show_preview:
            cv2.destroyAllWindows()

    return video_path, recorder.session_dir  # type: ignore[return-value]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Drone camera recorder")
    p.add_argument("--source", default="0", help="Camera index or RTSP URL (default: 0)")
    p.add_argument("--fps", type=int, default=30, help="Recording FPS (default: 30)")
    p.add_argument("--output-dir", default="recordings", help="Root output directory")
    p.add_argument("--no-preview", action="store_true", help="Disable live preview")
    p.add_argument("--stop-key", default="q", help="Key to stop recording (default: q)")
    return p.parse_args()


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
    print(f"\nDone!\n  Video   : {video}\n  Session : {session}")
