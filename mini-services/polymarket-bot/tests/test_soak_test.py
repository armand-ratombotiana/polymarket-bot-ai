"""Tests for the W26-4 soak test runner + ``POST /api/system/soak-test``.

Six test classes:

  (1) ``TestSoakTestCheck``        — dataclass shape + ``__dict__``
                                     serialisation.

  (2) ``TestSoakTestReport``        — dataclass shape + ``to_dict``
                                     serialisation + ``errors`` default-
                                     factory isolation.

  (3) ``TestSoakTestRunnerShort``  — the ``run(duration_override=...)``
                                     method completes within the expected
                                     window, populates the report, drives
                                     ``overall_pass`` from the check
                                     verdicts + recorded errors, and
                                     respects the ``stop()`` early-exit
                                     signal.

  (4) ``TestIndividualChecks``     — direct coverage of each ``_check_*``
                                     method (api / memory / audit / db /
                                     dedup / error_rate). Each check is
                                     exercised against the live
                                     conftest-redirected singletons so
                                     the wiring (immutable_audit /
                                     db_manager / dedup_registry /
                                     observability) is verified end-to-
                                     end, not just the unit contract.

  (5) ``TestSoakTestRunnerErrors`` — errors recorded inside the run loop
                                     are surfaced on the report's
                                     ``errors`` list and flip
                                     ``overall_pass`` to ``False`` even
                                     when every final-tick check passes.

  (6) ``TestSoakTestAPIRoute``     — HTTP-level coverage of the
                                     ``POST /api/system/soak-test``
                                     endpoint on the production app
                                     (registration / tags / auth /
                                     response shape).

W26-4 — the runner is async, so async tests use ``pytest.mark.asyncio``
explicitly (per-test mark, NOT module-level ``pytestmark``, mirroring the
convention in ``tests/test_state_recovery_wiring.py`` — sync tests that
source-inspect the route handlers don't trip pytest-asyncio's "marked
but not async" warning).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads ``os.environ`` at module-import time. ──────────
# Mirrors the env-redirect block in ``tests/test_state_recovery_wiring.py``
# — keeps this test file hermetic if it's the first sibling imported
# (conftest.py does the same setdefault, but ``setdefault`` is a no-op if
# either file has already set the key, so the redundancy is harmless).
_TMP_ROOT = Path("/tmp/pmbot_w26_4_soak_tests")
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
    "API_TOKEN": "test-token-w26-4",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*`` / ``api.*``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

# Per-test asyncio marker (NOT module-level ``pytestmark``) so the SYNC
# source-inspecting tests below don't trip pytest-asyncio's "marked but
# not async" warning. Mirrors the convention in
# ``tests/test_state_recovery_wiring.py``.
ASYNC = pytest.mark.asyncio

# ``conftest.py`` sets ``API_TOKEN`` via ``os.environ.setdefault`` BEFORE
# any project module is imported. The redirect block above sets a file-local
# ``API_TOKEN`` (``test-token-w26-4``) ONLY if conftest hasn't already set
# one — but conftest IS imported before this file, so the value below
# reflects whatever the conftest-redirected env won with. Resolving the
# token at import time (rather than hard-coding a value) makes the test
# robust to a future conftest token rotation without a coupled edit here.
VALID_TOKEN = os.environ.get("API_TOKEN", "test-token-conftest")


# ── (1) TestSoakTestCheck — dataclass shape ──────────────────────────────────


class TestSoakTestCheck:
    """``SoakTestCheck`` dataclass contract."""

    def test_dataclass_fields(self):
        from core.soak_test import SoakTestCheck

        c = SoakTestCheck("name", True, "value", "threshold", "msg")
        assert c.name == "name"
        assert c.passed is True
        assert c.value == "value"
        assert c.threshold == "threshold"
        assert c.message == "msg"

    def test_dataclass_dict_is_json_serialisable(self):
        """``__dict__`` produces a plain dict the FastAPI JSON encoder
        can serialise directly (no enum / dataclass / numpy scalars)."""
        from core.soak_test import SoakTestCheck

        c = SoakTestCheck("name", False, 42, "<10", "failed")
        assert c.__dict__ == {
            "name": "name",
            "passed": False,
            "value": 42,
            "threshold": "<10",
            "message": "failed",
        }

    def test_value_accepts_arbitrary_type(self):
        """``value: Any`` accepts dicts (the dedup check returns the
        full stats dict) without a TypeError."""
        from core.soak_test import SoakTestCheck

        c = SoakTestCheck("dedup_active", True, {"order": 5}, "active", "ok")
        assert c.value == {"order": 5}


# ── (2) TestSoakTestReport — aggregate report dataclass ──────────────────────


class TestSoakTestReport:
    """``SoakTestReport`` dataclass + ``to_dict`` serialisation."""

    def test_to_dict_shape(self):
        from core.soak_test import SoakTestCheck, SoakTestReport

        checks = [SoakTestCheck("c1", True, 1, "<10", "ok")]
        r = SoakTestReport(
            duration_seconds=5.0,
            overall_pass=True,
            checks=checks,
            metrics={"foo": 1},
            started_at=1000.0,
            ended_at=1005.0,
            errors=[],
        )
        d = r.to_dict()
        assert d["duration_seconds"] == 5.0
        assert d["overall_pass"] is True
        assert d["checks"][0]["name"] == "c1"
        assert d["metrics"] == {"foo": 1}
        assert d["started_at"] == 1000.0
        assert d["ended_at"] == 1005.0
        assert d["errors"] == []

    def test_to_dict_serialises_each_check_via_dict(self):
        """``to_dict`` returns each check as its ``__dict__`` (NOT the
        dataclass instance) so FastAPI's JSON encoder doesn't choke on
        the dataclass type."""
        from core.soak_test import SoakTestCheck, SoakTestReport

        c = SoakTestCheck("c1", True, "100ms", "<5000ms", "ok")
        r = SoakTestReport(
            duration_seconds=1.0,
            overall_pass=True,
            checks=[c],
            metrics={},
            started_at=0.0,
            ended_at=1.0,
        )
        d = r.to_dict()
        assert d["checks"] == [c.__dict__]
        assert isinstance(d["checks"][0], dict)

    def test_errors_default_factory_is_isolated_between_instances(self):
        """``errors`` uses ``field(default_factory=list)`` so two fresh
        reports don't share the same list (the classic mutable-default
        bug)."""
        from core.soak_test import SoakTestReport

        r1 = SoakTestReport(0, True, [], {}, 0, 0)
        r2 = SoakTestReport(0, True, [], {}, 0, 0)
        r1.errors.append("foo")
        assert r2.errors == []

    def test_to_dict_returns_a_copy_of_errors(self):
        """``to_dict`` returns ``list(self.errors)`` (NOT the live list)
        so a caller mutating the returned dict's ``errors`` doesn't
        perturb the report's in-memory state."""
        from core.soak_test import SoakTestReport

        r = SoakTestReport(0, True, [], {}, 0, 0, errors=["e1"])
        d = r.to_dict()
        d["errors"].append("e2")
        assert r.errors == ["e1"]


