"""tests/test_market_events.py — W33-4 market event ingestion coverage.

End-to-end + unit coverage for the W33-4 market lifecycle event
ingester (``ingestion.market_events``) and the additive ML label-
generation wiring it triggers on ``MARKET_RESOLVED``.

Scope
-----
Five coverage areas mirroring the W33-4 task spec's "Step 5" list:

  1. **Market creation detection** — ``detect_events`` emits
     ``MARKET_CREATED`` for every active market the ingester hasn't
     seen before. Verified via the mock-gamma path with one fresh
     active market.
  2. **Resolution detection** — ``detect_events`` emits
     ``MARKET_CLOSED`` + ``MARKET_RESOLVED`` for a market whose Gamma
     payload reports ``closed=True`` + ``outcomePrices=["1","0"]``.
     Verified via the mock-gamma path with one resolved market.
  3. **Event storage** — ``record_event`` stores the event in the
     ``market_events`` SQLite table, mirrors the raw payload into the
     W31-1 ``raw_vault``, and ``get_events`` returns the stored event
     with the payload parsed back from JSON.
  4. **ML label generation on resolution** — when ``record_event`` is
     called with ``event_type="MARKET_RESOLVED"`` and
     ``wire_ml=True``, the synchronous label write
     (``label_backfill.record_outcome``) is invoked AND the async ML
     update + cache-invalidation is scheduled. Verified by monkey-
     patching ``label_backfill_engine.record_outcome`` + the
     ``feature_pipeline`` + ``ml_model`` symbols.
  5. **API route** — ``GET /api/ingestion/market-events`` returns 200
     with the events list + ingester stats block, honours the
     ``token_id`` / ``event_type`` / ``limit`` query params, returns
     422 for an invalid ``event_type``, returns 401 when auth is
     missing, and returns the zero-state (empty ``events`` list)
     when no events have been recorded.

Mock strategy (per W33-4 task spec — "mocked gamma_client")
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * ``mock_gamma`` — a ``MagicMock`` whose ``get_markets`` and
    ``get_resolved_markets`` are ``AsyncMock`` attributes returning
    controlled market payloads. Injected via ``MarketEventIngester(gamma_client=mock_gamma)``
    so the production ``core.gamma_client.gamma_client`` singleton is
    NOT perturbed.
  * ``monkey_label_backfill`` — patches
    ``core.label_backfill.label_backfill_engine`` (the singleton the
    ingester resolves inside ``_wire_ml_label_generation``) with
    a ``MagicMock(wraps=...)`` whose ``record_outcome`` is spied.
  * ``monkey_feature_pipeline`` / ``monkey_ml_model`` — patches the
    module-level singletons the ingester resolves inside
    ``_wire_ml_label_generation`` with ``MagicMock``s whose
    ``get_features`` is an ``AsyncMock`` and whose ``update`` /
    ``invalidate`` are sync ``MagicMock``s. The ingester uses
    ``asyncio.get_running_loop().create_task`` for the async path,
    so tests await one ``await asyncio.sleep(0)`` after the
    ``record_event`` call to let the scheduled task complete.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` cannot be edited per the W33-4 "Do NOT edit existing
files" constraint, so ``asyncio_mode = "auto"`` cannot be enabled
via config — mirrors the convention in ``tests/test_label_backfill.py``
/ ``tests/test_settlement.py`` / etc.).

Isolation
~~~~~~~~~
The autouse ``_reset_market_event_ingester`` fixture calls
``market_event_ingester.truncate()`` before every test so the
module-level singleton + on-disk SQLite tables are clean. The
``ingester`` fixture (for unit tests) constructs a fresh
``MarketEventIngester`` and also calls ``truncate()`` so the per-test
DB state is hermetic. Cross-test pollution from prior tests in the
same pytest session is fully eliminated (mirrors the
``_reset_ingestion_singletons`` autouse fixture in
``tests/test_ingestion_api.py``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` so a sibling test file invoked directly
# (``python -m pytest tests/test_market_events.py``) boots hermetic to
# ``/tmp`` rather than clobbering any real persisted state in the repo's
# ``data/`` directory. ``setdefault`` lets the conftest's redirect win
# when both run.
_TMP_ROOT = Path("/tmp/pmbot_market_events_tests")
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
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    # W33-4 — the market event ingester's SQLite db. Module-level
    # singleton ``market_event_ingester`` is constructed at import
    # time and would otherwise try to mkdir ``/app/data`` (read-only
    # in the sandbox) — same defensive pattern as RAW_VAULT_DB_PATH.
    "MARKET_EVENTS_DB_PATH": str(_TMP_ROOT / "market_events.db"),
    # W32-4 lineage db (the W33-4 ingester doesn't use it directly, but
    # ``ingestion/__init__.py`` imports it defensively at package
    # import time, so the redirect keeps the test sandbox hermetic).
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*``, ``core.*``, ``api.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing
# our top-level ``ingestion`` package — same defensive cache-clear as
# ``tests/test_ingestion_infra.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_label_backfill.py`` /
# ``tests/test_settlement.py``.
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
    module shares the same on-disk store; the per-test
    ``ingester.truncate()`` call below wipes both the ``market_events``
    + ``market_state`` tables so each test starts from a clean state
    (mirrors the ``dead_letter_queue.clear()`` convention in
    ``tests/test_ingestion_api.py``).
    """
    from ingestion.market_events import MarketEventIngester
    fresh = MarketEventIngester()
    # Belt-and-braces: wipe the on-disk store so a prior test's seeded
    # events + state don't leak into this test's assertions. ``truncate``
    # also clears the in-memory cache + counters.
    fresh.truncate()
    return fresh


