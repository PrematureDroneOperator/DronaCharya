from __future__ import annotations

import argparse
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path

from training.common import ensure_dir, list_images_recursive


@dataclass
class Pair:
    image: Path
    label: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split YOLO image/label pairs into train/val/test folders.")
    parser.add_argument("--source-images-dir", type=Path, required=True, help="Directory with prepared images.")
    parser.add_argument("--source-labels-dir", type=Path, required=True, help="Directory with YOLO .txt labels.")
    parser.add_argument(
        "--output-dataset-dir",
        type=Path,
        default=Path("training/workspace/yolo_dataset"),
        help="Output YOLO dataset root.",
    )
    parser.add_argument("--train-ratio", type=float, default=0.8, help="Train split ratio.")
    parser.add_argument("--val-ratio", type=float, default=0.2, help="Validation split ratio.")
    parser.add_argument("--test-ratio", type=float, default=0.0, help="Test split ratio.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument("--class-names", type=str, default="target", help="Comma-separated class names.")
    parser.add_argument(
        "--copy-mode",
        choices=["copy", "hardlink"],
        default="copy",
        help="Copy files or create hard links.",
    )
    parser.add_argument(
        "--include-unlabeled",
        action="store_true",
        help="Include images with missing label file. Empty labels will be created.",
    )
    return parser.parse_args()


def validate_ratios(train_ratio: float, val_ratio: float, test_ratio: float) -> None:
    total = train_ratio + val_ratio + test_ratio
    if total <= 0:
        raise SystemExit("Ratios must sum to > 0.")
    if abs(total - 1.0) > 1e-6:
        raise SystemExit(f"Ratios must sum to 1.0, got {total:.6f}.")


def pair_images_and_labels(args: argparse.Namespace) -> tuple[list[Pair], int]:
    image_paths = list_images_recursive(args.source_images_dir)
    pairs: list[Pair] = []
    missing_labels = 0

    for image_path in image_paths:
        label_path = args.source_labels_dir / f"{image_path.stem}.txt"
        if not label_path.exists():
            missing_labels += 1
            if not args.include_unlabeled:
                continue
        pairs.append(Pair(image=image_path, label=label_path))
    return pairs, missing_labels


def assign_split(index: int, total: int, train_ratio: float, val_ratio: float) -> str:
    train_cutoff = int(total * train_ratio)
    val_cutoff = int(total * (train_ratio + val_ratio))
    if index < train_cutoff:
        return "train"
    if index < val_cutoff:
        return "val"
    return "test"


def copy_or_link(src: Path, dst: Path, mode: str) -> None:
    if dst.exists():
        dst.unlink()
    if mode == "hardlink":
        try:
            dst.hardlink_to(src)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def main() -> int:
    args = parse_args()
    validate_ratios(args.train_ratio, args.val_ratio, args.test_ratio)

    pairs, missing_labels = pair_images_and_labels(args)
    if not pairs:
        raise SystemExit("No image/label pairs found. Check source paths.")

    rng = random.Random(args.seed)
    rng.shuffle(pairs)

    dataset_root = ensure_dir(args.output_dataset_dir)
    split_names = ["train", "val", "test"]
    for split_name in split_names:
        ensure_dir(dataset_root / "images" / split_name)
        ensure_dir(dataset_root / "labels" / split_name)

    assignments = []
    split_counts = {"train": 0, "val": 0, "test": 0}

    for index, pair in enumerate(pairs):
        split_name = assign_split(index, len(pairs), args.train_ratio, args.val_ratio)
        if split_name == "test" and args.test_ratio <= 0:
            split_name = "val"

        image_out = dataset_root / "images" / split_name / pair.image.name
        label_out = dataset_root / "labels" / split_name / f"{pair.image.stem}.txt"

        copy_or_link(pair.image, image_out, args.copy_mode)
        if pair.label.exists():
            copy_or_link(pair.label, label_out, args.copy_mode)
        else:
            label_out.write_text("", encoding="utf-8")

        split_counts[split_name] += 1
        assignments.append(
            {
                "image": str(pair.image),
                "label": str(pair.label) if pair.label.exists() else "",
                "split": split_name,
            }
        )

    class_names = [name.strip() for name in args.class_names.split(",") if name.strip()]
    if not class_names:
        raise SystemExit("At least one class name required.")

    data_yaml = dataset_root / "data.yaml"
    names_block = "\n".join([f"  {idx}: {name}" for idx, name in enumerate(class_names)])
    data_yaml.write_text(
        "\n".join(
            [
                f"path: {dataset_root.resolve()}",
                "train: images/train",
                "val: images/val",
                "test: images/test",
                "names:",
                names_block,
                "",
            ]
        ),
        encoding="utf-8",
    )

    manifest_path = dataset_root / "split_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "total": len(pairs),
                "missing_labels_in_source": missing_labels,
                "splits": split_counts,
                "class_names": class_names,
                "assignments": assignments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Dataset split complete")
    print(f"Total pairs: {len(pairs)}")
    print(f"Split counts: {split_counts}")
    print(f"data.yaml: {data_yaml}")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
