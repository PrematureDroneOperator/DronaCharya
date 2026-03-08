import cv2
from pathlib import Path
from utils.config import load_config
from vision.frame_yolo_detector import FrameYoloDetector
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_image")

# 1. Load config and model
config = load_config(config_path="config/config.yaml", base_dir=Path("."))
config.vision.target_class_name = "" # Disable filter
detector = FrameYoloDetector(config, logger)
detector._load_model()

# 2. Load your test image
frame = cv2.imread("test_target.jpg")
if frame is None:
    print("Could not load test_target.jpg!")
    exit()

# 3. Detect and save
detections = detector.detect_frame(frame)
print(f"Detections found: {detections}")

annotated = detector.annotate_frame(frame, detections)
cv2.imwrite("test_target_output.jpg", annotated)
print("Saved annotated image to test_target_output.jpg. Open it to see if boxes were drawn!")
