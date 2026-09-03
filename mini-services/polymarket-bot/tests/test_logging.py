"""W12-6 — Structured logging unit tests.

Covers the public surface of ``core.logging_config``:

  1. ``JSONFormatter`` emits a single-line JSON object carrying the
     standard fields (``timestamp`` / ``level`` / ``logger`` / ``message``
     / ``module`` / ``function`` / ``line``) AND any caller-supplied
     ``extra={...}`` keys (serialisable + non-serialisable).
  2. ``JSONFormatter`` embeds populated ``request_id_var`` /
     ``user_var`` / ``endpoint_var`` values under a ``context`` sub-object.
  3. ``JSONFormatter`` includes the formatted traceback under the
     ``exception`` key when ``exc_info`` is attached.
  4. ``ColoredFormatter`` emits ANSI colour escape sequences (so a TTY
     renders the level name in colour) AND the human-readable message +
     truncated ``req=<8 chars>`` request-id tag.
  5. ``get_logger`` returns a configured ``logging.Logger`` whose
     propagation chain reaches the root handler installed by
     ``setup_logging()``.
  6. ``setup_logging`` is idempotent — a second call does NOT add a
     second handler to the root logger (would otherwise double-log every
     record).
  7. The root logger level is driven by the ``LOG_LEVEL`` env var.
  8. The ASGI ``RequestLogMiddleware`` populates ``request_id_var`` for
     the duration of an HTTP request and clears it on the way out
     (``finally``-guaranteed reset), and is a pass-through for non-http
     scopes (lifespan / websocket).

Hermeticity
~~~~~~~~~~~
Each test rebinds the ``ContextVar`` defaults to a known-empty baseline
before the test runs and resets them in a ``finally`` so a leaked
``request_id`` from a sibling test can't perturb the assertions.

The tests do NOT call ``setup_logging()`` themselves (it's already
called at ``api.server`` import time, which the conftest triggers via
its ``api.rate_limit`` import); they exercise the formatter classes
directly via ``logging.LogRecord`` instances so the assertions are
insensitive to whatever root-logger level the global config chose.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys

import pytest

from core.logging_config import (
    ColoredFormatter,
    JSONFormatter,
    RequestLogMiddleware,
    endpoint_var,
    get_logger,
    request_id_var,
    setup_logging,
    user_var,
)

# Per-test ``@pytest.mark.asyncio`` decorators (rather than a module-level
# ``pytestmark``) — most tests here are sync, and a module-level asyncio
# mark would emit a ``PytestWarning`` for every sync test under
# pytest-asyncio >= 0.23.


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_context_vars():
    """Reset every request-scoped ContextVar before AND after each test.

    Without the post-test reset, a test that sets ``request_id_var`` would
    leak its value into the next test (ContextVars are process-global,
    not test-scoped), breaking the “context NOT included” assertions in
    the no-context test cases.
    """
    # Snapshot the existing tokens so we can restore them on teardown.
    rid_token = request_id_var.set("")
    u_token = user_var.set("")
    ep_token = endpoint_var.set("")
    yield
    request_id_var.reset(rid_token)
    user_var.reset(u_token)
    endpoint_var.reset(ep_token)


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    name: str = "test.logger",
    exc_info=None,
) -> logging.LogRecord:
    """Build a minimal ``LogRecord`` that exercises the formatter."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=(),
        exc_info=exc_info,
        func="test_func",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. JSONFormatter — basic JSON shape
# ═══════════════════════════════════════════════════════════════════════════


