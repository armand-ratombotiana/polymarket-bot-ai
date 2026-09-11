"""
Unit tests for ``ingestion/backfill.py`` — W31-3 task.

Scope: NEW file only — no existing source files or test files edited.
Mirrors the isolation strategy already established by
``tests/test_label_backfill.py`` (W5) and ``tests/test_trade_ingester.py``
(W20-7).

Twelve tests, aligned with the W31-3 task spec's verification
requirements:

  1. Market discovery — pages through Gamma, upserts every market, and
     detects new markets since the last run.
  2. Price history fetching — fetches trades for a token and
     aggregates them into OHLCV candles at the requested resolution.
  3. Outcome backfill — pages through resolved markets, records the
     YES / NO outcome, and propagates the label to
     ``timescale_db.mark_resolved_outcomes``.
  4. Deduplication — running ``backfill_metadata`` twice produces the
     same row count (no duplicates), and re-running ``backfill_trades``
     is a no-op at the DB level (via ``record_trade``'s
     ``ON CONFLICT DO NOTHING`` clause).
  5. Resume / checkpoint — a checkpoint written after page 1 is
     honoured on the second invocation so the engine resumes from
     page 2 instead of restarting at page 1.
  6. Rate limit handling — the shared :class:`RateLimiter` enforces a
     minimum interval between successive ``acquire()`` calls, and
     ``record_rate_limit()`` multiplies the interval by the backoff
     factor (capped at ``max_interval_s``).
  7. CLI ``scripts/run_backfill.py`` — the ``--type`` flag accepts the
     five backfill types plus ``all``, ``--market`` restricts to a
     single market, ``--days`` sets the historical depth, and the
     ``--list-markets`` / ``--list-runs`` subcommands print the
     persisted state and exit cleanly.

Mock strategy
~~~~~~~~~~~~~~

  * ``mock_gamma`` — a :class:`unittest.mock.AsyncMock` (spec=GammaClient)
    whose ``get_markets`` / ``get_market`` coroutines return
    controlled payloads. Patched onto ``ingestion.backfill.gamma_client``
    so the engine's default ``gamma`` reference resolves to the mock.
  * ``mock_clob`` — a :class:`unittest.mock.AsyncMock` for the CLOB
    client. The engine's ``self.clob`` attribute is set directly to
    the mock in each test (the engine accepts a ``clob`` kwarg so no
    module-level patching is needed).
  * ``mock_db`` — a :class:`unittest.mock.MagicMock` for the
    :class:`TimescaleDBEngine`. ``record_snapshot`` / ``record_trade``
    are :class:`AsyncMock` returning ``True``; ``mark_resolved_outcomes``
    is a sync ``MagicMock``. Set via the engine's ``db`` kwarg.
  * ``store`` — a real :class:`BackfillStore` pointed at a temp
    ``MARKET_DB_PATH`` (the conftest already redirects ``MARKET_DB_PATH``
    to ``/tmp/pmbot_conftest_isolation/market_intelligence.db``; each
    test gets a fresh DB via the ``backfill_store`` fixture which
    overrides the path with a unique tmp file).
  * ``LabelBackfillEngine._resolve_outcome`` — NOT mocked. The real
    production parser is exercised so the threshold logic can't drift
    (same approach as ``tests/test_label_backfill.py``).

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` is intentionally minimal — ``testpaths = tests`` — so
``asyncio_mode = "auto"`` is not enabled via config).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). Mirrors the pattern in
# ``tests/test_ingestion_infra.py`` so the ``ingestion.*`` and
# ``core.timescale_db`` module-level singletons don't raise
# PermissionError on the read-only ``/app/data`` sandbox path.
_TMP_ROOT = Path("/tmp/pmbot_backfill_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-backfill",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# ── Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package. ──────────────────────────────────────────
# Same situation + fix as ``tests/test_ingestion_infra.py``: pytest's default
# ``prepend`` import mode inserts ``tests/`` at ``sys.path[0]`` during test
# collection, which lets the sibling ``tests/ingestion/`` package shadow our
# top-level ``polymarket-bot/ingestion/`` package. Without the ``remove``
# step below, the project root ends up behind ``tests/`` in sys.path, and
# ``from ingestion.backfill import ...`` resolves to
# ``tests/ingestion/__init__.py`` (which has no ``backfill`` submodule).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Clear any cached ``ingestion`` / ``ingestion.*`` module pointing at the
# ``tests/ingestion/`` directory so the next import resolves against the
# freshly-prepended ``_PROJECT_ROOT``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_label_backfill.py``.
pytestmark = pytest.mark.asyncio

from core.gamma_client import GammaClient  # noqa: E402
from ingestion.backfill import (  # noqa: E402
    DEFAULT_RATE_LIMIT_RPS,
    BackfillCheckpoint,
    BackfillEngine,
    BackfillStats,
    BackfillStore,
    BackfillType,
    RateLimiter,
    RESOLUTION_SECONDS,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers — sample market dicts used across tests
# ────────────────────────────────────────────────────────────────────────────

# Sentinel used to distinguish "argument not supplied" (use default
# ``["0.50", "0.50"]``) from "explicitly None" (use None — used to
# exercise the engine's unresolvable-outcome skip path).
_NO_OUTCOME_PRICES = object()


def _market_dict(
    condition_id: str = "0xCOND1",
    *,
    slug: str = "test-market",
    question: str = "Will the test pass?",
    outcome_prices: Any = _NO_OUTCOME_PRICES,
    active: bool = True,
    closed: bool = False,
    volume_24h: float = 1000.0,
    liquidity: float = 5000.0,
    token_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Gamma market dict for tests."""
    if outcome_prices is _NO_OUTCOME_PRICES:
        outcome_prices = ["0.50", "0.50"]
    # else: honour the caller's value — including explicit ``None``
    # (exercises the engine's "unresolvable outcome" skip path).
    if token_ids is None:
        token_ids = ["YES_TOK_1", "NO_TOK_1"]
    return {
        "conditionId": condition_id,
        "slug": slug,
        "question": question,
        "description": "test description",
        "category": "test",
        "tags": ["test", "unit"],
        "outcomePrices": outcome_prices,
        "outcomes": ["YES", "NO"],
        "rules": "test rules",
        "volume24hr": volume_24h,
        "liquidity": liquidity,
        "active": active,
        "closed": closed,
        "clobTokenIds": json.dumps(token_ids),
    }


