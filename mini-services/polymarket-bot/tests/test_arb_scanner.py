"""
tests/test_arb_scanner.py — Unit tests for ``strategies/arb_scanner.py``.

X13 — five tests covering the binary-market arbitrage scanner:

  (1) ``_scan_for_arb`` finds a YES+NO < $1.00 long-side Dutch Book
      arbitrage and dispatches ``_execute_arb`` with
      ``arb_type="long_dutch_book"`` and the correct prices + profit.
  (2) ``_scan_for_arb`` returns empty (does NOT call ``_execute_arb``)
      when no arb exists (ask side ≥ $1.00 AND bid side ≤ $1.00).
  (3) ``_execute_arb`` respects ``ARB_ORDER_SIZE_USDC`` — the share
      size passed to BOTH ``submit_order`` legs is exactly
      ``max(1.0, self._order_size / max(yes_price, 0.05))``.
  (4) ``arb_min_profit_bps`` filter works — ``_check_long_dutch_book``
      returns ``None`` when ``profit_per_share < _min_profit_frac``
      and returns a ``(yes_price, no_price, profit)`` tuple when
      ``profit_per_share >= _min_profit_frac``.
  (5) ``_run`` loop starts (calls ``_build_market_pairs``, populates
      ``_pairs``, registers tokens with ``book_poller``, enters the
      scan loop) and stops (exits cleanly when ``self._running`` is
      flipped to ``False``).

Mocking strategy
~~~~~~~~~~~~~~~~~

  * ``mock_gamma`` — replaces the module-level ``gamma_client``
    singleton (imported by ``strategies.arb_scanner`` at module-load
    time) with a ``MagicMock`` whose ``get_markets`` is an
    ``AsyncMock`` returning a canned binary market list and whose
    ``extract_binary_pair`` is a ``MagicMock`` returning a fixed
    ``(YES_TOK, NO_TOK)`` tuple. The lazy ``gamma_client.get_markets``
    / ``extract_binary_pair`` call sites inside
    ``_build_market_pairs`` pick up the mock at call time.

  * ``mock_book_poller`` — replaces the module-level ``book_poller``
    singleton with a ``MagicMock`` so ``add_tokens`` calls are
    recorded for assertion without mutating the real poller's
    Tier-1 / Tier-2 token sets.

  * ``no_bg_tasks`` — replaces ``asyncio.create_task`` (as seen by
    ``strategies.arb_scanner``) with a no-op that closes the passed
    coroutine. This prevents the fire-and-forget observability metric
    tasks inside ``_scan_for_arb`` and the ``_pair_refresh_loop``
    background task inside ``_run`` from being scheduled on the event
    loop, so tests don't leak pending tasks across test boundaries.

    NOTE: ``asyncio.gather`` (used by ``_execute_arb`` to submit both
    legs concurrently) does NOT route through ``asyncio.create_task``
    — it calls ``loop.create_task`` via ``asyncio.ensure_future``
    directly. So mocking ``asyncio.create_task`` does NOT break
    ``asyncio.gather`` — tests that exercise ``_execute_arb``
    directly (test 3) work fine with or without ``no_bg_tasks``.

  * The global ``store`` singleton (kept pristine by the autouse
    ``_reset_store_factory_defaults`` fixture in ``tests/conftest.py``)
    is used directly: order books are seeded via
    ``store.update_order_book`` so the production
    ``store.get_order_book`` path runs end-to-end (not mocked).

  * ``_ml_arb_suspicion`` is monkeypatched to ``return False`` in
    tests that exercise ``_check_long_dutch_book`` with a profitable
    book — this isolates the arb-detection / min-profit-filter logic
    from the ML quality gate (which would otherwise short-circuit on
    a stale model disagreement in the test sandbox).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the X13 "Do NOT edit existing
files" constraint — mirrors the convention in
``tests/test_book_poller.py`` (V8), ``tests/test_gamma_client.py``
(V7), and every other Wave 3/4/5 test module).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` cannot be edited per the X13 task
# constraint ("Do NOT edit existing files"), so we use the module-level
# ``pytestmark`` idiom instead of ``asyncio_mode = "auto"`` — mirrors
# ``tests/test_book_poller.py`` (V8), ``tests/test_gamma_client.py``
# (V7), and every other Wave 3/4/5 test module.
pytestmark = pytest.mark.asyncio

from core.data_store import OrderBook, PriceLevel, store  # noqa: E402
from strategies.arb_scanner import ArbScannerStrategy  # noqa: E402


# ── Constants ────────────────────────────────────────────────────────────────

YES_TOK = "YES_TOK_x13"
NO_TOK = "NO_TOK_x13"
SLUG = "test-binary-market-x13"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_book(
    token_id: str,
    *,
    best_bid: float | None = None,
    best_ask: float | None = None,
    bid_size: float = 100.0,
    ask_size: float = 100.0,
) -> OrderBook:
    """Build a fresh ``OrderBook`` with the requested top-of-book.

    The depth (``bid_size`` / ``ask_size``) defaults to 100 shares —
    well above the ``min_required_shares`` ceiling of
    ``max(1.0, order_size / max(price, 0.05))`` (≈ 3.3 shares at the
    default ``ARB_ORDER_SIZE_USDC=1.5`` and ``price=0.45``), so the
    depth guard in ``_check_long_dutch_book`` / ``_check_short_overpriced``
    never short-circuits the test's intended assertion path.

    ``updated_at`` is set to ``time.time()`` (i.e. NOW) so the 30-second
    staleness guard in ``_check_long_dutch_book`` / ``_check_short_overpriced``
    never trips during the test.
    """
    bids = [PriceLevel(price=best_bid, size=bid_size)] if best_bid is not None else []
    asks = [PriceLevel(price=best_ask, size=ask_size)] if best_ask is not None else []
    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        updated_at=time.time(),
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_gamma(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``strategies.arb_scanner.gamma_client`` with a mock.

    ``get_markets`` is an ``AsyncMock`` returning a single-element list
    with a stub binary market dict; ``extract_binary_pair`` is a
    ``MagicMock`` returning ``(YES_TOK, NO_TOK)``. Tests that don't
    exercise ``_build_market_pairs`` (tests 1-4) never call into the
    mock — the fixture is still installed so a stray
    ``_build_market_pairs`` call cannot hit the real Gamma API.
    """
    mock = MagicMock()
    mock.get_markets = AsyncMock(return_value=[{
        "slug": SLUG,
        "tokens": [
            {"token_id": YES_TOK, "outcome": "Yes"},
            {"token_id": NO_TOK, "outcome": "No"},
        ],
    }])
    mock.extract_binary_pair = MagicMock(return_value=(YES_TOK, NO_TOK))
    monkeypatch.setattr("strategies.arb_scanner.gamma_client", mock)
    return mock


