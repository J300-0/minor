"""
core/logger.py — Rotating + latest-run logging.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from core.config import LOG_DIR

# Silence noisy third-party loggers
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logging.getLogger("pdfplumber").setLevel(logging.WARNING)

_LOGGER_NAME = "paper_formatter"
_log = None


def get_logger() -> logging.Logger:
    global _log
    if _log is not None:
        return _log

    _log = logging.getLogger(_LOGGER_NAME)
    _log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Rotating file handler
    rotating = RotatingFileHandler(
        os.path.join(LOG_DIR, "pipeline.log"),
        maxBytes=2 * 1024 * 1024,  # 2 MB
        backupCount=3,
    )
    rotating.setLevel(logging.DEBUG)
    rotating.setFormatter(fmt)
    _log.addHandler(rotating)

    # Latest-run file handler (overwritten each run)
    latest = logging.FileHandler(
        os.path.join(LOG_DIR, "pipeline_latest.log"), mode="w"
    )
    latest.setLevel(logging.DEBUG)
    latest.setFormatter(fmt)
    _log.addHandler(latest)

    # Console handler (INFO+)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    _log.addHandler(console)

    return _log
