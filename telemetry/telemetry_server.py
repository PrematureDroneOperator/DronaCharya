from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from typing import Any, Callable

from telemetry.command_listener import CommandListener
from utils.config import TelemetryConfig


class TelemetryServer:
    def __init__(self, config: TelemetryConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self._listener: CommandListener | None = None
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self, on_command: Callable[[str, tuple[str, int]], None]) -> None:
        self._listener = CommandListener(
            host=self.config.command_host,
            port=self.config.command_port,
            on_command=on_command,
            logger=self.logger,
        )
        self._listener.start()
        self.send_event("INFO", {"message": "Telemetry server online"})

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
        self._socket.close()

    def send_status(self, status: dict[str, Any]) -> None:
        self.send_event("STATUS", status)

    def send_log(self, message: str, level: str = "INFO") -> None:
        self.send_event("LOG", {"level": level, "message": message})

    def send_event(self, event_type: str, payload: dict[str, Any]) -> None:
        packet = {
            "type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        encoded = json.dumps(packet).encode("utf-8")
        try:
            self._socket.sendto(encoded, (self.config.gcs_host, self.config.gcs_port))
        except OSError as exc:
            self.logger.warning("Failed to send telemetry packet: %s", exc)