@pytest.fixture
def mock_gamma() -> MagicMock:
    """Mock gamma_client with AsyncMock market-fetch methods.

    Production ``get_markets`` / ``get_resolved_markets`` are async —
    ``AsyncMock`` lets the ingester's ``await`` resolve against a
    controlled payload without spinning up a real HTTP transport.
    """
    mock = MagicMock()
    mock.get_markets = AsyncMock(return_value=[])
    mock.get_resolved_markets = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return a sanitised 500 instead of re-raising in the test process —
    mirrors the pattern in ``tests/test_ingestion_api.py``.

    The limiter is disabled in ``conftest.py`` so the ``READ_LIMIT``
    decorator doesn't 429 the second request in a class.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_market_event_ingester():
    """Reset the W33-4 ``market_event_ingester`` singleton before every test.

    The API route under ``/api/ingestion/market-events`` reads from the
    module-level singleton — it persists across tests within a pytest
    session. Without a reset, a prior test's seeded event would leak
    into the next test's HTTP response and break the count / membership
    assertions. Mirrors the ``_reset_ingestion_singletons`` autouse
    fixture in ``tests/test_ingestion_api.py``.

    ``truncate()`` (NOT ``reset_stats()``) so the on-disk SQLite tables
    are also wiped — a prior test's seeded ``MARKET_CREATED`` event for
    token ``"ACTIVE_YES"`` would otherwise show up in the next test's
    ``get_events(token_id="ACTIVE_YES")`` assertion (the dedup UNIQUE
    constraint silently drops duplicates, but the FIRST event still
    appears in the query result).
    """
    try:
        from ingestion.market_events import market_event_ingester
        market_event_ingester.truncate()
    except Exception:  # pragma: no cover — defensive
        pass
    yield
    # No post-test teardown — the pre-test reset of the NEXT test
    # cleans up whatever the prior test seeded.


# ── Module-level helpers ────────────────────────────────────────────────────


def _make_active_market(
    token_id: str = "ACTIVE_YES",
    *,
    condition_id: str = "0xACTIVE",
    slug: str = "active-market",
    question: str = "Will the active market work?",
    liquidity: float = 5000.0,
    active: bool = True,
    closed: bool = False,
) -> dict:
    """Build a synthetic active market dict in the Gamma shape."""
    return {
        "conditionId": condition_id,
        "slug": slug,
        "question": question,
        "clobTokenIds": f'["{token_id}","{token_id.replace("YES","NO")}"]',
        "active": active,
        "closed": closed,
        "liquidity": liquidity,
        "volume24hr": 10000.0,
    }


