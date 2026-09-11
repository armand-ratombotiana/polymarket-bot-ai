"""Data Access Objects — unified interface for PG/SQLite.

The ``core.dao`` package provides domain-typed façades over the unified
``core.database_manager.db_manager`` so callers stop constructing raw
SQL strings and ``sqlite3.connect(...)`` calls themselves. Each DAO:

  * Binds to a specific logical SQLite database (``market`` /
    ``decision_ledger`` / future ones) via ``sqlite_db_name``.
  * Exposes async methods that route to PG (primary) or SQLite
    (fallback) through ``db_manager``.
  * Is a process-global singleton (``market_data_dao`` /
    ``decision_ledger_dao``) — construction is cheap and side-effect-free.

Importing the package eagerly imports all DAO modules so the singletons
are constructed on first ``from core.dao import market_data_dao``. The
singletons themselves do NO I/O at construction time — the SQLite file
is opened lazily on the first method call.
"""
from .base import BaseDAO
from .market_data import MarketDataDAO, market_data_dao
from .decision_ledger import DecisionLedgerDAO, decision_ledger_dao

__all__ = [
    "BaseDAO",
    "MarketDataDAO",
    "market_data_dao",
    "DecisionLedgerDAO",
    "decision_ledger_dao",
]
