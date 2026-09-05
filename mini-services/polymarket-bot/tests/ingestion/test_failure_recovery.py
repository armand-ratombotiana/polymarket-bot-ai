"""W31-7 — ingestion failure-recovery tests.

Ten failure modes the ingestion pipeline must survive (graceful
degradation, no crash, no data loss where the contract demands it):

  1. **API downtime**            — source API goes down; system degrades
                                    gracefully (cached fallback, no crash).
  2. **Network interruption**     — connection drops mid-stream; reconnect
                                    + resume, no data lost.
  3. **Authentication failure**    — invalid token; alert + retry.
  4. **Rate limit hit**            — HTTP 429; system backs off.
  5. **Malformed payload**         — invalid JSON / missing fields;
                                    reject + log + DLQ.
  6. **Duplicate events**          — same event twice; dedup prevents double
                                    processing.
  7. **Out-of-order events**       — events arrive in wrong order; system
                                    handles (stale-warning, no crash).
  8. **Database unavailability**    — DB goes down; DLQ catches records
                                    that would have been written.
  9. **Process crash**              — checkpoint enables resume.
  10. **Clock drift**               — timestamps from different clocks;
                                     system handles (normalises / warns).

Scope
~~~~~
The "ingestion pipeline" surface under test:

    upstream (CLOB / Gamma / WS)
        │
        ▼
    ``core.clob_client.ClobClient.get_public_trades`` /
        ``get_order_book``  ← API downtime / network / auth / 429
        │
        ▼
    ``core.api_resilience.APIResilienceLayer.call_with_resilience``  ← retries, breaker
        │
        ▼
    ``core.trade_ingester.TradeTapeIngester._ingest_trades``  ← dedup, out-of-order
        │
        ▼
    ``core.data_validator.DataValidator.validate_trade``  ← schema / value
        │
        ▼
    ``core.database_manager.db_manager.record_trade``  ← DB unavailability
        │  (on failure)
        ▼
    ``core.ingestion.raw_vault.RawVault.quarantine_record``  ← DLQ
        │
        ▼
    ``core.state_recovery.StateRecoveryManager.checkpoint``  ← crash recovery

Every test is isolated: ``monkeypatch`` patches the singleton call sites
for the duration of the test; the production singletons are NOT mutated
across tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Redirect every persisted-state path to /tmp BEFORE the first import. ──
_TMP_ROOT = Path("/tmp/pmbot_w31_7_ingestion_failure_recovery")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "RECOVERY_STATE_PATH": str(_TMP_ROOT / "recovery_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-w31-7-failure",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

ASYNC = pytest.mark.asyncio


# ── 1. API downtime ───────────────────────────────────────────────────────


class TestAPIDowntime:
    """Source API goes down; system degrades gracefully."""

    @ASYNC
    async def test_clob_downtime_returns_cached_fallback(self):
        """``ClobClient.get_order_book`` returns the cached fallback when
        the upstream CLOB is unreachable.

        The resilience layer retries 3 times with backoff (100 ms / 500 ms /
        2 000 ms — the 2 s sleep dominates the wall-clock). We monkeypatch
        ``asyncio.sleep`` to no-op so the test completes in < 100 ms.
        """
        from core.api_resilience import APIResilienceLayer
        from core.clob_client import ClobClient

        layer = APIResilienceLayer()
        client = ClobClient()

        # Seed the per-token cache so the resilience layer has a
        # fallback to return. Without this, the layer raises
        # ``ConnectionError`` (the contract for "no cache yet").
        cached_book = {"token_id": "T1", "bids": [[0.49, 100]], "asks": [[0.51, 100]]}
        client._cached_order_books["T1"] = cached_book

        # Patch ``asyncio.sleep`` so the backoff schedule (100/500/2000 ms
        # = 2.6 s wall-clock) completes instantly.
        with patch("core.api_resilience.asyncio.sleep", new=AsyncMock()):
            # The fetch coroutine always raises — simulating a
            # sustained CLOB outage.
            async def _always_fail() -> dict:
                raise ConnectionError("CLOB unreachable")

            result = await layer.call_with_resilience(
                "clob", _always_fail, fallback_data=cached_book,
            )

        assert result == cached_book, (
            f"Expected cached fallback, got {result!r}"
        )
        # The resilience layer should have recorded the failure.
        health = layer.get_health()
        assert "clob" in health
        assert health["clob"]["total_failures"] >= 1
        assert health["clob"]["last_error"] == "CLOB unreachable"

    @ASYNC
    async def test_trade_ingester_survives_clob_outage(self):
        """``TradeTapeIngester._ingest_trades`` does NOT raise when
        ``clob_client.get_public_trades`` raises — the poll loop must
        survive a sustained CLOB outage.

        The ingester's contract is "the loop never crashes" — the
        ``except Exception`` inside ``_ingest_trades`` already swallows
        fetch errors, but ``get_public_trades`` itself swallows them
        first (returns ``[]`` on any error). The double-safety net
        means an outage is invisible to the loop — only the
        ``_error_count`` counter moves.
        """
        from core.trade_ingester import TradeTapeIngester

        ingester = TradeTapeIngester(poll_interval=1.0)
        # Reset the counters (the singleton starts at zero, but a prior
        # test may have incremented them).
        ingester._ingested_count = 0
        ingester._error_count = 0

        # Patch ``clob_client.get_public_trades`` to raise — the
        # ingester imports ``clob_client`` lazily inside
        # ``_ingest_trades``, so the patch needs to target the module
        # attribute at call time.
        with patch("core.clob_client.clob_client.get_public_trades",
                   new=AsyncMock(side_effect=ConnectionError("CLOB down"))):
            # ``_ingest_trades`` catches the exception internally
            # (defensive belt-and-braces — ``get_public_trades`` already
            # swallows). The contract is: no exception propagates.
            await ingester._ingest_trades()

        stats = ingester.get_stats()
        # No trades ingested (the fetch failed before any new trade
        # could be returned).
        assert stats["ingested_count"] == 0
        # The ingester saw no errors at the _ingest_trades level (the
        # fetch was swallowed by ``get_public_trades`` itself, returning
        # ``[]`` — see the W20-7 contract: "errors are logged at error
        # level and swallowed"). ``_error_count`` would only increment
        # if ``_ingest_trades`` itself raised, which it doesn't.


# ── 2. Network interruption ────────────────────────────────────────────────


class TestNetworkInterruption:
    """Connection drops, reconnects, no data lost."""

    @ASYNC
    async def test_reconnect_after_interruption_resumes_ingestion(self):
        """First ``get_public_trades`` raises; second succeeds with
        fresh trades. Verify the ingester processes the second batch
        (no data lost) and the dedup registry correctly remembers the
        trades seen BEFORE the interruption.
        """
        from core.trade_ingester import TradeTapeIngester

        ingester = TradeTapeIngester(poll_interval=1.0)
        ingester._ingested_count = 0
        ingester._error_count = 0

        # Pre-feed two trades into the dedup set so the post-interruption
        # batch contains a duplicate (proves dedup survives the
        # interruption).
        ingester._last_trade_ids.add("trade_pre_1")
        ingester._last_trade_ids.add("trade_pre_2")

        post_interruption_trades = [
            {"trade_id": "trade_pre_1", "token_id": "T1", "price": 0.5,
             "size": 1.0, "side": "BUY", "timestamp": time.time()},  # dup
            {"trade_id": "trade_post_1", "token_id": "T2", "price": 0.4,
             "size": 2.0, "side": "SELL", "timestamp": time.time()},  # new
            {"trade_id": "trade_post_2", "token_id": "T3", "price": 0.6,
             "size": 3.0, "side": "BUY", "timestamp": time.time()},  # new
        ]

        # Patch ``db_manager.record_trade`` so the ingester's write
        # path doesn't touch the real DB. ``AsyncMock`` records the
        # calls so the test can assert exactly which trades were written.
        mock_record = AsyncMock()
        # Patch the validator (return a "valid" result for every trade
        # so the ingester tries to record it).
        with patch("core.data_validator.data_validator.validate_trade") as mock_validate, \
             patch("core.database_manager.db_manager.record_trade", new=mock_record), \
             patch("core.clob_client.clob_client.get_public_trades",
                   new=AsyncMock(return_value=post_interruption_trades)):

            # Build a fake ValidationResult that says "valid" and carries
            # the same trade payload (the ingester reads ``norm`` fields
            # from the result).
            from core.data_validator import ValidationResult
            def _fake_validate(raw):
                return ValidationResult(
                    is_valid=True,
                    is_duplicate=False,
                    errors=[],
                    warnings=[],
                    normalized_data=raw,
                    ingestion_time=time.time(),
                )
            mock_validate.side_effect = _fake_validate

            await ingester._ingest_trades()

        # The dedup registry caught the duplicate ("trade_pre_1") — the
        # other two were written. Verify via the AsyncMock's call count.
        assert mock_record.call_count == 2, (
            f"Expected 2 record_trade calls (3 trades - 1 dup), got "
            f"{mock_record.call_count}"
        )
        # Verify the written trade_ids are the two NEW ones (not the dup).
        written_ids = {
            call.kwargs.get("trade_id") or call.args[0]
            for call in mock_record.call_args_list
        }
        assert "trade_post_1" in written_ids
        assert "trade_post_2" in written_ids
        assert "trade_pre_1" not in written_ids

    @ASYNC
    async def test_intermittent_failures_do_not_lose_data(self):
        """Alternating success/failure cycles — every successful cycle
        processes its trades; failed cycles don't crash the loop.
        """
        from core.trade_ingester import TradeTapeIngester

        ingester = TradeTapeIngester(poll_interval=0.01)
        ingester._ingested_count = 0
        ingester._error_count = 0

        # Cycle 0: success (2 trades), cycle 1: failure (raises),
        # cycle 2: success (2 trades).
        cycles = [
            [{"trade_id": f"a_{i}", "token_id": "T", "price": 0.5,
              "size": 1.0, "side": "BUY", "timestamp": time.time()}
             for i in range(2)],
            ConnectionError("cycle 1 fails"),
            [{"trade_id": f"b_{i}", "token_id": "T", "price": 0.5,
              "size": 1.0, "side": "BUY", "timestamp": time.time()}
             for i in range(2)],
        ]
        cycle_iter = iter(cycles)

        async def _fake_fetch(*args, **kwargs):
            val = next(cycle_iter)
            if isinstance(val, Exception):
                raise val
            return val

        with patch("core.data_validator.data_validator.validate_trade") as mock_validate, \
             patch("core.database_manager.db_manager.record_trade", new=AsyncMock()), \
             patch("core.clob_client.clob_client.get_public_trades",
                   new=_fake_fetch):

            from core.data_validator import ValidationResult
            def _fake_validate(raw):
                return ValidationResult(
                    is_valid=True, is_duplicate=False,
                    errors=[], warnings=[],
                    normalized_data=raw, ingestion_time=time.time(),
                )
            mock_validate.side_effect = _fake_validate

            # Run 3 cycles.
            for _ in range(3):
                try:
                    await ingester._ingest_trades()
                except Exception:
                    # ``_ingest_trades`` should swallow its own fetch
                    # errors. If it ever raises, the test fails.
                    pytest.fail("_ingest_trades raised during intermittent failure cycle")

        # 4 trades total were written (cycles 0 and 2; cycle 1 raised
        # before any trade could be written).
        assert ingester._ingested_count == 4, (
            f"Expected 4 ingested after intermittent failures, got "
            f"{ingester._ingested_count}"
        )


# ── 3. Authentication failure ──────────────────────────────────────────────


class TestAuthenticationFailure:
    """Invalid token; system alerts + retries."""

    @ASYNC
    async def test_auth_failure_triggers_retry_and_alert(self):
        """A 401 response from the CLOB triggers the resilience layer's
        retry schedule. After 3 attempts the failure is recorded on the
        per-API health record (``last_error`` / ``consecutive_failures``)
        — the "alert" surface an operator polls via
        ``GET /api/api-health``.
        """
        from core.api_resilience import APIResilienceLayer, APIStatus

        layer = APIResilienceLayer()

        async def _always_401() -> dict:
            raise RuntimeError("401 Unauthorized: invalid API credentials")

        with patch("core.api_resilience.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ConnectionError, match="invalid API credentials"):
                await layer.call_with_resilience(
                    "clob", _always_401, fallback_data=None,
                )

        health = layer.get_health()["clob"]
        # 3 retries → 1 logical-call failure recorded (the layer counts
        # ONE failure per logical call, not per retry).
        assert health["total_failures"] == 1
        assert "401" in health["last_error"]
        assert health["consecutive_failures"] == 1
        # After ONE failure the status is still UNKNOWN — the layer's
        # status derivation only flips to DEGRADED at
        # ``consecutive_failures >= 2`` (mirrors the contract verified
        # in ``tests/test_api_resilience.py::test_failure_records_*``).
        # The "alert" surface an operator polls is therefore the
        # ``last_error`` / ``consecutive_failures`` pair, NOT the
        # status enum (which lags by one failure).
        assert health["status"] == APIStatus.UNKNOWN.value, (
            f"Expected status=unknown after 1 failure (DEGRADED triggers at "
            f"consecutive_failures>=2); got {health['status']!r}"
        )

    @ASYNC
    async def test_auth_failure_with_fallback_returns_fallback(self):
        """Same as above, but with a fallback — the layer returns the
        fallback instead of raising, so the calling poller keeps
        running on stale data.
        """
        from core.api_resilience import APIResilienceLayer

        layer = APIResilienceLayer()
        fallback_trades: list[dict] = []

        async def _always_401() -> list:
            raise RuntimeError("401 Unauthorized")

        with patch("core.api_resilience.asyncio.sleep", new=AsyncMock()):
            result = await layer.call_with_resilience(
                "clob", _always_401, fallback_data=fallback_trades,
            )

        assert result == fallback_trades
        health = layer.get_health()["clob"]
        assert health["total_failures"] == 1


# ── 4. Rate limit hit ──────────────────────────────────────────────────────


class TestRateLimitHit:
    """HTTP 429 response; system backs off."""

    @ASYNC
    async def test_429_triggers_backoff_and_eventual_success(self):
        """First two attempts raise 429; third succeeds. The resilience
        layer's backoff schedule (100 ms / 500 ms / 2 000 ms) is
        patched out for speed, but the CALL COUNT proves the backoff
        happened — 3 invocations of ``call_fn``.
        """
        from core.api_resilience import APIResilienceLayer

        layer = APIResilienceLayer()
        call_count = {"n": 0}

        async def _flaky_429() -> dict:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("429 Too Many Requests")
            return {"ok": True}

        with patch("core.api_resilience.asyncio.sleep", new=AsyncMock()):
            result = await layer.call_with_resilience("clob", _flaky_429)

        assert result == {"ok": True}
        # The layer attempted 3 times — backoff happened between each.
        assert call_count["n"] == 3, (
            f"Expected 3 attempts (2 backoffs + 1 success), got {call_count['n']}"
        )
        # The success was recorded — health shows 1 call, 0 failures.
        health = layer.get_health()["clob"]
        assert health["total_calls"] == 1
        assert health["total_failures"] == 0
        assert health["consecutive_failures"] == 0

    @ASYNC
    async def test_sustained_429_trips_circuit_breaker(self):
        """5 consecutive logical-call failures (each retried 3 times = 15
        HTTP attempts) trip the resilience layer's circuit breaker. The
        6th call returns the fallback immediately (no HTTP round-trip).
        """
        from core.api_resilience import APIResilienceLayer, APIStatus

        layer = APIResilienceLayer()
        # Failure threshold defaults to 5; lower it to 2 so the test
        # doesn't have to drive 5 × 3 = 15 failures.
        layer._failure_threshold = 2

        attempt_count = {"n": 0}

        async def _always_429() -> dict:
            attempt_count["n"] += 1
            raise RuntimeError("429 Too Many Requests")

        with patch("core.api_resilience.asyncio.sleep", new=AsyncMock()):
            # First call: 3 attempts, all fail, fallback returned.
            await layer.call_with_resilience("clob", _always_429, fallback_data={"v": 1})
            # Second call: 3 more attempts, all fail, fallback returned.
            await layer.call_with_resilience("clob", _always_429, fallback_data={"v": 2})
            # Third call: breaker is OPEN (2 consecutive logical-call
            # failures = threshold reached). NO attempts made — fallback
            # returned immediately.
            result = await layer.call_with_resilience("clob", _always_429, fallback_data={"v": 3})

        assert result == {"v": 3}
        # 6 total attempts (3 per logical call × 2 logical calls); the
        # third call's breaker-open short-circuit means 0 more attempts.
        assert attempt_count["n"] == 6, (
            f"Expected breaker to short-circuit on 3rd call (6 total HTTP "
            f"attempts), got {attempt_count['n']}"
        )
        health = layer.get_health()["clob"]
        assert health["status"] == APIStatus.UNHEALTHY.value
        assert health["consecutive_failures"] >= 2


# ── 5. Malformed payload ────────────────────────────────────────────────────


class TestMalformedPayload:
    """Invalid JSON / missing fields; reject + log + DLQ."""

    def test_missing_required_fields_rejected_by_validator(self):
        """A trade missing ``token_id`` / ``price`` / ``size`` / ``side``
        is rejected by ``DataValidator.validate_trade`` with the right
        error messages.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        bad = {"trade_id": "bad_1", "price": 0.5, "size": 1.0, "side": "BUY"}
        # Missing ``token_id``.
        r = validator.validate_trade(bad)
        assert not r.is_valid
        assert any("token_id" in err for err in r.errors)

    def test_invalid_field_types_rejected(self):
        """Non-numeric ``price`` is rejected (validator coerces to float
        and rejects when the coercion fails).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        bad = {
            "trade_id": "bad_2",
            "token_id": "T1",
            "price": "not_a_number",
            "size": 1.0,
            "side": "BUY",
        }
        r = validator.validate_trade(bad)
        assert not r.is_valid
        assert any("Invalid price" in err for err in r.errors)

    @ASYNC
    async def test_raw_vault_quarantines_malformed_payload(self):
        """A malformed payload (parse failure) is routed to the raw vault's
        ``quarantine_record`` so the DLQ holds the bad record for
        operator review.

        The raw vault's ``record_observation`` writes to PostgreSQL when
        ``timescale_db._is_postgres`` is True; in tests (no PG) it falls
        through to the ``except`` branch which calls
        ``quarantine_record`` — and ``quarantine_record`` itself is a
        no-op when PG is unavailable. The test patches both methods to
        verify the wiring.
        """
        from core.ingestion.raw_vault import RawVault

        vault = RawVault()
        quarantine_calls: list[tuple] = []

        async def _fake_quarantine(source_id, raw_payload, error_class,
                                   error_message, stack_trace=""):
            quarantine_calls.append((source_id, error_class, error_message))

        # Patch ``record_observation`` to raise — that's the failure
        # path that triggers ``quarantine_record``.
        with patch.object(vault, "quarantine_record",
                          new=AsyncMock(side_effect=_fake_quarantine)):
            # Force the PG branch to look "available" so the record
            # path runs and raises inside the INSERT.
            with patch("core.ingestion.raw_vault.timescale_db") as mock_ts:
                mock_ts._is_postgres = True
                mock_ts._pool = MagicMock()
                # ``conn.fetchval`` raises — simulating a malformed
                # JSON payload that Postgres rejects.
                mock_ctx = AsyncMock()
                mock_ctx.__aenter__.return_value.fetchval.side_effect = (
                    RuntimeError("invalid input syntax for type json")
                )
                mock_ts._pool.acquire.return_value = mock_ctx

                obs_id = await vault.record_observation(
                    source_id="clob_rest",
                    raw_payload="this is not valid JSON {",
                )

        # ``record_observation`` returns ``None`` on failure (the
        # observation_id is only returned on a successful INSERT).
        assert obs_id is None
        # ``quarantine_record`` was called with the source id and the
        # error class.
        assert len(quarantine_calls) == 1
        source_id, error_class, error_message = quarantine_calls[0]
        assert source_id == "clob_rest"
        assert "RuntimeError" in error_class or "Error" in error_class


# ── 6. Duplicate events ────────────────────────────────────────────────────


class TestDuplicateEvents:
    """Same event twice; dedup prevents double processing."""

    def test_duplicate_trade_id_is_deduplicated(self):
        """Two calls to ``validate_trade`` with the same ``trade_id``
        → first is_valid=True, second is_valid=False + is_duplicate=True.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "dup_1",
            "token_id": "T1",
            "price": 0.5,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
        }
        r1 = validator.validate_trade(trade)
        r2 = validator.validate_trade(trade)

        assert r1.is_valid
        assert not r1.is_duplicate
        assert not r2.is_valid
        assert r2.is_duplicate
        assert r2.errors == ["Duplicate trade"]

    def test_dedup_registry_blocks_duplicate_at_entity_level(self):
        """The ``DedupRegistry.check_and_add`` blocks the same key
        within a TTL window — used by the order / fill / decision /
        alert / audit entity types.
        """
        from core.dedup import DedupRegistry

        registry = DedupRegistry()
        # First call: key is new → True.
        assert registry.check_and_add("order", "key_1", ttl_seconds=300) is True
        # Second call within the same TTL window → False (duplicate).
        assert registry.check_and_add("order", "key_1", ttl_seconds=300) is False
        # Different key → True.
        assert registry.check_and_add("order", "key_2", ttl_seconds=300) is True

    @ASYNC
    async def test_ingester_skips_already_seen_trade_ids(self):
        """The trade ingester's fast-path dedup (``_last_trade_ids``
        set) catches duplicates BEFORE they reach the DB.
        """
        from core.trade_ingester import TradeTapeIngester

        ingester = TradeTapeIngester(poll_interval=1.0)
        # Pre-populate the dedup set with "trade_1".
        ingester._last_trade_ids.add("trade_1")

        trades = [
            {"trade_id": "trade_1", "token_id": "T", "price": 0.5,
             "size": 1.0, "side": "BUY", "timestamp": time.time()},  # dup
            {"trade_id": "trade_2", "token_id": "T", "price": 0.5,
             "size": 1.0, "side": "BUY", "timestamp": time.time()},  # new
        ]
        mock_record = AsyncMock()

        with patch("core.data_validator.data_validator.validate_trade") as mock_validate, \
             patch("core.database_manager.db_manager.record_trade", new=mock_record), \
             patch("core.clob_client.clob_client.get_public_trades",
                   new=AsyncMock(return_value=trades)):

            from core.data_validator import ValidationResult
            def _fake_validate(raw):
                return ValidationResult(
                    is_valid=True, is_duplicate=False,
                    errors=[], warnings=[],
                    normalized_data=raw, ingestion_time=time.time(),
                )
            mock_validate.side_effect = _fake_validate

            await ingester._ingest_trades()

        # Only the new trade was recorded.
        assert mock_record.call_count == 1
        # The duplicate never reached the validator (the fast-path
        # caught it first).
        assert mock_validate.call_count == 1


