import socket
import threading
from typing import Callable, Optional, Tuple


VALID_COMMANDS = {
    "START_MAPPING",
    "RUN_DETECTION",
    "PLAN_ROUTE",
    "START_MISSION",
    "ABORT",
    "STATUS_REQUEST",
    "START_RECORDING",
    "STOP_RECORDING",
}


class CommandListener:
    def __init__(self, host: str, port: int, on_command: Callable[[str, Tuple[str, int]], None], logger) -> None:
        self.host = host
        self.port = port
        self.on_command = on_command
        self.logger = logger

        self._thread = None  # type: Optional[threading.Thread]
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="CommandListener", daemon=True)
        self._thread.start()
        self.logger.info("Command listener started on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.logger.info("Command listener stopped.")

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            self.logger.error("Failed to bind command listener on %s:%s (%s)", self.host, self.port, exc)
            sock.close()
            return
        sock.settimeout(1.0)

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    break

                raw = data.decode("utf-8", errors="ignore").strip().upper()
                if raw not in VALID_COMMANDS:
                    self.logger.warning("Unknown command '%s' from %s", raw, addr)
                    continue

                self.logger.info("Received command '%s' from %s", raw, addr)
                self.on_command(raw, addr)
        finally:
            sock.close()
