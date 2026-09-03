"""
Unit tests for ``core/label_backfill.py`` — W5 task.

Scope: NEW file only — no existing source files or test files edited.
Mirrors the isolation strategy already established by
``tests/test_settlement.py`` (U2), ``tests/test_decision_ledger.py`` (S9),
``tests/test_closed_positions.py`` (T11), and the shared
``tests/conftest.py`` (T15) autouse ``_reset_store_factory_defaults`` reset
fixture.

Seven tests, all aligned with the W5 task spec:

  1. ``_parse_resolved_yes(["1","0"])`` returns ``True``  (winner outcome).
  2. ``_parse_resolved_yes(["0","1"])`` returns ``False`` (loser outcome).
  3. ``_parse_resolved_yes(None)``     returns ``None``   (unresolvable).
  4. ``_build_synthetic_book`` returns an ``OrderBook`` with a valid mid.
  5. ``_build_synthetic_book`` returns ``None`` for ``None`` outcome_prices.
  6. ``_process_market`` returns ``1`` (added count) on successful label
     write.
  7. ``_process_market`` returns ``0`` (added count) for missing token_ids.

Test-local ``_parse_resolved_yes`` helper
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The production ``core/label_backfill.py`` exposes the outcome-parsing
logic as ``LabelBackfillEngine._resolve_outcome(market: dict) -> bool |
None`` (production lines 312-339) — a ``@staticmethod`` that reads
``market["outcomePrices"]``, JSON-decodes string inputs via
``json.loads``, applies the ``len(prices) >= 2`` guard and the
``float(prices[0]) >= 0.9`` winner threshold, and returns ``None`` for
any unresolvable input.

Unlike ``core/settlement.py`` (where the U2 task had to re-implement the
parsing logic in a test-local helper because production inlined it),
here the production parser is ALREADY a standalone static method. The
W5 task spec phrases the input as ``outcomePrices`` (the raw list /
JSON-string / ``None`` value the Gamma API returns), so this test module
defines a thin ``_parse_resolved_yes(outcome_prices)`` adapter that wraps
the raw value in a one-key ``market`` dict and delegates to the REAL
production ``_resolve_outcome`` static method. This exercises the actual
production code path (no test-local copy of the parsing logic to drift)
while matching the W5 spec's call signature exactly.

There is NO spec/code divergence for label_backfill: the production
``_resolve_outcome`` already returns ``None`` for the ``None`` case
(production line 320: ``if not outcome_prices: return None``), so the
W5 spec and production code agree on all three branches.

Mock strategy (per W5 task spec — "mocked gamma_client and timescale_db")
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * ``mock_gamma``    — a ``MagicMock(spec=GammaClient)`` whose
                        ``extract_token_ids`` returns a controlled token
                        list, monkey-patched onto
                        ``core.label_backfill.gamma_client``. The
                        production ``_process_market`` calls
                        ``gamma_client.extract_token_ids(market)`` at
                        line 218 to short-circuit on missing tokens, so
                        intercepting this call is load-bearing for tests
                        6-7.
  * ``mock_timescale``— a ``MagicMock`` placed on
                        ``core.label_backfill.timescale_db`` so the
                        idempotency gate (``has_labeled_sample``, sync)
                        and the label write (``record_feature_vector``,
                        async) are fully controlled. ``record_feature_vector``
                        is an ``AsyncMock`` returning ``True`` (successful
                        write). This keeps tests 6-7 deterministic and
                        free of SQLite I/O against the temp DB — the
                        production ``_persist_token_label`` flow exercises
                        the REAL ``_build_synthetic_book`` + the REAL
                        extract_fn + the REAL pad/trim/normalise logic,
                        with only the persistence layer stubbed.

  * ``engine`` — a fresh ``LabelBackfillEngine()`` (NOT the module-level
    singleton ``label_backfill_engine``, so its lifetime telemetry
    counters ``_total_added`` / ``_cycles_completed`` don't leak between
    tests). The production code references module-level names
    ``gamma_client`` and ``timescale_db`` (imported via
    ``from core.gamma_client import gamma_client`` and
    ``from core.timescale_db import timescale_db``); monkey-patching the
    ``core.label_backfill`` module attribute replaces what those bound
    names point at, so ``gamma_client.extract_token_ids(mkt)`` and
    ``timescale_db.has_labeled_sample(...)`` both resolve against the
    test mocks.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` / ``pyproject.toml`` are not edited per the W5 "Do NOT
edit existing files" constraint, so ``asyncio_mode = "auto"`` cannot be
enabled via config — mirrors the convention in
``tests/test_settlement.py``, ``tests/test_decision_ledger.py``,
``tests/test_closed_positions.py``, etc.).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_settlement.py`` (U2) and
# ``tests/test_decision_ledger.py`` (S9): the repo's ``pytest.ini`` cannot
# be edited per the W5 "Do NOT edit existing files" constraint, so we use
# the module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio

from core.data_store import OrderBook  # noqa: E402
from core.gamma_client import GammaClient  # noqa: E402
from core.label_backfill import LabelBackfillEngine  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Test-local helper: thin adapter for LabelBackfillEngine._resolve_outcome
# ────────────────────────────────────────────────────────────────────────────
def _parse_resolved_yes(outcome_prices: Any) -> bool | None:
    """Spec-shaped adapter for ``LabelBackfillEngine._resolve_outcome``.

    The W5 task spec phrases the input as the raw ``outcomePrices`` value
    (a list / JSON-string / ``None``), but the production static method
    takes a full ``market`` dict and reads ``market["outcomePrices"]``.
    This helper wraps the raw value in a one-key market dict and
    delegates to the REAL production parser — so tests 1-3 exercise the
    production code path (no test-local re-implementation that could
    drift if the threshold or guard logic ever changes).

    Behaviour contract (mirrors production ``_resolve_outcome`` exactly —
    there is NO spec/code divergence here, unlike ``core/settlement.py``
    where the U2 task had to re-implement the inline parsing logic):

      * ``None`` / empty / malformed input  → ``None``
        (production line 320: ``if not outcome_prices: return None``).
      * ``["1", "0"]``  → ``True``  (winner: ``float(prices[0]) >= 0.9``).
      * ``["0", "1"]``  → ``False`` (loser:  ``float(prices[0])  < 0.9``).
      * JSON-string inputs (e.g. ``'["1","0"]'``) are parsed via
        ``json.loads`` before applying the threshold — mirrors
        production's ``isinstance(outcome_prices, str)`` branch.
      * Lists shorter than 2 elements → ``None`` (production guard
        ``if not prices or len(prices) < 2: return None``).
    """
    return LabelBackfillEngine._resolve_outcome({"outcomePrices": outcome_prices})


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def mock_gamma(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the ``core.label_backfill.gamma_client`` singleton.

    A ``MagicMock(spec=GammaClient)`` so attribute access is restricted to
    the real ``GammaClient``'s public surface (catches typos at test
    time). ``extract_token_ids`` is configured per-test via
    ``mock_gamma.extract_token_ids.return_value`` — the production code
    calls it as ``gamma_client.extract_token_ids(market)`` (positional),
    so a plain MagicMock attribute (no ``spec`` on the method itself)
    works.
    """
    mock = MagicMock(spec=GammaClient)
    monkeypatch.setattr("core.label_backfill.gamma_client", mock)
    return mock


