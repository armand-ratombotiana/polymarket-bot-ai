"""Feature flags system — runtime feature toggles backed by SQLite.

Allows enabling/disabling features without redeploying.
Flags are evaluated per-request with optional user/strategy context.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FLAGS_DB_PATH = Path(os.environ.get("FLAGS_DB_PATH", "/app/data/feature_flags.db"))


@dataclass
class FeatureFlag:
    key: str
    enabled: bool
    description: str
    config: dict
    updated_at: float


DEFAULT_FLAGS = {
    "live_trading": {"enabled": False, "description": "Enable live trading (requires safety gate)", "config": {}},
    "shadow_trading": {"enabled": True, "description": "Record shadow/counterfactual trades", "config": {}},
    "ml_auto_retrain": {"enabled": True, "description": "Auto-retrain ML model on drift", "config": {"min_drift_psi": 0.25}},
    "market_maker": {"enabled": True, "description": "Market making strategy", "config": {"spread_bps": 200}},
    "signal_trader": {"enabled": False, "description": "ML signal-driven trading", "config": {"min_confidence": 0.50}},
    "arb_scanner": {"enabled": True, "description": "Arbitrage scanning", "config": {"min_profit_bps": 50}},
    "alerting": {"enabled": True, "description": "Threshold-based alerting", "config": {}},
    "observability_collector": {"enabled": True, "description": "Auto-collect system metrics", "config": {"interval_s": 30}},
    "label_backfill": {"enabled": True, "description": "Backfill labels from resolved markets", "config": {}},
    "capital_allocator": {"enabled": True, "description": "Saturating edge curve position sizing", "config": {}},
    "calibration": {"enabled": True, "description": "ML probability calibration", "config": {"method": "isotonic"}},
    "websocket_push": {"enabled": True, "description": "WebSocket real-time push", "config": {}},
    "pwa_offline": {"enabled": True, "description": "PWA offline support", "config": {}},
}


class FeatureFlagManager:
    """SQLite-backed feature flag store with an in-memory cache.

    The module-level singleton ``flag_manager`` (instantiated at import
    time) reads its DB path from the ``FLAGS_DB_PATH`` env var (default
    ``/app/data/feature_flags.db``). Production code uses the singleton;
    tests can construct ``FeatureFlagManager(db_path=...)`` against a
    ``tmp_path`` SQLite file for full isolation.
    """

    def __init__(self, db_path: Path = FLAGS_DB_PATH):
        self._db_path = db_path
        self._cache: dict[str, FeatureFlag] = {}  # In-memory cache
        self._init_db()
        self._load_all()

    def _init_db(self) -> None:
        """Create the ``feature_flags`` table (if absent) and seed defaults.

        Failures here are logged but swallowed so module import does NOT
        crash on a read-only filesystem (mirrors the defensive-init pattern
        used by ``core.decision_ledger.DecisionLedger._init_db``). The
        cache stays empty in that case; every ``is_enabled`` call returns
        ``False`` (fail-safe) until a writable DB path is configured.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover — sandbox-only failure mode
            logger.warning("[feature_flags] cannot create db dir %s: %s", self._db_path.parent, e)
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS feature_flags (
                        key TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL,
                        description TEXT,
                        config TEXT,
                        updated_at REAL
                    )
                    """
                )
                # Seed defaults (INSERT OR IGNORE keeps any existing row).
                for key, val in DEFAULT_FLAGS.items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO feature_flags (key, enabled, description, config, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (key, int(val["enabled"]), val["description"], json.dumps(val["config"]), 0.0),
                    )
        except sqlite3.Error as e:  # pragma: no cover — sandbox-only failure mode
            logger.warning("[feature_flags] db init failed at %s: %s", self._db_path, e)

    def _load_all(self) -> None:
        """Hydrate the in-memory cache from the on-disk table.

        On any error the cache is left in whatever state it had before
        this call (the prior successful hydration's values remain live)
        so a transient DB hiccup doesn't wipe the cache.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM feature_flags").fetchall()
        except sqlite3.Error as e:
            logger.warning("[feature_flags] load_all failed: %s", e)
            return
        self._cache = {
            r["key"]: FeatureFlag(
                key=r["key"],
                enabled=bool(r["enabled"]),
                description=r["description"] or "",
                config=json.loads(r["config"] or "{}"),
                updated_at=float(r["updated_at"] or 0.0),
            )
            for r in rows
        }

    def is_enabled(self, key: str) -> bool:
        """Return ``True`` iff flag ``key`` is enabled.

        Fails closed (returns ``False``) for an unknown key — the
        operator must explicitly opt-in by adding the key to
        ``DEFAULT_FLAGS``. A warning is logged so a typo in production
        code (e.g. ``flag_manager.is_enabled("marke_maker")``) is
        surfaced in the dashboard / log stream rather than silently
        disabling the feature.
        """
        flag = self._cache.get(key)
        if flag is None:
            logger.warning("Unknown feature flag: %s", key)
            return False
        return flag.enabled

    def get_flag(self, key: str) -> Optional[FeatureFlag]:
        """Return the cached :class:`FeatureFlag` for ``key`` (or ``None``)."""
        return self._cache.get(key)

    def get_all(self) -> list[dict]:
        """Return every known flag as a JSON-serialisable list of dicts."""
        return [
            {
                "key": f.key,
                "enabled": f.enabled,
                "description": f.description,
                "config": f.config,
                "updated_at": f.updated_at,
            }
            for f in self._cache.values()
        ]

    def set(self, key: str, enabled: bool, config: Optional[dict] = None) -> bool:
        """Persist a flag update and refresh the in-memory cache.

        Returns ``True`` on success, ``False`` if ``key`` is neither a
        known default NOR an existing flag row (i.e. an unknown flag
        cannot be created via this API — only toggled / re-configured).
        """
        if key not in DEFAULT_FLAGS and key not in self._cache:
            return False
        flag = self._cache.get(key)
        desc = flag.description if flag else DEFAULT_FLAGS.get(key, {}).get("description", "")
        cfg = config if config is not None else (flag.config if flag else {})
        now = time.time()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO feature_flags (key, enabled, description, config, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, int(enabled), desc, json.dumps(cfg), now),
                )
        except sqlite3.Error as e:
            logger.error("[feature_flags] set failed for key=%s: %s", key, e)
            return False
        self._cache[key] = FeatureFlag(key=key, enabled=enabled, description=desc, config=cfg, updated_at=now)
        logger.info("Feature flag '%s' set to %s", key, enabled)
        return True

    def reset(self, key: str) -> bool:
        """Reset a flag to its default ``enabled`` / ``config``.

        Returns ``False`` if ``key`` is not in ``DEFAULT_FLAGS``.
        """
        if key not in DEFAULT_FLAGS:
            return False
        default = DEFAULT_FLAGS[key]
        return self.set(key, default["enabled"], default.get("config", {}))


