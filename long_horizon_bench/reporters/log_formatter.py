"""Structured, coloured log formatting for Long-Horizon-Bench."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path


class StructuredFormatter(logging.Formatter):
    """Formatter that writes JSON-Lines structured logs."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


class ColoredConsoleFormatter(logging.Formatter):
    """Console formatter with ANSI colours per log level."""

    _COLORS = {
        logging.DEBUG: "\033[90m",      # grey
        logging.INFO: "\033[36m",       # cyan
        logging.WARNING: "\033[33m",    # yellow
        logging.ERROR: "\033[31m",      # red
        logging.CRITICAL: "\033[1;31m", # bold red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLORS.get(record.levelno, "")
        ts = datetime.now().strftime("%H:%M:%S")
        level = record.levelname.ljust(8)
        msg = record.getMessage()
        return f"{colour}{ts} | {level} | {msg}{self._RESET}"


def setup_file_logging(
    log_path: Path,
    log_level: int = logging.DEBUG,
    structured: bool = True,
    append: bool = False,
) -> logging.FileHandler:
    """Create and return a file handler with optional structured formatting.

    Args:
        log_path: Path to the log file.
        log_level: Minimum level for the handler.
        structured: If True use JSON-Lines, otherwise plain text.
        append: If True open in append mode (for resume).

    Returns:
        The configured ``FileHandler``.
    """
    handler = logging.FileHandler(log_path, mode="a" if append else "w")
    handler.setLevel(log_level)

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    return handler


def setup_console_logging(
    log_level: int = logging.INFO,
) -> logging.StreamHandler:
    """Create and return a coloured console handler.

    Args:
        log_level: Minimum level for the handler.

    Returns:
        The configured ``StreamHandler``.
    """
    handler = logging.StreamHandler()
    handler.setLevel(log_level)
    handler.setFormatter(ColoredConsoleFormatter())
    return handler
