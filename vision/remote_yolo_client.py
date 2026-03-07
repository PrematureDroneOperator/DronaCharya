import base64
import json
import socket
from typing import Any, Dict, List, Optional

import cv2


class RemoteYoloError(RuntimeError):
    pass


class RemoteYoloTimeout(RemoteYoloError):
    pass


class RemoteYoloClient:
    """TCP newline-JSON client for external YOLO detector service."""

    def __init__(
        self,
        host: str,
        port: int,
        request_timeout_sec: float,
        connect_timeout_sec: float,
        jpeg_quality: int,
        logger=None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.request_timeout_sec = float(max(0.1, request_timeout_sec))
        self.connect_timeout_sec = float(max(0.1, connect_timeout_sec))
        self.jpeg_quality = int(max(10, min(100, jpeg_quality)))
        self.logger = logger

        self._sock = None  # type: Optional[socket.socket]
        self._recv_buffer = b""

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout_sec)
            sock.settimeout(self.request_timeout_sec)
        except OSError as exc:
            raise RemoteYoloError(
                "Detector service connection failed ({0}:{1}): {2}".format(self.host, self.port, exc)
            )
        self._sock = sock
        self._recv_buffer = b""
        if self.logger:
            self.logger.info("Connected to detector service at %s:%s", self.host, self.port)

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        self._recv_buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def ping(self) -> Dict[str, Any]:
        return self._send_request({"op": "PING"})

    def session_start(self, session_id: str) -> Dict[str, Any]:
        return self._send_request({"op": "SESSION_START", "session_id": str(session_id)})

    def session_end(self, session_id: str) -> Dict[str, Any]:
        return self._send_request({"op": "SESSION_END", "session_id": str(session_id)})

    def infer(self, frame, frame_idx: int, frame_ts: str) -> List[Dict[str, Any]]:
        if frame is None:
            return []

        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)],
        )
        if not ok:
            raise RemoteYoloError("JPEG encoding failed for frame {0}".format(int(frame_idx)))

        payload = {
            "op": "INFER",
            "frame_idx": int(frame_idx),
            "frame_ts": str(frame_ts),
            "image_jpeg_b64": base64.b64encode(encoded.tobytes()).decode("ascii"),
        }
        response = self._send_request(payload)
        detections = response.get("detections", [])
        if not isinstance(detections, list):
            raise RemoteYoloError("Detector service returned invalid detections payload.")
        return detections

    def _send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._sock is None:
            self.connect()
        if self._sock is None:
            raise RemoteYoloError("Detector service socket is unavailable.")

        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self._sock.sendall(raw)
            line = self._readline()
            response = json.loads(line)
            if not isinstance(response, dict):
                raise RemoteYoloError("Detector response is not a JSON object.")
            if not bool(response.get("ok", False)):
                raise RemoteYoloError(str(response.get("error", "Detector service error")))
            return response
        except socket.timeout as exc:
            self.close()
            raise RemoteYoloTimeout("Detector service request timed out: {0}".format(exc))
        except OSError as exc:
            self.close()
            raise RemoteYoloError("Detector service request failed: {0}".format(exc))
        except ValueError as exc:
            self.close()
            raise RemoteYoloError("Detector service request failed: {0}".format(exc))

    def _readline(self) -> str:
        if self._sock is None:
            raise RemoteYoloError("Detector service socket is unavailable.")

        while True:
            idx = self._recv_buffer.find(b"\n")
            if idx >= 0:
                line = self._recv_buffer[:idx]
                self._recv_buffer = self._recv_buffer[idx + 1 :]
                return line.decode("utf-8", errors="strict")

            chunk = self._sock.recv(4096)
            if not chunk:
                raise RemoteYoloError("Detector service closed the connection.")
            self._recv_buffer += chunk
