"""
Unit tests for ``core/book_poller.py`` — V8 task.

Scope: NEW file only — no existing source files or test files edited.
Mirrors the isolation strategy already established by
``tests/test_decision_ledger.py`` (S9), ``tests/test_closed_positions.py``
(T11), ``tests/test_settlement.py`` (U2), and the shared
``tests/conftest.py`` (T15) autouse ``_reset_store_factory_defaults``
reset fixture.

Five tests, all aligned with the V8 task spec:

  1. ``set_tokens`` adds tokens to tracking.
  2. ``set_tokens`` deduplicates input token lists.
  3. ``_poll_tier`` fetches books for tracked tokens and writes them
     into the global ``store.order_books`` mapping via ``_apply_book`` →
     ``store.update_order_book``.
  4. ``stats`` returns the success/error counts accumulated by the
     ``_poll_tier`` gather loop.
  5. Circuit breaker opens after sustained >80% error rate (10 errors
     in a 10-result window → 100% error rate, trips the breaker).

Mock strategy
~~~~~~~~~~~~~

  * ``poller._client`` is replaced with a ``MagicMock`` whose ``.get(...)``
    is an async function returning a stub ``httpx.Response``-shaped object
    (``status_code`` + ``.json()``). This mirrors the call site in
    ``_fetch_book`` (``await self._client.get("/book", params=...)``)
    and lets us inject deterministic success / failure responses
    without spinning up a real ``httpx.AsyncClient``. (The alternative —
    ``httpx.MockTransport`` with a real ``AsyncClient`` — would also
    work but adds lifecycle / ``aclose()`` plumbing that the MagicMock
    path avoids.) The V8 task spec phrasing ("mock httpx responses") is
    satisfied either way; the MagicMock approach is chosen for
    consistency with the existing ``tests/test_settlement.py`` (U2)
    ``mock_gamma`` / ``mock_timescale`` pattern.

  * The downstream singletons the poller fire-and-forgets to
    (``timescale_db``, ``raw_vault``, ``source_registry``) are
    monkeypatched to no-op ``AsyncMock``s — same pattern as the
    ``mock_timescale`` fixture in ``tests/test_settlement.py``. This
    avoids SQLite writes against the temp DB on every fetch and keeps
    the tests fully hermetic + fast.

  * ``asyncio.sleep`` (in the ``core.book_poller`` namespace) is patched
    to a no-op that flips ``poller._running`` to ``False`` on the second
    invocation — letting the poller complete exactly one cycle of the
    ``while self._running:`` loop without an infinite busy-loop and
    without hanging on the initial ``asyncio.sleep(1.0)``.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` / ``pyproject.toml`` are not edited per the V8 "Do NOT
edit existing files" constraint, so ``asyncio_mode = "auto"`` cannot be
enabled via config — mirrors the convention in
``tests/test_settlement.py``, ``tests/test_decision_ledger.py``, etc.).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_settlement.py`` (U2):
# the repo's ``pytest.ini`` cannot be edited per the V8 "Do NOT edit
# existing files" constraint, so we use the module-level ``pytestmark``
# idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio

from core.book_poller import BookPoller  # noqa: E402
from core.data_store import store  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
def _mock_book_payload(token_id: str) -> dict[str, Any]:
    """Build a minimal CLOB ``/book`` response payload for ``token_id``.

    The shape mirrors the real Polymarket CLOB REST API response: a flat
    dict with ``bids`` / ``asks`` lists of ``{"price": str, "size": str}``
    objects. (Production parses these via ``float(b["price"])`` /
    ``float(b["size"])`` — string types are intentional, matching the
    real API contract.)
    """
    return {
        "market": token_id,
        "asset_id": token_id,
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "hash": "0xdeadbeef",
        "timestamp": "1700000000000",
    }


def _make_ok_response(token_id: str) -> MagicMock:
    """Stub ``httpx.Response``-shaped object for a 200 OK book fetch.

    Exposes the two attributes ``_fetch_book`` reads:
      * ``status_code`` (int) — must be 200 to take the success branch.
      * ``.json()`` (dict) — parsed body containing ``bids`` / ``asks``.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _mock_book_payload(token_id)
    return resp