# ── (3) TestSoakTestRunnerShort — run() loop contract ────────────────────────


class TestSoakTestRunnerShort:
    """The ``run(duration_override=...)`` loop completes within the
    expected window, populates the report, and respects ``stop()``."""

    @ASYNC
    async def test_run_completes_within_expected_window(self, monkeypatch):
        """A 1 s soak completes in < 5 s wall-clock and produces a
        report with all 6 checks populated. Stubs the API + DB + metrics
        checks so the test doesn't depend on a live server or perturb
        the conftest-redirected DB."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        # Shrink the check interval so the loop iterates within the
        # 1 s duration (default 60 s would skip every iteration).
        runner._check_interval = 0.1

        async def _stub_api():
            return SoakTestCheck("api_responds", True, "100ms", "<5000ms", "stub")

        async def _stub_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "stub")

        async def _stub_metrics():
            return {"stub": True}

        monkeypatch.setattr(runner, "_check_api_responds", _stub_api)
        monkeypatch.setattr(runner, "_check_db_writable", _stub_db)
        monkeypatch.setattr(runner, "_collect_metrics", _stub_metrics)

        start = time.time()
        report = await runner.run(duration_override=1)
        elapsed = time.time() - start

        # Should complete in ~1-1.5 s (1 s duration + final tick). Allow
        # a generous 5 s ceiling for slow CI sandboxes.
        assert elapsed < 5, f"soak took {elapsed:.1f}s — expected < 5s"
        # All 6 checks populated.
        assert len(report.checks) == 6
        check_names = [c.name for c in report.checks]
        assert check_names == [
            "api_responds",
            "memory_stable",
            "audit_chain_intact",
            "db_writable",
            "dedup_active",
            "error_rate",
        ]
        # Duration is at least the configured 1 s.
        assert report.duration_seconds >= 1.0

    @ASYNC
    async def test_run_returns_overall_pass_when_all_checks_pass(self, monkeypatch):
        """When every check returns ``passed=True`` and no errors are
        recorded, ``overall_pass`` is ``True``. Stubs every external-state
        check (api / audit chain / db) so the test is hermetic against the
        shared conftest-redirected singletons."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        runner._check_interval = 0.1

        async def _ok_api():
            return SoakTestCheck("api_responds", True, "100ms", "<5000ms", "ok")

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        monkeypatch.setattr(runner, "_check_api_responds", _ok_api)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        report = await runner.run(duration_override=0.1)
        assert report.overall_pass is True
        assert all(c.passed for c in report.checks)
        assert report.errors == []

    @ASYNC
    async def test_run_returns_overall_fail_when_a_check_fails(self, monkeypatch):
        """A single failing check flips ``overall_pass`` to ``False``
        even when no errors were recorded."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        runner._check_interval = 0.1

        async def _failing_api():
            return SoakTestCheck(
                "api_responds", False, "error", "200", "connection refused"
            )

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        monkeypatch.setattr(runner, "_check_api_responds", _failing_api)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        report = await runner.run(duration_override=0.1)
        assert report.overall_pass is False
        # The api_responds check is the only failure.
        api_check = next(c for c in report.checks if c.name == "api_responds")
        assert api_check.passed is False

    @ASYNC
    async def test_stop_signals_loop_exit_before_duration(self, monkeypatch):
        """``stop()`` causes the run loop to exit before the configured
        duration — the soak's contract is "stop on demand" so an operator
        can cancel a long soak without killing the process."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        runner._check_interval = 0.1

        async def _ok_api():
            return SoakTestCheck("api_responds", True, "100ms", "<5000ms", "ok")

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        monkeypatch.setattr(runner, "_check_api_responds", _ok_api)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        # Schedule stop() to fire after 0.5 s — the run loop should exit
        # shortly after, well before the 60 s duration_override.
        async def _stop_after_delay():
            await asyncio.sleep(0.5)
            runner.stop()

        stop_task = asyncio.create_task(_stop_after_delay())

        start = time.time()
        report = await runner.run(duration_override=60)
        elapsed = time.time() - start

        # The stop task has already completed (stop() was called at 0.5 s).
        if not stop_task.done():
            stop_task.cancel()

        # Should exit within ~1 s (0.5 s stop delay + a couple of ticks).
        assert elapsed < 5, f"soak took {elapsed:.1f}s — expected < 5s"
        # Final tick still runs — the report is populated.
        assert len(report.checks) == 6

    @ASYNC
    async def test_run_uses_default_duration_when_override_is_none(self, monkeypatch):
        """``run(duration_override=None)`` falls back to ``self.duration``.
        Verifies the None-vs-0 distinction (None = use default; 0 = run
        once and exit immediately)."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner(duration_seconds=0.05)  # 50 ms default
        runner._check_interval = 0.01

        async def _ok_api():
            return SoakTestCheck("api_responds", True, "100ms", "<5000ms", "ok")

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        monkeypatch.setattr(runner, "_check_api_responds", _ok_api)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        # No override → uses the 50 ms default.
        report = await runner.run()
        assert report.duration_seconds >= 0.05


# ── (4) TestIndividualChecks — per-check unit coverage ───────────────────────


class TestIndividualChecks:
    """Each ``_check_*`` method returns a ``SoakTestCheck`` with the
    expected name and never raises (the soak's fail-soft contract)."""

    @ASYNC
    async def test_check_api_responds_returns_check_without_crashing(self):
        """``_check_api_responds`` returns a ``SoakTestCheck`` even when
        no live server is running on localhost:8080 — the check's
        contract is "never raise"."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        check = await runner._check_api_responds()
        assert isinstance(check, SoakTestCheck)
        assert check.name == "api_responds"
        # Whether the check passes depends on whether a live server is
        # running on localhost:8080 in the test environment — either
        # outcome is acceptable as long as the check doesn't raise.

    def test_check_memory_stable_passes_under_1gb(self):
        """``_check_memory_stable`` passes for the test process (RSS is
        well under the 1 GB threshold)."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        check = runner._check_memory_stable()
        assert check.name == "memory_stable"
        # psutil is installed in the test env, so the check should
        # produce a real RSS reading + pass.
        assert check.passed is True
        # Value is a "NNNMB" string.
        assert "MB" in str(check.value)

    def test_check_memory_stable_returns_na_when_psutil_missing(self, monkeypatch):
        """When psutil isn't importable, the check returns ``passed=True``
        with value ``"N/A"`` (can't verify — don't fail)."""
        from core import soak_test as _st_module

        # Force the ``import psutil`` inside _check_memory_stable to fail
        # by making ``builtins.__import__`` raise for the psutil module.
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __builtins__["__import__"]

        def _fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("simulated: psutil not installed")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)

        runner = _st_module.SoakTestRunner()
        check = runner._check_memory_stable()
        assert check.passed is True
        assert check.value == "N/A"
        assert "psutil not available" in check.message

    def test_check_decision_chain_passes_when_verify_returns_valid(self, monkeypatch):
        """``_check_decision_chain`` passes when ``verify_chain()`` returns
        ``valid=True``. Stubs ``verify_chain`` so the test is hermetic —
        the shared conftest-redirected ``immutable_audit.db`` accumulates
        entries across test sessions (some of which may have corrupted
        the chain), so the unit test can't rely on the live DB's state."""
        from core import immutable_audit as _ia_module
        from core.soak_test import SoakTestRunner

        monkeypatch.setattr(
            _ia_module.immutable_audit,
            "verify_chain",
            lambda: {"valid": True, "checked": 5, "broken_at": None, "last_hash": "abc"},
        )

        runner = SoakTestRunner()
        check = runner._check_decision_chain()
        assert check.name == "audit_chain_intact"
        assert check.passed is True
        assert check.value == 5

    def test_check_decision_chain_fails_when_verify_returns_invalid(self, monkeypatch):
        """``_check_decision_chain`` fails when ``verify_chain()`` returns
        ``valid=False`` — surfaces the broken-at entry id in the message."""
        from core import immutable_audit as _ia_module
        from core.soak_test import SoakTestRunner

        monkeypatch.setattr(
            _ia_module.immutable_audit,
            "verify_chain",
            lambda: {"valid": False, "checked": 10, "broken_at": 4, "last_hash": None},
        )

        runner = SoakTestRunner()
        check = runner._check_decision_chain()
        assert check.name == "audit_chain_intact"
        assert check.passed is False
        assert check.value == 10

    @ASYNC
    async def test_check_db_writable_succeeds(self):
        """``_check_db_writable`` succeeds against the conftest-redirected
        SQLite DB. Belt-and-braces with the per-check unit test — verifies
        the wiring (``db_manager.record_snapshot`` accepts the spec'd
        kwargs + writes a row)."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        check = await runner._check_db_writable()
        assert check.name == "db_writable"
        assert check.passed is True
        assert check.value == "OK"

    def test_check_dedup_active_returns_check(self):
        """``_check_dedup_active`` always passes when the registry is
        importable (it's in-memory — can't be "down")."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        check = runner._check_dedup_active()
        assert check.name == "dedup_active"
        assert check.passed is True
        # Value is the full stats dict (dict-of-dicts keyed by entity_type).
        assert isinstance(check.value, dict)

    def test_check_error_rate_with_no_errors(self):
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        runner._errors = []
        check = runner._check_error_rate()
        assert check.name == "error_rate"
        assert check.passed is True
        assert check.value == 0

    def test_check_error_rate_with_few_errors_still_passes(self):
        """9 errors in the last minute — under the 10-error threshold."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        now = time.time()
        runner._errors = [f"{now}: error {i}" for i in range(9)]
        check = runner._check_error_rate()
        assert check.passed is True
        assert check.value == 9

    def test_check_error_rate_with_many_errors_fails(self):
        """15 errors in the last minute — over the 10-error threshold."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        now = time.time()
        runner._errors = [f"{now}: error {i}" for i in range(15)]
        check = runner._check_error_rate()
        assert check.passed is False
        assert check.value == 15

    def test_check_error_rate_ignores_old_errors(self):
        """Errors older than 60 s are excluded from the count."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        # 20 errors, all 120 s old — should be filtered out.
        old_ts = time.time() - 120
        runner._errors = [f"{old_ts}: error {i}" for i in range(20)]
        check = runner._check_error_rate()
        assert check.passed is True
        assert check.value == 0

    def test_check_error_rate_tolerates_malformed_entries(self):
        """Malformed error strings (no parseable timestamp) are counted
        as recent so the check surfaces the malformation rather than
        silently dropping it."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        runner._errors = [
            "not-a-timestamp: malformed entry",
            "another malformed entry without colon",
        ]
        check = runner._check_error_rate()
        # Both entries are counted as recent (under the 10 threshold).
        assert check.value == 2
        assert check.passed is True

    @ASYNC
    async def test_collect_metrics_returns_observability_report(self):
        """``_collect_metrics`` returns the structured health report
        from ``observability.get_health_report``."""
        from core.soak_test import SoakTestRunner

        runner = SoakTestRunner()
        metrics = await runner._collect_metrics()
        # Either an empty dict (if observability is unavailable) OR a
        # dict with the canonical ``categories`` key — both are valid.
        assert isinstance(metrics, dict)
        if metrics:
            assert "categories" in metrics or "generated_at" in metrics