@pytest.fixture
def mock_book_poller(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``strategies.arb_scanner.book_poller`` with a ``MagicMock``.

    The real ``book_poller.add_tokens`` mutates the singleton's
    Tier-1 / Tier-2 token sets — using a mock keeps those sets pristine
    across tests and lets us assert on the token list passed to
    ``add_tokens``.
    """
    mock = MagicMock()
    mock.add_tokens = MagicMock(return_value=None)
    monkeypatch.setattr("strategies.arb_scanner.book_poller", mock)
    return mock


@pytest.fixture
def no_bg_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``asyncio.create_task`` (as seen by ``arb_scanner``)
    with a no-op that closes the passed coroutine.

    Prevents the fire-and-forget observability metric tasks inside
    ``_scan_for_arb`` and the ``_pair_refresh_loop`` background task
    inside ``_run`` from being scheduled on the event loop. Without
    this, those tasks would be pending at test teardown and could
    produce "Task was destroyed but it is pending" warnings (or, worse,
    run mid-test and perturb ``store`` / ``book_poller`` state).

    The coroutine is explicitly ``.close()``-d so Python doesn't emit
    a ``RuntimeWarning: coroutine ... was never awaited`` for the
    unscheduled coroutine.

    NOTE: ``asyncio.gather`` (used by ``_execute_arb``) routes through
    ``loop.create_task`` (via ``asyncio.ensure_future``), NOT through
    the module-level ``asyncio.create_task`` — so this mock does NOT
    interfere with ``_execute_arb``'s concurrent leg submission.
    """
    def _noop_create_task(coro, **_kwargs):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(
        "strategies.arb_scanner.asyncio.create_task", _noop_create_task
    )


@pytest.fixture
def scanner(mock_gamma, mock_book_poller) -> ArbScannerStrategy:
    """Fresh ``ArbScannerStrategy`` per test.

    The ``mock_gamma`` and ``mock_book_poller`` fixtures are pulled in
    implicitly so every test starts with the gamma client and book
    poller mocked out (no real network I/O can occur, even if a test
    accidentally triggers ``_build_market_pairs``).

    A brand-new instance (NOT the module-level singleton, which
    ``arb_scanner.py`` doesn't actually export — the strategy is
    instantiated on demand by the registry) is used so
    ``_pairs`` / ``_market_slugs`` / ``_running`` start at the
    factory defaults every test.
    """
    return ArbScannerStrategy()


@pytest.fixture
def scanner_with_pair(scanner: ArbScannerStrategy) -> ArbScannerStrategy:
    """Scanner pre-populated with a single YES/NO token pair + slug map.

    Skips the ``_build_market_pairs`` discovery step so tests 1-4 can
    exercise the scan / execute / filter logic directly without going
    through Gamma. Both ``scanner._market_slugs`` and the global
    ``store.market_slugs`` are seeded so the production logging path
    (which looks up ``self._market_slugs.get(yes_token, yes_token[:12])``)
    resolves to the test slug.
    """
    scanner._pairs = {YES_TOK: NO_TOK}
    scanner._market_slugs = {YES_TOK: SLUG, NO_TOK: SLUG}
    store.market_slugs[YES_TOK] = SLUG
    store.market_slugs[NO_TOK] = SLUG
    return scanner


# ── (1) scan finds YES+NO < $1 arbitrage ─────────────────────────────────────

async def test_scan_finds_long_dutch_book_arbitrage(
    scanner_with_pair: ArbScannerStrategy, no_bg_tasks: None
):
    """``_scan_for_arb`` detects a long-side Dutch Book when
    ``ask(YES) + ask(NO) < 1.00`` and dispatches ``_execute_arb`` with
    ``arb_type="long_dutch_book"`` and the correct yes/no prices and
    profit-per-share.

    Setup: YES ask = 0.45, NO ask = 0.50 → total_cost = 0.95 →
    profit_per_share = 0.05 (well above the default
    ``_min_profit_frac = 0.005`` = 50 bps). The ML quality filter is
    stubbed to "not suspicious" so the arb is not skipped.

    The short-side check (``_check_short_overpriced``) is also invoked
    on the same pair, but with YES bid=0.40 + NO bid=0.45 = 0.85 < 1.00,
    so no short arb fires — only one ``_execute_arb`` call total.
    """
    scanner = scanner_with_pair
    # YES ask = 0.45, NO ask = 0.50 → long Dutch book (0.45 + 0.50 = 0.95 < 1.00)
    await store.update_order_book(
        _make_book(YES_TOK, best_bid=0.40, best_ask=0.45)
    )
    await store.update_order_book(
        _make_book(NO_TOK, best_bid=0.45, best_ask=0.50)
    )

    # Stub _execute_arb so no real orders are submitted; capture call args.
    scanner._execute_arb = AsyncMock(return_value=None)
    # Stub ML quality filter so the arb isn't skipped on suspicion.
    scanner._ml_arb_suspicion = MagicMock(return_value=False)

    await scanner._scan_for_arb()

    # _execute_arb must have been called exactly once with long_dutch_book.
    scanner._execute_arb.assert_awaited_once()
    args = scanner._execute_arb.await_args.args
    # _execute_arb(yes_token, no_token, yes_price, no_price, profit, arb_type)
    assert args[0] == YES_TOK
    assert args[1] == NO_TOK
    assert abs(args[2] - 0.45) < 1e-9, "yes_price must be the YES best ask"
    assert abs(args[3] - 0.50) < 1e-9, "no_price must be the NO best ask"
    assert abs(args[4] - 0.05) < 1e-9, (
        "profit must be 1.00 - (0.45 + 0.50) = 0.05"
    )
    assert args[5] == "long_dutch_book"


# ── (2) scan returns empty when no arb exists ────────────────────────────────

async def test_scan_returns_empty_when_no_arb(
    scanner_with_pair: ArbScannerStrategy, no_bg_tasks: None
):
    """``_scan_for_arb`` must NOT dispatch ``_execute_arb`` when no
    arbitrage exists — i.e. when the ask side sums to ≥ $1.00 (no long
    Dutch Book) AND the bid side sums to ≤ $1.00 (no short overpriced).

    Setup: YES ask = 0.55, NO ask = 0.50 → total_ask = 1.05 (no long);
    YES bid = 0.50, NO bid = 0.45 → total_bid = 0.95 (no short).
    Neither opportunity tuple is appended, so ``_execute_arb`` is never
    called.
    """
    scanner = scanner_with_pair
    # No long Dutch Book (1.05 > 1.00); no short overpriced (0.95 < 1.00)
    await store.update_order_book(
        _make_book(YES_TOK, best_bid=0.50, best_ask=0.55)
    )
    await store.update_order_book(
        _make_book(NO_TOK, best_bid=0.45, best_ask=0.50)
    )

    scanner._execute_arb = AsyncMock(return_value=None)
    scanner._ml_arb_suspicion = MagicMock(return_value=False)

    await scanner._scan_for_arb()

    scanner._execute_arb.assert_not_awaited()


# ── (3) arb size respects ARB_ORDER_SIZE_USDC ────────────────────────────────

async def test_arb_size_respects_order_size_usdc(
    scanner_with_pair: ArbScannerStrategy,
):
    """``_execute_arb`` computes the order size as
    ``max(1.0, self._order_size / max(yes_price, 0.05))`` and forwards
    it to BOTH ``submit_order`` calls (YES + NO legs).

    Two scenarios are exercised against the default
    ``ARB_ORDER_SIZE_USDC = 1.5`` and a re-tuned ``_order_size = 5.0``:

      (a) default 1.5 USDC, yes_price = 0.45 → size = 1.5 / 0.45 ≈ 3.333
      (b) custom  5.0 USDC, yes_price = 0.25 → size = 5.0 / 0.25 = 20.0

    The size on both legs must match the formula exactly — this is the
    contract that prevents the scanner from over-shooting available
    depth (the depth guard in ``_check_long_dutch_book`` already
    verified enough shares exist at the ask to fill the order).

    ``submit_order`` is mocked as an ``AsyncMock`` returning a truthy
    ``MagicMock`` so the ``if yes_order and no_order:`` branch (which
    logs the "executed" event) is exercised end-to-end. The
    ``OrderArgs`` objects passed to ``submit_order`` are captured via
    ``call_args_list`` and their ``.size`` field asserted against the
    expected formula output.

    NOTE: ``no_bg_tasks`` is intentionally NOT applied here —
    ``_execute_arb`` doesn't call ``asyncio.create_task`` (it uses
    ``asyncio.gather``, which routes through ``loop.create_task``).
    """
    scanner = scanner_with_pair

    # ── (a) default _order_size = 1.5, yes_price = 0.45 ──
    assert scanner._order_size == 1.5, (
        "Default ARB_ORDER_SIZE_USDC must be 1.5 (from config.Settings)"
    )

    mock_submit = AsyncMock(return_value=MagicMock())  # truthy → "executed" branch
    scanner.submit_order = mock_submit

    await scanner._execute_arb(
        YES_TOK, NO_TOK,
        yes_price=0.45, no_price=0.50,
        profit=0.05, arb_type="long_dutch_book",
    )

    expected_size_a = max(1.0, 1.5 / 0.45)  # ≈ 3.3333
    assert mock_submit.await_count == 2, (
        "Both YES and NO legs must be submitted via asyncio.gather"
    )
    sizes_a = [c.args[0].size for c in mock_submit.call_args_list]
    token_ids_a = [c.args[0].token_id for c in mock_submit.call_args_list]
    assert all(abs(s - expected_size_a) < 1e-9 for s in sizes_a), (
        f"Both legs must use size = order_size / yes_price = {expected_size_a}"
    )
    assert set(token_ids_a) == {YES_TOK, NO_TOK}, (
        "Both YES and NO token ids must be submitted"
    )

    # ── (b) re-tuned _order_size = 5.0, yes_price = 0.25 ──
    scanner._order_size = 5.0
    mock_submit.reset_mock()

    await scanner._execute_arb(
        YES_TOK, NO_TOK,
        yes_price=0.25, no_price=0.70,
        profit=0.05, arb_type="long_dutch_book",
    )

    expected_size_b = max(1.0, 5.0 / 0.25)  # = 20.0
    assert mock_submit.await_count == 2
    sizes_b = [c.args[0].size for c in mock_submit.call_args_list]
    assert all(abs(s - expected_size_b) < 1e-9 for s in sizes_b), (
        f"Both legs must use size = 5.0 / 0.25 = {expected_size_b}"
    )


# ── (4) arb_min_profit_bps filter works ──────────────────────────────────────

async def test_min_profit_bps_filter(
    scanner_with_pair: ArbScannerStrategy,
):
    """``_check_long_dutch_book`` enforces the ``arb_min_profit_bps``
    floor: only opportunities with
    ``profit_per_share >= _min_profit_frac`` are returned.

    With the default ``arb_min_profit_bps = 50``,
    ``_min_profit_frac = max(0.003, 50/10_000) = 0.005``. Two
    scenarios are exercised:

      (a) profit = 0.004 (below 0.005) → returns ``None`` (filtered out).
          yes_ask=0.49 + no_ask=0.506 = 0.996 → profit = 0.004.
      (b) profit = 0.010 (above 0.005) → returns the
          ``(yes_price, no_price, profit)`` tuple.
          yes_ask=0.45 + no_ask=0.54 = 0.99 → profit = 0.010.

    The ML quality filter is stubbed to "not suspicious" so the
    above-threshold case reaches the return statement (otherwise an
    unpredictable ML disagreement could short-circuit it).

    NOTE: ``no_bg_tasks`` is intentionally NOT applied here —
    ``_check_long_dutch_book`` is a pure read of the store (no
    ``asyncio.create_task`` calls), so there are no background tasks
    to suppress.
    """
    scanner = scanner_with_pair
    assert scanner._min_profit_frac == 0.005, (
        "Default arb_min_profit_bps=50 → _min_profit_frac=0.005"
    )
    scanner._ml_arb_suspicion = MagicMock(return_value=False)

    # ── (a) below threshold: profit = 0.004 < 0.005 ──
    # 0.49 + 0.506 = 0.996 → profit = 0.004
    await store.update_order_book(
        _make_book(YES_TOK, best_ask=0.49, best_bid=0.40)
    )
    await store.update_order_book(
        _make_book(NO_TOK, best_ask=0.506, best_bid=0.40)
    )
    result_below = await scanner._check_long_dutch_book(YES_TOK, NO_TOK)
    assert result_below is None, (
        "profit=0.004 < _min_profit_frac=0.005 → must be filtered out"
    )

    # ── (b) above threshold: profit = 0.010 >= 0.005 ──
    # 0.45 + 0.54 = 0.99 → profit = 0.010
    await store.update_order_book(
        _make_book(YES_TOK, best_ask=0.45, best_bid=0.40)
    )
    await store.update_order_book(
        _make_book(NO_TOK, best_ask=0.54, best_bid=0.40)
    )
    result_above = await scanner._check_long_dutch_book(YES_TOK, NO_TOK)
    assert result_above is not None, (
        "profit=0.010 >= _min_profit_frac=0.005 → must return a tuple"
    )
    yes_price, no_price, profit = result_above
    assert abs(yes_price - 0.45) < 1e-9
    assert abs(no_price - 0.54) < 1e-9
    assert abs(profit - 0.010) < 1e-9


# ── (5) _run loop starts and stops ────────────────────────────────────────────

async def test_run_loop_starts_and_stops(
    scanner: ArbScannerStrategy,
    mock_gamma: MagicMock,
    mock_book_poller: MagicMock,
    no_bg_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
):
    """``_run`` boots the strategy lifecycle:

      (a) Calls ``_build_market_pairs`` → populates ``_pairs`` from the
          mocked ``gamma_client.get_markets`` / ``extract_binary_pair``.
      (b) Registers the discovered token ids with ``book_poller.add_tokens``.
      (c) Enters the ``while self._running:`` scan loop and calls
          ``_scan_for_arb`` at least once.
      (d) Exits cleanly when ``self._running`` is flipped to ``False``
          (here, by a mocked ``asyncio.sleep`` that trips the flag on
          its first invocation — mirroring the
          ``_patch_sleep_to_run_one_cycle`` pattern in
          ``tests/test_book_poller.py`` (V8)).

    Assertions verify (a) ``_pairs`` is populated with the YES/NO pair,
    (b) ``book_poller.add_tokens`` was called with both token ids, and
    (c) ``_scan_for_arb`` was called exactly once before the loop
    exited (i.e. the loop ran exactly one iteration).

    ``_scan_for_arb`` is mocked as an ``AsyncMock`` so no real scan
    logic / observability tasks run — this test is about the loop
    lifecycle (start + stop), not the scan internals (those are
    covered by tests 1-4).
    """
    # Mock _scan_for_arb so no real scan / observability tasks run.
    scanner._scan_for_arb = AsyncMock(return_value=None)

    # Patch asyncio.sleep (as seen by arb_scanner) to flip _running off
    # on the first invocation — the while loop then exits after one
    # iteration. Mirrors the ``_patch_sleep_to_run_one_cycle`` pattern
    # in tests/test_book_poller.py (V8).
    sleep_calls = 0

    async def _mock_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        # First sleep == end-of-first-iteration sleep in _run's while
        # loop → stop the loop so _run returns.
        scanner._running = False

    monkeypatch.setattr("strategies.arb_scanner.asyncio.sleep", _mock_sleep)

    # Start the loop.
    scanner._running = True
    await scanner._run()  # must return (not hang)

    # (a) _pairs was populated from the mocked gamma client.
    assert scanner._pairs == {YES_TOK: NO_TOK}, (
        "_build_market_pairs must populate _pairs from gamma_client"
    )
    # The slug map should also be populated for both tokens.
    assert scanner._market_slugs.get(YES_TOK) == SLUG
    assert scanner._market_slugs.get(NO_TOK) == SLUG

    # (b) book_poller.add_tokens was called once with both token ids.
    mock_book_poller.add_tokens.assert_called_once()
    tokens_arg = mock_book_poller.add_tokens.call_args.args[0]
    assert set(tokens_arg) == {YES_TOK, NO_TOK}, (
        "add_tokens must receive the YES+NO token ids from _pairs"
    )

    # (c) _scan_for_arb was called exactly once (one loop iteration).
    assert scanner._scan_for_arb.await_count == 1, (
        "Scan loop must run exactly one iteration before _running was flipped"
    )

    # (d) _running is False (loop exited cleanly).
    assert scanner._running is False
