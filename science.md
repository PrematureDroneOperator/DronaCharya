# DronaCharya Science Note

## Problem Framing

Classical map-stitch-first pipelines are slow and brittle in dynamic field conditions. We replaced that with a direct detection-to-geotag strategy to reduce latency and simplify operational reliability.

## Core Logic

1. Drone starts a survey session.
2. Video recording runs continuously on the Jetson.
3. YOLOv8 runs in parallel on incoming frames.
4. For each detection:
   - if GPS fix is valid, store a geotagged point
   - if GPS fix is not valid, skip the row and count it as GPS-skipped
5. On stop:
   - cluster nearby detections (dedup by geodesic radius)
   - compute optimal visit order with TSP (nearest-neighbor + 2-opt improvement)
   - build a closed Hamiltonian cycle (return to start)
   - generate two plain-white diagnostic graphs:
     - raw points graph
     - TSP cycle graph with numbered order

## Why This Is Better

- Lower pipeline complexity: no dependency on stitched mosaics.
- Faster decision loop: detections are geotagged during flight.
- Better mission utility: route is directly actionable by autopilot.
- Better explainability: judges can see raw detections and optimized route side-by-side.

## TSP and Hamiltonian Cycle Interpretation

- Node: one deduplicated target location.
- Edge weight: Euclidean distance in local 2D projection from GPS.
- Objective: minimize total path length while visiting each node exactly once.
- We enforce cycle closure so mission includes the return leg to start.

## Session Reproducibility

Each run is isolated in `data/target_sessions/session-XXXX/` and includes:

- raw detections (`raw_detections.csv/json`)
- deduplicated targets (`unique_targets.csv/json`)
- route (`route_tsp_cycle.json`)
- visuals (`graphs/raw_points.png`, `graphs/tsp_cycle.png`)
- metadata (`metadata.json`)

This makes results auditable and easy to present.

## Operational Impact

During competition/demo:

1. Fly survey and detect.
2. Stop survey and instantly obtain route + graphs.
3. Trigger mission replay from the latest completed session.
4. Drone visits targets in optimized numeric order and returns to start.

---

## Survey Workflow — Detailed Technical Breakdown

### Overview

The survey system is implemented in `survey/session_manager.py` (`SurveySessionManager`) and orchestrated by `core/controller.py` (`DroneAcharyaController`). It is a fully concurrent, multi-threaded pipeline that runs three parallel threads during an active survey.

```
START_SURVEY command
        │
        ▼
┌──────────────────────┐
│  _record_thread      │  captures frames from camera → frame queue
├──────────────────────┤
│  _detect_thread      │  consumes frame queue → runs YOLOv8 → raw detections list
├──────────────────────┤
│  _gps_thread         │  polls MAVLink GPS_RAW_INT → latest GPS cache
└──────────────────────┘
        │
        ▼ (STOP_SURVEY command)
┌──────────────────────┐
│  _finalize_session() │  cluster → TSP solve → write files → render graphs
└──────────────────────┘
        │
        ▼
   route_tsp_cycle.json  (ready for START_MISSION)
```

---

### Phase 1 — Session Initialisation (`start_survey`)

When `START_SURVEY` is received:

1. **Camera opens** — `DroneRecorder` opens the configured source (`stream_url` or `device_id`). A new recording session folder is created under `data/recordings/`.
2. **Session directory created** — `_next_target_session_dir()` scans `data/target_sessions/` for existing `session-XXXX` folders and creates the next one (e.g. `session-0003/`). A `graphs/` subfolder is created immediately.
3. **`metadata.json` written** — An initial partial metadata file is written at start time capturing: session ID, UTC start timestamp, model path, confidence threshold, class name, `inference_every_n`, `dedup_radius_m`, `min_gps_fix_type`.
4. **Three threads launched** — `_record_thread`, `_detect_thread`, `_gps_thread` all start and run concurrently.
5. **`AppState` updated** → `survey_state=RUNNING`, `recording_state=RECORDING`, `mission_state=SURVEY_RUNNING`.

---

### Phase 2 — The Three Concurrent Threads

#### Thread 1: `_record_loop` (Camera → Frame Queue)

- Continuously calls `recorder.record_frame()`, which captures a raw BGR frame from the camera.
- Only every `inference_every_n`-th frame (config: `survey.inference_every_n`, default `1`) is pushed into the **bounded frame queue** (`maxsize=8`).
- Skipped frames are **not dropped from recording** — the full video is always saved. Only the frequency of *inference* is controlled.
- If the queue is full (detector is behind), the frame is dropped and `_dropped_frame_count` increments.
- On stop, `recorder.stop()` finalises the `.avi` video file and the path is stored in `_current_video_path`.

