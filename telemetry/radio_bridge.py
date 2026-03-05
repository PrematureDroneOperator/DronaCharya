#!/usr/bin/env python3
"""
radio_bridge.py - UDP <-> MAVLink bridge for Pixhawk-routed radio links.

This bridge keeps the app command/telemetry contract unchanged:
- local UDP :14560 carries commands to the drone app
- local UDP :14561 carries telemetry to the GCS app

Transport layer is now MAVLink tunnel traffic routed through Pixhawk.
Typical topology:
- GCS bridge talks to ground radio COM port
- Drone bridge talks to Pixhawk serial port on Jetson
"""

import argparse
from collections import deque
import logging
import socket
import struct
import threading
import time
from typing import Any, Deque, Dict, Optional, Tuple

from pymavlink import mavutil


CMD_PORT = 14560
TELEM_PORT = 14561
LOCALHOST = "127.0.0.1"

# Tunnel payload framing.
FRAME_MAGIC = b"DC"
FRAME_HEADER_FMT = "!2sB H B B"
FRAME_HEADER_LEN = struct.calcsize(FRAME_HEADER_FMT)  # 7 bytes
TUNNEL_PAYLOAD_MAX = 128
FRAME_DATA_MAX = TUNNEL_PAYLOAD_MAX - FRAME_HEADER_LEN

CHANNEL_CMD = 1
CHANNEL_TELEM = 2

MAX_PENDING_UDP = 256
RECONNECT_DELAY_SEC = 4.0
HEARTBEAT_SEND_SEC = 1.0
REASSEMBLY_TTL_SEC = 10.0

LOG = logging.getLogger("radio_bridge")


def _channel_name(channel: int) -> str:
    if channel == CHANNEL_CMD:
        return "CMD"
    if channel == CHANNEL_TELEM:
        return "TELEM"
    return "UNKNOWN({0})".format(channel)


