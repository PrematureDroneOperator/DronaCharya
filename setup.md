# DronaCharya Connection Setup (Pixhawk-Routed Radio)

## Hardware Topology

This setup assumes:

1. Ground telemetry radio (Simplify/SiK) is connected to the GCS laptop.
2. Air telemetry radio is connected to the Pixhawk.
3. Jetson is connected to the Pixhawk via serial (for companion link).
4. `telemetry/radio_bridge.py` runs on both GCS and Jetson and tunnels app UDP traffic over MAVLink `TUNNEL` frames through Pixhawk routing.

The app command set and behavior are unchanged.

## Architecture

```text
GCS Laptop                             Pixhawk + Air Radio                    Jetson
-----------                            -------------------                    ------
gcs_app.py (UDP localhost)   <---->    MAVLink routing over RF   <---->      radio_bridge.py
radio_bridge.py (COMx)                                                      main.py (UDP localhost)
```

Local UDP ports remain:

- `14560`: commands
- `14561`: telemetry

## 1. Prerequisites

- Python 3.x on both GCS and Jetson
- Dependencies installed:
  - `pip install -r requirements.txt`
- Serial permissions on Jetson (`dialout` group or equivalent)
- Ground radio COM port identified (Windows: `COMx`)
- Jetson-to-Pixhawk serial device identified (`/dev/ttyTHS1`, `/dev/ttyUSB0`, etc.)
- Pixhawk serial links set to MAVLink (`SERIALx_PROTOCOL=2` recommended for MAVLink2/TUNNEL support)

## 2. Configure `config/config.yaml` on Jetson

Keep telemetry local because bridge handles transport:

```yaml
telemetry:
  command_host: "0.0.0.0"
  command_port: 14560
  gcs_host: "127.0.0.1"
  gcs_port: 14561
```

For mission execution, set the Pixhawk MAVLink link used by DronaCharya:

```yaml
mission:
  mavlink_connection: "/dev/ttyTHS1"   # or your actual Jetson<->Pixhawk link
  mavlink_baudrate: 57600
```

## 3. Start Bridge on Jetson (Drone Side)

```bash
cd ~/DronaCharya
python3 telemetry/radio_bridge.py --port /dev/ttyTHS1 --baud 57600 --role drone --verbose
```

Use your actual Jetson-to-Pixhawk serial port if different.

Expected logs include:

- MAVLink link opened
- UDP listen/send port mapping
- bridge supervisor running

## 4. Start Bridge on GCS (Laptop Side)

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python telemetry/radio_bridge.py --port COM5 --baud 57600 --role gcs --verbose
```

Use your actual ground radio COM port.

## 5. Start Drone App on Jetson

```bash
cd ~/DronaCharya
python3 main.py
```

Expected:

- command listener on `:14560`
- telemetry server online

## 6. Start GCS App on Laptop

```powershell
cd C:\Users\LENOVO\Documents\college\Hackathons\Cognizance\DronaCharya
python gcs/gcs_app.py
```

Use:

- Host: `127.0.0.1`
- Command Port: `14560`
- Listen Port: `14561`

Click **Connect**, then issue `STATUS_REQUEST` or any mission/survey command.

## 7. Bridge ID Defaults

The MAVLink bridge uses non-autopilot IDs by default:

- GCS bridge source system: `246`
- Drone bridge source system: `247`

You can override if needed:

```bash
python3 telemetry/radio_bridge.py ... --source-system 230 --target-system 231
```

Both sides must target each other correctly.

## 8. Notes for Pixhawk-Routed Radio

- Close Mission Planner/QGroundControl if they are exclusively locking the same COM port.
- Ensure Pixhawk link baud matches bridge baud.
- Ensure both radios are linked and RF quality is healthy.
- If no packets flow, first verify both bridges show heartbeat/MAVLink link readiness.

## 9. Quick Health Test

On GCS (with both bridges + drone app runnindg):

```powershell
python -c "
import socket, json
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx.bind(('127.0.0.1',14561)); rx.settimeout(8)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); tx.sendto(b'STATUS_REQUEST', ('127.0.0.1',14560))
print('TX STATUS_REQUEST')
data, _ = rx.recvfrom(4096)
print('RX', data.decode('utf-8', 'ignore'))
"
```

If you receive a `STATUS` packet, command/telemetry transport is working.
