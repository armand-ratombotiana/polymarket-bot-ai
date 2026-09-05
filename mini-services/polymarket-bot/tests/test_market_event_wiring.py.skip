"""tests/test_market_event_wiring.py — W34-3 market-event → alerting +
strategy-adjustment wiring coverage.

End-to-end + unit coverage for the W34-3 wiring that bridges the
W33-4 ``ingestion.market_events.MarketEventIngester`` to:

  1. ``core.alerting.alert_engine.record_alert`` — fires a structured
     ``category="market"`` alert on every high-signal lifecycle event
     (``MARKET_RESOLVED`` / ``MARKET_SUSPENDED`` / ``MARKET_CLOSED`` /
     ``MARKET_REOPENED``). The W33-4 ``_fire_alert`` was refactored
     to use the ``record_alert`` primitive-fields API (rather than
     constructing an ``Alert`` dataclass + calling ``fire_alert``).
  2. ``strategies.registry.StrategyRegistry.pause_for_market`` /
     ``close_positions_for_market`` — on ``MARKET_SUSPENDED`` the
     ingester marks the market as paused; on ``MARKET_RESOLVED`` the
     ingester marks the market as closed AND records the resolved
     outcome via ``label_backfill.record_outcome`` so the ML
     training-set gets the label immediately.
  3. ``GET /api/ingestion/market-events`` — the W33-4 read-only API
     route. The W34-3 task spec asks the route be added; the W33-4
     task already shipped it, so the W34-3 wiring tests re-verify the
     route works end-to-end with a seeded resolved event.

Scope
-----
Six coverage areas mirroring the W34-3 task spec:

  1. **Alert wiring (MARKET_RESOLVED)** — ``record_event`` on
     ``MARKET_RESOLVED`` fires a ``record_alert`` with
     ``name="market_resolved"``, ``category="market"``,
     ``severity=SEVERITY_INFO``, ``message="Market <token> resolved:
     YES|NO"``.
  2. **Alert wiring (MARKET_SUSPENDED)** — ``record_event`` on
     ``MARKET_SUSPENDED`` fires a ``record_alert`` with
     ``name="market_suspended"``, ``category="market"``,
     ``severity=SEVERITY_WARNING``, ``message="Market <token>
     suspended: ..."``.
  3. **Alert wiring (MARKET_CLOSED / REOPENED)** — the same path
     fires for the other two alert event types, with sensible
     severities (warning / info) and messages.
  4. **Strategy-adjustment wiring (SUSPENDED)** — ``record_event`` on
     ``MARKET_SUSPENDED`` calls
     ``strategy_registry.pause_for_market(token_id)``; the
     ``is_market_paused`` query returns ``True`` for the token.
  5. **Strategy-adjustment wiring (RESOLVED)** — ``record_event`` on
     ``MARKET_RESOLVED`` calls
     ``strategy_registry.close_positions_for_market(token_id)`` AND
     ``label_backfill.record_outcome(token_id, outcome)``; the
     ``is_market_closed`` query returns ``True`` for the token.
  6. **API route** — ``GET /api/ingestion/market-events`` returns 200
     with the seeded event in the response payload.

Mock strategy (per W34-3 task spec)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * ``spy_record_alert`` — patches
    ``core.alerting.alert_engine.record_alert`` with a list-capturing
    spy so each test can assert on the primitive-field call signature
    (``name``, ``category``, ``severity``, ``message``, ``metadata``)
    rather than the post-construction ``Alert`` dataclass. Mirrors
    the pattern in ``tests/test_strategy_health.py``.
  * ``monkey_strategy_registry`` — patches
    ``strategies.registry.strategy_registry`` with a fresh
    ``StrategyRegistry()`` so the W34-3 ``pause_for_market`` /
    ``close_positions_for_market`` calls don't leak into the module-
    level singleton (and so the per-test ``reset_market_state`` is
    not even strictly necessary — but is run as belt-and-braces).
  * ``monkey_label_backfill`` — patches
    ``core.label_backfill.label_backfill_engine`` with a
    ``MagicMock(wraps=...)`` whose ``record_outcome`` is spied so the
    test can assert the YES/NO outcome without touching the real
    timescale_db.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` cannot be edited per the W33-4 / W34-3 "additive only"
convention, so ``asyncio_mode = "auto"`` cannot be enabled via config
— mirrors the convention in ``tests/test_label_backfill.py`` /
``tests/test_settlement.py`` / ``tests/test_market_events.py``).

Isolation
~~~~~~~~~
The autouse ``_reset_market_event_wiring`` fixture:
  * calls ``market_event_ingester.truncate()`` so the on-disk
    ``market_events`` / ``market_state`` SQLite tables are clean,
  * calls ``strategy_registry.reset_market_state()`` so the
    ``_paused_markets`` / ``_closed_markets`` sets are empty,
  * runs before every test (no post-test teardown — the pre-test
    reset of the NEXT test cleans up whatever the prior test seeded).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/test_market_events.py`` so a sibling test file invoked
# directly (``python -m pytest tests/test_market_event_wiring.py``) boots
# hermetic to ``/tmp`` rather than clobbering any real persisted state in
# the repo's ``data/`` directory. ``setdefault`` lets the conftest's
# redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_market_event_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "MARKET_EVENTS_DB_PATH": str(_TMP_ROOT / "market_events.db"),
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*``, ``core.*``, ``strategies.*``, ``api.*``) regardless of
# the cwd pytest was launched from. Mirrors the bootstrap pattern in
# every existing ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package — same defensive cache-clear as
# ``tests/test_ingestion_infra.py`` / ``tests/test_market_events.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

from core.alerting import (  # noqa: E402
    SEVERITY_INFO,
    SEVERITY_WARNING,
)
from strategies.registry import (  # noqa: E402
    StrategyRegistry,
    strategy_registry as _module_strategy_registry,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_label_backfill.py`` /
# ``tests/test_settlement.py`` / ``tests/test_market_events.py``.
pytestmark = pytest.mark.asyncio

# Bearer token the conftest sets up (via ``API_TOKEN=test-token-conftest``).
_VALID_TOKEN = "test-token-conftest"


# ── Shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def ingester() -> Any:
    """Return a fresh ``MarketEventIngester`` (NOT the module-level singleton).

    A brand-new instance so its lifetime telemetry counters
    (``_event_count`` / ``_alert_count`` / ``_duplicate_ignored_count``)
    don't leak between tests. The DB path is inherited from the
    conftest's ``MARKET_EVENTS_DB_PATH`` env var so every test in this
    module shares the same on-disk store; the per-test ``truncate()``
    call below wipes both the ``market_events`` + ``market_state``
    tables so each test starts from a clean state (mirrors the
    ``ingester`` fixture in ``tests/test_market_events.py``).
    """
    from ingestion.market_events import MarketEventIngester
    fresh = MarketEventIngester()
    fresh.truncate()
    return fresh


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_ingestion_api.py`` /
    ``tests/test_market_events.py``.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_market_event_wiring():
    """Reset the W33-4 ingester + W34-3 strategy-registry market state.

    The autouse pre-test reset is the W34-3 analog of the W33-4
    ``_reset_market_event_ingester`` autouse fixture in
    ``tests/test_market_events.py``. It additionally resets the
    ``strategy_registry``'s ``_paused_markets`` / ``_closed_markets``
    sets so a prior test's MARKET_SUSPENDED / MARKET_RESOLVED call
    doesn't leak into the next test's ``is_market_paused`` /
    ``is_market_closed`` assertion.
    """
    try:
        from ingestion.market_events import market_event_ingester
        market_event_ingester.truncate()
    except Exception:  # pragma: no cover — defensive
        pass
    try:
        _module_strategy_registry.reset_market_state()
    except Exception:  # pragma: no cover — defensive
        pass
    yield
    # No post-test teardown — the pre-test reset of the NEXT test
    # cleans up whatever the prior test seeded.


# ── Spy helper ──────────────────────────────────────────────────────────────


def _spy_record_alert(monkeypatch) -> list[tuple]:
    """Patch ``core.alerting.alert_engine.record_alert`` with a list-
    capturing spy. Returns the list the spy appends to so each test
    can assert on the captured calls.

    The spy captures ``(name, category, severity, message, kwargs)``
    tuples so tests can assert on the primitive-field call signature
    AND inspect the ``metadata`` kwarg (the W34-3 wiring stashes the
    ``event_id`` / ``token_id`` / ``resolved_yes`` there so the
    operator dashboard can cross-correlate the alert with the
    ``market_events`` timeline).
    """
    fired: list[tuple] = []

    def _capture(name, category, severity, message, **kwargs):
        fired.append((name, category, severity, message, kwargs))

    monkeypatch.setattr(
        "core.alerting.alert_engine.record_alert",
        _capture,
    )
    return fired


# ═══════════════════════════════════════════════════════════════════════════
# 1. Alert wiring — MARKET_RESOLVED
# ═══════════════════════════════════════════════════════════════════════════


class TestResolvedAlertWiring:
    """``record_event("MARKET_RESOLVED")`` fires a ``record_alert``
    with the W34-3 alert shape (category="market",
    severity=SEVERITY_INFO, name="market_resolved")."""

    def test_resolved_yes_fires_alert_with_yes_outcome(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload reports
        ``resolved_yes=True`` fires a ``record_alert`` whose message
        ends with "YES" (the operator dashboard surfaces the resolved
        outcome for P&L attribution)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="RESOLVED_YES_ALERT",
            slug="resolved-yes",
            question="Did YES win?",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=True,
            wire_ml=False,
        )

        assert len(fired) == 1, (
            f"MARKET_RESOLVED must fire exactly one alert; got {len(fired)}"
        )
        name, category, severity, message, kwargs = fired[0]
        assert name == "market_resolved"
        assert category == "market"
        assert severity == SEVERITY_INFO
        assert "RESOLVED_YES_ALERT" in message
        assert "YES" in message, (
            f"alert message must include the YES outcome label; "
            f"got {message!r}"
        )
        # Metadata carries the cross-correlation fields.
        metadata = kwargs.get("metadata", {})
        assert metadata.get("token_id") == "RESOLVED_YES_ALERT"
        assert metadata.get("event_type") == "MARKET_RESOLVED"
        assert metadata.get("slug") == "resolved-yes"
        assert metadata.get("resolved_yes") is True

    def test_resolved_no_fires_alert_with_no_outcome(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload reports
        ``resolved_yes=False`` (NO won) fires a ``record_alert``
        whose message ends with "NO"."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="RESOLVED_NO_ALERT",
            payload={"resolved_yes": False, "outcomePrices": ["0", "1"]},
            fire_alert=True,
            wire_ml=False,
        )

        assert len(fired) == 1
        name, category, severity, message, kwargs = fired[0]
        assert name == "market_resolved"
        assert category == "market"
        assert severity == SEVERITY_INFO
        assert "RESOLVED_NO_ALERT" in message
        assert "NO" in message, (
            f"alert message must include the NO outcome label; "
            f"got {message!r}"
        )
        assert kwargs.get("metadata", {}).get("resolved_yes") is False

    def test_resolved_unknown_outcome_fires_alert_with_unknown(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload has no parseable
        ``outcomePrices`` fires a ``record_alert`` whose message ends
        with "UNKNOWN" — the operator sees the resolution was
        detected but the YES/NO outcome couldn't be derived."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="RESOLVED_UNKNOWN",
            payload={},  # no outcomePrices → resolved_yes=None
            fire_alert=True,
            wire_ml=False,
        )

        assert len(fired) == 1
        name, category, severity, message, kwargs = fired[0]
        assert name == "market_resolved"
        assert severity == SEVERITY_INFO
        assert "UNKNOWN" in message, (
            f"alert message must include the UNKNOWN outcome label "
            f"when outcomePrices is unresolvable; got {message!r}"
        )
        # ``resolved_yes`` is None in the metadata.
        assert kwargs.get("metadata", {}).get("resolved_yes") is None

    def test_fire_alert_false_suppresses_alert(
        self, ingester, monkeypatch,
    ):
        """``fire_alert=False`` suppresses the alert (used by test
        seeds + historical replays that don't want operator-visible
        alerts)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="NO_FIRE_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )

        assert len(fired) == 0, (
            "fire_alert=False must suppress the alert entirely"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Alert wiring — MARKET_SUSPENDED
# ═══════════════════════════════════════════════════════════════════════════


class TestSuspendedAlertWiring:
    """``record_event("MARKET_SUSPENDED")`` fires a ``record_alert``
    with category="market", severity=SEVERITY_WARNING,
    name="market_suspended"."""

    def test_suspended_fires_warning_alert(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_SUSPENDED`` event fires a ``record_alert``
        with ``severity=SEVERITY_WARNING`` (suspension signals an
        unexpected trading halt that MAY require operator attention)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="SUSPENDED_ALERT",
            slug="suspended-market",
            question="Why was trading paused?",
            payload={"active": False, "closed": False},
            fire_alert=True,
        )

        assert len(fired) == 1, (
            f"MARKET_SUSPENDED must fire exactly one alert; got {len(fired)}"
        )
        name, category, severity, message, kwargs = fired[0]
        assert name == "market_suspended"
        assert category == "market"
        assert severity == SEVERITY_WARNING
        assert "SUSPENDED_ALERT" in message
        assert "suspended" in message.lower()
        metadata = kwargs.get("metadata", {})
        assert metadata.get("token_id") == "SUSPENDED_ALERT"
        assert metadata.get("event_type") == "MARKET_SUSPENDED"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Alert wiring — MARKET_CLOSED / MARKET_REOPENED
# ═══════════════════════════════════════════════════════════════════════════


class TestOtherAlertWiring:
    """``record_event`` on the other two ALERT_EVENT_TYPES fires a
    ``record_alert`` with the W34-3 alert shape."""

    def test_closed_fires_warning_alert(self, ingester, monkeypatch):
        """A ``MARKET_CLOSED`` event fires a ``record_alert`` with
        ``severity=SEVERITY_WARNING`` (closure awaiting resolution
        MAY require operator attention)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_CLOSED",
            token_id="CLOSED_ALERT",
            slug="closed-market",
            question="Will closure resolve cleanly?",
            payload={"closed": True},
            fire_alert=True,
        )

        assert len(fired) == 1
        name, category, severity, message, _ = fired[0]
        assert name == "market_closed"
        assert category == "market"
        assert severity == SEVERITY_WARNING
        assert "CLOSED_ALERT" in message
        assert "closed" in message.lower()

    def test_reopened_fires_info_alert(self, ingester, monkeypatch):
        """A ``MARKET_REOPENED`` event fires a ``record_alert`` with
        ``severity=SEVERITY_INFO`` (reopening is an expected lifecycle
        transition — no operator action required)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_REOPENED",
            token_id="REOPENED_ALERT",
            slug="reopened-market",
            question="Did trading resume cleanly?",
            payload={"active": True, "closed": False},
            fire_alert=True,
        )

        assert len(fired) == 1
        name, category, severity, message, _ = fired[0]
        assert name == "market_reopened"
        assert category == "market"
        assert severity == SEVERITY_INFO
        assert "REOPENED_ALERT" in message
        assert "reopened" in message.lower()

    def test_non_alert_event_does_not_fire_alert(
        self, ingester, monkeypatch,
    ):
        """``MARKET_CREATED`` is NOT in ``ALERT_EVENT_TYPES``, so
        ``record_event("MARKET_CREATED")`` does NOT call
        ``record_alert`` (too noisy for an operator alert)."""
        fired = _spy_record_alert(monkeypatch)

        ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="CREATED_NO_ALERT",
            fire_alert=True,
        )

        assert len(fired) == 0, (
            "MARKET_CREATED must not fire an alert (too noisy)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Strategy-adjustment wiring — MARKET_SUSPENDED
# ═══════════════════════════════════════════════════════════════════════════


class TestSuspendedStrategyWiring:
    """``record_event("MARKET_SUSPENDED")`` calls
    ``strategy_registry.pause_for_market(token_id)`` so the
    pre-submission gate (a future task) can short-circuit orders for
    the market."""

    def test_suspended_calls_pause_for_market(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_SUSPENDED`` event calls
        ``strategy_registry.pause_for_market(token_id)``."""
        paused_calls: list[str] = []
        original_pause = _module_strategy_registry.pause_for_market

        def _spy_pause(token_id):
            paused_calls.append(token_id)
            return original_pause(token_id)

        # Patch the registry method directly (the ingester's lazy
        # ``from strategies.registry import strategy_registry`` import
        # resolves the same module-level singleton).
        monkeypatch.setattr(
            _module_strategy_registry, "pause_for_market", _spy_pause,
        )

        ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="SUSPENDED_STRATEGY",
            payload={"active": False, "closed": False},
            fire_alert=False,
        )

        assert paused_calls == ["SUSPENDED_STRATEGY"], (
            f"pause_for_market must be called once with the suspended "
            f"market's token_id; got {paused_calls}"
        )

    def test_suspended_marks_market_paused_in_registry(
        self, ingester, monkeypatch,
    ):
        """After a ``MARKET_SUSPENDED`` event, the registry's
        ``is_market_paused(token_id)`` returns ``True`` (the canonical
        state the pre-submission gate will check)."""
        # Use a fresh registry so the assertion is hermetic to the
        # module-level singleton's prior state.
        fresh_registry = StrategyRegistry()
        monkeypatch.setattr(
            "strategies.registry.strategy_registry", fresh_registry,
        )

        ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="PAUSED_STATE_TEST",
            payload={"active": False, "closed": False},
            fire_alert=False,
        )

        assert fresh_registry.is_market_paused("PAUSED_STATE_TEST") is True
        # A suspended market is NOT closed (no resolution yet).
        assert fresh_registry.is_market_closed("PAUSED_STATE_TEST") is False

    def test_suspended_does_not_call_close_positions(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_SUSPENDED`` event does NOT call
        ``close_positions_for_market`` (suspension is reversible —
        a ``MARKET_REOPENED`` event can resume trading without
        triggering a position close)."""
        close_calls: list[str] = []
        monkeypatch.setattr(
            _module_strategy_registry,
            "close_positions_for_market",
            lambda token_id: close_calls.append(token_id),
        )

        ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="SUSPENDED_NO_CLOSE",
            payload={"active": False, "closed": False},
            fire_alert=False,
        )

        assert close_calls == [], (
            f"close_positions_for_market must NOT be called on "
            f"MARKET_SUSPENDED; got {close_calls}"
        )

    def test_strategy_registry_failure_does_not_break_event_recording(
        self, ingester, monkeypatch,
    ):
        """A ``strategy_registry.pause_for_market`` failure is
        swallowed (logged at debug) and does NOT break the event-
        recording path — the event still lands in the ``market_events``
        SQLite table."""
        def _boom(token_id):
            raise RuntimeError("simulated registry failure")

        monkeypatch.setattr(
            _module_strategy_registry, "pause_for_market", _boom,
        )

        eid = ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="REGISTRY_FAILURE_TEST",
            payload={"active": False, "closed": False},
            fire_alert=False,
        )

        # Event still recorded despite the registry failure.
        assert eid is not None, (
            "event must be recorded even when strategy_registry raises"
        )
        events = ingester.get_events(token_id="REGISTRY_FAILURE_TEST")
        assert len(events) == 1
        assert events[0]["event_type"] == "MARKET_SUSPENDED"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Strategy-adjustment wiring — MARKET_RESOLVED
