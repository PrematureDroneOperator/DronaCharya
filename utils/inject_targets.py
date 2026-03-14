import os
import csv
import json
import re
from pathlib import Path
from datetime import datetime, timezone

# --- CONFIGURATION ---
BASE_DIR = Path(r"c:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya")
SESSIONS_DIR = BASE_DIR / "data" / "target_sessions"

CSV_DATA = """target_id,latitude,longitude,hit_count,avg_confidence,max_confidence,first_seen_utc,last_seen_utc
0,29.8672601,77.8994095,1,0.8022,0.8022,2026-03-13T10:55:13.114134+00:00,2026-03-13T10:55:13.114134+00:00
1,29.8677465,77.8993321,3,0.5615,0.7123,2026-03-13T10:55:15.188521+00:00,2026-03-13T10:56:13.177365+00:00
2,29.8680833,77.8990554,1,0.3724,0.3724,2026-03-13T10:57:43.220305+00:00,2026-03-13T10:57:43.220305+00:00"""

RAW_CSV_COLUMNS = [
    "frame_idx", "timestamp_utc", "class_name", "confidence", "latitude", "longitude",
    "gps_fix_type", "pixel_x", "pixel_y", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"
]

def get_next_session_dir() -> Path:
    existing = []
    if SESSIONS_DIR.exists():
        for entry in SESSIONS_DIR.iterdir():
            if entry.is_dir() and re.fullmatch(r"session-\d{4}", entry.name):
                existing.append(entry.name)
    last_num = max([int(name.split("-")[1]) for name in existing]) if existing else -1
    path = SESSIONS_DIR / "session-{0:04d}".format(last_num + 1)
    path.mkdir(parents=True, exist_ok=True)
    return path

def inject():
    session_dir = get_next_session_dir()
    print(f"Creating injection session: {session_dir.name}")
    
    # 1. Prepare raw_detections.csv
    raw_path = session_dir / "raw_detections.csv"
    lines = CSV_DATA.strip().split('\n')
    reader = csv.DictReader(lines)
    
    raw_rows = []
    for i, row in enumerate(reader):
        raw_rows.append({
            "frame_idx": i + 1,
            "timestamp_utc": row["first_seen_utc"],
            "class_name": "target",
            "confidence": row["max_confidence"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "gps_fix_type": 3, # Force 3D fix
            "pixel_x": 640,
            "pixel_y": 360,
            "bbox_x1": 600,
            "bbox_y1": 300,
            "bbox_x2": 680,
            "bbox_y2": 420
        })
        
    with raw_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(raw_rows)
        
    # 2. Create raw_detections.json for consistency
    raw_json_path = session_dir / "raw_detections.json"
    with raw_json_path.open("w", encoding="utf-8") as f:
        json.dump({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "count": len(raw_rows),
            "raw_detections": raw_rows
        }, f, indent=2)

    # 3. Create dummy metadata.json
    metadata_path = session_dir / "metadata.json"
    with metadata_path.open("w", encoding="utf-8") as f:
        json.dump({
            "session_id": session_dir.name,
            "survey_started_utc": datetime.now(timezone.utc).isoformat(),
            "injected": True
        }, f, indent=2)

    print(f"Successfully injected {len(raw_rows)} targets into {session_dir}")
    print("\nNext steps:")
    print("1. In your GCS/CLI, run: BUILD_ROUTE")
    print("2. Run: BUILD_MISSION")
    print("3. Run: START_MISSION")

if __name__ == "__main__":
    inject()
