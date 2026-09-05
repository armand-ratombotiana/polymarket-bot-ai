"""W31-7 — ingestion schema-change tests.

Four schema-evolution scenarios required by the W31-7 task spec:

  1. **New field added**     — source adds a new field; system handles
                                (extra field is preserved through the
                                validator's ``{**raw_data, ...}`` spread).
  2. **Field removed**         — source removes a required field; system
                                rejects the record with a clear error
                                message.
  3. **Field type changed**    — field type changes (e.g. ``best_bid``
                                from ``float`` to ``str``); system
                                detects via the value-range check.
  4. **Schema version bump**   — data carries a ``schema_version`` field
                                the validator hasn't seen; system
                                handles (the version is preserved as an
                                extra field — the validator is
                                permissive by design, the durability
                                layer enforces version compatibility).

Scope
~~~~~
The schema validator is the only ingestion-stage component that
inspects the payload's shape (``core.data_validator.DataValidator``).
The raw vault stores payloads verbatim (no schema enforcement) and
the database manager accepts whatever the validator's normalized_data
contains — so the schema-change tests target the validator.

Why the validator is permissive by default
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The polymarket-bot's contract with the upstream CLOB is "accept any
well-formed JSON; reject only records that fail the value-range /
staleness / dedup checks." A new field (e.g. ``maker_fee``) the
validator hasn't seen is preserved through the ``{**raw_data, ...}``
spread so downstream consumers (the DB writer, the audit logger)
see the extra field without the validator having to be updated.

The tests below verify that permissive contract holds for new
fields, while the required-field and value-range checks still catch
removed fields and type changes.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE the first import. ──
_TMP_ROOT = Path("/tmp/pmbot_w31_7_ingestion_schema_changes")
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
    "API_TOKEN": "test-token-w31-7-schema",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

ASYNC = pytest.mark.asyncio


# ── 1. New field added ────────────────────────────────────────────────────


class TestNewFieldAdded:
    """Source adds a new field; system handles."""

    def test_extra_field_preserved_in_normalized_trade(self):
        """A trade carrying an unknown ``maker_fee`` field passes
        through the validator's ``{**raw_data, ...}`` spread and lands
        in ``normalized_data``.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "new_field_1",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            "maker_fee": 0.001,        # NEW — validator hasn't seen this
            "taker_fee": 0.002,        # NEW
            "fee_token": "USDC",       # NEW
        }
        r = validator.validate_trade(trade)
        assert r.is_valid, f"Extra fields should not cause rejection: {r.errors}"
        # Every extra field is preserved.
        assert r.normalized_data["maker_fee"] == 0.001
        assert r.normalized_data["taker_fee"] == 0.002
        assert r.normalized_data["fee_token"] == "USDC"
        # Required fields still present.
        assert r.normalized_data["trade_id"] == "new_field_1"
        assert r.normalized_data["price"] == 0.50

    def test_extra_field_preserved_in_normalized_snapshot(self):
        """A snapshot carrying an unknown ``order_book_depth`` field
        passes through to the normalized payload.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        snap = {
            "token_id": "T1",
            "best_bid": 0.49,
            "best_ask": 0.51,
            "timestamp": time.time(),
            "order_book_depth": 1000,         # NEW
            "market_slug": "will-btc-hit-100k", # NEW
            "resolution_source": "uma_oracle", # NEW
        }
        r = validator.validate_snapshot(snap)
        assert r.is_valid, f"Extra fields should not cause rejection: {r.errors}"
        assert r.normalized_data["order_book_depth"] == 1000
        assert r.normalized_data["market_slug"] == "will-btc-hit-100k"
        assert r.normalized_data["resolution_source"] == "uma_oracle"

    def test_extra_nested_object_field_preserved(self):
        """A nested object field (e.g. ``metadata``) is preserved
        verbatim — the validator does NOT flatten / parse nested
        structures.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "new_field_nested",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            "metadata": {
                "client_order_id": "abc-123",
                "strategy": "market_maker",
                "tags": ["momentum", "high_freq"],
            },
        }
        r = validator.validate_trade(trade)
        assert r.is_valid
        # Nested object survives the spread.
        assert r.normalized_data["metadata"]["client_order_id"] == "abc-123"
        assert r.normalized_data["metadata"]["tags"] == ["momentum", "high_freq"]


# ── 2. Field removed ──────────────────────────────────────────────────────


class TestFieldRemoved:
    """Source removes a required field; system rejects with a clear error."""

    def test_removed_token_id_in_trade_rejected(self):
        """Trade missing ``token_id`` → ``is_valid=False``, errors include
        ``"Missing required field: token_id"``.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "missing_token",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            # ``token_id`` REMOVED.
            "timestamp": time.time(),
        }
        r = validator.validate_trade(trade)
        assert not r.is_valid
        assert any("token_id" in e for e in r.errors), (
            f"Expected 'Missing required field: token_id' error, got: {r.errors}"
        )

    def test_removed_best_bid_in_snapshot_rejected(self):
        """Snapshot missing ``best_bid`` → ``is_valid=False``."""
        from core.data_validator import DataValidator

        validator = DataValidator()
        snap = {
            "token_id": "T1",
            "best_ask": 0.51,
            # ``best_bid`` REMOVED.
            "timestamp": time.time(),
        }
        r = validator.validate_snapshot(snap)
        assert not r.is_valid
        assert any("best_bid" in e for e in r.errors)

    def test_removed_optional_field_does_not_reject(self):
        """Removing an OPTIONAL field (e.g. ``maker_address`` on a trade)
        does not reject the record — the validator only enforces the
        required-field list.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "no_optional",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            # ``maker_address`` / ``taker_order_id`` removed.
        }
        r = validator.validate_trade(trade)
        assert r.is_valid, f"Optional-field removal should not reject: {r.errors}"


