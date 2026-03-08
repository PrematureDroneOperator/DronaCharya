import json
import queue
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class GCSApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("dronAcharya GCS")
        self.root.geometry("900x580")

        self.drone_host = tk.StringVar(value="127.0.0.1")
        self.command_port = tk.IntVar(value=14560)
        self.listen_port = tk.IntVar(value=14561)
        self.connection_state = tk.StringVar(value="DISCONNECTED")

        self._listener_thread = None
        self._stop_event = threading.Event()
        self._inbox = queue.Queue()  # type: queue.Queue
        self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(frame, text="Telemetry Link", padding=10)
        conn.pack(fill=tk.X, pady=4)
        ttk.Label(conn, text="Bridge Host").grid(row=0, column=0, padx=4, pady=2, sticky=tk.W)
        ttk.Entry(conn, textvariable=self.drone_host, width=18).grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(conn, text="Command Port").grid(row=0, column=2, padx=4, pady=2, sticky=tk.W)
        ttk.Entry(conn, textvariable=self.command_port, width=10).grid(row=0, column=3, padx=4, pady=2)
        ttk.Label(conn, text="Listen Port").grid(row=0, column=4, padx=4, pady=2, sticky=tk.W)
        ttk.Entry(conn, textvariable=self.listen_port, width=10).grid(row=0, column=5, padx=4, pady=2)
        ttk.Button(conn, text="Connect", command=self._connect).grid(row=0, column=6, padx=8, pady=2)
        ttk.Label(conn, text="Status:").grid(row=0, column=7, padx=4, pady=2, sticky=tk.W)
        ttk.Label(conn, textvariable=self.connection_state).grid(row=0, column=8, padx=4, pady=2, sticky=tk.W)

        controls = ttk.LabelFrame(frame, text="Mission Commands", padding=10)
        controls.pack(fill=tk.X, pady=4)
        buttons = [
            ("Start Survey", "START_SURVEY"),
            ("Stop Survey", "STOP_SURVEY"),
            ("Build Route", "BUILD_ROUTE"),
            ("Build Mission", "BUILD_MISSION"),
            ("Start Mission", "START_MISSION"),
            ("Abort", "ABORT"),
            ("Status Request", "STATUS_REQUEST"),
        ]
        for idx, (label, cmd) in enumerate(buttons):
            ttk.Button(controls, text=label, command=lambda c=cmd: self._send_command(c)).grid(
                row=0, column=idx, padx=4, pady=4
            )

        rec_frame = ttk.LabelFrame(
            frame,
            text="Survey Session (record + YOLO + GPS geotagging on Jetson Nano)",
            padding=10,
        )
        rec_frame.pack(fill=tk.X, pady=4)
        ttk.Label(
            rec_frame,
            text=(
                "Sends START_SURVEY / STOP_SURVEY over telemetry.\n"
                "Requires detector service on Jetson (vision/detector_service.py) before START_SURVEY.\n"
                "Raw detections, unique targets, and TSP graphs are stored in data/target_sessions/session-XXXX/.\n"
                "Camera source is configured on the Jetson in config/config.yaml -> camera.stream_url."
            ),
            foreground="#555",
            font=("TkDefaultFont", 8, "italic"),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, padx=4, pady=(2, 6), sticky=tk.W)

        ttk.Button(rec_frame, text="Start Survey on Drone", command=lambda: self._send_command("START_SURVEY")).grid(
            row=1, column=0, padx=6, pady=4
        )
        ttk.Button(rec_frame, text="Stop Survey on Drone", command=lambda: self._send_command("STOP_SURVEY")).grid(
            row=1, column=1, padx=6, pady=4
        )
        ttk.Label(
            rec_frame,
            text="After stop, route + graphs are auto-built. Use Build Route to rebuild manually.",
            foreground="#888",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=1, column=2, columnspan=2, padx=12, pady=4, sticky=tk.W)

        rec_only_frame = ttk.LabelFrame(
            frame,
            text="Video Only Recording (legacy mode, no detection/route)",
            padding=10,
        )
        rec_only_frame.pack(fill=tk.X, pady=4)
        ttk.Label(
            rec_only_frame,
            text=(
                "Sends START_RECORDING / STOP_RECORDING.\n"
                "This keeps the old behavior: only video + extracted frames in data/recordings/session-XXXX/."
            ),
            foreground="#555",
            font=("TkDefaultFont", 8, "italic"),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, padx=4, pady=(2, 6), sticky=tk.W)
        ttk.Button(
            rec_only_frame,
            text="Start Recording Only",
            command=lambda: self._send_command("START_RECORDING"),
        ).grid(row=1, column=0, padx=6, pady=4)
        ttk.Button(
            rec_only_frame,
            text="Stop Recording Only",
            command=lambda: self._send_command("STOP_RECORDING"),
        ).grid(row=1, column=1, padx=6, pady=4)
        
        gps_test_frame = ttk.LabelFrame(
            frame,
            text="GPS Logger Test",
            padding=10,
        )
        gps_test_frame.pack(fill=tk.X, pady=4)
        ttk.Label(
            gps_test_frame,
            text=(
                "Sends START_GPS_TEST / STOP_GPS_TEST.\n"
                "Logs continuous simulated GPS data to logs/gps_session_XXXX/."
            ),
            foreground="#555",
            font=("TkDefaultFont", 8, "italic"),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, padx=4, pady=(2, 6), sticky=tk.W)
        ttk.Button(
            gps_test_frame,
            text="Start GPS Logger Test",
            command=lambda: self._send_command("START_GPS_TEST"),
        ).grid(row=1, column=0, padx=6, pady=4)
        ttk.Button(
            gps_test_frame,
            text="Stop GPS Logger Test",
            command=lambda: self._send_command("STOP_GPS_TEST"),
        ).grid(row=1, column=1, padx=6, pady=4)

        log_box = ttk.LabelFrame(frame, text="Telemetry Feed", padding=10)
        log_box.pack(fill=tk.BOTH, expand=True, pady=4)
        self.log_widget = tk.Text(log_box, wrap=tk.WORD, state=tk.DISABLED)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

    def _connect(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            self.connection_state.set("CONNECTED")
            self._send_command("STATUS_REQUEST")
            return

        self._stop_event.clear()
        self._listener_thread = threading.Thread(target=self._listener_loop, name="GCSListener", daemon=True)
        self._listener_thread.start()
        self.connection_state.set("CONNECTED")
        self._send_command("STATUS_REQUEST")

    def _send_command(self, command: str) -> None:
        host = self.drone_host.get().strip()
        port = int(self.command_port.get())
        if host not in ("127.0.0.1", "localhost"):
            self._append_log("[WARN] Bridge Host is not localhost. For local radio_bridge mode use 127.0.0.1.")
        try:
            self._send_socket.sendto(command.encode("utf-8"), (host, port))
            self._append_log("[TX:{0}:{1}] {2}".format(host, port, command))
        except OSError as exc:
            self._append_log("[ERROR] Failed to send {0}: {1}".format(command, exc))

    def _listener_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", int(self.listen_port.get())))
            sock.settimeout(1.0)
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                decoded = data.decode("utf-8", errors="ignore")
                self._inbox.put("[RX:{0}:{1}] {2}".format(addr[0], addr[1], self._format_packet(decoded)))
        except OSError as exc:
            self._inbox.put("[ERROR] Telemetry listener error: {0}".format(exc))
            self.connection_state.set("DISCONNECTED")
        finally:
            sock.close()

    def _format_packet(self, payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload
            
        packet_type = parsed.get("type", "UNKNOWN")
        packet_payload = parsed.get("payload", {})
        
        if packet_type == "LOG":
            msg = packet_payload.get("message", "")
            return f"LOG: {msg}"
            
        return "{0}: {1}".format(packet_type, packet_payload)

    def _append_log(self, message: str) -> None:
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    def _poll_inbox(self) -> None:
        while True:
            try:
                message = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._append_log(message)
        self.root.after(300, self._poll_inbox)

    def _on_close(self) -> None:
        self._stop_event.set()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=1.5)
        self._send_socket.close()
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        self._poll_inbox()
        self.root.mainloop()


def main() -> int:
    app = GCSApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
