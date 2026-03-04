#!/usr/bin/env python3
"""
radio_bridge.py - Bidirectional Serial <-> UDP bridge for SiK telemetry radio.

Runs on BOTH the Jetson Nano (drone side) and the GCS laptop.
Transparently relays DronaCharya UDP packets over the SiK radio serial link.
"""

import argparse
from collections import deque
import logging
import socket
import threading
import time
from typing import Optional

try:
    import serial
except ImportError:
    raise SystemExit("pyserial is not installed. Run: pip install pyserial")


# Ports must match config/config.yaml and gcs_app.py.
CMD_PORT = 14560   # DronaCharya command port (drone listens)
TELEM_PORT = 14561  # DronaCharya telemetry port (GCS listens)
LOCALHOST = "127.0.0.1"

# Packet framing: simple newline delimiter keeps things readable in serial monitors.
DELIMITER = b"\n"

# Internal heartbeat packets (never forwarded to local UDP apps).
PING_PREFIX = b"__RB_PING__:"
PONG_PREFIX = b"__RB_PONG__:"

# Reliability tuning.
RECONNECT_DELAY_SEC = 5.0
HEARTBEAT_INTERVAL_SEC = 2.0
HEARTBEAT_TIMEOUT_SEC = 8.0
SERIAL_SETTLE_SEC = 1.5
MAX_GCS_PENDING_COMMANDS = 128

log = logging.getLogger("radio_bridge")


