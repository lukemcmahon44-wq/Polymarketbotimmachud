"""Structured logging configuration using structlog."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(
    log_level: str = "INFO",
    log_file: str | None = "logs/bot.log",
    json_output: bool = False,
) -> None:
    """Configure structlog with structured output.

    In production/Docker: use JSON for log aggregation.
    In development: use pretty console output.
    """
    Path("logs").mkdir(exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
        handlers=handlers,
    )

    # Never log private keys or secrets — add a filter
    for handler in logging.root.handlers:
        handler.addFilter(_SecretFilter())

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        processors = shared_processors + [structlog.processors.JSONRenderer()]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class _SecretFilter(logging.Filter):
    """Scrub private keys and secrets from log records."""

    _FORBIDDEN_PATTERNS = [
        "private_key",
        "privatekey",
        "secret",
        "password",
        "mnemonic",
        "seed_phrase",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage()).lower()
        for pattern in self._FORBIDDEN_PATTERNS:
            if pattern in msg and "0x" in str(record.getMessage()):
                record.msg = "[REDACTED: potential secret in log]"
                record.args = ()
                return True
        return True
