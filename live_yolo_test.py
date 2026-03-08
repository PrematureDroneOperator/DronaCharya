"""
live_yolo_test.py

A standalone script that directly opens the drone's configured camera, 
runs the frames through the YOLO detector, and displays the live feed 
on your screen with bounding boxes drawn over detected targets.

Useful for debugging "drone is not detecting targets" by physically
holding a target in front of the camera and verifying the model's output.
"""

import sys
import time
import cv2
import logging
from pathlib import Path

# Setup paths so we can import DronaCharya modules
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from vision.recorder import DroneRecorder
from vision.frame_yolo_detector import FrameYoloDetector

import argparse

def main():
    parser = argparse.ArgumentParser(description="Live camera YOLO debugger for Jetson")
    parser.add_argument("--no-preview", action="store_true", help="Disable cv2.imshow (useful for headless SSH)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("live_yolo")

    logger.info("Loading config...")
    config = load_config(config_path=PROJECT_ROOT / "config" / "config.yaml", base_dir=PROJECT_ROOT)

    logger.info("Initializing YOLO Detector (loading model... this may take a few seconds)")
    
    # We deliberately clear the target class name in this script so that ALL detections
    # are shown on screen. This helps debug if the model uses a different class name.
    original_target_class = config.vision.target_class_name
    config.vision.target_class_name = ""
    
    detector = FrameYoloDetector(config, logger)
    detector._load_model()
    logger.info("Model loaded successfully!")

    source = config.camera.stream_url.strip() or config.camera.device_id
    logger.info(f"Opening camera source: {source}")

    recorder = DroneRecorder(source=source, fps=30, output_dir=PROJECT_ROOT / "logs", auto_extract=False)
    
    try:
        recorder.start()
    except Exception as e:
        logger.error(f"Failed to start camera: {e}")
        return

    logger.info("=========================================================")
    logger.info(" LIVE YOLO TEST RUNNING ")
    logger.info(" Target class expected by config: '%s'", original_target_class)
    logger.info(" Minimum Confidence expected: %.2f", config.vision.conf_threshold)
    if not args.no_preview:
        logger.info(" Press 'q' to quit the window.")
    else:
        logger.info(" Preview disabled (--no-preview). Press Ctrl+C to quit.")
    logger.info("=========================================================")

    preview_failed = False

    try:
        while True:
            ok, frame, frame_idx, frame_ts = recorder.record_frame(include_frame=True)
            if not ok or frame is None:
                logger.warning("Failed to grab frame.")
                time.sleep(1.0)
                continue

            detections = detector.detect_frame(frame)
            
            if detections:
                logger.info(f"Frame {frame_idx} -> DETECTED {len(detections)} targets!")
                for d in detections:
                    logger.info(f"   - {d['class_name']}: {d['confidence']:.2f} at ({d['pixel_x']}, {d['pixel_y']})")

            # Draw boxes directly onto the frame
            annotated_frame = detector.annotate_frame(frame, detections)
            
            if not args.no_preview and not preview_failed:
                try:
                    display_frame = annotated_frame if annotated_frame is not None else frame
                    cv2.imshow("Live YOLO Debugger", display_frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        logger.info("Quit requested by user.")
                        break
                except Exception as e:
                    logger.warning(f"Failed to show video window (Headless/X11 issue?): {e}")
                    logger.warning("Falling back to text-only mode.")
                    preview_failed = True

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.error(f"Runtime error: {e}", exc_info=True)
    finally:
        logger.info("Releasing camera and closing windows...")
        recorder.stop()
        if not args.no_preview and not preview_failed:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

if __name__ == "__main__":
    main()
