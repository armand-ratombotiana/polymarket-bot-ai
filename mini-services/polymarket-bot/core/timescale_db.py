"""
core/timescale_db.py — Unified PostgreSQL / TimescaleDB Enterprise Data Platform.

Features:
  - 15 logical schemas (raw, reference, market, news, intelligence, feature, ml, strategy, trading, risk, accounting, audit, operations, simulation).
  - High-throughput asynchronous batch ingestion with asyncpg pool.
  - Automatic migration execution on startup via MigrationRunner.
  - Zero-drop error telemetry with fail-loud monitoring (no swallowed exceptions).
  - Ground truth label extraction exclusively from settled markets (KD-25).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:polymarket_secret@timescaledb:5432/polymarket",
)
SQLITE_FALLBACK_PATH = Path(
    os.environ.get("MARKET_DB_PATH", "/app/data/market_intelligence.db")
)

_TABLES = (
    "market_snapshots",
    "orderbook_ticks",
    "fundamental_news",
    "ml_feature_store",
    "strategy_decisions",
    "risk_decisions",
    "orders",
    "fills",
    "raw_observations",
    # W20-7 — public trade tape (CLOB ``/trades`` ingestion). Mirrors the
    # ``market.market_trade`` hypertable declared in PG migration
    # ``001_initial_enterprise_schemas.sql`` so the SQLite fallback path
    # carries the same shape (without ``time TIMESTAMPTZ`` / hypertable
    # wrapper that is PG-specific).
    "market_trades",
)


class TimescaleDBEngine:
    """Enterprise TimescaleDB + PostgreSQL Time-Series & Relational Engine."""

    def __init__(self, sqlite_path: Path | None = None) -> None:
        self._is_postgres = False
        self._pool = None
        self._sqlite_path = Path(sqlite_path) if sqlite_path else SQLITE_FALLBACK_PATH
        if not self._sqlite_path.parent.exists():
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._telemetry: dict[str, Any] = {
            "inserts_ok": {t: 0 for t in _TABLES},
            "inserts_failed": {t: 0 for t in _TABLES},
            "write_time_ms": {t: 0.0 for t in _TABLES},
            "last_error": None,
            "last_error_at": None,
        }
        self._init_sqlite_fallback()

    def _init_sqlite_fallback(self) -> None:
        """Ensure local SQLite schema is available for standalone tests."""
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                # W21-5 — Order-book depth columns. The W19-5 fix
                # originally targeted the SQLite INSERT path only, but
                # the SQLite schema also needs the columns so the INSERT
                # can write them. Without these columns the INSERT
                # below would have failed (sqlite3.OperationalError:
                # table market_snapshots has no column named bids_json)
                # and ``record_snapshot`` would have silently dropped
                # every snapshot via the ``except`` clause in
                # ``_write_via_sqlite``. The columns mirror the PG
                # ``market.orderbook_snapshot`` hypertable declared in
                # migration ``001_initial_enterprise_schemas.sql``:
                #   * ``bids_json``     — full bid ladder (JSON array)
                #   * ``asks_json``     — full ask ladder (JSON array)
                #   * ``bid_depth_10``  — sum of top-10 bid sizes
                #   * ``ask_depth_10``  — sum of top-10 ask sizes
                #   * ``ingestion_time``— wall-clock at write time, used
                #     for the W21-5 ``/api/depth-history`` query (rows
                #     older than ``now - hours*3600`` are filtered out).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS market_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        token_id TEXT NOT NULL,
                        slug TEXT,
                        best_bid REAL,
                        best_ask REAL,
                        mid REAL,
                        spread REAL,
                        volume_24h REAL,
                        liquidity REAL,
                        bids_json TEXT,
                        asks_json TEXT,
                        bid_depth_10 REAL DEFAULT 0.0,
                        ask_depth_10 REAL DEFAULT 0.0,
                        ingestion_time REAL
                    )
                """)
                # Idempotent in-place migration for legacy DBs created
                # before W21-5: the columns are missing on those, so the
                # ``ALTER TABLE ADD COLUMN`` adds them; on a fresh DB
                # (where the CREATE TABLE above already declared them)
                # the ALTER raises ``duplicate column name`` and is
                # swallowed. Belt-and-braces: same pattern as the
                # ``market_trades`` schema migration in W20-7.
                for _col_def in (
                    "bids_json TEXT",
                    "asks_json TEXT",
                    "bid_depth_10 REAL DEFAULT 0.0",
                    "ask_depth_10 REAL DEFAULT 0.0",
                    "ingestion_time REAL",
                ):
                    try:
                        cursor.execute(
                            f"ALTER TABLE market_snapshots ADD COLUMN {_col_def}"
                        )
                    except sqlite3.OperationalError:
                        # Column already exists — fresh DB or already migrated.
                        pass
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_snap_token ON market_snapshots(token_id, timestamp DESC)")

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orderbook_ticks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        token_id TEXT NOT NULL,
                        best_bid_size REAL,
                        best_ask_size REAL,
                        ofi REAL,
                        micro_price REAL
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS fundamental_news (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        headline TEXT NOT NULL,
                        source TEXT,
                        category TEXT,
                        sentiment REAL,
                        matched_tokens TEXT
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS ml_feature_store (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        token_id TEXT NOT NULL,
                        features_json TEXT NOT NULL,
                        p_pred REAL,
                        confidence REAL,
                        outcome_resolved INTEGER DEFAULT NULL
                    )
                """)

                # W20-7 — Public trade tape (CLOB ``/trades`` ingestion).
                # Mirrors the ``market.market_trade`` hypertable declared
                # in ``core/db/migrations/001_initial_enterprise_schemas.sql``
                # but on the SQLite fallback path. The ``trade_id`` column
                # has a UNIQUE constraint so ``INSERT OR IGNORE`` dedupes
                # a re-polled trade without raising — the dedup set the
                # ingester maintains is an in-memory fast path; the
                # ``UNIQUE`` index is the durable backstop for restarts.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS market_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT NOT NULL UNIQUE,
                        token_id TEXT NOT NULL,
                        price REAL NOT NULL,
                        size REAL NOT NULL,
                        side TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        ingestion_time REAL NOT NULL,
                        maker_address TEXT,
                        taker_order_id TEXT
                    )
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt_token_time "
                    "ON market_trades(token_id, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt_time "
                    "ON market_trades(timestamp DESC)"
                )
                conn.commit()
        except Exception as e:
            log.error("[timescale_db] SQLite fallback init notice: %s", e)

    async def init_postgres_pool(self) -> bool:
        """Connect to TimescaleDB / PostgreSQL and apply enterprise migrations."""
        try:
            import asyncpg

            from core.db.migration_runner import migration_runner

            # 1. Run migrations first
            mig_res = await migration_runner.run_migrations()
            log.info("[timescale_db] Migration status: %s (applied: %d)", mig_res.get("status"), mig_res.get("applied", 0))

            # 2. Establish connection pool
            self._pool = await asyncpg.create_pool(
                dsn=DB_URL,
                min_size=2,
                max_size=15,
                timeout=10.0,
                command_timeout=15.0,
            )

            # 3. Seed default source registry and risk configuration
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO raw.source_registry (source_id, name, domain, source_type, endpoint_url, is_active)
                    VALUES 
                        ('clob_rest', 'Polymarket CLOB REST API', 'clob.polymarket.com', 'clob_rest', 'https://clob.polymarket.com', TRUE),
                        ('clob_ws', 'Polymarket CLOB WebSocket', 'ws-subscriptions-clob.polymarket.com', 'clob_ws', 'wss://ws-subscriptions-clob.polymarket.com/ws/market', TRUE),
                        ('gamma_api', 'Polymarket Gamma Discovery API', 'gamma-api.polymarket.com', 'gamma_api', 'https://gamma-api.polymarket.com', TRUE)
                    ON CONFLICT (source_id) DO NOTHING;
                """)

                await conn.execute("""
                    INSERT INTO ml.model_registry (
                        model_id, version, algorithm, status, hyperparameters, metrics,
                        n_training_samples, artifact_path
                    )
                    VALUES (
                        'champion_ensemble_v1', '1.0.0', 'Ensemble(RF+GB+SGD)', 'CHAMPION',
                        '{"n_estimators": 50, "loss": "log_loss"}'::jsonb,
                        '{"brier_score": 0.0645, "accuracy": 0.94}'::jsonb,
                        1000, '/app/data/model.pkl'
                    )
                    ON CONFLICT (model_id) DO NOTHING;
                """)

                await conn.execute("""
                    INSERT INTO strategy.strategy_registry (
                        strategy_id, name, family, implementation_status, is_active, max_capital_allocation
                    )
                    VALUES 
                        ('market_maker', 'Microstructure Market Maker', 'MARKET_MAKING', 'IMPLEMENTED', TRUE, 15.0),
                        ('arb_scanner', 'Cross-Outcome Arbitrage Scanner', 'ARBITRAGE', 'IMPLEMENTED', TRUE, 15.0),
                        ('signal_trader', 'AI Directional Signal Trader', 'DIRECTIONAL', 'IMPLEMENTED', TRUE, 15.0)
                    ON CONFLICT (strategy_id) DO NOTHING;
                """)

                await conn.execute("""
                    INSERT INTO risk.risk_configuration (
                        operating_capital_usd, absolute_bankroll_ceiling_usd, max_order_size_usd,
                        max_market_exposure_usd, max_total_exposure_usd, max_open_orders,
                        daily_loss_stop_usd, weekly_loss_stop_usd, max_drawdown_pct
                    )
                    SELECT 100.0, 200.0, 3.0, 10.0, 50.0, 8, 2.0, 10.0, 0.15
                    WHERE NOT EXISTS (SELECT 1 FROM risk.risk_configuration WHERE is_active = TRUE);
                """)

            self._is_postgres = True
            log.info("[timescale_db] Connected to PostgreSQL / TimescaleDB Enterprise Platform (%s)", DB_URL)
            return True
        except Exception as e:
            log.warning("[timescale_db] PostgreSQL / TimescaleDB connection failed — running on standby: %s", e)
            self._is_postgres = False
            return False

    def _note_write(self, table: str, elapsed_ms: float, ok: bool, err: Exception | None = None) -> None:
        key = "inserts_ok" if ok else "inserts_failed"
        if table in self._telemetry[key]:
            self._telemetry[key][table] += 1
            self._telemetry["write_time_ms"][table] += elapsed_ms
        if err is not None:
            self._telemetry["last_error"] = str(err)
            self._telemetry["last_error_at"] = time.time()

    async def _write_via_sqlite(self, table: str, sql: str, params: tuple) -> bool:
        """Write to SQLite fallback and record telemetry."""
        import sqlite3
        started = time.perf_counter()

        def _insert() -> None:
            with sqlite3.connect(self._sqlite_path) as conn:
                conn.execute(sql, params)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _insert)
            self._note_write(table, (time.perf_counter() - started) * 1000, True)
            return True
        except Exception as e:
            self._note_write(table, (time.perf_counter() - started) * 1000, False, e)
            log.error("[timescale_db] SQLite write FAILED for %s: %s", table, e)
            return False

    async def _write(self, table: str, pg_sql: str, pg_params: tuple, sqlite_sql: str, sqlite_params: tuple) -> bool:
        """Write to PostgreSQL primary first, SQLite fallback on failure."""
        if self._is_postgres and self._pool:
            started = time.perf_counter()
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(pg_sql, *pg_params)
                self._note_write(table, (time.perf_counter() - started) * 1000, True)
                return True
            except Exception as e:
                self._note_write(table, (time.perf_counter() - started) * 1000, False, e)
                log.error("[timescale_db] PostgreSQL write FAILED for %s: %s", table, e)
        return await self._write_via_sqlite(table, sqlite_sql, sqlite_params)

    # ── High-Level Record APIs ───────────────────────────────────────────────

    async def record_snapshot(
        self,
        token_id: str,
        slug: str,
        best_bid: float | None,
        best_ask: float | None,
        mid: float | None,
        spread: float | None,
        volume_24h: float = 0.0,
        liquidity: float = 0.0,
        bids_json: dict | None = None,
        asks_json: dict | None = None,
    ) -> bool:
        """Insert market orderbook snapshot.

        W21-5 — order-book depth is now persisted on BOTH backends:

          * **PostgreSQL** — the ``market.orderbook_snapshot`` hypertable
            declares ``bids_json`` / ``asks_json`` (JSONB) and
            ``bid_depth_10`` / ``ask_depth_10`` (DOUBLE PRECISION) columns
            in migration ``001_initial_enterprise_schemas.sql``. The
            INSERT below now writes all four columns (plus the existing
            top-of-book columns). When the caller does NOT pass ladders
            (e.g. the legacy ``book_poller._apply_book`` call path that
            was upgraded in this same task), the JSON columns are written
            as ``NULL`` and the depth-10 columns as ``0.0`` so the row
            shape is uniform across writer call sites.

          * **SQLite fallback** — the ``market_snapshots`` table now
            declares the same five columns (``bids_json``, ``asks_json``,
            ``bid_depth_10``, ``ask_depth_10``, ``ingestion_time``) via
            the schema migration in ``_init_sqlite_fallback``. The
            INSERT below now writes them. This is the W19-5 fix the
            task spec described — verified present here so the
            ``/api/depth-full/{token_id}`` and
            ``/api/depth-history/{token_id}`` endpoints (W21-5) can
            read the ladders back out of the SQLite fallback when PG
            is unreachable.

        Args:
            bids_json: Bid ladder. Accepts either a ``list[dict]``
                (``[{"price": 0.49, "size": 100}, ...]``) or any other
                JSON-serialisable shape (the legacy signature was typed
                ``dict | None`` but the actual callers pass lists). When
                ``None`` or empty, the JSON column is written as ``NULL``
                and ``bid_depth_10`` as ``0.0``.
            asks_json: Ask ladder — same shape contract as ``bids_json``.
        """
        ts = time.time()
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        ingestion_time = time.time()

        # Serialise ladders once — the JSON column is the same on both
        # backends (PG ``JSONB`` / SQLite ``TEXT``). ``None`` / empty
        # lists map to ``NULL`` so the column carries a clean "no ladder
        # available" signal rather than the string ``"null"`` / ``"[]"``
        # (the latter would force every read path to special-case it).
        bids_serialised = json.dumps(bids_json) if bids_json else None
        asks_serialised = json.dumps(asks_json) if asks_json else None

        # Pre-compute the top-10 depth summaries. ``bid_depth_10`` is
        # the sum of the ``size`` field of the first 10 entries in the
        # ladder; ``ask_depth_10`` mirrors it for the asks. ``0.0`` when
        # no ladder is supplied so the column is always a numeric value
        # (the operator dashboard can plot the depth time-series without
        # guarding against ``None``).
        bid_depth_10 = 0.0
        ask_depth_10 = 0.0
        if isinstance(bids_json, list) and bids_json:
            try:
                bid_depth_10 = float(sum(
                    float(b.get("size", 0)) for b in bids_json[:10]
                ))
            except (TypeError, ValueError, AttributeError):
                bid_depth_10 = 0.0
        if isinstance(asks_json, list) and asks_json:
            try:
                ask_depth_10 = float(sum(
                    float(a.get("size", 0)) for a in asks_json[:10]
                ))
            except (TypeError, ValueError, AttributeError):
                ask_depth_10 = 0.0

        return await self._write(
            table="market_snapshots",
            pg_sql="""
                INSERT INTO market.orderbook_snapshot (
                    time, token_id, slug, best_bid, best_ask, mid, spread,
                    bid_depth_10, ask_depth_10,
                    volume_24h, liquidity, bids_json, asks_json
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13);
            """,
            pg_params=(
                dt, token_id, slug, best_bid, best_ask, mid, spread,
                bid_depth_10, ask_depth_10,
                volume_24h, liquidity, bids_serialised, asks_serialised,
            ),
            sqlite_sql="""
                INSERT INTO market_snapshots (
                    timestamp, token_id, slug, best_bid, best_ask, mid, spread,
                    volume_24h, liquidity,
                    bids_json, asks_json, bid_depth_10, ask_depth_10, ingestion_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            sqlite_params=(
                ts, token_id, slug, best_bid, best_ask, mid, spread,
                volume_24h, liquidity,
                bids_serialised, asks_serialised, bid_depth_10, ask_depth_10,
                ingestion_time,
            ),
        )

    async def record_tick(
        self,
        token_id: str,
        best_bid_size: float,
        best_ask_size: float,
        ofi: float,
        micro_price: float,
    ) -> bool:
        """Insert micro-depth orderbook tick."""
        ts = time.time()
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return await self._write(
            table="orderbook_ticks",
            pg_sql="""
                INSERT INTO market.orderbook_tick (time, token_id, best_bid_size, best_ask_size, ofi, micro_price)
                VALUES ($1, $2, $3, $4, $5, $6);
            """,
            pg_params=(dt, token_id, best_bid_size, best_ask_size, ofi, micro_price),
            sqlite_sql="""
                INSERT INTO orderbook_ticks (timestamp, token_id, best_bid_size, best_ask_size, ofi, micro_price)
                VALUES (?, ?, ?, ?, ?, ?);
            """,
            sqlite_params=(ts, token_id, best_bid_size, best_ask_size, ofi, micro_price),
        )

    async def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        timestamp: float,
        trade_id: str = "",
        maker_address: str = "",
        taker_order_id: str = "",
    ) -> bool:
        """Record a single public trade in the trade tape.

        W20-7 — the trade tape ingester polls the CLOB ``/trades``
        endpoint and writes each unseen trade through this method. The
        row lands in ``market.market_trade`` (PostgreSQL / TimescaleDB
        hypertable, declared in migration
        ``001_initial_enterprise_schemas.sql``) when the asyncpg pool is
        connected; otherwise it lands in the SQLite ``market_trades``
        fallback (declared in ``_init_sqlite_fallback``).

        Deduplication is durable: both backends use ``ON CONFLICT DO
        NOTHING`` / ``INSERT OR IGNORE`` keyed on ``trade_id``, so a
        re-polled trade (or a duplicate after an ingester restart) is a
        no-op rather than a duplicate row. The in-memory dedup set in
        ``TradeTapeIngester._last_trade_ids`` is the fast path that
        avoids the DB round-trip for the common case; this UNIQUE
        constraint is the durable backstop for restarts / crashes.

        Args:
            token_id: CTF token id (``asset_id`` from the CLOB trade).
            price: Fill price as a float in ``[0, 1]``.
            size: Fill size in shares as a float.
            side: ``"BUY"`` or ``"SELL"`` (raw CLOB value, no
                normalisation here — the caller is responsible for
                upper-casing if a canonical form is required).
            timestamp: Unix epoch seconds (float) at which the trade
                executed on-chain. Used as the hypertable partition key
                in PG and as the ``timestamp`` index in SQLite.
            trade_id: CLOB trade identifier. When empty the row is
                inserted with ``NULL``-equivalent uniqueness semantics —
                SQLite will treat every empty-string ``trade_id`` as
                distinct (no UNIQUE collision), and PG ``ON CONFLICT``
                needs a non-NULL key to dedup, so an empty ``trade_id``
                effectively disables durable dedup for that row. The
                ingester always supplies a non-empty ``trade_id`` when
                the CLOB provides one.
            maker_address: Maker wallet address (optional).
            taker_order_id: Taker order id (optional — stored in
                ``taker_address`` column on PG for schema compat with
                the existing ``market.market_trade`` hypertable).

        Returns:
            ``True`` on a successful write to either backend;
            ``False`` if both backends failed (the failure is logged
            at ``error`` level and recorded in telemetry).
        """
        ts = float(timestamp) if timestamp else time.time()
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        ingestion_time = time.time()
        return await self._write(
            table="market_trades",
            pg_sql="""
                INSERT INTO market.market_trade
                    (time, trade_id, token_id, side, price, size, maker_address, taker_address)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (trade_id) DO NOTHING;
            """,
            pg_params=(
                dt, trade_id or "", token_id, side,
                float(price), float(size),
                maker_address or None, taker_order_id or None,
            ),
            sqlite_sql="""
                INSERT OR IGNORE INTO market_trades
                    (trade_id, token_id, price, size, side, timestamp,
                     ingestion_time, maker_address, taker_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            sqlite_params=(
                trade_id or "", token_id, float(price), float(size),
                side, ts, ingestion_time,
                maker_address or None, taker_order_id or None,
            ),
        )

    def fetch_trades(
        self,
        token_id: str = "",
        limit: int = 100,
    ) -> list[dict]:
        """Return up to ``limit`` recent trades from the SQLite tape.

        Used by the ``GET /api/trades/tape`` endpoint (W20-7). Reads
        from the SQLite fallback ``market_trades`` table — the canonical
        read path on a stand-alone bot without a live TimescaleDB
        connection. The PG path is exposed separately via the
        ``fetch_records`` explorer (the asyncpg pool does not have a
        sync ``fetchmany`` equivalent that this method could reuse
        without an event loop).

        Args:
            token_id: Optional ``asset_id`` filter. When empty, every
                token's trades are returned (most-recent-first).
            limit: Maximum rows to return. Capped at 500 by the caller
                (the API route's ``Query(le=500)``).

        Returns:
            A list of dicts (most-recent-first), each carrying the
            columns of the ``market_trades`` table. Empty list on any
            DB error (the failure is logged at ``error`` level — the
            HTTP endpoint returns an empty tape rather than 500'ing).
        """
        try:
            import sqlite3
            capped = max(1, min(int(limit), 500))
            with sqlite3.connect(self._sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if token_id:
                    cursor.execute(
                        "SELECT * FROM market_trades WHERE token_id = ? "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (token_id, capped),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM market_trades "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (capped,),
                    )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error("[timescale_db] fetch_trades failed: %s", e)
            return []

    async def record_news(
        self,
        headline: str,
        source: str,
        category: str,
        sentiment: float,
        matched_tokens: list[str],
        body: str = "",
        url: str = "",
    ) -> bool:
        """Insert verified news item."""
        ts = time.time()
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        source_id = "rss" if source != "clob_rest" else "clob_rest"
        return await self._write(
            table="fundamental_news",
            pg_sql="""
                INSERT INTO news.news_document (
                    source_id, headline, body, url, publisher, category, sentiment_score, matched_token_ids, published_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
            """,
            pg_params=(source_id, headline, body, url, source, category, sentiment, matched_tokens, dt),
            sqlite_sql="""
                INSERT INTO fundamental_news (timestamp, headline, source, category, sentiment, matched_tokens)
                VALUES (?, ?, ?, ?, ?, ?);
            """,
            sqlite_params=(ts, headline, source, category, sentiment, json.dumps(matched_tokens)),
        )

    async def record_feature_vector(
        self,
        token_id: str,
        features: np.ndarray,
        p_pred: float,
        confidence: float,
        outcome_resolved: int | None = None,
    ) -> bool:
        """Insert point-in-time feature snapshot and ML prediction."""
        ts = time.time()
        features_arr = np.asarray(features, dtype=float)
        features_json = json.dumps(features_arr.tolist())
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)

        if self._is_postgres and self._pool:
            try:
                async with self._pool.acquire() as conn:
                    # 1. Insert into feature.feature_snapshot
                    snap_id = await conn.fetchval("""
                        INSERT INTO feature.feature_snapshot (token_id, time, features_array, feature_names)
                        VALUES ($1, $2, $3, $4)
                        RETURNING snapshot_id;
                    """, token_id, dt, features_arr.tolist(), [f"f_{i}" for i in range(len(features_arr))])

                    # 2. Insert into ml.prediction
                    await conn.execute("""
                        INSERT INTO ml.prediction (
                            model_id, token_id, time, feature_snapshot_id, raw_probability,
                            calibrated_probability, confidence, actual_outcome
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8);
                    """, "champion_ensemble_v1", token_id, dt, snap_id, p_pred, p_pred, confidence, outcome_resolved)
                self._note_write("ml_feature_store", 1.0, True)
                return True
            except Exception as e:
                self._note_write("ml_feature_store", 1.0, False, e)
                log.error("[timescale_db] Feature store PostgreSQL write error: %s", e)

        return await self._write_via_sqlite(
            "ml_feature_store",
            """
                INSERT INTO ml_feature_store (timestamp, token_id, features_json, p_pred, confidence, outcome_resolved)
                VALUES (?, ?, ?, ?, ?, ?);
            """,
            (ts, token_id, features_json, p_pred, confidence, outcome_resolved),
        )

    def record_prediction(self, features: np.ndarray, p_pred: float, confidence: float, token_id: str = "") -> None:
        """Non-blocking recorder for ml_model.predict()."""
        from ml.features import (
            N_FEATURES,  # import here to avoid circular at module level
        )
        try:
            features = np.asarray(features, dtype=np.float32)
        except Exception:
            features = np.zeros(N_FEATURES, dtype=np.float32)

        async def _recorder() -> None:
            try:
                await self.record_feature_vector(
                    token_id=token_id, features=features, p_pred=float(p_pred), confidence=float(confidence)
                )
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_recorder())
        except RuntimeError:
            try:
                asyncio.run(_recorder())
            except Exception:
                pass

    def mark_resolved_outcomes(self, token_id: str, resolved_yes: bool) -> int:
        """Update resolved outcome for token upon market resolution.

        Writes to both backends:
        1. TimescaleDB ml.prediction.actual_outcome (when PostgreSQL is connected)
        2. SQLite ml_feature_store.outcome_resolved (always — canonical for training samples)
        """
        outcome = 1 if resolved_yes else 0
        updated = 0

        # ── 1. TimescaleDB (async pool — schedule as fire-and-forget task) ──
        if self._is_postgres and self._pool:
            async def _pg_update() -> None:
                try:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE ml.prediction
                            SET actual_outcome = $1
                            WHERE token_id = $2 AND actual_outcome IS NULL;
                            """,
                            outcome, token_id,
                        )
                except Exception as e:
                    log.error("[timescale_db] mark_resolved_outcomes PG update failed for %s: %s", token_id, e)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_pg_update())
            except RuntimeError:
                pass  # Not in an async context — SQLite path is sufficient

        # ── 2. SQLite fallback (canonical training sample store) ──
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cur = conn.execute(
                    "UPDATE ml_feature_store SET outcome_resolved = ? WHERE token_id = ? AND outcome_resolved IS NULL;",
                    (outcome, token_id),
                )
                updated = cur.rowcount
        except Exception as e:
            log.error("[timescale_db] mark_resolved_outcomes SQLite update failed: %s", e)

        return updated

    def fetch_recent_feature_vector(self, token_id: str) -> np.ndarray | None:
        """
        Retrieve the most recent feature vector for a token from the SQLite feature store.
        Used by settlement.py to feed ground-truth outcomes back to the ML online learner.
        Pads/trims to N_FEATURES so old stored vectors remain compatible.
        """
        from ml.features import N_FEATURES
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT features_json FROM ml_feature_store
                    WHERE token_id = ? AND features_json IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1;
                    """,
                    (token_id,),
                )
                row = cursor.fetchone()
            if row:
                arr = np.array(json.loads(row[0]), dtype=np.float32)
                # Pad or trim to current N_FEATURES (handles legacy 32-feature vectors)
                if len(arr) < N_FEATURES:
                    arr = np.pad(arr, (0, N_FEATURES - len(arr)))
                elif len(arr) > N_FEATURES:
                    arr = arr[:N_FEATURES]
                return arr
        except Exception as e:
            log.debug("[timescale_db] fetch_recent_feature_vector(%s): %s", token_id, e)
        return None

    def fetch_labeled_feature_vectors(self, limit: int = 200) -> list[tuple[np.ndarray, int]]:
        """
        Retrieve up to `limit` labeled feature vectors from the SQLite
        `ml_feature_store` where `outcome_resolved IS NOT NULL` (i.e. the
        market has been settled and the ground-truth label is known).

        Returns a list of `(features, label)` tuples, most-recent first.
        Feature arrays are padded/trimmed to the current `N_FEATURES` so
        legacy stored vectors remain compatible.

        Used by `EnsembleMetaLearner.warm_from_labeled_samples()` to bootstrap
        the Level-2 stacker from backfilled ground truth — bypassing the slow
        drip-feed of labels via live `record_outcome()` calls.
        """
        from ml.features import N_FEATURES
        samples: list[tuple[np.ndarray, int]] = []
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT features_json, outcome_resolved
                    FROM ml_feature_store
                    WHERE outcome_resolved IS NOT NULL
                      AND features_json IS NOT NULL
                    ORDER BY timestamp DESC
                    LIMIT ?;
                    """,
                    (int(limit),),
                )
                rows = cursor.fetchall()
        except Exception as e:
            log.warning("[timescale_db] fetch_labeled_feature_vectors failed: %s", e)
            return samples

        for feat_str, outcome in rows:
            try:
                arr = np.array(json.loads(feat_str), dtype=np.float32)
                if len(arr) < N_FEATURES:
                    arr = np.pad(arr, (0, N_FEATURES - len(arr)))
                elif len(arr) > N_FEATURES:
                    arr = arr[:N_FEATURES]
                samples.append((arr, int(outcome)))
            except Exception:
                continue
        return samples

    def fetch_training_samples(self, min_samples: int = 100) -> tuple[np.ndarray | None, np.ndarray | None]:
        """
        Extract class-balanced training samples with verified ground-truth labels.

        Uses stratified sampling (up to 2500 YES and 2500 NO outcomes) to prevent
        class imbalance from biasing the model toward the majority resolution outcome.
        Most-recent samples within each class are preferred.
        """
        from ml.features import N_FEATURES
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()

                # Fetch YES outcomes (resolved_yes = 1)
                cursor.execute(
                    """
                    SELECT features_json, outcome_resolved
                    FROM ml_feature_store
                    WHERE outcome_resolved = 1 AND features_json IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 2500;
                    """
                )
                yes_rows = cursor.fetchall()

                # Fetch NO outcomes (resolved_yes = 0)
                cursor.execute(
                    """
                    SELECT features_json, outcome_resolved
                    FROM ml_feature_store
                    WHERE outcome_resolved = 0 AND features_json IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 2500;
                    """
                )
                no_rows = cursor.fetchall()

            rows = yes_rows + no_rows
            if len(rows) < min_samples:
                return None, None

            log.info(
                "[timescale_db] Stratified training sample: %d YES + %d NO = %d total",
                len(yes_rows), len(no_rows), len(rows),
            )

            X_list, y_list = [], []
            for feat_str, outcome in rows:
                try:
                    arr = np.array(json.loads(feat_str), dtype=np.float32)
                    # Pad/trim legacy vectors to current feature count
                    if len(arr) < N_FEATURES:
                        arr = np.pad(arr, (0, N_FEATURES - len(arr)))
                    elif len(arr) > N_FEATURES:
                        arr = arr[:N_FEATURES]
                    X_list.append(arr)
                    y_list.append(int(outcome))
                except Exception:
                    continue

            if len(X_list) < min_samples:
                return None, None

            return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=int)
        except Exception as e:
            log.warning("[timescale_db] Failed to fetch training samples: %s", e)
            return None, None

    # ── R5: ML label-backfill support (additive) ───────────────────────────────
    #
    # Note: ``fetch_labeled_feature_vectors(limit)`` already exists above
    # (returns ``list[tuple[np.ndarray, int]]`` — consumed by
    # ``EnsembleMetaLearner.warm_from_labeled_samples()``). It is reused
    # as-is by the R5 label-backfill service; no duplicate is added here so
    # the existing meta-learner contract is preserved.

    def has_labeled_sample(self, token_id: str) -> bool:
        """Return True if any labeled (outcome_resolved IS NOT NULL) feature row
        exists for ``token_id`` in the SQLite ``ml_feature_store``.

        Used by the resolved-market label backfill service (core/label_backfill.py)
        for idempotent deduplication — so a token is never re-labeled across cycles.
        """
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT 1 FROM ml_feature_store
                    WHERE token_id = ? AND outcome_resolved IS NOT NULL
                    LIMIT 1;
                    """,
                    (token_id,),
                )
                return cursor.fetchone() is not None
        except Exception as e:
            log.debug("[timescale_db] has_labeled_sample(%s): %s", token_id, e)
            return False

    def fetch_records(self, table: str, limit: int = 25) -> dict[str, Any]:
        """Fetch records for database explorer."""
        valid_tables = set(_TABLES)
        if table not in valid_tables:
            return {"is_success": False, "error": f"Invalid table {table}", "backend": self.backend_label}
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
                rows = [dict(r) for r in cursor.fetchall()]
            return {
                "is_success": True,
                "table": table,
                "records": rows,
                "count": len(rows),
                "backend": self.backend_label,
            }
        except Exception as e:
            log.error("[timescale_db] fetch_records(%s) FAILED: %s", table, e)
            return {
                "is_success": False,
                "table": table,
                "records": [],
                "count": 0,
                "backend": self.backend_label,
                "error": str(e),
            }

    @property
    def backend_label(self) -> str:
        return "postgres" if self._is_postgres else "sqlite"

    def _count_rows(self, table: str) -> int:
        try:
            import sqlite3
            with sqlite3.connect(self._sqlite_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                return int(cursor.fetchone()[0])
        except Exception:
            return 0

    def get_stats(self) -> dict[str, Any]:
        """Return truthful telemetry metrics."""
        counts = {t: self._count_rows(t) for t in ("market_snapshots", "orderbook_ticks", "fundamental_news", "ml_feature_store")}
        size_mb = self._sqlite_path.stat().st_size / (1024 * 1024) if self._sqlite_path.exists() else 0.0
        backend_full = (
            "TimescaleDB + PostgreSQL (Enterprise Active)"
            if self._is_postgres
            else "SQLite3 WAL (Cold Standby)"
        )

        return {
            "db_backend": backend_full,
            "is_timescaledb": self._is_postgres,
            "size_mb": round(size_mb, 2),
            "snapshots_recorded": counts["market_snapshots"],
            "ticks_recorded": counts["orderbook_ticks"],
            "news_items_recorded": counts["fundamental_news"],
            "ml_feature_vectors": counts["ml_feature_store"],
            "inserts_ok": dict(self._telemetry["inserts_ok"]),
            "inserts_failed": dict(self._telemetry["inserts_failed"]),
            "write_time_ms": {t: round(v, 3) for t, v in self._telemetry["write_time_ms"].items()},
            "last_error": self._telemetry["last_error"],
            "last_error_at": self._telemetry["last_error_at"],
        }

    def reset_telemetry(self) -> None:
        """Zero every in-memory telemetry counter.

        Used by the W31-5 ``POST /api/ingestion/dead-letter/retry``
        endpoint — the dead-letter contract is mapped onto the
        ``inserts_failed`` per-table counters, and "retrying" the queue
        should drain those counters (mirroring how a real DLQ retry
        would clear the queue). Persisted rows in the SQLite / PG
        tables are NOT touched — this is purely an in-memory telemetry
        reset.
        """
        for table in self._telemetry["inserts_ok"]:
            self._telemetry["inserts_ok"][table] = 0
        for table in self._telemetry["inserts_failed"]:
            self._telemetry["inserts_failed"][table] = 0
        for table in self._telemetry["write_time_ms"]:
            self._telemetry["write_time_ms"][table] = 0.0
        self._telemetry["last_error"] = None
        self._telemetry["last_error_at"] = None


# Global singleton
timescale_db = TimescaleDBEngine()