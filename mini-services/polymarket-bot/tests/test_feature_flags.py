"""
Unit + API tests for the W12-1 feature flags system.

Two test classes:

  (1) ``TestFeatureFlagManager`` — unit tests against a fresh
      ``FeatureFlagManager`` instance constructed on a ``tmp_path``
      SQLite file (full isolation from the module-level singleton).
      Covers: default seeding, ``is_enabled``, ``get_flag``,
      ``get_all``, ``set`` (incl. config update + unknown-key
      rejection), ``reset``.

  (2) ``TestFeatureFlagRoutes`` — integration tests via
      ``fastapi.testclient.TestClient`` against a fresh ``FastAPI``
      app with only the feature-flag routes registered. Covers:
      ``GET /api/flags`` (list), ``GET /api/flags/{key}`` (single +
      404 on unknown), ``POST /api/flags/{key}`` (update + config
      override + 404 on unknown), ``POST /api/flags/{key}/reset``
      (reset + 404 on non-default key).

Approach
~~~~~~~~
The module-level ``flag_manager`` singleton is constructed at import
time against the conftest-redirected ``/tmp/pmbot_conftest_isolation/
feature_flags.db`` (see ``tests/conftest.py::_ENV_REDIRECTS``). To
isolate the API-route tests from one another AND from any state the
unit tests may have left on that singleton, the API-test fixture
replaces ``core.feature_flags.flag_manager`` with a fresh
``FeatureFlagManager`` instance constructed on a ``tmp_path`` SQLite
file, then runs ``register_routes`` against a fresh ``FastAPI()`` app.
The route handlers resolve ``flag_manager`` from the module namespace at
*call time* (the closure captures the module global, not a snapshot), so
the swap is picked up by every handler.

Mirrors the ``shadow_db`` fixture in ``tests/test_shadow_trading_api.py``
(U3) — same monkeypatch-the-module-global pattern, same hermetic-per-test
SQLite file, same fresh-app TestClient approach.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import feature_flags
from core.feature_flags import (
    DEFAULT_FLAGS,
    FeatureFlag,
    FeatureFlagManager,
    register_routes,
)


# ── (1) Unit tests: FeatureFlagManager ──────────────────────────────────────


@pytest.fixture
def manager(tmp_path: Path) -> FeatureFlagManager:
    """Fresh ``FeatureFlagManager`` against a ``tmp_path`` SQLite file.

    Independent of the module-level singleton — no shared state with
    the API-route tests or any sibling test.
    """
    db_path = tmp_path / "test_feature_flags_unit.db"
    return FeatureFlagManager(db_path=db_path)


class TestFeatureFlagManager:
    """Direct method-level coverage of :class:`FeatureFlagManager`."""

    def test_default_flags_are_seeded_on_init(self, manager: FeatureFlagManager):
        """Every key in ``DEFAULT_FLAGS`` must be present in the cache
        immediately after construction, with the default ``enabled``
        state and config."""
        for key, default in DEFAULT_FLAGS.items():
            flag = manager.get_flag(key)
            assert flag is not None, f"default flag {key!r} not seeded"
            assert flag.enabled is default["enabled"]
            assert flag.config == default.get("config", {})
            assert flag.description == default["description"]

    def test_is_enabled_returns_default_value(self, manager: FeatureFlagManager):
        """``is_enabled`` returns the default ``enabled`` for every
        seeded flag without any mutation."""
        assert manager.is_enabled("shadow_trading") is True
        assert manager.is_enabled("live_trading") is False
        assert manager.is_enabled("market_maker") is True
        assert manager.is_enabled("signal_trader") is False

    def test_is_enabled_unknown_key_returns_false(self, manager: FeatureFlagManager):
        """An unknown key fails closed (returns ``False``)."""
        assert manager.is_enabled("definitely_not_a_real_flag") is False

    def test_get_flag_unknown_key_returns_none(self, manager: FeatureFlagManager):
        """``get_flag`` returns ``None`` for an unknown key (rather
        than raising)."""
        assert manager.get_flag("nonexistent_flag") is None

    def test_get_all_returns_every_default_flag(self, manager: FeatureFlagManager):
        """``get_all`` returns a list with one entry per default flag."""
        all_flags = manager.get_all()
        assert len(all_flags) == len(DEFAULT_FLAGS)
        keys = {f["key"] for f in all_flags}
        assert keys == set(DEFAULT_FLAGS.keys())
        # Each entry must be JSON-serialisable (dict / bool / str /
        # float / dict-of-primitives — no dataclass leaks).
        for entry in all_flags:
            assert isinstance(entry["key"], str)
            assert isinstance(entry["enabled"], bool)
            assert isinstance(entry["description"], str)
            assert isinstance(entry["config"], dict)
            assert isinstance(entry["updated_at"], (int, float))

    def test_set_toggles_enabled_state(self, manager: FeatureFlagManager):
        """``set`` flips the ``enabled`` flag and the new state is
        reflected on the next ``is_enabled`` call."""
        assert manager.is_enabled("live_trading") is False
        ok = manager.set("live_trading", True)
        assert ok is True
        assert manager.is_enabled("live_trading") is True
        # Persisted: a fresh manager against the same DB file sees
        # the new value.
        twin = FeatureFlagManager(db_path=manager._db_path)
        assert twin.is_enabled("live_trading") is True

    def test_set_updates_config_when_provided(self, manager: FeatureFlagManager):
        """Passing ``config=...`` overwrites the stored config."""
        ok = manager.set("market_maker", True, config={"spread_bps": 350, "tiers": 3})
        assert ok is True
        flag = manager.get_flag("market_maker")
        assert flag is not None
        assert flag.config == {"spread_bps": 350, "tiers": 3}

    def test_set_preserves_existing_config_when_omitted(self, manager: FeatureFlagManager):
        """When ``config`` is omitted, the existing config is preserved
        (not wiped to ``{}``)."""
        manager.set("market_maker", True, config={"spread_bps": 999})
        # Now toggle without passing config:
        manager.set("market_maker", False)
        flag = manager.get_flag("market_maker")
        assert flag is not None
        assert flag.config == {"spread_bps": 999}
        assert flag.enabled is False

    def test_set_unknown_key_returns_false(self, manager: FeatureFlagManager):
        """``set`` refuses to create a flag that is neither a default
        NOR an existing row — returns ``False``."""
        ok = manager.set("totally_new_flag", True)
        assert ok is False
        assert manager.get_flag("totally_new_flag") is None

    def test_reset_restores_default_enabled_and_config(self, manager: FeatureFlagManager):
        """``reset`` reverts both ``enabled`` and ``config`` to the
        defaults in ``DEFAULT_FLAGS``."""
        # Mutate first.
        manager.set("market_maker", False, config={"spread_bps": 999})
        flag = manager.get_flag("market_maker")
        assert flag is not None
        assert flag.enabled is False
        # Reset.
        ok = manager.reset("market_maker")
        assert ok is True
        default = DEFAULT_FLAGS["market_maker"]
        flag = manager.get_flag("market_maker")
        assert flag is not None
        assert flag.enabled is default["enabled"]
        assert flag.config == default.get("config", {})

    def test_reset_unknown_key_returns_false(self, manager: FeatureFlagManager):
        """``reset`` returns ``False`` for a key that is not in
        ``DEFAULT_FLAGS`` (cannot reset a flag that has no default)."""
        # An unknown key:
        assert manager.reset("nonexistent_flag") is False
        # A flag that exists in the DB but not in DEFAULT_FLAGS isn't
        # possible via the public API (``set`` refuses unknown keys),
        # so this branch is just the "unknown key" case in practice.


# ── (2) API tests: register_routes ───────────────────────────────────────────


@pytest.fixture
def isolated_flags(monkeypatch, tmp_path: Path):
    """Replace ``core.feature_flags.flag_manager`` with a fresh
    ``FeatureFlagManager`` constructed on a ``tmp_path`` SQLite file.

    The route handlers in ``register_routes`` reference the module
    global ``flag_manager`` at call time (closure over the module
    namespace), so the swap is picked up by every handler without
    re-registration.

    Mirrors the ``shadow_db`` fixture in
    ``tests/test_shadow_trading_api.py`` — same monkeypatch-the-module-
    global pattern, same hermetic-per-test SQLite file.
    """
    db_path = tmp_path / "test_feature_flags_api.db"
    fresh = FeatureFlagManager(db_path=db_path)
    monkeypatch.setattr(feature_flags, "flag_manager", fresh)
    return fresh


@pytest.fixture
def client(isolated_flags: FeatureFlagManager) -> TestClient:
    """Fresh ``FastAPI`` app with only the feature-flag routes registered.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` (W12-1 block) so the route definitions / Pydantic
    validation annotations exercised here are byte-identical to what
    the live server exposes — without the bearer-token auth middleware
    or the heavy ``lifespan`` startup.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestFeatureFlagRoutes:
    """HTTP-level coverage of the four ``/api/flags`` endpoints."""

    def test_get_flags_returns_200_with_all_defaults(self, client: TestClient):
        """``GET /api/flags`` returns 200 with one entry per default
        flag, and the ``count`` matches the list length."""
        response = client.get("/api/flags")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(DEFAULT_FLAGS)
        assert len(body["flags"]) == len(DEFAULT_FLAGS)
        keys = {f["key"] for f in body["flags"]}
        assert keys == set(DEFAULT_FLAGS.keys())

    def test_get_single_flag_returns_200_for_known_key(self, client: TestClient):
        """``GET /api/flags/{key}`` returns 200 with the full flag
        record for a known key."""
        response = client.get("/api/flags/shadow_trading")
        assert response.status_code == 200
        body = response.json()
        assert body["key"] == "shadow_trading"
        assert body["enabled"] is DEFAULT_FLAGS["shadow_trading"]["enabled"]
        assert body["description"] == DEFAULT_FLAGS["shadow_trading"]["description"]
        assert body["config"] == DEFAULT_FLAGS["shadow_trading"].get("config", {})

    def test_get_single_flag_returns_404_for_unknown_key(self, client: TestClient):
        """``GET /api/flags/{key}`` returns 404 for an unknown key."""
        response = client.get("/api/flags/not_a_real_flag")
        assert response.status_code == 404
        body = response.json()
        assert "detail" in body

    def test_post_flag_toggles_enabled(self, client: TestClient, isolated_flags: FeatureFlagManager):
        """``POST /api/flags/{key}`` with ``{enabled: true}`` flips the
        flag's state and the new state is reflected on the next
        ``GET``."""
        # Sanity: live_trading defaults to False.
        assert isolated_flags.is_enabled("live_trading") is False
        response = client.post("/api/flags/live_trading", json={"enabled": True})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["flag"]["enabled"] is True
        # Verify via GET.
        get_resp = client.get("/api/flags/live_trading")
        assert get_resp.json()["enabled"] is True

    def test_post_flag_overrides_config(self, client: TestClient, isolated_flags: FeatureFlagManager):
        """``POST /api/flags/{key}`` with ``{enabled, config}`` updates
        both fields."""
        response = client.post(
            "/api/flags/market_maker",
            json={"enabled": False, "config": {"spread_bps": 400}},
        )
        assert response.status_code == 200
        flag = isolated_flags.get_flag("market_maker")
        assert flag is not None
        assert flag.enabled is False
        assert flag.config == {"spread_bps": 400}

    def test_post_flag_returns_404_for_unknown_key(self, client: TestClient):
        """``POST /api/flags/{key}`` returns 404 for an unknown key."""
        response = client.post("/api/flags/fake_flag", json={"enabled": True})
        assert response.status_code == 404

    def test_post_flag_validates_enabled_field(self, client: TestClient):
        """``POST /api/flags/{key}`` with a missing ``enabled`` field
        returns 422 (Pydantic validation)."""
        response = client.post("/api/flags/live_trading", json={})
        assert response.status_code == 422
        body = response.json()
        assert "detail" in body

    def test_reset_flag_restores_defaults(self, client: TestClient, isolated_flags: FeatureFlagManager):
        """``POST /api/flags/{key}/reset`` reverts the flag to its
        default ``enabled`` + ``config``."""
        # Mutate first.
        client.post(
            "/api/flags/market_maker",
            json={"enabled": False, "config": {"spread_bps": 999}},
        )
        flag = isolated_flags.get_flag("market_maker")
        assert flag is not None and flag.enabled is False
        # Reset.
        response = client.post("/api/flags/market_maker/reset")
        assert response.status_code == 200
        flag = isolated_flags.get_flag("market_maker")
        assert flag is not None
        default = DEFAULT_FLAGS["market_maker"]
        assert flag.enabled is default["enabled"]
        assert flag.config == default.get("config", {})

    def test_reset_flag_returns_404_for_unknown_key(self, client: TestClient):
        """``POST /api/flags/{key}/reset`` returns 404 for a key that
        is not in ``DEFAULT_FLAGS``."""
        response = client.post("/api/flags/fake_flag/reset")
        assert response.status_code == 404

    def test_persistence_across_manager_instances(self, tmp_path: Path):
        """A flag set via one ``FeatureFlagManager`` instance must be
        visible to a fresh instance constructed against the same DB
        file (i.e. the SQLite write committed, not just updated the
        in-memory cache)."""
        db_path = tmp_path / "test_persistence.db"
        a = FeatureFlagManager(db_path=db_path)
        a.set("live_trading", True)
        b = FeatureFlagManager(db_path=db_path)
        assert b.is_enabled("live_trading") is True
