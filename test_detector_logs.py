"""
test_detector_logs.py

Test script to test detection pipeline with lacking GPS updates.
Logs each frame received, YOLO detector results, and outputs to logs/test_session-XXXX.
"""

import sys
import time
import logging
from pathlib import Path
import numpy as np
import cv2
import threading
import json

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from vision.detector_service import DetectorService
from survey.session_manager import SurveySessionManager

def setup_test_logger(session_dir: Path) -> logging.Logger:
    logger = logging.getLogger("test_detector")
    logger.setLevel(logging.DEBUG)
    
    # Create logs dir
    session_dir.mkdir(parents=True, exist_ok=True)
    
    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    # File handler
    fh = logging.FileHandler(session_dir / "detector_pipeline.log")
    fh.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    ch.setFormatter(formatter)
    fh.setFormatter(formatter)
    
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

def get_next_test_session_dir(logs_dir: Path) -> Path:
    existing = []
    if logs_dir.exists():
        for entry in logs_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("test_session-"):
                try:
                    num = int(entry.name.split("-")[1])
                    existing.append(num)
                except ValueError:
                    pass
    last_num = max(existing) if existing else -1
    return logs_dir / f"test_session-{last_num + 1:04d}"

def generate_test_image():
    # Create simple 640x480 test image
    img = np.full((480, 640, 3), 128, dtype=np.uint8)
    cv2.rectangle(img, (200, 200), (400, 400), (0, 0, 255), -1) # Red square
    return img

def main():
    config = load_config(config_path=PROJECT_ROOT / "config" / "config.yaml", base_dir=PROJECT_ROOT)
    
    logs_dir = PROJECT_ROOT / "logs"
    session_dir = get_next_test_session_dir(logs_dir)
    logger = setup_test_logger(session_dir)
    
    logger.info(f"Starting test session in {session_dir}")
    
    # Override config for testing avoiding real MAVLink connection
    config.survey.gps_preflight_required = False
    
    test_manager = SurveySessionManager(config=config, logger=logger)
    
    # Start survey logic
    logger.info("Initializing survey session (this spins up _record_loop, _detect_loop, _gps_loop)")
    result = test_manager.start_survey()
    real_session_dir = result.get("session_dir")
    logger.info(f"SurveyManager created internal target session at: {real_session_dir}")
    
    # Wait for detector to connect
    time.sleep(2)
    
    logger.info("Injecting 5 simulated frames...")
    
    try:
        for i in range(1, 6):
            # We bypass the hardware recorder by injecting directly into the queue
            frame = generate_test_image()
            frame_ts = time.time()
            
            logger.debug(f"[Queue] Injecting Frame {i} into frame queue")
            try:
                test_manager._frame_queue.put((i, str(frame_ts), frame), timeout=1.0)
                logger.info(f"Frame {i} successfully enqueued for YOLO.")
            except Exception as e:
                logger.error(f"Failed to enqueue frame {i}: {e}")
                
            time.sleep(0.5)
            
            # Print detector status
            status = test_manager.get_detector_status()
            logger.info(f"Detector Status: hits={test_manager._detector_hit_count}, dropped_inference={status['inference_dropped_count']}")
            
        time.sleep(2)
    except KeyboardInterrupt:
        logger.info("Interrupted test.")
        
    finally:
        logger.info("Stopping survey session...")
        test_manager.stop_survey()
        
        # Open and log results from the tracker
        try:
            with open(Path(real_session_dir) / "raw_detections.json", "r") as f:
                data = json.load(f)
                num_raw = len(data.get("raw_detections", []))
                logger.info(f"Session finalizing complete. Total raw detections recorded (even without GPS): {num_raw}")
                if num_raw > 0:
                    logger.info("SUCCESS: Detections are being successfully tracked without requiring valid GPS!")
                else:
                    logger.error("FAILURE: No raw detections recorded. GPS bypass might not be working.")
        except Exception as e:
            logger.error(f"Could not read raw_detections.json: {e}")

if __name__ == "__main__":
    main()