def _resolved_market_dict(
    condition_id: str = "0xRESOLVED1",
    *,
    yes_won: bool = True,
    token_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a resolved market dict with the canonical winner payload."""
    if yes_won:
        outcome_prices = ["1", "0"]
    else:
        outcome_prices = ["0", "1"]
    if token_ids is None:
        token_ids = ["YES_TOK_R", "NO_TOK_R"]
    return _market_dict(
        condition_id,
        slug="resolved-market",
        question="Did the resolved event occur?",
        outcome_prices=outcome_prices,
        active=False,
        closed=True,
        volume_24h=25000.0,
        liquidity=20000.0,
        token_ids=token_ids,
    )


def _trade_dict(
    trade_id: str,
    token_id: str = "YES_TOK_1",
    *,
    price: float = 0.55,
    size: float = 100.0,
    side: str = "BUY",
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal CLOB trade dict."""
    return {
        "trade_id": trade_id,
        "token_id": token_id,
        "price": price,
        "size": size,
        "side": side,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "maker_address": "0xMAKER",
        "taker_order_id": "0xTAKER",
    }


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def backfill_store(tmp_path: Path) -> BackfillStore:
    """Fresh :class:`BackfillStore` pointed at a unique temp SQLite file.

    Using a unique file per test (rather than the conftest-redirected
    ``MARKET_DB_PATH``) prevents cross-test pollution: the
    ``backfill_markets`` / ``backfill_checkpoint`` / ``backfill_runs``
    tables are empty on every test, so assertions on row counts and
    checkpoints are deterministic.
    """
    return BackfillStore(sqlite_path=tmp_path / "backfill_test.db")


@pytest.fixture
def mock_gamma(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mock the ``ingestion.backfill.gamma_client`` module-level singleton.

    A :class:`AsyncMock` with ``spec=GammaClient`` so attribute access
    is restricted to the real ``GammaClient``'s public surface. The
    async methods (``get_markets``, ``get_market``,
    ``get_resolved_markets``) are configured per-test via their
    ``side_effect`` / ``return_value`` attributes.
    """
    mock = AsyncMock(spec=GammaClient)
    mock.get_markets = AsyncMock(return_value=[])
    mock.get_market = AsyncMock(return_value={})
    mock.get_resolved_markets = AsyncMock(return_value=[])
    # ``extract_token_ids`` is a @staticmethod on the real class — keep
    # it pointing at the real implementation so token extraction logic
    # is exercised (mirrors the W5 ``test_label_backfill.py`` approach
    # of letting the REAL static methods run).
    mock.extract_token_ids = GammaClient.extract_token_ids
    mock.extract_binary_pair = GammaClient.extract_binary_pair
    monkeypatch.setattr("ingestion.backfill.gamma_client", mock)
    return mock


@pytest.fixture
def mock_clob() -> AsyncMock:
    """Mock for the CLOB client (injected via the engine's ``clob`` kwarg).

    ``get_public_trades`` and ``get_order_book`` are
    :class:`AsyncMock`s returning empty lists / empty dicts by default;
    per-test code rewrites them as needed.
    """
    mock = AsyncMock()
    mock.get_public_trades = AsyncMock(return_value=[])
    mock.get_order_book = AsyncMock(return_value={})
    return mock


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock for the :class:`TimescaleDBEngine` (injected via the engine's
    ``db`` kwarg).

    ``record_snapshot`` / ``record_trade`` / ``record_feature_vector``
    are :class:`AsyncMock`s returning ``True``; ``mark_resolved_outcomes``
    is a sync ``MagicMock`` returning ``1`` (one row updated). All other
    attributes fall through to default ``MagicMock`` behaviour so any
    unexpected call surfaces in the test rather than crashing silently.
    """
    mock = MagicMock()
    mock.record_snapshot = AsyncMock(return_value=True)
    mock.record_trade = AsyncMock(return_value=True)
    mock.record_feature_vector = AsyncMock(return_value=True)
    mock.mark_resolved_outcomes = MagicMock(return_value=1)
    return mock


@pytest.fixture
def engine(
    mock_gamma: AsyncMock,
    mock_clob: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
) -> BackfillEngine:
    """Fresh :class:`BackfillEngine` wired to the test mocks.

    The engine's default ``gamma_client`` singleton is replaced by
    ``mock_gamma`` via the monkeypatch in ``mock_gamma``; the ``clob`` /
    ``db`` kwargs point at the test mocks directly. ``store`` is a
    fresh per-test :class:`BackfillStore`.
    """
    return BackfillEngine(
        gamma=mock_gamma,
        db=mock_db,
        clob=mock_clob,
        target_rps=1000.0,  # high enough that the rate limiter doesn't sleep in tests
        concurrency=2,
        page_size=10,
        max_pages=5,
        store=backfill_store,
    )


# ────────────────────────────────────────────────────────────────────────────
# 1. Market discovery backfill
# ────────────────────────────────────────────────────────────────────────────


async def test_market_discovery_upserts_every_market(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """``backfill_metadata`` pages through Gamma and upserts every market.

    Setup: ``mock_gamma.get_markets`` returns 3 markets on the first
    call, then an empty list on the second call (terminating the
    pagination loop).

    Expected:
      * All 3 markets are upserted into ``backfill_markets``.
      * ``stats.total_added == 3``.
      * ``stats.total_processed == 3``.
      * The known-condition-ids set after the run contains all 3 ids.
    """
    markets = [
        _market_dict("0xCOND1", slug="m1", token_ids=["T1", "T2"]),
        _market_dict("0xCOND2", slug="m2", token_ids=["T3", "T4"]),
        _market_dict("0xCOND3", slug="m3", token_ids=["T5", "T6"]),
    ]
    mock_gamma.get_markets.side_effect = [markets, []]

    stats = await engine.backfill_metadata(resume=False)

    assert stats.total_processed == 3
    assert stats.total_added == 3
    assert stats.total_errors == 0
    known = backfill_store.get_known_condition_ids()
    assert known == {"0xCOND1", "0xCOND2", "0xCOND3"}


async def test_market_discovery_detects_new_markets_since_last_run(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """The engine detects markets newly appearing on Gamma since the last run.

    Setup: first run upserts 2 markets. Second run sees 3 markets on
    Gamma (2 known + 1 new). The new market should be upserted.

    Expected:
      * After run 1: ``known == {c1, c2}``.
      * After run 2: ``known == {c1, c2, c3}``.
      * Run 2's ``total_added == 3`` (all 3 markets in the page are
        upserted — re-upserts are no-ops at the SQL level, but the
        engine counts every successful upsert toward ``total_added``).
    """
    markets_run1 = [
        _market_dict("0xC1", slug="m1", token_ids=["T1"]),
        _market_dict("0xC2", slug="m2", token_ids=["T2"]),
    ]
    markets_run2 = [
        _market_dict("0xC1", slug="m1", token_ids=["T1"]),  # already known
        _market_dict("0xC2", slug="m2", token_ids=["T2"]),  # already known
        _market_dict("0xC3", slug="m3", token_ids=["T3"]),  # NEW
    ]
    # side_effect: run 1 consumes ``markets_run1`` (1 page); run 2
    # consumes ``markets_run2`` (1 page). The pagination loop breaks
    # immediately when ``len(markets) < page_size`` so a trailing
    # empty list is not needed.
    mock_gamma.get_markets.side_effect = [markets_run1, markets_run2]

    # ── Run 1 ──
    stats1 = await engine.backfill_metadata(resume=False)
    assert stats1.total_added == 2
    assert backfill_store.get_known_condition_ids() == {"0xC1", "0xC2"}

    # ── Run 2 ──
    stats2 = await engine.backfill_metadata(resume=False)
    assert stats2.total_added == 3
    assert backfill_store.get_known_condition_ids() == {"0xC1", "0xC2", "0xC3"}

    # ── The new-market detection is observable via the known-set diff ──
    new_since_run1 = backfill_store.get_known_condition_ids() - {"0xC1", "0xC2"}
    assert new_since_run1 == {"0xC3"}


async def test_market_discovery_single_market_path(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """``backfill_metadata(market_token=...)`` fetches a single market.

    Setup: ``mock_gamma.get_market`` returns one market dict.

    Expected:
      * ``get_markets`` is NOT called (single-market path skips pagination).
      * ``get_market`` is called exactly once with the condition_id.
      * ``total_added == 1``.
    """
    market = _market_dict("0xSINGLE", slug="single", token_ids=["T1", "T2"])
    mock_gamma.get_market.return_value = market

    stats = await engine.backfill_metadata(market_token="0xSINGLE", resume=False)

    assert stats.total_processed == 1
    assert stats.total_added == 1
    assert mock_gamma.get_market.await_count == 1
    assert mock_gamma.get_markets.await_count == 0
    assert "0xSINGLE" in backfill_store.get_known_condition_ids()


# ────────────────────────────────────────────────────────────────────────────
# 2. Price history backfill
# ────────────────────────────────────────────────────────────────────────────


async def test_price_history_fetches_trades_and_writes_ohlcv_candles(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_clob: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
):
    """``backfill_prices`` fetches trades for each token and writes OHLCV candles.

    Setup:
      * Seed ``backfill_markets`` with one market whose token list is
        ``["YES_TOK", "NO_TOK"]``.
      * ``mock_clob.get_public_trades`` returns 3 trades at 3 distinct
        timestamps within the same 1-hour bucket.

    Expected:
      * ``mock_clob.get_public_trades`` is called with
        ``token_id="YES_TOK"`` (and ``"NO_TOK"``).
      * ``mock_db.record_snapshot`` is called once per candle (here 1
        candle, since all 3 trades fall in the same hour bucket).
      * ``stats.total_added >= 1`` (one candle written).
    """
    # Seed one market with 2 tokens
    market = _market_dict("0xP1", slug="p1", token_ids=["YES_TOK", "NO_TOK"])
    backfill_store.upsert_market(market, backfilled_metadata=True)

    # 3 trades at 1-minute intervals within the same hour
    base_ts = time.time() - 600  # 10 minutes ago
    trades = [
        _trade_dict("tr1", "YES_TOK", price=0.50, size=100, timestamp=base_ts),
        _trade_dict("tr2", "YES_TOK", price=0.55, size=200, timestamp=base_ts + 60),
        _trade_dict("tr3", "YES_TOK", price=0.52, size=150, timestamp=base_ts + 120),
    ]
    mock_clob.get_public_trades.return_value = trades

    stats = await engine.backfill_prices(days=1, resolution="1h", resume=False)

    assert stats.total_processed >= 1
    assert stats.total_added >= 1
    assert stats.total_errors == 0
    # Snapshot was written at least once (one candle bucket per token)
    assert mock_db.record_snapshot.await_count >= 1
    # Verify the snapshot was written with the YES token id
    call_kwargs = mock_db.record_snapshot.await_args_list[0].kwargs
    assert call_kwargs.get("token_id") in {"YES_TOK", "NO_TOK"}


async def test_price_history_aggregate_ohlcv_buckets_correctly(
    engine: BackfillEngine,
):
    """``_aggregate_ohlcv`` correctly aggregates trades into OHLCV candles.

    Setup: 4 trades — 2 in bucket 0 (ts=10, 60) and 2 in bucket 1
    (ts=130, 180) — at a 2-minute (120 s) resolution. Bucket 0 contains
    ts=10 (price=0.50, size=100) and ts=60 (price=0.55, size=200);
    bucket 1 contains ts=130 (price=0.52, size=150) and ts=180
    (price=0.58, size=100).

    Expected:
      * 2 candles returned (bucket 0 at ts=0, bucket 1 at ts=120).
      * Bucket 0: open=0.50, high=0.55, low=0.50, close=0.55,
        volume=(0.50*100 + 0.55*200)=160.0
      * Bucket 1: open=0.52, high=0.58, low=0.52, close=0.58,
        volume=(0.52*150 + 0.58*100)=136.0

    Note: ts=0 is filtered out by the production code's
    ``if ts <= 0 or price <= 0: continue`` guard (a ts=0 typically means
    "unknown timestamp" in the CLOB response). All test trades use
    ts > 0 so they're not skipped.
    """
    trades = [
        {"price": 0.50, "size": 100, "timestamp": 10},
        {"price": 0.55, "size": 200, "timestamp": 60},
        {"price": 0.52, "size": 150, "timestamp": 130},
        {"price": 0.58, "size": 100, "timestamp": 180},
    ]
    # candle_s=120 (2-minute candles) → ts=10 and ts=60 fall in bucket 0;
    # ts=130 and ts=180 fall in bucket 120.
    candles = BackfillEngine._aggregate_ohlcv(trades, candle_s=120)
    assert len(candles) == 2
    assert candles[0]["timestamp"] == 0
    assert candles[0]["open"] == 0.50
    assert candles[0]["high"] == 0.55
    assert candles[0]["low"] == 0.50
    assert candles[0]["close"] == 0.55
    assert candles[0]["volume"] == pytest.approx(160.0)
    assert candles[1]["timestamp"] == 120
    assert candles[1]["open"] == 0.52
    assert candles[1]["high"] == 0.58
    assert candles[1]["low"] == 0.52
    assert candles[1]["close"] == 0.58
    assert candles[1]["volume"] == pytest.approx(136.0)


async def test_price_history_aggregate_ohlcv_empty_input(
    engine: BackfillEngine,
):
    """``_aggregate_ohlcv`` on empty input returns an empty list."""
    candles = BackfillEngine._aggregate_ohlcv([], candle_s=60)
    assert candles == []


async def test_price_history_rejects_unknown_resolution(
    engine: BackfillEngine,
):
    """``backfill_prices`` raises ``ValueError`` on an unknown resolution."""
    with pytest.raises(ValueError, match="unsupported resolution"):
        await engine.backfill_prices(resolution="2h", resume=False)


# ────────────────────────────────────────────────────────────────────────────
# 3. Outcome backfill
# ────────────────────────────────────────────────────────────────────────────


async def test_outcome_backfill_records_resolved_yes_winner(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
):
    """``backfill_outcomes`` records YES winner and propagates to feature store.

    Setup: ``mock_gamma.get_markets`` returns one resolved market with
    ``outcomePrices=["1","0"]`` (YES won). Token list is
    ``["YES_R", "NO_R"]``.

    Expected:
      * ``backfill_markets.resolved_outcome_yes == 1`` for that market.
      * ``timescale_db.mark_resolved_outcomes`` is called twice (once
        for YES token with ``True``, once for NO token with ``False``).
      * ``stats.total_added == 1``.
    """
    market = _resolved_market_dict("0xOUT1", yes_won=True,
                                  token_ids=["YES_R", "NO_R"])
    mock_gamma.get_markets.side_effect = [[market], []]

    stats = await engine.backfill_outcomes(resume=False)

    assert stats.total_processed == 1
    assert stats.total_added == 1
    assert stats.total_errors == 0

    # The market row should now carry the YES winner
    markets = backfill_store.list_markets(resolved=True)
    assert len(markets) == 1
    assert markets[0]["resolved_outcome_yes"] == 1

    # The ML feature store was marked twice: YES token=True, NO token=False
    assert mock_db.mark_resolved_outcomes.call_count == 2
    yes_call = mock_db.mark_resolved_outcomes.call_args_list[0]
    no_call = mock_db.mark_resolved_outcomes.call_args_list[1]
    assert yes_call.args[0] == "YES_R"
    assert yes_call.args[1] is True
    assert no_call.args[0] == "NO_R"
    assert no_call.args[1] is False


async def test_outcome_backfill_records_resolved_no_winner(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
):
    """``backfill_outcomes`` records NO winner when ``outcomePrices=["0","1"]``.

    The label convention is that the YES token's outcome_resolved is the
    inverse of the YES winner: when YES loses (``p0 < 0.9``), YES token
    gets ``outcome_resolved=0`` and NO token gets ``outcome_resolved=1``.
    """
    market = _resolved_market_dict("0xOUT2", yes_won=False,
                                  token_ids=["YES_R2", "NO_R2"])
    mock_gamma.get_markets.side_effect = [[market], []]

    stats = await engine.backfill_outcomes(resume=False)

    assert stats.total_added == 1
    markets = backfill_store.list_markets(resolved=True)
    assert markets[0]["resolved_outcome_yes"] == 0

    # YES token marked False (it lost); NO token marked True (it won)
    assert mock_db.mark_resolved_outcomes.call_count == 2
    yes_call = mock_db.mark_resolved_outcomes.call_args_list[0]
    no_call = mock_db.mark_resolved_outcomes.call_args_list[1]
    assert yes_call.args[0] == "YES_R2"
    assert yes_call.args[1] is False
    assert no_call.args[0] == "NO_R2"
    assert no_call.args[1] is True


async def test_outcome_backfill_skips_unresolvable_markets(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_db: MagicMock,
):
    """``backfill_outcomes`` skips markets with no parseable outcomePrices.

    Setup: market dict has ``outcomePrices=None``.

    Expected:
      * ``stats.total_added == 0`` (no outcome recorded).
      * ``stats.total_skipped == 1``.
      * ``mark_resolved_outcomes`` NOT called.
    """
    market = _market_dict("0xOUT3", outcome_prices=None)
    market["closed"] = True
    market["active"] = False
    mock_gamma.get_markets.side_effect = [[market], []]

    stats = await engine.backfill_outcomes(resume=False)

    assert stats.total_processed == 1
    assert stats.total_added == 0
    assert stats.total_skipped == 1
    assert mock_db.mark_resolved_outcomes.call_count == 0


# ────────────────────────────────────────────────────────────────────────────
# 4. Deduplication
# ────────────────────────────────────────────────────────────────────────────


async def test_dedup_metadata_idempotent_across_runs(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """Re-running ``backfill_metadata`` produces no duplicate condition_ids.

    The ``backfill_markets`` table has ``condition_id`` as PRIMARY KEY
    and uses ``INSERT OR REPLACE``, so a re-run upserts in-place rather
    than duplicating. The known-condition-ids set should be the same
    after run 2 as it was after run 1.
    """
    markets = [
        _market_dict("0xD1", slug="d1", token_ids=["T1"]),
        _market_dict("0xD2", slug="d2", token_ids=["T2"]),
    ]
    mock_gamma.get_markets.side_effect = [markets, [], markets, []]

    await engine.backfill_metadata(resume=False)
    after_run1 = backfill_store.get_known_condition_ids()

    await engine.backfill_metadata(resume=False)
    after_run2 = backfill_store.get_known_condition_ids()

    assert after_run1 == after_run2 == {"0xD1", "0xD2"}

    # No duplicate rows in the table
    with sqlite3.connect(backfill_store._sqlite_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM backfill_markets")
        assert cur.fetchone()[0] == 2


async def test_dedup_trades_uses_record_trade_unique_constraint(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_clob: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
):
    """Re-running ``backfill_trades`` re-issues ``record_trade`` for the same
    trade_id, but the durable ``ON CONFLICT DO NOTHING`` clause in
    ``timescale_db.record_trade`` is the backstop — so the test asserts
    that ``record_trade`` is called with the same ``trade_id`` both
    times (the dedup is the DB's responsibility, not the engine's).

    Setup: one market, one trade. Run ``backfill_trades`` twice.
    """
    market = _market_dict("0xTD1", slug="td1", token_ids=["TD_TOK"])
    backfill_store.upsert_market(market, backfilled_metadata=True)
    trade = _trade_dict("trade_unique_1", "TD_TOK", price=0.55, size=100)
    mock_clob.get_public_trades.return_value = [trade]

    # ── Run 1 ──
    stats1 = await engine.backfill_trades(days=1, resume=False)
    assert stats1.total_added == 1
    assert mock_db.record_trade.await_count == 1

    # ── Run 2 — re-run with the same trade ──
    # Reset the mock to count only the second run's calls
    mock_db.record_trade.reset_mock()
    mock_db.record_trade.return_value = True
    stats2 = await engine.backfill_trades(days=1, resume=False)

    # The engine re-issues the record_trade call (the engine doesn't
    # maintain a per-trade dedup set; the DB's UNIQUE constraint is the
    # durable backstop). The test asserts that the call IS made with
    # the same trade_id (the dedup happens at the DB layer).
    assert mock_db.record_trade.await_count == 1
    call_kwargs = mock_db.record_trade.await_args.kwargs
    assert call_kwargs.get("trade_id") == "trade_unique_1"


# ────────────────────────────────────────────────────────────────────────────
# 5. Resume / checkpoint
# ────────────────────────────────────────────────────────────────────────────


async def test_resume_metadata_picks_up_from_checkpoint_offset(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """``backfill_metadata(resume=True)`` resumes from the persisted offset.

    Setup: write a checkpoint with ``last_offset=20``. The engine
    should call ``get_markets(offset=20, …)`` on the first call rather
    than ``offset=0``.

    Expected:
      * The first ``get_markets`` call has ``offset=20``.
    """
    cp = BackfillCheckpoint(
        type=BackfillType.METADATA.value,
        last_offset=20,
        last_token_id="",
        last_run_at=time.time(),
        completed=False,
    )
    backfill_store.save_checkpoint(cp)
    mock_gamma.get_markets.return_value = []  # no markets → fast exit

    await engine.backfill_metadata(resume=True)

    first_call = mock_gamma.get_markets.await_args_list[0]
    assert first_call.kwargs.get("offset") == 20


async def test_resume_prices_picks_up_from_checkpoint_token(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    mock_clob: AsyncMock,
    mock_db: MagicMock,
    backfill_store: BackfillStore,
):
    """``backfill_prices(resume=True)`` skips tokens already processed.

    Setup: seed 3 markets with token lists ``[T1]``, ``[T2]``, ``[T3]``.
    Write a checkpoint with ``last_token_id="T2"``. The engine should
    skip T1 and T2 and only process T3.

    Expected:
      * ``mock_clob.get_public_trades`` is called with
        ``token_id="T3"`` (only).
    """
    backfill_store.upsert_market(
        _market_dict("0xR1", slug="r1", token_ids=["T1"]),
        backfilled_metadata=True,
    )
    backfill_store.upsert_market(
        _market_dict("0xR2", slug="r2", token_ids=["T2"]),
        backfilled_metadata=True,
    )
    backfill_store.upsert_market(
        _market_dict("0xR3", slug="r3", token_ids=["T3"]),
        backfilled_metadata=True,
    )

    cp = BackfillCheckpoint(
        type=BackfillType.PRICES.value,
        last_offset=0,
        last_token_id="T2",
        last_run_at=time.time(),
        completed=False,
    )
    backfill_store.save_checkpoint(cp)
    mock_clob.get_public_trades.return_value = []

    await engine.backfill_prices(days=1, resume=True)

    # Only T3 should have been processed (T1 was before T2 in the
    # sort order, T2 is skipped, T3 is the only remaining token).
    tokens_seen = {
        call.kwargs.get("token_id")
        for call in mock_clob.get_public_trades.await_args_list
    }
    assert "T3" in tokens_seen
    assert "T1" not in tokens_seen
    assert "T2" not in tokens_seen


async def test_no_resume_resets_checkpoint(
    engine: BackfillEngine,
    mock_gamma: AsyncMock,
    backfill_store: BackfillStore,
):
    """``backfill_metadata(resume=False)`` clears the persisted checkpoint.

    Setup: write a checkpoint with ``last_offset=99``. Then run with
    ``resume=False``.

    Expected:
      * The first ``get_markets`` call has ``offset=0`` (the checkpoint
        was reset).
    """
    cp = BackfillCheckpoint(
        type=BackfillType.METADATA.value,
        last_offset=99,
        last_run_at=time.time(),
    )
    backfill_store.save_checkpoint(cp)
    mock_gamma.get_markets.return_value = []

    await engine.backfill_metadata(resume=False)

    first_call = mock_gamma.get_markets.await_args_list[0]
    assert first_call.kwargs.get("offset") == 0


async def test_checkpoint_save_and_load_roundtrip(
    backfill_store: BackfillStore,
):
    """``BackfillStore.save_checkpoint`` then ``load_checkpoint`` round-trips."""
    cp = BackfillCheckpoint(
        type="metadata",
        last_offset=42,
        last_token_id="TOK_X",
        last_run_at=1234567.0,
        completed=False,
    )
    backfill_store.save_checkpoint(cp)

    loaded = backfill_store.load_checkpoint("metadata")
    assert loaded is not None
    assert loaded.last_offset == 42
    assert loaded.last_token_id == "TOK_X"
    assert loaded.last_run_at == 1234567.0
    assert loaded.completed is False


# ────────────────────────────────────────────────────────────────────────────
# 6. Rate limit handling
# ────────────────────────────────────────────────────────────────────────────


async def test_rate_limiter_enforces_minimum_interval():
    """``RateLimiter.acquire()`` enforces a minimum interval between calls.

    Setup: target RPS = 10 (min interval = 0.1 s). Call ``acquire``
    twice; measure the wall-clock gap.

    Expected:
      * The gap is ≥ 0.1 s (within a small tolerance for scheduler
        jitter).
    """
    rl = RateLimiter(target_rps=10.0)
    start = time.monotonic()
    await rl.acquire()
    await rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08, (
        f"expected ≥0.08s between two acquire() calls at 10 RPS, "
        f"got {elapsed:.4f}s"
    )


async def test_rate_limiter_backs_off_on_rate_limit_signal():
    """``record_rate_limit()`` multiplies the interval by the backoff factor."""
    rl = RateLimiter(
        target_rps=10.0,  # min interval = 0.1 s
        backoff_factor=2.0,
        max_interval_s=5.0,
    )
    initial = rl.current_interval
    assert initial == pytest.approx(0.1, abs=1e-3)

    rl.record_rate_limit()
    assert rl.current_interval == pytest.approx(0.2, abs=1e-3)
    assert rl.consecutive_rate_limits == 1

    rl.record_rate_limit()
    assert rl.current_interval == pytest.approx(0.4, abs=1e-3)
    assert rl.consecutive_rate_limits == 2


async def test_rate_limiter_caps_at_max_interval():
    """``record_rate_limit()`` is capped at ``max_interval_s``."""
    rl = RateLimiter(
        target_rps=10.0,  # min interval = 0.1 s
        backoff_factor=10.0,
        max_interval_s=1.0,
    )
    # Burn through 5 rate-limit signals — interval grows 0.1 → 1.0 → 1.0 → 1.0
    for _ in range(5):
        rl.record_rate_limit()
    assert rl.current_interval == 1.0  # capped


async def test_rate_limiter_decays_on_success():
    """``record_success()`` halves the current interval back toward the floor."""
    rl = RateLimiter(
        target_rps=10.0,  # min interval = 0.1 s
        backoff_factor=2.0,
        max_interval_s=5.0,
    )
    # Push interval up to 0.4 s
    rl.record_rate_limit()
    rl.record_rate_limit()
    assert rl.current_interval == pytest.approx(0.4, abs=1e-3)

    # One success → decays by backoff factor
    rl.record_success()
    assert rl.current_interval == pytest.approx(0.2, abs=1e-3)

    # Another success → decays back to floor
    rl.record_success()
    assert rl.current_interval == pytest.approx(0.1, abs=1e-3)


async def test_rate_limiter_default_construction():
    """The default :class:`RateLimiter` uses ``DEFAULT_RATE_LIMIT_RPS``."""
    rl = RateLimiter()
    assert rl.current_interval == pytest.approx(1.0 / DEFAULT_RATE_LIMIT_RPS,
                                                  abs=1e-3)


# ────────────────────────────────────────────────────────────────────────────
# 7. CLI: scripts/run_backfill.py
# ────────────────────────────────────────────────────────────────────────────


def test_cli_parse_args_valid_type():
    """The ``--type`` flag accepts all five backfill types plus ``all``."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backfill",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_backfill.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for t in ("metadata", "prices", "trades", "outcomes", "snapshots", "all"):
        args = mod._parse_args(["--type", t])
        assert args.type == t


def test_cli_parse_args_market_and_days():
    """The ``--market`` and ``--days`` flags round-trip."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backfill",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_backfill.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    args = mod._parse_args([
        "--type", "prices",
        "--market", "TOKEN_X",
        "--days", "30",
        "--resolution", "15m",
        "--no-resume",
        "--concurrency", "8",
    ])
    assert args.market == "TOKEN_X"
    assert args.days == 30
    assert args.resolution == "15m"
    assert args.no_resume is True
    assert args.concurrency == 8


def test_cli_parse_args_invalid_type_exits_with_code_2():
    """An invalid ``--type`` value triggers argparse's exit code 2."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backfill",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_backfill.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args(["--type", "bogus"])
    assert exc_info.value.code == 2


def test_cli_parse_args_requires_a_subcommand():
    """Calling the CLI with no subcommand exits with code 2."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backfill",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_backfill.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args([])
    assert exc_info.value.code == 2


def test_cli_list_markets_prints_empty_state(capsys: pytest.CaptureFixture):
    """``--list-markets`` with no markets prints a friendly message and exits 0."""
    import importlib.util

    # Override the env redirect so the CLI's MARKET_DB_PATH points at
    # a fresh temp file (no markets yet).
    tmp_db = Path("/tmp/pmbot_cli_test_list") / "market_intelligence.db"
    tmp_db.parent.mkdir(parents=True, exist_ok=True)
    if tmp_db.exists():
        tmp_db.unlink()
    import os
    os.environ["PMBOT_CLI_TMP_ROOT"] = str(tmp_db.parent)

    spec = importlib.util.spec_from_file_location(
        "run_backfill",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_backfill.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rc = mod.main(["--list-markets"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no markets" in captured.out.lower()


# ────────────────────────────────────────────────────────────────────────────
# 8. BackfillStore schema
# ────────────────────────────────────────────────────────────────────────────


def test_backfill_store_schema_is_idempotent(tmp_path: Path):
    """``BackfillStore.__init__`` is idempotent — re-instantiating doesn't
    raise (``CREATE TABLE IF NOT EXISTS`` is a no-op on the second run).
    """
    p = tmp_path / "bf_idem.db"
    BackfillStore(sqlite_path=p)
    BackfillStore(sqlite_path=p)  # should not raise


def test_backfill_store_upsert_market_preserves_first_seen_at(
    backfill_store: BackfillStore,
):
    """Re-upserting a market preserves its original ``first_seen_at``."""
    market = _market_dict("0xFS1", slug="fs1", token_ids=["T1"])
    assert backfill_store.upsert_market(market)

    with sqlite3.connect(backfill_store._sqlite_path) as conn:
        cur = conn.execute(
            "SELECT first_seen_at FROM backfill_markets WHERE condition_id = ?",
            ("0xFS1",),
        )
        first_seen_1 = cur.fetchone()[0]

    # Sleep a tiny bit so the timestamp would differ if not preserved.
    time.sleep(0.05)
    backfill_store.upsert_market(market)

    with sqlite3.connect(backfill_store._sqlite_path) as conn:
        cur = conn.execute(
            "SELECT first_seen_at, last_updated_at FROM backfill_markets "
            "WHERE condition_id = ?",
            ("0xFS1",),
        )
        first_seen_2, last_updated = cur.fetchone()

    assert first_seen_1 == first_seen_2
    assert last_updated > first_seen_2


def test_backfill_store_record_run_appends_ledger(
    backfill_store: BackfillStore,
):
    """``record_run`` appends a row to the ``backfill_runs`` ledger."""
    stats = BackfillStats(type="metadata")
    stats.total_added = 5
    stats.total_skipped = 2
    stats.total_errors = 1
    stats.mark_done()
    backfill_store.record_run(stats)

    with sqlite3.connect(backfill_store._sqlite_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM backfill_runs")
        assert cur.fetchone()[0] == 1
        cur = conn.execute(
            "SELECT type, total_added, total_skipped, total_errors "
            "FROM backfill_runs"
        )
        row = cur.fetchone()
        assert row == ("metadata", 5, 2, 1)


def test_backfill_store_list_markets_filters_by_state(
    backfill_store: BackfillStore,
):
    """``list_markets`` filters by ``active`` / ``closed`` / ``resolved``."""
    backfill_store.upsert_market(
        _market_dict("0xA1", active=True, closed=False, token_ids=["T1"]),
    )
    backfill_store.upsert_market(
        _market_dict("0xC1", active=False, closed=True, token_ids=["T2"],
                     outcome_prices=["1", "0"]),
    )

    active = backfill_store.list_markets(active=True)
    assert len(active) == 1
    assert active[0]["condition_id"] == "0xA1"

    closed = backfill_store.list_markets(closed=True)
    assert len(closed) == 1
    assert closed[0]["condition_id"] == "0xC1"

    resolved = backfill_store.list_markets(resolved=True)
    assert len(resolved) == 1
    assert resolved[0]["condition_id"] == "0xC1"

    unresolved = backfill_store.list_markets(resolved=False)
    assert len(unresolved) == 1
    assert unresolved[0]["condition_id"] == "0xA1"


# ────────────────────────────────────────────────────────────────────────────
# 9. BackfillType enum
# ────────────────────────────────────────────────────────────────────────────


def test_backfill_type_parse_accepts_strings():
    """``BackfillType.parse`` accepts case-insensitive string forms."""
    assert BackfillType.parse("metadata") == BackfillType.METADATA
    assert BackfillType.parse("PRICES") == BackfillType.PRICES
    assert BackfillType.parse("  Trades  ") == BackfillType.TRADES
    assert BackfillType.parse("all") == BackfillType.ALL


def test_backfill_type_parse_rejects_unknown():
    """``BackfillType.parse`` raises ``ValueError`` on an unknown type."""
    with pytest.raises(ValueError, match="unknown backfill type"):
        BackfillType.parse("bogus")


def test_resolution_seconds_table_has_expected_entries():
    """``RESOLUTION_SECONDS`` exposes the 5 standard candle sizes."""
    assert RESOLUTION_SECONDS["1m"] == 60
    assert RESOLUTION_SECONDS["5m"] == 300
    assert RESOLUTION_SECONDS["15m"] == 900
    assert RESOLUTION_SECONDS["1h"] == 3600
    assert RESOLUTION_SECONDS["1d"] == 86400
