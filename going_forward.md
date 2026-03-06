# DronaCharya — Operator Guide

> **Who is this for?** Anyone operating the drone system at the field — no programming knowledge needed.

---

## What is the system?

DronaCharya is a drone intelligence system with two sides:

| Side | Hardware | What runs on it |
|---|---|---|
| **GCS (Ground)** | Your Windows laptop | `gcs_app.py` + `radio_bridge.py` |
| **Drone (Air)** | Jetson Nano on drone | `main.py` + `radio_bridge.py` (drone side) |

Commands travel from the GCS app → laptop radio → drone radio → Jetson → Pixhawk.

---

## Pre-Flight Checklist

Before powering on the drone:

- [ ] Drone battery charged and plugged in
- [ ] Radio telemetry modules plugged in (USB on laptop, UART on Jetson)
- [ ] Laptop and Jetson are on the same firmware/code version
- [ ] GPS has a clear sky view (wait for solid GPS fix — usually 2–3 minutes after power-on)
- [ ] Pixhawk armed and in correct flight mode (GUIDED or AUTO)

---

## Step 1 — Start the Drone App (on Jetson)

SSH into the Jetson or use a connected keyboard/monitor:

```bash
cd /home/jetbot/Documents/DronaCharya
python main.py
```

Leave this terminal open. You should see:
```
dronAcharya started in MISSION mode.
```

---

## Step 2 — Start the Drone-Side Bridge (on Jetson)

Open a **second terminal** on the Jetson:

```bash
cd /home/jetbot/Documents/DronaCharya
python telemetry/radio_bridge.py --port /dev/ttyTHS1 --baud 57600 --role drone
```

> **Note:** The port is usually `/dev/ttyTHS1` (Pixhawk UART) or `/dev/ttyUSB0` (USB radio). Confirm with `ls /dev/tty*`.

You should see:
```
MAVLink link ready on /dev/ttyTHS1
  MAV tunnel (CMD) -> UDP send 127.0.0.1:14560
  UDP listen : 127.0.0.1:14561 -> MAV tunnel (TELEM)
```

---

## Step 3 — Start the GCS Bridge (on Laptop)

Open a terminal on your **Windows laptop**:

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python telemetry/radio_bridge.py --port COM11 --baud 57600 --role gcs
```

> **Tip:** If COM11 is wrong, open Device Manager → Ports to find the correct COM port for the telemetry radio.

You should see:
```
MAVLink link ready on COM11 (v2)
  UDP listen : 127.0.0.1:14560 -> MAV tunnel (CMD)
  MAV tunnel (TELEM) -> UDP send 127.0.0.1:14561
```

---

## Step 4 — Open the GCS App (on Laptop)

Open a **second terminal** on your laptop:

```powershell
python gcs/gcs_app.py
```

The GCS window opens. Click **Connect**. Status should change to **CONNECTED** and you'll see a status update in the Telemetry Feed panel.

> ⚠️ **Important:** Do NOT run `main.py` on the laptop. Only run `gcs_app.py` and `radio_bridge.py --role gcs` on the laptop.

---

## Step 5 — Survey the Area

The drone flies a pattern and photographs ground targets using its camera + YOLO AI detection.

1. Place the drone on the field, disarm safety interlocks
2. In the GCS app, click **Start Survey on Drone**
3. The drone will take off, fly its pattern, and detect targets — all automatically
4. When it lands (or you want to stop early), click **Stop Survey on Drone**

After stop, the system automatically:
- Deduplicates detected targets by GPS position
- Builds a Travelling Salesman Problem (TSP) optimised route to visit all targets
- Logs the route to `data/target_sessions/session-XXXX/`

---

## Step 6 — Review the Route (Optional)

After survey stop, the GCS Telemetry Feed will show:
```
Survey complete: raw=12, unique=5, route=data/target_sessions/session-0003/route.json
```

- **raw** = total individual detections
- **unique** = deduplicated target count (radius-filtered)
- The route JSON and graph images are saved on the Jetson

If you want to **rebuild the route** from the last session without flying again:

→ Click **Build Route** in the GCS app.

---

## Step 7 — Execute the Mission

1. Click **Start Mission** in the GCS app
2. The drone takes off to the configured altitude (`config.yaml → mission.default_altitude_m`)
3. It flies to each target waypoint in TSP-optimised order, hovers briefly at each, then returns home
4. Status updates appear live in the Telemetry Feed panel

### Emergency Stop
Click **Abort** at any time. The drone will stop the mission and hover in place. Land it manually via your RC transmitter.

---

## Step 8 — Video-Only Recording (No Survey)

If you just want to record video without full AI detection:

1. Click **Start Recording Only**
2. Click **Stop Recording Only** when done

The video is saved to `data/recordings/session-XXXX/recording.avi` on the Jetson, with frames extracted to `session-XXXX/frames/`.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| GCS shows CONNECTED but no telemetry | Drone-side bridge not running | SSH to Jetson, check `radio_bridge.py --role drone` is running |
| Commands sent but drone doesn't respond | `main.py` not running on Jetson | SSH to Jetson and restart `main.py` |
| Bridge shows no `[UDP->MAV:CMD]` log | Another process on port 14560 | Run `netstat -ano \| findstr :14560` — kill the stray `python` PID |
| Camera fails to open on Jetson | Wrong sensor-id in pipeline | Check `config.yaml → camera.device_id` (usually `0`) |
| No GPS targets detected | Low altitude or no GPS fix | Wait for GPS fix type ≥ 3; fly higher than `config.yaml → survey.min_gps_fix_type` |
| Mission says "no waypoints" | Survey not completed or route not built | Run **Stop Survey** then **Build Route**, then **Start Mission** |

---

## Port Reference

| Port | Direction | What it carries |
|---|---|---|
| `127.0.0.1:14560` (laptop) | GCS app → bridge | Commands (text) |
| `127.0.0.1:14561` (laptop) | Bridge → GCS app | Telemetry (JSON) |
| `127.0.0.1:14560` (Jetson) | Bridge → drone app | Commands (text) |
| `127.0.0.1:14561` (Jetson) | Drone app → bridge | Telemetry (JSON) |
| `127.0.0.1:14550` (Jetson) | MAVLink to Pixhawk | Mission / flight control |
