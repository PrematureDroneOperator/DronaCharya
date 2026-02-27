from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from training.common import ensure_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert COCO annotations to YOLO label txt files.")
    parser.add_argument("--coco-json", type=Path, required=True, help="Path to COCO annotation json.")
    parser.add_argument("--output-labels-dir", type=Path, required=True, help="Output folder for YOLO txt labels.")
    parser.add_argument(
        "--class-names",
        type=str,
        default="target",
        help="Comma-separated class names in final YOLO order.",
    )
    parser.add_argument(
        "--write-empty-labels",
        action="store_true",
        help="Write empty txt files for images with no annotations.",
    )
    parser.add_argument(
        "--skip-crowd",
        action="store_true",
        help="Ignore annotations where iscrowd=1.",
    )
    return parser.parse_args()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def main() -> int:
    args = parse_args()
    with args.coco_json.open("r", encoding="utf-8") as handle:
        coco = json.load(handle)

    categories = coco.get("categories", [])
    images = coco.get("images", [])
    annotations = coco.get("annotations", [])

    yolo_class_names = [name.strip() for name in args.class_names.split(",") if name.strip()]
    if not yolo_class_names:
        raise SystemExit("At least one class name is required.")
    yolo_class_to_id = {name: idx for idx, name in enumerate(yolo_class_names)}

    coco_category_id_to_name = {int(cat["id"]): cat["name"] for cat in categories}
    image_by_id = {int(image["id"]): image for image in images}
    anns_by_image: dict[int, list[dict]] = defaultdict(list)
    for ann in annotations:
        anns_by_image[int(ann["image_id"])].append(ann)

    output_labels_dir = ensure_dir(args.output_labels_dir)
    converted = 0
    skipped = 0

    for image_id, image_info in image_by_id.items():
        width = float(image_info["width"])
        height = float(image_info["height"])
        if width <= 0 or height <= 0:
            skipped += 1
            continue

        lines: list[str] = []
        for ann in anns_by_image.get(image_id, []):
            if args.skip_crowd and int(ann.get("iscrowd", 0)) == 1:
                continue

            coco_cat_id = int(ann["category_id"])
            coco_name = coco_category_id_to_name.get(coco_cat_id)
            if coco_name is None:
                continue
            if coco_name not in yolo_class_to_id:
                continue

            bbox = ann.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            x, y, w, h = [float(v) for v in bbox]
            if w <= 0 or h <= 0:
                continue

            cx = (x + w / 2.0) / width
            cy = (y + h / 2.0) / height
            nw = w / width
            nh = h / height

            cx = clamp(cx, 0.0, 1.0)
            cy = clamp(cy, 0.0, 1.0)
            nw = clamp(nw, 0.0, 1.0)
            nh = clamp(nh, 0.0, 1.0)

            class_id = yolo_class_to_id[coco_name]
            lines.append(f"{class_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

        stem = Path(image_info["file_name"]).stem
        label_path = output_labels_dir / f"{stem}.txt"

        if lines or args.write_empty_labels:
            with label_path.open("w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
            converted += 1

    classes_path = output_labels_dir / "classes.txt"
    with classes_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(yolo_class_names) + "\n")

    print("COCO -> YOLO conversion complete")
    print(f"Labels written: {converted}")
    print(f"Images skipped due to invalid dimensions: {skipped}")
    print(f"Classes file: {classes_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
