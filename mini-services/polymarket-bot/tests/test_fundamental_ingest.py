"""
W9-5 — Unit tests for ``core/fundamental_ingest.py``.

Covers the news-ingestion NLP + dedup + sentiment-decay public surface:

  1. ``score_text`` returns POSITIVE polarity for bullish text (words like
     "surge", "gain", "rally").
  2. ``score_text`` returns NEGATIVE polarity for bearish text ("crash",
     "drop", "halt").
  3. ``score_text`` returns 0.0 for neutral text (no bullish / bearish
     keywords present).
  4. ``score_text`` returns 0.0 for EMPTY text (no words to score).
  5. ``score_text`` returns 0.0 when bullish and bearish counts are equal
     (the (pos - neg) / total formula yields zero).
  6. ``score_text`` is case-insensitive — ``"SURGE"`` and ``"surge"`` both
     count as a bullish hit.
  7. ``ingest_news_item`` returns a ``FundamentalNewsItem`` with the supplied
     headline / source / category fields, a non-empty ``hash``, and a
     sentiment derived from ``score_text``.
  8. ``ingest_news_item`` deduplicates by SHA-256(source:headline) — the
     second call with the SAME source+headline returns ``None`` and does
     NOT append a duplicate row to ``news_feed``.
  9. ``ingest_news_item`` with a different source for the SAME headline
     is NOT deduplicated (the hash includes the source).
 10. ``get_token_sentiment`` returns 0.0 for a token with no recorded
     history (the cold-start contract).
 11. ``get_token_sentiment`` returns the input sentiment when only one
     history entry exists (no decay applies to a single data point).
 12. ``get_source_catalog`` honestly reports GDELT as NOT connected
     (``gdelt_connected=False``, ``gdelt_global_network_count=0``).
 13. ``get_news_stats`` returns the sentiment distribution (bullish /
     bearish / neutral counts) and the count of distinct sources.
 14. ``FundamentalNewsItem.to_dict`` echoes every public field and rounds
     ``sentiment`` to 3dp.

Isolation
----------
Each test operates on a FRESH ``FundamentalIngestionEngine()`` instance so
no singleton state leaks across tests. The fire-and-forget
``asyncio.create_task(timescale_db.record_news(...))`` call inside
``ingest_news_item`` writes to the conftest-redirected ``MARKET_DB_PATH``
SQLite file — it does not block the test, but we await a short sleep to
let the executor flush so test teardown doesn't leave pending tasks.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module).
"""
from __future__ import annotations

import asyncio

import pytest

