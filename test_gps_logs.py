"""
test_gps_logs.py

Test script to test continuous GPS connection without the overhead of the surveyor.
Logs each valid GPS reading to a local folder logs/gps_session-XXXX until stopped.
Designed to be callable via a command and integrated into the GCS interface.
"""

import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
import json
import threading
import signal

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from navigation.mavlink_controller import MavlinkController

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def setup_gps_logger(session_dir: Path) -> logging.Logger:
    logger = logging.getLogger("test_gps")
    logger.setLevel(logging.DEBUG)
    
    session_dir.mkdir(parents=True, exist_ok=True)
    
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    fh = logging.FileHandler(session_dir / "gps_tracker.log")
    fh.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    ch.setFormatter(formatter)
    fh.setFormatter(formatter)
    
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

def get_next_gps_session_dir(logs_dir: Path) -> Path:
    existing = []
    if logs_dir.exists():
        for entry in logs_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("gps_session-"):
                try:
                    num = int(entry.name.split("-")[1])
                    existing.append(num)
                except ValueError:
                    pass
    last_num = max(existing) if existing else -1
    return logs_dir / f"gps_session-{last_num + 1:04d}"

class GPSTracker:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.stop_event = threading.Event()
        self.mavlink = None
        
    def start_tracking(self, session_dir: Path):
        self.logger.info("Initializing MAVLink controller for GPS tracking...")
        self.mavlink = MavlinkController(
            connection_string=self.config.mission.mavlink_connection,
            baudrate=self.config.mission.mavlink_baudrate,
            logger=self.logger
        )
        
        gps_history = []
        json_path = session_dir / "gps_history.json"
        
        try:
            self.logger.info(f"Attempting to connect to {self.config.mission.mavlink_connection}...")
            self.mavlink.connect(timeout_sec=10)
            self.logger.info("Connection established. Waiting for valid GPS fixes...")
            
            while not self.stop_event.is_set():
                start_time = time.monotonic()
                try:
                    msg = self.mavlink.recv_match(types="GPS_RAW_INT", timeout=1.0)
                except Exception as e:
                    self.logger.warning(f"Failed to read from MAVLink: {e}")
                    time.sleep(1.0)
                    continue
                    
                if not msg:
                    continue
                    
                fix_type = int(getattr(msg, "fix_type", 0))
                
                payload = {
                    "timestamp_utc": _utc_now_iso(),
                    "latitude": float(getattr(msg, "lat", 0.0)) / 1e7,
                    "longitude": float(getattr(msg, "lon", 0.0)) / 1e7,
                    "altitude_m": float(getattr(msg, "alt", 0.0)) / 1000.0,
                    "fix_type": fix_type,
                    "satellites_visible": int(getattr(msg, "satellites_visible", 0))
                }
                
                if fix_type >= self.config.survey.min_gps_fix_type:
                    self.logger.info(f"VALID FIX [Type {fix_type}] Lat/Lon: ({payload['latitude']:.6f}, {payload['longitude']:.6f}) Alt: {payload['altitude_m']:.2f}m")
                else:
                    self.logger.warning(f"NO FIX [Type {fix_type}] Sats: {payload['satellites_visible']}")
                    
                gps_history.append(payload)
                
                # Periodically sync to disk inside the loop
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({"session": session_dir.name, "count": len(gps_history), "history": gps_history}, f, indent=2)
                
                # Sleep to enforce 1 per second matching user request
                elapsed = time.monotonic() - start_time
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
                    
        except KeyboardInterrupt:
            pass
        except Exception as e:
            self.logger.error(f"GPS Tracking aborted due to error: {e}")
        finally:
            self.logger.info(f"Stopping tracker. Logged {len(gps_history)} updates to {json_path}")
            if self.mavlink:
                try:
                    self.mavlink.close()
                except:
                    pass

def main():
    config = load_config(config_path=PROJECT_ROOT / "config" / "config.yaml", base_dir=PROJECT_ROOT)
    
    logs_dir = PROJECT_ROOT / "logs"
    session_dir = get_next_gps_session_dir(logs_dir)
    logger = setup_gps_logger(session_dir)
    
    logger.info(f"Started continuous GPS logger in {session_dir}")
    
    tracker = GPSTracker(config, logger)
    
    def handle_sigint(sig, frame):
        logger.info("Received stop signal. Shutting down cleanly...")
        tracker.stop_event.set()
        
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    
    tracker.start_tracking(session_dir)

if __name__ == "__main__":
    main()
