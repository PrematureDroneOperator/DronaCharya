"""
test_model.py  –  Quick local test for the DronaCharya target-detection model.

Modes
-----
1. Webcam live feed  (default)
2. Image file(s)     --source path/to/image.jpg  (or a folder)
3. Video file        --source path/to/video.mp4

Debug flags
-----------
--conf 0.01      Drop threshold very low to see ALL predictions the model makes
--debug          Print raw confidence scores to terminal every N frames

Usage examples
--------------
  python test_model.py --source malan.mp4                        # normal
  python test_model.py --source malan.mp4 --conf 0.05 --debug   # debug: see ALL predictions
  python test_model.py --source "My First Project.v1i.yolov8/test/images/" --conf 0.05 --debug
  python test_model.py   # webcam
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

try:
    from ultralytics import YOLO
except ImportError:
    print("[ERROR] ultralytics not installed. Run: pip install ultralytics")
    sys.exit(1)

# ── defaults ─────────────────────────────────────────────────────────────────
DEFAULT_MODEL  = "models/best.pt"
DEFAULT_CONF   = 0.10          # ← lowered from 0.35; small datasets need lower thresholds
DEFAULT_IMGSZ  = 640
BOX_COLOR      = (0, 255, 80)
TEXT_COLOR     = (0, 0, 0)
TEXT_BG_COLOR  = (0, 255, 80)
FONT           = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE     = 0.6
THICKNESS      = 2
# ─────────────────────────────────────────────────────────────────────────────


def draw_detections(frame: np.ndarray, results) -> tuple[np.ndarray, int]:
    detections = 0
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        for box in boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf  = float(box.conf[0])
            cls   = int(box.cls[0])
            label = result.names[cls]
            text  = f"{label} {conf:.2f}"

            cv2.rectangle(frame, (x1, y1), (x2, y2), BOX_COLOR, THICKNESS)
            (tw, th), _ = cv2.getTextSize(text, FONT, FONT_SCALE, THICKNESS)
            cv2.rectangle(frame, (x1, y1 - th - 8), (x1 + tw + 4, y1), TEXT_BG_COLOR, -1)
            cv2.putText(frame, text, (x1 + 2, y1 - 4), FONT, FONT_SCALE, TEXT_COLOR, THICKNESS)
            detections += 1

    return frame, detections


def debug_print(results, frame_idx: int, every_n: int = 10) -> None:
    """Print raw confidence scores from the model before threshold filtering."""
    if frame_idx % every_n != 0:
        return
    for result in results:
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            print(f"  [frame {frame_idx}] No raw predictions above internal pre-NMS filter.")
            return
        confs = [float(b.conf[0]) for b in boxes]
        print(f"  [frame {frame_idx}] Raw confidences: {[f'{c:.3f}' for c in confs]}")


def overlay_stats(frame: np.ndarray, fps: float, detections: int,
                  conf: float, debug: bool) -> np.ndarray:
    w = frame.shape[1]
    cv2.rectangle(frame, (0, 0), (w, 38), (20, 20, 20), -1)
    mode_str = "DEBUG" if debug else "RUN"
    info = f"{mode_str} | conf>={conf:.2f} | FPS:{fps:5.1f} | Det:{detections} | [Q]quit"
    cv2.putText(frame, info, (8, 26), FONT, 0.60, (220, 220, 220), 1, cv2.LINE_AA)
    return frame


# ── source handlers ───────────────────────────────────────────────────────────

def run_webcam(model: YOLO, conf: float, imgsz: int, debug: bool) -> None:
    print("[INFO] Opening webcam (index 0). Press Q to quit.")
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] Cannot open webcam. Try --source <video_file> instead.")
        return
    fps_t = time.time()
    fps = 0.0
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        results = model.predict(frame, conf=conf, imgsz=imgsz, verbose=False)
        if debug:
            debug_print(results, frame_idx)
        frame, n = draw_detections(frame, results)
        now = time.time()
        fps = 0.9 * fps + 0.1 * (1.0 / max(now - fps_t, 1e-6))
        fps_t = now
        frame = overlay_stats(frame, fps, n, conf, debug)
        cv2.imshow("DronaCharya – detector", frame)
        if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q"), 27):
            break
        frame_idx += 1
    cap.release()
    cv2.destroyAllWindows()


def run_video(source: str, model: YOLO, conf: float, imgsz: int,
              save: bool, debug: bool) -> None:
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open video: {source}")
        return

    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    orig_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    writer = None
    if save:
        out_path = Path(source).stem + "_detected.mp4"
        writer = cv2.VideoWriter(str(out_path),
                                 cv2.VideoWriter_fourcc(*"mp4v"), orig_fps, (w, h))
        print(f"[INFO] Saving output to {out_path}")

    if debug:
        print(f"[DEBUG] conf threshold = {conf}  (raw predictions printed every 10 frames)")
        print(f"[DEBUG] Video: {w}x{h}  {orig_fps:.1f}fps  {total} frames")

    fps_t = time.time()
    fps = 0.0
    frame_idx = 0
    detection_frames = 0  # frames that had ≥1 detection

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        results = model.predict(frame, conf=conf, imgsz=imgsz, verbose=False)

        if debug:
            debug_print(results, frame_idx)

        frame, n = draw_detections(frame, results)

        if n > 0:
            detection_frames += 1

        now = time.time()
        fps = 0.9 * fps + 0.1 * (1.0 / max(now - fps_t, 1e-6))
        fps_t = now

        frame = overlay_stats(frame, fps, n, conf, debug)

        if writer:
            writer.write(frame)

        cv2.imshow("DronaCharya – detector", frame)
        if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q"), 27):
            break

        frame_idx += 1

    cap.release()
    if writer:
        writer.release()
    cv2.destroyAllWindows()

    print(f"\n[SUMMARY] Frames processed: {frame_idx}")
    print(f"[SUMMARY] Frames with detections: {detection_frames} "
          f"({100*detection_frames/max(frame_idx,1):.1f}%)")
    if detection_frames == 0:
        print("[HINT] Zero detections. Try:")
        print("       1. Lower conf:  --conf 0.01")
        print("       2. Add debug:   --debug  (shows raw scores per frame)")
        print("       3. Test on a known image from the dataset first:")
        print(f"          python test_model.py --source \"My First Project.v1i.yolov8/test/images/\" --conf 0.01 --debug")


def run_images(source: str, model: YOLO, conf: float, imgsz: int,
               save: bool, debug: bool) -> None:
    p = Path(source)
    if p.is_dir():
        exts  = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        paths = sorted(f for f in p.iterdir() if f.suffix.lower() in exts)
    else:
        paths = [p]

    if not paths:
        print(f"[WARN] No image files found at: {source}")
        return

    print(f"[INFO] Running on {len(paths)} image(s).  conf={conf}  debug={debug}")
    print("[INFO] Press any key → next image, Q → quit")

    for idx, img_path in enumerate(paths):
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"[WARN] Cannot read {img_path}")
            continue

        t0      = time.perf_counter()
        results = model.predict(frame, conf=conf, imgsz=imgsz, verbose=False)
        ms      = (time.perf_counter() - t0) * 1000

        if debug:
            debug_print(results, idx, every_n=1)   # every image in debug mode

        frame, n = draw_detections(frame, results)

        h_f, w_f = frame.shape[:2]
        cv2.rectangle(frame, (0, 0), (w_f, 38), (20, 20, 20), -1)
        banner = f"{img_path.name}  |  {ms:.0f}ms  |  Det:{n}  |  conf>={conf:.2f}  |  [Q]quit [any]next"
        cv2.putText(frame, banner, (8, 26), FONT, 0.55, (220, 220, 220), 1, cv2.LINE_AA)

        print(f"  [{idx+1}/{len(paths)}] {img_path.name}  →  {n} detection(s)  ({ms:.0f} ms)")

        if save:
            out_path = img_path.stem + "_detected" + img_path.suffix
            cv2.imwrite(str(out_path), frame)
            print(f"    saved → {out_path}")

        max_w, max_h = 1280, 720
        fh, fw = frame.shape[:2]
        if fw > max_w or fh > max_h:
            scale = min(max_w / fw, max_h / fh)
            frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)))

        cv2.imshow("DronaCharya – detector", frame)
        if cv2.waitKey(0) & 0xFF in (ord("q"), ord("Q"), 27):
            break

    cv2.destroyAllWindows()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Test DronaCharya target-detection model.")
    p.add_argument("--model",  default=DEFAULT_MODEL, help="Path to .pt weights.")
    p.add_argument("--source", default=None,          help="Image / video / folder. Omit → webcam.")
    p.add_argument("--conf",   type=float, default=DEFAULT_CONF,  help="Confidence threshold (default 0.10).")
    p.add_argument("--imgsz",  type=int,   default=DEFAULT_IMGSZ, help="Inference image size.")
    p.add_argument("--save",   action="store_true",   help="Save annotated output.")
    p.add_argument("--debug",  action="store_true",   help="Print raw confidence scores per frame.")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"[ERROR] Model not found: {model_path.resolve()}")
        print("  Make sure you run from the DronaCharya project root.")
        sys.exit(1)

    print(f"[INFO] Loading model : {model_path.resolve()}")
    model = YOLO(str(model_path))
    print(f"[INFO] Classes       : {model.names}")
    print(f"[INFO] Conf threshold: {args.conf}")
    print(f"[INFO] Debug mode    : {args.debug}")
    print()

    if args.source is None:
        run_webcam(model, args.conf, args.imgsz, args.debug)
    else:
        src = Path(args.source)
        if src.suffix.lower() in {".mp4", ".avi", ".mov", ".mkv", ".webm"}:
            run_video(str(src), model, args.conf, args.imgsz, args.save, args.debug)
        else:
            run_images(str(src), model, args.conf, args.imgsz, args.save, args.debug)


if __name__ == "__main__":
    main()