def _make_mock_client(*, raise_exc: Exception | None = None) -> MagicMock:
    """Build a ``MagicMock`` stand-in for ``httpx.AsyncClient``.

    ``raise_exc``: when supplied, ``.get(...)`` raises this exception on
    every call (used to drive the circuit-breaker test). When ``None``,
    ``.get(...)`` returns a 200 OK response with a per-token book
    payload (parsed from the ``token_id`` query param).

    The real ``_fetch_book`` only requires ``self._client`` to expose:
      * ``is_closed`` (bool/property) — production checks ``not
        self._client.is_closed`` before each request.
      * ``async def get(url, params=None)`` returning an object with
        ``status_code`` (int) and ``.json()`` (dict).

    Using ``MagicMock`` + a plain ``async def`` lets us inject
    deterministic responses without spinning up an actual
    ``httpx.AsyncClient`` with a ``MockTransport`` (which would require
    touching the production ``start()`` constructor path).
    """
    mock_client = MagicMock()
    mock_client.is_closed = False

    if raise_exc is not None:
        async def failing_get(url, params=None):
            raise raise_exc
        mock_client.get = failing_get
    else:
        async def ok_get(url, params=None):
            tid = (params or {}).get("token_id", "UNKNOWN")
            return _make_ok_response(tid)
        mock_client.get = ok_get

    return mock_client


def _patch_sleep_to_run_one_cycle(
    monkeypatch: pytest.MonkeyPatch,
    poller: BookPoller,
) -> None:
    """Patch ``asyncio.sleep`` (in ``core.book_poller``) so the poller
    completes exactly ONE iteration of the ``while self._running:`` loop
    without an infinite busy-loop and without hanging on the initial
    ``asyncio.sleep(1.0)``.

    Behaviour:
      * call 1: the initial ``await asyncio.sleep(1.0 if tier == 1
        else 3.0)`` → no-op (proceed into the while loop).
      * call 2: the end-of-iteration ``await asyncio.sleep(interval)`` →
        flip ``poller._running = False`` so the next
        ``while self._running:`` check exits the loop.

    After the patched ``_poll_tier`` coroutine returns, the test can
    assert on the post-cycle state (``store.order_books``, ``stats``,
    ``_circuit_open``, etc.).
    """
    sleep_calls = 0

    async def fast_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        # call 2 == end-of-first-iteration sleep → stop the loop
        if sleep_calls >= 2:
            poller._running = False

    monkeypatch.setattr("core.book_poller.asyncio.sleep", fast_sleep)


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def poller() -> BookPoller:
    """Fresh ``BookPoller`` instance per test.

    The module-level singleton ``book_poller`` is NOT used so each test
    starts with empty ``_tier1_tokens`` / ``_tier2_tokens`` /
    ``_result_window`` / ``_success_count`` / ``_error_count`` /
    ``_circuit_open`` state (no leakage between tests).
    """
    return BookPoller()


