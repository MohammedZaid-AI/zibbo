"""Structured logging bootstrap.

Every log line — ours, uvicorn's, and any third-party library's — is funnelled
through a single structlog ``ProcessorFormatter`` so that the output is uniform
and machine-parseable in production. Request-scoped fields (request id, method,
path) are bound to context variables by the request-context middleware and are
therefore attached automatically to *all* records emitted while handling a
request, including logs from code that knows nothing about the request.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

from gateway.config import LogFormat

if TYPE_CHECKING:
    from structlog.typing import Processor

    from gateway.config import Settings

# uvicorn emits its own access log; ours carries the request id and duration,
# so the built-in one is pure duplication.
_SILENCED_LOGGERS = ("uvicorn.access",)
_PROPAGATE_LOGGERS = ("uvicorn", "uvicorn.error", "httpx", "httpcore")


def _shared_processors() -> list[Processor]:
    """Processors applied to structlog and stdlib records alike."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def _renderer(log_format: LogFormat) -> Processor:
    if log_format is LogFormat.JSON:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())


def configure_logging(settings: Settings) -> None:
    """Install the logging pipeline. Idempotent — safe to call per test."""
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _renderer(settings.log_format),
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.value)

    for name in _PROPAGATE_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    for name in _SILENCED_LOGGERS:
        logging.getLogger(name).disabled = True


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Prefer module-level ``get_logger(__name__)``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_values:
        return logger.bind(**initial_values)
    return logger