class RadioBridge:
    """
    Bidirectional serial <-> UDP bridge.

    Parameters
    ----------
    port    : serial port path (e.g. "/dev/ttyUSB0" or "COM5")
    baud    : baud rate matching both SiK radios (default 57600)
    role    : "drone" or "gcs"
    verbose : if True, log every forwarded packet
    """

    def __init__(self, port: str, baud: int, role: str, verbose: bool = False) -> None:
        if role not in ("drone", "gcs"):
            raise ValueError(f"role must be 'drone' or 'gcs', got '{role}'")

        self.port = port
        self.baud = baud
        self.role = role
        self.verbose = verbose

        self._ser: Optional[serial.Serial] = None
        self._ser_state_lock = threading.Lock()
        self._ser_write_lock = threading.Lock()

        self._stop = threading.Event()
        self._serial_ready = threading.Event()
        self._serial_error = threading.Event()

        self._health_lock = threading.Lock()
        self._last_ping_sent = 0.0
        self._last_heartbeat_rx = 0.0
        self._heartbeat_warned = False
        self._remote_alive = False
        self._pending_lock = threading.Lock()
        self._pending_gcs_commands = deque(maxlen=MAX_GCS_PENDING_COMMANDS)

        # Which UDP port to SEND received serial data to.
        # Which UDP port to LISTEN on for data to be written to serial.
        if role == "drone":
            self._udp_send_port = CMD_PORT    # forward radio -> local DronaCharya
            self._udp_listen_port = TELEM_PORT  # forward local DronaCharya -> radio
        else:  # gcs
            self._udp_send_port = TELEM_PORT  # forward radio -> local gcs_app
            self._udp_listen_port = CMD_PORT  # forward local gcs_app -> radio

    # Public
    def start(self) -> None:
        t1 = threading.Thread(target=self._serial_to_udp, name="serial_to_udp", daemon=True)
        t2 = threading.Thread(target=self._udp_to_serial, name="udp_to_serial", daemon=True)
        t3 = threading.Thread(target=self._heartbeat_monitor, name="heartbeat", daemon=True)
        t1.start()
        t2.start()
        t3.start()
        log.info("Bridge supervisor running. Press Ctrl+C to stop.")

        try:
            while not self._stop.is_set():
                if not self._serial_ready.is_set():
                    if self._open_serial():
                        continue
                    if self._stop.wait(RECONNECT_DELAY_SEC):
                        break
                    continue

                if not self._serial_error.wait(timeout=0.5):
                    continue

                if self._stop.is_set():
                    break

                self._serial_error.clear()
                self._close_serial()
                log.info("Retrying serial reconnect in %.0f seconds ...", RECONNECT_DELAY_SEC)
                if self._stop.wait(RECONNECT_DELAY_SEC):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        already_stopping = self._stop.is_set()
        self._stop.set()
        self._serial_error.set()  # Wake the supervisor wait loop.
        self._close_serial()
        if not already_stopping:
            log.info("Bridge stopped.")

    # Internal helpers
    def _get_serial(self) -> Optional[serial.Serial]:
        with self._ser_state_lock:
            return self._ser

    def _mark_serial_error(self, context: str, exc: Exception) -> None:
        if self._stop.is_set():
            return
        if not self._serial_error.is_set():
            log.error("%s: %s", context, exc)
        self._serial_error.set()

    def _open_serial(self) -> bool:
        log.info("Opening serial port %s @ %d baud ...", self.port, self.baud)
        try:
            ser = serial.Serial(self.port, self.baud, timeout=1)
        except (serial.SerialException, OSError) as exc:
            log.error("Unable to open serial port %s: %s", self.port, exc)
            return False

        # SiK radios usually need a short post-open settle period.
        time.sleep(SERIAL_SETTLE_SEC)
        if self._stop.is_set():
            try:
                ser.close()
            except Exception:
                pass
            return False

        with self._ser_state_lock:
            self._ser = ser

        now = time.monotonic()
        with self._health_lock:
            self._last_ping_sent = 0.0
            self._last_heartbeat_rx = now
            self._heartbeat_warned = False
            self._remote_alive = False

        self._serial_error.clear()
        self._serial_ready.set()
        log.info("Serial port open. Role: %s", self.role.upper())
        log.info("  serial RX -> UDP %s:%d", LOCALHOST, self._udp_send_port)
        log.info("  UDP       :%d -> serial TX", self._udp_listen_port)
        return True

    def _close_serial(self) -> None:
        self._serial_ready.clear()
        with self._ser_state_lock:
            ser = self._ser
            self._ser = None

        if ser is not None and ser.is_open:
            try:
                ser.close()
            except Exception as exc:
                log.warning("Error while closing serial port: %s", exc)

    def _serial_write(self, payload: bytes, error_context: str) -> bool:
        ser = self._get_serial()
        if ser is None:
            return False

        try:
            with self._ser_write_lock:
                ser.write(payload)
            return True
        except (serial.SerialException, OSError) as exc:
            self._mark_serial_error(error_context, exc)
            return False

    def _update_heartbeat_rx(self) -> None:
        now = time.monotonic()
        restored = False
        with self._health_lock:
            restored = not self._remote_alive
            self._remote_alive = True
            self._heartbeat_warned = False
            self._last_heartbeat_rx = now

        if restored:
            log.info("Telemetry heartbeat restored.")

    def _handle_control_packet(self, packet: bytes) -> bool:
        if packet.startswith(PING_PREFIX):
            self._update_heartbeat_rx()
            token = packet[len(PING_PREFIX):]
            self._serial_write(
                PONG_PREFIX + token + DELIMITER,
                "Serial heartbeat pong write error",
            )
            return True

        if packet.startswith(PONG_PREFIX):
            self._update_heartbeat_rx()
            return True

        return False

    def _queue_gcs_command(self, payload: bytes) -> None:
        if self.role != "gcs":
            return

        with self._pending_lock:
            # Avoid queue spam from repeated status polling while link is down.
            if payload == b"STATUS_REQUEST" and self._pending_gcs_commands:
                if self._pending_gcs_commands[-1] == payload:
                    return

            was_full = len(self._pending_gcs_commands) == self._pending_gcs_commands.maxlen
            self._pending_gcs_commands.append(payload)
            queued = len(self._pending_gcs_commands)

        if was_full:
            log.warning("Pending GCS command buffer full. Oldest queued command was dropped.")
        if self.verbose:
            log.debug("Queued GCS command while link down (queued=%d): %s", queued, payload)

    def _flush_gcs_command_queue(self) -> int:
        if self.role != "gcs" or not self._serial_ready.is_set():
            return 0

        flushed = 0
        while not self._stop.is_set():
            with self._pending_lock:
                if not self._pending_gcs_commands:
                    break
                payload = self._pending_gcs_commands.popleft()

            if not self._serial_write(payload + DELIMITER, "Serial write error"):
                # Push back for next reconnect cycle.
                with self._pending_lock:
                    self._pending_gcs_commands.appendleft(payload)
                break
            flushed += 1

        if flushed:
            log.info("Flushed %d queued GCS command(s) after reconnect.", flushed)
        return flushed

    # Internal threads
    def _serial_to_udp(self) -> None:
        """
        Reads newline-delimited packets from the serial port and forwards
        each as a UDP datagram to the appropriate local port.
        """
        tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        buf = b""

        while not self._stop.is_set():
            if not self._serial_ready.wait(timeout=0.2):
                buf = b""
                continue

            ser = self._get_serial()
            if ser is None:
                continue

            try:
                chunk = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as exc:
                self._mark_serial_error("Serial read error", exc)
                continue

            if not chunk:
                continue

            buf += chunk
            while DELIMITER in buf:
                packet, buf = buf.split(DELIMITER, 1)
                packet = packet.strip()
                if not packet:
                    continue

                if self._handle_control_packet(packet):
                    continue

                if self.verbose:
                    log.debug("[serial->UDP:%d] %s", self._udp_send_port, packet)
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
                "Cannot bind UDP :%d - %s\n"
                "  Tip: check nothing else is using that port "
                "(netstat -ano | findstr :%d)",
                self._udp_listen_port, exc, self._udp_listen_port,
            )
            self._stop.set()
            return

        rx_sock.settimeout(1.0)
        dropped_warning_logged = False
        while not self._stop.is_set():
            try:
                data, addr = rx_sock.recvfrom(4096)
            except socket.timeout:
                # No local packet right now; still attempt queued command flush on GCS side.
                self._flush_gcs_command_queue()
                continue
            except OSError:
                break

            if not data:
                continue

            payload = data.strip()
            if not payload:
                continue

            if self.verbose:
                log.debug("[UDP:%d->serial] from %s: %s", self._udp_listen_port, addr, data)

            if not self._serial_ready.is_set():
                if self.role == "gcs":
                    self._queue_gcs_command(payload)
                    if not dropped_warning_logged:
                        log.warning("Serial link unavailable. Queueing GCS commands until reconnect.")
                        dropped_warning_logged = True
                else:
                    if not dropped_warning_logged:
                        log.warning("Serial link unavailable. Dropping UDP packets until reconnect.")
                        dropped_warning_logged = True
                continue

            if dropped_warning_logged:
                if self.role == "gcs":
                    log.info("Serial link restored. Resuming UDP -> serial forwarding and flushing queue.")
                else:
                    log.info("Serial link restored. Resuming UDP -> serial forwarding.")
                dropped_warning_logged = False

            # Preserve command order: flush queued commands before the new one.
            self._flush_gcs_command_queue()
            self._serial_write(payload + DELIMITER, "Serial write error")

        rx_sock.close()

    def _heartbeat_monitor(self) -> None:
        while not self._stop.is_set():
            if not self._serial_ready.wait(timeout=0.5):
                continue

            now = time.monotonic()
            with self._health_lock:
                should_ping = (now - self._last_ping_sent) >= HEARTBEAT_INTERVAL_SEC
                last_rx = self._last_heartbeat_rx
                warned = self._heartbeat_warned

            if should_ping:
                token = str(int(now * 1000)).encode("ascii")
                if self._serial_write(
                    PING_PREFIX + token + DELIMITER,
                    "Serial heartbeat write error",
                ):
                    with self._health_lock:
                        self._last_ping_sent = now

            elapsed = now - last_rx
            if elapsed > HEARTBEAT_TIMEOUT_SEC and not warned:
                with self._health_lock:
                    self._heartbeat_warned = True
                    self._remote_alive = False
                log.warning(
                    "Heartbeat timeout (%.1fs without ping/pong). Link may be down.",
                    elapsed,
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DronaCharya serial <-> UDP radio bridge",
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
