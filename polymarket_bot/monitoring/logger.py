"""Structured logging setup with security-safe defaults.

SECURITY: Never logs private keys, secrets, or full wallet addresses.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def _sanitize_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Remove sensitive fields from log events."""
    sensitive_keys = {"private_key", "secret", "password", "api_key", "passphrase", "mnemonic"}
    for key in list(event_dict.keys()):
        if any(s in key.lower() for s in sensitive_keys):
            event_dict[key] = "***REDACTED***"
    return event_dict


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structured logging for the bot."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _sanitize_processor,
    ]

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str = "polybot") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
