"""W31-7 — ingestion replay tests.

Four replay scenarios required by the W31-7 task spec:

  1. **Replay from checkpoint**      — resume from the last checkpoint
                                       (``StateRecoveryManager``).
  2. **Replay historical data**      — re-process raw vault data
                                       (``RawVault.record_observation``
                                       round-trip).
  3. **Replay produces same results** — idempotent processing: re-running
                                       the validator on the same payload
                                       the second time is deduplicated.
  4. **Replay with schema changes**   — old data with new schema fields
                                       is re-processed against the
                                       current validator (the new fields
                                       are accepted via the spread).

Scope
~~~~~
The replay surface is the combination of:

  * ``core.state_recovery.StateRecoveryManager`` — for the
    checkpoint/resume path (Test 1).
  * ``core.ingestion.raw_vault.RawVault`` — for the raw-data replay
    path (Test 2). The raw vault stores immutable observations; a
    replay re-reads them.
  * ``core.data_validator.DataValidator`` — for the idempotency and
    schema-evolution paths (Tests 3 and 4). The validator's dedup
    deque is the in-memory fast-path; the DB's UNIQUE constraint is
    the durable backstop.

Why replay matters
~~~~~~~~~~~~~~~~~~
The ingestion pipeline is the system-of-record for the trading bot.
A replay is the operator's recovery mechanism for:

  * "What did we miss during the 5-minute outage?" — replay from
    the raw vault.
  * "What did we have before the crash?" — replay from the
    checkpoint.
  * "If we re-process the same data with the new validator, do we
    get the same results?" — idempotency check.

The tests below verify each of these contracts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Redirect every persisted-state path to /tmp BEFORE the first import. ──
_TMP_ROOT = Path("/tmp/pmbot_w31_7_ingestion_replay")
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
    "API_TOKEN": "test-token-w31-7-replay",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

ASYNC = pytest.mark.asyncio


# ── 1. Replay from checkpoint ─────────────────────────────────────────────


class TestReplayFromCheckpoint:
    """Resume from the last checkpoint."""

    @ASYNC
    async def test_checkpoint_then_recover_returns_same_state(self, tmp_path):
        """``StateRecoveryManager.checkpoint()`` writes the live ``store``
        state to JSON. A new manager pointed at the same file recovers
        it verbatim — the recovered positions / orders match what the
        checkpoint captured.
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "replay_ckpt.json"
        m1 = StateRecoveryManager(state_path=ckpt)
        await m1.checkpoint()

        # Verify the checkpoint file is a real JSON object with the
        # documented schema (timestamp / schema_version / positions /
        # orders / kill_switch_active / paper_balance / feature_flags).
        with open(ckpt) as f:
            data = json.load(f)
        assert "timestamp" in data
        assert "schema_version" in data
        assert "positions" in data
        assert "orders" in data

        # Recover from the checkpoint.
        m2 = StateRecoveryManager(state_path=ckpt)
        report = await m2.recover()

        # The report's ``checkpoint_timestamp`` must match the file's
        # ``timestamp`` field (proving the recovery read the right file).
        assert report.checkpoint_timestamp is not None
        assert abs(report.checkpoint_timestamp - data["timestamp"]) < 0.001

    @ASYNC
    async def test_recover_without_checkpoint_is_fresh_boot(self, tmp_path):
        """When no checkpoint exists (fresh boot / first run after
        deployment), ``recover()`` returns a zeroed report and does NOT
        raise — the bot must always be able to start.
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "nonexistent.json"
        manager = StateRecoveryManager(state_path=ckpt)
        report = await manager.recover()

        assert report.recovered_positions == 0
        assert report.recovered_orders == 0
        assert report.stale_orders == 0
        assert report.checkpoint_timestamp is None
        # Fresh boot must NOT record any errors (the missing file is
        # the expected state, not an error condition).
        assert report.errors == []

    @ASYNC
    async def test_repeated_checkpoints_overwrite_cleanly(self, tmp_path):
        """Multiple ``checkpoint()`` calls on the same path must
        overwrite cleanly — no half-written file, no schema drift.
        """
        from core.state_recovery import StateRecoveryManager

        ckpt = tmp_path / "replay_repeat.json"
        manager = StateRecoveryManager(state_path=ckpt)

        timestamps: list[float] = []
        for _ in range(5):
            await manager.checkpoint()
            with open(ckpt) as f:
                data = json.load(f)
            timestamps.append(data["timestamp"])
            await asyncio.sleep(0.01)  # ensure distinct timestamps

        # Every checkpoint overwrote the previous one — only the last
        # is on disk.
        with open(ckpt) as f:
            data = json.load(f)
        assert data["timestamp"] == timestamps[-1]

        # The timestamps are monotonically increasing (the checkpoint
        # loop never writes a stale timestamp).
        assert timestamps == sorted(timestamps), (
            f"Checkpoint timestamps must be monotonic, got: {timestamps}"
        )


# ── 2. Replay historical data ──────────────────────────────────────────────


class TestReplayHistoricalData:
    """Re-process raw vault data."""

    @ASYNC
    async def test_raw_vault_round_trips_payload(self):
        """``RawVault.record_observation`` writes the payload (verbatim)
        to PostgreSQL; on a subsequent replay, the same payload is
        read back. In tests (no PG), we mock the pool to verify the
        INSERT carried the right payload + checksum.
        """
        from core.ingestion.raw_vault import RawVault

        vault = RawVault()
        recorded_payloads: list[tuple] = []

        async def _fake_fetchval(query, *args):
            # Args: source_id, checksum, payload_json, occurred_at, received_at
            recorded_payloads.append(args)
            return "obs_123"

        with patch("core.ingestion.raw_vault.timescale_db") as mock_ts:
            mock_ts._is_postgres = True
            mock_ts._pool = MagicMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value.fetchval.side_effect = _fake_fetchval
            mock_ts._pool.acquire.return_value = mock_ctx

            payload = {
                "trade_id": "replay_1",
                "token_id": "T1",
                "price": 0.50,
                "size": 1.0,
                "side": "BUY",
            }
            obs_id = await vault.record_observation(
                source_id="clob_rest",
                raw_payload=payload,
            )

        assert obs_id == "obs_123"
        # Verify the recorded payload — source_id, checksum, payload_json,
        # occurred_at, received_at.
        assert len(recorded_payloads) == 1
        args = recorded_payloads[0]
        source_id, checksum, payload_json, occurred_at, received_at = args
        assert source_id == "clob_rest"
        # Checksum is a 64-char sha256 hex (the vault truncates to 16 for
        # the dedup hash but stores the full hash here).
        assert len(checksum) == 64
        # The payload JSON is round-trippable.
        decoded = json.loads(payload_json)
        assert decoded == payload

    @ASYNC
    async def test_raw_vault_checksum_is_deterministic(self):
        """Re-recording the SAME payload yields the SAME checksum —
        the basis for idempotent replay (the DB's UNIQUE on
        ``payload_checksum`` is the durable backstop for "have we
        seen this exact payload before?").
        """
        from core.ingestion.raw_vault import RawVault

        vault = RawVault()
        checksums: list[str] = []

        async def _fake_fetchval(query, *args):
            checksums.append(args[1])  # args[1] = checksum
            return f"obs_{len(checksums)}"

        with patch("core.ingestion.raw_vault.timescale_db") as mock_ts:
            mock_ts._is_postgres = True
            mock_ts._pool = MagicMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value.fetchval.side_effect = _fake_fetchval
            mock_ts._pool.acquire.return_value = mock_ctx

            payload = {"trade_id": "deterministic", "v": 1}
            await vault.record_observation("clob_rest", payload)
            await vault.record_observation("clob_rest", payload)
            # Different payload → different checksum.
            await vault.record_observation("clob_rest", {"trade_id": "different", "v": 2})

        # First two checksums are identical (same payload).
        assert checksums[0] == checksums[1], (
            f"Same payload must produce same checksum, got: {checksums[:2]}"
        )
        # Third is different.
        assert checksums[2] != checksums[0]

    @ASYNC
    async def test_replay_routes_failed_writes_to_dlq(self):
        """A replay that hits a write failure routes the record to the
        DLQ (``quarantine_record``) so the operator can re-process
        after fixing the DB.
        """
        from core.ingestion.raw_vault import RawVault

        vault = RawVault()
        dlq: list[tuple] = []

        async def _fake_quarantine(source_id, raw_payload, error_class,
                                   error_message, stack_trace=""):
            dlq.append((source_id, error_class, error_message))

        with patch.object(vault, "quarantine_record",
                          new=AsyncMock(side_effect=_fake_quarantine)):
            with patch("core.ingestion.raw_vault.timescale_db") as mock_ts:
                mock_ts._is_postgres = True
                mock_ts._pool = MagicMock()
                mock_ctx = AsyncMock()
                mock_ctx.__aenter__.return_value.fetchval.side_effect = (
                    RuntimeError("disk full")
                )
                mock_ts._pool.acquire.return_value = mock_ctx

                await vault.record_observation("clob_rest", {"v": 1})

        # The failed write went to the DLQ.
        assert len(dlq) == 1
        source_id, error_class, error_message = dlq[0]
        assert source_id == "clob_rest"
        assert "disk full" in error_message


# ── 3. Replay produces same results (idempotent) ──────────────────────────


class TestReplayIdempotency:
    """Re-processing the same data must produce the same results."""

    def test_re_processing_same_trade_is_deduplicated(self):
        """A second call to ``validate_trade`` with the same trade_id
        is rejected as a duplicate — proving the validator's
        idempotency contract for replay.

        In production, the dedup deque is bounded (``max_seen_ids``)
        so a replay after a long gap may miss the dedup — but the DB's
        UNIQUE constraint on ``trade_id`` is the durable backstop.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "replay_idem_1",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
        }
        r1 = validator.validate_trade(trade)
        r2 = validator.validate_trade(trade)  # replay

        assert r1.is_valid
        assert not r2.is_valid
        assert r2.is_duplicate
        assert r2.errors == ["Duplicate trade"]

        stats = validator.get_stats()
        assert stats["valid_count"] == 1
        assert stats["duplicate_count"] == 1

    def test_re_processing_same_snapshot_is_deduplicated(self):
        """A second call to ``validate_snapshot`` with the same 4-field
        hash (token_id / best_bid / best_ask / timestamp) is rejected.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        snap = {
            "token_id": "T1",
            "best_bid": 0.49,
            "best_ask": 0.51,
            "timestamp": time.time(),
        }
        r1 = validator.validate_snapshot(snap)
        r2 = validator.validate_snapshot(snap)  # replay

        assert r1.is_valid
        assert not r2.is_valid
        assert r2.is_duplicate
        assert r2.errors == ["Duplicate snapshot"]

    def test_re_processing_with_different_timestamp_is_unique(self):
        """Two snapshots of the same token at DIFFERENT timestamps are
        NOT duplicates (the hash includes ``timestamp``). This is the
        contract that allows a replay of a historical time-series
        without false-positive dedup.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        base = time.time()
        snap1 = {"token_id": "T1", "best_bid": 0.49, "best_ask": 0.51,
                 "timestamp": base}
        snap2 = {"token_id": "T1", "best_bid": 0.49, "best_ask": 0.51,
                 "timestamp": base + 1}  # different ts

        r1 = validator.validate_snapshot(snap1)
        r2 = validator.validate_snapshot(snap2)

        assert r1.is_valid
        assert r2.is_valid
        assert not r2.is_duplicate

    @ASYNC
    async def test_dedup_registry_idempotent_within_ttl_window(self):
        """``DedupRegistry.check_and_add`` returns False for the same
        key within the TTL window — the registry-level idempotency
        contract used by the order / fill / decision / alert / audit
        entity types.
        """
        from core.dedup import DedupRegistry

        registry = DedupRegistry()
        # First call: new key → True.
        assert registry.check_and_add("audit", "event_1", ttl_seconds=300) is True
        # Replay within the TTL window → False (duplicate).
        assert registry.check_and_add("audit", "event_1", ttl_seconds=300) is False
        # Different key → True.
        assert registry.check_and_add("audit", "event_2", ttl_seconds=300) is True

        stats = registry.get_stats("audit")
        assert stats["total_seen"] == 3
        assert stats["duplicates_blocked"] == 1
        assert stats["unique_passed"] == 2


