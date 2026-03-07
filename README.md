# dronAcharya

`dronAcharya` is an onboard autonomous survey, target geotagging, route planning, and MAVLink mission execution stack for NVIDIA Jetson Nano.

## Current Mission Flow

The active runtime flow is split across two Python runtimes:

- Native Jetson app (Python 3.6): camera, MAVLink/GPS, survey/session management, route generation, mission execution.
- Detector service (Python 3.12 conda): YOLO inference over localhost TCP JSON IPC.

Session flow:

1. Start survey from GCS (`START_SURVEY`).
2. Jetson records video locally and streams sampled frames to detector service.
3. Each valid detection is geotagged with live GPS and stored as raw points.
4. Stop survey (`STOP_SURVEY`) to auto-finalize:
   - deduplicated target list
   - TSP Hamiltonian cycle route
   - raw graph + TSP graph
5. Start mission (`START_MISSION`) to fly the saved numbered order, including return-to-start leg.

The legacy map stitching modules are still in the repo but no longer used by the active control flow.

## Folder Structure

```text
dronAcharya/
├── config/
│   └── config.yaml
├── core/
│   ├── controller.py
│   ├── main.py
│   └── mode_selector.py
├── survey/
│   └── session_manager.py
├── planning/
│   ├── coordinate_transform.py
│   └── tsp_solver.py
├── navigation/
│   ├── mavlink_controller.py
│   └── mission_executor.py
├── vision/
│   ├── frame_yolo_detector.py
│   ├── recorder.py
│   ├── frame_extractor.py
│   └── yolo_detector.py
├── telemetry/
│   ├── command_listener.py
│   └── telemetry_server.py
├── gcs/
│   └── gcs_app.py
├── ui/
│   ├── cli_interface.py
│   └── gui_app.py
├── mapping/                  # kept for reference, not active runtime
│   ├── mapper.py
│   └── stitching.py
├── data/
│   ├── recordings/
│   ├── target_sessions/
│   ├── routes/
│   ├── detections/
│   └── logs/
└── training/
```

## Setup (Jetson Ubuntu)

```bash
cd dronAcharya
chmod +x setup.sh
./setup.sh
```

Notes:
- Python 3.6 syntax compatibility is preserved.
- YOLO/Ultralytics is optional in base requirements.
- On Jetson, prefer system OpenCV/NumPy packages.

## Run

Start detector service (Jetson, Python 3.12 conda):

```bash
cd dronAcharya
conda activate <your-py312-env>
pip install -r requirements-detector.txt
python3 vision/detector_service.py --config config/config.yaml
```

Keep this service running before sending `START_SURVEY`.

Drone application:

```bash
cd dronAcharya
python3 main.py
```

GCS application:

```bash
cd dronAcharya
python3 gcs/gcs_app.py
```

## Connection Mechanism (Updated)

Transport now assumes telemetry radio is connected through Pixhawk routing:

- GCS side: radio on COM/serial -> `telemetry/radio_bridge.py --role gcs`
- Drone side: Jetson serial link to Pixhawk -> `telemetry/radio_bridge.py --role drone`
- App ports remain unchanged (`14560` commands, `14561` telemetry on localhost)

See full setup instructions in `setup.md`.

## Commands

### Canonical telemetry commands

- `START_SURVEY`
- `STOP_SURVEY`
- `BUILD_ROUTE`
- `START_RECORDING` (video-only mode)
- `STOP_RECORDING` (video-only mode)
- `START_MISSION`
- `ABORT`
- `STATUS_REQUEST`

### Backward-compatible aliases

- `START_MAPPING -> START_SURVEY`
- `RUN_DETECTION -> BUILD_ROUTE`
- `PLAN_ROUTE -> BUILD_ROUTE`

### CLI commands

- `start_survey`
- `stop_survey`
- `start_recording`
- `stop_recording`
- `build_route`
- `start_mission`
- `status`
- `abort`
- `exit`

Aliases in CLI:
- `map -> start_survey`
- `detect -> build_route`
- `plan -> build_route`

## Video-Only Recording (Legacy Mode)

Use standalone recording when you only want video + extracted frames and do not want survey detections/route generation.

Artifacts are stored in:

`data/recordings/session-XXXX/`

## Session Artifacts

All survey results are partitioned by `session-XXXX` under:

`data/target_sessions/`

Example:

```text
data/target_sessions/
└── session-0004/
    ├── metadata.json
    ├── raw_detections.csv
    ├── raw_detections.json
    ├── unique_targets.csv
    ├── unique_targets.json
    ├── route_tsp_cycle.json
    └── graphs/
        ├── raw_points.png
        └── tsp_cycle.png
```

### Graph location (for judges/demo)

- Raw point graph: `data/target_sessions/session-XXXX/graphs/raw_points.png`
- TSP Hamiltonian-cycle graph: `data/target_sessions/session-XXXX/graphs/tsp_cycle.png`

## CSV / JSON Schemas

### `raw_detections.csv`

Columns:

`frame_idx,timestamp_utc,class_name,confidence,latitude,longitude,gps_fix_type,pixel_x,pixel_y,bbox_x1,bbox_y1,bbox_x2,bbox_y2`

### `unique_targets.csv`

Columns:

`target_id,latitude,longitude,hit_count,avg_confidence,max_confidence,first_seen_utc,last_seen_utc`

### `route_tsp_cycle.json`

Main fields:

- `session_id`
- `timestamp_utc`
- `closed_cycle`
- `total_targets`
- `total_distance_m`
- `start_position`
- `ordered_targets`
- `waypoints`

## Mission Replay Behavior

When `START_MISSION` is triggered:

1. If in-memory waypoints exist, use them.
2. Otherwise auto-load the latest completed session route from `data/target_sessions/session-XXXX/route_tsp_cycle.json`.
3. Execute numbered TSP order and include return-to-start waypoint.

## Survey Configuration

`config/config.yaml` includes:

```yaml
survey:
  sessions_dir: "data/target_sessions"
  inference_every_n: 1
  detection_interval_sec: 0.5
  gps_preflight_required: true
  gps_preflight_timeout_sec: 12.0
  dedup_radius_m: 3.0
  graph_canvas_px: 1200
  graph_margin_px: 60
  min_gps_fix_type: 3
```

Detector IPC settings:

```yaml
detector_service:
  host: "127.0.0.1"
  port: 17660
  request_timeout_sec: 8.0
  connect_timeout_sec: 2.0
  jpeg_quality: 80
  enabled: true
```

## Training Pipeline

Target model training modules remain under `training/`.

Main scripts:
- `python -m training.prepare_raw_media`
- `python -m training.coco_to_yolo`
- `python -m training.split_yolo_dataset`
- `python -m training.validate_yolo_dataset`
- `python -m training.train_yolo`
- `python -m training.evaluate_yolo`
- `python -m training.export_model`
