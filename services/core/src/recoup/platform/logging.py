"""Structured JSON logging, trace-correlated.

Every log line carries the active OpenTelemetry trace and span ID when one
exists, so a log line and a distributed trace can always be joined (TR-73,
TR-74). PII never reaches this layer -- domain objects carry references, not
values (DATA-MODEL section 4) -- but a belt-and-braces redaction processor
still runs, because a defence that depends on every caller behaving is not
a defence.
"""

import logging
import re
from typing import Any

import structlog
from opentelemetry import trace
from structlog.types import EventDict, Processor

_PII_KEY_PATTERN = re.compile(r"(phone|email|name|address)$", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def _add_trace_context(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _redact_pii_keys(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if _PII_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    """Call once at process startup, before any logger is used."""
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_trace_context,
        _redact_pii_keys,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[log_level]
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    # structlog's get_logger() is untyped upstream; Any is the honest signature,
    # not a shortcut -- callers still get full autocomplete from the bound logger
    # instance itself.
    return structlog.get_logger(name)
