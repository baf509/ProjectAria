"""
ARIA - Logging Configuration

Purpose: Structured JSON logging with secret scrubbing and correlation IDs for all ARIA services.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional


# Request-scoped correlation ID for end-to-end tracing
correlation_id_var: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> Optional[str]:
    """Get the current correlation ID."""
    return correlation_id_var.get()


def set_correlation_id(cid: Optional[str] = None) -> str:
    """Set a correlation ID for the current async context. Returns the ID."""
    if cid is None:
        cid = uuid.uuid4().hex[:12]
    correlation_id_var.set(cid)
    return cid


_REDACTED = "***REDACTED***"

# Patterns that look like secrets — applied to all log output.
# Each tuple is (compiled regex, replacement string).
# Capture groups in the regex preserve prefixes; the secret portion is replaced.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anthropic keys: sk-ant-...
    (re.compile(r"sk-ant-[A-Za-z0-9\-]{8,}", re.ASCII), _REDACTED),
    # OpenAI / OpenRouter keys: sk-...
    (re.compile(r"sk-[A-Za-z0-9]{20,}", re.ASCII), _REDACTED),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.ASCII), r"\1" + _REDACTED),
    # MongoDB connection strings with credentials
    (re.compile(r"(mongodb(?:\+srv)?://)[^@\s]+@", re.ASCII), r"\1" + _REDACTED + "@"),
    # Generic password/secret in key=value or key: value
    (re.compile(
        r"((?:password|secret|token|api_key|apikey|api-key|access_key|auth)[\s]*[=:]\s*)[^\s,;\"'}{]+",
        re.IGNORECASE,
    ), r"\1" + _REDACTED),
]


def scrub_secrets(text: str) -> str:
    """Replace secret-looking values in a string with a redaction marker."""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class SecretScrubFilter(logging.Filter):
    """Logging filter that redacts secret-looking values from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: scrub_secrets(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    scrub_secrets(str(a)) if isinstance(a, str) else a for a in record.args
                )
        return True


class CorrelationFilter(logging.Filter):
    """Inject the current correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-readable log output."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add source location
        if record.pathname:
            log_entry["source"] = f"{record.pathname}:{record.lineno}"

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add any extra fields attached to the record
        for key in ("correlation_id", "request_id", "conversation_id", "tool_name", "backend", "agent_slug"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        return json.dumps(log_entry, default=str)


def setup_logging(*, json_output: bool = False, level: str = "INFO") -> None:
    """Configure ARIA-wide logging with secret scrubbing.

    Args:
        json_output: Use JSON formatter (for production). False = human-readable.
        level: Root log level.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates on reload
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()

    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    # Always attach the secret scrubbing and correlation filters
    handler.addFilter(SecretScrubFilter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for name in ("uvicorn.access", "httpx", "httpcore", "motor"):
        logging.getLogger(name).setLevel(logging.WARNING)
