"""
Unit + integration tests for ``core/rejected_opportunities.py`` — W22-4.

Covers the five contract surfaces the W22-4 task spec enumerates:

  (1) ``record_rejected_opportunity()`` / ``RejectedOpportunityStore.record``
      stores every caller-supplied field (``token_id``, ``strategy``,
      ``signal_action``, ``signal_price``, ``signal_size``,
      ``predicted_edge``, ``confidence``, ``rejection_reason`` slug,
      ``rejection_details`` JSON, ``market_price_at_rejection``,
      ``correlation_id``) and returns a non-``None`` row id.
  (2) ``update_outcome()`` computes the counterfactual ``would_have_pnl``
      correctly for BOTH ``BUY`` and ``SELL`` sides — the two governing
      questions ("did the risk system reject good trades?" / "did it
      correctly avoid bad ones?") are answered from this single column
      once the market resolves.
  (3) ``get_recent()`` returns rows most-recent-first (DESC by
      ``timestamp``) and supports filtering by ``rejection_reason`` slug.
  (4) ``get_analytics()`` returns the four top-level keys
      (``total_rejections``, ``by_reason``, ``by_strategy``,
      ``resolved_opportunities``) with the documented sub-fields and
      the correct aggregate values once ``update_outcome`` has
      back-filled the counterfactual P&L.
  (5) API routes — ``GET /api/rejected-opportunities`` and
      ``GET /api/rejected-opportunities/analytics`` return 200 + the
      documented payload on a fresh DB, honour the ``limit`` / ``reason``
      / ``hours`` query params, and reject out-of-range values with 422.

Plus three additive integration tests that exercise the W22-4 risk-manager
wiring:

  (6) ``risk_manager.check_order`` records a rejected opportunity on
      every ``return False, reason`` path — verified by triggering the
      kill-switch gate and asserting a row lands in the store with the
      ``kill_switch`` reason slug and the original English message
      preserved verbatim under ``rejection_details.raw_message``.
  (7) ``_categorize_reason`` maps every canonical rejection message
      produced by ``risk/manager._check_order_impl`` to the short slug
      vocabulary so the analytics roll-up groups by slug rather than by
      interpolated dollar amounts.
  (8) The wiring is fire-and-forget — a transient store failure (e.g.
      the DB file is removed mid-test) does NOT alter the rejection
      return value (``check_order`` still returns ``(False, reason)``
      verbatim — the persistence layer is never on the critical path).

The store reads its DB path from a module-level ``DB_PATH`` constant at
*call time* (every public method resolves ``DB_PATH`` from the module
namespace at *call time*, not at import time — see the
``RejectedOpportunityStore.db_path`` property). Each test monkeypatches
``core.rejected_opportunities.DB_PATH`` to a fresh ``tmp_path``-scoped
SQLite file and (re)initialises the schema, mirroring the
``shadow_db`` fixture in ``tests/test_shadow_trading.py`` (U3) +
``tests/test_shadow_trading_api.py``.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` are not edited per the W22-4 "Do NOT edit existing
files" constraint — mirrors ``tests/test_shadow_trading.py``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# Belt-and-braces with the repo-wide ``tests/conftest.py`` env-var redirect:
# this module-local redirect runs FIRST (before any sibling test file
# imports ``core.rejected_opportunities``) so the module-import-time
# ``_init_db()`` call targets a writable path even if the conftest hasn't
# been loaded yet (e.g. when this file is the first one collected).
_TMP_ROOT = Path("/tmp/rejected_opportunities_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    # The ``core.rejected_opportunities`` module reads
    # ``DECISION_LEDGER_DB_PATH.parent`` for its DB path, so the redirect
    # above suffices to keep the module-import-time ``_init_db()`` off
    # the read-only ``/app/data`` path.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# regardless of the cwd pytest was launched from. Mirrors the bootstrap
# pattern in ``tests/test_risk_manager.py`` and ``tests/test_features.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core import rejected_opportunities  # noqa: E402
from core.data_store import Order, Side, store  # noqa: E402
from core.rejected_opportunities import (  # noqa: E402
    RejectedOpportunity,
    RejectedOpportunityStore,
    _categorize_reason,
    record_rejected_opportunity,
    register_routes,
    rejected_opportunities_store,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# This module mixes async + sync tests (the async tests exercise the
# ``RejectedOpportunityStore`` directly; the sync tests exercise the
# FastAPI routes via ``TestClient``, which is itself synchronous). The
# repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited per the
# W22-4 task constraint ("Do NOT edit existing files"), so we cannot
# enable ``asyncio_mode = "auto"`` via config. Instead we decorate
# each ``async def test_...`` with ``@pytest.mark.asyncio`` individually
# (mirrors the mixed-module pattern in ``tests/test_decision_ledger.py``
# + ``tests/test_decision_ledger_api.py`` — the two-file split is
# collapsed here per the W22-4 single-file requirement). Sync tests
# carry no decorator so pytest-asyncio doesn't try to drive them
# through its own event loop (which would emit a ``PytestWarning``
# about an asyncio mark on a sync function).



# ── Fixture: fresh temp-DB-backed store per test ────────────────────────────
@pytest.fixture
def ro_db(monkeypatch, tmp_path):
    """Point ``core.rejected_opportunities.DB_PATH`` at a fresh ``tmp_path``
    SQLite file and (re)initialise the ``rejected_opportunities`` schema on
    it.

    The module-level ``DB_PATH`` constant is monkeypatched in place —
    the same global-lookup code path every public method in
    ``RejectedOpportunityStore`` uses (the ``db_path`` property resolves
    the live module attribute at *call time*, so monkeypatching
    ``core.rejected_opportunities.DB_PATH`` is picked up automatically
    even when the singleton was constructed before the patch).

    After patching, we explicitly re-run ``rejected_opportunities._init_db()``
    so the ``rejected_opportunities`` table + its four indexes exist on
    the new path; the import-time ``_init_db()`` call only created the
    schema at the conftest-redirected ``/tmp/.../rejected_opportunities.db``
    path, not at this test's ``tmp_path``.

    Mirrors the ``shadow_db`` fixture in ``tests/test_shadow_trading.py``
    (U3) + ``tests/test_shadow_trading_api.py`` so the three test
    modules share an identical isolation contract.
    """
    db_path = tmp_path / "test_rejected_opportunities.db"
    monkeypatch.setattr("core.rejected_opportunities.DB_PATH", db_path)
    rejected_opportunities._init_db()
    return db_path


# ── 1. record_rejected_opportunity() stores all fields ─────────────────────
@pytest.mark.asyncio
async def test_record_stores_all_fields(ro_db):
    """``record_rejected_opportunity`` must persist every caller-supplied
    field verbatim — ``token_id``, ``strategy``, ``signal_action``,
    ``signal_price``, ``signal_size``, ``predicted_edge``, ``confidence``,
    ``rejection_reason`` (slug), ``rejection_details`` (JSON-decodable),
    ``market_price_at_rejection``, ``correlation_id`` — and return a
    non-``None`` integer row id.

    The ``rejection_reason`` argument is a free-text risk-manager message
    ("Daily loss stop reached ($2.00)"); the store must derive the short
    slug ("daily_loss_stop") for the ``rejection_reason`` column (the
    GROUP BY dimension) AND preserve the original message verbatim inside
    ``rejection_details.raw_message`` so the audit trail keeps the
    human-readable text.
    """
    row_id = await record_rejected_opportunity(
        token_id="TOK_FULL",
        strategy="ml_sig_v1",
        signal_action="BUY",
        signal_price=0.55,
        signal_size=100.0,
        predicted_edge=0.08,
        confidence=0.72,
        rejection_reason="Daily loss stop reached ($2.00)",
        rejection_details={"paper": True, "raw_message": "Daily loss stop reached ($2.00)"},
        market_price_at_rejection=0.52,
        correlation_id="dec-full-1",
        timestamp=1_700_000_000.0,
    )

    # The returned row id is a positive integer (autoincrement PK).
    assert row_id is not None
    assert isinstance(row_id, int)
    assert row_id > 0

    rows = await rejected_opportunities_store.get_recent(limit=10)
    assert len(rows) == 1
    r = rows[0]

    # Identity columns persisted verbatim.
    assert r["id"] == row_id
    assert r["token_id"] == "TOK_FULL"
    assert r["strategy"] == "ml_sig_v1"
    assert r["signal_action"] == "BUY"
    assert r["correlation_id"] == "dec-full-1"

    # Numeric columns persisted with full float fidelity.
    assert r["signal_price"] == pytest.approx(0.55)
    assert r["signal_size"] == pytest.approx(100.0)
    assert r["predicted_edge"] == pytest.approx(0.08)
    assert r["confidence"] == pytest.approx(0.72)
    assert r["market_price_at_rejection"] == pytest.approx(0.52)

    # The free-text message was slugified into the GROUP BY column.
    assert r["rejection_reason"] == "daily_loss_stop"

    # The original message is preserved inside rejection_details JSON.
    details = r["rejection_details"]
    assert isinstance(details, dict)
    assert details.get("raw_message") == "Daily loss stop reached ($2.00)"
    assert details.get("paper") is True

    # The market_outcome / would_have_pnl slots are NULL until the
    # market resolves and ``update_outcome`` is called.
    assert r["market_outcome"] is None
    assert r["would_have_pnl"] is None

    # ``timestamp`` was set to the caller-supplied value.
    assert r["timestamp"] == pytest.approx(1_700_000_000.0)


# ── 2. update_outcome() computes would_have_pnl for BUY + SELL ────────────
@pytest.mark.asyncio
async def test_update_outcome_computes_counterfactual_pnl_for_buy_and_sell(ro_db):
    """``update_outcome(token_id, final_price, outcome)`` must back-fill
    the ``market_outcome`` (1=YES / 0=NO) AND the counterfactual
    ``would_have_pnl`` for every prior rejection on that token. The
    counterfactual formula is::

        BUY : would_have_pnl = (final_price - signal_price) * signal_size
        SELL: would_have_pnl = (signal_price - final_price) * signal_size

    A positive ``would_have_pnl`` means the risk system COST P&L by
    rejecting the trade (a missed winner). A negative value means the
    rejection SAVED capital (correctly avoided a losing trade).

    This test seeds THREE rejections on the same token — two BUY
    (one would-have-won, one would-have-lost) and one SELL (would-have-
    won) — then resolves the market to ``final_price=1.0`` (YES) and
    asserts each row's counterfactual P&L matches the formula.
    """
    # ── Seed three rejections on token "TOK_RESOLVE" ────────────────────
    # BUY @ 0.40, size=100 → resolves to 1.0 → would_have_pnl = (1.0-0.4)*100 = +60 (missed winner)
    rid1 = await record_rejected_opportunity(
        token_id="TOK_RESOLVE",
        strategy="alpha",
        signal_action="BUY",
        signal_price=0.40,
        signal_size=100.0,
        predicted_edge=0.10,
        confidence=0.65,
        rejection_reason="Cash reserve breach: total exposure $61.50 exceeds deployable capital $60.00",
        rejection_details={},
        market_price_at_rejection=0.40,
        timestamp=1_700_000_001.0,
    )
    assert rid1 is not None

    # BUY @ 0.80, size=50 → resolves to 1.0 → would_have_pnl = (1.0-0.8)*50 = +10 (missed winner)
    rid2 = await record_rejected_opportunity(
        token_id="TOK_RESOLVE",
        strategy="alpha",
        signal_action="BUY",
        signal_price=0.80,
        signal_size=50.0,
        predicted_edge=0.05,
        confidence=0.55,
        rejection_reason="Max drawdown limit reached ($8.00)",
        rejection_details={},
        market_price_at_rejection=0.80,
        timestamp=1_700_000_002.0,
    )
    assert rid2 is not None

    # SELL @ 0.90, size=20 → resolves to 1.0 → would_have_pnl = (0.9-1.0)*20 = -2 (correctly avoided — would have LOST)
    rid3 = await record_rejected_opportunity(
        token_id="TOK_RESOLVE",
        strategy="beta",
        signal_action="SELL",
        signal_price=0.90,
        signal_size=20.0,
        predicted_edge=0.03,
        confidence=0.50,
        rejection_reason="Max open orders (50) reached",
        rejection_details={},
        market_price_at_rejection=0.90,
        timestamp=1_700_000_003.0,
    )
    assert rid3 is not None

    # ── Resolve the market: YES (outcome=1, final_price=1.0) ──────────
    updated = await rejected_opportunities_store.update_outcome(
        token_id="TOK_RESOLVE",
        final_price=1.0,
        outcome=1,
    )
    assert updated == 3, f"expected 3 rows updated, got {updated}"

    # ── Verify the counterfactual P&L on each row ─────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=10)
    # Most-recent-first: rid3 (SELL @ 0.90), rid2 (BUY @ 0.80), rid1 (BUY @ 0.40)
    by_id = {r["id"]: r for r in rows}
    assert len(by_id) == 3

    # BUY @ 0.40, size 100 → would_have_pnl = (1.0 - 0.40) * 100 = +60.0 (missed winner)
    assert by_id[rid1]["market_outcome"] == 1
    assert by_id[rid1]["would_have_pnl"] == pytest.approx(60.0)

    # BUY @ 0.80, size 50 → would_have_pnl = (1.0 - 0.80) * 50 = +10.0 (missed winner)
    assert by_id[rid2]["market_outcome"] == 1
    assert by_id[rid2]["would_have_pnl"] == pytest.approx(10.0)

    # SELL @ 0.90, size 20 → would_have_pnl = (0.90 - 1.0) * 20 = -2.0 (correctly avoided)
    assert by_id[rid3]["market_outcome"] == 1
    assert by_id[rid3]["would_have_pnl"] == pytest.approx(-2.0)

    # ── Calling update_outcome again is idempotent (no new updates) ──
    # The WHERE clause filters ``market_outcome IS NULL``, so already-
    # resolved rows are skipped on the second pass.
    second_updated = await rejected_opportunities_store.update_outcome(
        token_id="TOK_RESOLVE",
        final_price=1.0,
        outcome=1,
    )
    assert second_updated == 0, (
        f"second update_outcome should be a no-op (rows already resolved), "
        f"got {second_updated}"
    )


# ── 3. get_recent() returns most-recent-first + supports reason filter ─────
@pytest.mark.asyncio
async def test_get_recent_returns_most_recent_first_and_supports_reason_filter(ro_db):
    """``get_recent`` must return rows in DESCENDING ``timestamp`` order
    (most recent first) — the ordering the HTTP
    ``GET /api/rejected-opportunities`` endpoint promises its callers.

    ``record_rejected_opportunity`` accepts a caller-supplied
    ``timestamp``, so we can seed rows with deterministic timestamps
    (no need for the 5 ms ``asyncio.sleep`` trick the
    ``shadow_trading`` tests use).

    The ``reason`` filter narrows to a single ``rejection_reason`` slug;
    ``reason=None`` / ``""`` returns across all reasons.
    """
    # ── Seed 4 rows: 2 daily_loss_stop + 1 kill_switch + 1 max_drawdown ─
    for i, (reason, ts) in enumerate([
        ("Daily loss stop reached ($2.00)", 1_700_000_010.0),  # newest
        ("Kill switch is active — all trading halted", 1_700_000_009.0),
        ("Max drawdown limit reached ($8.00)", 1_700_000_008.0),
        ("Daily loss stop reached ($2.00)", 1_700_000_007.0),  # oldest
    ]):
        rid = await record_rejected_opportunity(
            token_id=f"TOK_ORDER_{i}",
            strategy="alpha",
            signal_action="BUY",
            signal_price=0.50,
            signal_size=10.0,
            predicted_edge=0.02,
            confidence=0.55,
            rejection_reason=reason,
            rejection_details={},
            market_price_at_rejection=0.50,
            timestamp=ts,
        )
        assert rid is not None

    # ── No filter: 4 rows, most-recent-first ──────────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=50)
    assert len(rows) == 4
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True), (
        f"expected DESC timestamps, got {timestamps}"
    )
    # The newest row is the daily_loss_stop one at ts=1_700_000_010.
    assert rows[0]["timestamp"] == pytest.approx(1_700_000_010.0)
    assert rows[0]["rejection_reason"] == "daily_loss_stop"

    # ── Filter by reason="daily_loss_stop": 2 rows ─────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=50, reason="daily_loss_stop")
    assert len(rows) == 2
    assert all(r["rejection_reason"] == "daily_loss_stop" for r in rows)

    # ── Filter by reason="kill_switch": 1 row ─────────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=50, reason="kill_switch")
    assert len(rows) == 1
    assert rows[0]["rejection_reason"] == "kill_switch"

    # ── Filter by an unknown reason slug: 0 rows ──────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=50, reason="nonexistent_slug")
    assert len(rows) == 0

    # ── limit parameter caps the row count ────────────────────────────
    rows = await rejected_opportunities_store.get_recent(limit=2)
    assert len(rows) == 2
    # Most-recent-first still holds within the slice.
    assert rows[0]["timestamp"] >= rows[1]["timestamp"]


# ── 4. get_analytics() returns the documented payload + correct roll-up ──
@pytest.mark.asyncio
async def test_get_analytics_returns_documented_payload_and_correct_rollup(ro_db):
    """``get_analytics(hours)`` must return a payload with the four
    documented top-level keys (``total_rejections``, ``by_reason``,
    ``by_strategy``, ``resolved_opportunities``) AND the correct
    aggregate values once ``update_outcome`` has back-filled the
    counterfactual P&L.

    The ``resolved_opportunities`` roll-up is the load-bearing piece —
    it answers the two governing questions ("did the risk system
    reject good trades?" / "did it correctly avoid bad ones?") from
    the SAME ``would_have_pnl`` column.
    """
    # ── Seed 4 rejections: 2 on TOK_A (1 winner, 1 loser), 2 on TOK_B ─
    # TOK_A row 1: BUY @ 0.40 size 100, will resolve to 1.0 → +60 (missed winner)
    await record_rejected_opportunity(
        token_id="TOK_A",
        strategy="alpha",
        signal_action="BUY",
        signal_price=0.40,
        signal_size=100.0,
        predicted_edge=0.10,
        confidence=0.65,
        rejection_reason="Daily loss stop reached ($2.00)",
        rejection_details={},
        market_price_at_rejection=0.40,
        timestamp=time.time() - 60.0,  # 1 minute ago — inside the 24h window
    )
    # TOK_A row 2: BUY @ 0.80 size 50, will resolve to 1.0 → +10 (missed winner)
    await record_rejected_opportunity(
        token_id="TOK_A",
        strategy="beta",
        signal_action="BUY",
        signal_price=0.80,
        signal_size=50.0,
        predicted_edge=0.05,
        confidence=0.55,
        rejection_reason="Max drawdown limit reached ($8.00)",
        rejection_details={},
        market_price_at_rejection=0.80,
        timestamp=time.time() - 30.0,  # 30 seconds ago
    )
    # TOK_B row 1: SELL @ 0.90 size 20, will resolve to 1.0 → -2 (correctly avoided)
    await record_rejected_opportunity(
        token_id="TOK_B",
        strategy="alpha",
        signal_action="SELL",
        signal_price=0.90,
        signal_size=20.0,
        predicted_edge=0.03,
        confidence=0.50,
        rejection_reason="Kill switch is active — all trading halted",
        rejection_details={},
        market_price_at_rejection=0.90,
        timestamp=time.time() - 15.0,  # 15 seconds ago
    )
    # TOK_B row 2: BUY @ 0.30 size 80, will resolve to 0.0 → -24 (correctly avoided)
    await record_rejected_opportunity(
        token_id="TOK_B",
        strategy="beta",
        signal_action="BUY",
        signal_price=0.30,
        signal_size=80.0,
        predicted_edge=0.04,
        confidence=0.60,
        rejection_reason="Daily loss stop reached ($2.00)",
        rejection_details={},
        market_price_at_rejection=0.30,
        timestamp=time.time() - 5.0,  # 5 seconds ago
    )

    # ── Before resolution: analytics has the 4 rejections, none resolved ─
    analytics = await rejected_opportunities_store.get_analytics(hours=24)
    assert analytics["total_rejections"] == 4
    assert analytics["resolved_opportunities"]["total"] == 0
    assert analytics["resolved_opportunities"]["would_have_won"] == 0
    assert analytics["resolved_opportunities"]["total_would_have_pnl"] == pytest.approx(0.0)
    assert analytics["period_hours"] == pytest.approx(24.0)

    # by_reason: 2 daily_loss_stop + 1 kill_switch + 1 max_drawdown
    by_reason = {r["rejection_reason"]: r for r in analytics["by_reason"]}
    assert by_reason["daily_loss_stop"]["count"] == 2
    assert by_reason["kill_switch"]["count"] == 1
    assert by_reason["max_drawdown"]["count"] == 1

    # by_strategy: alpha=2, beta=2
    by_strategy = {r["strategy"]: r for r in analytics["by_strategy"]}
    assert by_strategy["alpha"]["count"] == 2
    assert by_strategy["beta"]["count"] == 2

    # ── Resolve TOK_A: YES (final_price=1.0) ──────────────────────────
    # → TOK_A row 1: would_have_pnl = (1.0 - 0.40) * 100 = +60.0 (winner)
    # → TOK_A row 2: would_have_pnl = (1.0 - 0.80) * 50 = +10.0 (winner)
    await rejected_opportunities_store.update_outcome(
        token_id="TOK_A", final_price=1.0, outcome=1,
    )

    # ── Resolve TOK_B: NO (final_price=0.0) ────────────────────────────
    # → TOK_B row 1 (SELL @ 0.90, size 20): would_have_pnl = (0.90 - 0.0) * 20 = +18.0 (winner)
    # → TOK_B row 2 (BUY @ 0.30, size 80): would_have_pnl = (0.0 - 0.30) * 80 = -24.0 (loser, correctly avoided)
    await rejected_opportunities_store.update_outcome(
        token_id="TOK_B", final_price=0.0, outcome=0,
    )

    # ── After resolution: 4 resolved, 3 winners + 1 loser ───────────────
    # total_would_have_pnl = +60 + +10 + +18 + -24 = +64.0
    # The risk system COST $64 of P&L over the window (rejected more
    # winners than losers). The single loser it correctly avoided was
    # the $24 BUY on TOK_B (saved $24).
    analytics = await rejected_opportunities_store.get_analytics(hours=24)
    assert analytics["total_rejections"] == 4  # unchanged — still counts all
    resolved = analytics["resolved_opportunities"]
    assert resolved["total"] == 4
    assert resolved["would_have_won"] == 3
    assert resolved["total_would_have_pnl"] == pytest.approx(64.0)
    assert resolved["avg_would_have_pnl"] == pytest.approx(16.0)  # 64 / 4


# ── 4b. get_analytics() respects the hours window ─────────────────────────
@pytest.mark.asyncio
async def test_get_analytics_respects_hours_window(ro_db):
    """``get_analytics(hours=N)`` must only count rejections whose
    ``timestamp > time.time() - N * 3600``. Older rejections are excluded
    from ``total_rejections`` AND from the ``by_reason`` / ``by_strategy``
    roll-ups — so an operator asking "what did we reject in the last
    hour?" doesn't see last week's rejections pollute the roll-up.
    """
    now = time.time()
    # ── Seed: one old rejection (>2h ago) + one recent (<1h ago) ─────
    await record_rejected_opportunity(
        token_id="TOK_OLD",
        strategy="alpha",
        signal_action="BUY",
        signal_price=0.40,
        signal_size=10.0,
        predicted_edge=0.05,
        confidence=0.60,
        rejection_reason="Daily loss stop reached ($2.00)",
        rejection_details={},
        market_price_at_rejection=0.40,
        timestamp=now - 7200.0 - 60.0,  # 2h 1m ago — outside the 2h window
    )
    await record_rejected_opportunity(
        token_id="TOK_NEW",
        strategy="beta",
        signal_action="BUY",
        signal_price=0.50,
        signal_size=20.0,
        predicted_edge=0.04,
        confidence=0.55,
        rejection_reason="Kill switch is active — all trading halted",
        rejection_details={},
        market_price_at_rejection=0.50,
        timestamp=now - 60.0,  # 1 minute ago — inside every window
    )

    # 1-hour window: only the recent row counts.
    analytics_1h = await rejected_opportunities_store.get_analytics(hours=1)
    assert analytics_1h["total_rejections"] == 1
    by_reason_1h = {r["rejection_reason"]: r for r in analytics_1h["by_reason"]}
    assert "kill_switch" in by_reason_1h
    assert "daily_loss_stop" not in by_reason_1h

    # 3-hour window: both rows count.
    analytics_3h = await rejected_opportunities_store.get_analytics(hours=3)
    assert analytics_3h["total_rejections"] == 2


# ── 5. API routes — 200 + documented payload on a fresh DB ──────────────
@pytest.fixture
def api_client(ro_db):
    """Fresh ``FastAPI`` app with only the rejected-opportunity routes
    registered.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` so the route definitions / validation annotations
    exercised here are byte-identical to what the live server exposes —
    without the bearer-token auth middleware (``enforce_api_auth`` — a
    server-level concern exercised by separate auth tests) or the heavy
    ``lifespan`` startup (TimescaleDB, paper_sim, market seeding, watchdog)
    which would make the suite slow and brittle.

    The default ``FastAPI()`` constructor adds no lifespan, so
    ``TestClient`` requests don't trigger any startup side effects.
    Mirrors the ``client`` fixture in ``tests/test_shadow_trading_api.py``.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _seed_sync(*rows: dict) -> None:
    """Sync wrapper around ``record_rejected_opportunity`` so sync API
    tests can seed the SQLite file before issuing ``TestClient`` requests.

    Each positional arg is a ``dict`` of the kwargs to
    ``record_rejected_opportunity``. Runs all inserts inside a single
    ``asyncio.run`` call (one fresh event loop); each insert commits
    inside its ``with sqlite3.connect(DB_PATH) as conn:`` context
    manager before its coroutine returns, so by the time
    ``asyncio.run`` returns the rows are durable on disk — visible to
    the TestClient's portal-side event loop on the next
    ``client.get(...)``.

    Mirrors the ``_seed`` helper in
    ``tests/test_shadow_trading_api.py``.
    """
    async def _seed_all() -> None:
        for r in rows:
            row_id = await record_rejected_opportunity(**r)
            assert row_id is not None and row_id > 0, (
                f"seed insert failed for row={r!r} — "
                f"record_rejected_opportunity returned {row_id!r}"
            )

    asyncio.run(_seed_all())


def test_api_list_returns_200_with_empty_list_initially(api_client):
    """GET /api/rejected-opportunities on a fresh DB must return HTTP 200
    with ``count=0`` and an empty ``opportunities`` list. The ``ro_db``
    fixture's ``tmp_path`` SQLite file is brand-new, so the
    ``rejected_opportunities`` table has zero rows — the read path must
    NOT 500 on an empty table.
    """
    response = api_client.get("/api/rejected-opportunities")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["opportunities"] == []


def test_api_list_with_reason_filter_returns_200(api_client):
    """GET /api/rejected-opportunities?reason=kill_switch must return 200 —
    the reason filter is a no-op on an empty DB (returns ``count=0``,
    ``opportunities=[]``) rather than erroring.
    """
    response = api_client.get(
        "/api/rejected-opportunities", params={"reason": "kill_switch"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["opportunities"] == []


def test_api_analytics_returns_200_with_documented_payload(api_client):
    """GET /api/rejected-opportunities/analytics must return 200 with a
    payload carrying the four documented top-level keys
    (``total_rejections``, ``by_reason``, ``by_strategy``,
    ``resolved_opportunities``) plus the ``period_hours`` echo.
    """
    response = api_client.get("/api/rejected-opportunities/analytics")
    assert response.status_code == 200
    body = response.json()

    # Top-level shape.
    assert "total_rejections" in body
    assert "by_reason" in body
    assert "by_strategy" in body
    assert "resolved_opportunities" in body
    assert "period_hours" in body

    # Fresh DB → zeroed-out roll-up.
    assert body["total_rejections"] == 0
    assert body["by_reason"] == []
    assert body["by_strategy"] == []

    resolved = body["resolved_opportunities"]
    assert resolved["total"] == 0
    assert resolved["would_have_won"] == 0
    assert resolved["total_would_have_pnl"] == pytest.approx(0.0)
    assert resolved["avg_would_have_pnl"] == pytest.approx(0.0)

    # Default period is 24h.
    assert body["period_hours"] == pytest.approx(24.0)


def test_api_list_returns_seeded_rows_most_recent_first(api_client):
    """GET /api/rejected-opportunities must return seeded rows
    most-recent-first (DESC by ``timestamp``) — the ordering the API
    docstring promises. Also verifies the ``rejection_details`` JSON
    column is decoded (not returned as a raw string).
    """
    _seed_sync(*[
        dict(
            token_id="TOK_API_1",
            strategy="alpha",
            signal_action="BUY",
            signal_price=0.40,
            signal_size=100.0,
            predicted_edge=0.08,
            confidence=0.65,
            rejection_reason="Daily loss stop reached ($2.00)",
            rejection_details={"paper": True},
            market_price_at_rejection=0.40,
            timestamp=1_700_000_010.0,  # newest
        ),
        dict(
            token_id="TOK_API_2",
            strategy="beta",
            signal_action="SELL",
            signal_price=0.50,
            signal_size=20.0,
            predicted_edge=0.02,
            confidence=0.55,
            rejection_reason="Kill switch is active — all trading halted",
            rejection_details={},
            market_price_at_rejection=0.50,
            timestamp=1_700_000_005.0,  # older
        ),
    ])

    response = api_client.get("/api/rejected-opportunities")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2

    rows = body["opportunities"]
    # Most-recent-first: TOK_API_1 (ts=1_700_000_010) before TOK_API_2.
    assert rows[0]["token_id"] == "TOK_API_1"
    assert rows[1]["token_id"] == "TOK_API_2"

    # The rejection_reason column is the SHORT SLUG, not the original message.
    assert rows[0]["rejection_reason"] == "daily_loss_stop"
    assert rows[1]["rejection_reason"] == "kill_switch"

    # The ``rejection_details`` JSON column is decoded (not a raw string).
    assert isinstance(rows[0]["rejection_details"], dict)
    assert rows[0]["rejection_details"].get("paper") is True
    assert rows[0]["rejection_details"].get("raw_message") == "Daily loss stop reached ($2.00)"


def test_api_list_limit_parameter_honored(api_client):
    """The ``limit`` query param (declared ``Query(50, ge=1, le=1000)``
    on the route signature) must cap the number of rows returned.
    """
    # Seed 3 rows.
    _seed_sync(*[
        dict(
            token_id=f"TOK_LIMIT_{i}",
            strategy="alpha",
            signal_action="BUY",
            signal_price=0.40,
            signal_size=10.0,
            predicted_edge=0.05,
            confidence=0.55,
            rejection_reason="Daily loss stop reached ($2.00)",
            rejection_details={},
            market_price_at_rejection=0.40,
            timestamp=1_700_000_000.0 + i,
        )
        for i in range(3)
    ])

    # Sanity: an unfiltered request returns all 3 rows.
    sanity = api_client.get("/api/rejected-opportunities", params={"limit": 50})
    assert sanity.status_code == 200
    assert sanity.json()["count"] == 3

    # The actual limit test: limit=2 → exactly 2 rows, most-recent-first.
    response = api_client.get("/api/rejected-opportunities", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    # Most-recent-first: TOK_LIMIT_2 (ts=1_700_000_002) at index 0.
    assert body["opportunities"][0]["token_id"] == "TOK_LIMIT_2"


@pytest.mark.parametrize(
    "bad_limit, reason",
    [
        (0, "ge=1 violation (zero)"),
        (-1, "ge=1 violation (negative)"),
        (1001, "le=1000 violation"),
        ("abc", "non-int-coercible string"),
        ("1.5", "float-string not coercible to int"),
    ],
)
def test_api_list_invalid_limit_returns_422(api_client, bad_limit, reason):
    """An out-of-range or non-integer ``limit`` must trigger FastAPI's
    422 Unprocessable Entity response.

    The route signature ``limit: int = Query(50, ge=1, le=1000)``
    enforces three independent constraints at the framework layer
    (before the handler runs): int type, ge=1, le=1000.
    """
    response = api_client.get(
        "/api/rejected-opportunities", params={"limit": bad_limit}
    )
    assert response.status_code == 422, (
        f"expected 422 for bad_limit={bad_limit!r} ({reason}), got "
        f"{response.status_code}: {response.text}"
    )


def test_api_analytics_hours_parameter_honored(api_client):
    """GET /api/rejected-opportunities/analytics?hours=N must echo the
    ``period_hours`` field back AND scope the roll-up to the trailing
    ``N`` hour window.
    """
    # Seed one row inside the 1h window and one outside.
    now = time.time()
    _seed_sync(*[
        dict(
            token_id="TOK_HOURS_NEW",
            strategy="alpha",
            signal_action="BUY",
            signal_price=0.40,
            signal_size=10.0,
            predicted_edge=0.05,
            confidence=0.55,
            rejection_reason="Daily loss stop reached ($2.00)",
            rejection_details={},
            market_price_at_rejection=0.40,
            timestamp=now - 60.0,  # 1 minute ago
        ),
        dict(
            token_id="TOK_HOURS_OLD",
            strategy="beta",
            signal_action="BUY",
            signal_price=0.50,
            signal_size=20.0,
            predicted_edge=0.04,
            confidence=0.55,
            rejection_reason="Kill switch is active — all trading halted",
            rejection_details={},
            market_price_at_rejection=0.50,
            timestamp=now - 7200.0 - 60.0,  # 2h 1m ago
        ),
    ])

    # 1-hour window: only the recent row counts.
    response = api_client.get(
        "/api/rejected-opportunities/analytics", params={"hours": 1.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_rejections"] == 1
    assert body["period_hours"] == pytest.approx(1.0)
    by_reason = {r["rejection_reason"]: r for r in body["by_reason"]}
    assert "daily_loss_stop" in by_reason
    assert "kill_switch" not in by_reason

    # 3-hour window: both rows count.
    response = api_client.get(
        "/api/rejected-opportunities/analytics", params={"hours": 3.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_rejections"] == 2
    assert body["period_hours"] == pytest.approx(3.0)


@pytest.mark.parametrize(
    "bad_hours, reason",
    [
        (-1.0, "ge=0 violation (negative)"),
        (721.0, "le=720 violation"),
        ("abc", "non-float-coercible string"),
    ],
)
def test_api_analytics_invalid_hours_returns_422(api_client, bad_hours, reason):
    """An out-of-range or non-numeric ``hours`` must trigger FastAPI's
    422 Unprocessable Entity response.

    The route signature ``hours: float = Query(24.0, ge=0.0, le=720.0)``
    enforces three independent constraints at the framework layer:
    float type, ge=0.0, le=720.0.
    """
    response = api_client.get(
        "/api/rejected-opportunities/analytics", params={"hours": bad_hours}
    )
    assert response.status_code == 422, (
        f"expected 422 for bad_hours={bad_hours!r} ({reason}), got "
        f"{response.status_code}: {response.text}"
    )


# ── 6. _categorize_reason maps every canonical risk message to a slug ─────
def test_categorize_reason_maps_canonical_messages_to_slugs():
    """``_categorize_reason`` must map every canonical rejection message
    produced by ``risk/manager._check_order_impl`` to a short slug so the
    analytics roll-up groups by slug rather than by interpolated dollar
    amounts. The mapping is intentionally defensive — any unmapped
    message falls back to ``"other"`` so the roll-up never silently
    drops a category.
    """
    # (message_fragment, expected_slug)
    cases = [
        ("Shadow trading mode active — evaluation only, no orders", "shadow_mode"),
        ("Kill switch is active — all trading halted", "kill_switch"),
        ("Observation-only mode active (foo) — new live orders disabled", "observation_only"),
        ("Live trading is disabled by default — enable explicitly to trade real funds", "live_trading_disabled"),
        ("Strategy 'foo' is in per-trade-loss cooldown (60s remaining)", "strategy_cooldown"),
        ("Daily loss stop reached ($2.00)", "daily_loss_stop"),
        ("Weekly loss stop reached ($5.00)", "weekly_loss_stop"),
        ("Max drawdown limit reached ($8.00)", "max_drawdown"),
        ("Cash reserve breach: total exposure $61.50 exceeds deployable capital $60.00", "cash_reserve"),
        ("Total open risk cap exceeded ($25.50 > $25.00)", "total_open_risk"),
        ("Absolute position cap exceeded ($5.50 > $5.00)", "absolute_position_cap"),
        ("Normal position cap exceeded for new position ($2.50 > $2.00, scale=60%)", "normal_position_cap"),
        ("Per-market position cap exceeded ($3.50 > $3.00, scale=100%)", "per_market_cap"),
        ("Strategy exposure cap exceeded ($15.50 > $15.00)", "strategy_exposure"),
        ("Correlated exposure cap exceeded ($8.50 > $8.00)", "correlated_exposure"),
        ("Mark-to-market exposure $24.50 + order $1.50 exceeds $25.00 cap", "mtm_exposure"),
        ("MTM risk gate failed closed (...) — all trades blocked", "mtm_gate_failed"),
        ("Max simultaneous open positions (8) reached", "max_open_positions"),
        ("Pending order capital cap exceeded ($10.50 > $10.00)", "pending_order_capital"),
        ("Max open orders (50) reached", "max_open_orders"),
        ("Price 1.50 out of valid bounds [0.01, 0.99]", "invalid_price"),
        ("Order size 0.3 is below minimum liquidity threshold", "insufficient_size"),
        ("Order would put max possible loss $160.00 above the deployable bankroll ceiling $160.00", "bankroll_ceiling"),
        # Unmapped message → "other" (defensive fallback).
        ("Some brand-new rejection reason we haven't seen yet", "other"),
        # Empty / None inputs.
        ("", "other"),
    ]
    for message, expected_slug in cases:
        actual = _categorize_reason(message)
        assert actual == expected_slug, (
            f"_categorize_reason({message!r}) = {actual!r}, expected {expected_slug!r}"
        )


# ── 7. Risk-manager wiring records a rejection on the kill-switch path ────
@pytest.mark.asyncio
async def test_risk_manager_wiring_records_rejected_opportunity_on_kill_switch(ro_db, monkeypatch):
    """``risk_manager.check_order`` must record a rejected-opportunity
    entry on every ``return False, reason`` path. Verified by
    triggering the kill-switch gate (``store.kill_switch_active = True``)
    and asserting a row lands in the store with the ``kill_switch``
    reason slug + the original English message preserved verbatim under
    ``rejection_details.raw_message``.

    The wiring is fire-and-forget (``asyncio.create_task``) so the test
    must yield to the event loop briefly to let the scheduled task
    finish before reading the store.
    """
    from core.safety import kill_switch_file_exists
    from risk.manager import risk_manager

    # Belt-and-braces: neutralize the durable kill-switch file check
    # so the gate under test is the in-memory flag (not the file).
    monkeypatch.setattr("core.safety.kill_switch_file_exists", lambda: False)
    assert kill_switch_file_exists() is False

    # Reset the global ``store`` to a clean baseline (kill switch off,
    # paper balance at $100, peak equity at $100, no positions/orders).
    store.kill_switch_active = False
    store.daily_pnl = 0.0
    store.weekly_pnl = 0.0
    store.peak_equity = 100.0
    store.paper_balance = 100.0
    store.open_orders.clear()
    store.positions.clear()
    store.trades.clear()
    store.market_slugs.clear()
    store.order_books.clear()
    store.event_log.clear()

    # ── Stage 1: baseline — no rejections recorded yet ────────────────
    rows = await rejected_opportunities_store.get_recent(limit=10)
    assert rows == [], f"expected empty store on fresh DB, got {len(rows)} rows"

    # ── Stage 2: arm the in-memory kill switch ────────────────────────
    store.kill_switch_active = True

    # ── Stage 3: build a paper BUY order + check_order ───────────────
    order = Order(
        order_id="order-w22-4-test-1",
        token_id="TOK_W22_4_TEST",
        side=Side.BUY,
        price=0.50,
        size=3.0,
        strategy="w22_4_test_strategy",
        paper=True,
        decision_id="dec-w22-4-test-1",
    )
    allowed, reason = await risk_manager.check_order(order)

    # ── Stage 4: assert the rejection was returned verbatim ──────────
    assert allowed is False
    assert reason == "Kill switch is active — all trading halted"

    # ── Stage 5: yield to the event loop so the fire-and-forget
    # ``asyncio.create_task(record_rejected_opportunity(...))`` finishes
    # before we read the store. A 0.05 s sleep is generous — the
    # underlying SQLite write is sub-millisecond.
    await asyncio.sleep(0.05)

    # ── Stage 6: assert a row landed in the store with the expected fields ──
    rows = await rejected_opportunities_store.get_recent(limit=10)
    assert len(rows) == 1, (
        f"expected exactly 1 rejected opportunity recorded, got {len(rows)} "
        f"(fire-and-forget task may not have completed — try increasing the sleep)"
    )
    r = rows[0]

    # Identity columns propagated from the Order.
    assert r["token_id"] == "TOK_W22_4_TEST"
    assert r["strategy"] == "w22_4_test_strategy"
    assert r["signal_action"] == "BUY"
    assert r["signal_price"] == pytest.approx(0.50)
    assert r["signal_size"] == pytest.approx(3.0)
    assert r["correlation_id"] == "dec-w22-4-test-1"

    # The free-text rejection message was slugified into the GROUP BY column.
    assert r["rejection_reason"] == "kill_switch"

    # The original English message is preserved verbatim under raw_message.
    details = r["rejection_details"]
    assert isinstance(details, dict)
    assert details.get("raw_message") == "Kill switch is active — all trading halted"
    assert details.get("paper") is True

    # No order-book was seeded, so market_price_at_rejection is NULL.
    assert r["market_price_at_rejection"] is None


# ── 8. Wiring is fire-and-forget — store failure doesn't alter rejection ─
@pytest.mark.asyncio
async def test_risk_manager_wiring_is_fire_and_forget(ro_db, monkeypatch):
    """A transient store failure (e.g. the DB file is removed mid-test)
    must NOT alter the rejection return value — ``check_order`` still
    returns ``(False, reason)`` verbatim. The persistence layer is
    never on the critical path; a store hiccup is logged but cannot
    block or change the trading-pipeline decision.

    Verified by:
      1. Monkeypatching ``record_rejected_opportunity`` to raise.
      2. Triggering a rejection (kill switch active).
      3. Asserting ``check_order`` returns ``(False, reason)`` verbatim.

    The ``asyncio.create_task`` wrapping in the wiring swallows the
    exception via the outer ``try/except: pass``, so the rejection
    return path is never perturbed.
    """
    from risk.manager import risk_manager

    # Neutralize the durable kill-switch file check.
    monkeypatch.setattr("core.safety.kill_switch_file_exists", lambda: False)

    # Reset ``store`` to a clean baseline.
    store.kill_switch_active = False
    store.daily_pnl = 0.0
    store.weekly_pnl = 0.0
    store.peak_equity = 100.0
    store.paper_balance = 100.0
    store.open_orders.clear()
    store.positions.clear()

    # ── Monkeypatch ``record_rejected_opportunity`` to raise ──────────
    # The wiring imports ``record_rejected_opportunity`` lazily inside
    # ``check_order`` via ``from core.rejected_opportunities import
    # record_rejected_opportunity``. To make the raise observable, we
    # patch the module-level attribute so the lazy import picks up our
    # raising stub.
    async def _raising_stub(**_kwargs):
        raise RuntimeError("simulated store failure (W22-4 fire-and-forget test)")

    monkeypatch.setattr(
        "core.rejected_opportunities.record_rejected_opportunity",
        _raising_stub,
    )

    # ── Arm the kill switch + submit a paper order ────────────────────
    store.kill_switch_active = True
    order = Order(
        order_id="order-w22-4-fire-forget",
        token_id="TOK_W22_4_FF",
        side=Side.BUY,
        price=0.50,
        size=3.0,
        strategy="w22_4_ff_strategy",
        paper=True,
    )

    # ── check_order must STILL return the verbatim rejection ──────────
    allowed, reason = await risk_manager.check_order(order)
    assert allowed is False
    assert reason == "Kill switch is active — all trading halted"

    # Yield to let any pending ``asyncio.create_task`` finish (the
    # raising stub is called inside the scheduled task; the outer
    # try/except swallows it).
    await asyncio.sleep(0.05)

    # The store should have ZERO rows (the raising stub prevented the
    # insert) — confirming the fire-and-forget contract: a store
    # failure is logged but never blocks the rejection return.
    rows = await rejected_opportunities_store.get_recent(limit=10)
    assert rows == [], (
        f"expected zero rows after a simulated store failure, got {len(rows)} "
        f"— the fire-and-forget contract is broken if the raising stub "
        f"managed to insert a row"
    )
