from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import cv2

from training.common import ensure_dir, list_images_recursive, list_videos_recursive, sha1_digest, stem_without_spaces


@dataclass
class PrepareStats:
    processed_images: int = 0
    extracted_frames: int = 0
    skipped_blurry: int = 0
    skipped_duplicates: int = 0
    failed_reads: int = 0
    written: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert raw drone images/videos into a cleaned image pool for YOLO annotation."
    )
    parser.add_argument("--raw-images-dir", type=Path, default=None, help="Directory with raw source images.")
    parser.add_argument("--raw-videos-dir", type=Path, default=None, help="Directory with raw source videos.")
    parser.add_argument(
        "--output-images-dir",
        type=Path,
        default=Path("training/workspace/prepared/images"),
        help="Destination directory for prepared images.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=Path("training/workspace/prepared/manifest.csv"),
        help="CSV file containing source-to-output mapping.",
    )
    parser.add_argument("--video-frame-step", type=int, default=8, help="Keep one frame every N video frames.")
    parser.add_argument(
        "--min-sharpness",
        type=float,
        default=40.0,
        help="Minimum Laplacian variance threshold. Lower means blurrier images accepted.",
    )
    parser.add_argument("--resize-width", type=int, default=0, help="Optional resize width. 0 disables resize.")
    parser.add_argument("--resize-height", type=int, default=0, help="Optional resize height. 0 disables resize.")
    parser.add_argument("--max-output-images", type=int, default=0, help="Optional cap on output image count.")
    parser.add_argument("--prefix", type=str, default="target", help="Filename prefix for generated images.")
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=95,
        help="JPEG quality for saved images (1-100).",
    )
    return parser.parse_args()


def resize_if_needed(image, width: int, height: int):
    if width <= 0 and height <= 0:
        return image
    if width > 0 and height > 0:
        return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

    original_h, original_w = image.shape[:2]
    if width > 0:
        scale = width / float(original_w)
        target_h = max(1, int(original_h * scale))
        return cv2.resize(image, (width, target_h), interpolation=cv2.INTER_AREA)

    scale = height / float(original_h)
    target_w = max(1, int(original_w * scale))
    return cv2.resize(image, (target_w, height), interpolation=cv2.INTER_AREA)


def sharpness_score(image) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def write_image(image, output_path: Path, jpeg_quality: int) -> bool:
    jpeg_quality = max(1, min(jpeg_quality, 100))
    return bool(cv2.imwrite(str(output_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]))


def process_image(
    image,
    source_ref: str,
    output_images_dir: Path,
    manifest_writer,
    seen_hashes: set[str],
    sequence_id: int,
    args: argparse.Namespace,
    stats: PrepareStats,
) -> int:
    resized = resize_if_needed(image, args.resize_width, args.resize_height)
    score = sharpness_score(resized)
    if score < args.min_sharpness:
        stats.skipped_blurry += 1
        return sequence_id

    encoded_ok, encoded = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), args.jpeg_quality])
    if not encoded_ok:
        stats.failed_reads += 1
        return sequence_id

    digest = sha1_digest(encoded.tobytes())
    if digest in seen_hashes:
        stats.skipped_duplicates += 1
        return sequence_id
    seen_hashes.add(digest)

    output_name = f"{stem_without_spaces(args.prefix)}_{sequence_id:06d}.jpg"
    output_path = output_images_dir / output_name
    if not write_image(resized, output_path, args.jpeg_quality):
        stats.failed_reads += 1
        return sequence_id

    manifest_writer.writerow(
        {
            "output_file": output_name,
            "source": source_ref,
            "width": int(resized.shape[1]),
            "height": int(resized.shape[0]),
            "sharpness": round(score, 3),
            "sha1": digest,
        }
    )
    stats.written += 1
    return sequence_id + 1


def ingest_raw_images(
    args: argparse.Namespace,
    output_images_dir: Path,
    manifest_writer,
    seen_hashes: set[str],
    start_index: int,
    stats: PrepareStats,
) -> int:
    if args.raw_images_dir is None:
        return start_index
    image_paths = list_images_recursive(args.raw_images_dir)
    for image_path in image_paths:
        if args.max_output_images > 0 and stats.written >= args.max_output_images:
            break
        image = cv2.imread(str(image_path))
        if image is None:
            stats.failed_reads += 1
            continue
        stats.processed_images += 1
        start_index = process_image(
            image=image,
            source_ref=str(image_path),
            output_images_dir=output_images_dir,
            manifest_writer=manifest_writer,
            seen_hashes=seen_hashes,
            sequence_id=start_index,
            args=args,
            stats=stats,
        )
    return start_index


def ingest_videos(
    args: argparse.Namespace,
    output_images_dir: Path,
    manifest_writer,
    seen_hashes: set[str],
    start_index: int,
    stats: PrepareStats,
) -> int:
    if args.raw_videos_dir is None:
        return start_index

    video_paths = list_videos_recursive(args.raw_videos_dir)
    for video_path in video_paths:
        if args.max_output_images > 0 and stats.written >= args.max_output_images:
            break

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            stats.failed_reads += 1
            continue

        frame_idx = 0
        try:
            while True:
                if args.max_output_images > 0 and stats.written >= args.max_output_images:
                    break

                ok, frame = cap.read()
                if not ok:
                    break

                if frame_idx % args.video_frame_step != 0:
                    frame_idx += 1
                    continue

                stats.extracted_frames += 1
                source = f"{video_path}#frame={frame_idx}"
                start_index = process_image(
                    image=frame,
                    source_ref=source,
                    output_images_dir=output_images_dir,
                    manifest_writer=manifest_writer,
                    seen_hashes=seen_hashes,
                    sequence_id=start_index,
                    args=args,
                    stats=stats,
                )
                frame_idx += 1
        finally:
            cap.release()
    return start_index


def main() -> int:
    args = parse_args()
    if args.raw_images_dir is None and args.raw_videos_dir is None:
        raise SystemExit("Provide at least one of --raw-images-dir or --raw-videos-dir.")
    if args.video_frame_step <= 0:
        raise SystemExit("--video-frame-step must be >= 1.")

    output_images_dir = ensure_dir(args.output_images_dir)
    ensure_dir(args.manifest_path.parent)

    seen_hashes: set[str] = set()
    stats = PrepareStats()
    sequence_id = 0

    with args.manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["output_file", "source", "width", "height", "sharpness", "sha1"],
        )
        writer.writeheader()

        sequence_id = ingest_raw_images(
            args=args,
            output_images_dir=output_images_dir,
            manifest_writer=writer,
            seen_hashes=seen_hashes,
            start_index=sequence_id,
            stats=stats,
        )

        sequence_id = ingest_videos(
            args=args,
            output_images_dir=output_images_dir,
            manifest_writer=writer,
            seen_hashes=seen_hashes,
            start_index=sequence_id,
            stats=stats,
        )

    print("Prepare complete")
    print(f"Written images: {stats.written}")
    print(f"Processed raw images: {stats.processed_images}")
    print(f"Extracted video frames: {stats.extracted_frames}")
    print(f"Skipped blurry: {stats.skipped_blurry}")
    print(f"Skipped duplicates: {stats.skipped_duplicates}")
    print(f"Failed reads/writes: {stats.failed_reads}")
    print(f"Output directory: {output_images_dir}")
    print(f"Manifest: {args.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
