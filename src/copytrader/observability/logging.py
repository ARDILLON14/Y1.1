"""Structured logging (structlog → JSON on stdout) with secret redaction.

Every record passes through the redactor as the last processor before
rendering, so a secret can only leak if it is *both* unknown to the redactor
and not matched by any pattern.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from copytrader.security.redaction import REDACTOR


def _redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return REDACTOR.data(event_dict)  # type: ignore[no-any-return]


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party libraries are noisy at INFO and may print URLs containing keys.
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "uvicorn.access", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind_trace(trace_id: str | None, **kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(trace_id=trace_id, **kwargs)


def clear_trace() -> None:
    structlog.contextvars.clear_contextvars()