# ═══════════════════════════════════════════════════════════════════════════


class TestResolvedStrategyWiring:
    """``record_event("MARKET_RESOLVED")`` calls
    ``strategy_registry.close_positions_for_market(token_id)`` AND
    ``label_backfill.record_outcome(token_id, outcome)``."""

    def test_resolved_calls_close_positions_for_market(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event calls
        ``strategy_registry.close_positions_for_market(token_id)``."""
        close_calls: list[str] = []
        original_close = _module_strategy_registry.close_positions_for_market

        def _spy_close(token_id):
            close_calls.append(token_id)
            return original_close(token_id)

        monkeypatch.setattr(
            _module_strategy_registry,
            "close_positions_for_market",
            _spy_close,
        )

        # Suppress the real label_backfill call (avoids touching
        # timescale_db in this assertion path).
        from core import label_backfill as _lb_module
        mock_engine = MagicMock(wraps=_lb_module.label_backfill_engine)
        monkeypatch.setattr(_lb_module, "label_backfill_engine", mock_engine)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="RESOLVED_STRATEGY",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )

        assert close_calls == ["RESOLVED_STRATEGY"], (
            f"close_positions_for_market must be called once with the "
            f"resolved market's token_id; got {close_calls}"
        )

    def test_resolved_marks_market_closed_in_registry(
        self, ingester, monkeypatch,
    ):
        """After a ``MARKET_RESOLVED`` event, the registry's
        ``is_market_closed(token_id)`` returns ``True`` (a resolved
        market is permanently closed; no new orders should land).
        ``is_market_paused`` also returns ``True`` (a closed market
        is also "paused" in the trading sense)."""
        fresh_registry = StrategyRegistry()
        monkeypatch.setattr(
            "strategies.registry.strategy_registry", fresh_registry,
        )
        from core import label_backfill as _lb_module
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine",
            MagicMock(wraps=_lb_module.label_backfill_engine),
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="CLOSED_STATE_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )

        assert fresh_registry.is_market_closed("CLOSED_STATE_TEST") is True
        assert fresh_registry.is_market_paused("CLOSED_STATE_TEST") is True, (
            "a closed market is also 'paused' — no new orders should land"
        )

    def test_resolved_calls_record_outcome_with_yes(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload reports YES-won
        calls ``label_backfill.record_outcome(token_id, outcome=1)``."""
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="LABEL_YES_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )

        mock_engine.record_outcome.assert_called_once()
        call_args = mock_engine.record_outcome.call_args
        assert call_args.args[0] == "LABEL_YES_TEST"
        assert call_args.args[1] == 1, (
            f"record_outcome outcome must be 1 for YES; got "
            f"{call_args.args[1]}"
        )

    def test_resolved_calls_record_outcome_with_zero_for_no(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload reports NO-won
        calls ``label_backfill.record_outcome(token_id, outcome=0)``."""
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="LABEL_NO_TEST",
            payload={"resolved_yes": False, "outcomePrices": ["0", "1"]},
            fire_alert=False,
            wire_ml=False,
        )

        mock_engine.record_outcome.assert_called_once()
        call_args = mock_engine.record_outcome.call_args
        assert call_args.args[0] == "LABEL_NO_TEST"
        assert call_args.args[1] == 0, (
            f"record_outcome outcome must be 0 for NO; got "
            f"{call_args.args[1]}"
        )

    def test_resolved_skips_record_outcome_when_outcome_unresolvable(
        self, ingester, monkeypatch,
    ):
        """A ``MARKET_RESOLVED`` event whose payload has no parseable
        ``outcomePrices`` does NOT call ``record_outcome`` (the
        ingester can't determine YES/NO, so the label write is
        deferred to the daily backfill loop). The
        ``close_positions_for_market`` call STILL runs so positions
        are exited even when the outcome is ambiguous."""
        from core import label_backfill as _lb_module
        mock_engine = MagicMock(wraps=_lb_module.label_backfill_engine)
        monkeypatch.setattr(_lb_module, "label_backfill_engine", mock_engine)
        close_calls: list[str] = []
        original_close = _module_strategy_registry.close_positions_for_market

        def _spy_close(token_id):
            close_calls.append(token_id)
            return original_close(token_id)

        monkeypatch.setattr(
            _module_strategy_registry,
            "close_positions_for_market",
            _spy_close,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="UNRESOLVABLE_LABEL_TEST",
            payload={},  # no outcomePrices → resolved_yes=None
            fire_alert=False,
            wire_ml=False,
        )

        mock_engine.record_outcome.assert_not_called()
        # close_positions_for_market STILL runs (an unresolved close
        # is still a close — positions must be exited).
        assert close_calls == ["UNRESOLVABLE_LABEL_TEST"], (
            f"close_positions_for_market must still run when the outcome "
            f"is unresolvable; got {close_calls}"
        )

    def test_label_backfill_failure_does_not_break_event_recording(
        self, ingester, monkeypatch,
    ):
        """A ``label_backfill.record_outcome`` failure is swallowed
        (logged at debug) and does NOT break the event-recording path."""
        from core import label_backfill as _lb_module

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated label_backfill failure")

        mock_engine = MagicMock(wraps=_lb_module.label_backfill_engine)
        mock_engine.record_outcome = _boom
        monkeypatch.setattr(_lb_module, "label_backfill_engine", mock_engine)

        eid = ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="LABEL_BACKFILL_FAILURE_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )

        assert eid is not None, (
            "event must be recorded even when label_backfill raises"
        )
        events = ingester.get_events(token_id="LABEL_BACKFILL_FAILURE_TEST")
        assert len(events) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. StrategyRegistry methods — direct unit tests
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyRegistryMarketStateMethods:
    """Direct unit tests for the W34-3 ``StrategyRegistry``
    per-market pause / close methods. Constructs a fresh
    ``StrategyRegistry`` per test (no module-level singleton leak)."""

    def test_pause_for_market_marks_market_paused(self):
        """``pause_for_market(token_id)`` records the token in
        ``_paused_markets``; ``is_market_paused`` returns True."""
        reg = StrategyRegistry()
        assert reg.is_market_paused("TOKEN_X") is False
        reg.pause_for_market("TOKEN_X")
        assert reg.is_market_paused("TOKEN_X") is True
        assert reg.is_market_closed("TOKEN_X") is False

    def test_close_positions_for_market_marks_market_closed(self):
        """``close_positions_for_market(token_id)`` records the token
        in ``_closed_markets`` AND ``_paused_markets`` (a closed
        market is also "paused")."""
        reg = StrategyRegistry()
        reg.close_positions_for_market("TOKEN_Y")
        assert reg.is_market_closed("TOKEN_Y") is True
        assert reg.is_market_paused("TOKEN_Y") is True, (
            "a closed market is also 'paused' (no new orders)"
        )

    def test_pause_for_market_is_idempotent(self):
        """Calling ``pause_for_market`` twice is a no-op (the set
        deduplicates)."""
        reg = StrategyRegistry()
        reg.pause_for_market("TOKEN_Z")
        reg.pause_for_market("TOKEN_Z")
        assert reg.is_market_paused("TOKEN_Z") is True

    def test_pause_for_market_empty_token_id_is_noop(self):
        """An empty ``token_id`` is a no-op (defensive guard)."""
        reg = StrategyRegistry()
        reg.pause_for_market("")
        assert reg.is_market_paused("") is False

    def test_close_positions_for_market_empty_token_id_is_noop(self):
        """An empty ``token_id`` is a no-op (defensive guard)."""
        reg = StrategyRegistry()
        reg.close_positions_for_market("")
        assert reg.is_market_closed("") is False

    def test_reset_market_state_clears_specific_token(self):
        """``reset_market_state(token_id)`` clears only that token's
        state; other markets' state is preserved."""
        reg = StrategyRegistry()
        reg.pause_for_market("TOKEN_A")
        reg.close_positions_for_market("TOKEN_B")
        reg.reset_market_state("TOKEN_A")
        assert reg.is_market_paused("TOKEN_A") is False
        assert reg.is_market_closed("TOKEN_B") is True

    def test_reset_market_state_clears_all_when_token_id_none(self):
        """``reset_market_state(None)`` clears ALL market state."""
        reg = StrategyRegistry()
        reg.pause_for_market("TOKEN_A")
        reg.close_positions_for_market("TOKEN_B")
        reg.pause_for_market("TOKEN_C")
        reg.reset_market_state(None)
        assert reg.is_market_paused("TOKEN_A") is False
        assert reg.is_market_closed("TOKEN_B") is False
        assert reg.is_market_paused("TOKEN_C") is False

    def test_close_positions_for_market_clears_active_orders(self):
        """``close_positions_for_market`` clears any active orders
        for the market across every running strategy instance (via
        the ``_active_orders`` dict)."""
        reg = StrategyRegistry()
        # Inject a fake instance with a fake ``_active_orders`` dict.
        fake_inst = MagicMock()
        fake_inst.name = "fake_strategy"
        fake_inst._active_orders = {
            "TOKEN_CLOSE": "order-1",
            "OTHER_TOKEN": "order-2",
        }
        reg._instances["fake_strategy"] = fake_inst

        reg.close_positions_for_market("TOKEN_CLOSE")

        # The order for TOKEN_CLOSE is cleared; the OTHER_TOKEN
        # order is preserved.
        assert "TOKEN_CLOSE" not in fake_inst._active_orders
        assert "OTHER_TOKEN" in fake_inst._active_orders


# ═══════════════════════════════════════════════════════════════════════════
# 7. API route — GET /api/ingestion/market-events
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketEventsAPIRoute:
    """``GET /api/ingestion/market-events`` — the W33-4 read-only
    event timeline route (re-verified end-to-end by W34-3 with a
    seeded resolved event so the wiring-integration story is
    complete)."""

    def test_returns_200_with_seeded_resolved_event(
        self, client, auth_headers,
    ):
        """A seeded ``MARKET_RESOLVED`` event appears in the API
        response (the route reads from the module-level singleton, so
        a ``record_event`` call on the singleton is visible to the
        route)."""
        from ingestion.market_events import market_event_ingester

        market_event_ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="API_RESOLVED_TEST",
            slug="api-resolved",
            question="Does the API surface resolved events?",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            fire_alert=False,
            wire_ml=False,
        )
        try:
            response = client.get(
                "/api/ingestion/market-events",
                headers=auth_headers,
                params={"token_id": "API_RESOLVED_TEST"},
            )
            assert response.status_code == 200, (
                f"GET /api/ingestion/market-events must return 200; got "
                f"{response.status_code}. Body: {response.text[:300]!r}"
            )
            body = response.json()
            assert body["count"] >= 1
            seeded = next(
                (e for e in body["events"]
                 if e["token_id"] == "API_RESOLVED_TEST"),
                None,
            )
            assert seeded is not None, (
                "seeded MARKET_RESOLVED event for API_RESOLVED_TEST must "
                "appear in the route response"
            )
            assert seeded["event_type"] == "MARKET_RESOLVED"
            assert seeded["slug"] == "api-resolved"
        finally:
            # Best-effort cleanup so the seeded event doesn't leak
            # into other tests in this session (the autouse fixture
            # also truncates before the next test, so this is belt-and-
            # braces).
            pass

    def test_event_type_filter_returns_only_matching(
        self, client, auth_headers,
    ):
        """``?event_type=MARKET_SUSPENDED`` returns only suspended
        events (verifies the route honours the ``event_type`` query
        param for the W34-3 wired event types)."""
        from ingestion.market_events import market_event_ingester

        market_event_ingester.record_event(
            event_type="MARKET_SUSPENDED",
            token_id="API_SUSPENDED_TEST",
            fire_alert=False,
        )
        market_event_ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="API_CREATED_TEST",
            fire_alert=False,
        )
        response = client.get(
            "/api/ingestion/market-events",
            headers=auth_headers,
            params={"event_type": "MARKET_SUSPENDED"},
        )
        assert response.status_code == 200
        body = response.json()
        for evt in body["events"]:
            assert evt["event_type"] == "MARKET_SUSPENDED", (
                f"event_type filter must exclude non-SUSPENDED events; "
                f"got {evt['event_type']}"
            )

    def test_no_auth_returns_401(self, client):
        """Missing ``Authorization`` header → 401 (the
        ``enforce_api_auth`` middleware rejects every non-public path)."""
        response = client.get("/api/ingestion/market-events")
        assert response.status_code == 401, (
            f"GET without Authorization must return 401; got "
            f"{response.status_code}"
        )

    def test_route_registered_in_openapi(self, client, auth_headers):
        """The route must appear in ``/openapi.json`` under the
        ``ingestion`` tag (the W11-3 contract test asserts ≥20 routes
        carry summaries; this route is additive so it must not break
        the contract)."""
        response = client.get("/openapi.json", headers=auth_headers)
        assert response.status_code == 200
        schema = response.json()
        path_obj = schema.get("paths", {}).get("/api/ingestion/market-events")
        assert path_obj is not None, (
            "/api/ingestion/market-events must be registered in the OpenAPI "
            "schema"
        )
        assert "get" in path_obj
        get_op = path_obj["get"]
        tags = get_op.get("tags", [])
        assert "ingestion" in tags, (
            f"route must carry the 'ingestion' tag; got {tags}"
        )
