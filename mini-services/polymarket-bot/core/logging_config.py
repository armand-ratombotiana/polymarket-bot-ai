"""Structured JSON logging configuration.

Provides:
- JSON-formatted logs for production (parseable by log aggregators)
- Human-readable colored logs for development
- Contextual fields (request_id, user, endpoint) via contextvars
- Log levels configurable via env var

W12-6 — Structured logging.

Design
------
``setup_logging()`` is the single entry point. It is **idempotent** — calling
it more than once (e.g. when ``api.server`` is imported by several sibling
test modules in one session) is a no-op after the first call, so it cannot
accidentally stack duplicate handlers on the root logger.

Format selection
~~~~~~~~~~~~~~~~
``LOG_FORMAT=json`` (or ``ENV=production``) selects the JSON formatter; any
other value (the default for local dev) selects the colored console formatter.
``LOG_LEVEL`` (default ``INFO``) sets the root logger level.

Context propagation
~~~~~~~~~~~~~~~~~~~
Three ``contextvars.ContextVar`` instances (``request_id_var`` /
``user_var`` / ``endpoint_var``) carry request-scoped data through the
async call stack. ``RequestLogMiddleware`` (ASGI) populates them on every
HTTP request; the JSON formatter embeds them under a ``context`` key so a
log aggregator can filter every log line for a given request without
correlating timestamps.
"""
from __future__ import annotations

import json
import logging
import logging.config
import os
import sys
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# ── Context variables for request-scoped data ────────────────────────────────
# These are set per-request by ``RequestLogMiddleware`` (ASGI) or by the
# inline ``@app.middleware("http")`` decorator in ``api.server``. The
# ``ContextVar`` machinery propagates them across ``await`` boundaries
# without thread-locals, so concurrent requests don't stomp on each other.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_var: ContextVar[str] = ContextVar("user", default="")
endpoint_var: ContextVar[str] = ContextVar("endpoint", default="")

# Idempotency guard — ``setup_logging()`` may be called many times in one
# process (test collection imports ``api.server`` repeatedly, server reload
# in dev mode, etc.). The first call wins; subsequent calls are no-ops so
# we never stack duplicate handlers on the root logger.
_LOGGING_CONFIGURED: bool = False
_CONFIG_LOCK = threading.Lock()


class JSONFormatter(logging.Formatter):
    """JSON log formatter for production.

    Emits a single JSON object per log record on a single line, with
    deterministic top-level keys (``timestamp`` / ``level`` / ``logger`` /
    ``message`` / ``module`` / ``function`` / ``line``) plus a ``context``
    sub-object for any populated request-scoped ``ContextVar`` values and
    an ``exception`` key when ``exc_info`` is attached.

    Extra keyword arguments passed to ``logger.info(..., extra={...})``
    are merged into the top-level object (after a serialisability probe —
    values that fail ``json.dumps`` are stringified so a single bad extra
    never crashes the formatter).
    """

    # Standard ``LogRecord`` attributes that should NOT be promoted to the
    # top-level JSON object — they're either already represented (``module``,
    # ``funcName``, ``lineno``, ``message``) or are pure runtime metadata
    # (``thread``, ``process``, ``relativeCreated`` …).
    _RESERVED_ATTRS: set[str] = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "getMessage",
        "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add context vars (only the non-empty ones, to keep the payload small).
        ctx = {
            "request_id": request_id_var.get(""),
            "user": user_var.get(""),
            "endpoint": endpoint_var.get(""),
        }
        log_entry["context"] = {k: v for k, v in ctx.items() if v}

        # Add exception info (traceback string) when present.
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Promote any caller-supplied ``extra={...}`` keys to the top level.
        for key, value in record.__dict__.items():
            if key in self._RESERVED_ATTRS:
                continue
            if key.startswith("_"):
                continue
            try:
                json.dumps(value)
                log_entry[key] = value
            except (TypeError, ValueError):
                log_entry[key] = str(value)

        return json.dumps(log_entry, default=str)


class ColoredFormatter(logging.Formatter):
    """Colored formatter for development.

    Single-line human-readable format:
    ``HH:MM:SS.mmm LEVEL   logger.name              message [req=abcd1234]``

    ANSI colour codes are emitted unconditionally — most modern terminals
    (and pytest's ``-s`` capture) render them; when piped to a file the
    escape sequences remain but are harmless. A short request_id (first 8
    chars) is appended in brackets when the ``request_id_var`` is set, so
    a developer can grep a single request out of interleaved logs.
    """

    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        ctx_parts: list[str] = []
        rid = request_id_var.get("")
        if rid:
            ctx_parts.append(f"req={rid[:8]}")
        ctx = " ".join(ctx_parts)
        ctx_str = f" [{ctx}]" if ctx else ""
        return (
            f"{color}{timestamp}{self.RESET} "
            f"{color}{record.levelname:<7}{self.RESET} "
            f"{record.name:<20} "
            f"{record.getMessage()}{ctx_str}"
        )


def setup_logging() -> None:
    """Configure logging based on environment (idempotent).

    Reads:
      * ``LOG_LEVEL`` (default ``INFO``) — root logger level.
      * ``LOG_FORMAT`` (default: ``json`` if ``ENV=production``, else
        ``console``) — selects ``JSONFormatter`` or ``ColoredFormatter``.
      * ``ENV`` — when set to ``production``, the default format flips
        to ``json`` (so operators don't have to set both env vars).

    The first call installs the configuration via
    ``logging.config.dictConfig``; subsequent calls are no-ops (guarded
    by ``_LOGGING_CONFIGURED`` under a ``Lock`` so concurrent callers
    in a multi-threaded bootstrap can't double-install).
    """
    global _LOGGING_CONFIGURED
    with _CONFIG_LOCK:
        if _LOGGING_CONFIGURED:
            return
        _LOGGING_CONFIGURED = True

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    default_fmt = "json" if os.environ.get("ENV") == "production" else "console"
    log_format = os.environ.get("LOG_FORMAT", default_fmt)

    formatter: logging.Formatter = (
        JSONFormatter() if log_format == "json" else ColoredFormatter()
    )

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "main",
                "stream": sys.stdout,
            },
        },
        "formatters": {
            "main": {"()": lambda: formatter},
        },
        "loggers": {
            "": {"handlers": ["console"], "level": log_level},
            "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
            "urllib3": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        },
    }
    logging.config.dictConfig(config)


class RequestLogMiddleware:
    """ASGI middleware to add request_id to log context.

    Pure-ASGI variant — works with any ASGI app (Starlette / FastAPI /
    Quart …). For FastAPI apps that already use ``@app.middleware("http")``
    for request logging, prefer enhancing that decorator with the
    ``request_id_var`` directly (avoids stacking two layers that do the
    same job). This class is provided for non-FastAPI ASGI deployments
    and for tests that want to exercise the context-propagation path
    without spinning up a full HTTP middleware stack.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            request_id = str(uuid.uuid4())
            token = request_id_var.set(request_id)
            endpoint_var.set(f"{scope.get('method', '')} {scope.get('path', '')}")
            try:
                await self.app(scope, receive, send)
            finally:
                request_id_var.reset(token)
        else:
            await self.app(scope, receive, send)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name.

    Thin wrapper around ``logging.getLogger`` so callers don't have to
    import ``logging`` directly. Returns the same logger instance
    whether or not ``setup_logging()`` has been called yet (logging is
    lazily configured — a logger created before ``setup_logging`` will
    pick up the new handlers the moment they're installed on the root).
    """
    return logging.getLogger(name)
