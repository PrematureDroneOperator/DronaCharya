"""vision/frame_extractor.py
Extract frames from a recorded video file into a target directory.

Usage:
    python -m vision.frame_extractor path/to/recording.mp4 --out frames/
"""

import argparse
import logging
from pathlib import Path
from typing import Union

import cv2

logger = logging.getLogger(__name__)


class FrameExtractor:
    """Extract frames from video files into image sequences."""

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

    def extract(self, video_path: Union[str, Path], output_dir: Union[str, Path]) -> int:
        video_path = Path(video_path)
        output_dir = Path(output_dir)

        if not video_path.exists():
            raise FileNotFoundError("Video not found: {}".format(video_path))

        output_dir.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError("Cannot open video file: {}".format(video_path))

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS) or self.fps
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        logger.info(
            "Extracting frames from %s  [%dx%d, %.1f fps, ~%d frames]",
            video_path.name,
            width,
            height,
            actual_fps,
            total_frames,
        )

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        count = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                count += 1
                filename = output_dir / "{0}{1:06d}.{2}".format(self.prefix, count, self.ext)
                cv2.imwrite(str(filename), frame, encode_params)
        finally:
            cap.release()

        logger.info("Extraction complete - %d frames saved to %s", count, output_dir)
        return count

    def extract_nth(
        self,
        video_path: Union[str, Path],
        output_dir: Union[str, Path],
        every_n: int = 1,
    ) -> int:
        """Extract only every N-th frame."""
        if every_n <= 0:
            raise ValueError("every_n must be >= 1")

        video_path = Path(video_path)
        output_dir = Path(output_dir)

        if not video_path.exists():
            raise FileNotFoundError("Video not found: {}".format(video_path))

        output_dir.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError("Cannot open video file: {}".format(video_path))

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        frame_idx = 0
        saved = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_idx += 1
                if frame_idx % every_n == 0:
                    saved += 1
                    filename = output_dir / "{0}{1:06d}.{2}".format(self.prefix, saved, self.ext)
                    cv2.imwrite(str(filename), frame, encode_params)
        finally:
            cap.release()

        logger.info("Sparse extraction (every %d frames) - %d frames saved to %s", every_n, saved, output_dir)
        return saved


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract frames from a recorded video")
    parser.add_argument("video", help="Path to the video file")
    parser.add_argument("--out", default="frames", help="Output directory (default: frames/)")
    parser.add_argument("--every-n", type=int, default=1, help="Save every N-th frame (default: 1)")
    parser.add_argument("--quality", type=int, default=95, help="JPEG quality 0-100 (default: 95)")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    extractor = FrameExtractor(jpeg_quality=args.quality)
    count = extractor.extract_nth(args.video, args.out, every_n=args.every_n)
    print("Extracted {} frames -> {}".format(count, args.out))
