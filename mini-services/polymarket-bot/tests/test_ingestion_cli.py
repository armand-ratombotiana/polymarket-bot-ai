"""tests/test_ingestion_cli.py — W33-5 unit tests for
``scripts/run_ingestion.py``.

Scope: NEW file only — no existing source files or test files edited.
Mirrors the isolation strategy already established by
``tests/test_backfill.py`` (W31-3) and ``tests/test_ingestion_infra.py``
(W31-4).

Eight tests covering the four W33-5 contract surfaces:

  1. **Backfill command** — the ``backfill`` subcommand translates the
     CLI's user-facing ``--type`` value (``markets`` → ``metadata``;
     ``all`` → ``all``) and the ``--token`` / ``--days`` / ``--no-resume``
     flags into the ``BackfillEngine.run`` keyword contract, then
     prints a per-type summary line.
  2. **Status command** — ``status`` reads the live
     ``ingestion_pipeline`` properties + ``ingestion_health_monitor
     .get_summary()`` and prints the four pipeline counters + every
     health-summary field.
  3. **Replay command** — ``replay --source clob_rest --from <ts>``
     calls ``raw_vault.replay_range(start_ts=, source=, limit=)`` and
     prints the count + per-record summary.
  4. **DLQ management** — ``dlq --list`` prints pending records;
     ``dlq --retry`` iterates pending records and marks each retried.

Mock strategy
~~~~~~~~~~~~~

  * The CLI's ``run_backfill`` / ``run_replay`` / ``manage_dlq``
    handlers import their dependencies lazily inside the function body
    (so the env-var redirect at the top of ``run_ingestion.py`` runs
    first). Tests patch the *module attribute* on
    ``ingestion.backfill.BackfillEngine`` /
    ``ingestion.raw_vault.raw_vault`` /
    ``ingestion.dead_letter.dead_letter_queue`` via ``monkeypatch.
    setattr`` so the lazy import resolves to the mock.

  * ``BackfillEngine`` is replaced with a ``MagicMock`` whose ``run``
    is an ``AsyncMock`` returning a deterministic ``BackfillStats``
    dict — mirrors the W31-3 ``test_backfill.py`` pattern.

  * ``raw_vault.replay_range`` is replaced with a plain ``MagicMock``
    returning a list of dicts (the real ``replay_range`` returns an
    ``Iterable[dict[str, Any]]``).

  * ``dead_letter_queue.get_pending`` is replaced with a ``MagicMock``
    returning a list of lightweight ``SimpleNamespace`` objects that
    quack like ``DeadLetterRecord`` (the CLI only reads
    ``record_id`` / ``source`` / ``record_type`` / ``reason`` /
    ``error`` / ``retry_count`` / ``status`` — exactly the
    ``DeadLetterRecord`` dataclass fields).

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` is intentionally minimal — ``testpaths = tests`` — so
``asyncio_mode = "auto"`` is not enabled via config).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Mirrors the bootstrap pattern in ``tests/test_backfill.py`` (W31-3) and
# ``tests/test_ingestion_infra.py`` (W31-4). Belt-and-braces with the same
# redirect in ``tests/conftest.py`` (which pytest loads before this file) so
# the ``ingestion.*`` and ``core.timescale_db`` module-level singletons
# don't raise PermissionError on the read-only ``/app/data`` sandbox path.
_TMP_ROOT = Path("/tmp/pmbot_ingestion_cli_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "bot_data"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-ingestion-cli",
    "CORS_ORIGINS": "http://localhost",
    "PMBOT_CLI_TMP_ROOT": str(_TMP_ROOT),
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*`` / ``core.*``). Mirrors the bootstrap pattern in every
# existing ``tests/test_*.py`` sibling.
#
# IMPORTANT: We use ``remove`` + ``insert(0, ...)`` (NOT
# ``if not in sys.path``) because pytest's default ``prepend`` import
# mode inserts ``tests/`` at ``sys.path[0]`` AFTER conftest's own
# ``sys.path.insert(0, _PROJECT_ROOT)`` has already run. Without the
# ``remove`` step, our project root ends up at position 1 (behind
# ``tests/``), which lets the sibling ``tests/ingestion/`` package
# shadow our top-level ``ingestion`` package — see the W31-4 note in
# ``tests/test_ingestion_infra.py`` for the full rationale.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Clear any cached ``ingestion`` / ``ingestion.*`` module pointing at the
# ``tests/ingestion/`` directory so the next import resolves against the
# freshly-prepended ``_PROJECT_ROOT`` (same belt-and-braces guard as
# ``tests/test_ingestion_infra.py``).
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

# Apply ``@pytest.mark.asyncio`` ONLY to the async tests. The module-
# level ``pytestmark = pytest.mark.asyncio`` alternative emits
# ``PytestWarning`` for every sync ``test_*`` function in the file
# (see ``tests/test_ingestion_wiring.py``'s docstring for the same
# convention). Each async test gets an explicit decorator below.
import pytest  # noqa: E402  (env must be set first)

# Trigger ``ingestion/__init__.py`` so the ``raw_vault`` /
# ``dead_letter`` modules are loaded into ``sys.modules``. We then
# resolve the MODULE objects (NOT the package-level re-exports) from
# ``sys.modules`` because ``ingestion/__init__.py`` does
# ``from ingestion.raw_vault import raw_vault`` and ``from
# ingestion.dead_letter import dead_letter_queue`` which OVERRIDES the
# package's ``raw_vault`` / ``dead_letter`` attributes with the
# singleton instances (the from-imports run AFTER the import system
# has set the submodule attribute). ``import ingestion.raw_vault as
# _mod`` would therefore bind ``_mod`` to the RawVault instance, NOT
# the module — fetching the module from ``sys.modules`` is the only
# way to address the singleton at the module layer so
# ``monkeypatch.setattr(module, "raw_vault", fake)`` patches the
# attribute the CLI's ``from ingestion.raw_vault import raw_vault``
# actually reads.
import ingestion.raw_vault  # noqa: E402, F401  (triggers ingestion/__init__.py)
import ingestion.dead_letter  # noqa: E402, F401
_raw_vault_module = sys.modules["ingestion.raw_vault"]
_dead_letter_module = sys.modules["ingestion.dead_letter"]


# ── Helpers ─────────────────────────────────────────────────────────────────


_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "run_ingestion.py"


def _load_cli_module() -> types.ModuleType:
    """Import ``scripts/run_ingestion.py`` as a fresh module.

    Uses ``importlib.util.spec_from_file_location`` (NOT a regular
    import) so the test always sees the on-disk version of the
    script — even if a prior test in the same session imported a
    different (cached) version. Mirrors the pattern in
    ``tests/test_backfill.py::test_cli_parse_args_valid_type``.
    """
    spec = importlib.util.spec_from_file_location(
        "run_ingestion_test_subject",
        str(_SCRIPT_PATH),
    )
    assert spec is not None and spec.loader is not None, "spec load failed"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_stats_dict(type_: str = "metadata", added: int = 5) -> dict:
    """Build a minimal dict that quacks like ``BackfillStats.to_dict()``.

    The CLI's ``run_backfill`` handler reads ``type`` /
    ``total_added`` / ``total_skipped`` / ``total_errors`` /
    ``elapsed_s`` / ``error_message`` — exactly the keys
    ``BackfillStats.to_dict()`` returns.
    """
    return {
        "type": type_,
        "started_at": 1000.0,
        "ended_at": 1001.0,
        "total_processed": added,
        "total_added": added,
        "total_skipped": 0,
        "total_errors": 0,
        "last_offset": 0,
        "last_token_id": "",
        "error_message": "",
        "elapsed_s": 1.0,
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. Argument parsing — sync tests (no async needed)
# ────────────────────────────────────────────────────────────────────────────


def test_parse_args_backfill_markets():
    """``backfill --type markets`` round-trips the user-facing alias."""
    mod = _load_cli_module()
    args = mod._parse_args(["backfill", "--type", "markets"])
    assert args.command == "backfill"
    assert args.type == "markets"
    assert args.token is None
    assert args.days == 30
    assert args.no_resume is False


def test_parse_args_backfill_prices_with_token_and_days():
    """``backfill --type prices --token T1 --days 30`` round-trips."""
    mod = _load_cli_module()
    args = mod._parse_args([
        "backfill",
        "--type", "prices",
        "--token", "T1",
        "--days", "30",
        "--no-resume",
    ])
    assert args.type == "prices"
    assert args.token == "T1"
    assert args.days == 30
    assert args.no_resume is True


def test_parse_args_backfill_invalid_type_exits_2():
    """An invalid ``--type`` triggers argparse's exit code 2."""
    mod = _load_cli_module()
    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args(["backfill", "--type", "bogus"])
    assert exc_info.value.code == 2