def _make_resolved_market(
    token_id: str = "RESOLVED_YES",
    *,
    condition_id: str = "0xRESOLVED",
    slug: str = "resolved-market",
    question: str = "Did the resolution fire?",
    outcome_prices: list[str] | None = None,
) -> dict:
    """Build a synthetic resolved market dict in the Gamma shape."""
    if outcome_prices is None:
        outcome_prices = ["1", "0"]  # YES won
    return {
        "conditionId": condition_id,
        "slug": slug,
        "question": question,
        "clobTokenIds": f'["{token_id}","{token_id.replace("YES","NO")}"]',
        "active": False,
        "closed": True,
        "outcomePrices": outcome_prices,
        "liquidity": 0.0,
        "volume24hr": 50000.0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Market creation detection
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketCreationDetection:
    """``detect_events`` emits ``MARKET_CREATED`` for new active markets."""

    async def test_emits_market_created_for_new_active_market(
        self, ingester, mock_gamma,
    ):
        """A single active market the ingester hasn't seen → one
        ``MARKET_CREATED`` event with the token_id / slug / question
        propagated from the Gamma payload."""
        mock_gamma.get_markets.return_value = [_make_active_market()]
        mock_gamma.get_resolved_markets.return_value = []
        ingester._gamma_client = mock_gamma

        emitted = await ingester.detect_events()
        assert emitted == 1, (
            f"detect_events should emit exactly 1 event for one new active "
            f"market; got {emitted}"
        )

        events = ingester.get_events(event_type="MARKET_CREATED")
        assert len(events) == 1, (
            f"expected 1 MARKET_CREATED event in storage; got {len(events)}"
        )
        evt = events[0]
        assert evt["event_type"] == "MARKET_CREATED"
        assert evt["token_id"] == "ACTIVE_YES"
        assert evt["condition_id"] == "0xACTIVE"
        assert evt["slug"] == "active-market"
        assert evt["question"] == "Will the active market work?"
        # Payload carries the full Gamma dict so a debugger can replay.
        assert evt["payload"]["conditionId"] == "0xACTIVE"
        assert evt["acknowledged"] is False

    async def test_does_not_reemit_for_known_market(
        self, ingester, mock_gamma,
    ):
        """A market the ingester has already seen (cached in
        ``_market_state``) does NOT trigger another ``MARKET_CREATED``
        — the second poll is a no-op for that market."""
        mock_gamma.get_markets.return_value = [_make_active_market()]
        mock_gamma.get_resolved_markets.return_value = []
        ingester._gamma_client = mock_gamma

        # First poll — emits MARKET_CREATED.
        first = await ingester.detect_events()
        assert first == 1
        # Second poll — no new events (the market is now cached).
        second = await ingester.detect_events()
        assert second == 0, (
            f"second poll should emit 0 events for an already-known market; "
            f"got {second}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Resolution detection
# ═══════════════════════════════════════════════════════════════════════════


class TestResolutionDetection:
    """``detect_events`` emits ``MARKET_CLOSED`` + ``MARKET_RESOLVED``
    for newly resolved markets."""

    async def test_emits_market_closed_and_resolved_for_newly_resolved(
        self, ingester, mock_gamma,
    ):
        """A closed market with a YES-winning ``outcomePrices`` payload
        triggers BOTH ``MARKET_CLOSED`` and ``MARKET_RESOLVED`` events
        (the closure is the lifecycle transition; the resolution is the
        outcome determination)."""
        mock_gamma.get_markets.return_value = []
        mock_gamma.get_resolved_markets.return_value = [
            _make_resolved_market(outcome_prices=["1", "0"]),
        ]
        ingester._gamma_client = mock_gamma

        emitted = await ingester.detect_events()
        assert emitted == 2, (
            f"detect_events should emit 2 events (MARKET_CLOSED + "
            f"MARKET_RESOLVED) for one newly-resolved market; got {emitted}"
        )

        resolved_events = ingester.get_events(event_type="MARKET_RESOLVED")
        assert len(resolved_events) == 1
        assert resolved_events[0]["token_id"] == "RESOLVED_YES"
        assert resolved_events[0]["payload"]["resolved_yes"] is True

        closed_events = ingester.get_events(event_type="MARKET_CLOSED")
        assert len(closed_events) == 1
        assert closed_events[0]["token_id"] == "RESOLVED_YES"

    async def test_emits_market_resolved_no_when_no_wins(
        self, ingester, mock_gamma,
    ):
        """A resolved market with ``outcomePrices=["0","1"]`` (NO won)
        emits a ``MARKET_RESOLVED`` event whose payload reports
        ``resolved_yes=False`` (the spec's ``outcome = 1 if resolved_yes
        else 0`` wiring)."""
        mock_gamma.get_markets.return_value = []
        mock_gamma.get_resolved_markets.return_value = [
            _make_resolved_market(outcome_prices=["0", "1"]),
        ]
        ingester._gamma_client = mock_gamma

        await ingester.detect_events()
        resolved_events = ingester.get_events(event_type="MARKET_RESOLVED")
        assert len(resolved_events) == 1
        assert resolved_events[0]["payload"]["resolved_yes"] is False

    async def test_skips_resolution_when_outcomeprices_missing(
        self, ingester, mock_gamma,
    ):
        """A closed market WITHOUT a parseable ``outcomePrices`` payload
        emits ``MARKET_CLOSED`` but NOT ``MARKET_RESOLVED`` (the
        ingester can't determine the YES/NO outcome, so the resolution
        event is deferred until the daily backfill loop can derive it
        from the synthetic book)."""
        mock_gamma.get_markets.return_value = []
        # Strip outcomePrices — the market is closed but unresolved.
        mkt = _make_resolved_market()
        mkt.pop("outcomePrices")
        mock_gamma.get_resolved_markets.return_value = [mkt]
        ingester._gamma_client = mock_gamma

        emitted = await ingester.detect_events()
        # MARKET_CLOSED only — no MARKET_RESOLVED because outcome is None.
        assert emitted == 1, (
            f"closed-but-unresolved market should emit exactly 1 event "
            f"(MARKET_CLOSED only); got {emitted}"
        )
        closed_events = ingester.get_events(event_type="MARKET_CLOSED")
        assert len(closed_events) == 1
        resolved_events = ingester.get_events(event_type="MARKET_RESOLVED")
        assert len(resolved_events) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Event storage
# ═══════════════════════════════════════════════════════════════════════════


class TestEventStorage:
    """``record_event`` stores events; ``get_events`` returns them."""

    def test_record_event_returns_uuid(self, ingester):
        """``record_event`` returns a UUID4 string ``event_id`` that
        uniquely identifies the stored event."""
        eid = ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="STORAGE_TEST",
            fire_alert=False,
        )
        assert eid is not None
        # UUID4 hex format — 36 chars with dashes.
        assert len(eid) == 36
        assert eid.count("-") == 4

    def test_invalid_event_type_raises_valueerror(self, ingester):
        """An event_type outside ``EVENT_TYPES`` raises ``ValueError``
        (the precondition check, NOT a silent drop)."""
        with pytest.raises(ValueError, match="event_type must be one of"):
            ingester.record_event(
                event_type="NOT_A_REAL_EVENT_TYPE",
                token_id="X",
                fire_alert=False,
            )

    def test_get_events_returns_payload_parsed(self, ingester):
        """``get_events`` returns the ``payload`` field parsed back
        from JSON to a Python dict (the storage path serialises with
        ``default=str``, but the canonical JSON round-trip is preserved
        for the common dict-of-primitives shape)."""
        payload = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}
        eid = ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="PAYLOAD_TEST",
            payload=payload,
            fire_alert=False,
        )
        events = ingester.get_events(token_id="PAYLOAD_TEST")
        assert len(events) == 1
        assert events[0]["event_id"] == eid
        assert events[0]["payload"] == payload

    def test_get_events_filters_by_token_id(self, ingester):
        """``get_events(token_id=X)`` returns only events for that
        token (the ``idx_market_events_token`` index services the
        query — no full table scan)."""
        ingester.record_event(
            event_type="MARKET_CREATED", token_id="TOKEN_A",
            fire_alert=False,
        )
        ingester.record_event(
            event_type="MARKET_CREATED", token_id="TOKEN_B",
            fire_alert=False,
        )
        a_events = ingester.get_events(token_id="TOKEN_A")
        assert len(a_events) == 1
        assert a_events[0]["token_id"] == "TOKEN_A"

    def test_get_events_filters_by_event_type(self, ingester):
        """``get_events(event_type=X)`` returns only events of that
        type (the ``idx_market_events_type`` index services the
        query)."""
        ingester.record_event(
            event_type="MARKET_CREATED", token_id="FILTER_TYPE_1",
            fire_alert=False,
        )
        ingester.record_event(
            event_type="MARKET_CLOSED", token_id="FILTER_TYPE_2",
            fire_alert=False,
        )
        created = ingester.get_events(event_type="MARKET_CREATED")
        assert len(created) == 1
        assert created[0]["event_type"] == "MARKET_CREATED"

    def test_get_events_orders_by_timestamp_desc(self, ingester):
        """``get_events`` returns the most-recent-first ordering so a
        dashboard's "recent events" view shows the latest at the top."""
        eid1 = ingester.record_event(
            event_type="MARKET_CREATED", token_id="ORDER_1",
            timestamp=time.time() - 100.0, fire_alert=False,
        )
        eid2 = ingester.record_event(
            event_type="MARKET_CREATED", token_id="ORDER_2",
            timestamp=time.time(), fire_alert=False,
        )
        events = ingester.get_events(limit=10)
        # Most-recent-first → ORDER_2 before ORDER_1.
        assert events[0]["event_id"] == eid2
        assert events[1]["event_id"] == eid1

    def test_get_events_limits_to_cap(self, ingester):
        """``get_events(limit=N)`` caps the result count at N (the
        hard ceiling ``MAX_EVENT_LIMIT = 1000`` is also enforced)."""
        for i in range(5):
            ingester.record_event(
                event_type="MARKET_CREATED",
                token_id=f"LIMIT_{i}",
                fire_alert=False,
            )
        events = ingester.get_events(limit=3)
        assert len(events) == 3

    def test_record_event_mirrors_to_raw_vault(self, ingester):
        """``record_event`` writes a mirror copy to the W31-1
        ``raw_vault`` so the event is audit-grade replayable. Verified
        by querying the vault's ``replay_range`` for the
        ``market_market_created`` event_type."""
        from ingestion.raw_vault import raw_vault

        # Reset the vault's dedup deque so a prior test's seed doesn't
        # mask the assertion (the vault's UNIQUE constraint is the
        # restart-safe backstop — clearing the deque is sufficient for
        # the in-memory fast-path check).
        raw_vault.reset_stats()

        ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="VAULT_MIRROR_TEST",
            fire_alert=False,
        )

        vault_records = list(raw_vault.replay_range(
            source="gamma",
            event_type="market_market_created",
            limit=10,
        ))
        vault_tokens = [
            r.get("raw_payload", {}).get("token_id")
            for r in vault_records
        ]
        assert "VAULT_MIRROR_TEST" in vault_tokens, (
            "MARKET_CREATED event must be mirrored into the raw_vault so "
            "the event is audit-grade replayable"
        )

    def test_acknowledge_event_flips_flag(self, ingester):
        """``acknowledge_event(event_id)`` sets ``acknowledged=1`` on
        the stored row (mirrors the ``Alert.acknowledged`` convention
        so a future dashboard can reuse the same ack-queue UX)."""
        eid = ingester.record_event(
            event_type="MARKET_CREATED", token_id="ACK_TEST",
            fire_alert=False,
        )
        ok = ingester.acknowledge_event(eid)
        assert ok is True
        events = ingester.get_events(token_id="ACK_TEST")
        assert events[0]["acknowledged"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 4. ML label generation on resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestMLLabelGeneration:
    """``record_event("MARKET_RESOLVED", wire_ml=True)`` triggers the
    ML label-generation pipeline (label_backfill.record_outcome →
    ml_model.update → feature_pipeline.invalidate)."""

    async def test_record_outcome_called_on_resolution(
        self, ingester, monkeypatch,
    ):
        """``record_event("MARKET_RESOLVED", wire_ml=True)`` calls
        ``label_backfill_engine.record_outcome(token_id, outcome)``
        with ``outcome=1`` when ``resolved_yes=True``.

        W34-3 contract: ``record_outcome`` is invoked from TWO paths on
        a resolved market — the synchronous strategy-registry wiring
        (``_wire_strategy_registry`` durably records the label so the
        daily backfill retrain picks it up regardless of the
        ``wire_ml`` flag) AND the async ML wiring (``wire_ml=True``
        also schedules the online ``ml_model.update`` + cache
        invalidation). Both calls carry the same ``(token_id, outcome)``
        args so we assert on ``call_args`` (most-recent call) rather
        than ``assert_called_once``.

        The ingester uses ``asyncio.get_running_loop().create_task`` for
        the async path, so we await one ``await asyncio.sleep(0)`` to
        let the scheduled task complete before asserting.
        """
        # Spy on ``label_backfill_engine.record_outcome``.
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="ML_WIRE_TEST_YES",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            wire_ml=True,
            fire_alert=False,
        )
        # Let the scheduled async task complete.
        await asyncio.sleep(0.01)

        # W34-3 — ``record_outcome`` is called at least once (the
        # synchronous strategy-registry wiring always records the label
        # for a resolved market). The async ML wiring may add a second
        # call with the same args; we assert on the most-recent call
        # rather than the call count so the test is robust to the
        # W34-3 dual-path contract.
        mock_engine.record_outcome.assert_called()
        assert mock_engine.record_outcome.call_count >= 1
        call_args = mock_engine.record_outcome.call_args
        # The first positional arg is the token_id; the second is the
        # outcome (1 for YES).
        assert call_args.args[0] == "ML_WIRE_TEST_YES"
        assert call_args.args[1] == 1, (
            f"record_outcome must be called with outcome=1 when "
            f"resolved_yes=True; got {call_args.args[1]}"
        )
        # Every call must carry the same expected args (the sync +
        # async paths both write the same label — a divergence would
        # be a real bug).
        for call in mock_engine.record_outcome.call_args_list:
            assert call.args[0] == "ML_WIRE_TEST_YES"
            assert call.args[1] == 1

    async def test_record_outcome_outcome_zero_when_no_wins(
        self, ingester, monkeypatch,
    ):
        """When the resolved market's ``outcomePrices=["0","1"]``,
        ``record_outcome`` is called with ``outcome=0`` (NO won).

        W34-3 — see ``test_record_outcome_called_on_resolution`` for
        the dual-path contract: the synchronous strategy-registry
        wiring + the async ML wiring both write the same label. We
        assert on ``call_args`` (most-recent call) and verify every
        call carries ``outcome=0``.
        """
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="ML_WIRE_TEST_NO",
            payload={"resolved_yes": False, "outcomePrices": ["0", "1"]},
            wire_ml=True,
            fire_alert=False,
        )
        await asyncio.sleep(0.01)

        mock_engine.record_outcome.assert_called()
        assert mock_engine.record_outcome.call_count >= 1
        call_args = mock_engine.record_outcome.call_args
        assert call_args.args[0] == "ML_WIRE_TEST_NO"
        assert call_args.args[1] == 0
        for call in mock_engine.record_outcome.call_args_list:
            assert call.args[0] == "ML_WIRE_TEST_NO"
            assert call.args[1] == 0

    async def test_ml_wiring_skipped_when_wire_ml_false(
        self, ingester, monkeypatch,
    ):
        """``wire_ml=False`` does NOT trigger the async ML wiring path
        (``feature_pipeline.invalidate`` + ``ml_model.update``) — the
        online update + cache invalidation are opt-in per call so a
        test seed or a historical replay doesn't trigger an unwanted
        retrain.

        W34-3 contract note: ``label_backfill.record_outcome`` IS
        still called by the synchronous strategy-registry wiring
        (the label is durably recorded for the daily backfill retrain
        regardless of the ``wire_ml`` flag — see
        ``_wire_strategy_registry``'s docstring). What ``wire_ml``
        controls is the additional async online ML update + feature
        cache invalidation. We therefore spy on
        ``feature_pipeline.invalidate`` (the W33-4 additive method)
        rather than ``record_outcome`` to verify the opt-in contract.
        """
        # Mock the ``get_feature_pipeline`` lazy resolver so the
        # ingester's async ML wiring picks up our spy if (and only if)
        # ``wire_ml=True``.
        from ingestion import feature_pipeline as _fp_module
        mock_pipe = MagicMock()
        mock_pipe.get_features = AsyncMock(return_value=None)
        mock_pipe.invalidate = MagicMock(return_value=True)
        mock_get = MagicMock(return_value=mock_pipe)
        monkeypatch.setattr(
            _fp_module, "get_feature_pipeline", mock_get,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="NO_WIRE_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            wire_ml=False,
            fire_alert=False,
        )
        # Give any (unexpected) scheduled task a chance to run so the
        # assertion is meaningful rather than a race-condition pass.
        await asyncio.sleep(0.01)

        mock_pipe.invalidate.assert_not_called()
        mock_get.assert_not_called()

    async def test_ml_wiring_skipped_for_non_resolution_events(
        self, ingester, monkeypatch,
    ):
        """``wire_ml=True`` on a non-``MARKET_RESOLVED`` event is a
        no-op (the ML wiring fires only on resolution)."""
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="NON_RES_WIRE_TEST",
            wire_ml=True,  # should be ignored for non-resolution events
            fire_alert=False,
        )
        await asyncio.sleep(0.01)

        mock_engine.record_outcome.assert_not_called()

    async def test_ml_wiring_skipped_when_outcome_unresolvable(
        self, ingester, monkeypatch,
    ):
        """``wire_ml=True`` on a ``MARKET_RESOLVED`` event whose
        payload has no parseable ``outcomePrices`` does NOT call
        ``label_backfill.record_outcome`` (the ingester can't determine
        the YES/NO outcome, so the ML wiring is deferred until the
        daily backfill loop can derive it)."""
        from core import label_backfill as _lb_module
        original_engine = _lb_module.label_backfill_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(
            _lb_module, "label_backfill_engine", mock_engine,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="UNRESOLVABLE_WIRE_TEST",
            payload={},  # no outcomePrices → resolved_yes=None
            wire_ml=True,
            fire_alert=False,
        )
        await asyncio.sleep(0.01)

        mock_engine.record_outcome.assert_not_called()

    async def test_feature_pipeline_invalidate_called(
        self, ingester, monkeypatch,
    ):
        """The async ML wiring calls
        ``feature_pipeline.invalidate(token_id)`` (W33-4 additive
        method on ``FeaturePipeline``) so the next prediction uses
        fresh data rather than the resolved market's stale price
        history."""
        # Mock the ``get_feature_pipeline`` lazy resolver so the
        # ingester's ``_wire_ml_label_generation`` picks up our spy.
        from ingestion import feature_pipeline as _fp_module
        mock_pipe = MagicMock()
        mock_pipe.get_features = AsyncMock(return_value=None)
        mock_pipe.invalidate = MagicMock(return_value=True)
        mock_get = MagicMock(return_value=mock_pipe)
        monkeypatch.setattr(
            _fp_module, "get_feature_pipeline", mock_get,
        )

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="INVALIDATE_TEST",
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
            wire_ml=True,
            fire_alert=False,
        )
        # Let the scheduled async task complete.
        await asyncio.sleep(0.05)

        mock_pipe.invalidate.assert_called_once_with("INVALIDATE_TEST")


