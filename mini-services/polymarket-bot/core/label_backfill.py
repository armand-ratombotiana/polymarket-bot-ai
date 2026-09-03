"""
core/label_backfill.py — Resolved-Market ML Label Backfill Service (R5).

Pages through resolved markets from the Polymarket Gamma API, builds a 38-dim
feature vector per token from market metadata + a synthetic order book, and
writes (features, resolved_label) rows into the SQLite ``ml_feature_store`` so
the ML ensemble has ground-truth training data even for markets the bot never
traded live.

Lifecycle:
    start()  → schedules a background task that:
        1. Waits ``STARTUP_GRACE_SECONDS`` (45 s) so the rest of the stack
           (DB pool, gamma_client, ml_model) can finish booting.
        2. Runs one full backfill pass.
        3. Triggers ``ml_model.fit_initial()`` if ≥ ``MIN_LABELS_FOR_RETRAIN``
           labeled rows now exist in the feature store.
        4. Loops on a daily interval (``DAILY_INTERVAL_SECONDS`` = 86 400 s).

Idempotency:
    Each token is only ever backfilled once. ``timescale_db.has_labeled_sample``
    is checked before any write — already-labeled tokens are skipped on
    subsequent cycles, so re-running the service is safe.

Synthetic order book:
    Resolved markets no longer have a live CLOB book. We reconstruct a
    plausible 5-level book from Gamma's metadata (outcomePrices, volume24hr,
    liquidity) so ``ml.features.extract_features()`` produces a usable
    feature vector — clipped into a valid (non-resolution-convergence) regime.

This module is purely additive — it does not modify any existing ML or DB
contract. It re-uses:
    * ``core.gamma_client.gamma_client`` for paging
    * ``core.timescale_db.timescale_db`` for persistence + label reads
    * ``ml.features.extract_features`` / ``N_FEATURES`` for feature extraction
    * ``ml.model.ml_model.fit_initial`` for the threshold-triggered retrain
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import numpy as np

from core.data_store import OrderBook, PriceLevel
from core.gamma_client import gamma_client
from core.timescale_db import timescale_db

log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
STARTUP_GRACE_SECONDS = 45.0          # startup grace before first backfill pass
DAILY_INTERVAL_SECONDS = 86400.0      # 24 h cycle
PAGE_SIZE = 100                       # markets per Gamma API page
MAX_PAGES = 25                        # safety cap on pagination depth (≤ 2 500 markets)
MIN_LABELS_FOR_RETRAIN = 50           # threshold for triggering a model retrain


class LabelBackfillEngine:
    """Resolved-market label backfill + threshold-triggered retrain service."""

    def __init__(self) -> None:
        self._running: bool = False
        self._task: asyncio.Task | None = None
        # ── Cycle / lifetime telemetry ──
        self._last_run_at: float = 0.0
        self._last_added: int = 0
        self._last_skipped: int = 0
        self._last_retrain_triggered: bool = False
        self._total_added: int = 0
        self._cycles_completed: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background backfill task (idempotent)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="label-backfill")
        log.info(
            "[label_backfill] Started — startup_grace=%.0fs, daily_interval=%.0fs, "
            "page_size=%d, max_pages=%d, retrain_threshold=%d labels",
            STARTUP_GRACE_SECONDS, DAILY_INTERVAL_SECONDS,
            PAGE_SIZE, MAX_PAGES, MIN_LABELS_FOR_RETRAIN,
        )

    async def stop(self) -> None:
        """Cancel the background backfill task and await clean shutdown."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # defensive: never raise from stop()
                log.debug("[label_backfill] Task teardown raised: %s", e)
            self._task = None

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Startup grace → backfill pass → optional retrain → daily loop."""
        # ── 45 s startup grace so DB pool / gamma_client / ml_model are ready ──
        log.info(
            "[label_backfill] Waiting %.0fs startup grace before first backfill pass…",
            STARTUP_GRACE_SECONDS,
        )
        try:
            await asyncio.sleep(STARTUP_GRACE_SECONDS)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                added, skipped = await self.run_backfill_once()
                self._cycles_completed += 1
                log.info(
                    "[label_backfill] Cycle #%d complete: added=%d, skipped=%d, "
                    "total_added=%d",
                    self._cycles_completed, added, skipped, self._total_added,
                )

                # ── Trigger retrain only when this cycle wrote new labels ──
                if added > 0:
                    self._last_retrain_triggered = await self._maybe_trigger_retrain()
                else:
                    self._last_retrain_triggered = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let an unexpected backfill error tear down the loop.
                log.error("[label_backfill] Backfill cycle failed: %s", e, exc_info=True)

            try:
                await asyncio.sleep(DAILY_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    # ── Backfill pass ─────────────────────────────────────────────────────────

    async def run_backfill_once(self) -> tuple[int, int]:
        """Page through resolved markets, extract features, persist labeled rows.

        Returns ``(added, skipped)`` counts. Safe to call directly (e.g. from
        a CLI or test) — it does not depend on the background loop running.
        """
        added = 0
        skipped = 0

        # Late import so this module is safe to import even before ml.* is ready
        # (mirrors the pattern in timescale_db.fetch_training_samples).
        try:
            from ml.features import N_FEATURES, extract_features
        except Exception as e:
            log.warning("[label_backfill] ml.features unavailable — skipping cycle: %s", e)
            return 0, 0

        offset = 0
        for page_idx in range(MAX_PAGES):
            if not self._running:
                # Co-operative cancellation between pages.
                break

            try:
                markets = await gamma_client.get_markets(
                    active=False, closed=True,
                    limit=PAGE_SIZE, offset=offset,
                    order="updatedAt", ascending=False,
                )
            except Exception as e:
                log.warning(
                    "[label_backfill] Gamma fetch failed at offset=%d (page %d): %s",
                    offset, page_idx, e,
                )
                break

            if not markets:
                log.info("[label_backfill] No more resolved markets at offset=%d", offset)
                break

            for mkt in markets:
                try:
                    n_added, n_skipped = await self._process_market(
                        mkt, extract_features, N_FEATURES,
                    )
                    added += n_added
                    skipped += n_skipped
                except Exception as e:
                    log.debug("[label_backfill] Market processing error: %s", e)
                    skipped += 1

            offset += len(markets)
            if len(markets) < PAGE_SIZE:
                break

        self._last_run_at = time.time()
        self._last_added = added
        self._last_skipped = skipped
        self._total_added += added
        return added, skipped

    # ── Per-market processing ─────────────────────────────────────────────────

    async def _process_market(
        self,
        market: dict,
        extract_fn: Any,
        n_features: int,
    ) -> tuple[int, int]:
        """Resolve YES/NO outcomes, build features, persist labeled rows.

        Returns ``(added, skipped)`` for this market (one market → up to 2 tokens).
        """
        token_ids = gamma_client.extract_token_ids(market)
        if not token_ids:
            return 0, 1

        yes_token = token_ids[0]
        no_token = token_ids[1] if len(token_ids) > 1 else None

        resolved_yes = self._resolve_outcome(market)
        if resolved_yes is None:
            # Market has no parseable outcomePrices — skip (can't derive label).
            return 0, 1

        added = 0
        skipped = 0

        # ── YES token (label = resolved_yes) ──
        n_a, n_s = await self._persist_token_label(
            market, yes_token, resolved_yes=bool(resolved_yes),
            extract_fn=extract_fn, n_features=n_features,
        )
        added += n_a
        skipped += n_s

        # ── NO token (label = NOT resolved_yes) ──
        if no_token:
            n_a, n_s = await self._persist_token_label(
                market, no_token, resolved_yes=not bool(resolved_yes),
                extract_fn=extract_fn, n_features=n_features,
            )
            added += n_a
            skipped += n_s

        return added, skipped

    async def _persist_token_label(
        self,
        market: dict,
        token_id: str,
        resolved_yes: bool,
        extract_fn: Any,
        n_features: int,
    ) -> tuple[int, int]:
        """Build a synthetic book, extract features, write (features, label) row.

        Idempotent: skips if ``token_id`` already has any labeled sample.
        """
        # ── Idempotency gate: never re-label a token across cycles ──
        if timescale_db.has_labeled_sample(token_id):
            return 0, 1

        # ── Build synthetic order book from market metadata ──
        book = self._build_synthetic_book(market, token_id)
        if book is None:
            return 0, 1

        # ── Extract 38-dim feature vector ──
        features = extract_fn(market, book)
        if features is None:
            return 0, 1

        # ── Pad/trim to N_FEATURES for schema safety (legacy vector compat) ──
        features = np.asarray(features, dtype=np.float32)
        if len(features) < n_features:
            features = np.pad(features, (0, n_features - len(features)))
        elif len(features) > n_features:
            features = features[:n_features]

        # ── Write (features, resolved_label) into SQLite ml_feature_store ──
        outcome = 1 if resolved_yes else 0
        # Use mid_price (feature[0]) as a placeholder prior + derive confidence
        # from |mid - 0.5|. The label is the only field that actually matters for
        # training; p_pred/confidence are kept consistent with the schema.
        mid_price = float(features[0]) if len(features) > 0 else 0.5
        try:
            ok = await timescale_db.record_feature_vector(
                token_id=token_id,
                features=features,
                p_pred=mid_price,
                confidence=abs(mid_price - 0.5) * 2.0,
                outcome_resolved=outcome,
            )
        except Exception as e:
            log.debug(
                "[label_backfill] record_feature_vector failed for %s: %s",
                token_id, e,
            )
            return 0, 1

        if ok:
            return 1, 0
        return 0, 1

    # ── Outcome resolution (mirrors core/settlement.py logic) ──────────────────

    @staticmethod
    def _resolve_outcome(market: dict) -> bool | None:
        """Parse ``outcomePrices`` to determine if the YES outcome won.

        Returns True if YES won, False if NO won, None if unresolvable.
        Mirrors the convention used by ``core/settlement.py`` (YES price ≥ 0.9).
        """
        outcome_prices = market.get("outcomePrices")
        if not outcome_prices:
            return None

        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
            except Exception:
                return None
        else:
            prices = outcome_prices

        if not prices or len(prices) < 2:
            return None

        try:
            p0 = float(prices[0])
        except Exception:
            return None

        return p0 >= 0.9

    # ── Synthetic order book construction ─────────────────────────────────────

    @staticmethod
    def _build_synthetic_book(market: dict, token_id: str) -> OrderBook | None:
        """Synthesize a 5-level order book from Gamma market metadata.

        Resolved markets no longer have a live CLOB book, so we reconstruct a
        plausible 5-level book from metadata so ``ml.features.extract_features``
        can produce a usable feature vector. The mid is clipped into
        ``[0.02, 0.98]`` to avoid extract_features() rejecting it as a
        resolution-convergence edge case.
        """
        # 1. Derive mid from outcomePrices (YES probability).
        yes_price: float | None = None
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            if isinstance(outcome_prices, str):
                try:
                    prices = json.loads(outcome_prices)
                except Exception:
                    prices = []
            else:
                prices = outcome_prices
            if prices:
                try:
                    yes_price = float(prices[0])
                except Exception:
                    pass

        # 2. Fall back to lastTradePrice if outcomePrices missing.
        if yes_price is None:
            last_price = market.get("lastTradePrice")
            if last_price is not None:
                try:
                    yes_price = float(last_price)
                except Exception:
                    pass
        if yes_price is None:
            return None

        # Clip mid into valid range so extract_features() doesn't reject it.
        mid = float(np.clip(yes_price, 0.02, 0.98))

        # 3. Spread is inversely related to market liquidity (more liquidity → tighter).
        liquidity = float(
            market.get("liquidity") or market.get("liquidityNum") or 0.0
        )
        if liquidity > 100_000.0:
            spread = 0.005
        elif liquidity > 10_000.0:
            spread = 0.010
        elif liquidity > 1_000.0:
            spread = 0.020
        else:
            spread = 0.040

        best_bid = max(mid - spread / 2.0, 0.01)
        best_ask = min(mid + spread / 2.0, 0.99)

        # 4. Depth sized from volume24hr (a proxy for resting depth on the CLOB).
        vol_24h = float(market.get("volume24hr") or 0.0)
        base_size = float(np.clip(vol_24h / 1000.0, 50.0, 500.0))

        # 5. Build 5-level book with decaying depth.
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for i in range(5):
            bid_p = max(best_bid - i * spread * 0.5, 0.01)
            ask_p = min(best_ask + i * spread * 0.5, 0.99)
            size = max(base_size * (1.0 - i * 0.15), 10.0)
            bids.append(PriceLevel(price=bid_p, size=size))
            asks.append(PriceLevel(price=ask_p, size=size))

        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    # ── Retrain trigger ────────────────────────────────────────────────────────

    async def _maybe_trigger_retrain(self) -> bool:
        """Trigger a model retrain if ≥ ``MIN_LABELS_FOR_RETRAIN`` labeled rows
        exist in the SQLite feature store.

        The actual training is delegated to ``ml_model.fit_initial()`` which
        blends real DB samples with synthetic data via
        ``timescale_db.fetch_training_samples()``. Retraining runs off the
        event loop (``asyncio.to_thread``) so it cannot block the bot.
        """
        try:
            # Re-uses the pre-existing ``fetch_labeled_feature_vectors`` API
            # (returns ``list[tuple[np.ndarray, int]]``) — also consumed by
            # ``EnsembleMetaLearner.warm_from_labeled_samples()``. We only need
            # the count here; the actual training data fetch is delegated to
            # ``ml_model.fit_initial()`` which calls ``fetch_training_samples``.
            samples = timescale_db.fetch_labeled_feature_vectors(limit=10_000)
        except Exception as e:
            log.warning("[label_backfill] Failed to fetch labeled samples: %s", e)
            return False

        n_labeled = len(samples)
        if n_labeled < MIN_LABELS_FOR_RETRAIN:
            log.info(
                "[label_backfill] %d labeled samples (< %d threshold) — retrain deferred",
                n_labeled, MIN_LABELS_FOR_RETRAIN,
            )
            return False

        log.info(
            "[label_backfill] %d labeled samples ≥ %d — triggering model retrain",
            n_labeled, MIN_LABELS_FOR_RETRAIN,
        )

        try:
            # Late import: avoids importing the entire ML stack at module load.
            from ml.model import ml_model

            # fit_initial() reads timescale_db.fetch_training_samples() internally,
            # which will include the freshly backfilled labeled rows.
            await asyncio.to_thread(ml_model.fit_initial)
            await asyncio.to_thread(ml_model.save)

            log.info(
                "[label_backfill] ✅ Model retrained: source=%s, real=%d, synth=%d, "
                "brier=%.4f, auc=%.4f, ece=%.4f",
                ml_model.training_source,
                ml_model.n_real_samples,
                ml_model.n_synthetic_samples,
                ml_model.brier_score,
                ml_model.roc_auc,
                ml_model.ece,
            )
            return True
        except Exception as e:
            log.error("[label_backfill] Retrain trigger failed: %s", e, exc_info=True)
            return False

    # ── Stats / observability ──────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Return a snapshot of the engine's lifetime telemetry."""
        return {
            "running": self._running,
            "cycles_completed": self._cycles_completed,
            "total_added": self._total_added,
            "last_added": self._last_added,
            "last_skipped": self._last_skipped,
            "last_run_at": self._last_run_at,
            "last_retrain_triggered": self._last_retrain_triggered,
            "startup_grace_seconds": STARTUP_GRACE_SECONDS,
            "daily_interval_seconds": DAILY_INTERVAL_SECONDS,
            "page_size": PAGE_SIZE,
            "max_pages": MAX_PAGES,
            "min_labels_for_retrain": MIN_LABELS_FOR_RETRAIN,
        }


# Module-level singleton (matches the convention used by settlement_engine,
# training_orchestrator, book_poller, etc.)
label_backfill_engine = LabelBackfillEngine()
