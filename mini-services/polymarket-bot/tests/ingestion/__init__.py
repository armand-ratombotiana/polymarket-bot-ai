"""W33-1 — ingestion test suite (Pipeline / DLQ / Checkpoint).

Stress, failure-recovery, and replay tests for the unified ingestion
pipeline introduced in W31-1 / W31-4 / W32-2 / W32-4:

  * ``ingestion.pipeline.Pipeline``          — the unified 5-stage
    ingestion pipeline (validate → raw-vault → normalize → enrich →
    route) wired with a per-test ``RawVault``.
  * ``ingestion.dead_letter.DeadLetterQueue`` — the SQLite-backed DLQ
    that captures records the pipeline couldn't store downstream.
  * ``ingestion.checkpoint.CheckpointManager`` — the SQLite-backed
    checkpoint store the pipeline uses to resume after a crash.
  * ``ingestion.raw_vault.RawVault``           — the immutable raw-
    observation store; the ``replay()`` / ``replay_range()`` methods
    are the contract under test for the replay suite.

Sister modules:

  * ``test_stress.py``            — high-throughput / large payload /
                                    burst / sustained / memory stability.
  * ``test_failure_recovery.py`` — 10 failure modes (API downtime,
                                    network interruption, auth failure,
                                    rate limit, malformed payload,
                                    duplicates, out-of-order, DB
                                    unavailable, crash, clock drift).
  * ``test_replay.py``            — checkpoint resume, raw-vault replay,
                                    idempotent reprocessing, schema-evolved
                                    replay.

The package is intentionally minimal — every test module is self-
contained (re-redirects every persisted-state path to ``/tmp`` via
``setdefault`` BEFORE the first project import, mirrors the established
pattern in ``tests/test_state_recovery.py`` /
``tests/test_soak_test.py``).
"""
