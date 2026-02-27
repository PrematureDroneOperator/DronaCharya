# dronAcharya

`dronAcharya` is an onboard autonomous mapping, target detection, route planning, and MAVLink mission execution stack designed for NVIDIA Jetson Nano.

The system supports:
- `GUI` mode (Tkinter)
- `CLI` mode (headless)

Startup always prompts for mode selection unless `--mode` is passed.

## Folder Structure

```text
dronAcharya/
├── config/
│   └── config.yaml
├── core/
│   ├── controller.py
│   ├── main.py
│   └── mode_selector.py
├── mapping/
│   ├── mapper.py
│   └── stitching.py
├── navigation/
│   ├── mavlink_controller.py
│   └── mission_executor.py
├── planning/
│   ├── coordinate_transform.py
│   └── tsp_solver.py
├── telemetry/
│   ├── command_listener.py
│   └── telemetry_server.py
├── ui/
│   ├── cli_interface.py
│   └── gui_app.py
├── utils/
│   ├── config.py
│   └── logger.py
├── vision/
│   ├── yolo_detector.py
│   ├── recorder.py
│   └── frame_extractor.py
├── gcs/
│   └── gcs_app.py
├── data/
│   ├── maps/
│   ├── detections/
│   ├── routes/
│   └── logs/
├── main.py
├── requirements.txt
└── setup.sh
```

## Setup (Jetson Ubuntu)

```bash
cd dronAcharya
chmod +x setup.sh
./setup.sh
```

## Run

Drone application:

```bash
cd dronAcharya
python3 main.py
```

Optional direct mode:

```bash
python3 main.py --mode gui
python3 main.py --mode cli
```

Ground Control Station app:

```bash
cd dronAcharya
python3 gcs/gcs_app.py
```

## Camera Recording

### Architecture — everything stays on the drone

```
┌─────────────────────────────────────┐         ┌───────────────────────┐
│  DRONE  (Jetson Nano)               │         │  GCS LAPTOP           │
│                                     │         │                       │
│  dronAcharya main.py                │         │  gcs_app.py           │
│    ├─ CommandListener               │◄─SiK───►│   ▶ Start Recording   │
│    └─ DroneRecorder  ←── triggers   │  Radio  │   ■ Stop  Recording   │
│           │                         │  only   │                       │
│           ▼                         │         │  Telemetry Feed shows │
│   data/recordings/session-XXXX/     │         │  session path +       │
│     ├── recording.mp4               │         │  frame count          │
│     └── frames/frame_000001.jpg …   │         │                       │
└─────────────────────────────────────┘         └───────────────────────┘
```

- **No video is sent over the radio.** The GCS sends only two small text commands.
- Files accumulate on the Jetson's filesystem. Retrieve them via SSH/SCP or a USB drive after the flight.
- The camera source (device index or internal pipeline) is configured **on the Jetson** — not on the GCS.

---

### Step 1 — Configure the camera source on the Jetson Nano

Open `config/config.yaml` **on the Jetson** and set `stream_url`:

```yaml
# config/config.yaml  ← edit this ON THE JETSON
camera:
  device_id: 0          # used when stream_url is empty
  stream_url: ""        # ← set to "rtsp://..." or leave "" for device_id
```

| `stream_url` value | Camera used |
|---|---|
| `""` (empty) | `device_id` — local V4L2/CSI device (`/dev/video0` etc.) |
| `"rtsp://..."` | Any RTSP source (another camera, GStreamer pipeline, etc.) |

For the Jetson CSI camera (ribbon-cable module) leave `stream_url` empty and `device_id: 0`.
For a USB webcam set `device_id: 0` (or whichever `/dev/videoX`).

### Step 2 — From the GCS app

Open the GCS app (`python3 gcs/gcs_app.py`) and connect to the drone.

| Button | Command sent over radio | What happens on the drone |
|---|---|---|
| **▶ Start Recording on Drone** | `START_RECORDING` | Opens camera, writes `session-XXXX/recording.mp4` at 30 FPS |
| **■ Stop Recording on Drone** | `STOP_RECORDING` | Stops capture; auto-extracts frames to `session-XXXX/frames/` |

The **Telemetry Feed** log will show:
```
[RX] LOG: Recording started → /home/nvidia/.../data/recordings/session-0000
[RX] LOG: Recording saved → recording.mp4 | 1234 frames extracted to frames/
```

### Step 3 — Retrieve files after the flight

```bash
# Copy the whole recordings directory via SSH
scp -r nvidia@<jetson-ip>:/home/nvidia/dronAcharya/data/recordings ./

# Or mount the Jetson over USB and copy manually
```

### Session layout (on the Jetson)

```text
data/recordings/
└── session-0000/
    ├── recording.mp4
    └── frames/
        ├── frame_000001.jpg
        ├── frame_000002.jpg
        └── …
```

Sessions auto-increment (`session-0001`, `session-0002`, …) and are never overwritten.

### Running the recorder manually on the Jetson (CLI)

```bash
# Run on Jetson – press q to stop
python3 -m vision.recorder

# Specify a camera device explicitly
python3 -m vision.recorder --source 0 --fps 30 --output-dir data/recordings

# Extract frames from an existing recording
python3 -m vision.frame_extractor data/recordings/session-0000/recording.mp4

# Extract every 5th frame only
python3 -m vision.frame_extractor recording.mp4 --out frames/ --every-n 5
```


# Option 3 – extract frames from an existing file
extractor = FrameExtractor(jpeg_quality=95)
count = extractor.extract("session-0000/recording.mp4", "session-0000/frames/")
print(f"{count} frames extracted")
```

### Session layout

```text
recordings/
└── session-0000/          ← auto-named, 4-digit zero-padded
    ├── recording.mp4
    └── frames/
        ├── frame_000001.jpg
        ├── frame_000002.jpg
        └── …
```

Sessions are numbered sequentially (`session-0000`, `session-0001`, …) — existing sessions are never overwritten.

## CLI Commands

- `map`
- `detect`
- `plan`
- `start_mission`
- `status`
- `exit`

## Telemetry Commands (GCS to Drone)

- `START_MAPPING`
- `RUN_DETECTION`
- `PLAN_ROUTE`
- `START_MISSION`
- `ABORT`
- `STATUS_REQUEST`

## Training Pipeline

A complete target-model training module is available under:
- `training/`

End-to-end guide:
- `training/README.md`

Main scripts:
- `python -m training.prepare_raw_media`
- `python -m training.coco_to_yolo`
- `python -m training.split_yolo_dataset`
- `python -m training.validate_yolo_dataset`
- `python -m training.train_yolo`
- `python -m training.evaluate_yolo`
- `python -m training.export_model`
