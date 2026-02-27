from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train YOLO detector for dronAcharya targets.")
    parser.add_argument("--data", type=Path, required=True, help="Path to dataset data.yaml.")
    parser.add_argument("--model", type=str, default="yolov8n.pt", help="Base model (pretrained .pt).")
    parser.add_argument("--project", type=Path, default=Path("training/runs"), help="Run output project folder.")
    parser.add_argument("--name", type=str, default="target-detector", help="Run name.")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs.")
    parser.add_argument("--imgsz", type=int, default=640, help="Image size.")
    parser.add_argument("--batch", type=int, default=16, help="Batch size.")
    parser.add_argument("--device", type=str, default="0", help="Training device (0,1,cpu...).")
    parser.add_argument("--workers", type=int, default=4, help="Data loader workers.")
    parser.add_argument("--patience", type=int, default=20, help="Early stopping patience.")
    parser.add_argument("--cache", choices=["ram", "disk", "false"], default="false", help="Dataset cache mode.")
    parser.add_argument("--optimizer", type=str, default="auto", help="Optimizer override.")
    parser.add_argument("--lr0", type=float, default=0.01, help="Initial learning rate.")
    parser.add_argument("--weight-decay", type=float, default=0.0005, help="Weight decay.")
    parser.add_argument("--close-mosaic", type=int, default=10, help="Disable mosaic in final N epochs.")
    parser.add_argument("--cos-lr", action="store_true", help="Use cosine LR schedule.")
    parser.add_argument("--freeze", type=int, default=0, help="Freeze first N layers.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.project.mkdir(parents=True, exist_ok=True)

    cache_value = args.cache if args.cache != "false" else False
    model = YOLO(args.model)

    results = model.train(
        data=str(args.data),
        project=str(args.project),
        name=args.name,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=args.workers,
        patience=args.patience,
        cache=cache_value,
        optimizer=args.optimizer,
        lr0=args.lr0,
        weight_decay=args.weight_decay,
        close_mosaic=args.close_mosaic,
        cos_lr=args.cos_lr,
        freeze=args.freeze,
    )

    save_dir = Path(results.save_dir)
    best_path = save_dir / "weights" / "best.pt"
    last_path = save_dir / "weights" / "last.pt"
    print("Training completed")
    print(f"Run directory: {save_dir}")
    print(f"Best weights: {best_path}")
    print(f"Last weights: {last_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
