import cv2
from pathlib import Path
from utils.config import load_config
from vision.frame_yolo_detector import FrameYoloDetector
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_image")

# 1. Load config and model
config = load_config(config_path=Path("config/config.yaml"), base_dir=Path("."))
config.vision.target_class_name = "" # Disable filter
detector = FrameYoloDetector(config, logger)
detector._load_model()

# 2. Open the test video
video_path = "test_video.avi"
output_path = "test_video_output.avi"

cap = cv2.VideoCapture(video_path)
if not cap.isOpened():
    print(f"Could not open {video_path}!")
    exit()

# Get video properties for writer
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fourcc = cv2.VideoWriter_fourcc(*'MJPG')

writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

logger.info(f"Processing {video_path}... Please wait.")

# 3. Process frame by frame
frame_count = 0
detected_frames = 0

while True:
    ret, frame = cap.read()
    if not ret:
        break
        
    frame_count += 1
    
    # Run YOLO
    detections = detector.detect_frame(frame)
    if detections:
        detected_frames += 1
        
    # Draw boxes
    annotated = detector.annotate_frame(frame, detections)
    annotated = annotated if annotated is not None else frame
    
    # Save frame
    writer.write(annotated)
    
    # Print progress every 30 frames
    if frame_count % 30 == 0:
        logger.info(f"Processed {frame_count} frames... (targets found in {detected_frames} frames so far)")

cap.release()
writer.release()
logger.info(f"DONE! Processed {frame_count} total frames.")
logger.info(f"Targets were detected in {detected_frames} frames.")
logger.info(f"Saved annotated video to {output_path}. Play it to see the bounding boxes!")
