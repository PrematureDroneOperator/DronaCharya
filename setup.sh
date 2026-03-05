#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found"
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate

if python - <<'PY'
import sys
raise SystemExit(0 if sys.version_info[:2] <= (3, 6) else 1)
PY
then
  python -m pip install --upgrade "pip<22" "setuptools<60" "wheel<0.38"
else
  python -m pip install --upgrade pip setuptools wheel
fi
python -m pip install -r requirements.txt

python - <<'PY'
import importlib

missing = []
for module in ("cv2", "numpy"):
    try:
        importlib.import_module(module)
    except Exception:
        missing.append(module)

if missing:
    print("WARNING: Missing runtime module(s): {}".format(", ".join(missing)))
    print("On Jetson Nano, install system packages, for example:")
    print("  sudo apt-get update")
    print("  sudo apt-get install -y python3-opencv python3-numpy")
PY

mkdir -p data/maps data/detections data/routes data/logs data/recordings data/target_sessions

echo "Setup completed."
echo "Activate with: source .venv/bin/activate"
echo "Run with: python3 main.py"
