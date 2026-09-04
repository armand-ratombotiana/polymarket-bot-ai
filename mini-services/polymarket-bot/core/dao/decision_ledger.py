"""Decision ledger DAO — unified access to decision events."""
import time
import json
from typing import Optional, List

from core.dao.base import BaseDAO


class DecisionLedgerDAO(BaseDAO):
    """Decision ledger DAO.

    Persists and retrieves decision events keyed by ``correlation_id``
    (a.k.a. the cross-stage trace key — identical to ``decision_id``
    in the legacy ``core/decision_ledger.py``; the W21-4 DAO spec uses
    the spec's preferred name). Each event carries:

      * ``correlation_id`` — the trace key linking all stages of a single
        decision (PREDICTION → SIGNAL → RISK_* → ORDER → FILL → ...).
      * ``token_id`` — the Polymarket condition token the decision is
        about (NULL for global / non-token events).
      * ``stage`` — the canonical stage name (PREDICTION / SIGNAL /
        RISK_APPROVED / RISK_REJECTED / ORDER / FILL / POSITION /
        OUTCOME / PNL / MARKET_SNAPSHOT / INTELLIGENCE_SNAPSHOT /
        FEATURE_SNAPSHOT).
      * ``data`` — arbitrary JSON-serialisable dict carrying the
        stage-specific payload (feature vector for FEATURE_SNAPSHOT,
        fill price/size for FILL, etc.).
      * ``model_version`` — optional ML model version (auto-stamped on
        PREDICTION events by the legacy ledger; the DAO leaves it to
        the caller to populate).

    Schema lives in a *separate* SQLite file (resolved from
    ``BOT_DATA_DIR`` / ``DECISION_LEDGER_DAO_DB_PATH`` via
    ``db_manager.get_sqlite_path("decision_ledger")``) so the legacy
    ``core/decision_ledger.py`` immutable audit trail's contract is not
    perturbed. The schema is created lazily on first write via
    ``db_manager._init_decision_ledger_db()`` so the file is opened on
    demand rather than at module-import time.
    """

    def __init__(self):
        super().__init__(
            table_name="decision_events", sqlite_db_name="decision_ledger"
        )

    def _ensure_schema(self) -> None:
        """Create the ``decision_events`` table if absent.

        Idempotent — ``CREATE TABLE IF NOT EXISTS`` so a re-call on an
        already-initialised DB is a no-op. Called before every write so
        a fresh ``DECISION_LEDGER_DAO_DB_PATH`` (e.g. a per-test
        ``tmp_path``) is bootstrapped automatically without requiring
        the lifespan startup hook to have called ``initialize()``.
        """
        from core.database_manager import db_manager

        # ``db_manager._init_decision_ledger_db()`` is idempotent — it
        # uses ``CREATE TABLE IF NOT EXISTS`` so re-calling on an
        # already-initialised DB is a no-op. The method resolves the
        # path from ``self._sqlite_paths["decision_ledger"]`` (populated
        # eagerly in ``__init__``) so the path is the one the
        # ``get_sqlite_path`` helper below also returns.
        db_manager._init_decision_ledger_db()

    async def record(
        self,
        correlation_id: str,
        token_id: str,
        stage: str,
        data: dict,
        model_version: str = None,
    ):
        """Record a decision event.

        The ``data`` dict is JSON-serialised with ``default=str`` so
        dataclasses / Decimals / enums don't blow up. ``model_version``
        is stored as a separate column (not inside ``data_json``) so
        PG-side queries can index it without unpacking the JSONB blob.
        """
        self._ensure_schema()
        timestamp = time.time()
        query = """
            INSERT INTO decision_events
                (correlation_id, token_id, stage, timestamp,
                 data_json, model_version)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        # Route through the BaseDAO's execute() dispatcher so PG is
        # attempted first when ``db_manager.is_postgres`` is True. The
        # SQLite ``?`` placeholders are mechanically rewritten to PG's
        # ``$N`` form on the PG path — the rewrite is positional (the
        # 6 ``?`` markers map 1:1 to the 6 values in the params tuple),
        # so the placeholder order must match the column order above.
        from core.database_manager import db_manager

        if db_manager.is_postgres:
            query = (
                query.replace("?", "$1", 1)
                .replace("?", "$2", 1)
                .replace("?", "$3", 1)
                .replace("?", "$4", 1)
                .replace("?", "$5", 1)
                .replace("?", "$6", 1)
            )

        await self.execute(
            query,
            (
                correlation_id,
                token_id,
                stage,
                timestamp,
                json.dumps(data, default=str) if data else None,
                model_version,
            ),
        )

    async def get_by_correlation(
        self, correlation_id: str
    ) -> List[dict]:
        """Get all events for a correlation ID (ascending timestamp).

        Returns the ordered stage chain for a single decision — used by
        the dashboard's "decision timeline" view to render the
        PREDICTION → SIGNAL → ... → PNL chain.
        """
        self._ensure_schema()
        query = (
            "SELECT * FROM decision_events "
            "WHERE correlation_id = ? ORDER BY timestamp ASC"
        )
        from core.database_manager import db_manager

        if db_manager.is_postgres:
            query = query.replace("?", "$1", 1)
        return await self.execute(query, (correlation_id,))

    async def get_by_token(
        self, token_id: str, limit: int = 50
    ) -> List[dict]:
        """Get events for a token (most-recent-first).

        ``limit`` defaults to 50 — a sensible dashboard page size; the
        caller can request more by passing a larger value.
        """
        self._ensure_schema()
        query = (
            "SELECT * FROM decision_events "
            "WHERE token_id = ? ORDER BY timestamp DESC LIMIT ?"
        )
        from core.database_manager import db_manager

        if db_manager.is_postgres:
            query = (
                query.replace("?", "$1", 1).replace("?", "$2", 1)
            )
        return await self.execute(query, (token_id, int(limit)))


# Singleton — see market_data_dao for the construction-side rationale.
decision_ledger_dao = DecisionLedgerDAO()
