"""Logging de las corridas: consola (si existe) + archivo con rotación en logs/radar.log."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from radar import config

LOGGER_NAME = "radar"
_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_logging(log_file: str | Path | None = None, level: int = logging.INFO,
                  max_bytes: int = 1_000_000, backup_count: int = 7) -> logging.Logger:
    """Configura (una sola vez por archivo) el logger 'radar' y lo devuelve.

    Con pythonw.exe no hay consola (sys.stderr es None): entonces solo se escribe al archivo.
    """
    log_file = Path(log_file) if log_file else config.LOG_FILE
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    target = str(log_file.resolve())
    for h in list(logger.handlers):
        if getattr(h, "_radar", False):
            if isinstance(h, RotatingFileHandler) and h.baseFilename == target:
                return logger
            logger.removeHandler(h)
            h.close()

    fmt = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    fh.setFormatter(fmt)
    fh._radar = True
    logger.addHandler(fh)

    if sys.stderr is not None:
        if hasattr(sys.stderr, "reconfigure"):
            try:
                sys.stderr.reconfigure(encoding="utf-8")  # tildes en la consola de Windows
            except (ValueError, OSError):
                pass
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(message)s"))
        ch._radar = True
        logger.addHandler(ch)
    return logger
