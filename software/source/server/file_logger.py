"""Persistent FRIDAY log file that overwrites on every app start.

Without this, console output can be lost when FRIDAY runs without a visible
console or when the in-panel LogBuffer rolls over. `setup_file_logging` opens
`~/.friday/logs/friday.log` (override with FRIDAY_LOG_DIR) in write mode, so a
fresh app start truncates the previous log.
"""

import logging
import os
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".friday" / "logs"
_LOGGER_NAME = "friday"
_log_file = None


def log_file_path() -> Path:
    env_dir = os.environ.get("FRIDAY_LOG_DIR")
    base = Path(env_dir) if env_dir else _DEFAULT_DIR
    return base / "friday.log"


def friday_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def setup_file_logging(debug: bool = False) -> Path:
    global _log_file
    if _log_file is not None:
        return _log_file
    path = log_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if debug:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    _log_file = path
    return path