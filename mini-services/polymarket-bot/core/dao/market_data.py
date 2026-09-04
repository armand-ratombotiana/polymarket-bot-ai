"""Market data DAO — snapshots, trades, order books."""
import time
import json
from typing import Optional, List

from core.dao.base import BaseDAO


class MarketDataDAO(BaseDAO):
    """Market data access object.

    Delegates to ``core.database_manager.db_manager`` for the actual
    persistence so the PG ↔ SQLite routing logic lives in exactly one
    place. The DAO is a thin domain-typed façade — its value is in the
    *typed* surface (``record_snapshot(token_id=..., best_bid=..., ...)``
    rather than raw ``execute("INSERT INTO market_snapshots ...", (...))``)
    so callers don't have to remember the column order or the
    placeholder dialect ($1 vs ?).

    The DAO also exposes convenience readers (``get_latest_price``) that
    compose the lower-level ``get_snapshots`` call so the dashboard /
    strategy layer doesn't have to re-implement the "latest snapshot"
    lookup pattern.
    """

    def __init__(self):
        super().__init__(
            table_name="market_snapshots", sqlite_db_name="market"
        )

    async def record_snapshot(
        self,
        token_id: str,
        best_bid: float,
        best_ask: float,
        mid: float,
        spread: float,
        bid_size: float = 0,
        ask_size: float = 0,
        volume: float = 0,
        bids_json: str = None,
        asks_json: str = None,
        bid_depth_10: float = 0,
        ask_depth_10: float = 0,
    ) -> bool:
        """Record a market snapshot.

        Routes through ``db_manager.record_snapshot`` so PG ↔ SQLite
        fallback logic is unified across the codebase. ``True`` on a
        successful write to either backend.

        Note: ``bid_size`` / ``ask_size`` are accepted for forward-compat
        with a future schema migration that adds top-of-book size
        columns to ``market_snapshots``; they are NOT currently
        persisted (the existing schema only has ``volume_24h`` /
        ``liquidity``). See ``db_manager.record_snapshot`` for the full
        rationale.
        """
        # Use database_manager's unified method
        from core.database_manager import db_manager

        await db_manager.record_snapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=volume,
            bids_json=bids_json,
            asks_json=asks_json,
            bid_depth_10=bid_depth_10,
            ask_depth_10=ask_depth_10,
        )
        return True

    async def get_snapshots(
        self, token_id: str, limit: int = 100
    ) -> List[dict]:
        """Get snapshots for a token (most-recent-first).

        Empty list when the backend returns no rows or fails.
        """
        from core.database_manager import db_manager

        return await db_manager.get_snapshots(token_id, limit)

    async def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        trade_id: str = None,
        timestamp: float = None,
    ):
        """Record a public trade on the trade tape.

        ``trade_id`` is the dedup key on both PG (``ON CONFLICT DO
        NOTHING``) and SQLite (``INSERT OR IGNORE`` — enforced by the
        unique index on ``trade_id``). When ``trade_id`` is ``None``
        the row is inserted with an empty-string ``trade_id`` —
        effectively no dedup, matching the W20-7 contract in
        ``core/timescale_db.py``.
        """
        from core.database_manager import db_manager

        await db_manager.record_trade(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            trade_id=trade_id or "",
            timestamp=timestamp,
        )

    async def get_trades(
        self, token_id: str = None, limit: int = 100
    ) -> List[dict]:
        """Get recent trades (most-recent-first).

        When ``token_id`` is ``None`` returns the global recent tape
        across all tokens (via ``get_trade_tape`` — ``get_trades`` on
        ``db_manager`` requires a non-empty ``token_id``).
        """
        from core.database_manager import db_manager

        if token_id is None:
            return await db_manager.get_trade_tape(
                token_id=None, limit=limit
            )
        return await db_manager.get_trades(token_id, limit)

    async def get_latest_price(self, token_id: str) -> Optional[dict]:
        """Get the latest snapshot for a token.

        Returns ``None`` when no snapshots exist for the token (the
        underlying ``get_snapshots(token_id, limit=1)`` returns an empty
        list). Otherwise returns the single most-recent snapshot row.
        """
        snapshots = await self.get_snapshots(token_id, limit=1)
        return snapshots[0] if snapshots else None


# Singleton — mirrors the pattern used by every other core module
# (``store = DataStore()``, ``decision_ledger = DecisionLedger()``, etc.).
# Construction is cheap (BaseDAO.__init__ only stores two strings) and
# side-effect-free; the SQLite file is opened lazily on the first method
# call.
market_data_dao = MarketDataDAO()
