"""File logging with a flat key=value event format. Never logs headers/cookies/tokens."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "crawler"


def setup_logging(log_file: Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event: str, *, level: int = logging.INFO, url: str | None = None,
              status: int | None = None, error: str | None = None, **extra: object) -> None:
    """Log `event=... url=... status=... error=...` on one line."""
    parts = [f"event={event}"]
    if url:
        parts.append(f"url={url}")
    if status is not None:
        parts.append(f"status={status}")
    if error:
        parts.append(f"error={str(error)[:300]!r}")
    parts.extend(f"{k}={v}" for k, v in extra.items())
    get_logger().log(level, " ".join(parts))
