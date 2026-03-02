from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass
class CameraConfig:
    device_id: int = 0
    stream_url: str = ""          # RTSP / HTTP URL or GStreamer pipeline; overrides device_id when set
    fourcc: str = "XVID"          # VideoWriter codec — XVID recommended for Jetson L4T OpenCV
    container: str = ".avi"       # output file extension matching fourcc (.avi for XVID, .mp4 for mp4v)
    capture_count: int = 24
    capture_interval_sec: float = 0.25
    frame_width: int = 1280
    frame_height: int = 720


@dataclass
class MappingConfig:
    max_dimension: int = 1600
    meters_per_pixel: float = 0.05


@dataclass
class VisionConfig:
    model_path: str = "models/target_yolo.pt"
    conf_threshold: float = 0.35
    target_class_name: str = "target"
    image_size: int = 640


@dataclass
class MissionConfig:
    default_altitude_m: float = 15.0
    hover_time_sec: int = 5
    mavlink_connection: str = "udp:127.0.0.1:14550"
    mavlink_baudrate: int = 57600
    home_latitude: Optional[float] = None
    home_longitude: Optional[float] = None
    max_mission_duration_sec: int = 900


@dataclass
class TelemetryConfig:
    command_host: str = "0.0.0.0"
    command_port: int = 14560
    gcs_host: str = "127.0.0.1"
    gcs_port: int = 14561


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file_name: str = "dronacharya.log"


@dataclass
class AppPaths:
    base_dir: Path
    data_dir: Path
    maps_dir: Path
    detections_dir: Path
    routes_dir: Path
    logs_dir: Path


@dataclass
class AppConfig:
    paths: AppPaths
    camera: CameraConfig
    mapping: MappingConfig
    vision: VisionConfig
    mission: MissionConfig
    telemetry: TelemetryConfig
    logging: LoggingConfig


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_dict() -> Dict[str, Any]:
    return {
        "camera": CameraConfig().__dict__,
        "mapping": MappingConfig().__dict__,
        "vision": VisionConfig().__dict__,
        "mission": MissionConfig().__dict__,
        "telemetry": TelemetryConfig().__dict__,
        "logging": LoggingConfig().__dict__,
    }


def ensure_data_dirs(base_dir: Path) -> AppPaths:
    data_dir = base_dir / "data"
    maps_dir = data_dir / "maps"
    detections_dir = data_dir / "detections"
    routes_dir = data_dir / "routes"
    logs_dir = data_dir / "logs"

    for directory in (data_dir, maps_dir, detections_dir, routes_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return AppPaths(
        base_dir=base_dir,
        data_dir=data_dir,
        maps_dir=maps_dir,
        detections_dir=detections_dir,
        routes_dir=routes_dir,
        logs_dir=logs_dir,
    )


def load_config(config_path: Path, base_dir: Optional[Path] = None) -> AppConfig:
    resolved_config_path = config_path.resolve()
    root = base_dir.resolve() if base_dir else resolved_config_path.parents[1]

    raw = {}  # type: Dict[str, Any]
    if resolved_config_path.exists():
        with resolved_config_path.open("r", encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle) or {}
            if not isinstance(parsed, dict):
                raise ValueError("Config file must contain a YAML object.")
            raw = parsed

    merged = _merge(_default_dict(), raw)
    paths = ensure_data_dirs(root)

    return AppConfig(
        paths=paths,
        camera=CameraConfig(**merged["camera"]),
        mapping=MappingConfig(**merged["mapping"]),
        vision=VisionConfig(**merged["vision"]),
        mission=MissionConfig(**merged["mission"]),
        telemetry=TelemetryConfig(**merged["telemetry"]),
        logging=LoggingConfig(**merged["logging"]),
    )