#### Thread 2: `_detect_loop` (Frame Queue → Raw Detections)

- Blocks on `_frame_queue.get()` waiting for frames.
- For each frame, calls `FrameYoloDetector.detect_frame(frame)` — this runs YOLOv8 inference with the configured `conf_threshold` and returns a list of detections.
- **Center-region gate** (applied before geotagging): the bounding-box center `(pixel_x, pixel_y)` is checked against the frame's center region defined by `survey.center_region_ratio` (default `0.4` = middle 40 % of width and height). Detections outside this region are **silently skipped** and `_center_skipped_count` increments. This ensures only targets near the drone's nadir are geotagged, minimising GPS-to-target offset error.
- For each detection that passes the center gate, calls `_append_raw_detection()`:
  - **GPS gate**: reads `_latest_gps` (shared, updated by GPS thread). If GPS is `None` (no fix yet), the detection is **silently skipped** and `_gps_skipped_count` increments. This is the key guard that prevents spurious geotagging.
  - If GPS is valid: stamps the detection with `(frame_idx, timestamp_utc, class_name, confidence, latitude, longitude, gps_fix_type, pixel_x, pixel_y, bbox_x1, bbox_y1, bbox_x2, bbox_y2)` and appends to `_raw_detections`.
- Keeps draining the queue even after `_detect_stop_event` is set — processes all queued frames before exiting.

#### Thread 3: `_gps_loop` (MAVLink → GPS Cache)

- Connects to MAVLink (`config.mission.mavlink_connection`, default `udp:127.0.0.1:14550`).
- Polls for `GPS_RAW_INT` MAVLink messages in a tight loop (1s timeout per poll).
- **Fix quality filter**: only accepts GPS readings where `fix_type >= min_gps_fix_type` (config default: `3` = 3D fix). Readings with `fix_type < 3` (no fix or 2D only) are discarded.
- Converts raw integer coordinates: `lat = msg.lat / 1e7`, `lon = msg.lon / 1e7`.
- Writes the payload into `_latest_gps` (thread-safe, under `_state_lock`).
- **Start position capture**: the *first* valid GPS reading during a survey is saved as `_start_position`. This becomes the TSP route's origin and the return waypoint.
- On connection failure, waits 2s and retries automatically.

---

### Phase 3 — Stopping the Survey (`stop_survey`)

When `STOP_SURVEY` is received:

1. `_record_stop_event` is set → record thread exits, video is saved.
2. Record thread joined (up to 10s).
3. `_detect_stop_event` is set → detect thread drains queue then exits.
4. Detect thread joined (up to 20s, to allow queue drain).
5. `_gps_stop_event` is set → GPS thread closes MAVLink connection and exits.
6. GPS thread joined (up to 5s).
7. `_finalize_session()` is called.

---

### Phase 4 — Session Finalisation (`_finalize_session`)

This is the post-processing stage. It runs synchronously after all threads stop.

#### Step 4a — Deduplication Clustering (`_cluster_targets`)

Raw detections contain many hits on the same physical target from different frames and slightly different GPS readings (GPS jitter). The clustering step collapses these into unique physical targets.

**Algorithm: greedy single-pass nearest-neighbor clustering**

```
For each raw detection (lat, lon, conf):
    Find nearest existing cluster within dedup_radius_m (config default: 3.0 m)
    Distance measured by Haversine formula (true geodesic distance in metres)
    
    If within radius:
        Update cluster centre: running weighted mean of (lat, lon)
        Increment hit_count
        Accumulate confidence_sum, update max_confidence
        Update last_seen_utc
    
    If no cluster within radius:
        Create new cluster at this point
```

Each final cluster becomes one `unique_target` with:
- `target_id` (0-indexed integer)
- `latitude`, `longitude` (mean GPS position, 8 decimal places)
- `hit_count` (how many raw detections were merged)
- `avg_confidence`, `max_confidence`
- `first_seen_utc`, `last_seen_utc`

#### Step 4b — TSP Route Construction (`_build_route_payload`)

The goal is to find the shortest path visiting every unique target exactly once and returning to the start.

1. **Coordinate projection**: each target's GPS `(lat, lon)` is converted to a local 2D metric frame `(x_m, y_m)` relative to `start_position` using `CoordinateTransformer.gps_to_relative()`. This converts the spherical GPS problem to flat Euclidean geometry.

2. **TSP solve**: `TSPSolver.solve(targets_xy, start_xy=(0,0), include_return_to_start=True)` is called. The solver uses a **nearest-neighbour heuristic** followed by **2-opt improvement** to find a near-optimal Hamiltonian cycle.

