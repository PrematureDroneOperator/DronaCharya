#!/usr/bin/env python3
"""gcs/jetson_stream_server.py
===================================================
Run this on the **Jetson Nano** (onboard the drone).

It creates a GStreamer RTSP server on port 8554.
The GCS laptop then sets stream_url in config.yaml to:

    rtsp://<jetson-ip>:8554/drone

Requirements (Jetson Nano – Ubuntu 18/20):
    sudo apt install -y gstreamer1.0-tools gstreamer1.0-rtsp \
                        python3-gi gir1.2-gst-rtsp-server-1.0

Usage:
    python3 gcs/jetson_stream_server.py              # CSI camera (default)
    python3 gcs/jetson_stream_server.py --usb        # USB webcam /dev/video0
    python3 gcs/jetson_stream_server.py --usb --dev 2  # /dev/video2
    python3 gcs/jetson_stream_server.py --port 8554 --mount /drone
"""
import argparse
import logging
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GStreamer pipeline strings
# ---------------------------------------------------------------------------
# CSI camera via nvarguscamerasrc (Jetson-native, lowest latency)
_PIPELINE_CSI = (
    "( nvarguscamerasrc sensor-id=0 ! "
    "video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1 ! "
    "nvvidconv ! video/x-raw,format=I420 ! "
    "x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast ! "
    "rtph264pay name=pay0 pt=96 )"
)

# USB / V4L2 camera (fallback, no hardware encoder)
_PIPELINE_USB = (
    "( v4l2src device={device} ! "
    "video/x-raw,width=1280,height=720,framerate=30/1 ! "
    "videoconvert ! "
    "x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast ! "
    "rtph264pay name=pay0 pt=96 )"
)


def _build_pipeline(use_usb: bool, usb_device: str) -> str:
    if use_usb:
        return _PIPELINE_USB.format(device=usb_device)
    return _PIPELINE_CSI


def run_server(
    mount: str = "/drone",
    port: int = 8554,
    use_usb: bool = False,
    usb_device: str = "/dev/video0",
) -> None:
    """Start the RTSP server and block until Ctrl-C."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtspServer", "1.0")
        from gi.repository import GLib, Gst, GstRtspServer
    except (ImportError, ValueError) as exc:
        logger.error(
            "GStreamer Python bindings not found.\n"
            "Install with:\n"
            "  sudo apt install python3-gi gir1.2-gst-rtsp-server-1.0\n"
            "Error: %s",
            exc,
        )
        sys.exit(1)

    Gst.init(None)
    loop = GLib.MainLoop()

    server = GstRtspServer.RTSPServer.new()
    server.props.service = str(port)

    pipeline_str = _build_pipeline(use_usb, usb_device)
    factory = GstRtspServer.RTSPMediaFactory.new()
    factory.set_launch(pipeline_str)
    factory.set_shared(True)          # re-use pipeline for all clients

    mounts = server.get_mount_points()
    mounts.add_factory(mount, factory)
    server.attach(None)

    import socket as _socket
    hostname = _socket.gethostname()
    try:
        local_ip = _socket.gethostbyname(hostname)
    except Exception:
        local_ip = "<jetson-ip>"

    print(
        f"\n  RTSP server running.\n"
        f"  Stream URL  : rtsp://{local_ip}:{port}{mount}\n"
        f"  Pipeline    : {'USB (' + usb_device + ')' if use_usb else 'CSI (nvarguscamerasrc)'}\n"
        f"\n  Set this in GCS config/config.yaml:\n"
        f"    stream_url: \"rtsp://{local_ip}:{port}{mount}\"\n"
        f"\n  Press Ctrl-C to stop.\n"
    )

    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nShutting down RTSP server.")
        loop.quit()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jetson Nano RTSP camera server")
    p.add_argument("--port", type=int, default=8554, help="RTSP port (default 8554)")
    p.add_argument("--mount", default="/drone", help="RTSP mount point (default /drone)")
    p.add_argument("--usb", action="store_true", help="Use USB/V4L2 camera instead of CSI")
    p.add_argument("--dev", default="/dev/video0", help="USB device path (default /dev/video0)")
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args()
    run_server(mount=args.mount, port=args.port, use_usb=args.usb, usb_device=args.dev)
