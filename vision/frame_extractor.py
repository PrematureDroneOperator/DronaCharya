"""vision/frame_extractor.py
Extract individual frames from a recorded video file and save them inside a
target directory as zero-padded JPEG images.

Directory layout produced
-------------------------
::

    session-0001/
    ├── recording.mp4
    └── frames/
        ├── frame_000001.jpg
        ├── frame_000002.jpg
        └── …

Usage (standalone)
------------------
    python -m vision.frame_extractor path/to/recording.mp4 --out frames/
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)


class FrameExtractor:
    """Extract every frame from a video file to a directory.

    Parameters
    ----------
    fps:
        Informational – used only for logging; the actual frame rate is read
        from the video file itself.
    prefix:
        Filename prefix for each frame image (default ``"frame_"``).
    ext:
        Image extension / format (default ``"jpg"``).
    jpeg_quality:
        JPEG compression quality 0-100 (default ``95``).
    """

    def __init__(
        self,
        fps: int = 30,
        prefix: str = "frame_",
        ext: str = "jpg",
        jpeg_quality: int = 95,
    ) -> None:
        self.fps = fps
        self.prefix = prefix
        self.ext = ext.lstrip(".")
        self.jpeg_quality = jpeg_quality

    def extract(self, video_path: str | Path, output_dir: str | Path) -> int:
        """Read *video_path* and write every frame to *output_dir*.

        Parameters
        ----------
        video_path:
            Path to the recorded ``.mp4`` (or any OpenCV-readable format).
        output_dir:
            Directory where frame images will be saved.  Created if absent.

        Returns
        -------
        int
            Number of frames successfully extracted.

        Raises
        ------
        FileNotFoundError
            If *video_path* does not exist.
        RuntimeError
            If the video file cannot be opened.
        """
        video_path = Path(video_path)
        output_dir = Path(output_dir)

        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        output_dir.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video file: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps   = cap.get(cv2.CAP_PROP_FPS) or self.fps
        width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        logger.info(
            "Extracting frames from %s  [%dx%d, %.1f fps, ~%d frames]",
            video_path.name, width, height, actual_fps, total_frames,
        )

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        count = 0

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                count += 1
                filename = output_dir / f"{self.prefix}{count:06d}.{self.ext}"
                cv2.imwrite(str(filename), frame, encode_params)
        finally:
            cap.release()

        logger.info("Extraction complete – %d frames saved to %s", count, output_dir)
        return count

    # ------------------------------------------------------------------
    # Convenience: extract a subset (every N-th frame)
    # ------------------------------------------------------------------

    def extract_nth(
        self,
        video_path: str | Path,
        output_dir: str | Path,
        every_n: int = 1,
    ) -> int:
        """Like :meth:`extract` but only saves every *every_n*-th frame.

        Useful for creating lower-resolution datasets without storing all
        30 frames per second.
        """
        video_path = Path(video_path)
        output_dir = Path(output_dir)

        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        output_dir.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video file: {video_path}")

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        frame_idx = 0
        saved     = 0

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_idx += 1
                if frame_idx % every_n == 0:
                    saved += 1
                    filename = output_dir / f"{self.prefix}{saved:06d}.{self.ext}"
                    cv2.imwrite(str(filename), frame, encode_params)
        finally:
            cap.release()

        logger.info(
            "Sparse extraction (every %d frames) – %d frames saved to %s",
            every_n, saved, output_dir,
        )
        return saved


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract frames from a recorded video")
    p.add_argument("video", help="Path to the video file")
    p.add_argument("--out", default="frames", help="Output directory (default: frames/)")
    p.add_argument("--every-n", type=int, default=1, help="Save every N-th frame (default: 1)")
    p.add_argument("--quality", type=int, default=95, help="JPEG quality 0-100 (default: 95)")
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    extractor = FrameExtractor(jpeg_quality=args.quality)
    n = extractor.extract_nth(args.video, args.out, every_n=args.every_n)
    print(f"Extracted {n} frames → {args.out}")
