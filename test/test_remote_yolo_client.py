import json
import socket
import threading
import unittest

import numpy as np

from vision.remote_yolo_client import RemoteYoloClient, RemoteYoloError


class _StubDetectorServer(object):
    def __init__(self, send_malformed_first: bool = False) -> None:
        self.send_malformed_first = bool(send_malformed_first)
        self._sent_malformed = False
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.host = "127.0.0.1"
        self.port = 0
        self._listen_socket = None

    def start(self) -> None:
        self._thread.start()
        if not self._ready_event.wait(timeout=3.0):
            raise RuntimeError("stub detector server did not start")

    def stop(self) -> None:
        self._stop_event.set()
        if self.port > 0:
            try:
                wake = socket.create_connection((self.host, self.port), timeout=0.2)
                wake.close()
            except Exception:
                pass
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket = listen
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind((self.host, 0))
        self.port = int(listen.getsockname()[1])
        listen.listen(1)
        listen.settimeout(0.5)
        self._ready_event.set()

        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = listen.accept()
                except socket.timeout:
                    continue
                conn.settimeout(0.5)
                self._handle_conn(conn)
                try:
                    conn.close()
                except OSError:
                    pass
        finally:
            listen.close()

    def _handle_conn(self, conn: socket.socket) -> None:
        buffer = b""
        while not self._stop_event.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk
            while True:
                nl = buffer.find(b"\n")
                if nl < 0:
                    break
                line = buffer[:nl]
                buffer = buffer[nl + 1 :]
                if not line:
                    continue

                if self.send_malformed_first and not self._sent_malformed:
                    self._sent_malformed = True
                    conn.sendall(b"not-json\n")
                    continue

                request = json.loads(line.decode("utf-8"))
                op = str(request.get("op", "")).upper()

                if op == "PING":
                    response = {"ok": True, "op": "PING", "status": "ready"}
                elif op == "SESSION_START":
                    response = {"ok": True, "op": "SESSION_START", "session_id": request.get("session_id", "")}
                elif op == "SESSION_END":
                    response = {"ok": True, "op": "SESSION_END", "session_id": request.get("session_id", "")}
                elif op == "INFER":
                    response = {
                        "ok": True,
                        "op": "INFER",
                        "frame_idx": int(request.get("frame_idx", 0)),
                        "detections": [
                            {
                                "class_name": "target",
                                "confidence": 0.91,
                                "pixel_x": 100.0,
                                "pixel_y": 120.0,
                                "bbox_xyxy": [80.0, 90.0, 120.0, 150.0],
                            }
                        ],
                    }
                else:
                    response = {"ok": False, "error": "unsupported_op"}

                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))


class RemoteYoloClientTest(unittest.TestCase):
    def test_roundtrip_ping_infer_session(self) -> None:
        server = _StubDetectorServer(send_malformed_first=False)
        server.start()
        try:
            client = RemoteYoloClient(
                host="127.0.0.1",
                port=server.port,
                request_timeout_sec=1.0,
                connect_timeout_sec=1.0,
                jpeg_quality=80,
            )
            ping = client.ping()
            self.assertTrue(bool(ping.get("ok")))
            self.assertEqual("PING", ping.get("op"))

            start = client.session_start("session-0001")
            self.assertTrue(bool(start.get("ok")))
            self.assertEqual("SESSION_START", start.get("op"))

            frame = np.zeros((32, 32, 3), dtype=np.uint8)
            detections = client.infer(frame=frame, frame_idx=7, frame_ts="2026-01-01T00:00:00Z")
            self.assertEqual(1, len(detections))
            self.assertEqual("target", detections[0].get("class_name"))

            end = client.session_end("session-0001")
            self.assertTrue(bool(end.get("ok")))
            client.close()
        finally:
            server.stop()

    def test_malformed_response_raises(self) -> None:
        server = _StubDetectorServer(send_malformed_first=True)
        server.start()
        try:
            client = RemoteYoloClient(
                host="127.0.0.1",
                port=server.port,
                request_timeout_sec=1.0,
                connect_timeout_sec=1.0,
                jpeg_quality=80,
            )
            with self.assertRaises(RemoteYoloError):
                client.ping()
            client.close()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