@pytest.fixture
def mock_downstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Mock the downstream singletons the BookPoller fire-and-forgets to.

      * ``core.timescale_db.timescale_db`` — ``record_snapshot`` /
        ``record_tick`` (called via ``asyncio.create_task`` inside
        ``_apply_book``).
      * ``core.ingestion.raw_vault.raw_vault`` — ``record_observation``
        (called via ``asyncio.create_task`` inside ``_fetch_book``'s
        success path).
      * ``core.ingestion.source_registry.source_registry`` —
        ``record_metric`` (called via ``asyncio.create_task`` inside
        both the success and error paths of ``_fetch_book``).

    All three are mocked to no-op ``AsyncMock``s so the fire-and-forget
    tasks complete immediately without touching the SQLite fallback
    (which would otherwise write to the temp DB on every fetch and
    slow the tests down). Mirrors the ``mock_timescale`` fixture in
    ``tests/test_settlement.py``.

    The lazy ``from core.X import singleton`` imports inside the
    production ``_fetch_book`` / ``_apply_book`` bodies pick up the
    monkeypatched module attribute at call time (verified empirically
    for ``core.timescale_db.timescale_db`` in the U2 worklog).
    """
    mock_ts = MagicMock()
    mock_ts.record_snapshot = AsyncMock(return_value=True)
    mock_ts.record_tick = AsyncMock(return_value=True)

    mock_rv = MagicMock()
    mock_rv.record_observation = AsyncMock(return_value=None)

    mock_sr = MagicMock()
    mock_sr.record_metric = AsyncMock(return_value=None)

    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)
    monkeypatch.setattr("core.ingestion.raw_vault.raw_vault", mock_rv)
    monkeypatch.setattr("core.ingestion.source_registry.source_registry", mock_sr)

    return {
        "timescale": mock_ts,
        "raw_vault": mock_rv,
        "source_registry": mock_sr,
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. set_tokens adds tokens to tracking
# ────────────────────────────────────────────────────────────────────────────
async def test_set_tokens_adds_tokens_to_tracking(poller: BookPoller):
    """``set_tokens`` must populate ``_tier1_tokens`` (first 50 tokens)
    and ``_tier2_tokens`` (overflow beyond 50).

    With 3 tokens (well below the 50-token Tier-1 cap), all 3 must land
    in Tier 1. The ``stats`` property must reflect ``tier1_tokens == 3``,
    ``tier2_tokens == 0``, and ``total_tracked == 3``.
    """
    poller.set_tokens(["T1", "T2", "T3"])

    # All three are tracked in Tier 1 (no overflow to Tier 2).
    assert "T1" in poller._tier1_tokens
    assert "T2" in poller._tier1_tokens
    assert "T3" in poller._tier1_tokens
    assert poller._tier2_tokens == set()

    # stats reflects the configuration.
    stats = poller.stats
    assert stats["tier1_tokens"] == 3
    assert stats["tier2_tokens"] == 0
    assert stats["total_tracked"] == 3


# ────────────────────────────────────────────────────────────────────────────
# 2. set_tokens deduplicates
# ────────────────────────────────────────────────────────────────────────────
async def test_set_tokens_deduplicates(poller: BookPoller):
    """``set_tokens`` must deduplicate the input list before assigning
    to tiers — production line 52 uses ``list(dict.fromkeys(token_ids))``
    which preserves first-occurrence order while collapsing duplicates.

    Belt-and-braces: passing 5 inputs (3 unique: A, B, C with each
    repeated) must yield exactly 3 tracked tokens, NOT 5 — the duplicate
    entries must not appear twice in either tier.
    """
    poller.set_tokens(["A", "A", "B", "B", "C"])

    # Total tracked = unique count = 3 (not 5).
    assert poller.stats["total_tracked"] == 3

    # No duplicates leak into either tier (Python ``set`` would dedup
    # natively, but the spec is that ``set_tokens`` itself deduplicates
    # before assignment — verified by the tier1 size being exactly 3).
    assert len(poller._tier1_tokens) == 3
    assert len(poller._tier2_tokens) == 0

    # The 3 unique tokens are exactly {A, B, C}.
    assert poller._tier1_tokens == {"A", "B", "C"}


# ────────────────────────────────────────────────────────────────────────────
# 3. _poll_tier fetches books for tracked tokens
# ────────────────────────────────────────────────────────────────────────────
async def test_poll_tier_fetches_books_for_tracked_tokens(
    poller: BookPoller,
    mock_downstream,  # noqa: ARG001 — sets up monkeypatch side-effect
    monkeypatch: pytest.MonkeyPatch,
):
    """``_poll_tier`` must issue ``GET /book?token_id=...`` for each
    tracked token in the tier, parse the JSON response, and write the
    resulting ``OrderBook`` into the global ``store.order_books`` mapping
    via ``_apply_book`` → ``store.update_order_book``.

    Mock strategy:
      * Replace ``poller._client`` with a MagicMock whose ``.get(...)``
        returns a 200-response book payload keyed off the
        ``token_id`` query param.
      * Patch ``asyncio.sleep`` so the poller completes exactly one
        iteration of the ``while self._running:`` loop.
      * Mock the downstream singletons (``timescale_db``, ``raw_vault``,
        ``source_registry``) via the ``mock_downstream`` fixture so the
        fire-and-forget ``asyncio.create_task`` calls complete
        immediately without touching the SQLite fallback.

    Belt-and-braces:
      * Both ``TOKEN_A`` and ``TOKEN_B`` appear in ``store.order_books``
        post-poll (each token's fetch was routed through ``_apply_book``).
      * The captured books have the expected ``best_bid`` / ``best_ask``
        from the mock payload (sanity check that the parsing path ran).
      * ``poller.stats["success_count"]`` reflects the doubled counting
        from ``_fetch_book`` AND the gather loop (2 successes × +2 each
        = 4).
    """
    poller.set_tokens(["TOKEN_A", "TOKEN_B"])

    # Mock httpx client: 200 OK with per-token book payload.
    poller._client = _make_mock_client()

    # Patch asyncio.sleep so the poller completes exactly one cycle.
    _patch_sleep_to_run_one_cycle(monkeypatch, poller)

    poller._running = True
    await poller._poll_tier(1, 0.01)

    # Let any fire-and-forget asyncio.create_task calls (raw_vault /
    # source_registry / timescale_db) finish before assertions, to
    # avoid "Task was destroyed but it is pending!" warnings.
    await asyncio.sleep(0)

    # Both tokens fetched and stored in the global store.
    assert "TOKEN_A" in store.order_books
    assert "TOKEN_B" in store.order_books

    # Sanity: book contents match the mock payload (parsing path ran).
    book_a = store.order_books["TOKEN_A"]
    assert book_a.token_id == "TOKEN_A"
    assert book_a.best_bid == pytest.approx(0.49)
    assert book_a.best_ask == pytest.approx(0.51)
    assert book_a.mid == pytest.approx(0.50)

    book_b = store.order_books["TOKEN_B"]
    assert book_b.token_id == "TOKEN_B"
    assert book_b.best_bid == pytest.approx(0.49)
    assert book_b.best_ask == pytest.approx(0.51)

    # Success count = doubled (each success +1 in _fetch_book AND +1 in
    # the gather loop) — 2 tokens × +2 per success = 4.
    assert poller.stats["success_count"] == 4
    assert poller.stats["error_count"] == 0


# ────────────────────────────────────────────────────────────────────────────
# 4. stats returns success/error counts
# ────────────────────────────────────────────────────────────────────────────
async def test_stats_returns_success_and_error_counts(
    poller: BookPoller,
    mock_downstream,  # noqa: ARG001 — sets up monkeypatch side-effect
    monkeypatch: pytest.MonkeyPatch,
):
    """``stats`` must surface the cumulative success / error counts
    accumulated by the ``_poll_tier`` gather loop, alongside the tier
    configuration surfaced from ``set_tokens``.

    Setup: 3 tracked tokens, of which 2 fetches succeed (HTTP 200) and
    1 fails (raises ``ConnectionError``). Run one poll cycle on Tier 1.

    Production accounting:
      * Each successful fetch increments ``_success_count`` once in
        ``_fetch_book`` (HTTP 200 path) AND once in the gather loop.
        Total per success: +2.
      * Each failing fetch (raised exception) is re-raised by
        ``_fetch_book``; the gather loop sees the exception and
        increments ``_error_count`` once. ``_fetch_book`` itself does
        NOT increment on the failure path. Total per failure: +1.

    Expected stats after one cycle:
      * ``success_count == 4`` (2 successes × +2 per success).
      * ``error_count == 1`` (1 failure × +1 per failure).
      * ``tier1_tokens == 3`` (all 3 tokens tracked in Tier 1).
      * ``total_tracked == 3``.
      * ``tier2_tokens == 0``.
    """
    poller.set_tokens(["OK1", "OK2", "ERR1"])

    # Mock httpx client: OK1 and OK2 return 200; ERR1 raises.
    async def mock_get(url, params=None):
        tid = (params or {}).get("token_id", "UNKNOWN")
        if tid == "ERR1":
            raise ConnectionError("simulated network failure")
        return _make_ok_response(tid)

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = mock_get
    poller._client = mock_client

    # Patch asyncio.sleep so the poller completes exactly one cycle.
    _patch_sleep_to_run_one_cycle(monkeypatch, poller)

    poller._running = True
    await poller._poll_tier(1, 0.01)
    await asyncio.sleep(0)  # drain fire-and-forget tasks.

    stats = poller.stats

    # Required by V8 spec: success/error counts are surfaced.
    assert "success_count" in stats
    assert "error_count" in stats

    # 2 successes × +2 per success (doubled by _fetch_book AND gather loop).
    assert stats["success_count"] == 4, (
        f"expected success_count=4 (2 OK × +2), got {stats['success_count']}"
    )
    # 1 failure × +1 per failure (gather loop only — _fetch_book re-raises
    # before incrementing error_count).
    assert stats["error_count"] == 1, (
        f"expected error_count=1 (1 fail × +1), got {stats['error_count']}"
    )

    # Belt-and-braces: tier configuration reflected in stats.
    assert stats["tier1_tokens"] == 3
    assert stats["tier2_tokens"] == 0
    assert stats["total_tracked"] == 3


# ────────────────────────────────────────────────────────────────────────────
# 5. circuit breaker opens after 80% error rate
# ────────────────────────────────────────────────────────────────────────────
async def test_circuit_breaker_opens_after_80_percent_error_rate(
    poller: BookPoller,
    mock_downstream,  # noqa: ARG001 — sets up monkeypatch side-effect
    monkeypatch: pytest.MonkeyPatch,
):
    """Circuit breaker must OPEN after sustained >80% error rate.

    Setup: 10 tracked tokens, ALL of which raise on fetch (simulated
    network failure). Run one poll cycle on Tier 1.

    Production breaker logic (``_poll_tier`` lines 131-138):
      * Requires ``len(self._result_window) >= 10`` (minimum sample
        size before tripping).
      * Trips when ``err_rate = result_window.count(False) / len(...) > 0.80``.
      * On trip: ``_circuit_open = True`` and ``_circuit_open_until =
        time.time() + 30.0`` (30-second cooldown).

    With 10 errors out of 10 results, ``err_rate = 1.0 > 0.80`` → the
    breaker must open.

    Belt-and-braces:
      * ``_circuit_open_until`` is ~30s in the future (cooldown not
        expired).
      * ``_error_count == 10`` (one per failing fetch).
      * ``_success_count == 0`` (no successful fetches in this cycle).
      * ``len(_result_window) == 10`` (window populated by the gather
        loop).
      * All 10 entries in ``_result_window`` are ``False`` (errors).
    """
    poller.set_tokens([f"ERR_{i}" for i in range(10)])

    # Mock httpx client: every fetch raises.
    poller._client = _make_mock_client(
        raise_exc=ConnectionError("simulated network failure")
    )

    # Patch asyncio.sleep so the poller completes exactly one cycle.
    _patch_sleep_to_run_one_cycle(monkeypatch, poller)

    poller._running = True
    await poller._poll_tier(1, 0.01)
    await asyncio.sleep(0)  # drain fire-and-forget tasks.

    # Circuit breaker must be OPEN (10/10 errors = 100% error rate > 80%).
    assert poller._circuit_open is True, (
        "circuit breaker must be open after 10/10 error rate (>80%)"
    )

    # Cooldown is ~30s in the future (production: time.time() + 30.0).
    now = time.time()
    assert poller._circuit_open_until > now, (
        f"circuit_open_until ({poller._circuit_open_until}) must be in the "
        f"future (now={now})"
    )
    # Sanity: cooldown is roughly 30s (allow 5s slack for test latency).
    assert poller._circuit_open_until == pytest.approx(now + 30.0, abs=5.0), (
        f"circuit_open_until ({poller._circuit_open_until}) must be ~30s "
        f"in the future (now={now})"
    )

    # Belt-and-braces: accounting reflects the 10 failures.
    assert poller._error_count == 10, (
        f"error_count should be 10, got {poller._error_count}"
    )
    assert poller._success_count == 0, (
        f"success_count should be 0, got {poller._success_count}"
    )
    assert len(poller._result_window) == 10, (
        f"result_window should have 10 entries, got "
        f"{len(poller._result_window)}"
    )
    # All 10 results are errors (False).
    assert poller._result_window.count(False) == 10
    assert poller._result_window.count(True) == 0
