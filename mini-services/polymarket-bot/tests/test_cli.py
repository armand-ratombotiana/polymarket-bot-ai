"""Tests for the Polymarket Bot CLI (W14-3).

These tests use typer.testing.CliRunner to invoke commands without a real
backend, and monkeypatch ``httpx.get`` / ``httpx.post`` to verify HTTP call
shape without any network I/O.

Coverage:
  * CLI app existence + 14 commands registered + ``--help`` output.
  * ``_headers()`` helper: Bearer auth header when token set, Exit(1) when missing.
  * ``_get()`` helper: correct URL/headers/params/timeout, parsed JSON return,
    401/429 → Exit(1), 5xx → HTTPStatusError propagates, ConnectError → Exit(1).
  * ``_post()`` helper: correct URL/auth/Content-Type headers, body shape,
    ConnectError → Exit(1).
  * Each of the 14 commands rendered via CliRunner against a mocked httpx.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import typer
from typer.testing import CliRunner

# Make the project root importable so ``import cli`` resolves the local module
# (rather than picking up an unrelated package on sys.path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import cli as cli_module  # noqa: E402

app = cli_module.app
runner = CliRunner()


# ── Fake httpx.Response stand-in ────────────────────────────────────────────
class FakeResponse:
    """Minimal stand-in for ``httpx.Response``.

    Exposes the three members the CLI reads: ``status_code``, ``json()``,
    and ``raise_for_status()``. ``raise_for_status`` mirrors httpx's real
    behaviour — raises ``HTTPStatusError`` for any status code ≥ 400 unless
    the test supplies a specific exception via ``raise_exc``.
    """

    def __init__(self, status_code: int = 200, json_data=None,
                 raise_exc: Exception | None = None) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(status_code=self.status_code),
            )

    def json(self):
        return self._json


@pytest.fixture
def fake_get(monkeypatch):
    """Replace ``httpx.get`` with a MagicMock returning a 200 FakeResponse.

    Individual tests can reconfigure the return value or side_effect on the
    returned mock to drive the CLI's error paths.
    """
    mock = MagicMock(return_value=FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(httpx, "get", mock)
    return mock


@pytest.fixture
def fake_post(monkeypatch):
    """Replace ``httpx.post`` with a MagicMock returning a 200 FakeResponse."""
    mock = MagicMock(return_value=FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr(httpx, "post", mock)
    return mock


@pytest.fixture(autouse=True)
def _stable_env(monkeypatch):
    """Pin API_TOKEN + API_URL for every test so URL/header assertions are deterministic.

    The conftest's autouse env-redirect fixture sets ``API_TOKEN=test-token-conftest``
    via ``setdefault`` BEFORE this test module is imported, so the module-level
    ``API_TOKEN = os.environ.get(...)`` line captures that value at import time.
    We override it here to a clean deterministic value, and also pin API_URL so
    the URL-assertion tests don't depend on the ambient ``BOT_API_URL`` env var.
    """
    monkeypatch.setattr(cli_module, "API_TOKEN", "test-token")
    monkeypatch.setattr(cli_module, "API_URL", "http://test:8080")


# ── Expected command surface ────────────────────────────────────────────────
# Typer auto-converts ``kill_switch`` → ``kill-switch`` and
# ``circuit_breakers`` → ``circuit-breakers`` in the CLI.
EXPECTED_COMMANDS = {
    "status", "balance", "positions", "orders", "trades", "health",
    "retrain", "kill-switch", "flags", "flag", "alerts", "metrics",
    "circuit-breakers", "cache",
}


# ── CLI app existence + command surface ─────────────────────────────────────

def test_app_object_exists():
    assert app is not None
    assert callable(app)


def test_app_has_all_commands_registered():
    """Every expected command name should derive from a registered callback."""
    names = {c.callback.__name__.replace("_", "-")
             for c in app.registered_commands
             if c.callback is not None}
    missing = EXPECTED_COMMANDS - names
    assert not missing, f"missing CLI commands: {missing}"


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_COMMANDS:
        assert name in result.output, f"--help output missing command: {name}"


def test_help_shows_app_description():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Polymarket Bot CLI" in result.output


# ── _headers() helper ───────────────────────────────────────────────────────

def test_headers_returns_bearer_token(monkeypatch):
    monkeypatch.setattr(cli_module, "API_TOKEN", "abc-123")
    h = cli_module._headers()
    assert h == {"Authorization": "Bearer abc-123"}


def test_headers_exits_when_no_token(monkeypatch):
    monkeypatch.setattr(cli_module, "API_TOKEN", "")
    with pytest.raises(typer.Exit) as exc:
        cli_module._headers()
    assert exc.value.exit_code == 1


# ── _get() helper ────────────────────────────────────────────────────────────

def test_get_calls_httpx_with_auth_header_and_default_params(fake_get):
    cli_module._get("/api/status")
    fake_get.assert_called_once()
    args, kwargs = fake_get.call_args
    assert args[0] == "http://test:8080/api/status"
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert kwargs["params"] is None
    assert kwargs["timeout"] == 30


def test_get_returns_parsed_json(fake_get):
    fake_get.return_value = FakeResponse(json_data={"paper_balance": 100.0})
    assert cli_module._get("/api/status") == {"paper_balance": 100.0}


def test_get_forwards_params(fake_get):
    cli_module._get("/api/trades", {"limit": 50})
    _, kwargs = fake_get.call_args
    assert kwargs["params"] == {"limit": 50}


def test_get_exits_1_on_401(fake_get):
    fake_get.return_value = FakeResponse(status_code=401,
                                         json_data={"detail": "unauth"})
    with pytest.raises(typer.Exit) as exc:
        cli_module._get("/api/status")
    assert exc.value.exit_code == 1


def test_get_exits_1_on_429(fake_get):
    fake_get.return_value = FakeResponse(status_code=429)
    with pytest.raises(typer.Exit) as exc:
        cli_module._get("/api/status")
    assert exc.value.exit_code == 1


def test_get_propagates_http_status_error(fake_get):
    """Non-401/429 errors should propagate via ``raise_for_status``."""
    fake_get.return_value = FakeResponse(status_code=500)
    with pytest.raises(httpx.HTTPStatusError):
        cli_module._get("/api/status")


def test_get_exits_1_on_connect_error(monkeypatch):
    def _raise_connect(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "get", _raise_connect)
    with pytest.raises(typer.Exit) as exc:
        cli_module._get("/api/status")
    assert exc.value.exit_code == 1


# ── _post() helper ───────────────────────────────────────────────────────────

def test_post_calls_httpx_with_auth_and_content_type(fake_post):
    cli_module._post("/api/ml/retrain", {"force": True})
    fake_post.assert_called_once()
    args, kwargs = fake_post.call_args
    assert args[0] == "http://test:8080/api/ml/retrain"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["json"] == {"force": True}
    assert kwargs["timeout"] == 60


def test_post_defaults_to_empty_body(fake_post):
    cli_module._post("/api/ml/retrain")
    _, kwargs = fake_post.call_args
    assert kwargs["json"] == {}


def test_post_returns_parsed_json(fake_post):
    fake_post.return_value = FakeResponse(json_data={"job_id": "x"})
    assert cli_module._post("/api/ml/retrain") == {"job_id": "x"}


def test_post_exits_1_on_connect_error(monkeypatch):
    def _raise_connect(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "post", _raise_connect)
    with pytest.raises(typer.Exit) as exc:
        cli_module._post("/api/ml/retrain")
    assert exc.value.exit_code == 1


# ── status ───────────────────────────────────────────────────────────────────

def test_status_command_renders_table(fake_get):
    fake_get.return_value = FakeResponse(json_data={
        "paper_balance": 100.0,
        "mode": "paper",
    })
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Bot Status" in result.output
    assert "paper_balance" in result.output
    assert "100.0" in result.output


# ── balance ──────────────────────────────────────────────────────────────────

def test_balance_command_renders_panel(fake_get):
    fake_get.return_value = FakeResponse(json_data={"paper_balance": 1234.5})
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    assert "Account Balance" in result.output
    assert "1234.5" in result.output


def test_balance_command_falls_back_to_balance_key(fake_get):
    """When ``paper_balance`` is missing, fall back to ``balance``."""
    fake_get.return_value = FakeResponse(json_data={"balance": 999.0})
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    assert "999.0" in result.output


# ── positions ────────────────────────────────────────────────────────────────

def test_positions_command_empty_state(fake_get):
    fake_get.return_value = FakeResponse(json_data={"positions": []})
    result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0
    assert "No open positions" in result.output


def test_positions_command_with_data(fake_get):
    fake_get.return_value = FakeResponse(json_data={"positions": [
        {
            "token_id": "tok-abc-1234567890",
            "side": "BUY",
            "size": 10.0,
            "avg_price": 0.5000,
            "current_price": 0.6000,
            "unrealized_pnl": 1.0,
        },
        {
            "token_id": "tok-short-abcdef",
            "side": "SELL",
            "size": 5.0,
            "avg_price": 0.4000,
            "current_price": 0.4500,
            "pnl": -0.25,
        },
    ]})
    result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0
    assert "Open Positions" in result.output
    assert "tok-abc-12345678" in result.output  # truncated to 16 chars


# ── orders ──────────────────────────────────────────────────────────────────

def test_orders_command_empty_state(fake_get):
    fake_get.return_value = FakeResponse(json_data={"orders": []})
    result = runner.invoke(app, ["orders"])
    assert result.exit_code == 0
    assert "No open orders" in result.output


def test_orders_command_with_data(fake_get):
    fake_get.return_value = FakeResponse(json_data={"orders": [
        {
            "order_id": "ord-1234567890abcdef",
            "token_id": "tok-abc",
            "side": "BUY",
            "price": 0.5000,
            "size": 10,
            "status": "open",
        },
    ]})
    result = runner.invoke(app, ["orders"])
    assert result.exit_code == 0
    assert "Open Orders" in result.output
    assert "tok-abc" in result.output


# ── trades ──────────────────────────────────────────────────────────────────

def test_trades_command_empty_state(fake_get):
    fake_get.return_value = FakeResponse(json_data={"trades": []})
    result = runner.invoke(app, ["trades"])
    assert result.exit_code == 0
    assert "No trades found" in result.output


def test_trades_command_with_data(fake_get):
    fake_get.return_value = FakeResponse(json_data={"trades": [
        {
            "token_id": "tok-abc",
            "side": "BUY",
            "price": 0.5000,
            "size": 10,
            "timestamp": "2024-01-01T12:00:00Z",
        },
    ]})
    result = runner.invoke(app, ["trades"])
    assert result.exit_code == 0
    assert "Last 1 Trades" in result.output


def test_trades_command_forwards_limit_param(fake_get):
    fake_get.return_value = FakeResponse(json_data={"trades": []})
    result = runner.invoke(app, ["trades", "-n", "5"])
    assert result.exit_code == 0
    _, kwargs = fake_get.call_args
    assert kwargs["params"] == {"limit": 5}


def test_trades_command_default_limit_is_20(fake_get):
    fake_get.return_value = FakeResponse(json_data={"trades": []})
    result = runner.invoke(app, ["trades"])
    assert result.exit_code == 0
    _, kwargs = fake_get.call_args
    assert kwargs["params"] == {"limit": 20}


# ── health ───────────────────────────────────────────────────────────────────

def test_health_command_renders_panel(fake_get):
    fake_get.return_value = FakeResponse(json_data={"status": "ok",
                                                    "version": "1.0"})
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "Health Check" in result.output
    args, _ = fake_get.call_args
    assert args[0] == "http://test:8080/api/health"


# ── retrain ──────────────────────────────────────────────────────────────────

def test_retrain_command_posts_to_ml_retrain(fake_post):
    fake_post.return_value = FakeResponse(json_data={
        "job_id": "job-1",
        "status": "queued",
    })
    result = runner.invoke(app, ["retrain"])
    assert result.exit_code == 0
    assert "Retrain Result" in result.output
    assert "Triggering ML retrain" in result.output
    args, _ = fake_post.call_args
    assert args[0] == "http://test:8080/api/ml/retrain"


# ── kill-switch ──────────────────────────────────────────────────────────────

def test_kill_switch_command_confirmed(fake_post):
    fake_post.return_value = FakeResponse(json_data={"status": "activated"})
    result = runner.invoke(app, ["kill-switch"], input="y\n")
    assert result.exit_code == 0
    assert "Kill Switch Activated" in result.output
    args, _ = fake_post.call_args
    assert args[0] == "http://test:8080/api/kill-switch/activate"


def test_kill_switch_command_cancelled(fake_post):
    result = runner.invoke(app, ["kill-switch"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    fake_post.assert_not_called()


# ── flags ────────────────────────────────────────────────────────────────────

def test_flags_command_renders_table(fake_get):
    fake_get.return_value = FakeResponse(json_data=[
        {"key": "alpha_feature", "enabled": True,
         "description": "new ML stack"},
        {"key": "beta_feature", "enabled": False,
         "description": "experimental"},
    ])
    result = runner.invoke(app, ["flags"])
    assert result.exit_code == 0
    assert "Feature Flags" in result.output
    assert "alpha_feature" in result.output
    assert "beta_feature" in result.output
    assert "ON" in result.output
    assert "OFF" in result.output


# ── flag (get / set) ────────────────────────────────────────────────────────

def test_flag_command_get_when_no_value(fake_get):
    fake_get.return_value = FakeResponse(json_data={
        "key": "alpha_feature", "enabled": True,
    })
    result = runner.invoke(app, ["flag", "alpha_feature"])
    assert result.exit_code == 0
    assert "Flag: alpha_feature" in result.output
    args, _ = fake_get.call_args
    assert args[0] == "http://test:8080/api/flags/alpha_feature"


def test_flag_command_set_true(fake_post):
    fake_post.return_value = FakeResponse(json_data={
        "key": "alpha_feature", "enabled": True,
    })
    result = runner.invoke(app, ["flag", "alpha_feature", "--enabled"])
    assert result.exit_code == 0
    args, kwargs = fake_post.call_args
    assert args[0] == "http://test:8080/api/flags/alpha_feature"
    assert kwargs["json"] == {"enabled": True}


def test_flag_command_set_false(fake_post):
    fake_post.return_value = FakeResponse(json_data={
        "key": "alpha_feature", "enabled": False,
    })
    result = runner.invoke(app, ["flag", "alpha_feature", "--no-enabled"])
    assert result.exit_code == 0
    _, kwargs = fake_post.call_args
    assert kwargs["json"] == {"enabled": False}


# ── alerts ───────────────────────────────────────────────────────────────────

def test_alerts_command_empty(fake_get):
    fake_get.return_value = FakeResponse(json_data={"alerts": []})
    result = runner.invoke(app, ["alerts"])
    assert result.exit_code == 0
    assert "No alerts" in result.output


def test_alerts_command_with_data(fake_get):
    fake_get.return_value = FakeResponse(json_data={"alerts": [
        {"severity": "critical", "name": "daily_loss",
         "message": "exceeded threshold", "acknowledged": False},
        {"severity": "info", "name": "ping", "message": "ok",
         "acknowledged": True},
    ]})
    result = runner.invoke(app, ["alerts"])
    assert result.exit_code == 0
    assert "Recent Alerts" in result.output
    assert "daily_loss" in result.output
    assert "ping" in result.output


def test_alerts_command_forwards_limit_param(fake_get):
    fake_get.return_value = FakeResponse(json_data={"alerts": []})
    result = runner.invoke(app, ["alerts"])
    assert result.exit_code == 0
    _, kwargs = fake_get.call_args
    assert kwargs["params"] == {"limit": 20}


# ── metrics ─────────────────────────────────────────────────────────────────

def test_metrics_command_renders_panel(fake_get):
    fake_get.return_value = FakeResponse(json_data={"auc": 0.85,
                                                    "logloss": 0.32})
    result = runner.invoke(app, ["metrics"])
    assert result.exit_code == 0
    assert "ML Metrics" in result.output
    args, _ = fake_get.call_args
    assert args[0] == "http://test:8080/api/ml/metrics"


# ── circuit-breakers ────────────────────────────────────────────────────────

def test_circuit_breakers_command_renders_table(fake_get):
    fake_get.return_value = FakeResponse(json_data={"breakers": [
        {"name": "api", "state": "closed",
         "failure_count": 0, "failure_threshold": 5},
        {"name": "risk_engine", "state": "open",
         "failure_count": 5, "failure_threshold": 5},
        {"name": "data_feed", "state": "half_open",
         "failure_count": 3, "failure_threshold": 5},
    ]})
    result = runner.invoke(app, ["circuit-breakers"])
    assert result.exit_code == 0
    assert "Circuit Breakers" in result.output
    assert "api" in result.output
    assert "risk_engine" in result.output
    assert "data_feed" in result.output


# ── cache ───────────────────────────────────────────────────────────────────

def test_cache_command_renders_table(fake_get):
    fake_get.return_value = FakeResponse(json_data={"caches": [
        {"name": "markets_cache", "size": 50, "hits": 100, "misses": 20,
         "hit_rate": 0.8333},
        {"name": "slow_cache", "size": 0, "hits": 0, "misses": 10,
         "hit_rate": 0.0},
    ]})
    result = runner.invoke(app, ["cache"])
    assert result.exit_code == 0
    assert "Cache Statistics" in result.output
    assert "markets_cache" in result.output
    assert "slow_cache" in result.output
    args, _ = fake_get.call_args
    assert args[0] == "http://test:8080/api/cache/stats"