3. **Waypoint assembly**: the ordered targets are converted to a `waypoints` list:
   - Each waypoint has `(index, visit_order, target_id, latitude, longitude, altitude_m, hover_time)`.
   - `altitude_m` and `hover_time` come from `config.mission.default_altitude_m` and `config.mission.hover_time_sec`.
   - A final `RETURN_START` waypoint is appended at the start GPS position to close the cycle.

#### Step 4c — Graph Rendering

Two diagnostic PNG images are generated on a plain white canvas (`graph_canvas_px` × `graph_canvas_px`, default 1200×1200px):

**`raw_points.png`** — scatter plot of all raw GPS detections:
- Blue filled circle = takeoff/start position, labelled `START`
- Red filled circles = each raw detection point, numbered 1–25 (if ≤ 25)
- Points are projected using `_GraphProjector` which fits all points into the canvas with a configurable margin, preserving aspect ratio

**`tsp_cycle.png`** — the optimised route:
- Blue filled circle = start position, labelled `START`
- Green lines connecting all nodes in TSP visit order, closing back to start
- Red filled circles = unique deduplicated target locations, numbered by visit order

#### Step 4d — Artifact Writing

All outputs are written to `data/target_sessions/session-XXXX/`:

| File | Content |
|---|---|
| `raw_detections.csv` | Every individual YOLO detection with GPS stamp and bbox coords |
| `raw_detections.json` | Same data in JSON with count and UTC timestamp |
| `unique_targets.csv` | Deduplicated targets with `target_id`, coords, `hit_count`, confidence stats |
| `unique_targets.json` | Same in JSON |
| `route_tsp_cycle.json` | Full TSP route: `ordered_targets`, `waypoints`, `total_distance_m`, `start_position`, `closed_cycle: true` |
| `metadata.json` | Complete session metadata: all config params, frame counts, GPS skip counts, graph paths, artifact paths, UTC timestamps |
| `graphs/raw_points.png` | Raw GPS scatter plot |
| `graphs/tsp_cycle.png` | Optimised TSP route visualisation |

---

### Phase 5 — Mission Execution (`START_MISSION`)

After `STOP_SURVEY`, the controller holds `_ordered_waypoints` in memory. When `START_MISSION` is received:

1. If `_ordered_waypoints` is empty, `load_latest_route()` is called — it finds the latest `session-XXXX` folder that has a `route_tsp_cycle.json` and loads waypoints from it. This allows mission replay across restarts.
2. `MavlinkController` connects to ArduPilot/PX4 via MAVLink.
3. `MissionExecutor.execute()` sends the drone to each waypoint in `visit_order`, hovers for `hover_time_sec`, and proceeds to the next.
4. An `abort_checker` lambda is polled continuously — `ABORT` command sets `_abort_event` which stops the executor mid-flight.
5. The last waypoint is always `RETURN_START` (closes the Hamiltonian cycle).

---

### Configuration Parameters Reference

All survey parameters live in `config/config.yaml`:

| Key | Default | Effect |
|---|---|---|
| `survey.inference_every_n` | `1` | Run YOLO on every Nth frame (raise to reduce CPU load) |
| `survey.dedup_radius_m` | `3.0` | Cluster radius — detections within 3m of an existing cluster are merged |
| `survey.min_gps_fix_type` | `3` | Minimum MAVLink GPS fix type accepted (3 = 3D fix) |
| `survey.center_region_ratio` | `0.4` | Fraction of frame (0–1) around center — only detections within this region are geotagged |
| `survey.graph_canvas_px` | `1200` | Size of diagnostic graph images in pixels |
| `survey.graph_margin_px` | `60` | Margin padding inside graphs |
| `vision.conf_threshold` | `0.35` | YOLO minimum confidence to accept a detection |
| `vision.target_class_name` | `"targetrotation"` | Class name filter — only this class is geotagged |
| `mission.default_altitude_m` | `15.0` | Altitude assigned to all waypoints in the route |
| `mission.hover_time_sec` | `5` | How long to hover at each target waypoint |

---

### State Machine Summary

```
IDLE
 │  START_SURVEY
 ▼
SURVEY_RUNNING  (3 threads active)
 │  STOP_SURVEY
 ▼
SURVEY_STOPPING (threads joining, finalize running)
 │  done
 ▼
ROUTE_READY  (waypoints in memory, graphs on disk)
 │  START_MISSION
 ▼
MISSION_RUNNING  (MissionExecutor sending MAVLink waypoints)
 │  done / ABORT
 ▼
MISSION_COMPLETE / ABORT_REQUESTED
```

