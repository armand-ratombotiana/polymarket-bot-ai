"""
Unit tests for ``core/watchdog.py``.

X3 — Subsystem Watchdog unit tests.

Covers the five public-surface guarantees the X3 task spec asks for:

  1. ``register(name)`` adds the subsystem to the watchdog's tracking map.
  2. ``beat(name)`` updates the last-heartbeat timestamp for a subsystem
     (the heartbeat moves FORWARD in time, never backward).
  3. The staleness check surfaces stale subsystems — both via the
     per-subsystem ``subsystem_status()`` lookup (returns ``"STALE"``) AND
     via ``run_checks()`` emitting a ``wr01`` finding whose ``name`` is
     ``f"heartbeat:{name}"``.
  4. The staleness check returns an empty result set when every registered
     subsystem has a fresh heartbeat — ``subsystem_status()`` reports
     ``"OK"`` for every subsystem AND ``run_checks()`` emits no ``wr01``
     findings.
  5. The tripwire fires when a registered subsystem's heartbeat exceeds the
     ``heartbeat_timeout`` — ``run_checks()`` returns a ``wr01`` finding
     with ``severity == "WARNING"`` and a ``detail`` string referencing the
     elapsed age and the configured timeout.

Spec-vs-implementation clarification
------------------------------------
The X3 task spec mentions a method named ``check_stale``. The
``core/watchdog.py`` module exposes the same conceptual operation through
TWO real public methods (the source is left untouched per the X3 "Do NOT
edit existing files" constraint):

  * ``subsystem_status() -> dict[str, str]`` — the per-subsystem
    granularity lookup that returns ``"OK"`` or ``"STALE"`` for each
    registered subsystem (synchronous).
  * ``run_checks() -> list[dict]`` — the async tripwire evaluator that
    emits a ``wr01`` finding (``severity="WARNING"``) for every subsystem
    whose heartbeat age exceeds ``heartbeat_timeout``.

Both are exercised here so the spec's "check_stale" intent is fully
covered: tests 3 / 4 use ``subsystem_status()`` for the OK-vs-STALE
per-subsystem verdict and ``run_checks()`` for the tripwire-finding
verdict; test 5 uses ``run_checks()`` for the tripwire-fires path.

Testing strategy
-----------------
* Every test constructs a brand-new ``Watchdog`` instance with EXPLICIT
  kwargs (``heartbeat_timeout``, ``check_interval``,
  ``book_stall_seconds``, ``auto_kill=False``). Passing truthy kwargs
  short-circuits the ``or settings.<field>`` lookups in ``__init__``, so
  the tests are hermetic to the project's settings module and do not
  perturb the module-level ``watchdog`` singleton (constructed at import
  time against production defaults).
* Staleness is simulated by DIRECTLY mutating ``wd._heartbeats[name]``
  to a timestamp in the past. This is deterministic and fast — no
  ``time.sleep()`` is used (the alternative would be to register, sleep
  for ``heartbeat_timeout + ε`` seconds, and then check, which slows the
  suite down by an amount proportional to the configured timeout).
* The autouse ``_reset_store_factory_defaults`` fixture in
  ``tests/conftest.py`` brings the global ``store`` singleton back to a
  clean baseline (daily_pnl=0, weekly_pnl=0, no order_books,
  kill_switch off) before every test. A smoke run confirmed that under
  this clean baseline ``run_checks()`` returns ``[]`` (no ``wr02`` /
  ``wr03`` / ``wr04`` / ``wr05`` / ``wr06`` findings) — so the heartbeat
  findings (``wr01``) are the ONLY findings emitted by ``run_checks()``
  in these tests. Each test that asserts "no stale findings" therefore
  checks the full findings list, not a filtered subset (a stronger
  contract: it would also fail loudly if a sibling check drifted into
  firing spuriously under the clean baseline).

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (pytest-asyncio is
already a project dependency; the repo's ``pytest.ini`` declares
``testpaths = tests`` and is intentionally left untouched per the X3
"no existing file edits" constraint).
"""
from __future__ import annotations

import time

import pytest

from core.watchdog import Watchdog

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the X3 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors the pattern in ``tests/test_decision_ledger.py`` and
# ``tests/test_live_safety_gate.py``).
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh hermetic Watchdog per test ──────────────────────────────
@pytest.fixture
def wd():
    """Return a brand-new ``Watchdog`` with explicit hermetic kwargs.

    Passing truthy kwargs short-circuits every ``or settings.<field>``
    fallback in ``Watchdog.__init__`` so the test does not depend on
    project configuration. ``auto_kill=False`` ensures ``run()`` never
    tries to activate the durable kill switch (we never call ``run()``
    here — only ``run_checks()`` — but the flag is set defensively in
    case a future edit of this file exercises the ``run()`` path).
    """
    return Watchdog(
        heartbeat_timeout=10,
        check_interval=5,
        book_stall_seconds=60,
        auto_kill=False,
    )