from core.fundamental_ingest import (
    BEARISH_TERMS,
    BULLISH_TERMS,
    FundamentalIngestionEngine,
    FundamentalNewsItem,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def engine():
    """Fresh ``FundamentalIngestionEngine`` per test (no singleton state leak)."""
    return FundamentalIngestionEngine()


# ── 1. score_text bullish → positive polarity ───────────────────────────────
def test_score_text_bullish_returns_positive_polarity(engine):
    """A headline with only bullish keywords must yield a positive polarity
    in ``[-1.0, 1.0]`` — ``pos > 0, neg = 0`` → ``pos / (pos+neg) > 0``."""
    score = engine.score_text("Bitcoin SURGES on institutional inflows and ETF rally")
    assert score > 0.0
    # All-keyword headline → polarity == 1.0 (pos / pos = 1.0).
    assert score == pytest.approx(1.0, abs=1e-3)


# ── 2. score_text bearish → negative polarity ──────────────────────────────
def test_score_text_bearish_returns_negative_polarity(engine):
    """A headline with only bearish keywords must yield a negative polarity
    in ``[-1.0, 1.0]`` — ``neg > 0, pos = 0`` → ``-neg / (pos+neg) < 0``."""
    score = engine.score_text("Market crash warning as prices drop and recession looms")
    assert score < 0.0
    assert score == pytest.approx(-1.0, abs=1e-3)


# ── 3. score_text neutral (no keywords) → 0.0 ───────────────────────────────
def test_score_text_neutral_no_keywords_returns_zero(engine):
    """A headline with NO bullish / bearish keywords must return 0.0 —
    ``pos == neg == 0`` triggers the ``total == 0`` early return."""
    score = engine.score_text("The committee met on Tuesday to discuss the agenda.")
    assert score == 0.0


# ── 4. score_text empty → 0.0 ──────────────────────────────────────────────
def test_score_text_empty_returns_zero(engine):
    """An empty string yields no words to score → 0.0."""
    assert engine.score_text("") == 0.0
    # Whitespace-only is also empty after tokenization.
    assert engine.score_text("   ") == 0.0
    # Punctuation-only yields no word tokens.
    assert engine.score_text("!!! ... ???") == 0.0


# ── 5. score_text equal bullish/bearish → 0.0 ──────────────────────────────
def test_score_text_equal_bullish_bearish_returns_zero(engine):
    """When pos == neg (e.g. one bullish + one bearish keyword), the
    formula ``(pos - neg) / total`` yields 0.0."""
    score = engine.score_text("surge crash")  # 1 bullish + 1 bearish
    assert score == 0.0


# ── 6. score_text case-insensitive ─────────────────────────────────────────
def test_score_text_is_case_insensitive(engine):
    """``score_text`` lowercases the input before tokenization — ``"SURGE"``
    and ``"surge"`` both count as a bullish hit."""
    upper = engine.score_text("SURGE RALLY")
    lower = engine.score_text("surge rally")
    assert upper == lower
    assert upper == pytest.approx(1.0, abs=1e-3)


# ── 7. ingest_news_item returns FundamentalNewsItem with proper fields ──────
async def test_ingest_news_item_returns_item_with_proper_fields(engine):
    """``ingest_news_item`` returns a ``FundamentalNewsItem`` echoing the
    supplied headline / source / category, with a 16-char SHA-256 hash
    and a sentiment derived from ``score_text``."""
    item = await engine.ingest_news_item(
        headline="Bitcoin surges on institutional inflows",
        source="Reuters Global",
        category="Crypto",
        url="https://example.com/news/1",
    )
    assert item is not None
    assert isinstance(item, FundamentalNewsItem)
    assert item.headline == "Bitcoin surges on institutional inflows"
    assert item.source == "Reuters Global"
    assert item.category == "Crypto"
    assert item.url == "https://example.com/news/1"
    # Hash is the first 16 chars of sha256(source:headline).
    assert isinstance(item.hash, str)
    assert len(item.hash) == 16
    # Sentiment was derived from score_text — bullish headline → positive.
    assert item.sentiment > 0.0
    # The item was appended to news_feed (newest-first).
    assert engine.news_feed[0] is item
    assert engine._total_ingested == 1


# ── 8. ingest_news_item deduplicates by source+headline hash ────────────────
async def test_ingest_news_item_deduplicates_by_source_and_headline(engine):
    """A second call with the SAME source+headline returns ``None`` and does
    NOT append a duplicate row to ``news_feed`` — the SHA-256 dedup
    contract."""
    item1 = await engine.ingest_news_item(
        headline="Fed cuts rates", source="Bloomberg", category="Macro",
    )
    assert item1 is not None
    assert len(engine.news_feed) == 1

    item2 = await engine.ingest_news_item(
        headline="Fed cuts rates", source="Bloomberg", category="Macro",
    )
    # Second call must return None and NOT append a duplicate row.
    assert item2 is None
    assert len(engine.news_feed) == 1
    # ``_total_ingested`` only counts successful (dedup-passing) inserts.
    assert engine._total_ingested == 1


# ── 9. ingest_news_item different source for same headline is NOT deduped ──
async def test_ingest_news_item_different_source_not_deduped(engine):
    """The dedup hash is ``sha256(source:headline)`` — the SAME headline from
    a DIFFERENT source is a distinct hash and must NOT be deduplicated."""
    item1 = await engine.ingest_news_item(
        headline="Fed cuts rates", source="Bloomberg", category="Macro",
    )
    item2 = await engine.ingest_news_item(
        headline="Fed cuts rates", source="Reuters", category="Macro",
    )
    assert item1 is not None
    assert item2 is not None
    assert item1.hash != item2.hash
    assert len(engine.news_feed) == 2


# ── 10. get_token_sentiment returns 0.0 for unknown token ──────────────────
def test_get_token_sentiment_unknown_token_returns_zero(engine):
    """A token with no recorded sentiment history must return 0.0 — the
    cold-start contract."""
    assert engine.get_token_sentiment("UNKNOWN_TOKEN") == 0.0


# ── 11. get_token_sentiment single-entry returns that sentiment ────────────
def test_get_token_sentiment_single_entry_returns_that_sentiment(engine):
    """When only one history entry exists, the time-decay weighted average
    collapses to that entry's sentiment (the single weight dominates)."""
    import time
    now = time.time()
    # Inject a single history entry directly (skip the full ingest pipeline).
    engine._token_sentiment_history["TOK_X"] = [(now, 0.6)]
    score = engine.get_token_sentiment("TOK_X")
    assert score == pytest.approx(0.6, abs=1e-3)


# ── 12. get_source_catalog honestly reports GDELT as not connected ─────────
def test_get_source_catalog_honestly_reports_gdelt_disconnected(engine):
    """``get_source_catalog`` must report GDELT as NOT connected — the
    documented "CONFIG-ONLY entry, no sources actively indexed" contract.
    GDELT contributes ZERO to ``total_sources_supported``."""
    catalog = engine.get_source_catalog()
    # GDELT is disconnected — honest reporting.
    assert catalog["gdelt_connected"] is False
    assert catalog["gdelt_global_network_count"] == 0
    # The curated wires count equals the total supported count (no GDELT
    # contribution).
    assert catalog["curated_wires_count"] == catalog["total_sources_supported"]
    # Curated wires are tier1 (16) + tier2 (10) + tier3 (12) + tier4 (10) = 48.
    assert catalog["curated_wires_count"] == 48
    # The source_tiers dict carries the GDELT metadata entry verbatim.
    gdelt_entry = catalog["source_tiers"]["gdelt_global_network"]
    assert gdelt_entry["connected"] is False
    assert gdelt_entry["source_count_estimate"] == 0


# ── 13. get_news_stats returns sentiment distribution + distinct sources ───
async def test_get_news_stats_returns_sentiment_distribution_and_sources(engine):
    """``get_news_stats`` returns a sentiment distribution dict with
    ``bullish`` / ``bearish`` / ``neutral`` counts AND ``sources_indexed``
    (distinct source count)."""
    # Ingest 3 items: 1 bullish, 1 bearish, 1 neutral.
    await engine.ingest_news_item(
        headline="Bitcoin surges on ETF inflows", source="Bloomberg", category="Crypto",
    )
    await engine.ingest_news_item(
        headline="Market crash warning as prices drop", source="Reuters", category="Macro",
    )
    await engine.ingest_news_item(
        headline="The committee met on Tuesday", source="AP", category="Politics",
    )

    stats = engine.get_news_stats()
    assert stats["total_news_items"] == 3
    assert stats["total_ingested_lifetime"] == 3
    assert stats["sources_indexed"] == 3  # Bloomberg + Reuters + AP
    dist = stats["sentiment_distribution"]
    assert dist["bullish"] == 1
    assert dist["bearish"] == 1
    assert dist["neutral"] == 1  # 3 - 1 - 1 = 1
    assert stats["seed_items"] == 0  # no is_seed=True items ingested


# ── 14. FundamentalNewsItem.to_dict echoes fields + rounds sentiment ───────
def test_fundamental_news_item_to_dict_rounds_sentiment():
    """``to_dict`` echoes every public field and rounds ``sentiment`` to 3dp."""
    item = FundamentalNewsItem(
        headline="Test headline",
        source="Test Source",
        category="Macro",
        timestamp=1234567890.0,
        sentiment=0.123456,  # 6dp — rounds to 0.123
        related_tokens=["TOK_A", "TOK_B"],
        url="https://example.com/x",
        is_seed=True,
    )
    d = item.to_dict()
    assert d["headline"] == "Test headline"
    assert d["source"] == "Test Source"
    assert d["category"] == "Macro"
    assert d["timestamp"] == 1234567890.0
    assert d["sentiment"] == 0.123  # rounded to 3dp
    assert d["related_tokens"] == ["TOK_A", "TOK_B"]
    assert d["url"] == "https://example.com/x"
    assert d["hash"] == item.hash
    assert d["is_seed"] is True


# ── 15. ingest_news_item trims news_feed to 500 items ──────────────────────
async def test_ingest_news_item_trims_news_feed_to_500(engine):
    """After 500 items, the feed is trimmed (newest-first) — the
    ``self.news_feed = self.news_feed[:500]`` cap. This test ingests 502
    distinct items and asserts the feed length is exactly 500 with the
    NEWEST item at the front."""
    for i in range(502):
        await engine.ingest_news_item(
            headline=f"Headline {i}", source="Test", category="Macro",
        )
        # Yield to the event loop so the timescale_db fire-and-forget
        # write task can flush.
        await asyncio.sleep(0)

    assert len(engine.news_feed) == 500
    # Newest item is at index 0 (last ingested).
    assert engine.news_feed[0].headline == "Headline 501"
    # Oldest retained is "Headline 2" (Headline 0 and 1 were trimmed off).
    assert engine.news_feed[-1].headline == "Headline 2"


# ── 16. BULLISH_TERMS / BEARISH_TERMS are non-empty & disjoint ────────────
def test_bullish_and_bearish_term_sets_are_non_empty_and_disjoint():
    """The keyword lexicons must be non-empty (the entire NLP engine depends
    on them) and the two sets must be DISJOINT (a keyword cannot be both
    bullish and bearish — that would be a self-contradiction)."""
    assert len(BULLISH_TERMS) > 10  # lexicon has dozens of entries
    assert len(BEARISH_TERMS) > 10
    overlap = BULLISH_TERMS & BEARISH_TERMS
    assert overlap == set(), (
        f"BULLISH and BEARISH lexicons overlap: {overlap}"
    )