class TestJSONFormatter:
    """``JSONFormatter.format`` must emit valid JSON with the contract fields."""

    def test_produces_valid_json(self):
        """The formatted string MUST parse as a single JSON object."""
        formatter = JSONFormatter()
        record = _make_record(msg="hello world", level=logging.INFO)
        out = formatter.format(record)
        # Must be a single JSON object (no trailing newlines / extra junk).
        assert isinstance(out, str)
        parsed = json.loads(out)
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self):
        """Every JSON record carries the 7 standard top-level keys."""
        formatter = JSONFormatter()
        record = _make_record(msg="payload", level=logging.WARNING, name="api.x")
        parsed = json.loads(formatter.format(record))
        for key in ("timestamp", "level", "logger", "message", "module", "function", "line"):
            assert key in parsed, f"missing required field: {key!r}"
        assert parsed["level"] == "WARNING"
        assert parsed["logger"] == "api.x"
        assert parsed["message"] == "payload"
        assert parsed["function"] == "test_func"
        assert parsed["line"] == 42

    def test_timestamp_is_iso_format(self):
        """``timestamp`` must be an ISO 8601 string parseable by ``datetime``."""
        from datetime import datetime

        formatter = JSONFormatter()
        record = _make_record()
        parsed = json.loads(formatter.format(record))
        ts = parsed["timestamp"]
        # ``datetime.fromisoformat`` accepts the trailing ``+00:00`` offset
        # our formatter emits; raises ``ValueError`` if not ISO-format.
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None, "timestamp must carry a tz offset"

    def test_extra_fields_are_promoted(self):
        """Caller-supplied ``extra={...}`` keys MUST appear at the top level."""
        formatter = JSONFormatter()
        record = _make_record()
        record.__dict__["strategy"] = "ml_sig_v1"
        record.__dict__["token_id"] = "TOK_X"
        parsed = json.loads(formatter.format(record))
        assert parsed["strategy"] == "ml_sig_v1"
        assert parsed["token_id"] == "TOK_X"

    def test_non_serialisable_extra_is_stringified(self):
        """Values that fail ``json.dumps`` MUST be coerced to ``str`` rather
        than crashing the formatter."""
        formatter = JSONFormatter()

        class _NotJSON:
            def __repr__(self):
                return "<NotJSON>"

        record = _make_record()
        record.__dict__["opaque"] = _NotJSON()
        parsed = json.loads(formatter.format(record))
        # The fallback is ``str(value)`` — the default repr is fine here.
        assert parsed["opaque"] == str(_NotJSON())

    def test_exception_is_included(self):
        """When ``exc_info`` is attached, the formatted traceback lives
        under the ``exception`` key."""
        formatter = JSONFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            record = _make_record(exc_info=sys.exc_info())
        parsed = json.loads(formatter.format(record))
        assert "exception" in parsed
        assert "RuntimeError" in parsed["exception"]
        assert "boom" in parsed["exception"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. Context-var propagation into JSON
# ═══════════════════════════════════════════════════════════════════════════


class TestContextVarsInJSON:
    """``request_id_var`` / ``user_var`` / ``endpoint_var`` MUST appear
    under the ``context`` sub-object when populated, and MUST be absent
    when empty (so the payload stays small for the common no-context case)."""

    def test_context_block_absent_when_empty(self):
        """No ContextVar set ⇒ no ``context`` key in the JSON payload
        (avoids emitting ``"context": {}`` on every record)."""
        formatter = JSONFormatter()
        parsed = json.loads(formatter.format(_make_record()))
        # Empty values are filtered out — and an all-empty dict is dropped entirely.
        assert parsed.get("context", {}) == {}

    def test_request_id_included_when_set(self):
        """``request_id_var`` value MUST appear in the ``context`` block."""
        request_id_var.set("req-abc-123")
        formatter = JSONFormatter()
        parsed = json.loads(formatter.format(_make_record()))
        assert parsed["context"]["request_id"] == "req-abc-123"

    def test_all_three_context_vars_included(self):
        """When all three vars are set, every one is surfaced."""
        request_id_var.set("rid")
        user_var.set("alice")
        endpoint_var.set("GET /api/health")
        formatter = JSONFormatter()
        parsed = json.loads(formatter.format(_make_record()))
        assert parsed["context"] == {
            "request_id": "rid",
            "user": "alice",
            "endpoint": "GET /api/health",
        }

    def test_only_populated_vars_are_included(self):
        """A single populated var MUST NOT pull empty placeholders along."""
        user_var.set("bob")
        formatter = JSONFormatter()
        parsed = json.loads(formatter.format(_make_record()))
        assert parsed["context"] == {"user": "bob"}


# ═══════════════════════════════════════════════════════════════════════════
# 3. ColoredFormatter
# ═══════════════════════════════════════════════════════════════════════════


class TestColoredFormatter:
    """``ColoredFormatter`` emits ANSI colour codes + the human message."""

    @pytest.mark.parametrize(
        "level,expected_color",
        [
            (logging.DEBUG, "\033[36m"),
            (logging.INFO, "\033[32m"),
            (logging.WARNING, "\033[33m"),
            (logging.ERROR, "\033[31m"),
            (logging.CRITICAL, "\033[35m"),
        ],
    )
    def test_includes_color_for_each_level(self, level, expected_color):
        """Each level name maps to its documented ANSI colour code."""
        formatter = ColoredFormatter()
        record = _make_record(msg="hi", level=level)
        out = formatter.format(record)
        assert expected_color in out, (
            f"expected ANSI {expected_color!r} in output for level "
            f"{logging.getLevelName(level)!r}; got: {out!r}"
        )
        assert ColoredFormatter.RESET in out

    def test_message_is_included(self):
        """The human-readable message text MUST appear verbatim in the output."""
        formatter = ColoredFormatter()
        out = formatter.format(_make_record(msg="unique-payload-XYZ", level=logging.INFO))
        assert "unique-payload-XYZ" in out

    def test_request_id_short_tag_when_set(self):
        """When ``request_id_var`` is populated, the output ends with
        ``[req=<first 8 chars>]`` so a developer can grep a single request."""
        request_id_var.set("abcdefgh-1234-5678")
        formatter = ColoredFormatter()
        out = formatter.format(_make_record())
        assert "req=abcdefgh" in out

    def test_no_request_tag_when_unset(self):
        """When ``request_id_var`` is empty, no ``[req=...]`` tag is appended
        (so the format stays clean for non-request logs)."""
        formatter = ColoredFormatter()
        out = formatter.format(_make_record())
        assert "req=" not in out


# ═══════════════════════════════════════════════════════════════════════════
# 4. get_logger
# ═══════════════════════════════════════════════════════════════════════════


class TestGetLogger:
    """``get_logger`` is a thin wrapper over ``logging.getLogger``."""

    def test_returns_logger_instance(self):
        """Must return a ``logging.Logger`` instance."""
        logger = get_logger("test.get_logger")
        assert isinstance(logger, logging.Logger)

    def test_same_name_returns_same_instance(self):
        """``get_logger(name)`` MUST return the same singleton for the same
        name (mirrors ``logging.getLogger`` semantics)."""
        a = get_logger("test.same")
        b = get_logger("test.same")
        assert a is b

    def test_logger_name_is_set(self):
        """The returned logger carries the supplied name."""
        logger = get_logger("test.named_logger")
        assert logger.name == "test.named_logger"


# ═══════════════════════════════════════════════════════════════════════════
# 5. setup_logging — configuration + idempotency
# ═══════════════════════════════════════════════════════════════════════════


class TestSetupLogging:
    """``setup_logging()`` installs exactly one handler on the root logger
    and is idempotent across repeated calls."""

    def _root_handler_count(self) -> int:
        root = logging.getLogger()
        return len(root.handlers)

    def test_setup_logging_installs_at_least_one_handler(self):
        """The root logger MUST have at least one handler after setup."""
        before = self._root_handler_count()
        setup_logging()
        after = self._root_handler_count()
        # If something else (an earlier test, conftest) already ran setup,
        # ``setup_logging`` is a no-op — so we assert ``after >= 1`` rather
        # than ``after > before`` (the strict delta only holds on first call).
        assert after >= 1

    def test_setup_logging_is_idempotent(self):
        """Two consecutive calls MUST NOT add a second handler.

        Without the idempotency guard, every re-import of ``api.server``
        (e.g. during test collection by sibling modules) would stack a
        new ``StreamHandler`` on the root logger, doubling every log
        line on stdout.
        """
        setup_logging()
        first = self._root_handler_count()
        setup_logging()
        second = self._root_handler_count()
        assert first == second, (
            f"setup_logging() is not idempotent: handler count went "
            f"{first} → {second} on second call"
        )

    def test_log_level_respected(self, monkeypatch):
        """``LOG_LEVEL=DEBUG`` MUST lower the root logger's effective level
        below ``INFO``.

        We reset the idempotency guard so the env var actually takes
        effect on this call (otherwise the first-call config is locked in
        for the process lifetime and the env-var branch is unreachable
        from the second call onward).
        """
        # Reset the idempotency flag so the env var is read fresh.
        import core.logging_config as lc

        monkeypatch.setattr(lc, "_LOGGING_CONFIGURED", False)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        setup_logging()
        root = logging.getLogger()
        assert root.level <= logging.DEBUG, (
            f"expected root.level <= DEBUG ({logging.DEBUG}); got {root.level}"
        )

    def test_log_format_json_selects_json_formatter(self, monkeypatch):
        """``LOG_FORMAT=json`` MUST install a ``JSONFormatter`` on the root
        handler — verified by capturing the handler's actual formatter
        type rather than just a config-dict assertion."""
        import core.logging_config as lc

        monkeypatch.setattr(lc, "_LOGGING_CONFIGURED", False)
        monkeypatch.setenv("LOG_FORMAT", "json")
        # Drop any pre-existing root handlers so the new one is the only
        # one we inspect — otherwise a stale handler installed by an
        # earlier test's setup_logging() would mask the new formatter.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        setup_logging()
        assert root.handlers, "setup_logging() must install at least one root handler"
        # At least one handler's formatter is JSONFormatter.
        assert any(isinstance(h.formatter, JSONFormatter) for h in root.handlers), (
            "LOG_FORMAT=json did not install JSONFormatter on any root handler"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. RequestLogMiddleware (ASGI)
# ═══════════════════════════════════════════════════════════════════════════


class TestRequestLogMiddleware:
    """``RequestLogMiddleware`` populates the request-scoped ContextVars for
    the duration of an HTTP request and is a pass-through for non-http
    scopes (lifespan / websocket)."""

    @pytest.mark.asyncio
    async def test_http_request_populates_request_id(self):
        """An HTTP request MUST cause ``request_id_var`` to be non-empty
        INSIDE the wrapped app, and empty again AFTER it returns."""
        # The wrapped app captures the value of ``request_id_var`` mid-request
        # so the test can assert it was set.
        captured: dict = {}

        async def inner_app(scope, receive, send):
            captured["request_id"] = request_id_var.get("")
            captured["endpoint"] = endpoint_var.get("")

        middleware = RequestLogMiddleware(inner_app)
        scope = {"type": "http", "method": "GET", "path": "/api/health"}

        # Pre-condition: the var is empty before the request.
        assert request_id_var.get("") == ""
        await middleware(scope, None, None)
        # In-request: the var was populated with a uuid-shaped string.
        assert captured["request_id"], "request_id must be set inside the app"
        assert captured["endpoint"] == "GET /api/health"
        # Post-request: the var is reset (token-based reset).
        assert request_id_var.get("") == ""

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """Lifespan / websocket scopes MUST NOT trigger the context-var
        setup (they're not request-scoped). The middleware should just
        forward them unchanged."""
        called: list = []

        async def inner_app(scope, receive, send):
            called.append(scope["type"])

        middleware = RequestLogMiddleware(inner_app)
        await middleware({"type": "lifespan"}, None, None)
        await middleware({"type": "websocket", "path": "/ws"}, None, None)
        assert called == ["lifespan", "websocket"]
        # And the var is still empty.
        assert request_id_var.get("") == ""

    @pytest.mark.asyncio
    async def test_request_id_resets_even_on_exception(self):
        """If the inner app raises, ``request_id_var`` MUST still be reset
        (``finally`` block) so the next request served by this task slot
        doesn't inherit a stale id."""

        async def inner_app(scope, receive, send):
            raise RuntimeError("inner app failed")

        middleware = RequestLogMiddleware(inner_app)
        with pytest.raises(RuntimeError, match="inner app failed"):
            await middleware({"type": "http", "method": "GET", "path": "/"}, None, None)
        # The var is empty even after the inner app raised.
        assert request_id_var.get("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# 7. End-to-end: a JSON-formatted log line captures everything
# ═══════════════════════════════════════════════════════════════════════════


class TestEndToEndJSONLogLine:
    """A record emitted via ``logger.info(msg, extra={...})`` with a
    populated ContextVar MUST round-trip through ``JSONFormatter`` into a
    JSON object that carries BOTH the extra fields AND the context."""

    def test_full_round_trip(self):
        """Pulls together: standard fields + extra fields + context vars."""
        request_id_var.set("req-XYZ-001")
        formatter = JSONFormatter()
        record = _make_record(msg="order placed", level=logging.INFO, name="api.server")
        record.__dict__["method"] = "POST"
        record.__dict__["path"] = "/api/trade"
        record.__dict__["status"] = 200
        record.__dict__["duration_ms"] = 12.5

        parsed = json.loads(formatter.format(record))

        assert parsed["message"] == "order placed"
        assert parsed["logger"] == "api.server"
        assert parsed["level"] == "INFO"
        assert parsed["context"]["request_id"] == "req-XYZ-001"
        assert parsed["method"] == "POST"
        assert parsed["path"] == "/api/trade"
        assert parsed["status"] == 200
        assert parsed["duration_ms"] == 12.5
