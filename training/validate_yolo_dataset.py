from __future__ import annotations

import argparse
import json
from pathlib import Path

from training.common import list_images_recursive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate YOLO dataset structure and annotation values.")
    parser.add_argument("--dataset-dir", type=Path, required=True, help="YOLO dataset root containing data.yaml.")
    parser.add_argument("--num-classes", type=int, default=1, help="Number of classes.")
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path("training/workspace/validation_report.json"),
        help="Path for validation report json.",
    )
    parser.add_argument(
        "--fail-on-missing-label",
        action="store_true",
        help="Treat missing label files as hard errors.",
    )
    return parser.parse_args()


def parse_label_line(line: str) -> tuple[int, float, float, float, float]:
    parts = line.strip().split()
    if len(parts) != 5:
        raise ValueError("Expected 5 values per label line.")
    cls_id = int(parts[0])
    x, y, w, h = [float(v) for v in parts[1:]]
    return cls_id, x, y, w, h


def validate_split(
    dataset_dir: Path,
    split: str,
    num_classes: int,
    fail_on_missing_label: bool,
) -> dict:
    images_dir = dataset_dir / "images" / split
    labels_dir = dataset_dir / "labels" / split
    if not images_dir.exists():
        return {"split": split, "images": 0, "labels": 0, "errors": [f"Missing {images_dir}"], "warnings": []}

    image_paths = list_images_recursive(images_dir)
    errors: list[str] = []
    warnings: list[str] = []
    label_count = 0
    object_count = 0

    for image_path in image_paths:
        label_path = labels_dir / f"{image_path.stem}.txt"
        if not label_path.exists():
            message = f"Missing label: {label_path}"
            if fail_on_missing_label:
                errors.append(message)
            else:
                warnings.append(message)
            continue

        label_count += 1
        lines = [line.strip() for line in label_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for line_index, line in enumerate(lines, start=1):
            try:
                cls_id, x, y, w, h = parse_label_line(line)
            except Exception as exc:
                errors.append(f"{label_path}:{line_index} parse error ({exc})")
                continue

            if cls_id < 0 or cls_id >= num_classes:
                errors.append(f"{label_path}:{line_index} invalid class id {cls_id}")
            for value, name in ((x, "x"), (y, "y"), (w, "w"), (h, "h")):
                if value < 0.0 or value > 1.0:
                    errors.append(f"{label_path}:{line_index} {name} out of [0,1]: {value}")
            if w <= 0.0 or h <= 0.0:
                errors.append(f"{label_path}:{line_index} width/height must be > 0.")
            object_count += 1

    return {
        "split": split,
        "images": len(image_paths),
        "labels": label_count,
        "objects": object_count,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    args = parse_args()
    if args.num_classes <= 0:
        raise SystemExit("--num-classes must be > 0")

    summaries = [
        validate_split(args.dataset_dir, "train", args.num_classes, args.fail_on_missing_label),
        validate_split(args.dataset_dir, "val", args.num_classes, args.fail_on_missing_label),
        validate_split(args.dataset_dir, "test", args.num_classes, args.fail_on_missing_label),
    ]

    total_images = sum(split["images"] for split in summaries)
    total_objects = sum(split["objects"] for split in summaries)
    total_errors = sum(len(split["errors"]) for split in summaries)
    total_warnings = sum(len(split["warnings"]) for split in summaries)

    report = {
        "dataset_dir": str(args.dataset_dir.resolve()),
        "num_classes": args.num_classes,
        "totals": {
            "images": total_images,
            "objects": total_objects,
            "errors": total_errors,
            "warnings": total_warnings,
        },
        "splits": summaries,
    }

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("Validation complete")
    print(f"Images: {total_images}")
    print(f"Objects: {total_objects}")
    print(f"Errors: {total_errors}")
    print(f"Warnings: {total_warnings}")
    print(f"Report: {args.report_json}")
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