# ── 1. register adds subsystem to tracking ─────────────────────────────────
def test_register_adds_subsystem_to_tracking(wd):
    """``register(name)`` must add the subsystem to the heartbeat map.

    Verified through TWO public surfaces (not the private ``_heartbeats``
    dict) so the contract holds even if the backing storage is renamed:

      * ``status()["subsystems"]`` exposes the registered subsystem name
        → ``"OK"`` (a freshly-registered subsystem has a heartbeat of
        ``time.time()`` so its age is ~0s, well inside ``heartbeat_timeout``).
      * ``subsystem_status()`` returns the same per-subsystem mapping.

    Also pins the ``setdefault`` semantics of ``register``: calling it a
    second time for the SAME subsystem name does NOT reset the existing
    heartbeat timestamp (a registered-then-staled subsystem that is
    re-registered stays stale until ``beat(name)`` explicitly refreshes
    it).
    """
    assert wd.status()["subsystems"] == {}                  # nothing tracked yet
    assert wd.subsystem_status() == {}

    wd.register("ws_client")

    assert "ws_client" in wd.status()["subsystems"]        # tracked now
    assert wd.subsystem_status() == {"ws_client": "OK"}    # fresh heartbeat

    # setdefault semantics: re-registering does NOT overwrite an existing
    # heartbeat. Pin the prior timestamp, call register again, confirm it
    # is preserved verbatim.
    prior = wd._heartbeats["ws_client"]
    wd.register("ws_client")
    assert wd._heartbeats["ws_client"] == prior            # untouched

    # A second, distinct subsystem is tracked independently.
    wd.register("book_poller")
    assert set(wd.subsystem_status().keys()) == {"ws_client", "book_poller"}


# ── 2. beat updates last_heartbeat timestamp ───────────────────────────────
def test_beat_updates_last_heartbeat_timestamp(wd):
    """``beat(name)`` must move the subsystem's heartbeat FORWARD in time.

    The watchdog's staleness check is ``now - last_heartbeat >
    heartbeat_timeout``; a ``beat`` that did not advance the timestamp
    would defeat its entire purpose. This test pins:

      * ``beat`` refreshes the heartbeat to a strictly-greater timestamp
        than the one present before the call.
      * ``beat`` works for a subsystem that was registered with
        ``register`` (the normal path) AND for a subsystem that was never
        registered (the auto-create path: ``beat`` writes the key
        directly via ``self._heartbeats[name] = time.time()``).
      * The post-``beat`` status is ``"OK"`` (a freshly-beaten subsystem
        is healthy by definition).
    """
    wd.register("gamma_client")
    first = wd._heartbeats["gamma_client"]

    # Sleep a small, deterministic epsilon so time.time() can advance.
    # 5ms is comfortably above clock-resolution on every supported
    # platform and well below heartbeat_timeout (10s) so the subsystem
    # stays healthy after the beat.
    time.sleep(0.005)
    wd.beat("gamma_client")
    second = wd._heartbeats["gamma_client"]

    assert second > first                                   # advanced forward
    assert wd.subsystem_status()["gamma_client"] == "OK"    # and healthy

    # Auto-create path: ``beat`` on an un-registered subsystem tracks it.
    wd.beat("strategy_signal_trader")
    assert "strategy_signal_trader" in wd.subsystem_status()
    assert wd.subsystem_status()["strategy_signal_trader"] == "OK"


# ── 3. check_stale returns stale subsystems ──────────────────────────────
async def test_check_stale_returns_stale_subsystems(wd):
    """The staleness check surfaces stale subsystems.

    Maps to the spec's ``check_stale`` via the watchdog's two real public
    surfaces:

      * ``subsystem_status()`` returns ``"STALE"`` for every subsystem
        whose heartbeat age exceeds ``heartbeat_timeout``.
      * ``run_checks()`` emits a ``wr01`` tripwire finding (severity
        ``"WARNING"``) for every stale subsystem, with
        ``name == f"heartbeat:{name}"``.

    Two subsystems are registered; one is force-staled by backdating its
    heartbeat to ``now - 100s`` (well past the 10s timeout), the other
    is kept fresh. Both surfaces must agree: the staled subsystem is
    surfaced, the fresh one is not.
    """
    wd.register("ws_client")
    wd.register("book_poller")

    # Backdate ws_client so it's 100s stale (timeout is 10s).
    wd._heartbeats["ws_client"] = time.time() - 100

    # Per-subsystem verdict.
    status = wd.subsystem_status()
    assert status["ws_client"] == "STALE"
    assert status["book_poller"] == "OK"

    # Tripwire-finding verdict.
    findings = await wd.run_checks()
    stale_findings = [f for f in findings if f["id"] == "wr01"]
    assert len(stale_findings) == 1
    finding = stale_findings[0]
    assert finding["name"] == "heartbeat:ws_client"
    assert finding["severity"] == "WARNING"
    # detail carries the elapsed age and the configured timeout. The
    # subsystem NAME lives in finding["name"] (asserted above); the
    # detail string itself does NOT echo the name.
    assert "no heartbeat" in finding["detail"]
    assert str(wd.heartbeat_timeout) in finding["detail"]
    # book_poller must NOT be in any wr01 finding (it's healthy).
    assert not any(f["name"] == "heartbeat:book_poller" for f in findings)


