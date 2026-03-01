# 🛸 DronaCharya — GCS ↔ Drone Connection Setup Guide

> **Real hardware topology:**  
> SiK Telemetry Radio → Pixhawk TELEM port → UART → Jetson Nano (runs DronaCharya)

---

## Table of Contents

1. [System Architecture — Read This First](#1-system-architecture--read-this-first)
2. [Two Separate Communication Channels](#2-two-separate-communication-channels)
3. [Prerequisites Checklist](#3-prerequisites-checklist)
4. [Channel 1 — Jetson↔Pixhawk via UART (MAVLink)](#4-channel-1--jetsonnpixhawk-via-uart-mavlink)
5. [Channel 2 — GCS Laptop↔Jetson via Wi-Fi (DronaCharya UDP)](#5-channel-2--gcs-laptopjetson-via-wi-fi-dronacharya-udp)
6. [Channel 2 (Alt) — GCS Laptop↔Jetson via SiK Radio + Serial Bridge](#6-channel-2-alt--gcs-laptopjetson-via-sik-radio--serial-bridge)
7. [config.yaml — Full Reference for This Setup](#7-configyaml--full-reference-for-this-setup)
8. [Starting Everything — Boot Order Matters](#8-starting-everything--boot-order-matters)
9. [Verifying the Connection End-to-End](#9-verifying-the-connection-end-to-end)
10. [Debug Tips & Troubleshooting](#10-debug-tips--troubleshooting)

---

## 1. System Architecture — Read This First

```
╔════════════════════════════════════════════════════════════════════════════╗
║                          FULL SYSTEM DIAGRAM                               ║
╠═══════════════════╦════════════════════════════════════════════════════════╣
║   LAPTOP (GCS)    ║                   DRONE                                ║
║                   ║                                                        ║
║  gcs_app.py       ║     Wi-Fi / Ethernet (Channel 2 — DronaCharya UDP)    ║
║  UDP 14560/14561  ╠═══════════════════════════════════╗                   ║
║                   ║                                   ▼                   ║
║                   ║                         ┌─────────────────┐           ║
║                   ║                         │   Jetson Nano   │           ║
║  [SiK USB Dongle] ║                         │  main.py        │           ║
║  (QGC/MavProxy)   ║ RF Link (Channel 1      │  (DronaCharya)  │           ║
║  MAVLink only  ◄══╬═ MAVLink via SiK) ═══► │  pymavlink      │           ║
║                   ║                         └───────┬─────────┘           ║
║                   ║                                 │ UART pins           ║
║                   ║                                 │ (TX→RX, RX→TX, GND)║
║                   ║                         ┌───────▼─────────┐           ║
║                   ║                         │    Pixhawk FC   │           ║
║                   ║                         │  (autopilot)    │           ║
║                   ║                         └───────┬─────────┘           ║
║                   ║                                 │ TELEM1 port         ║
║                   ║                         ┌───────▼─────────┐           ║
║                   ║                         │  SiK Radio (air)│           ║
║                   ╚═════════════════════════╧═════════════════╧═══════════╝
```

---

## 2. Two Separate Communication Channels

This is the most important thing to understand. **There are exactly two independent comms channels**, and they serve different purposes:

| | Channel 1 (MAVLink) | Channel 2 (DronaCharya) |
|---|---|---|
| **Purpose** | Flight controller telemetry, GPS, attitude, arming | DronaCharya app commands (mapping, detection, abort…) |
| **Protocol** | MAVLink v1/v2 | Custom plaintext UDP |
| **Physical path** | SiK Radio ↔ Pixhawk TELEM ↔ Pixhawk UART ↔ Jetson | Wi-Fi/LAN (or serial bridge) between Laptop and Jetson |
| **Ports** | 14550 (MAVLink standard) | 14560 (commands) / 14561 (telemetry) |
| **Who reads it** | pymavlink inside DronaCharya, QGroundControl | `gcs_app.py` on laptop, `command_listener.py` on Jetson |
| **Goes through SiK?** | ✅ Yes | ❌ No — SiK is busy with Pixhawk MAVLink |

> ⚠️ **Common Mistake**: People assume the SiK radio carries DronaCharya commands. It doesn't.  
> The SiK radio is wired to Pixhawk's **TELEM port** and only speaks MAVLink.  
> DronaCharya's `gcs_app.py` commands travel over **Wi-Fi** directly to the Jetson.

---

## 3. Prerequisites Checklist

### Hardware Wiring (verify before anything)

- [ ] SiK radio connected to **Pixhawk TELEM1** port (JST-GH 6-pin cable)
- [ ] Pixhawk connected to Jetson Nano via **UART**:
  - Pixhawk `TELEM2` TX → Jetson UART RX
  - Pixhawk `TELEM2` RX → Jetson UART TX
  - Pixhawk GND → Jetson GND
  - **Do NOT connect 5V** if Jetson is powered separately
- [ ] SiK USB radio dongle plugged into laptop
- [ ] Laptop and Jetson Nano on the **same Wi-Fi network** (both connected to same router/hotspot)

> **Which UART port on Jetson Nano?**  
> Jetson Nano has UART pins on the 40-pin GPIO header.  
> Default UART device: `/dev/ttyTHS1` (pins 8=TX, 10=RX)  
> You may need to enable it first — see [§10.3](#103-enabling-uart-on-jetson-nano).

### Software

- [ ] DronaCharya repo cloned on **both** laptop and Jetson
- [ ] `pip install -r requirements.txt` run on both
- [ ] `pip install MAVProxy` on Jetson (for UART→UDP bridge)
- [ ] Python 3.9+ on both machines

---

## 4. Channel 1 — Jetson↔Pixhawk via UART (MAVLink)

DronaCharya's `main.py` needs to communicate with the Pixhawk flight controller over MAVLink. Since the Pixhawk is connected to the Jetson via UART, we use **MAVProxy on the Jetson** to bridge the serial UART into a local UDP connection that pymavlink can consume.

### Step 1 — Find the UART device on Jetson

```bash
ls /dev/ttyTHS*   # Jetson native UART header pins
ls /dev/ttyUSB*   # USB-serial adapters
ls /dev/ttyACM*   # USB ACM devices
```

Most common: `/dev/ttyTHS1` for Jetson native UART header.

### Step 2 — Grant serial port access

```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
# Test immediately without logout:
sudo chmod a+rw /dev/ttyTHS1
```

### Step 3 — Run MAVProxy bridge on Jetson

This bridges Pixhawk's UART serial stream into a local UDP port (14550) that DronaCharya reads:

```bash
mavproxy.py \
  --master=/dev/ttyTHS1,57600 \
  --out=udp:127.0.0.1:14550 \
  --daemon
```

**Parameters:**
| Parameter | Meaning |
|-----------|---------|
| `--master=/dev/ttyTHS1,57600` | Read from UART at 57600 baud (must match Pixhawk TELEM2 baud) |
| `--out=udp:127.0.0.1:14550` | Forward to localhost port 14550 (what DronaCharya connects to) |
| `--daemon` | Run in background |

### Step 4 — Set Pixhawk TELEM2 baud rate

In **QGroundControl** or **Mission Planner**, set:
```
SERIAL2_BAUD = 57   (means 57600 baud)
SERIAL2_PROTOCOL = 2  (MAVLink 2)
```

> `TELEM2` corresponds to `SERIAL2` in Pixhawk parameters.  
> `TELEM1` is used by the SiK radio.

✅ **Checkpoint 4**: Run `mavproxy.py --master=/dev/ttyTHS1,57600` — you should see heartbeat messages scrolling.

---

## 5. Channel 2 — GCS Laptop↔Jetson via Wi-Fi (DronaCharya UDP)

This is how your `gcs_app.py` talks to DronaCharya running on the Jetson.

### Step 1 — Get the Jetson's IP address

**On the Jetson Nano:**
```bash
hostname -I
# Example: 192.168.1.42
```

**Verify from your laptop:**
```powershell
ping 192.168.1.42
```
You must get replies before proceeding.

### Step 2 — Edit `config.yaml` on the Jetson

```yaml
telemetry:
  command_host: "0.0.0.0"          # Listens on all interfaces — do not change
  command_port: 14560               # GCS sends commands to this port
  gcs_host: "192.168.1.XX"         # ← Replace with YOUR LAPTOP'S Wi-Fi IP
  gcs_port: 14561                   # Drone sends telemetry back to this port
```

**Find your laptop's Wi-Fi IP:**
```powershell
ipconfig
# Look for: "IPv4 Address" under "Wireless LAN adapter Wi-Fi"
# Example: 192.168.1.10
```

### Step 3 — Open firewall on Windows (laptop)

```powershell
# Run PowerShell as Administrator
New-NetFirewallRule -DisplayName "DronaCharya Telemetry IN" `
  -Direction Inbound -Protocol UDP -LocalPort 14561 -Action Allow

New-NetFirewallRule -DisplayName "DronaCharya Commands OUT" `
  -Direction Outbound -Protocol UDP -RemotePort 14560 -Action Allow
```

### Step 4 — Launch GCS app on laptop

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py
```

Fill in the **Telemetry Link** fields:

| Field | Value |
|-------|-------|
| **Drone Host** | `192.168.1.42` (Jetson's Wi-Fi IP) |
| **Command Port** | `14560` |
| **Listen Port** | `14561` |

Click **Connect**.

✅ **Checkpoint 5**: Telemetry Feed shows `[TX] STATUS_REQUEST` followed by `[RX:192.168.1.42:...] STATUS: {...}`.

---

## 6. Channel 2 (Alt) — GCS Laptop↔Jetson via SiK Radio + Serial Bridge

> ⚠️ **Use this only if Wi-Fi is unavailable.** It's complex and the SiK radio is already carrying Pixhawk MAVLink — you'd be running two streams through it simultaneously using MAVProxy multiplexing.

### How it works

MAVProxy can output to multiple destinations simultaneously. On the Jetson, instead of only forwarding to `127.0.0.1:14550`, we add a second output back through the radio to the laptop:

**On the Jetson Nano:**
```bash
mavproxy.py \
  --master=/dev/ttyTHS1,57600 \
  --out=udp:127.0.0.1:14550 \
  --out=udp:LAPTOP_IP:14561 \
  --daemon
```

**On the Laptop:**
```powershell
mavproxy.py `
  --master=COM5,57600 `
  --out=udp:127.0.0.1:14560 `
  --daemon
```

Then set `Drone Host = 127.0.0.1` in the GCS app — MAVProxy on the laptop forwards to/from the radio.

> ⚠️ **Caveat**: DronaCharya commands (`START_MAPPING`, etc.) are **not** MAVLink packets. They are raw plaintext UDP. MAVProxy only bridges MAVLink. This approach only works if you set up `socat` (serial pipe tool) rather than MAVProxy for the DronaCharya channel.

**Recommended**: Use Wi-Fi (Section 5) for DronaCharya commands. Use the SiK radio only for Pixhawk MAVLink.

---

## 7. config.yaml — Full Reference for This Setup

Below is the complete `config/config.yaml` for the **Jetson Nano** with UART-connected Pixhawk:

```yaml
camera:
  device_id: 0
  stream_url: ""                      # Set to RTSP URL if using IP camera on drone
  capture_count: 24
  capture_interval_sec: 0.25
  frame_width: 1280
  frame_height: 720

mapping:
  max_dimension: 1600
  meters_per_pixel: 0.05

vision:
  model_path: "models/target_yolo.pt"
  conf_threshold: 0.35
  target_class_name: "target"
  image_size: 640

mission:
  default_altitude_m: 15.0
  hover_time_sec: 5
  # MAVLink to Pixhawk via local MAVProxy bridge (UART → UDP)
  mavlink_connection: "udp:127.0.0.1:14550"
  mavlink_baudrate: 57600
  home_latitude: 28.6139            # ← Set your actual launch site coords!
  home_longitude: 77.2090
  max_mission_duration_sec: 900

telemetry:
  command_host: "0.0.0.0"           # Listen on all interfaces
  command_port: 14560               # GCS sends commands here
  gcs_host: "192.168.1.10"         # ← Your LAPTOP's Wi-Fi IP
  gcs_port: 14561                   # GCS listens for telemetry here

logging:
  level: "INFO"
  file_name: "dronacharya.log"
```

> **If connecting directly via serial (no MAVProxy bridge), change:**
> ```yaml
> mission:
>   mavlink_connection: "/dev/ttyTHS1"   # Direct UART to Pixhawk
>   mavlink_baudrate: 57600
> ```

---

## 8. Starting Everything — Boot Order Matters

Follow this exact order to avoid race conditions:

```
Step 1 ──► Power on Pixhawk (wait for arming tones / LED solid)
Step 2 ──► Boot Jetson Nano, SSH in from laptop
Step 3 ──► On Jetson: Start MAVProxy bridge (UART → UDP 14550)
Step 4 ──► On Jetson: Start DronaCharya main app
Step 5 ──► On Laptop: Plug in SiK USB dongle (optional: open QGC)
Step 6 ──► On Laptop: Start gcs_app.py and click Connect
```

### SSH into Jetson from your laptop

```powershell
ssh username@192.168.1.42
# Replace username with your Jetson user (default is often 'jetson' or 'ubuntu')
```

### Step 3 — MAVProxy bridge on Jetson

```bash
# Use tmux so it keeps running after SSH closes
tmux new -s mavbridge
mavproxy.py --master=/dev/ttyTHS1,57600 --out=udp:127.0.0.1:14550 --daemon
# Detach: Ctrl+B, then D
```

### Step 4 — DronaCharya main app on Jetson

```bash
tmux new -s drone
cd ~/DronaCharya
python main.py
# Expected output:
# INFO  Command listener started on 0.0.0.0:14560
# INFO  Telemetry server online → 192.168.1.10:14561
# INFO  MAVLink connection initialised
```

### Step 6 — GCS app on Laptop

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py
```

Set Drone Host to `192.168.1.42`, ports `14560`/`14561`, click **Connect**.

---

## 9. Verifying the Connection End-to-End

### Full end-to-end check

Open **two PowerShell windows** on your laptop:

**Window 1 — Listen for drone telemetry:**
```powershell
python -c "
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 14561))
s.settimeout(10)
print('Listening on :14561 ...')
while True:
    try:
        data, addr = s.recvfrom(4096)
        pkt = json.loads(data.decode())
        print(f'[RX from {addr[0]}] {pkt[\"type\"]}: {pkt[\"payload\"]}')
    except socket.timeout:
        print('Timeout — no data received')
        break
"
```

**Window 2 — Send a STATUS_REQUEST:**
```powershell
python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(b'STATUS_REQUEST', ('192.168.1.42', 14560))
print('[TX] STATUS_REQUEST sent')
s.close()
"
```

**Expected output in Window 1:**
```
Listening on :14561 ...
[RX from 192.168.1.42] STATUS: {'state': 'IDLE', 'mission_active': False}
```

### Verify MAVLink to Pixhawk (on Jetson)

```bash
python3 -c "
from pymavlink import mavutil
m = mavutil.mavlink_connection('udp:127.0.0.1:14550')
m.wait_heartbeat(timeout=10)
print(f'Heartbeat from system {m.target_system}, component {m.target_component}')
"
```

Expected: `Heartbeat from system 1, component 1`

---

## 10. Debug Tips & Troubleshooting

### 10.1 No telemetry reply from Jetson (link is down)

**Checklist:**
1. Is Jetson's `main.py` actually running? On Jetson: `tmux ls` → attach with `tmux a -t drone`
2. Are both on the same Wi-Fi subnet? (`192.168.1.x` on both sides)
3. Windows Firewall — run the firewall rules from §5 Step 3
4. Is `gcs_host` in `config.yaml` set to your **laptop's** IP (not the Jetson's)?

**Quick test from laptop:**
```powershell
# Check if Jetson port 14560 is reachable
Test-NetConnection -ComputerName 192.168.1.42 -Port 14560
# Note: UDP can't be probed with TCP test — use the Python script in §9 instead
```

---

### 10.2 MAVProxy says "no heartbeat" from Pixhawk UART

**Symptom:** `mavproxy.py --master=/dev/ttyTHS1,57600` shows `Waiting for heartbeat...` indefinitely.

**Checklist:**
1. **Baud rate mismatch** — Pixhawk `SERIAL2_BAUD` must match MAVProxy baud:
   - In QGC: Parameters → `SERIAL2_BAUD` → set to `57` (= 57600)
2. **Wrong UART device** — try `/dev/ttyTHS0` instead of `/dev/ttyTHS1`
3. **TX/RX crossed** — Pixhawk TX → Jetson RX (not TX→TX). Swap the wires if no heartbeat
4. **Pixhawk not fully booted** — wait for the solid green LED before connecting
5. **Serial protocol not set** — `SERIAL2_PROTOCOL` must be `2` (MAVLink 2)

---

### 10.3 Enabling UART on Jetson Nano

The Jetson Nano's hardware UART (`/dev/ttyTHS1`) may be in use by the serial console by default.

**Disable serial console to free UART:**
```bash
sudo systemctl stop nvgetty
sudo systemctl disable nvgetty
sudo udevadm trigger
```

Then verify the device exists and is accessible:
```bash
ls -la /dev/ttyTHS1
sudo chmod a+rw /dev/ttyTHS1
```

---

### 10.4 Windows Firewall Blocking UDP

```powershell
# Elevated PowerShell (Run as Administrator)

# Allow inbound telemetry
New-NetFirewallRule -DisplayName "DronaCharya Telemetry IN" `
  -Direction Inbound -Protocol UDP -LocalPort 14561 -Action Allow

# Temporarily disable all firewalls to test:
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False
# Test your connection, then re-enable:
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
```

---

### 10.5 Port 14561 Already In Use on Laptop

```powershell
# Find what process is using it
netstat -ano | findstr :14561

# Kill by PID (replace 12345)
Stop-Process -Id 12345 -Force
```

---

### 10.6 `gcs_app.py` Crashes — ModuleNotFoundError

Always run from the **project root**:
```powershell
# ✅ Correct
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py

# ❌ Wrong
cd gcs && python gcs_app.py
```

---

### 10.7 SiK Radio Not Appearing on Laptop (No COM Port)

1. Try a different USB port
2. Check Device Manager → look for "USB Serial Device" with a yellow `!`
3. Install CP210x or FTDI drivers (depending on your radio's USB chip):
   - SiK 3DR/Holybro: usually CP210x → https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers
4. Verify in PowerShell:
   ```powershell
   Get-WMIObject Win32_SerialPort | Select-Object Name, DeviceID
   ```

---

### 10.8 DronaCharya Receives Command But Ignores It

The drone may already be in an active state. Send `STATUS_REQUEST` first and read the state from the Telemetry Feed:

```
STATUS: {'state': 'MAPPING', 'mission_active': True, ...}
```

Valid state transitions — you must `ABORT` before issuing conflicting commands.

---

### 10.9 Quick Diagnostic Cheat Sheet

```powershell
# Run this from laptop to test the full DronaCharya UDP path
python -c "
import socket, json, time
JETSON = '192.168.1.42'  # ← change this
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(('0.0.0.0', 14561))
rx.settimeout(5)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx.sendto(b'STATUS_REQUEST', (JETSON, 14560))
print(f'[TX] STATUS_REQUEST → {JETSON}:14560')
try:
    d, a = rx.recvfrom(4096)
    p = json.loads(d)
    print(f'[RX] {p[\"type\"]}: {p[\"payload\"]}')
    print('✅  DronaCharya link UP')
except socket.timeout:
    print('❌  No reply — check Jetson main.py is running and IPs match')
finally:
    rx.close(); tx.close()
"
```

---

## Quick Reference Card

```
┌──────────────────────────────────────────────────────────────────────────┐
│                 DronaCharya Connection Quick Reference                    │
├────────────────────────────────┬─────────────────────────────────────────┤
│ CHANNEL 1 — MAVLink (FC)       │ CHANNEL 2 — DronaCharya Commands        │
├────────────────────────────────┼─────────────────────────────────────────┤
│ Path: SiK radio ↔ Pixhawk     │ Path: Wi-Fi  Laptop ↔ Jetson            │
│       ↔ UART ↔ Jetson          │                                          │
│ Port: 14550 (MAVLink std)      │ Cmd port:  14560 (drone listens)        │
│ Bridge: mavproxy on Jetson     │ Telem port: 14561 (laptop listens)      │
│ Device: /dev/ttyTHS1, 57600   │ Protocol: plaintext UDP                  │
├────────────────────────────────┼─────────────────────────────────────────┤
│ Start drone app    │ ssh → tmux → python main.py                         │
│ Start MAVProxy     │ mavproxy.py --master=/dev/ttyTHS1,57600             │
│                    │   --out=udp:127.0.0.1:14550 --daemon                │
│ Start GCS app      │ python gcs/gcs_app.py  (from project root)          │
│ Drone Host field   │ 192.168.1.42  (Jetson Wi-Fi IP)                     │
│ Config file        │ config/config.yaml  (edit on Jetson)                │
│ Log file (drone)   │ dronacharya.log                                      │
└────────────────────────────────┴─────────────────────────────────────────┘
```