def test_parse_args_backfill_missing_type_exits_2():
    """``backfill`` without ``--type`` exits with code 2 (argparse required)."""
    mod = _load_cli_module()
    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args(["backfill"])
    assert exc_info.value.code == 2


def test_parse_args_status():
    """``status`` subcommand parses with no extra flags."""
    mod = _load_cli_module()
    args = mod._parse_args(["status"])
    assert args.command == "status"


def test_parse_args_replay():
    """``replay --source clob_rest --from 1234`` round-trips."""
    mod = _load_cli_module()
    args = mod._parse_args([
        "replay",
        "--source", "clob_rest",
        "--from", "1234",
        "--limit", "500",
    ])
    assert args.command == "replay"
    assert args.source == "clob_rest"
    assert args.from_ts == 1234.0
    assert args.limit == 500


def test_parse_args_replay_missing_source_exits_2():
    """``replay`` without ``--source`` exits with code 2."""
    mod = _load_cli_module()
    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args(["replay"])
    assert exc_info.value.code == 2


def test_parse_args_dlq_list():
    """``dlq --list`` parses with ``args.list is True``."""
    mod = _load_cli_module()
    args = mod._parse_args(["dlq", "--list"])
    assert args.command == "dlq"
    assert args.list is True
    assert args.retry is False


