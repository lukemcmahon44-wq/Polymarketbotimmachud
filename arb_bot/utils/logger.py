"""Structured JSON logging to file + console via Python's standard logging."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LOG_DIR = Path("logs")
_FILE_HANDLER: Optional[logging.FileHandler] = None


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        log_obj = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def _setup_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # Already configured

    root.setLevel(logging.DEBUG)

    # Console handler — human-readable
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(console)

    # File handler — structured JSON
    _LOG_DIR.mkdir(exist_ok=True)
    log_file = _LOG_DIR / f"arb_bot_{datetime.now().strftime('%Y%m%d')}.jsonl"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)
    global _FILE_HANDLER
    _FILE_HANDLER = file_handler


_setup_root_logger()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