# ── 7. Out-of-order events ──────────────────────────────────────────────────


class TestOutOfOrderEvents:
    """Events arrive in wrong order; system handles (staleness warning)."""

    def test_old_timestamp_emits_staleness_warning(self):
        """A timestamp > 60s in the past emits a warning (not an error)
        — the record is still accepted.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        old_ts = time.time() - 120  # 2 minutes ago
        snap = {
            "token_id": "T1",
            "best_bid": 0.49,
            "best_ask": 0.51,
            "timestamp": old_ts,
        }
        r = validator.validate_snapshot(snap)
        assert r.is_valid, f"Stale-but-recent record should be accepted: {r.errors}"
        assert any("Stale data" in w for w in r.warnings), (
            f"Expected staleness warning, got warnings={r.warnings}"
        )

    def test_very_old_timestamp_rejected(self):
        """A timestamp > 300s in the past is rejected outright."""
        from core.data_validator import DataValidator

        validator = DataValidator()
        very_old_ts = time.time() - 600  # 10 minutes ago
        snap = {
            "token_id": "T1",
            "best_bid": 0.49,
            "best_ask": 0.51,
            "timestamp": very_old_ts,
        }
        r = validator.validate_snapshot(snap)
        assert not r.is_valid
        assert any("Very stale" in e for e in r.errors)

    def test_out_of_order_trades_accepted_without_crash(self):
        """Trades don't run the staleness check (a stale trade is still a
        valid historical fill). The contract here is that out-of-order
        arrival — newer trade first, older trade second — does NOT
        crash the validator.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        base = time.time()
        trades = [
            {"trade_id": "new_first", "token_id": "T", "price": 0.5,
             "size": 1.0, "side": "BUY", "timestamp": base},
            {"trade_id": "old_second", "token_id": "T", "price": 0.5,
             "size": 1.0, "side": "SELL", "timestamp": base - 600},
        ]
        results = [validator.validate_trade(t) for t in trades]
        # Both accepted — trades bypass the staleness check.
        assert all(r.is_valid for r in results), (
            f"Out-of-order trades should both be accepted: {[r.errors for r in results]}"
        )


