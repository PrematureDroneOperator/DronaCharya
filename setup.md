# 🛸 DronaCharya — GCS ↔ Drone Connection Setup Guide

> **Hardware topology:**  
> SiK Telemetry Radio connected **directly to Jetson Nano** (USB or UART pins).  
> No Pixhawk or MAVProxy in the loop — one radio link carries everything.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [How the Radio Bridge Works](#2-how-the-radio-bridge-works)
3. [Prerequisites](#3-prerequisites)
4. [Step 1 — Install Dependencies (Both Sides)](#4-step-1--install-dependencies-both-sides)
5. [Step 2 — Identify Serial Ports](#5-step-2--identify-serial-ports)
6. [Step 3 — Configure config.yaml on the Jetson](#6-step-3--configure-configyaml-on-the-jetson)
7. [Step 4 — Start the Radio Bridge on the Jetson (Drone side)](#7-step-4--start-the-radio-bridge-on-the-jetson-drone-side)
8. [Step 5 — Start the Radio Bridge on the Laptop (GCS side)](#8-step-5--start-the-radio-bridge-on-the-laptop-gcs-side)
9. [Step 6 — Start DronaCharya on the Jetson](#9-step-6--start-dronacharya-on-the-jetson)
10. [Step 7 — Start the GCS App on the Laptop](#10-step-7--start-the-gcs-app-on-the-laptop)
11. [Verifying the Connection End-to-End](#11-verifying-the-connection-end-to-end)
12. [Debug Tips & Troubleshooting](#12-debug-tips--troubleshooting)
13. [Camera Configuration for Recording](#13-camera-configuration-for-recording)

---

## 1. System Architecture

```
┌─────────────────────────────┐     SiK Radio Link (serial/RF)    ┌──────────────────────────────────┐
│       LAPTOP (GCS)          │◄──────────────────────────────────►│          DRONE (Jetson Nano)     │
│                             │                                     │                                  │
│  gcs_app.py   (UDP)         │                                     │  main.py  — DronaCharya (UDP)    │
│      ↕  localhost           │                                     │      ↕  localhost                │
│  radio_bridge.py            │                                     │  radio_bridge.py                 │
│      ↕  serial              │                                     │      ↕  serial                   │
│  [SiK USB dongle]  ═══════════════════ RF ═══════════════════  [SiK radio on /dev/ttyUSB0]          │
└─────────────────────────────┘                                     └──────────────────────────────────┘
```

### Port map (both sides talk to localhost — the bridge handles the radio)

| Traffic | Port | Direction |
|---------|------|-----------|
| GCS → Drone commands | **14560** | gcs_app → bridge → radio → bridge → DronaCharya |
| Drone → GCS telemetry | **14561** | DronaCharya → bridge → radio → bridge → gcs_app |

---

## 2. How the Radio Bridge Works

`telemetry/radio_bridge.py` is a small Python script that acts as a transparent relay between UDP (what the app speaks) and serial (what the SiK radio speaks).

- It runs on **both** the Jetson and the laptop.  
- Each side uses a different `--role` flag.
- Packets are **newline-delimited** on the wire — compatible with standard serial monitors.

```
Drone side                                GCS side
──────────────────────────────────────    ──────────────────────────────────────
DronaCharya sends telemetry to            gcs_app sends commands to
  UDP 127.0.0.1:14561                       UDP 127.0.0.1:14560
      ↓                                         ↓
bridge listens on UDP :14561              bridge listens on UDP :14560
bridge writes to serial /dev/ttyUSB0     bridge writes to serial COM5
      ──────────────── RF ────────────────────────
bridge reads from serial /dev/ttyUSB0    bridge reads from serial COM5
bridge sends to UDP 127.0.0.1:14560      bridge sends to UDP 127.0.0.1:14561
      ↓                                         ↓
DronaCharya listens on :14560            gcs_app listens on :14561
```

---

## 3. Prerequisites

### Hardware

- [ ] SiK radio (air unit) connected to Jetson Nano:
  - Via **USB** → appears as `/dev/ttyUSB0` (easiest, recommended)
  - Via **UART header pins** → appears as `/dev/ttyTHS1`
- [ ] SiK radio (ground unit) plugged into laptop USB
  - Appears as `COM5` (or similar) in Windows Device Manager
- [ ] Both radios **configured to same channel and baud rate**  
  (factory default: channel 0, baud 57600 — usually works out of the box)

### Software

- [ ] Python 3.9+ on both machines
- [ ] `pip install -r requirements.txt` run on both (includes `pyserial`)

---

## 4. Step 1 — Install Dependencies (Both Sides)

### On Jetson Nano

```bash
cd ~/DronaCharya
pip3 install -r requirements.txt
# Or install pyserial directly:
pip3 install pyserial
```

### On Laptop (Windows)

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
pip install -r requirements.txt
```

---

## 5. Step 2 — Identify Serial Ports

### Jetson Nano — find the SiK radio port

```bash
# Unplug and replug the radio, then run:
dmesg | tail -20
# Look for a line like: usb 1-1: cp210x converter now attached to ttyUSB0

# Or list all serial devices:
ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyTHS*
```

Most common result: `/dev/ttyUSB0`

Grant your user permanent access:
```bash
sudo usermod -aG dialout $USER
# Log out and back in, or run immediately:
sudo chmod a+rw /dev/ttyUSB0
```

### Laptop (Windows) — find the COM port

1. Open **Device Manager** → **Ports (COM & LPT)**
2. Look for **Silicon Labs CP210x** or **FTDI USB Serial Device**
3. Note the number, e.g., `COM5`

Or from PowerShell:
```powershell
Get-WMIObject Win32_SerialPort | Select-Object Name, DeviceID, Description
```

If you see nothing, install the CP210x driver:  
👉 https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers

---

## 6. Step 3 — Configure config.yaml on the Jetson

Because the bridge runs **locally** on both machines, all IPs are `127.0.0.1`.  
Edit `config/config.yaml` on the **Jetson Nano**:

```yaml
telemetry:
  command_host: "0.0.0.0"        # DronaCharya listens on all interfaces
  command_port: 14560             # bridge forwards radio packets here
  gcs_host: "127.0.0.1"          # bridge picks up telemetry from here (LOCAL)
  gcs_port: 14561                 # bridge picks up telemetry from this port
```

> ✅ No IP addresses of the laptop needed — the SiK radio link handles the transport.

For the `mission` block (MAVLink to flight controller, if you have one connected):
```yaml
mission:
  mavlink_connection: "udp:127.0.0.1:14550"  # If using FC via MAVProxy
  # OR direct serial if FC is connected to Jetson:
  # mavlink_connection: "/dev/ttyTHS1"
  mavlink_baudrate: 57600
```

---

## 7. Step 4 — Start the Radio Bridge on the Jetson (Drone side)

SSH into the Jetson:
```powershell
ssh username@<JETSON_IP>
```

Start the bridge inside a `tmux` session (survives SSH disconnect):
```bash
tmux new -s bridge
cd ~/DronaCharya
python3 telemetry/radio_bridge.py --port /dev/ttyUSB0 --baud 57600 --role drone --verbose
# Detach: Ctrl+B, then D
```

**Expected output:**
```
10:15:32  INFO      radio_bridge  Opening serial port /dev/ttyUSB0 @ 57600 baud …
10:15:34  INFO      radio_bridge  Serial port open. Role: DRONE
10:15:34  INFO      radio_bridge  serial RX → UDP 127.0.0.1:14560
10:15:34  INFO      radio_bridge  UDP       :14561  → serial TX
10:15:34  INFO      radio_bridge  Bridge running. Press Ctrl+C to stop.
```

✅ **Checkpoint**: You see "Bridge running" with no errors.

---

## 8. Step 5 — Start the Radio Bridge on the Laptop (GCS side)

Open a **dedicated PowerShell window** (keep it open while flying):

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python telemetry/radio_bridge.py --port COM5 --baud 57600 --role gcs --verbose
```

Replace `COM5` with your actual COM port from Step 2.

**Expected output:**
```
10:15:35  INFO      radio_bridge  Opening serial port COM5 @ 57600 baud …
10:15:37  INFO      radio_bridge  Serial port open. Role: GCS
10:15:37  INFO      radio_bridge  serial RX → UDP 127.0.0.1:14561
10:15:37  INFO      radio_bridge  UDP       :14560  → serial TX
10:15:37  INFO      radio_bridge  Bridge running. Press Ctrl+C to stop.
```

✅ **Checkpoint**: Both bridges running with no errors. You'll see `[serial→UDP:14560]` / `[UDP:14560→serial]` debug lines once there's traffic.

---

## 9. Step 6 — Start DronaCharya on the Jetson

In a new `tmux` window:
```bash
tmux new -s drone
cd ~/DronaCharya
python3 main.py
```

**Expected output:**
```
INFO  Command listener started on 0.0.0.0:14560
INFO  Telemetry server online
INFO  DronaCharya ready — waiting for GCS commands
```

---

## 10. Step 7 — Start the GCS App on the Laptop

Open a **second PowerShell window:**
```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py
```

Fill in the **Telemetry Link** fields — all **localhost** now:

| Field | Value | Why |
|-------|-------|-----|
| **Drone Host** | `127.0.0.1` | Bridge is local on your laptop |
| **Command Port** | `14560` | Bridge forwards this to radio |
| **Listen Port** | `14561` | Bridge delivers radio data here |

Click **Connect**.

✅ **Checkpoint**: Telemetry Feed shows:
```
[TX] STATUS_REQUEST
[RX:127.0.0.1:PORT] STATUS: {'state': 'IDLE', ...}
```

---

## 11. Verifying the Connection End-to-End

### Quick link test (run on laptop, bridge must be running)

```powershell
python -c "
import socket, json

rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(('127.0.0.1', 14561))
rx.settimeout(8)

tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx.sendto(b'STATUS_REQUEST', ('127.0.0.1', 14560))
print('[TX] STATUS_REQUEST  →  bridge  →  radio  →  Jetson')

try:
    data, addr = rx.recvfrom(4096)
    pkt = json.loads(data.decode())
    print(f'[RX] {pkt[\"type\"]}: {pkt[\"payload\"]}')
    print('==> Radio link UP and DronaCharya responding')
except socket.timeout:
    print('==> TIMEOUT — check both bridges and that main.py is running on Jetson')
finally:
    rx.close(); tx.close()
"
```

### Verbose bridge log (what good traffic looks like)

```
# GCS bridge window:
10:22:05  DEBUG  radio_bridge  [UDP:14560→serial] from ('127.0.0.1', 55421): b'STATUS_REQUEST'
10:22:05  DEBUG  radio_bridge  [serial→UDP:14561] b'{"type": "STATUS", "payload": {...}}'

# Drone bridge window (on Jetson):
10:22:05  DEBUG  radio_bridge  [serial→UDP:14560] b'STATUS_REQUEST'
10:22:05  DEBUG  radio_bridge  [UDP:14561→serial] from ('127.0.0.1', 14561): b'{"type": "STATUS"...}'
```

---

## 12. Debug Tips & Troubleshooting

### 12.1 Bridge crashes — `serial.SerialException: could not open port`

| Platform | Fix |
|----------|-----|
| Jetson | `sudo chmod a+rw /dev/ttyUSB0` · Check the port name (`dmesg | tail`) |
| Windows | Is another app (Mission Planner, QGC) holding the COM port? Close it first |
| Windows | Try a different USB port on the laptop |

---

### 12.2 Bridge starts but no packets flow (both `--verbose` show silence)

1. **Are the SiK radios linked?** The green LED on both radios should be **solid green** (not flashing). Flashing = searching for pair. Fix: ensure both are set to the same Net ID and baud.

2. **Check radio Link quality** using Mission Planner SiK radio config tool:  
   Connect Mission Planner → Initial Setup → SiK Radio → **Load Settings**  
   Confirm **NETID**, **BAUD**, and **AIR_SPEED** match on both radios.

3. **Baud mismatch** — the `--baud` you give to `radio_bridge.py` must match the radio's configured baud. Default is **57600**. If your radios are configured differently:
   ```bash
   python3 telemetry/radio_bridge.py --port /dev/ttyUSB0 --baud 115200 --role drone
   ```

---

### 12.3 GCS app sends commands but Jetson never receives them

1. Make sure the **drone-side bridge** is running (`tmux a -t bridge` on Jetson)
2. Make sure **DronaCharya `main.py`** is running (`tmux a -t drone`)
3. Use `--verbose` on both bridges — you should see forwarded packets appear
4. Use the quick link test from §11 to isolate which side is failing

---

### 12.4 Telemetry replies arrive on laptop but GCS app shows nothing

Port 14561 may already be bound by another process:
```powershell
netstat -ano | findstr :14561
Stop-Process -Id <PID> -Force
```
Then restart both the bridge and `gcs_app.py`.

---

### 12.5 Windows — `[WinError 10048]` port in use

The Python bridge is already running in another window. Close it, or:
```powershell
netstat -ano | findstr :14560
Stop-Process -Id <PID> -Force
```

---

### 12.6 Jetson — bridge dies with `Permission denied` on serial

```bash
sudo usermod -aG dialout $USER
newgrp dialout          # apply without full logout
sudo chmod a+rw /dev/ttyUSB0
```

---

### 12.7 Radio link drops in flight (intermittent packets)

1. **Antenna orientation** — both antennas must be vertical. Never lay a SiK radio flat.
2. **Distance** — 915 MHz SiK: ~1 km LOS. 433 MHz: ~2 km but narrower band.
3. **USB cable noise** — if the radio is USB on the Jetson, use a short, high-quality USB cable or a ferrite choke to reduce EMI from the motors.
4. **ESD / vibration** — mount the radio away from ESCs and power leads.
5. Check RSSI in the bridge verbose log; values below 90 indicate a weak link.

---

## 13. Camera Configuration for Recording

> **Why you get** `Cannot open camera source: 0`  
> `cv2.VideoCapture(0)` tries to open `/dev/video0` as a plain V4L2 device.  
> On a Jetson Nano, **CSI cameras are not exposed as `/dev/video0`** — they require  
> a GStreamer pipeline through `nvarguscamerasrc`. USB webcams _are_ V4L2 but may  
> show up at a different index, or OpenCV may not be built with V4L2 support.

---

### 13.1 Identify connected cameras on the Jetson

```bash
# List all video devices
ls /dev/video*
# Expected output examples:
#   /dev/video0          → USB webcam (V4L2)
#   (nothing)            → only a CSI camera is connected

# Detailed info for each device:
for dev in /dev/video*; do echo "--- $dev ---"; v4l2-ctl --device=$dev --info 2>/dev/null | head -5; done

# Check if a CSI camera is detected by the NVIDIA ISP:
ls /dev/nvhost-vi*
# If files appear here, you have a CSI camera (IMX219 / IMX477 etc.)
```

---

### 13.2 Choose the right source in config.yaml

Edit `config/config.yaml` on the **Jetson** and set **exactly one** of these:

#### Case A — USB webcam (e.g. Logitech, generic)

`/dev/video0` is present in `ls /dev/video*` output:

```yaml
camera:
  device_id: 0        # index matching /dev/video0
  stream_url: ""      # leave blank
```

If `cv2.VideoCapture(0)` still fails (OpenCV built without V4L2), use a GStreamer pipeline instead:

```yaml
camera:
  device_id: 0
  stream_url: "v4l2src device=/dev/video0 ! video/x-raw,width=1280,height=720,framerate=30/1 ! videoconvert ! video/x-raw,format=BGR ! appsink drop=1"
```

#### Case B — CSI camera (IMX219 "Raspberry Pi cam v2", IMX477, etc.)

No `/dev/video0`, but `ls /dev/nvhost-vi*` shows files:

```yaml
camera:
  device_id: 0
  stream_url: "nvarguscamerasrc sensor-id=0 ! video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1 ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink drop=1"
```

> `sensor-id=0` is the primary CSI port (J13 on Jetson Nano). Use `sensor-id=1` for the second CSI port.

#### Case C — RTSP stream (using jetson_stream_server.py)

Start the stream server first (see §13.3), then point config.yaml at it:

```yaml
camera:
  device_id: 0
  stream_url: "rtsp://<jetson-ip>:8554/drone"
```

---

### 13.3 Start the RTSP stream server on the Jetson (optional)

This is only needed if you want to preview the stream on the GCS laptop *and* record it on the Jetson simultaneously, or if you prefer RTSP over a direct pipeline.

```bash
# Install GStreamer RTSP server (one-time)
sudo apt install -y gstreamer1.0-tools gstreamer1.0-rtsp \
                    python3-gi gir1.2-gst-rtsp-server-1.0

# CSI camera
python3 gcs/jetson_stream_server.py

# USB webcam
python3 gcs/jetson_stream_server.py --usb --dev /dev/video0
```

The server prints the exact `stream_url` to paste into `config.yaml`.

---

### 13.4 Quick camera sanity-test (run on Jetson before starting main.py)

```bash
# Test USB/index source:
python3 -c "
import cv2, sys
cap = cv2.VideoCapture(0)
if not cap.isOpened(): sys.exit('FAIL: cannot open /dev/video0')
ok, frame = cap.read()
print('OK — frame shape:', frame.shape if ok else 'read failed')
cap.release()
"

# Test GStreamer CSI pipeline:
python3 -c "
import cv2, sys
pipeline = 'nvarguscamerasrc sensor-id=0 ! video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1 ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink drop=1'
cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
if not cap.isOpened(): sys.exit('FAIL: CSI camera pipeline failed — is nvarguscamerasrc installed?')
ok, frame = cap.read()
print('OK — frame shape:', frame.shape if ok else 'read failed')
cap.release()
"
```

✅ **Checkpoint**: You see `OK — frame shape: (720, 1280, 3)` (or similar).  
Now set `stream_url` in `config.yaml` to the working pipeline and restart `main.py`.

---

### 13.5 Troubleshooting `Cannot open camera source: 0`

| Symptom | Cause | Fix |
|---------|-------|-----|
| Both `device_id: 0` fails and `/dev/video0` missing | CSI-only drone cam | Use Case B pipeline in `stream_url` |
| `/dev/video0` exists but still fails | OpenCV not built with GStreamer/V4L2 | Use V4L2 GStreamer pipeline in `stream_url` |
| GStreamer pipeline fails | `nvarguscamerasrc` not available | Run `gst-inspect-1.0 nvarguscamerasrc` to confirm; install JetPack if missing |
| RTSP `stream_url` fails | Server not running or firewall | Confirm `jetson_stream_server.py` is running; `nc -zv <ip> 8554` to test port |
| Multiple cameras — wrong device | Wrong `device_id` or `sensor-id` | Run `v4l2-ctl --list-devices` and try index 1, 2… |

---

## Quick Reference Card

```
Boot order:
  1. Power on Jetson Nano
  2. tmux new -s bridge  →  python3 telemetry/radio_bridge.py --port /dev/ttyUSB0 --baud 57600 --role drone
  3. tmux new -s drone   →  python3 main.py
  4. On laptop: plug in SiK USB dongle
  5. python telemetry/radio_bridge.py --port COM5 --baud 57600 --role gcs
  6. python gcs/gcs_app.py  →  Drone Host: 127.0.0.1 | Cmd: 14560 | Listen: 14561 → Connect

Key files:
  config/config.yaml          camera.stream_url / camera.device_id  (choose one)
  telemetry/radio_bridge.py   the serial↔UDP bridge (run on both sides)
  gcs/gcs_app.py              GCS GUI (laptop)
  gcs/jetson_stream_server.py RTSP server for CSI / USB cam (Jetson only)
  main.py                     DronaCharya entry point (Jetson)

Ports (all localhost after bridging):
  14560  →  DronaCharya command listener
  14561  →  GCS telemetry listener
  8554   →  RTSP camera stream (jetson_stream_server.py, optional)
```