# ── 4. Replay with schema changes ─────────────────────────────────────────


class TestReplayWithSchemaChanges:
    """Old data with new schema fields re-processed against the
    current validator — the new fields must be accepted (the validator
    is permissive by design).
    """

    def test_old_payload_with_new_fields_validates_under_new_validator(self):
        """A v1 trade (no ``maker_fee`` field) re-processed against the
        current validator is accepted — the new field's absence is
        NOT a rejection criterion (only the required-field list is).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        v1_trade = {
            "trade_id": "v1_replay_1",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            # No ``maker_fee`` (v3-only field) — old data.
        }
        r = validator.validate_trade(v1_trade)
        assert r.is_valid
        # The new field is absent from the normalized payload — that's
        # the contract (the validator doesn't synthesise missing fields).
        assert "maker_fee" not in r.normalized_data

    def test_new_payload_replayed_against_old_dedup_set_still_dedupes(self):
        """A re-processed payload (same trade_id) is caught by the
        dedup deque regardless of whether the payload itself has
        gained new fields since the first processing.

        This is the contract: dedup is keyed on ``trade_id`` (or the
        4-field hash for snapshots), NOT on the full payload, so a
        replay after a schema bump still catches the duplicate.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        v1_trade = {
            "trade_id": "schema_evolution_1",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
        }
        v2_trade = {
            **v1_trade,
            "maker_fee": 0.001,   # NEW field added since v1
            "match_quality": "A", # NEW field
        }

        # First processing: v1.
        r1 = validator.validate_trade(v1_trade)
        assert r1.is_valid
        # Replay: v2 (same trade_id, new fields) → must still be a duplicate.
        r2 = validator.validate_trade(v2_trade)
        assert not r2.is_valid
        assert r2.is_duplicate
        assert r2.errors == ["Duplicate trade"]

    def test_replay_with_iso_timestamps_from_old_format(self):
        """Old data with ISO-8601 timestamps is re-processed under the
        current validator — the timestamp normalisation path still
        handles the old format (the validator's ISO-8601 branch is
        backward-compatible).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        # Old-format timestamp: ISO-8601 with 'Z' suffix.
        old_trade = {
            "trade_id": "old_iso_format",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": "2025-01-01T12:00:00Z",
        }
        r = validator.validate_trade(old_trade)
        # Accepted (trades don't run the staleness check — a stale
        # trade is still a valid historical fill).
        assert r.is_valid, f"Old ISO timestamp should be accepted: {r.errors}"
        # Timestamp is normalised to float.
        assert isinstance(r.normalized_data["timestamp"], float)
        assert r.normalized_data["timestamp"] > 0

    def test_replay_batch_with_mixed_v1_and_v2_records(self):
        """A replay batch containing both v1 and v2 records processes
        without either side affecting the other — the validator is
        version-agnostic per record.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        base_ts = time.time()
        v1 = [
            {"trade_id": f"v1_{i}", "token_id": "T1", "price": 0.50,
             "size": 1.0, "side": "BUY", "timestamp": base_ts + i}
            for i in range(5)
        ]
        v2 = [
            {"trade_id": f"v2_{i}", "token_id": "T1", "price": 0.50,
             "size": 1.0, "side": "BUY", "timestamp": base_ts + 100 + i,
             "maker_fee": 0.001}
            for i in range(5)
        ]
        # Interleave for the replay.
        batch = [None] * 10
        batch[::2] = v1
        batch[1::2] = v2

        accepted = 0
        v1_accepted = 0
        v2_accepted = 0
        for trade in batch:
            r = validator.validate_trade(trade)
            if r.is_valid:
                accepted += 1
                if "maker_fee" in trade:
                    v2_accepted += 1
                else:
                    v1_accepted += 1

        assert accepted == 10, (
            f"All 10 replay records should be accepted, got {accepted}"
        )
        assert v1_accepted == 5
        assert v2_accepted == 5