# ═══════════════════════════════════════════════════════════════════════════
# 5. API route — GET /api/ingestion/market-events
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketEventsAPIRoute:
    """``GET /api/ingestion/market-events`` — read-only event timeline."""

    def test_returns_200_with_empty_state(self, client, auth_headers):
        """``GET /api/ingestion/market-events`` returns 200 with the
        zero-state (empty ``events`` list + zeroed ``ingester_stats``)
        when no events have been recorded — no fabrication, no 500."""
        response = client.get(
            "/api/ingestion/market-events", headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"GET /api/ingestion/market-events must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        body = response.json()
        assert "events" in body
        assert body["count"] == len(body["events"])
        assert "ingester_stats" in body
        assert "generated_at" in body

    def test_returns_seeded_event(self, client, auth_headers):
        """A seeded event appears in the API response (the route
        reads from the module-level singleton, so a ``record_event``
        call on the singleton is visible to the route)."""
        from ingestion.market_events import market_event_ingester

        market_event_ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="API_SEED_TEST",
            slug="api-seed",
            question="Does the API surface seeded events?",
            fire_alert=False,
        )
        try:
            response = client.get(
                "/api/ingestion/market-events",
                headers=auth_headers,
                params={"token_id": "API_SEED_TEST"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["count"] >= 1
            seeded = next(
                (e for e in body["events"] if e["token_id"] == "API_SEED_TEST"),
                None,
            )
            assert seeded is not None, (
                "seeded MARKET_CREATED event for API_SEED_TEST must appear "
                "in the route response"
            )
            assert seeded["event_type"] == "MARKET_CREATED"
            assert seeded["slug"] == "api-seed"
        finally:
            # Best-effort cleanup so the seeded event doesn't leak
            # into other tests in this session (the autouse fixture
            # also truncates before the next test, so this is belt-and-
            # braces).
            pass

    def test_event_type_filter(self, client, auth_headers):
        """``?event_type=MARKET_CREATED`` returns only creation events."""
        from ingestion.market_events import market_event_ingester

        market_event_ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="API_FILTER_CREATE",
            fire_alert=False,
        )
        market_event_ingester.record_event(
            event_type="MARKET_CLOSED",
            token_id="API_FILTER_CLOSE",
            fire_alert=False,
        )
        response = client.get(
            "/api/ingestion/market-events",
            headers=auth_headers,
            params={"event_type": "MARKET_CREATED"},
        )
        assert response.status_code == 200
        body = response.json()
        for evt in body["events"]:
            assert evt["event_type"] == "MARKET_CREATED", (
                f"event_type filter must exclude non-MARKET_CREATED events; "
                f"got {evt['event_type']}"
            )

    def test_invalid_event_type_returns_422(self, client, auth_headers):
        """An invalid ``event_type`` query param returns 422 (the
        route validates against the canonical ``EVENT_TYPES``
        vocabulary so a typo doesn't silently return an empty list)."""
        response = client.get(
            "/api/ingestion/market-events",
            headers=auth_headers,
            params={"event_type": "NOT_A_REAL_EVENT_TYPE"},
        )
        assert response.status_code == 422, (
            f"invalid event_type must return 422; got {response.status_code}"
        )

    def test_limit_query_param_enforced(self, client, auth_headers):
        """``?limit=1`` caps the result count at 1 (the route's
        ``Query(le=1000)`` validator enforces the hard ceiling)."""
        from ingestion.market_events import market_event_ingester

        for i in range(3):
            market_event_ingester.record_event(
                event_type="MARKET_CREATED",
                token_id=f"API_LIMIT_{i}",
                fire_alert=False,
            )
        response = client.get(
            "/api/ingestion/market-events",
            headers=auth_headers,
            params={"limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["events"]) <= 1

    def test_limit_above_ceiling_returns_422(self, client, auth_headers):
        """``?limit=10000`` exceeds the 1000 hard ceiling → 422 (the
        ``Query(le=1000)`` validator rejects the request before it
        reaches the route body)."""
        response = client.get(
            "/api/ingestion/market-events",
            headers=auth_headers,
            params={"limit": 10000},
        )
        assert response.status_code == 422

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


# ═══════════════════════════════════════════════════════════════════════════
# 6. Misc — stats / alerting / liquidity change
# ═══════════════════════════════════════════════════════════════════════════


class TestStatsAndAlerting:
    """Stats accounting + alerting on high-signal events."""

    def test_stats_zero_state(self, ingester):
        """``stats`` returns the zero-state when no events have been
        recorded (mirrors the W17-4 "honest health" convention — no
        fabrication)."""
        stats = ingester.stats
        assert stats["running"] is False
        assert stats["event_count"] == 0
        assert stats["alert_count"] == 0
        assert stats["duplicate_ignored_count"] == 0
        assert stats["tracked_markets"] == 0
        assert stats["last_poll_at"] == 0.0
        assert stats["last_poll_delta"] == 0

    def test_alert_fired_on_market_resolved(self, ingester, monkeypatch):
        """``MARKET_RESOLVED`` is in ``ALERT_EVENT_TYPES`` so the
        ingester dispatches ``alert_engine.record_alert`` on resolution.

        W34-3 — the ingester uses the primitive-field ``record_alert``
        convenience wrapper (rather than constructing an ``Alert``
        dataclass + calling ``fire_alert`` directly) so the alert
        carries the canonical ``market`` category + ``info`` severity
        (MARKET_RESOLVED is an expected lifecycle transition — no
        operator action required). We spy on ``record_alert`` to
        verify the primitive-field arguments.
        """
        from core import alerting as _al_module
        original_engine = _al_module.alert_engine
        mock_engine = MagicMock(wraps=original_engine)
        monkeypatch.setattr(_al_module, "alert_engine", mock_engine)

        ingester.record_event(
            event_type="MARKET_RESOLVED",
            token_id="ALERT_TEST",
            fire_alert=True,  # default
            wire_ml=False,  # don't trigger the ML wiring here
            payload={"resolved_yes": True, "outcomePrices": ["1", "0"]},
        )
        mock_engine.record_alert.assert_called_once()
        call_kwargs = mock_engine.record_alert.call_args.kwargs
        assert call_kwargs["category"] == "market"
        assert call_kwargs["severity"] == "info"  # MARKET_RESOLVED → info
        assert call_kwargs["name"] == "market_resolved"
        # Metadata carries the event_id + token_id so the dashboard can
        # cross-link the alert card with the market_events row.
        metadata = call_kwargs.get("metadata", {}) or {}
        assert metadata.get("token_id") == "ALERT_TEST"
        assert metadata.get("event_type") == "MARKET_RESOLVED"
        assert "event_id" in metadata

    def test_no_alert_for_market_created(self, ingester, monkeypatch):
        """``MARKET_CREATED`` is NOT in ``ALERT_EVENT_TYPES`` (too
        noisy for an operator alert — Polymarket lists 100+ markets a
        day), so the ingester does NOT call ``fire_alert`` for it."""
        from core import alerting as _al_module
        mock_engine = MagicMock(wraps=_al_module.alert_engine)
        monkeypatch.setattr(_al_module, "alert_engine", mock_engine)

        ingester.record_event(
            event_type="MARKET_CREATED",
            token_id="NO_ALERT_TEST",
            fire_alert=True,  # default, but event_type is not in ALERT_EVENT_TYPES
        )
        mock_engine.fire_alert.assert_not_called()

    async def test_liquidity_change_detected(
        self, ingester, mock_gamma,
    ):
        """A market whose ``liquidity`` changes by ≥ 20% between polls
        fires a ``MARKET_LIQUIDITY_CHANGED`` event."""
        # First poll — emit MARKET_CREATED + cache the liquidity.
        mock_gamma.get_markets.return_value = [
            _make_active_market(token_id="LIQ_YES", liquidity=10000.0),
        ]
        mock_gamma.get_resolved_markets.return_value = []
        ingester._gamma_client = mock_gamma
        await ingester.detect_events()
        # Sanity: one event (MARKET_CREATED).
        assert len(ingester.get_events(token_id="LIQ_YES")) == 1

        # Second poll — liquidity drops 50% (well above the 20% threshold).
        mock_gamma.get_markets.return_value = [
            _make_active_market(token_id="LIQ_YES", liquidity=5000.0),
        ]
        await ingester.detect_events()

        liq_events = ingester.get_events(
            token_id="LIQ_YES", event_type="MARKET_LIQUIDITY_CHANGED",
        )
        assert len(liq_events) == 1, (
            "a 50% liquidity drop must trigger a "
            "MARKET_LIQUIDITY_CHANGED event"
        )
        assert liq_events[0]["payload"]["prior_liquidity"] == 10000.0
        assert liq_events[0]["payload"]["new_liquidity"] == 5000.0
        assert liq_events[0]["payload"]["delta_pct"] == 0.5
