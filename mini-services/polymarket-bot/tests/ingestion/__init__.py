"""W31-7 — ingestion test suite.

Stress, soak, failure-recovery, schema-change, and replay tests for the
ingestion pipeline (``core.data_validator`` / ``core.trade_ingester`` /
``core.ingestion.raw_vault`` / ``core.ingestion.source_registry`` /
``core.state_recovery``).

Sister modules:

  * ``test_stress.py``            — high-throughput / large payload / burst /
                                    sustained / memory stability.
  * ``test_failure_recovery.py`` — 10 failure modes (API downtime, network
                                    interruption, auth failure, rate limit,
                                    malformed payload, duplicates, out-of-
                                    order, DB unavailability, crash, clock
                                    drift).
  * ``test_schema_changes.py``    — new / removed / type-changed fields +
                                    schema-version bump.
  * ``test_replay.py``            — checkpoint resume, raw-vault replay,
                                    idempotent reprocessing, schema-evolved
                                    replay.

The package is intentionally minimal — every test module is self-contained
(re-redirects every persisted-state path to ``/tmp`` via ``setdefault``
BEFORE the first project import, mirrors the established pattern in
``tests/test_state_recovery.py`` / ``tests/test_soak_test.py``).
"""
