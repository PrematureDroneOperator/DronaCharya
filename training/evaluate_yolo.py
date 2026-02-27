from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate trained YOLO model.")
    parser.add_argument("--model", type=Path, required=True, help="Path to trained .pt weights.")
    parser.add_argument("--data", type=Path, required=True, help="Path to dataset data.yaml.")
    parser.add_argument("--split", choices=["val", "test"], default="val", help="Split for evaluation.")
    parser.add_argument("--imgsz", type=int, default=640, help="Validation image size.")
    parser.add_argument("--device", type=str, default="cpu", help="Evaluation device.")
    parser.add_argument("--conf", type=float, default=0.001, help="Confidence threshold.")
    parser.add_argument("--iou", type=float, default=0.6, help="IoU threshold.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model = YOLO(str(args.model))
    metrics = model.val(
        data=str(args.data),
        split=args.split,
        imgsz=args.imgsz,
        device=args.device,
        conf=args.conf,
        iou=args.iou,
    )

    box_metrics = getattr(metrics, "box", None)
    map50 = getattr(box_metrics, "map50", None)
    map5095 = getattr(box_metrics, "map", None)

    print("Evaluation complete")
    print(f"mAP50: {map50}")
    print(f"mAP50-95: {map5095}")
    if hasattr(metrics, "save_dir"):
        print(f"Metrics artifacts: {metrics.save_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