def test_parse_args_dlq_retry():
    """``dlq --retry`` parses with ``args.retry is True``."""
    mod = _load_cli_module()
    args = mod._parse_args(["dlq", "--retry"])
    assert args.retry is True
    assert args.list is False


def test_parse_args_dlq_without_action_exits_2():
    """``dlq`` without ``--list`` or ``--retry`` exits with code 2."""
    mod = _load_cli_module()
    with pytest.raises(SystemExit) as exc_info:
        mod._parse_args(["dlq"])
    assert exc_info.value.code == 2


def test_parse_args_no_subcommand():
    """A bare ``run_ingestion.py`` invocation leaves ``args.command is None``."""
    mod = _load_cli_module()
    args = mod._parse_args([])
    assert args.command is None


# ────────────────────────────────────────────────────────────────────────────
# 2. Backfill subcommand — async tests with mocked BackfillEngine
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_backfill_markets_maps_to_metadata(monkeypatch: pytest.MonkeyPatch):
    """``--type markets`` is translated to ``metadata`` inside engine.run."""
    mod = _load_cli_module()

    # Mock ``ingestion.backfill.BackfillEngine`` so its ``.run()`` is
    # an AsyncMock returning a single-type results dict. We patch the
    # class object so the handler's ``from ingestion.backfill import
    # BackfillEngine`` resolves to the mock.
    fake_engine = MagicMock()
    fake_engine.run = AsyncMock(return_value={
        "metadata": SimpleNamespace(to_dict=lambda: _fake_stats_dict("metadata", 7)),
    })
    fake_engine_cls = MagicMock(return_value=fake_engine)
    monkeypatch.setattr(
        "ingestion.backfill.BackfillEngine", fake_engine_cls,
    )

    args = mod._parse_args(["backfill", "--type", "markets"])
    rc = await mod.run_backfill(args)

    assert rc == 0
    fake_engine_cls.assert_called_once()
    fake_engine.run.assert_awaited_once()
    # The user-facing alias ``markets`` is translated to ``metadata``
    # before reaching the engine.
    call_kwargs = fake_engine.run.await_args
    assert call_kwargs.args[0] == "metadata"
    assert call_kwargs.kwargs["market_token"] is None
    assert call_kwargs.kwargs["days"] == 30
    assert call_kwargs.kwargs["resume"] is True


@pytest.mark.asyncio
async def test_run_backfill_prices_passes_token_and_days(monkeypatch: pytest.MonkeyPatch):
    """``--type prices --token T1 --days 30`` round-trips into engine.run."""
    mod = _load_cli_module()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock(return_value={
        "prices": SimpleNamespace(to_dict=lambda: _fake_stats_dict("prices", 12)),
    })
    monkeypatch.setattr(
        "ingestion.backfill.BackfillEngine",
        MagicMock(return_value=fake_engine),
    )

    args = mod._parse_args([
        "backfill", "--type", "prices",
        "--token", "T1", "--days", "30",
    ])
    rc = await mod.run_backfill(args)

    assert rc == 0
    call_kwargs = fake_engine.run.await_args
    assert call_kwargs.args[0] == "prices"
    assert call_kwargs.kwargs["market_token"] == "T1"
    assert call_kwargs.kwargs["days"] == 30


