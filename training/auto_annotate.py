from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO

from training.common import ensure_dir, list_images_recursive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap YOLO labels from an existing model. Use for pre-labeling before manual correction."
    )
    parser.add_argument("--images-dir", type=Path, required=True, help="Directory with images to annotate.")
    parser.add_argument("--model", type=Path, required=True, help="Path to YOLO model weights.")
    parser.add_argument("--output-labels-dir", type=Path, required=True, help="Output folder for YOLO txt labels.")
    parser.add_argument("--conf", type=float, default=0.30, help="Confidence threshold.")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference size.")
    parser.add_argument("--device", type=str, default="cpu", help="Device string. Example: cpu, 0.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing labels.")
    parser.add_argument("--class-id-filter", type=int, default=-1, help="Only keep this class id. -1 keeps all.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dir(args.output_labels_dir)

    image_paths = list_images_recursive(args.images_dir)
    if not image_paths:
        raise SystemExit("No images found.")

    model = YOLO(str(args.model))
    written = 0
    skipped = 0

    for image_path in image_paths:
        label_path = args.output_labels_dir / f"{image_path.stem}.txt"
        if label_path.exists() and not args.overwrite:
            skipped += 1
            continue

        results = model.predict(
            source=str(image_path),
            conf=args.conf,
            imgsz=args.imgsz,
            device=args.device,
            verbose=False,
        )
        if not results:
            continue

        lines: list[str] = []
        res = results[0]
        for box in res.boxes:
            class_id = int(box.cls.item())
            if args.class_id_filter >= 0 and class_id != args.class_id_filter:
                continue
            x_center, y_center, width, height = box.xywhn[0].tolist()
            lines.append(
                f"{class_id} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}"
            )

        label_path.write_text("\n".join(lines), encoding="utf-8")
        written += 1

    print("Auto-annotation complete")
    print(f"Images processed: {len(image_paths)}")
    print(f"Labels written: {written}")
    print(f"Skipped existing: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