# Module-level singleton — production callers do ``from core.feature_flags
# import flag_manager`` then ``flag_manager.is_enabled("live_trading")``.
flag_manager = FeatureFlagManager()


# ── FastAPI route registration ──────────────────────────────────────────────
# The ``FlagUpdate`` Pydantic model is declared at module scope (NOT inside
# ``register_routes``) because the file uses ``from __future__ import
# annotations`` (PEP 563) — every annotation is a string at runtime, and
# FastAPI resolves the string by looking up the handler's ``__globals__``
# (the module namespace). A locally-scoped model would resolve to ``None``
# and FastAPI would fall back to treating ``body`` as a query parameter
# (returning 422 "Field required" on a JSON POST).
try:  # Pydantic v2 — optional at module load if FastAPI is not installed.
    from pydantic import BaseModel

    class FlagUpdate(BaseModel):
        enabled: bool
        config: Optional[dict] = None
except ImportError:  # pragma: no cover — defensive: pydantic is required
    # by FastAPI; if it's missing the routes can't be registered anyway.
    FlagUpdate = None  # type: ignore[assignment,misc]


def register_routes(app: Any) -> None:
    """Append feature-flag management endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET  /api/flags                list all flags + their state/config
      GET  /api/flags/{key}          get a single flag (404 if unknown)
      POST /api/flags/{key}          update a flag (body: {enabled, config?})
      POST /api/flags/{key}/reset    reset a flag to its default value
    """
    from fastapi import HTTPException  # local import — FastAPI is optional at module load

    @app.get("/api/flags", tags=["flags"])
    async def _list_flags():
        """Return every known feature flag + its current state / config."""
        return {"flags": flag_manager.get_all(), "count": len(flag_manager._cache)}

    @app.get("/api/flags/{key}", tags=["flags"])
    async def _get_flag(key: str):
        """Return a single feature flag by key.

        Returns 404 if the key is unknown (not in ``DEFAULT_FLAGS`` and
        not present in the DB), so a typo in a dashboard URL surfaces
        clearly rather than silently returning a disabled flag.
        """
        flag = flag_manager.get_flag(key)
        if flag is None:
            raise HTTPException(status_code=404, detail=f"unknown feature flag: {key}")
        return {
            "key": flag.key,
            "enabled": flag.enabled,
            "description": flag.description,
            "config": flag.config,
            "updated_at": flag.updated_at,
        }

    @app.post("/api/flags/{key}", tags=["flags"])
    async def _set_flag(key: str, body: FlagUpdate):
        """Update a flag's ``enabled`` state (and optionally its ``config``).

        Returns 404 if the key is unknown; 200 with the new flag state
        on success.
        """
        ok = flag_manager.set(key, body.enabled, body.config)
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown feature flag: {key}")
        flag = flag_manager.get_flag(key)
        return {
            "ok": True,
            "flag": {
                "key": flag.key,
                "enabled": flag.enabled,
                "description": flag.description,
                "config": flag.config,
                "updated_at": flag.updated_at,
            }
            if flag
            else None,
        }

    @app.post("/api/flags/{key}/reset", tags=["flags"])
    async def _reset_flag(key: str):
        """Reset a flag to its default ``enabled`` / ``config``.

        Returns 404 if the key is not in ``DEFAULT_FLAGS`` (a flag that
        was never a default cannot be "reset to default").
        """
        ok = flag_manager.reset(key)
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown feature flag (cannot reset): {key}")
        flag = flag_manager.get_flag(key)
        return {
            "ok": True,
            "flag": {
                "key": flag.key,
                "enabled": flag.enabled,
                "description": flag.description,
                "config": flag.config,
                "updated_at": flag.updated_at,
            }
            if flag
            else None,
        }