# ── 3. Field type changed ──────────────────────────────────────────────────


class TestFieldTypeChanged:
    """Field type changes; system detects via value validation."""

    def test_price_changed_from_float_to_non_numeric_string(self):
        """``price`` was ``0.50`` (float), now ``"half"`` (non-numeric
        string). The validator coerces to float; coercion fails →
        ``price_f = -1.0`` → ``<= 0`` check trips → error appended.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "type_change_1",
            "token_id": "T1",
            "price": "half",     # was 0.50 (float), now str
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
        }
        r = validator.validate_trade(trade)
        assert not r.is_valid
        assert any("Invalid price" in e for e in r.errors), (
            f"Expected 'Invalid price' error, got: {r.errors}"
        )

    def test_best_bid_changed_from_float_to_string_one(self):
        """``best_bid`` as the string ``"0.50"`` is ACCEPTED — the
        validator's ``_is_in_unit_range`` coerces to float before
        checking, so numeric strings survive. This is the contract:
        numeric strings are equivalent to numeric literals; the
        validator normalises them.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        snap = {
            "token_id": "T1",
            "best_bid": "0.49",   # str, but numeric
            "best_ask": 0.51,
            "timestamp": time.time(),
        }
        r = validator.validate_snapshot(snap)
        assert r.is_valid, f"Numeric string should be accepted: {r.errors}"

    def test_best_bid_changed_to_list_rejected(self):
        """``best_bid`` as a list ``[0.49, 0.48]`` — the validator's
        ``_is_in_unit_range`` returns False (lists aren't numeric), so
        the record is rejected.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        snap = {
            "token_id": "T1",
            "best_bid": [0.49, 0.48],   # WRONG type — list, not float
            "best_ask": 0.51,
            "timestamp": time.time(),
        }
        r = validator.validate_snapshot(snap)
        assert not r.is_valid
        assert any("Invalid best_bid" in e for e in r.errors), (
            f"Expected 'Invalid best_bid' error for list type, got: {r.errors}"
        )

    def test_side_changed_from_enum_string_to_int_rejected(self):
        """``side`` as the integer ``1`` (instead of the string
        ``"BUY"``) is rejected — the validator upper-cases the string,
        then checks membership in ``("BUY", "SELL")``. An integer
        fails the membership check.
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "type_change_side",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": 1,   # was "BUY" (str), now int
            "timestamp": time.time(),
        }
        r = validator.validate_trade(trade)
        assert not r.is_valid
        assert any("Invalid side" in e for e in r.errors)


# ── 4. Schema version bump ────────────────────────────────────────────────


class TestSchemaVersionBump:
    """Data carries a ``schema_version`` field the validator hasn't seen."""

    def test_schema_version_field_preserved(self):
        """A trade with ``schema_version=2`` is accepted (the version is
        an unknown field → preserved through the spread).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "v2_trade",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            "schema_version": 2,   # NEW — validator hasn't seen this
        }
        r = validator.validate_trade(trade)
        assert r.is_valid, (
            f"schema_version field should not cause rejection: {r.errors}"
        )
        assert r.normalized_data["schema_version"] == 2

    def test_schema_version_bump_does_not_invalidate_legacy_fields(self):
        """A v3 trade with NEW v3-only fields still validates because
        the validator's contract is "accept any well-formed JSON."
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        trade = {
            "trade_id": "v3_trade",
            "token_id": "T1",
            "price": 0.50,
            "size": 1.0,
            "side": "BUY",
            "timestamp": time.time(),
            "schema_version": 3,
            # v3-only fields:
            "match_quality": "A+",
            "settlement_window": 60,
            "is_block_trade": False,
        }
        r = validator.validate_trade(trade)
        assert r.is_valid
        assert r.normalized_data["match_quality"] == "A+"
        assert r.normalized_data["settlement_window"] == 60
        assert r.normalized_data["is_block_trade"] is False

    def test_mixed_schema_versions_in_same_batch(self):
        """A batch with v1, v2, v3 records all processes successfully —
        the validator is version-agnostic by design (it inspects
        fields, not the version tag).
        """
        from core.data_validator import DataValidator

        validator = DataValidator()
        versions: list[int] = []
        base_ts = time.time()
        for i, version in enumerate([1, 2, 3, 1, 2, 3]):
            trade = {
                "trade_id": f"v{version}_trade_{i}",
                "token_id": "T1",
                "price": 0.50,
                "size": 1.0,
                "side": "BUY",
                "timestamp": base_ts + i,
                "schema_version": version,
            }
            # Add version-specific fields.
            if version >= 2:
                trade["maker_fee"] = 0.001
            if version >= 3:
                trade["match_quality"] = "A"
            r = validator.validate_trade(trade)
            assert r.is_valid, (
                f"v{version} trade rejected: {r.errors}"
            )
            versions.append(version)

        # All 6 trades accepted, every version present in the output.
        assert len(versions) == 6
        assert set(versions) == {1, 2, 3}
        stats = validator.get_stats()
        assert stats["valid_count"] == 6
        assert stats["invalid_count"] == 0