# ── (5) TestSoakTestRunnerErrors — error recording contract ─────────────────


class TestSoakTestRunnerErrors:
    """Errors raised inside the run loop are recorded on the report's
    ``errors`` list and flip ``overall_pass`` to ``False``."""

    @ASYNC
    async def test_error_in_check_loop_is_recorded_on_report(self, monkeypatch):
        """A check method that raises is caught by the ``try/except``
        inside ``run`` and the exception is appended to ``_errors`` —
        the soak continues (doesn't crash) and the final report surfaces
        the error."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        runner._check_interval = 0.05

        async def _raising_check():
            raise RuntimeError("simulated check failure")

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        # Stub the api_responds check to raise — the run loop must catch
        # the RuntimeError, append it to ``_errors``, and continue. The
        # final-tick ``_run_checks`` raises too (api_responds still raises),
        # so the final-tick try/except in ``run`` catches it + appends to
        # ``_errors`` + returns ``final_checks=[]``.
        monkeypatch.setattr(runner, "_check_api_responds", _raising_check)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        report = await runner.run(duration_override=0.2)

        # At least one error recorded (the loop runs multiple times in
        # 0.2 s with a 0.05 s interval, plus the final tick).
        assert len(report.errors) >= 1
        assert any("simulated check failure" in e for e in report.errors)
        # overall_pass is False because errors were recorded.
        assert report.overall_pass is False

    @ASYNC
    async def test_no_errors_when_all_checks_succeed(self, monkeypatch):
        """When every check returns without raising, ``errors`` is empty
        and ``overall_pass`` reflects only the check verdicts. Stubs every
        external-state check so the test is hermetic."""
        from core.soak_test import SoakTestCheck, SoakTestRunner

        runner = SoakTestRunner()
        runner._check_interval = 0.05

        async def _ok_api():
            return SoakTestCheck("api_responds", True, "100ms", "<5000ms", "ok")

        def _ok_audit():
            return SoakTestCheck("audit_chain_intact", True, 0, "valid", "ok")

        async def _ok_db():
            return SoakTestCheck("db_writable", True, "OK", "OK", "ok")

        async def _ok_metrics():
            return {}

        monkeypatch.setattr(runner, "_check_api_responds", _ok_api)
        monkeypatch.setattr(runner, "_check_decision_chain", _ok_audit)
        monkeypatch.setattr(runner, "_check_db_writable", _ok_db)
        monkeypatch.setattr(runner, "_collect_metrics", _ok_metrics)

        report = await runner.run(duration_override=0.1)
        assert report.errors == []
        assert report.overall_pass is True


# ── (6) TestSoakTestAPIRoute — HTTP-level coverage ───────────────────────────


class TestSoakTestAPIRoute:
    """HTTP-level coverage of ``POST /api/system/soak-test`` on the
    production app.

    Mirrors the pattern in ``tests/test_state_recovery_wiring.py``'s
    ``TestRecoveryReportEndpoint`` class — uses the production
    ``api.server.app`` (NOT a hand-rolled minimal FastAPI app) so the
    test verifies the wiring chain (route registration → auth
    middleware → handler → runner → response) end-to-end.
    """

    def test_endpoint_registered_on_production_app(self):
        """``POST /api/system/soak-test`` must be registered on the
        production ``api.server.app`` so an operator (or a CI soak job)
        can kick off a soak test via the standard REST surface."""
        from api.server import app

        paths = {
            getattr(r, "path", None)
            for r in app.routes
            if hasattr(r, "path")
        }
        assert "/api/system/soak-test" in paths, (
            "``POST /api/system/soak-test`` must be registered on the "
            "production app — W26-4 contract: the endpoint is the "
            "operator-facing surface for the soak test runner."
        )

    def test_endpoint_tags_include_system(self):
        """The route's ``tags`` must include ``"system"`` so the OpenAPI
        / Swagger UI groups it with the other system routes
        (``/api/system/mode`` / ``/api/system/health`` /
        ``/api/system/recovery-report``)."""
        from api.server import app

        for r in app.routes:
            if getattr(r, "path", None) == "/api/system/soak-test":
                tags = getattr(r, "tags", []) or []
                assert "system" in tags, (
                    "``/api/system/soak-test`` must be tagged "
                    "``\"system\"`` so it's grouped correctly in the "
                    "OpenAPI / Swagger UI — W26-4 contract."
                )
                return
        pytest.fail("route registered but tags not found — investigate")

    def test_endpoint_uses_post_method(self):
        """The route must use ``POST`` (not GET) because it kicks off a
        long-running soak that mutates the runner singleton's internal
        state (``_running`` flag) — GET routes must be side-effect-free
        per REST convention."""
        from api.server import app

        for r in app.routes:
            if getattr(r, "path", None) == "/api/system/soak-test":
                methods = set(getattr(r, "methods", []) or [])
                assert "POST" in methods, (
                    "``/api/system/soak-test`` must accept POST — W26-4 "
                    "contract: the soak mutates the runner's state."
                )
                return
        pytest.fail("route not registered — investigate")

    def test_endpoint_requires_auth(self):
        """``POST /api/system/soak-test`` must NOT be in ``PUBLIC_PATHS``
        — the soak report includes internal state (memory / DB / audit
        chain / dedup stats) that an unauthenticated client must not see.
        Belt-and-braces with the W11-6 fail-closed auth contract."""
        from api.server import PUBLIC_PATHS

        assert "/api/system/soak-test" not in PUBLIC_PATHS, (
            "``/api/system/soak-test`` must NOT be public — the soak "
            "report includes internal state an unauthenticated client "
            "must not see. W26-4 contract + the W11-6 fail-closed auth."
        )

    def test_endpoint_rejects_missing_token(self):
        """``POST /api/system/soak-test`` without a bearer token must
        return ``401 Unauthorized`` (the W11-6 fail-closed auth contract)."""
        from fastapi.testclient import TestClient

        from api.server import app

        # ``TestClient(app)`` (NOT ``with TestClient(app)``) — skips the
        # production lifespan so the test stays fast.
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/api/system/soak-test")
        assert response.status_code == 401, (
            "POST /api/system/soak-test without a bearer token must "
            "return 401 — W26-4 contract + W11-6 fail-closed auth."
        )

    @ASYNC
    async def test_endpoint_returns_report_dict(self, monkeypatch):
        """``POST /api/system/soak-test?duration_seconds=1`` returns 200
        with a report dict in the documented shape.

        Patches the singleton's ``run`` method with a stub that returns
        a fixed report immediately (so the test stays sub-second — the
        real ``run`` blocks for at least the supplied ``duration_seconds``).
        """
        from fastapi.testclient import TestClient

        from api.server import app
        from core.soak_test import SoakTestCheck, SoakTestReport, soak_test_runner

        async def _stub_run(duration_override=None):
            return SoakTestReport(
                duration_seconds=0.001,
                overall_pass=True,
                checks=[
                    SoakTestCheck(
                        "api_responds", True, "1ms", "<5000ms", "stub"
                    ),
                ],
                metrics={"stub": True},
                started_at=1000.0,
                ended_at=1000.001,
                errors=[],
            )

        # Patch the singleton's ``run`` method (instance attribute shadowing
        # the class method — the route handler imports the singleton at
        # call time so the patch is picked up).
        monkeypatch.setattr(soak_test_runner, "run", _stub_run)

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/system/soak-test?duration_seconds=1",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["overall_pass"] is True
        assert body["duration_seconds"] == pytest.approx(0.001)
        assert len(body["checks"]) == 1
        assert body["checks"][0]["name"] == "api_responds"
        assert body["checks"][0]["passed"] is True
        assert body["metrics"] == {"stub": True}
        assert body["errors"] == []
        assert body["started_at"] == pytest.approx(1000.0)
        assert body["ended_at"] == pytest.approx(1000.001)

    def test_endpoint_rejects_duration_below_minimum(self):
        """``duration_seconds`` must be ≥ 1 (the Query validation
        ``ge=1`` enforces this). A 0-second soak is nonsensical — the
        loop would never iterate."""
        from fastapi.testclient import TestClient

        from api.server import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/system/soak-test?duration_seconds=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.status_code == 422, response.text

    def test_endpoint_rejects_duration_above_maximum(self):
        """``duration_seconds`` must be ≤ 86 400 (24 h). The Query
        validation ``le=86400`` enforces this — a longer soak would
        risk resource accumulation (file handles / DB connections /
        observability metrics table growth) beyond the system's
        designed-for ceiling."""
        from fastapi.testclient import TestClient

        from api.server import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/system/soak-test?duration_seconds=100000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.status_code == 422, response.text

    def test_endpoint_uses_default_duration_when_param_omitted(self, monkeypatch):
        """When ``duration_seconds`` is omitted, the endpoint uses the
        default (60 s). Patches ``run`` with a stub that records the
        supplied duration so the test can verify the default propagated."""
        from fastapi.testclient import TestClient

        from api.server import app
        from core.soak_test import SoakTestReport, soak_test_runner

        captured: dict = {}

        async def _capture_run(duration_override=None):
            captured["duration_override"] = duration_override
            return SoakTestReport(
                duration_seconds=0.0,
                overall_pass=True,
                checks=[],
                metrics={},
                started_at=0.0,
                ended_at=0.0,
                errors=[],
            )

        monkeypatch.setattr(soak_test_runner, "run", _capture_run)

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/system/soak-test",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        # The default 60 s propagated through to the runner.
        assert captured["duration_override"] == 60