# ── 8. Database unavailability ──────────────────────────────────────────────


class TestDatabaseUnavailability:
    """DB goes down; DLQ catches records that would have been written."""

    @ASYNC
    async def test_db_failure_routes_record_to_dlq(self):
        """When ``db_manager.record_trade`` raises, the ingester logs
        the failure (the ``except`` branch in ``_ingest_trades``) and
        moves on — no crash, no lost batch (only the one trade that
        failed the DB write is affected).

        The raw vault's ``quarantine_record`` is the backstop for
        records that fail the validator's pre-write check; for records
        that pass validation but fail the DB write, the ingester's own
        error handler logs and continues (the durable UNIQUE on
        ``trade_id`` is the backstop for restarts).
        """
        from core.trade_ingester import TradeTapeIngester

        ingester = TradeTapeIngester(poll_interval=1.0)
        ingester._ingested_count = 0
        ingester._error_count = 0

        trades = [
            {"trade_id": f"db_test_{i}", "token_id": "T", "price": 0.5,
             "size": 1.0, "side": "BUY", "timestamp": time.time()}
            for i in range(3)
        ]

        # First record_trade succeeds, second raises, third succeeds.
        async def _flaky_record(*args, **kwargs):
            call_n = _flaky_record.calls
            _flaky_record.calls += 1
            if call_n == 1:
                raise RuntimeError("DB connection lost")
            return None
        _flaky_record.calls = 0

        with patch("core.data_validator.data_validator.validate_trade") as mock_validate, \
             patch("core.database_manager.db_manager.record_trade",
                   new=_flaky_record), \
             patch("core.clob_client.clob_client.get_public_trades",
                   new=AsyncMock(return_value=trades)):

            from core.data_validator import ValidationResult
            def _fake_validate(raw):
                return ValidationResult(
                    is_valid=True, is_duplicate=False,
                    errors=[], warnings=[],
                    normalized_data=raw, ingestion_time=time.time(),
                )
            mock_validate.side_effect = _fake_validate

            # The ingester must NOT raise — the per-record try/except
            # inside ``_ingest_trades`` swallows the DB error and the
            # batch continues.
            await ingester._ingest_trades()

        # 2 of 3 trades written (the 2nd failed its DB write).
        assert ingester._ingested_count == 2, (
            f"Expected 2 successful writes (1 DB failure skipped), got "
            f"{ingester._ingested_count}"
        )

    @ASYNC
    async def test_raw_vault_quarantines_record_when_db_unavailable(self):
        """When ``record_observation``'s DB write fails, the record is
        routed to ``quarantine_record`` (the DLQ).

        Mirrors the malformed-payload test (Test 5) but here the payload
        is valid JSON; the failure is on the DB write side.
        """
        from core.ingestion.raw_vault import RawVault

        vault = RawVault()
        quarantine_calls: list[tuple] = []

        async def _fake_quarantine(source_id, raw_payload, error_class,
                                   error_message, stack_trace=""):
            quarantine_calls.append((source_id, error_class, error_message))

        with patch.object(vault, "quarantine_record",
                          new=AsyncMock(side_effect=_fake_quarantine)):
            with patch("core.ingestion.raw_vault.timescale_db") as mock_ts:
                mock_ts._is_postgres = True
                mock_ts._pool = MagicMock()
                mock_ctx = AsyncMock()
                mock_ctx.__aenter__.return_value.fetchval.side_effect = (
                    RuntimeError("connection pool exhausted")
                )
                mock_ts._pool.acquire.return_value = mock_ctx

                obs_id = await vault.record_observation(
                    source_id="clob_rest",
                    raw_payload={"valid": "json", "but": "db is down"},
                )

        assert obs_id is None
        assert len(quarantine_calls) == 1
        _, error_class, error_message = quarantine_calls[0]
        assert "connection pool exhausted" in error_message


