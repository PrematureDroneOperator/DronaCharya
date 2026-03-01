import logging
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Deque, List, Tuple


class RingBufferLogHandler(logging.Handler):
    def __init__(self, max_entries: int = 500) -> None:
        super().__init__()
        self._records = deque(maxlen=max_entries)  # type: Deque[str]
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        with self._lock:
            self._records.append(message)

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self._records)


def setup_logger(name: str, log_file: Path, level: str = "INFO") -> Tuple[logging.Logger, RingBufferLogHandler]:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    ring_handler = RingBufferLogHandler()
    ring_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.addHandler(ring_handler)

    return logger, ring_handler