class MavTunnelBridge:
    def __init__(
        self,
        port: str,
        baud: int,
        role: str,
        source_system: int,
        source_component: int,
        target_system: int,
        target_component: int,
        payload_type: int,
        verbose: bool = False,
    ) -> None:
        if role not in ("drone", "gcs"):
            raise ValueError("role must be 'drone' or 'gcs', got '{0}'".format(role))

        self.port = port
        self.baud = baud
        self.role = role
        self.source_system = source_system
        self.source_component = source_component
        self.target_system = target_system
        self.target_component = target_component
        self.payload_type = payload_type
        self.verbose = verbose

        if role == "gcs":
            self._udp_listen_port = CMD_PORT
            self._udp_send_port = TELEM_PORT
            self._tx_channel = CHANNEL_CMD
            self._rx_channel = CHANNEL_TELEM
        else:
            self._udp_listen_port = TELEM_PORT
            self._udp_send_port = CMD_PORT
            self._tx_channel = CHANNEL_TELEM
            self._rx_channel = CHANNEL_CMD

        self._stop = threading.Event()
        self._mav_ready = threading.Event()
        self._mav_error = threading.Event()

        self._mav_lock = threading.Lock()
        self._mav_write_lock = threading.Lock()
        self._mav = None  # type: Optional[Any]

        self._msg_id_lock = threading.Lock()
        self._next_msg_id = 1

        self._pending_udp_lock = threading.Lock()
        self._pending_udp = deque(maxlen=MAX_PENDING_UDP)  # type: Deque[bytes]

        self._reassembly_lock = threading.Lock()
        self._reassembly = {}  # type: Dict[Tuple[int, int, int], Dict[str, object]]

    def start(self) -> None:
        t_udp = threading.Thread(target=self._udp_to_mav_loop, name="udp_to_mav", daemon=True)
        t_mav = threading.Thread(target=self._mav_to_udp_loop, name="mav_to_udp", daemon=True)
        t_hb = threading.Thread(target=self._heartbeat_loop, name="bridge_heartbeat", daemon=True)
        t_gc = threading.Thread(target=self._reassembly_gc_loop, name="reassembly_gc", daemon=True)

        t_udp.start()
        t_mav.start()
        t_hb.start()
        t_gc.start()

        LOG.info("Bridge supervisor running. Press Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                if not self._mav_ready.is_set():
                    if self._open_mavlink():
                        continue
                    if self._stop.wait(RECONNECT_DELAY_SEC):
                        break
                    continue

                if not self._mav_error.wait(timeout=0.5):
                    continue

                if self._stop.is_set():
                    break

                self._mav_error.clear()
                self._close_mavlink()
                LOG.info("Retrying MAVLink reconnect in %.0f seconds ...", RECONNECT_DELAY_SEC)
                if self._stop.wait(RECONNECT_DELAY_SEC):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        already_stopping = self._stop.is_set()
        self._stop.set()
        self._mav_error.set()
        self._close_mavlink()
        if not already_stopping:
            LOG.info("Bridge stopped.")

    def _get_mav(self):
        with self._mav_lock:
            return self._mav

    def _open_mavlink(self) -> bool:
        LOG.info(
            "Opening MAVLink port %s @ %d (role=%s, src=%d.%d, target=%d.%d) ...",
            self.port,
            self.baud,
            self.role.upper(),
            self.source_system,
            self.source_component,
            self.target_system,
            self.target_component,
        )
        try:
            mav = mavutil.mavlink_connection(
                self.port,
                baud=self.baud,
                source_system=self.source_system,
                source_component=self.source_component,
                autoreconnect=False,
            )
        except Exception as exc:
            LOG.error("Unable to open MAVLink port %s: %s", self.port, exc)
            return False

        try:
            hb = mav.wait_heartbeat(timeout=10)
            if hb is None:
                raise RuntimeError("heartbeat timeout")
        except Exception as exc:
            LOG.error("No heartbeat on %s: %s", self.port, exc)
            try:
                mav.close()
            except Exception:
                pass
            return False

        with self._mav_lock:
            self._mav = mav

        self._mav_error.clear()
        self._mav_ready.set()
        LOG.info("MAVLink link ready on %s", self.port)
        LOG.info("  UDP listen : %s:%d -> MAV tunnel (%s)", LOCALHOST, self._udp_listen_port, _channel_name(self._tx_channel))
        LOG.info("  MAV tunnel (%s) -> UDP send %s:%d", _channel_name(self._rx_channel), LOCALHOST, self._udp_send_port)
        return True

    def _close_mavlink(self) -> None:
        self._mav_ready.clear()
        with self._mav_lock:
            mav = self._mav
            self._mav = None
        if mav is not None:
            try:
                mav.close()
            except Exception as exc:
                LOG.warning("Error while closing MAVLink connection: %s", exc)

    def _mark_mav_error(self, context: str, exc: Exception) -> None:
        if self._stop.is_set():
            return
        if not self._mav_error.is_set():
            LOG.error("%s: %s", context, exc)
        self._mav_error.set()

    def _next_message_id(self) -> int:
        with self._msg_id_lock:
            self._next_msg_id = (self._next_msg_id + 1) % 65536
            if self._next_msg_id == 0:
                self._next_msg_id = 1
            return self._next_msg_id

    def _queue_udp_payload(self, payload: bytes) -> None:
        with self._pending_udp_lock:
            was_full = len(self._pending_udp) == self._pending_udp.maxlen
            self._pending_udp.append(payload)
        if was_full:
            LOG.warning("Pending UDP queue full. Oldest payload dropped.")

    def _flush_udp_queue(self) -> int:
        if not self._mav_ready.is_set():
            return 0
        flushed = 0
        while not self._stop.is_set():
            with self._pending_udp_lock:
                if not self._pending_udp:
                    break
                payload = self._pending_udp.popleft()
            if not self._send_tunnel_payload(payload, self._tx_channel):
                with self._pending_udp_lock:
                    self._pending_udp.appendleft(payload)
                break
            flushed += 1
        if flushed:
            LOG.info("Flushed %d queued UDP payload(s) after reconnect.", flushed)
        return flushed

    def _send_tunnel_payload(self, payload: bytes, channel: int) -> bool:
        mav = self._get_mav()
        if mav is None:
            return False
        msg_id = self._next_message_id()
        parts = [payload[i : i + FRAME_DATA_MAX] for i in range(0, len(payload), FRAME_DATA_MAX)] or [b""]
        total_parts = len(parts)

        for idx, part in enumerate(parts, start=1):
            header = struct.pack(FRAME_HEADER_FMT, FRAME_MAGIC, channel, msg_id, idx, total_parts)
            frame = header + part
            padded = frame + (b"\x00" * (TUNNEL_PAYLOAD_MAX - len(frame)))

            try:
                with self._mav_write_lock:
                    mav.mav.tunnel_send(
                        self.target_system,
                        self.target_component,
                        self.payload_type,
                        len(frame),
                        padded,
                    )
            except Exception as exc:
                self._mark_mav_error("MAV tunnel send error", exc)
                return False

            if self.verbose:
                LOG.debug(
                    "[UDP->MAV:%s] msg=%d part=%d/%d bytes=%d",
                    _channel_name(channel),
                    msg_id,
                    idx,
                    total_parts,
                    len(part),
                )
        return True

    def _udp_to_mav_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((LOCALHOST, self._udp_listen_port))
        except OSError as exc:
            LOG.error(
                "Cannot bind UDP :%d - %s\n"
                "  Tip: check nothing else is using that port (netstat -ano | findstr :%d)",
                self._udp_listen_port,
                exc,
                self._udp_listen_port,
            )
            self._stop.set()
            return
        sock.settimeout(1.0)

        warned_link_down = False
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                if self._mav_ready.is_set():
                    self._flush_udp_queue()
                    if warned_link_down:
                        LOG.info("MAVLink restored. Resuming UDP -> MAV forwarding.")
                        warned_link_down = False
                continue
            except OSError:
                break

            payload = data.strip()
            if not payload:
                continue

            if not self._mav_ready.is_set():
                self._queue_udp_payload(payload)
                if not warned_link_down:
                    LOG.warning("MAVLink unavailable. Queueing UDP payloads until reconnect.")
                    warned_link_down = True
                continue

            if warned_link_down:
                LOG.info("MAVLink restored. Resuming UDP -> MAV forwarding.")
                warned_link_down = False

            self._flush_udp_queue()
            if not self._send_tunnel_payload(payload, self._tx_channel):
                self._queue_udp_payload(payload)
                continue

            if self.verbose:
                LOG.debug("[UDP:%d->MAV] from %s bytes=%d", self._udp_listen_port, addr, len(payload))
        sock.close()

    def _parse_tunnel_frame(self, msg) -> Optional[Tuple[int, int, int, int, bytes, int]]:
        try:
            payload_length = int(getattr(msg, "payload_length", 0))
            payload_type = int(getattr(msg, "payload_type", -1))
            if payload_type != int(self.payload_type):
                return None

            raw = bytes(getattr(msg, "payload", b"")[:payload_length])
            if len(raw) < FRAME_HEADER_LEN:
                return None

            magic, channel, msg_id, part_idx, total_parts = struct.unpack(FRAME_HEADER_FMT, raw[:FRAME_HEADER_LEN])
            if magic != FRAME_MAGIC:
                return None
            if total_parts < 1 or part_idx < 1 or part_idx > total_parts:
                return None

            chunk = raw[FRAME_HEADER_LEN:]
            src_system = 0
            try:
                src_system = int(msg.get_srcSystem())
            except Exception:
                pass
            return channel, msg_id, part_idx, total_parts, chunk, src_system
        except Exception:
            return None

    def _mav_to_udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self._stop.is_set():
            if not self._mav_ready.wait(timeout=0.5):
                continue
            mav = self._get_mav()
            if mav is None:
                continue

            try:
                msg = mav.recv_match(type="TUNNEL", blocking=True, timeout=1.0)
            except Exception as exc:
                self._mark_mav_error("MAV tunnel receive error", exc)
                continue

            if msg is None:
                continue

            parsed = self._parse_tunnel_frame(msg)
            if parsed is None:
                continue
            channel, msg_id, part_idx, total_parts, chunk, src_system = parsed
            if channel != self._rx_channel:
                continue

            key = (src_system, channel, msg_id)
            complete_payload = None  # type: Optional[bytes]
            with self._reassembly_lock:
                entry = self._reassembly.get(key)
                if entry is None:
                    entry = {"total": total_parts, "parts": {}, "updated": time.monotonic()}
                    self._reassembly[key] = entry
                else:
                    entry["total"] = total_parts
                    entry["updated"] = time.monotonic()

                parts = entry["parts"]  # type: ignore[assignment]
                parts[part_idx] = chunk
                if len(parts) == int(entry["total"]):
                    ordered = []
                    for idx in range(1, int(entry["total"]) + 1):
                        if idx not in parts:
                            ordered = []
                            break
                        ordered.append(parts[idx])
                    if ordered:
                        complete_payload = b"".join(ordered)
                        del self._reassembly[key]

            if complete_payload is None:
                continue

            if self.verbose:
                LOG.debug(
                    "[MAV->UDP:%s] src=%d msg=%d parts=%d bytes=%d",
                    _channel_name(channel),
                    src_system,
                    msg_id,
                    total_parts,
                    len(complete_payload),
                )
            try:
                sock.sendto(complete_payload, (LOCALHOST, self._udp_send_port))
            except OSError as exc:
                LOG.warning("UDP send failed: %s", exc)
        sock.close()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if not self._mav_ready.wait(timeout=0.5):
                continue
            mav = self._get_mav()
            if mav is None:
                continue
            try:
                with self._mav_write_lock:
                    mav.mav.heartbeat_send(
                        mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                        0,
                        0,
                        mavutil.mavlink.MAV_STATE_ACTIVE,
                    )
            except Exception as exc:
                self._mark_mav_error("MAV heartbeat send error", exc)
            time.sleep(HEARTBEAT_SEND_SEC)

    def _reassembly_gc_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            now = time.monotonic()
            with self._reassembly_lock:
                stale = [
                    key
                    for key, entry in self._reassembly.items()
                    if now - float(entry.get("updated", now)) > REASSEMBLY_TTL_SEC
                ]
                for key in stale:
                    del self._reassembly[key]


def _default_ids_for_role(role: str) -> Tuple[int, int]:
    # Two non-autopilot IDs so Pixhawk can MAVLink-route between links.
    if role == "gcs":
        return 246, 247
    return 247, 246


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DronaCharya UDP <-> MAVLink tunnel bridge (Pixhawk-routed radio)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port", required=True, help="Serial/MAVLink port (e.g. COM5, /dev/ttyTHS1, /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=57600, help="Baud rate (default: 57600)")
    parser.add_argument("--role", required=True, choices=["drone", "gcs"], help="Bridge role")
    parser.add_argument("--source-system", type=int, default=None, help="MAVLink source system ID")
    parser.add_argument("--source-component", type=int, default=191, help="MAVLink source component ID")
    parser.add_argument("--target-system", type=int, default=None, help="MAVLink target bridge system ID")
    parser.add_argument("--target-component", type=int, default=191, help="MAVLink target bridge component ID")
    parser.add_argument("--payload-type", type=int, default=49001, help="MAVLink TUNNEL payload_type tag")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose packet logs")
    args = parser.parse_args()

    default_src, default_tgt = _default_ids_for_role(args.role)
    source_system = int(args.source_system) if args.source_system is not None else default_src
    target_system = int(args.target_system) if args.target_system is not None else default_tgt

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = MavTunnelBridge(
        port=args.port,
        baud=args.baud,
        role=args.role,
        source_system=source_system,
        source_component=int(args.source_component),
        target_system=target_system,
        target_component=int(args.target_component),
        payload_type=int(args.payload_type),
        verbose=bool(args.verbose),
    )
    bridge.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
