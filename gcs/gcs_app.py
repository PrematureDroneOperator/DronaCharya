"""gcs_app.py — Ground Control Station UI for dronAcharya.

Main GCS window:  telemetry link, mission commands, telemetry feed log.
LiveMapWindow:     pops up automatically when TELEMETRY GPS packets arrive
                   after START_MISSION is clicked. Shows an OpenStreetMap tile
                   as background, plots drone GPS points live, and connects them.
                   Stays open until the user closes it with the X button.
"""

import io
import json
import math
import queue
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import List, Optional, Tuple
from datetime import datetime, timezone

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Optional dependencies for the live map (gracefully degraded if missing)
try:
    import requests
    from PIL import Image, ImageTk
    _MAP_AVAILABLE = True
except ImportError:
    _MAP_AVAILABLE = False


# ---------------------------------------------------------------------------
# OSM tile helpers
# ---------------------------------------------------------------------------

def _deg2tile(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    """Convert GPS to OSM tile (x, y) at given zoom level."""
    lat_r = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile2deg(tile_x: int, tile_y: int, zoom: int) -> Tuple[float, float]:
    """Top-left corner of an OSM tile -> (lat, lon)."""
    n = 2 ** zoom
    lon = tile_x / n * 360.0 - 180.0
    lat_r = math.atan(math.sinh(math.pi * (1.0 - 2.0 * tile_y / n)))
    lat = math.degrees(lat_r)
    return lat, lon


def _fetch_osm_tile(tile_x: int, tile_y: int, zoom: int, timeout: float = 8.0) -> Optional[bytes]:
    """Download a single 256×256 OSM tile, returning raw PNG bytes or None."""
    url = f"https://tile.openstreetmap.org/{zoom}/{tile_x}/{tile_y}.png"
    headers = {"User-Agent": "dronAcharya-GCS/1.0 (+https://github.com/PrematureDroneOperator/DronaCharya)"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            return response.content
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# LiveMapWindow
# ---------------------------------------------------------------------------

class LiveMapWindow:
    """Toplevel Tkinter window that shows a satellite/OSM tile with live GPS dots.

    Usage:
        win = LiveMapWindow(parent_root)
        win.add_point(lat, lon)   # called from the GCS poll loop (main thread only)
    """

    TILE_PX = 256         # OSM tile size
    CANVAS_PX = 768       # canvas width and height (3×3 tiles)
    ZOOM = 17             # OSM zoom level (street-level detail)
    DOT_R = 5             # dot radius in pixels
    LINE_COLOR = "#00aaff"
    DOT_COLOR = "#ff4400"
    DOT_CURRENT_COLOR = "#00ff88"

    def __init__(self, parent: tk.Tk) -> None:
        self._win = tk.Toplevel(parent)
        self._win.title("Live Drone Map")
        self._win.resizable(False, False)
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._canvas = tk.Canvas(self._win, width=self.CANVAS_PX, height=self.CANVAS_PX, bg="#1a1a2e")
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._status_var = tk.StringVar(value="Waiting for GPS…")
        ttk.Label(self._win, textvariable=self._status_var, foreground="#888").pack(pady=2)

        # Map state
        self._tile_img_ref = None          # keep PIL reference alive
        self._tile_x0: Optional[int] = None  # top-left tile of the 3×3 grid
        self._tile_y0: Optional[int] = None
        self._map_origin_lat: Optional[float] = None   # lat of top-left pixel
        self._map_origin_lon: Optional[float] = None
        self._pixels_per_lon_deg: float = 0.0
        self._pixels_per_lat_deg: float = 0.0

        self._points: List[Tuple[float, float]] = []   # (lat, lon) history
        self._canvas_points: List[Tuple[int, int]] = []  # pixel coords

        self._map_loaded = False
        self._closed = False
        self._tile_fail_label: Optional[int] = None

        self._draw_placeholder()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        return not self._closed

    def add_point(self, lat: float, lon: float) -> None:
        """Add a new GPS point (call from Tk main thread)."""
        if self._closed:
            return

        self._points.append((lat, lon))

        if not self._map_loaded and len(self._points) == 1:
            # First point — try to load the background tile asynchronously
            threading.Thread(target=self._load_map_async, args=(lat, lon), daemon=True).start()

        if self._map_loaded:
            px = self._gps_to_canvas(lat, lon)
            if px:
                self._draw_new_point(px)
        else:
            # Fallback: draw on the plain canvas even without the tile
            px = self._gps_to_canvas_fallback(lat, lon, len(self._points))
            if px:
                self._draw_new_point(px)

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._status_var.set(f"Latest: {lat:.6f}, {lon:.6f}  ({len(self._points)} pts)  [{ts} UTC]")

    # ------------------------------------------------------------------
    # Map loading
    # ------------------------------------------------------------------

    def _draw_placeholder(self) -> None:
        self._canvas.create_rectangle(0, 0, self.CANVAS_PX, self.CANVAS_PX, fill="#1a1a2e", outline="")
        self._canvas.create_text(
            self.CANVAS_PX // 2, self.CANVAS_PX // 2,
            text="Waiting for first GPS point\nto load satellite map…",
            fill="#aaaaaa", font=("Arial", 14), justify=tk.CENTER
        )

    def _load_map_async(self, center_lat: float, center_lon: float) -> None:
        """Download a 3×3 grid of OSM tiles around the first GPS point and stitch them."""
        if not _MAP_AVAILABLE:
            self._win.after(0, self._show_tile_unavailable)
            return

        zoom = self.ZOOM
        cx, cy = _deg2tile(center_lat, center_lon, zoom)
        # Top-left of 3×3 grid
        x0, y0 = cx - 1, cy - 1
        grid_px = self.TILE_PX * 3   # 768 px

        combined = Image.new("RGB", (grid_px, grid_px), (30, 30, 50))
        all_ok = False
        for dx in range(3):
            for dy in range(3):
                raw = _fetch_osm_tile(x0 + dx, y0 + dy, zoom)
                if raw:
                    tile_img = Image.open(io.BytesIO(raw)).convert("RGB")
                    combined.paste(tile_img, (dx * self.TILE_PX, dy * self.TILE_PX))
                    all_ok = True

        # Resize to canvas size (may already match)
        if grid_px != self.CANVAS_PX:
            combined = combined.resize((self.CANVAS_PX, self.CANVAS_PX), Image.LANCZOS)

        # Compute geographic extent of the combined image
        lat_tl, lon_tl = _tile2deg(x0, y0, zoom)
        lat_br, lon_br = _tile2deg(x0 + 3, y0 + 3, zoom)

        self._win.after(0, self._apply_map, combined, lat_tl, lon_tl, lat_br, lon_br, all_ok)

    def _apply_map(self, img: "Image.Image", lat_tl: float, lon_tl: float,
                   lat_br: float, lon_br: float, partial: bool) -> None:
        if self._closed:
            return
        self._tile_img_ref = ImageTk.PhotoImage(img)
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._tile_img_ref)

        # Geographic → pixel calibration
        self._map_origin_lat = lat_tl
        self._map_origin_lon = lon_tl
        self._pixels_per_lat_deg = self.CANVAS_PX / (lat_tl - lat_br)   # lat decreases downward
        self._pixels_per_lon_deg = self.CANVAS_PX / (lon_br - lon_tl)

        self._map_loaded = True

        if not partial:
            self._canvas.create_text(
                self.CANVAS_PX // 2, 16, text="⚠ Some tiles unavailable (offline?)",
                fill="#ffaa00", font=("Arial", 9)
            )

        # Re-draw all accumulated points now that the map is ready
        prev = None
        for lat, lon in self._points:
            px = self._gps_to_canvas(lat, lon)
            if px:
                self._draw_new_point(px, prev)
                prev = px

    def _show_tile_unavailable(self) -> None:
        if self._closed:
            return
        if self._tile_fail_label is None:
            self._tile_fail_label = self._canvas.create_text(
                self.CANVAS_PX // 2, self.CANVAS_PX // 2,
                text="Satellite tiles unavailable\n(install 'requests' and 'Pillow')\nDisplaying GPS points on plain background",
                fill="#ffaa00", font=("Arial", 12), justify=tk.CENTER
            )

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _gps_to_canvas(self, lat: float, lon: float) -> Optional[Tuple[int, int]]:
        """Convert GPS to canvas pixel. Returns None if map not loaded or out of bounds."""
        if not self._map_loaded:
            return None
        px = int((lon - self._map_origin_lon) * self._pixels_per_lon_deg)
        py = int((self._map_origin_lat - lat) * self._pixels_per_lat_deg)
        return (px, py)

    def _gps_to_canvas_fallback(self, lat: float, lon: float, count: int) -> Tuple[int, int]:
        """Simple fallback when tile unavailable: spread points over known range."""
        if len(self._points) == 1:
            return (self.CANVAS_PX // 2, self.CANVAS_PX // 2)
        lats = [p[0] for p in self._points]
        lons = [p[1] for p in self._points]
        lat_range = max(lats) - min(lats) or 1e-6
        lon_range = max(lons) - min(lons) or 1e-6
        m = 60
        px = int(m + (lon - min(lons)) / lon_range * (self.CANVAS_PX - 2 * m))
        py = int(m + (max(lats) - lat) / lat_range * (self.CANVAS_PX - 2 * m))
        return (px, py)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw_new_point(self, px: Tuple[int, int], prev: Optional[Tuple[int, int]] = None) -> None:
        canvas_pts = self._canvas_points

        # Erase the "current" dot from the previous last point
        if canvas_pts:
            last = canvas_pts[-1]
            r = self.DOT_R - 1
            self._canvas.create_oval(last[0] - r, last[1] - r, last[0] + r, last[1] + r,
                                     fill=self.DOT_COLOR, outline="")

        # Draw line to previous point
        if canvas_pts:
            p0 = canvas_pts[-1]
            self._canvas.create_line(p0[0], p0[1], px[0], px[1],
                                     fill=self.LINE_COLOR, width=2, smooth=True)

        # Draw the new point as the "current" dot (larger, different colour)
        r = self.DOT_R
        self._canvas.create_oval(px[0] - r, px[1] - r, px[0] + r, px[1] + r,
                                 fill=self.DOT_CURRENT_COLOR, outline="white", width=1)

        canvas_pts.append(px)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self._closed = True
        self._win.destroy()


# ---------------------------------------------------------------------------
# Main GCS Application
# ---------------------------------------------------------------------------

class GCSApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("dronAcharya GCS")
        self.root.geometry("900x620")

        self.drone_host = tk.StringVar(value="127.0.0.1")
        self.command_port = tk.IntVar(value=14560)
        self.listen_port = tk.IntVar(value=14561)
        self.connection_state = tk.StringVar(value="DISCONNECTED")

        self._listener_thread = None
        self._stop_event = threading.Event()
        self._inbox = queue.Queue()  # type: queue.Queue
        self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Live map state
        self._live_map: Optional[LiveMapWindow] = None
        self._mission_active = False   # True after START_MISSION, False after X is closed

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

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

        # Track mission active state for the live map
        if command == "START_MISSION":
            self._mission_active = True
        elif command in ("ABORT", "STOP_SURVEY"):
            self._mission_active = False

    # ------------------------------------------------------------------
    # Listener loop
    # ------------------------------------------------------------------

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
                self._inbox.put((decoded, addr))
        except OSError as exc:
            self._inbox.put(("[ERROR] Telemetry listener error: {0}".format(exc), None))
            self.connection_state.set("DISCONNECTED")
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Packet formatting
    # ------------------------------------------------------------------

    def _format_packet(self, payload: str) -> Tuple[str, Optional[dict]]:
        """Return (display_string, telemetry_dict_or_None)."""
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload, None

        packet_type = parsed.get("type", "UNKNOWN")
        packet_payload = parsed.get("payload", {})

        if packet_type == "LOG":
            msg = packet_payload.get("message", "")
            return "LOG: {0}".format(msg), None

        if packet_type == "TELEMETRY":
            lat = packet_payload.get("latitude")
            lon = packet_payload.get("longitude")
            alt = packet_payload.get("altitude_m")
            display = "TELEMETRY: lat={lat:.6f} lon={lon:.6f} alt={alt:.1f}m".format(
                lat=float(lat) if lat is not None else 0.0,
                lon=float(lon) if lon is not None else 0.0,
                alt=float(alt) if alt is not None else 0.0,
            )
            return display, packet_payload

        return "{0}: {1}".format(packet_type, packet_payload), None

    # ------------------------------------------------------------------
    # Poll inbox (runs in the Tk main thread via after())
    # ------------------------------------------------------------------

    def _poll_inbox(self) -> None:
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, tuple) and len(item) == 2:
                raw, addr = item
                if addr is None:
                    # Error message string placed directly
                    self._append_log(raw)
                else:
                    display, telemetry = self._format_packet(raw)
                    self._append_log("[RX:{0}:{1}] {2}".format(addr[0], addr[1], display))
                    if telemetry is not None and self._mission_active:
                        self._handle_telemetry(telemetry)
            else:
                self._append_log(str(item))

        self.root.after(300, self._poll_inbox)

    def _handle_telemetry(self, payload: dict) -> None:
        """Route incoming TELEMETRY GPS payload to the live map window (main thread)."""
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if lat is None or lon is None:
            return

        lat = float(lat)
        lon = float(lon)

        # Create the window on first point
        if self._live_map is None or not self._live_map.is_open():
            self._live_map = LiveMapWindow(self.root)

        self._live_map.add_point(lat, lon)

    # ------------------------------------------------------------------
    # Log widget
    # ------------------------------------------------------------------

    def _append_log(self, message: str) -> None:
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self._stop_event.set()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=1.5)
        self._send_socket.close()
        if self._live_map and self._live_map.is_open():
            try:
                self._live_map._win.destroy()
            except Exception:
                pass
        self.root.quit()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._poll_inbox()
        self.root.mainloop()


def main() -> int:
    app = GCSApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
