# 🛸 DronaCharya — GCS ↔ Drone Connection Setup Guide

> **Interactive setup guide** — follow the sections in order.  
> Each section has a ✅ **checkpoint** so you know when you can move on.

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Scenario A — Direct Wi-Fi / LAN Connection](#3-scenario-a--direct-wi-fi--lan-connection)
4. [Scenario B — SiK Telemetry Radio (915 MHz / 433 MHz)](#4-scenario-b--sik-telemetry-radio)
5. [Configuring `config.yaml` on Both Sides](#5-configuring-configyaml-on-both-sides)
6. [Starting the Drone-Side App (Jetson Nano)](#6-starting-the-drone-side-app-jetson-nano)
7. [Starting the GCS App on Your Laptop](#7-starting-the-gcs-app-on-your-laptop)
8. [Verifying the Connection is Live](#8-verifying-the-connection-is-live)
9. [Sending Mission Commands](#9-sending-mission-commands)
10. [Debug Tips & Troubleshooting](#10-debug-tips--troubleshooting)

---

## 1. System Architecture Overview

```
┌──────────────────────────────────┐        UDP / Radio Link        ┌──────────────────────────────────┐
│         LAPTOP  (GCS)            │◄──────────────────────────────►│      DRONE  (Jetson Nano)        │
│                                  │                                 │                                  │
│  gcs/gcs_app.py                  │   Commands  →  port 14560      │  main.py  (core/main.py)         │
│  • Sends commands via UDP        │   Telemetry ←  port 14561      │  telemetry/command_listener.py   │
│  • Listens for telemetry on      │                                 │  telemetry/telemetry_server.py   │
│    port 14561 (listen_port)      │                                 │                                  │
└──────────────────────────────────┘                                 └──────────────────────────────────┘
```

### Port Summary

| Direction | Source | Destination | Port | Protocol |
|-----------|--------|-------------|------|----------|
| GCS → Drone (command) | Laptop | Jetson Nano | **14560** | UDP |
| Drone → GCS (telemetry) | Jetson Nano | Laptop | **14561** | UDP |

Both sides use **UDP sockets only** — there is no TCP handshake. This means the connection is "fire and forget"; if the link goes down, packets are silently dropped.

---

## 2. Prerequisites

### On Your Laptop (GCS Side)

- [ ] Python 3.9+ installed (`python --version`)
- [ ] Dependencies installed:
  ```powershell
  cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
  pip install -r requirements.txt
  ```
- [ ] Firewall allows UDP on ports **14560** and **14561** (see [Debug Tips §10.1](#101-windows-firewall-blocking-udp))

### On the Drone (Jetson Nano Side)

- [ ] DronaCharya repo cloned
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] YOLOv8 model weights present at `models/target_yolo.pt`
- [ ] Camera connected and tested

---

## 3. Scenario A — Direct Wi-Fi / LAN Connection

Use this when your laptop and the Jetson Nano are on the **same Wi-Fi network or Ethernet switch** (e.g., during bench testing, or when the drone is close enough for Wi-Fi range).

### Step 1 — Find the Jetson Nano's IP address

On the Jetson Nano, run:
```bash
hostname -I
# Example output: 192.168.1.42
```

Note this IP. You will enter it in the GCS app as **Drone Host**.

### Step 2 — Verify line-of-sight via ping

From your **laptop**:
```powershell
ping 192.168.1.42
```
You should see replies with low latency (< 5 ms on LAN). If you get timeouts, fix the network before proceeding.

### Step 3 — Update `config.yaml` on the Jetson

On the **Jetson Nano**, edit `config/config.yaml`:

```yaml
telemetry:
  command_host: "0.0.0.0"      # Listen on all interfaces — leave this as-is
  command_port: 14560           # Drone listens for GCS commands here
  gcs_host: "192.168.1.XX"     # ← YOUR LAPTOP'S IP on the same network
  gcs_port: 14561               # Drone sends telemetry to this port on the laptop
```

To find your **laptop's IP**:
```powershell
ipconfig
# Look for "IPv4 Address" under your Wi-Fi adapter
```

✅ **Checkpoint A**: You can ping the Jetson from the laptop, and `config.yaml` has both IPs filled in.

---

## 4. Scenario B — SiK Telemetry Radio

Use this when the drone is **airborne** or out of Wi-Fi range. SiK radios (e.g., RFD900, 3DR 915 MHz) create a transparent serial-over-radio link.

### How SiK Radio Fits In

```
Laptop USB          Radio Link          Drone USB
   │                                        │
[GCS Radio] ═══════════════════════════ [Drone Radio]
   │ (serial/COM port)                      │ (serial/UART on Jetson)
   ▼                                        ▼
[UDP ←→ Serial bridge]              [UDP ←→ Serial bridge]
(MAVProxy or socat)                 (MAVProxy or socat)
```

SiK radios appear as **virtual COM/serial ports**. You need a bridge to forward UDP packets (which the GCS app uses) through the radio serial port.

---

### Step 4A — Identify the COM port (Laptop)

1. Plug in the SiK radio USB dongle to your laptop.
2. Open **Device Manager** → **Ports (COM & LPT)**.
3. Note the COM port, e.g., `COM5`.

Alternatively in PowerShell:
```powershell
Get-WMIObject Win32_SerialPort | Select-Object Name, DeviceID
```

### Step 4B — Identify the serial port (Jetson Nano)

On the Jetson:
```bash
ls /dev/ttyUSB* /dev/ttyACM*
# Usually: /dev/ttyUSB0  or  /dev/ttyACM0
```

### Step 4C — Install the bridge tool (both sides)

We use **MAVProxy** to bridge UDP ↔ serial. Install it:

```bash
# On both laptop (PowerShell) and Jetson (bash)
pip install MAVProxy
```

### Step 4D — Run the bridge on the Jetson Nano (drone side)

```bash
mavproxy.py \
  --master=/dev/ttyUSB0,57600 \
  --out=udp:127.0.0.1:14560 \
  --out=udp:127.0.0.1:14561 \
  --daemon
```

> **Explanation:**  
> `--master` = the radio's serial port at 57600 baud  
> `--out` = forward received packets to the DronaCharya app on localhost  
> `--daemon` = run in background

### Step 4E — Run the bridge on your Laptop (GCS side)

```powershell
mavproxy.py `
  --master=COM5,57600 `
  --out=udp:127.0.0.1:14561 `
  --daemon
```

> Replace `COM5` with your actual COM port and `57600` with the baud rate the radios are configured to (default is 57600 for SiK).

### Step 4F — Update `config.yaml` for radio mode

Since MAVProxy bridges the radio to **localhost**, both sides talk to `127.0.0.1`:

**On the Jetson Nano** `config/config.yaml`:
```yaml
telemetry:
  command_host: "0.0.0.0"
  command_port: 14560
  gcs_host: "127.0.0.1"      # MAVProxy bridge is local
  gcs_port: 14561
```

**In the GCS app** (or via config):
```
Drone Host  →  127.0.0.1    (MAVProxy on laptop forwards to radio)
Command Port → 14560
Listen Port  → 14561
```

### Step 4G — Check radio link quality

On the laptop, open a second PowerShell and connect to MAVProxy's interactive console:
```powershell
mavproxy.py --master=COM5,57600
```
Type `link` and press Enter. You should see:

```
1 links
link 1 OK
```

The status display will show **RSSI** (signal strength). Aim for RSSI > 150 for reliable operation.

✅ **Checkpoint B**: MAVProxy says `link 1 OK` on both sides and RSSI is stable.

---

## 5. Configuring `config.yaml` on Both Sides

This is the **master checklist** for both scenarios. `config/config.yaml` lives in the project root on each machine.

```yaml
# ── Telemetry block (most important for connection) ──────────────────────
telemetry:
  command_host: "0.0.0.0"        # Drone always binds to all interfaces
  command_port: 14560             # GCS sends commands TO this port on the drone
  gcs_host: "<LAPTOP_IP>"        # Drone sends telemetry TO this IP (Scenario A)
                                  # Use "127.0.0.1" when using radio bridge (Scenario B)
  gcs_port: 14561                 # Drone sends telemetry to this port

# ── Mission block ────────────────────────────────────────────────────────
mission:
  mavlink_connection: "udp:127.0.0.1:14550"   # MAVLink to flight controller
  mavlink_baudrate: 57600
  default_altitude_m: 15.0
  home_latitude: null             # Set these before a real flight!
  home_longitude: null

# ── Camera block ─────────────────────────────────────────────────────────
camera:
  stream_url: ""                  # e.g. "rtsp://192.168.1.42:8554/stream"
                                  # Leave blank to use local webcam (device_id: 0)
```

---

## 6. Starting the Drone-Side App (Jetson Nano)

SSH into the Jetson Nano from your laptop:
```powershell
ssh user@192.168.1.42
```

Then start DronaCharya:
```bash
cd ~/DronaCharya
python main.py
```

**Expected startup output:**
```
INFO  Telemetry server online
INFO  Command listener started on 0.0.0.0:14560
INFO  MAVLink connection initialised
INFO  DronaCharya ready — waiting for GCS commands
```

> **Tip**: Run it inside a `tmux` session so it survives SSH disconnects:  
> ```bash
> tmux new -s drone
> python main.py
> # Detach with Ctrl+B, then D
> ```

✅ **Checkpoint 6**: The Jetson output shows `Command listener started on 0.0.0.0:14560`.

---

## 7. Starting the GCS App on Your Laptop

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py
```

The **dronAcharya GCS** window appears.

### Fill in the connection fields

| Field | Scenario A (Wi-Fi) | Scenario B (Radio) |
|-------|--------------------|--------------------|
| **Drone Host** | `192.168.1.42` (Jetson IP) | `127.0.0.1` |
| **Command Port** | `14560` | `14560` |
| **Listen Port** | `14561` | `14561` |

Click **Connect**.

✅ **Checkpoint 7**: The **Status** field changes to `CONNECTED` and you see `[TX] STATUS_REQUEST` in the Telemetry Feed log.

---

## 8. Verifying the Connection is Live

After clicking **Connect**, the GCS automatically sends a `STATUS_REQUEST` command.

### What to look for in the Telemetry Feed log

```
[TX] STATUS_REQUEST
[RX:192.168.1.42:PORT] STATUS: {'state': 'IDLE', 'mission_active': False, ...}
```

- `[TX]` = packet sent from your laptop to the drone
- `[RX:IP:PORT]` = telemetry packet received back from the drone

If you see both lines, **the bidirectional link is working**.

### Manual link test from terminal

You can also test without the GUI. Open a second PowerShell:

```powershell
# Send a raw STATUS_REQUEST to the drone
echo "STATUS_REQUEST" | nc -u 192.168.1.42 14560

# Or using Python one-liner:
python -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.sendto(b'STATUS_REQUEST', ('192.168.1.42', 14560))"
```

And listen for replies on port 14561:
```powershell
# Listen for incoming telemetry packets
python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 14561))
print('Listening on :14561 ...')
while True:
    data, addr = s.recvfrom(4096)
    print(f'From {addr}: {data.decode()}')
"
```

---

## 9. Sending Mission Commands

Once connected, the **Mission Commands** toolbar contains all available commands:

| Button | UDP Payload | What It Does |
|--------|-------------|--------------|
| Start Mapping | `START_MAPPING` | Begins autonomous photogrammetry sweep |
| Run Detection | `RUN_DETECTION` | Runs YOLOv8 target detection on captured frames |
| Plan Route | `PLAN_ROUTE` | Computes optimal path to detected targets |
| Start Mission | `START_MISSION` | Arms motors and executes planned route |
| Abort | `ABORT` | Emergency stop — drone returns to home |
| Status Request | `STATUS_REQUEST` | Polls drone state (auto-sent on connect) |
| ▶ Start Recording | `START_RECORDING` | Starts video recording on the drone |
| ■ Stop Recording | `STOP_RECORDING` | Stops recording |

> ⚠️ **Before sending `START_MISSION`**, ensure `home_latitude` and `home_longitude` are set in `config.yaml` on the Jetson. The drone needs a home point to return to on abort/completion.

---

## 10. Debug Tips & Troubleshooting

### 10.1 Windows Firewall Blocking UDP

**Symptom**: `[RX]` messages never appear even though the drone is running.

**Fix**: Open an elevated PowerShell and run:
```powershell
# Allow inbound UDP on the GCS listen port
New-NetFirewallRule -DisplayName "DronaCharya GCS Telemetry" `
  -Direction Inbound -Protocol UDP -LocalPort 14561 -Action Allow

# Allow outbound UDP on command port (usually open by default)
New-NetFirewallRule -DisplayName "DronaCharya GCS Commands" `
  -Direction Outbound -Protocol UDP -RemotePort 14560 -Action Allow
```

Alternatively, temporarily disable the firewall to test:
```powershell
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False
# Remember to re-enable after testing!
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
```

---

### 10.2 Port Already In Use

**Symptom**: GCS shows `[ERROR] Telemetry listener error: [WinError 10048] Only one usage of each socket address`

**Fix**: Another process is holding port 14561. Find and kill it:
```powershell
# Find what's using port 14561
netstat -ano | findstr :14561

# Kill the process by PID (replace 12345)
Stop-Process -Id 12345 -Force
```
Then click **Connect** again.

---

### 10.3 Drone Not Receiving Commands

**Symptom**: `[TX]` appears in the GCS log, but the Jetson shows nothing.

**Checklist:**
1. Is the **Drone Host** IP correct? Double-check with `ping`.
2. Is the Jetson's firewall blocking port 14560?
   ```bash
   # On Jetson, temporarily disable ufw:
   sudo ufw disable
   # Then test; re-enable after: sudo ufw enable
   ```
3. Are you on the **same subnet**? (e.g., both on `192.168.1.x`)
4. For radio scenario: Is MAVProxy running on **both** sides? Check with:
   ```bash
   ps aux | grep mavproxy
   ```

---

### 10.4 Telemetry Feed Shows `[DISCONNECTED]`

**Symptom**: After connecting, the status flips back to `DISCONNECTED`.

**Cause**: The listener thread crashed because it couldn't bind port 14561.

**Fix**: See [§10.2](#102-port-already-in-use). Also check logs in `dronacharya.log` on the Jetson for the drone-side view:
```bash
tail -f ~/DronaCharya/dronacharya.log
```

---

### 10.5 Radio RSSI is Poor / Dropping

**Symptom**: Commands are intermittent or lost; RSSI in MAVProxy is < 100.

**Fixes (in order of ease):**
1. **Antenna orientation** — keep antennas vertical (upright), not horizontal.
2. **Distance** — SiK 915 MHz: up to ~1 km line-of-sight. Ensure no concrete walls between devices.
3. **Interference** — 915 MHz can conflict with other ISM-band devices. Try switching the radio's channel in MAVProxy:
   ```
   param set SYSID_THISMAV 1
   param set RC_CHANNEL 5
   ```
4. **Baud rate mismatch** — both radios must use the **same baud rate**. Check via `mavproxy.py --master=COM5,57600` and run `link`.

---

### 10.6 Commands Sent But Drone Ignores Them

**Symptom**: Jetson receives the command (visible in logs) but nothing happens.

**Cause**: The drone may be in a state that doesn't accept that command (e.g., `START_MISSION` while mapping is still running).

**Fix**: Check the state machine via `STATUS_REQUEST`. The telemetry feed will show:
```
STATUS: {'state': 'MAPPING', 'mission_active': True, ...}
```
Wait for the current task to complete, or send `ABORT` first, then retry.

---

### 10.7 `gcs_app.py` Crashes on Import

**Symptom**: `ModuleNotFoundError: No module named 'utils'`

**Fix**: Always run from the **project root**, not from inside the `gcs/` folder:
```powershell
# ✅ Correct
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py

# ❌ Wrong
cd gcs
python gcs_app.py
```

---

### 10.8 Connection Verification Cheat Sheet

Run this quick diagnostic script from your laptop to verify all paths:

```powershell
# Save as check_link.ps1 and run from the project root
python -c "
import socket, time, json

DRONE_IP   = '192.168.1.42'   # Change to your Jetson IP
CMD_PORT   = 14560
TELEM_PORT = 14561

# Open a listener first
recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
recv_sock.bind(('0.0.0.0', TELEM_PORT))
recv_sock.settimeout(5)

# Send STATUS_REQUEST
send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
send_sock.sendto(b'STATUS_REQUEST', (DRONE_IP, CMD_PORT))
print(f'[TX] STATUS_REQUEST -> {DRONE_IP}:{CMD_PORT}')

# Wait for reply
try:
    data, addr = recv_sock.recvfrom(4096)
    pkt = json.loads(data.decode())
    print(f'[RX] {pkt[\"type\"]}: {pkt[\"payload\"]}')
    print('✅ Link is UP')
except socket.timeout:
    print('❌ No reply — link is DOWN')
finally:
    recv_sock.close()
    send_sock.close()
"
```

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────────┐
│  DronaCharya Connection Quick Reference                      │
├──────────────────────┬──────────────────────────────────────┤
│ GCS app start        │ python gcs/gcs_app.py                │
│ Drone app start      │ python main.py  (on Jetson)          │
│ Command port         │ 14560  (drone listens)               │
│ Telemetry port       │ 14561  (laptop listens)              │
│ Protocol             │ UDP (no handshake)                   │
│ Config file          │ config/config.yaml                   │
│ Log file (drone)     │ dronacharya.log                      │
├──────────────────────┼──────────────────────────────────────┤
│ Test send (laptop)   │ python -c "import socket; ..."       │
│ Port check (Windows) │ netstat -ano | findstr :14561        │
│ Radio bridge         │ mavproxy.py --master=COM5,57600      │
│ Radio RSSI check     │ MAVProxy console → type: link        │
└──────────────────────┴──────────────────────────────────────┘
```
