#!/usr/bin/env python3
"""
radio_bridge.py — Bidirectional Serial ↔ UDP bridge for SiK telemetry radio
=============================================================================

Runs on BOTH the Jetson Nano (drone side) and the GCS laptop.
Transparently relays DronaCharya UDP packets over the SiK radio serial link.

Architecture
------------

  [GCS Laptop]                         [Jetson Nano / Drone]
  gcs_app.py                           main.py (DronaCharya)
      │  UDP                               │  UDP
      ▼  :14560 (send)                     ▼  :14560 (listen)
  radio_bridge  ←── serial/RF ──►  radio_bridge
      ▲  UDP                               ▲  UDP
      │  :14561 (listen)                   │  :14561 (send)
  gcs_app.py                           main.py (DronaCharya)

Usage
-----
  # ── Jetson Nano (drone side) ──────────────────────────────────────────
  python telemetry/radio_bridge.py --port /dev/ttyUSB0 --baud 57600 --role drone

  # ── GCS Laptop (Windows) ──────────────────────────────────────────────
  python telemetry/radio_bridge.py --port COM5 --baud 57600 --role gcs

  # ── Test / dry-run without radio hardware ─────────────────────────────
  python telemetry/radio_bridge.py --port /dev/ttyUSB0 --baud 57600 --role drone --verbose

Roles
-----
  drone  ┌─ serial RX → UDP TX to 127.0.0.1:14560  (DronaCharya cmd port)
         └─ UDP RX on  :14561                  → serial TX  (telemetry out)

  gcs    ┌─ UDP RX on  :14560                  → serial TX  (commands out)
         └─ serial RX → UDP TX to 127.0.0.1:14561  (gcs_app listen port)

Dependencies
------------
  pip install pyserial
"""

import argparse
import logging
import socket
import threading
import time
from typing import Optional

try:
    import serial
except ImportError:
    raise SystemExit(
        "pyserial is not installed. Run:  pip install pyserial"
    )

# ── Ports must match config/config.yaml and gcs_app.py ──────────────────────
CMD_PORT   = 14560   # DronaCharya command port (drone listens)
TELEM_PORT = 14561   # DronaCharya telemetry port (GCS listens)
LOCALHOST  = "127.0.0.1"

# Packet framing: simple newline delimiter keeps things readable in serial monitors
DELIMITER = b"\n"

log = logging.getLogger("radio_bridge")


# ─────────────────────────────────────────────────────────────────────────────
# Bridge core
# ─────────────────────────────────────────────────────────────────────────────

class RadioBridge:
    """
    Bidirectional serial ↔ UDP bridge.

    Parameters
    ----------
    port    : serial port path  (e.g. '/dev/ttyUSB0'  or  'COM5')
    baud    : baud rate matching both SiK radios (default 57600)
    role    : 'drone' or 'gcs'
    verbose : if True, log every forwarded packet
    """

    def __init__(self, port: str, baud: int, role: str, verbose: bool = False) -> None:
        if role not in ("drone", "gcs"):
            raise ValueError(f"role must be 'drone' or 'gcs', got '{role}'")

        self.port    = port
        self.baud    = baud
        self.role    = role
        self.verbose = verbose

        self._ser: Optional[serial.Serial] = None
        self._stop  = threading.Event()

        # Which UDP port to SEND received serial data to
        # Which UDP port to LISTEN on for data to be written to serial
        if role == "drone":
            self._udp_send_port   = CMD_PORT    # forward radio → local DronaCharya
            self._udp_listen_port = TELEM_PORT  # forward local DronaCharya → radio
        else:  # gcs
            self._udp_send_port   = TELEM_PORT  # forward radio → local gcs_app
            self._udp_listen_port = CMD_PORT    # forward local gcs_app → radio

    # ── Public ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        log.info("Opening serial port %s @ %d baud …", self.port, self.baud)
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        # Short settle time — SiK radios take ~1 s to negotiate link
        time.sleep(1.5)
        log.info("Serial port open. Role: %s", self.role.upper())
        log.info("  serial RX → UDP %s:%d", LOCALHOST, self._udp_send_port)
        log.info("  UDP       :%d  → serial TX", self._udp_listen_port)

        t1 = threading.Thread(
            target=self._serial_to_udp,
            name="serial→udp",
            daemon=True,
        )
        t2 = threading.Thread(
            target=self._udp_to_serial,
            name="udp→serial",
            daemon=True,
        )
        t1.start()
        t2.start()
        log.info("Bridge running. Press Ctrl+C to stop.")

        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._ser and self._ser.is_open:
            self._ser.close()
        log.info("Bridge stopped.")

    # ── Internal threads ─────────────────────────────────────────────────────

    def _serial_to_udp(self) -> None:
        """
        Reads newline-delimited packets from the serial port and forwards
        each as a UDP datagram to the appropriate local port.
        """
        tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(self._ser.in_waiting or 1)
            except serial.SerialException as exc:
                log.error("Serial read error: %s", exc)
                self._stop.set()
                break

            if not chunk:
                continue

            buf += chunk
            while DELIMITER in buf:
                packet, buf = buf.split(DELIMITER, 1)
                packet = packet.strip()
                if not packet:
                    continue
                if self.verbose:
                    log.debug("[serial→UDP:%d] %s", self._udp_send_port, packet)
                try:
                    tx_sock.sendto(packet, (LOCALHOST, self._udp_send_port))
                except OSError as exc:
                    log.warning("UDP send failed: %s", exc)

        tx_sock.close()

    def _udp_to_serial(self) -> None:
        """
        Listens on a local UDP port and writes each received datagram to
        the serial port (forwarding over the radio link).
        """
        rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rx_sock.bind((LOCALHOST, self._udp_listen_port))
        except OSError as exc:
            log.error(
                "Cannot bind UDP :%d — %s\n"
                "  Tip: check nothing else is using that port "
                "(netstat -ano | findstr :%d)",
                self._udp_listen_port, exc, self._udp_listen_port,
            )
            self._stop.set()
            return

        rx_sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = rx_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue

            # Append delimiter so the other side can frame-split correctly
            framed = data.strip() + DELIMITER
            if self.verbose:
                log.debug("[UDP:%d→serial] from %s: %s", self._udp_listen_port, addr, data)
            try:
                self._ser.write(framed)
            except serial.SerialException as exc:
                log.error("Serial write error: %s", exc)
                self._stop.set()
                break

        rx_sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DronaCharya serial ↔ UDP radio bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--port", required=True,
        help="Serial port (e.g. /dev/ttyUSB0 or COM5)",
    )
    parser.add_argument(
        "--baud", type=int, default=57600,
        help="Baud rate (must match both SiK radios, default: 57600)",
    )
    parser.add_argument(
        "--role", required=True, choices=["drone", "gcs"],
        help="'drone' = runs on Jetson Nano; 'gcs' = runs on GCS laptop",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every forwarded packet",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = RadioBridge(
        port=args.port,
        baud=args.baud,
        role=args.role,
        verbose=args.verbose,
    )
    bridge.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
