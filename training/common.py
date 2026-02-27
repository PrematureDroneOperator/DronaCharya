from __future__ import annotations

import hashlib
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_files_recursive(root: Path, extensions: set[str]) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        [
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in extensions
        ]
    )


def list_images_recursive(root: Path) -> list[Path]:
    return list_files_recursive(root, IMAGE_EXTENSIONS)


def list_videos_recursive(root: Path) -> list[Path]:
    return list_files_recursive(root, VIDEO_EXTENSIONS)


def sha1_digest(payload: bytes) -> str:
    return hashlib.sha1(payload).hexdigest()


def stem_without_spaces(name: str) -> str:
    return "_".join(name.strip().split())