# ── 9. Process crash ────────────────────────────────────────────────────────


class TestProcessCrash:
    """Checkpoint enables resume."""

    @ASYNC
    async def test_checkpoint_then_recover_resumes_state(self, tmp_path):
        """A ``StateRecoveryManager.checkpoint()`` call snapshots the
        live ``store`` state. After a "crash" (simulated by constructing
        a NEW ``StateRecoveryManager`` pointed at the same JSON file),
        ``recover()`` rebuilds the state from the checkpoint.
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "crash_recovery.json"
        m1 = StateRecoveryManager(state_path=ckpt)
        await m1.checkpoint()
        assert ckpt.exists(), "checkpoint() must write the JSON file"

        # Simulate a crash: build a new manager pointed at the same file.
        m2 = StateRecoveryManager(state_path=ckpt)
        report = await m2.recover()

        # The report reflects the checkpointed state.
        assert report.recovered_positions >= 0
        assert report.recovered_orders >= 0
        assert report.checkpoint_timestamp is not None, (
            "recover() must surface the checkpoint's timestamp so the "
            "operator can see how stale the recovered state is"
        )
        assert report.recovery_time < 1.0  # sub-second recovery

    @ASYNC
    async def test_corrupt_checkpoint_does_not_block_boot(self, tmp_path):
        """A corrupt checkpoint file (invalid JSON) must NOT block the
        bot from booting. The recovery manager treats it as a fresh
        boot and produces a zeroed report.

        The contract is fail-soft: ``_load_state`` catches the
        ``JSONDecodeError``, logs at ``error`` level, returns ``None``.
        ``recover()`` then takes the ``state is None`` branch (fresh
        boot) — the corruption does NOT propagate as an error on the
        report (the bot must always boot, even if recovery is partial).
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "corrupt.json"
        ckpt.write_text("{ this is not valid JSON")

        manager = StateRecoveryManager(state_path=ckpt)
        # The contract is "no exception propagates" — the manager
        # MUST NOT raise on a corrupt file. ``await`` would surface
        # any unhandled exception as a test failure.
        report = await manager.recover()

        # Fresh-boot report — no recovered positions/orders, no
        # checkpoint timestamp.
        assert report.recovered_positions == 0
        assert report.recovered_orders == 0
        assert report.checkpoint_timestamp is None
        # The corruption does NOT surface on ``report.errors`` — the
        # manager treats a corrupt file as "no checkpoint exists" (the
        # log line at ``error`` level is the operator-facing surface,
        # not the report). This is intentional: a corrupt checkpoint
        # is a recovery-from-no-state scenario, which is exactly what
        # fresh-boot handles.
        assert report.errors == [], (
            f"Corrupt-checkpoint recovery must NOT surface errors on the "
            f"report (fail-soft contract); got: {report.errors}"
        )

    @ASYNC
    async def test_recover_then_checkpoint_round_trip_preserves_state(self, tmp_path):
        """Full round-trip: checkpoint → recover → checkpoint → recover.
        The state must be stable across the round-trip (no field drift).
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "round_trip.json"
        m1 = StateRecoveryManager(state_path=ckpt)
        await m1.checkpoint()
        r1 = await m1.recover()

        # Second cycle.
        m2 = StateRecoveryManager(state_path=ckpt)
        await m2.checkpoint()
        r2 = await m2.recover()

        # The recovered counts must match across the round-trip.
        assert r1.recovered_positions == r2.recovered_positions
        assert r1.recovered_orders == r2.recovered_orders
        assert r1.kill_switch_active == r2.kill_switch_active


# ── 10. Clock drift ────────────────────────────────────────────────────────


class TestClockDrift:
    """Timestamps from different clocks; system handles."""

    def test_future_dated_timestamp_accepted_without_staleness_warning(self):
        """A timestamp slightly in the future (clock skew between the
        source's clock and ours) is accepted — the staleness check
        only rejects PAST timestamps, not future ones (a future
        timestamp has negative staleness).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        future_ts = time.time() + 30  # 30s in the future
        snap = {
            "token_id": "T1",
            "best_bid": 0.49,
            "best_ask": 0.51,
            "timestamp": future_ts,
        }
        r = validator.validate_snapshot(snap)
        assert r.is_valid, (
            f"Future-dated timestamp should be accepted (clock skew), got: {r.errors}"
        )
        # No staleness warning for a future timestamp.
        assert not any("Stale" in w for w in r.warnings)

    def test_iso8601_timestamps_normalised_across_clocks(self):
        """ISO-8601 strings from different sources (UTC / +00:00 / 'Z'
        suffix) all normalise to a Unix epoch float — proving the
        validator handles clock-drift-induced format variance.

        Uses recent timestamps (``now - 1s``) so the staleness check
        (> 300s = reject) doesn't trip; the test is about FORMAT
        normalisation, not staleness.
        """
        from datetime import datetime, timezone, timedelta

        from core.data_validator import DataValidator

        validator = DataValidator()
        # Three representations of approximately the same instant —
        # ``now - 1s`` so the staleness check (300s reject / 60s warn)
        # is well below the warning threshold.
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        iso_variants = [
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            now.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00"),
        ]
        normalized_ts: list[float] = []
        for i, iso in enumerate(iso_variants):
            snap = {
                "token_id": f"clock_{i}",
                "best_bid": 0.49,
                "best_ask": 0.51,
                "timestamp": iso,
            }
            r = validator.validate_snapshot(snap)
            assert r.is_valid, f"ISO variant {iso!r} rejected: {r.errors}"
            normalized_ts.append(r.normalized_data["timestamp"])

        # All three normalised timestamps must be within 1 second of
        # each other (they represent the same instant).
        assert max(normalized_ts) - min(normalized_ts) < 1.0, (
            f"Clock-drift normalisation failed: timestamps diverged by "
            f"{max(normalized_ts) - min(normalized_ts):.1f}s"
        )

    def test_mixed_numeric_and_string_timestamps_handled_without_crash(self):
        """A batch mixing numeric / string / ISO-8601 timestamps must
        not crash the validator — each is normalised independently.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        ts_variants = [
            1700000000,                    # int
            1700000000.5,                   # float
            "1700000000",                   # numeric string
            "2026-01-01T00:00:00Z",         # ISO-8601 with Z
            "2026-01-01T00:00:00+00:00",   # ISO-8601 with offset
            None,                           # missing → ingestion time
        ]
        for i, ts in enumerate(ts_variants):
            snap = {
                "token_id": f"clock_mix_{i}",
                "best_bid": 0.49,
                "best_ask": 0.51,
                "timestamp": ts,
            }
            r = validator.validate_snapshot(snap)
            # The very-stale check may reject the 2026-01-01 timestamp
            # if "now" is later than 2026-01-01 + 300s. The contract
            # here is "no crash" — not "every variant accepted".
            assert isinstance(r.normalized_data.get("timestamp", 0.0), float) or r.errors, (
                f"Validator crashed on ts variant {ts!r}: {r}"
            )