@pytest.fixture
def mock_timescale(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the ``core.label_backfill.timescale_db`` singleton.

    The production ``_persist_token_label`` flow consults two
    ``timescale_db`` surfaces:

      * ``has_labeled_sample(token_id)`` — SYNC idempotency gate
        (production line 265). Returns ``False`` here so the
        "already-labeled → skip" short-circuit never fires in test 6,
        letting the label-write path run end-to-end.
      * ``record_feature_vector(...)`` — ASYNC label write
        (production line 292). Stubs to return ``True`` (successful
        write) so the ``_persist_token_label`` "ok" branch is taken
        (production line 306-307: ``return 1, 0``).

    Both are stubbed here so tests 6-7 don't touch SQLite. The
    ``has_labeled_sample`` mock is a plain ``MagicMock`` attribute
    (sync call → ``return_value``); the ``record_feature_vector`` mock
    is an ``AsyncMock`` (async call → ``return_value`` is awaited).
    """
    mock = MagicMock()
    mock.has_labeled_sample.return_value = False
    mock.record_feature_vector = AsyncMock(return_value=True)
    monkeypatch.setattr("core.label_backfill.timescale_db", mock)
    return mock


@pytest.fixture
def engine() -> LabelBackfillEngine:
    """Fresh ``LabelBackfillEngine`` (NOT the module-level singleton).

    A brand-new instance so its lifetime telemetry counters
    (``_total_added``, ``_cycles_completed``, ``_last_run_at`` …) don't
    leak between tests. The production ``_process_market`` /
    ``_persist_token_label`` methods read ``self``-free module-level
    names (``gamma_client``, ``timescale_db``) — those are intercepted via
    the ``mock_gamma`` / ``mock_timescale`` fixtures, so the engine
    instance itself needs no per-test wiring.
    """
    return LabelBackfillEngine()


def _stub_extract_fn(market: dict, book: OrderBook) -> list[float]:
    """Deterministic 38-dim feature stub for ``_process_market`` tests.

    The production ``_persist_token_label`` calls
    ``extract_fn(market, book)`` and pads / trims the result to
    ``n_features`` (production lines 274-283). Returning a fixed
    38-element list (all 0.5) gives a deterministic feature vector that
    survives the ``np.asarray(features, dtype=np.float32)`` + pad/trim
    pipeline unchanged, and ``features[0] = 0.5`` → ``mid_price = 0.5``
    → ``confidence = abs(0.5 - 0.5) * 2.0 = 0.0`` — all consistent,
    nothing asserted (the feature vector content is not under test in
    W5; the orchestration counts are).
    """
    return [0.5] * 38


# ────────────────────────────────────────────────────────────────────────────
# 1. _parse_resolved_yes(["1","0"]) → True
# ────────────────────────────────────────────────────────────────────────────
async def test_parse_resolved_yes_returns_true_for_winner():
    """``_parse_resolved_yes(["1","0"])`` must return ``True``.

    ``["1","0"]`` is the canonical Polymarket winner payload: outcome
    index 0 (YES) priced at $1.00 (post-resolution, the winning side
    pays $1.00/share), outcome index 1 (NO) priced at $0.00. The
    production parser's threshold is ``float(prices[0]) >= 0.9`` —
    ``1.0 >= 0.9`` is ``True`` → WINNER resolution. The label backfill
    service will write ``outcome_resolved=1`` for the YES token (and
    ``0`` for the NO token, via the ``not bool(resolved_yes)`` flip in
    ``_process_market``).
    """
    assert _parse_resolved_yes(["1", "0"]) is True


# ────────────────────────────────────────────────────────────────────────────
# 2. _parse_resolved_yes(["0","1"]) → False
# ────────────────────────────────────────────────────────────────────────────
async def test_parse_resolved_yes_returns_false_for_loser():
    """``_parse_resolved_yes(["0","1"])`` must return ``False``.

    ``["0","1"]`` is the canonical Polymarket loser payload: outcome
    index 0 (YES) priced at $0.00 (the losing side pays nothing),
    outcome index 1 (NO) priced at $1.00. The production parser's
    threshold is ``float(prices[0]) >= 0.9`` — ``0.0 >= 0.9`` is
    ``False`` → ZERO-payout resolution. The label backfill service will
    write ``outcome_resolved=0`` for the YES token (and ``1`` for the NO
    token, via the ``not bool(resolved_yes)`` flip).
    """
    assert _parse_resolved_yes(["0", "1"]) is False


# ────────────────────────────────────────────────────────────────────────────
# 3. _parse_resolved_yes(None) → None
# ────────────────────────────────────────────────────────────────────────────
async def test_parse_resolved_yes_returns_none_when_outcome_prices_missing():
    """``_parse_resolved_yes(None)`` must return ``None``.

    A market whose ``outcomePrices`` field is missing / null has no
    machine-readable winner; the production parser surfaces this as
    ``None`` (production line 320: ``if not outcome_prices: return
    None``) — the "we don't know" sentinel. The label backfill service's
    ``_process_market`` then short-circuits at production line 226-228
    (``if resolved_yes is None: return 0, 1``) so no label row is
    written for a market that has no resolvable outcome.

    Note: there is NO spec/code divergence here for ``label_backfill``
    (unlike ``core/settlement.py`` whose inline parser resolves
    ``None`` → ``False`` because of a pre-``if`` initialisation). The
    production ``_resolve_outcome`` already returns ``None`` for the
    ``None`` case, so the test asserts both the spec AND the real
    production behaviour.
    """
    assert _parse_resolved_yes(None) is None


# ────────────────────────────────────────────────────────────────────────────
# 4. _build_synthetic_book returns OrderBook with valid mid
# ────────────────────────────────────────────────────────────────────────────
async def test_build_synthetic_book_returns_orderbook_with_valid_mid():
    """``_build_synthetic_book`` must return an ``OrderBook`` with a valid
    mid price for a market with parseable ``outcomePrices``.

    Setup: ``outcomePrices=["0.65","0.35"]`` (YES probability 0.65),
    ``liquidity=50000.0`` (mid-tier → ``spread = 0.010`` per the
    production liquidity-tier ladder), ``volume24hr=25000.0`` (drives
    ``base_size = clip(25000/1000, 50, 500) = 50.0``).

    Expected behaviour:
      * Returns a non-None ``OrderBook`` instance (not ``None``).
      * ``book.token_id`` matches the input ``token_id``.
      * The book has 5 bid levels and 5 ask levels (production
        ``for i in range(5):`` loop).
      * ``best_bid`` and ``best_ask`` are both non-None.
      * ``mid`` (the load-bearing assertion — what
        ``ml.features.extract_features`` consumes) is non-None and
        clipped into ``[0.02, 0.98]`` (production ``np.clip``).
      * ``mid == 0.65`` exactly (``yes_price=0.65`` is already inside
        the clip range, so the clip is a no-op).
      * ``best_bid < best_ask`` (positive spread — the synthetic book
        is internally consistent; no crossed quotes).
    """
    market = {
        "outcomePrices": ["0.65", "0.35"],
        "liquidity": 50000.0,
        "volume24hr": 25000.0,
    }
    book = LabelBackfillEngine._build_synthetic_book(market, "TEST_TOK")

    # ── Load-bearing: a real OrderBook was constructed ──
    assert book is not None, (
        "expected an OrderBook, got None — outcomePrices was parseable "
        "so _build_synthetic_book must not short-circuit"
    )
    assert isinstance(book, OrderBook)
    assert book.token_id == "TEST_TOK"

    # ── 5-level book on each side ──
    assert len(book.bids) == 5
    assert len(book.asks) == 5

    # ── Load-bearing: valid mid price (the feature extract_features consumes) ──
    assert book.best_bid is not None
    assert book.best_ask is not None
    mid = book.mid
    assert mid is not None, (
        "mid must not be None when both best_bid and best_ask are present"
    )
    # mid is clipped into [0.02, 0.98] by production (np.clip line 382).
    assert 0.02 <= mid <= 0.98
    # For outcomePrices[0]=0.65 (inside the clip range), mid == 0.65
    # exactly: best_bid = mid - spread/2, best_ask = mid + spread/2,
    # so mid = (best_bid + best_ask) / 2 == 0.65.
    assert mid == pytest.approx(0.65, abs=1e-6)

    # ── Internal consistency: no crossed quotes ──
    assert book.best_bid < book.best_ask, (
        f"synthetic book has crossed quotes: "
        f"best_bid={book.best_bid} >= best_ask={book.best_ask}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 5. _build_synthetic_book returns None for None outcome_prices
# ────────────────────────────────────────────────────────────────────────────
async def test_build_synthetic_book_returns_none_for_none_outcome_prices():
    """``_build_synthetic_book`` must return ``None`` when the market has
    no parseable ``outcomePrices`` AND no ``lastTradePrice`` fallback.

    Production logic (lines 354-379):
      1. Try ``market.get("outcomePrices")`` → ``None`` here.
      2. Fall back to ``market.get("lastTradePrice")`` → also ``None``
         (not in the market dict).
      3. ``if yes_price is None: return None`` (line 378-379).

    A resolved market with neither ``outcomePrices`` nor
    ``lastTradePrice`` has no usable mid reference, so the synthetic
    book cannot be constructed — ``_persist_token_label`` will then
    short-circuit at production line 270-271 (``if book is None: return
    0, 1``), skipping the label write. This is the correct behaviour:
    we never persist a feature vector for a market we cannot price.
    """
    market = {"outcomePrices": None}
    book = LabelBackfillEngine._build_synthetic_book(market, "TEST_TOK")
    assert book is None, (
        "expected None when outcomePrices is None and no lastTradePrice "
        "fallback is present"
    )


# ────────────────────────────────────────────────────────────────────────────
# 6. _process_market returns 1 on successful label write
# ────────────────────────────────────────────────────────────────────────────
async def test_process_market_returns_one_on_successful_label_write(
    engine: LabelBackfillEngine,
    mock_gamma: MagicMock,
    mock_timescale: MagicMock,  # noqa: ARG001 — referenced for side-effect setup
):
    """``_process_market`` must return ``(1, 0)`` — i.e., one label
    successfully written — when token extraction succeeds, outcome
    resolution is non-None, and the underlying ``_persist_token_label``
    flow completes a successful write.

    Setup:
      * ``mock_gamma.extract_token_ids`` returns ``["YES_TOK"]`` (single
        token — only the YES branch of ``_process_market`` runs, so the
        NO branch is NOT exercised; this keeps the added count at
        exactly 1 rather than 2).
      * Market dict has ``outcomePrices=["1","0"]`` →
        ``_resolve_outcome`` returns ``True`` (winner), so the
        ``if resolved_yes is None: return 0, 1`` short-circuit does NOT
        fire.
      * ``mock_timescale.has_labeled_sample`` returns ``False`` so the
        idempotency gate at production line 265 does NOT skip.
      * ``mock_timescale.record_feature_vector`` is an ``AsyncMock``
        returning ``True`` so the ``if ok: return 1, 0`` branch at
        production line 306-307 is taken (not the ``return 0, 1``
        failure branch).

    Expected:
      * ``_process_market`` returns ``(1, 0)`` — one label added, zero
        skipped.
      * Belt-and-braces: ``record_feature_vector`` was awaited exactly
        once (single-token market → single persist call).
      * Belt-and-braces: the YES label was written with
        ``outcome_resolved=1`` (since ``resolved_yes=True`` for
        ``outcomePrices=["1","0"]``).

    Note: this test lets the REAL ``_persist_token_label`` run (only
    ``gamma_client`` + ``timescale_db`` are mocked), so the full
    orchestration path — token extraction, outcome resolution, book
    construction, feature extraction, pad/trim, label write — is
    exercised end-to-end. This is a more thorough test than mocking
    ``_persist_token_label`` directly: it verifies that
    ``_process_market`` correctly aggregates the count from a REAL
    successful persist, not just from a stub.
    """
    # Single token_id → only YES branch runs → added count is exactly 1.
    mock_gamma.extract_token_ids.return_value = ["YES_TOK"]
    market = {
        "outcomePrices": ["1", "0"],
        "liquidity": 50000.0,
        "volume24hr": 25000.0,
    }

    added, skipped = await engine._process_market(
        market, extract_fn=_stub_extract_fn, n_features=38,
    )

    # ── Load-bearing: exactly one label was written ──
    assert added == 1, (
        f"expected added=1 (one successful label write), got added={added}"
    )
    assert skipped == 0, (
        f"expected skipped=0 (no skips on the happy path), got skipped={skipped}"
    )

    # ── Belt-and-braces: the label write actually fired exactly once ──
    assert mock_timescale.record_feature_vector.await_count == 1, (
        f"expected record_feature_vector to be awaited once (single-token "
        f"market), got {mock_timescale.record_feature_vector.await_count} calls"
    )

    # ── Belt-and-braces: the YES label was written with outcome_resolved=1
    # (resolved_yes=True for outcomePrices=["1","0"]). ──
    call_kwargs = mock_timescale.record_feature_vector.await_args.kwargs
    assert call_kwargs.get("token_id") == "YES_TOK"
    assert call_kwargs.get("outcome_resolved") == 1, (
        f"expected outcome_resolved=1 (YES winner for outcomePrices=['1','0']), "
        f"got {call_kwargs.get('outcome_resolved')!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 7. _process_market returns 0 for missing token_ids
# ────────────────────────────────────────────────────────────────────────────
async def test_process_market_returns_zero_for_missing_token_ids(
    engine: LabelBackfillEngine,
    mock_gamma: MagicMock,
    mock_timescale: MagicMock,  # noqa: ARG001 — referenced for side-effect setup
):
    """``_process_market`` must return ``(0, 1)`` — i.e., zero labels
    added, one market skipped — when ``gamma_client.extract_token_ids``
    returns an empty list.

    Production short-circuit (lines 218-220):
        token_ids = gamma_client.extract_token_ids(market)
        if not token_ids:
            return 0, 1

    This is the "market has no parseable token IDs" guard: a market
    without ``tokens`` / ``clobTokenIds`` (or with malformed values that
    fall through both extraction branches in
    ``GammaClient.extract_token_ids``) cannot have per-token feature
    rows written, so the market is counted as skipped and the loop
    moves on. No ``_persist_token_label`` call is made.

    Expected:
      * ``_process_market`` returns ``(0, 1)`` — zero added, one
        skipped.
      * Belt-and-braces: ``record_feature_vector`` was NOT awaited
        (the short-circuit happens before any persist call).
      * Belt-and-braces: ``has_labeled_sample`` was NOT called (same
        reason).
    """
    mock_gamma.extract_token_ids.return_value = []  # missing token_ids
    market = {"outcomePrices": ["1", "0"]}  # parseable, but moot

    added, skipped = await engine._process_market(
        market, extract_fn=_stub_extract_fn, n_features=38,
    )

    # ── Load-bearing: zero labels written ──
    assert added == 0, (
        f"expected added=0 (missing token_ids short-circuit), got added={added}"
    )
    assert skipped == 1, (
        f"expected skipped=1 (one market skipped), got skipped={skipped}"
    )

    # ── Belt-and-braces: the persist path was never entered ──
    assert mock_timescale.record_feature_vector.await_count == 0, (
        "record_feature_vector must NOT be awaited when token_ids is empty"
    )
    assert mock_timescale.has_labeled_sample.call_count == 0, (
        "has_labeled_sample must NOT be called when token_ids is empty"
    )
