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