@pytest.mark.asyncio
async def test_run_backfill_all_passes_all_type(monkeypatch: pytest.MonkeyPatch):
    """``--type all`` is forwarded to engine.run as ``'all'``."""
    mod = _load_cli_module()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock(return_value={
        "metadata": SimpleNamespace(to_dict=lambda: _fake_stats_dict("metadata", 3)),
        "prices": SimpleNamespace(to_dict=lambda: _fake_stats_dict("prices", 10)),
        "trades": SimpleNamespace(to_dict=lambda: _fake_stats_dict("trades", 5)),
        "outcomes": SimpleNamespace(to_dict=lambda: _fake_stats_dict("outcomes", 2)),
        "snapshots": SimpleNamespace(to_dict=lambda: _fake_stats_dict("snapshots", 1)),
    })
    monkeypatch.setattr(
        "ingestion.backfill.BackfillEngine",
        MagicMock(return_value=fake_engine),
    )

    args = mod._parse_args(["backfill", "--type", "all"])
    rc = await mod.run_backfill(args)

    assert rc == 0
    call_kwargs = fake_engine.run.await_args
    assert call_kwargs.args[0] == "all"


@pytest.mark.asyncio
async def test_run_backfill_no_resume_flips_resume_false(monkeypatch: pytest.MonkeyPatch):
    """``--no-resume`` translates to ``resume=False`` inside engine.run."""
    mod = _load_cli_module()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock(return_value={
        "trades": SimpleNamespace(to_dict=lambda: _fake_stats_dict("trades", 0)),
    })
    monkeypatch.setattr(
        "ingestion.backfill.BackfillEngine",
        MagicMock(return_value=fake_engine),
    )

    args = mod._parse_args([
        "backfill", "--type", "trades", "--days", "7", "--no-resume",
    ])
    rc = await mod.run_backfill(args)

    assert rc == 0
    call_kwargs = fake_engine.run.await_args
    assert call_kwargs.args[0] == "trades"
    assert call_kwargs.kwargs["days"] == 7
    assert call_kwargs.kwargs["resume"] is False