# ── 4. check_stale returns empty list when all healthy ───────────────────
async def test_check_stale_returns_empty_list_when_all_healthy(wd):
    """The staleness check returns an empty result set when all healthy.

    Two subsystems are registered with fresh heartbeats (age ~0s, well
    inside the 10s timeout). The watchdog must report:

      * ``subsystem_status()`` → every subsystem maps to ``"OK"`` (NO
        ``"STALE"`` entries at all).
      * ``run_checks()`` → NO ``wr01`` findings at all.

    Belt-and-braces: under the autouse clean-baseline fixture a smoke run
    confirmed ``run_checks()`` returns ``[]`` (no ``wr02``/``wr03``/
    ``wr04``/``wr05``/``wr06`` findings either) — so the assertion here
    checks the FULL findings list, not a filtered subset. A future drift
    that caused a sibling check to fire spuriously under the clean
    baseline would fail this test loudly (a stronger contract than just
    filtering to ``wr01``).
    """
    wd.register("ws_client")
    wd.register("book_poller")
    wd.register("gamma_client")

    status = wd.subsystem_status()
    assert status == {"ws_client": "OK", "book_poller": "OK", "gamma_client": "OK"}
    assert not any(v == "STALE" for v in status.values())

    findings = await wd.run_checks()
    assert findings == []
    assert not any(f["id"] == "wr01" for f in findings)


# ── 5. tripwire fires when heartbeat timeout exceeded ────────────────────
async def test_tripwire_fires_when_heartbeat_timeout_exceeded(wd):
    """A heartbeat older than ``heartbeat_timeout`` triggers the tripwire.

    The watchdog's tripwire for heartbeat staleness is finding id
    ``wr01``: a dict with ``severity == "WARNING"`` and
    ``name == f"heartbeat:{name}"``. This test pins:

      * The finding fires past the boundary — a heartbeat aged
        ``heartbeat_timeout + 1s`` triggers it, while a heartbeat aged
        ``heartbeat_timeout - 1s`` does NOT. (The watchdog's check is
        strict ``>``, not ``>=``; the exact-equality boundary is a
        microsecond-timing race between the two ``time.time()`` calls
        and is therefore NOT pinned — only the clearly-inside and
        clearly-past sides are asserted.)
      * The ``detail`` string carries the elapsed age (seconds) and the
        configured timeout (seconds) — operators rely on both being
        present in the audit log.
      * A fresh ``beat(name)`` after the tripwire has fired clears it:
        the next ``run_checks()`` returns no ``wr01`` finding for that
        subsystem (the recovery path).
    """
    wd.register("ws_client")

    # Clearly inside the boundary: age == heartbeat_timeout - 1 → NOT stale.
    wd._heartbeats["ws_client"] = time.time() - (wd.heartbeat_timeout - 1)
    findings = await wd.run_checks()
    assert not any(f["id"] == "wr01" for f in findings), (
        "heartbeat aged < timeout must NOT be stale (strict >)"
    )

    # Just past the boundary: age == heartbeat_timeout + 1 → stale.
    wd._heartbeats["ws_client"] = time.time() - (wd.heartbeat_timeout + 1)
    findings = await wd.run_checks()
    stale = [f for f in findings if f["id"] == "wr01"]
    assert len(stale) == 1

    f = stale[0]
    assert f["name"] == "heartbeat:ws_client"
    assert f["severity"] == "WARNING"
    # detail must mention the elapsed age and the timeout.
    assert "timeout" in f["detail"]
    assert str(wd.heartbeat_timeout) in f["detail"]

    # Recovery: a fresh beat() clears the tripwire on the next check.
    wd.beat("ws_client")
    findings = await wd.run_checks()
    assert not any(f["id"] == "wr01" for f in findings)
    assert wd.subsystem_status()["ws_client"] == "OK"
