from __future__ import annotations

import argparse
from pathlib import Path

from core.controller import DroneAcharyaController
from core.mode_selector import select_mode
from ui.cli_interface import CLIInterface
from ui.gui_app import DroneGUI
from utils.config import load_config
from utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="dronAcharya autonomous drone software")
    parser.add_argument("--mode", choices=["gui", "cli"], help="Startup mode override")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to YAML config file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / config_path

    config = load_config(config_path=config_path, base_dir=base_dir)
    logger, log_handler = setup_logger(
        name="dronAcharya",
        log_file=config.paths.logs_dir / config.logging.file_name,
        level=config.logging.level,
    )

    mode = args.mode or select_mode()
    controller = DroneAcharyaController(config=config, logger=logger, log_handler=log_handler)
    controller.start(mode=mode)

    try:
        if mode == "gui":
            app = DroneGUI(controller)
            app.run()
        else:
            cli = CLIInterface(controller)
            cli.run()
    finally:
        controller.stop()

    return 0