@pytest.mark.asyncio
async def test_run_backfill_engine_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """If the engine raises, ``run_backfill`` returns 1 + prints the error."""
    mod = _load_cli_module()

    fake_engine = MagicMock()
    fake_engine.run = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "ingestion.backfill.BackfillEngine",
        MagicMock(return_value=fake_engine),
    )

    args = mod._parse_args(["backfill", "--type", "markets"])
    rc = await mod.run_backfill(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "Backfill failed: boom" in captured.err


# ────────────────────────────────────────────────────────────────────────────
# 3. Status subcommand
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_status_prints_pipeline_and_health(
    capsys: pytest.CaptureFixture,
):
    """``status`` prints the four pipeline counters + every health field."""
    mod = _load_cli_module()
    rc = await mod.show_status()

    assert rc == 0
    captured = capsys.readouterr().out
    assert "Ingestion Pipeline Status:" in captured
    assert "Running:" in captured
    assert "Active sources:" in captured
    assert "Events ingested:" in captured
    assert "Failed records:" in captured
    assert "Health Summary:" in captured
    # Every key returned by ``ingestion_health_monitor.get_summary()`` —
    # mirrors the dict shape documented in ``ingestion/health.py``.
    for key in (
        "sources", "available_sources", "events_received", "events_failed",
        "error_rate", "throughput_eps", "avg_latency_ms", "dlq_depth",
        "last_event_at", "is_running", "alerts",
    ):
        assert key in captured, f"missing health key in output: {key}"


# ────────────────────────────────────────────────────────────────────────────
# 4. Replay subcommand — async tests with mocked raw_vault
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_replay_prints_record_count(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """``replay --source clob_rest`` prints the count + per-record summary."""
    mod = _load_cli_module()

    fake_records = [
        {"observation_id": f"obs-{i}", "event_timestamp": 1000.0 + i}
        for i in range(3)
    ]
    fake_vault = MagicMock()
    fake_vault.replay_range = MagicMock(return_value=fake_records)
    # Patch the module-level singleton via the module object directly
    # (NOT the dotted string) — see the import block note for why
    # ``monkeypatch.setattr("ingestion.raw_vault.raw_vault", ...)``
    # fails when the parent package re-exports the singleton name.
    monkeypatch.setattr(_raw_vault_module, "raw_vault", fake_vault)

    args = mod._parse_args(["replay", "--source", "clob_rest"])
    rc = await mod.run_replay(args)

    assert rc == 0
    fake_vault.replay_range.assert_called_once()
    call_kwargs = fake_vault.replay_range.call_args
    assert call_kwargs.kwargs["source"] == "clob_rest"
    assert call_kwargs.kwargs["start_ts"] is None
    assert call_kwargs.kwargs["limit"] == 1000

    captured = capsys.readouterr().out
    assert "Replaying 3 records from 'clob_rest'" in captured
    for i in range(3):
        assert f"obs-{i}" in captured


@pytest.mark.asyncio
async def test_run_replay_passes_from_ts_and_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    """``replay --source X --from 1234 --limit 100`` round-trips."""
    mod = _load_cli_module()

    fake_vault = MagicMock()
    fake_vault.replay_range = MagicMock(return_value=[])
    monkeypatch.setattr(_raw_vault_module, "raw_vault", fake_vault)

    args = mod._parse_args([
        "replay", "--source", "gamma_api", "--from", "1234", "--limit", "100",
    ])
    rc = await mod.run_replay(args)

    assert rc == 0
    call_kwargs = fake_vault.replay_range.call_args
    assert call_kwargs.kwargs["source"] == "gamma_api"
    assert call_kwargs.kwargs["start_ts"] == 1234.0
    assert call_kwargs.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_run_replay_empty_vault_prints_zero_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """An empty vault prints ``Replaying 0 records``."""
    mod = _load_cli_module()

    fake_vault = MagicMock()
    fake_vault.replay_range = MagicMock(return_value=[])
    monkeypatch.setattr(_raw_vault_module, "raw_vault", fake_vault)

    args = mod._parse_args(["replay", "--source", "clob_rest"])
    rc = await mod.run_replay(args)

    assert rc == 0
    captured = capsys.readouterr().out
    assert "Replaying 0 records from 'clob_rest'" in captured


# ────────────────────────────────────────────────────────────────────────────
# 5. DLQ management — async tests with mocked dead_letter_queue
# ────────────────────────────────────────────────────────────────────────────


def _fake_dlq_record(
    record_id: str = "rec-1",
    source: str = "clob_rest",
    record_type: str = "trade",
    reason: str = "validation_failed",
    error: str = "boom",
    retry_count: int = 0,
    status: str = "pending",
) -> SimpleNamespace:
    """A minimal object that quacks like a ``DeadLetterRecord``."""
    return SimpleNamespace(
        record_id=record_id,
        source=source,
        record_type=record_type,
        payload={},
        reason=reason,
        error=error,
        stack_trace="",
        first_seen=1000.0,
        last_attempt=0.0,
        retry_count=retry_count,
        status=status,
        metadata={},
    )


@pytest.mark.asyncio
async def test_dlq_list_prints_pending_records(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """``dlq --list`` prints every pending record's id + source + reason."""
    mod = _load_cli_module()

    fake_records = [
        _fake_dlq_record("rec-A", source="clob_rest", reason="validation_failed", error="boom"),
        _fake_dlq_record("rec-B", source="gamma_api", reason="storage_error", error="disk full"),
    ]
    fake_dlq = MagicMock()
    fake_dlq.get_pending = MagicMock(return_value=fake_records)
    monkeypatch.setattr(_dead_letter_module, "dead_letter_queue", fake_dlq)

    args = mod._parse_args(["dlq", "--list"])
    rc = await mod.manage_dlq(args)

    assert rc == 0
    fake_dlq.get_pending.assert_called_once_with(limit=50)
    captured = capsys.readouterr().out
    assert "Dead-letter queue (2 pending items)" in captured
    assert "rec-A" in captured
    assert "rec-B" in captured
    assert "clob_rest" in captured
    assert "gamma_api" in captured
    assert "validation_failed" in captured
    assert "storage_error" in captured


@pytest.mark.asyncio
async def test_dlq_list_empty_queue_prints_zero_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """An empty DLQ prints ``0 pending items``."""
    mod = _load_cli_module()

    fake_dlq = MagicMock()
    fake_dlq.get_pending = MagicMock(return_value=[])
    monkeypatch.setattr(_dead_letter_module, "dead_letter_queue", fake_dlq)

    args = mod._parse_args(["dlq", "--list"])
    rc = await mod.manage_dlq(args)

    assert rc == 0
    captured = capsys.readouterr().out
    assert "Dead-letter queue (0 pending items)" in captured


@pytest.mark.asyncio
async def test_dlq_retry_marks_every_pending_record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """``dlq --retry`` calls ``mark_retried(success=True)`` for each record."""
    mod = _load_cli_module()

    fake_records = [_fake_dlq_record(f"rec-{i}") for i in range(3)]
    fake_dlq = MagicMock()
    fake_dlq.get_pending = MagicMock(return_value=fake_records)
    fake_dlq.mark_retried = MagicMock(return_value=True)
    monkeypatch.setattr(_dead_letter_module, "dead_letter_queue", fake_dlq)

    args = mod._parse_args(["dlq", "--retry"])
    rc = await mod.manage_dlq(args)

    assert rc == 0
    # ``get_pending`` is called with the hard-ceiling 10_000 so every
    # pending record is drained.
    fake_dlq.get_pending.assert_called_once_with(limit=10_000)
    assert fake_dlq.mark_retried.call_count == 3
    # Each call passes ``success=True`` as a keyword (the API contract
    # for ``POST /api/ingestion/dead-letter/retry`` mirrors the same
    # flag). The CLI's call shape is
    # ``mark_retried(item.record_id, success=True)`` so the positional
    # arg is the record_id and the success flag is a kwarg.
    for call in fake_dlq.mark_retried.call_args_list:
        assert len(call.args) == 1, (
            f"expected exactly one positional arg (record_id); "
            f"got {call.args!r}"
        )
        assert call.kwargs.get("success") is True, (
            f"expected success=True kwarg; got kwargs={call.kwargs!r}"
        )
    captured = capsys.readouterr().out
    assert "Retried 3 / 3 records" in captured


@pytest.mark.asyncio
async def test_dlq_retry_empty_queue_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """``dlq --retry`` on an empty queue prints ``nothing to retry`` and exits 0."""
    mod = _load_cli_module()

    fake_dlq = MagicMock()
    fake_dlq.get_pending = MagicMock(return_value=[])
    fake_dlq.mark_retried = MagicMock()
    monkeypatch.setattr(_dead_letter_module, "dead_letter_queue", fake_dlq)

    args = mod._parse_args(["dlq", "--retry"])
    rc = await mod.manage_dlq(args)

    assert rc == 0
    fake_dlq.mark_retried.assert_not_called()
    captured = capsys.readouterr().out
    assert "nothing to retry" in captured


@pytest.mark.asyncio
async def test_dlq_retry_partial_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """If ``mark_retried`` returns False for some records, the exit code is 1."""
    mod = _load_cli_module()

    fake_records = [_fake_dlq_record(f"rec-{i}") for i in range(4)]
    fake_dlq = MagicMock()
    fake_dlq.get_pending = MagicMock(return_value=fake_records)
    # Record 1 fails to mark (e.g. race-condition delete by another process).
    fake_dlq.mark_retried = MagicMock(side_effect=[True, False, True, True])
    monkeypatch.setattr(_dead_letter_module, "dead_letter_queue", fake_dlq)

    args = mod._parse_args(["dlq", "--retry"])
    rc = await mod.manage_dlq(args)

    assert rc == 1
    captured = capsys.readouterr().out
    assert "Retried 3 / 4 records" in captured
    assert "1 failed to mark retried" in captured


# ────────────────────────────────────────────────────────────────────────────
# 6. End-to-end ``main()`` — sync tests
# ────────────────────────────────────────────────────────────────────────────


def test_main_no_subcommand_returns_0(
    capsys: pytest.CaptureFixture,
):
    """A bare ``main([])`` invocation returns 0 + prints a hint."""
    mod = _load_cli_module()
    rc = mod.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "--help" in captured.err


def test_main_status_end_to_end(capsys: pytest.CaptureFixture):
    """``main(["status"])`` dispatches to ``show_status`` and returns 0."""
    mod = _load_cli_module()
    rc = mod.main(["status"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Ingestion Pipeline Status:" in captured
