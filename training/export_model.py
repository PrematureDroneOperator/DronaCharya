from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export trained YOLO model to ONNX/TensorRT for deployment.")
    parser.add_argument("--model", type=Path, required=True, help="Path to trained .pt weights.")
    parser.add_argument("--format", choices=["onnx", "engine"], default="onnx", help="Export format.")
    parser.add_argument("--imgsz", type=int, default=640, help="Export image size.")
    parser.add_argument("--device", type=str, default="cpu", help="Device used during export.")
    parser.add_argument("--half", action="store_true", help="FP16 export when supported.")
    parser.add_argument("--dynamic", action="store_true", help="Enable dynamic input shape.")
    parser.add_argument("--int8", action="store_true", help="INT8 TensorRT export (engine only).")
    parser.add_argument(
        "--workspace",
        type=float,
        default=2.0,
        help="TensorRT workspace (GB) for engine export.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.int8 and args.format != "engine":
        raise SystemExit("--int8 is only valid with --format engine.")

    model = YOLO(str(args.model))
    exported_path = model.export(
        format=args.format,
        imgsz=args.imgsz,
        device=args.device,
        half=args.half,
        dynamic=args.dynamic,
        int8=args.int8,
        workspace=args.workspace,
    )

    print("Export complete")
    print(f"Exported artifact: {exported_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
