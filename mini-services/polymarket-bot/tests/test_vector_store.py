"""
Unit tests for ``ml/vector_store.py``.

X7 — MarketVectorStore unit tests.

Covers the five public-surface guarantees of the embedded semantic vector
store used for prediction-market retrieval & RAG:

  1. ``add_market(token_id, market)`` stores a sparse TF-IDF embedding plus
     the canonical market metadata payload (token_id / slug / title /
     category / volume24hr / liquidity).
  2. ``search(query, top_k)`` returns relevant markets ranked by cosine
     similarity — the top result for a Bitcoin-related query is the
     Bitcoin-titled market, not an unrelated politics / sports market.
  3. ``search("")`` returns ``[]`` (empty query → no query tokens → empty
     result list, never raises).
  4. ``search(query, top_k=N)`` returns at most ``N`` results, even when
     more than N markets clear the ``sim > 0.05`` similarity threshold.
  5. ``save_to_disk()`` → ``load_from_disk()`` round-trips the full state
     (``doc_metadata`` / ``doc_vectors`` / ``idf`` / ``_doc_count``)
     byte-for-byte so a reload produces an equivalent store.

The vector-store module reads its on-disk persistence path from a
module-level ``VECTOR_STORE_PATH`` constant (itself derived from the
``VECTOR_STORE_PATH`` environment variable at module-import time). Each
test:

  * sets the ``VECTOR_STORE_PATH`` environment variable to a per-test
    ``tmp_path``-scoped JSON file under ``/tmp`` (pytest's default
    ``tmp_path`` root is ``/tmp/pytest-of-<user>/pytest-<N>/`` — so the
    env var always resolves to ``/tmp`` per the task spec);
  * monkeypatches ``ml.vector_store.VECTOR_STORE_PATH`` to the same
    path so the fresh ``MarketVectorStore()`` instance constructed in
    the test resolves it via the module global (the same code path
    production uses inside ``save_to_disk`` / ``load_from_disk``); and
  * constructs a fresh ``MarketVectorStore()`` instance — NOT the
    module-level ``vector_store`` singleton (which is constructed at
    import time and is left untouched so the rest of the test session
    sees a consistent global).

The repo's ``tests/conftest.py`` already calls
``os.environ.setdefault("VECTOR_STORE_PATH", "<tmp>/vector_index.json")``
before the first import of any project module — so the singleton is
already constructed against a writable ``/tmp`` path, never against the
production ``/app/data/vector_index.json``. These tests build on top of
that by redirecting their own per-test instance to a fresh
``tmp_path``-scoped JSON file, so each test starts from a clean slate.

All ``MarketVectorStore`` methods are synchronous; no ``pytest-asyncio``
marker is needed (unlike ``tests/test_decision_ledger.py``).
"""
from __future__ import annotations

import json

import pytest

from ml.vector_store import MarketVectorStore, _tokenize


# ── Fixture: fresh temp-file-backed store per test ──────────────────────────
@pytest.fixture
def store(monkeypatch, tmp_path):
    """Return a ``MarketVectorStore`` whose persistence file lives under
    ``tmp_path``.

    ``VECTOR_STORE_PATH`` is set as an environment variable to a JSON file
    under the pytest-managed ``tmp_path`` (which defaults to ``/tmp`` per
    the task spec), and the module-level ``ml.vector_store.VECTOR_STORE_PATH``
    constant is monkeypatched to the same path so the fresh
    ``MarketVectorStore()`` instance picks it up — exactly the code path
    production uses inside ``save_to_disk`` / ``load_from_disk``.

    A brand-new ``MarketVectorStore()`` is constructed per test (NOT the
    module-level singleton ``vector_store`` constructed at import time),
    so test isolation is total: no in-memory state leaks between tests,
    and the singleton is never perturbed.
    """
    persist_path = tmp_path / "test_vector_index.json"
    # Set the env var (task spec: "Set VECTOR_STORE_PATH env var to /tmp").
    # ``tmp_path`` defaults to ``/tmp/pytest-of-<user>/pytest-<N>/`` so the
    # resolved path is always under ``/tmp``.
    monkeypatch.setenv("VECTOR_STORE_PATH", str(persist_path))
    # Patch the module-level constant so the no-arg methods
    # ``save_to_disk()`` / ``load_from_disk()`` resolve the per-test path.
    monkeypatch.setattr("ml.vector_store.VECTOR_STORE_PATH", persist_path)
    return MarketVectorStore()


# ── 1. add_market stores market embedding ───────────────────────────────────
def test_add_market_stores_market_embedding(store):
    """``add_market`` must persist both the sparse TF-IDF vector and the
    canonical metadata payload keyed by ``token_id``.

    Covers the documented contract:
      * ``doc_vectors[token_id]`` is a non-empty ``dict[str, float]`` of
        term-frequency-normalised token weights.
      * ``doc_metadata[token_id]`` carries ``token_id`` / ``slug`` /
        ``title`` / ``category`` / ``volume24hr`` / ``liquidity`` (with
        numeric coercion of the volume / liquidity fields).
      * ``_doc_count`` is bumped to reflect the new doc.
    """
    market = {
        "groupItemTitle": "Will Bitcoin reach $100k by end of 2024?",
        "slug": "bitcoin-100k-2024",
        "category": "crypto",
        "description": "Resolves YES if BTC closes above $100,000 on any major exchange.",
        "tags": ["bitcoin", "btc", "crypto", "price-target"],
        "volume24hr": "123456.78",
        "liquidity": "9876.50",
    }
    store.add_market("tok_btc", market)

    # Embedding stored
    assert "tok_btc" in store.doc_vectors
    vec = store.doc_vectors["tok_btc"]
    assert isinstance(vec, dict)
    assert len(vec) > 0
    # All vector values are floats in (0, 1] (TF normalised by doc length).
    assert all(isinstance(v, float) and 0.0 < v <= 1.0 for v in vec.values())
    # Expected content tokens survived the tokenizer (lowercased words
    # and their bigrams — see ``_tokenize``).
    assert "bitcoin" in vec
    assert "reach" in vec
    assert "crypto" in vec
    # Bigram of "will bitcoin" survives — confirms the bigram half of
    # ``_tokenize`` actually ran.
    assert "will_bitcoin" in vec

    # Metadata stored with the canonical shape + numeric coercion
    assert "tok_btc" in store.doc_metadata
    meta = store.doc_metadata["tok_btc"]
    assert meta["token_id"] == "tok_btc"
    assert meta["slug"] == "bitcoin-100k-2024"
    assert meta["title"] == "Will Bitcoin reach $100k by end of 2024?"
    assert meta["category"] == "crypto"
    assert meta["volume24hr"] == pytest.approx(123456.78)
    assert meta["liquidity"] == pytest.approx(9876.50)

    # Document count reflects the addition
    assert store._doc_count == 1


# ── 2. search returns relevant markets ───────────────────────────────────────
def test_search_returns_relevant_markets(store):
    """``search`` must rank a semantically-related market above unrelated
    ones via cosine similarity over the TF-IDF vectors.

    Three markets are indexed: a Bitcoin price-target market, a US
    presidential-election market, and an NBA championship market. A
    Bitcoin-themed query must surface the Bitcoin market as the top
    result with a similarity score strictly above the ``0.05`` cutoff
    that ``search`` applies internally.
    """
    store.add_market(
        "tok_btc",
        {
            "groupItemTitle": "Will Bitcoin reach $100k by end of 2024?",
            "category": "crypto",
            "description": "BTC price target.",
            "tags": ["bitcoin", "btc", "crypto"],
            "volume24hr": 50000,
            "liquidity": 8000,
        },
    )
    store.add_market(
        "tok_election",
        {
            "groupItemTitle": "Will the incumbent win the 2024 US presidential election?",
            "category": "politics",
            "description": "US presidential election outcome.",
            "tags": ["election", "politics", "president"],
            "volume24hr": 200000,
            "liquidity": 15000,
        },
    )
    store.add_market(
        "tok_nba",
        {
            "groupItemTitle": "Will the Boston Celtics win the 2024 NBA championship?",
            "category": "sports",
            "description": "NBA championship winner.",
            "tags": ["nba", "basketball", "celtics"],
            "volume24hr": 75000,
            "liquidity": 12000,
        },
    )
    store.build_index()

    results = store.search("bitcoin crypto price target", top_k=3)

    # At least one result returned (the BTC market cleared the threshold)
    assert len(results) >= 1
    # Top result is the Bitcoin market
    top_meta, top_score = results[0]
    assert top_meta["token_id"] == "tok_btc"
    # Score is above the internal 0.05 cutoff and is positive
    assert top_score > 0.05
    # Results are sorted by descending score (ranked retrieval contract)
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


# ── 3. search with empty query returns empty ───────────────────────────────
def test_search_with_empty_query_returns_empty(store):
    """An empty (or token-free) query must short-circuit to ``[]``.

    ``_tokenize("")`` returns ``[]``, so ``search``'s
    ``if not q_tokens or not self.doc_vectors`` guard fires and returns
    ``[]`` without ever computing similarity scores. The same guard
    covers whitespace-only and punctuation-only queries (the tokenizer
    only keeps ``[a-z0-9_]{2,}`` tokens, so ``"!!!"`` / ``"   "`` produce
    no tokens either).
    """
    store.add_market(
        "tok_btc",
        {
            "groupItemTitle": "Will Bitcoin reach $100k?",
            "category": "crypto",
            "tags": ["bitcoin", "crypto"],
        },
    )
    store.build_index()

    # Empty string
    assert store.search("") == []
    # Whitespace-only (no tokens after tokenisation)
    assert store.search("    ") == []
    # Punctuation-only (no [a-z0-9_]{2,} tokens survive)
    assert store.search("!!! ??? ---") == []

    # Also: a populated query against an EMPTY store returns []
    empty_store = MarketVectorStore()
    empty_store.build_index()  # no-op when no docs
    assert empty_store.search("bitcoin crypto") == []


# ── 4. search respects top_k limit ───────────────────────────────────────────
def test_search_respects_top_k_limit(store):
    """``search(query, top_k=N)`` must return AT MOST ``N`` results.

    Five markets that ALL share the same distinguishing vocabulary
    ("bitcoin crypto price prediction") are indexed so every one clears
    the ``sim > 0.05`` threshold for a matching query — this guarantees
    the result pool is larger than the requested ``top_k``, so the
    truncation logic in ``return scores[:top_k]`` is actually exercised.

    Two truncation points (``top_k=2`` and ``top_k=3``) are checked to
    rule out a "always returns the same fixed cap" implementation.
    """
    # 5 markets with overlapping vocabulary so all match the query
    for i in range(5):
        store.add_market(
            f"tok_btc_{i}",
            {
                "groupItemTitle": f"Bitcoin crypto price prediction market {i}",
                "category": "crypto",
                "description": "bitcoin crypto price prediction",
                "tags": ["bitcoin", "crypto", "price", "prediction"],
                "volume24hr": 1000 * (i + 1),
                "liquidity": 100 * (i + 1),
            },
        )
    store.build_index()

    # Sanity: with no truncation, all 5 markets would match
    all_results = store.search("bitcoin crypto price prediction", top_k=10)
    assert len(all_results) == 5, (
        "fixture setup invariant: all 5 markets must clear the sim > 0.05 "
        "threshold for the truncation assertions below to be meaningful"
    )

    # top_k=2 — at most 2 returned
    results_k2 = store.search("bitcoin crypto price prediction", top_k=2)
    assert len(results_k2) <= 2
    assert len(results_k2) == 2  # exactly 2 because 5 candidates are available

    # top_k=3 — at most 3 returned
    results_k3 = store.search("bitcoin crypto price prediction", top_k=3)
    assert len(results_k3) <= 3
    assert len(results_k3) == 3  # exactly 3 because 5 candidates are available

    # Truncated results are the top-scored prefix of the untruncated list
    # (search.sort is descending, then ``scores[:top_k]`` takes the head)
    top_tokens_k2 = {meta["token_id"] for meta, _ in results_k2}
    top_tokens_k3 = {meta["token_id"] for meta, _ in results_k3}
    assert top_tokens_k2.issubset(top_tokens_k3), (
        "top_k=2 result set must be a subset of top_k=3 result set when "
        "results are sorted by descending similarity"
    )

    # top_k=0 — degenerate edge: empty result list
    assert store.search("bitcoin crypto price prediction", top_k=0) == []


# ── 5. load/save round-trips correctly ──────────────────────────────────────
def test_load_save_round_trips_correctly(store, monkeypatch, tmp_path):
    """``save_to_disk()`` → ``load_from_disk()`` must reproduce an
    equivalent store: same ``doc_metadata``, same ``doc_vectors``, same
    ``idf`` table, same ``_doc_count``.

    The on-disk JSON schema (see ``save_to_disk``) is:
      ``{"metadata": ..., "doc_vectors": ..., "idf": ..., "doc_count": ...}``
    and ``load_from_disk`` rehydrates each field back into the instance.

    The persisted path is the per-test ``tmp_path / "test_vector_index.json"``
    that the ``store`` fixture set via both the env var and the
    monkeypatched module global. A second ``MarketVectorStore()`` is
    constructed in the same scope so it picks up the same path and reads
    back the saved file — proving the round-trip.
    """
    # Populate the source store
    markets = {
        "tok_btc": {
            "groupItemTitle": "Will Bitcoin reach $100k by end of 2024?",
            "slug": "bitcoin-100k-2024",
            "category": "crypto",
            "description": "BTC price target.",
            "tags": ["bitcoin", "btc", "crypto"],
            "volume24hr": 50000,
            "liquidity": 8000,
        },
        "tok_election": {
            "groupItemTitle": "Will the incumbent win the 2024 US presidential election?",
            "slug": "us-presidential-2024",
            "category": "politics",
            "description": "US presidential election outcome.",
            "tags": ["election", "politics", "president"],
            "volume24hr": 200000,
            "liquidity": 15000,
        },
        "tok_nba": {
            "groupItemTitle": "Will the Boston Celtics win the 2024 NBA championship?",
            "slug": "nba-championship-2024",
            "category": "sports",
            "description": "NBA championship winner.",
            "tags": ["nba", "basketball", "celtics"],
            "volume24hr": 75000,
            "liquidity": 12000,
        },
    }
    for tid, market in markets.items():
        store.add_market(tid, market)
    store.build_index()

    # Persist to disk
    store.save_to_disk()

    # The JSON file actually exists on disk (not a silent no-op)
    persist_path = tmp_path / "test_vector_index.json"
    assert persist_path.exists(), "save_to_disk() must create the JSON file"
    # And it's valid JSON with the canonical schema keys
    with persist_path.open("r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert set(on_disk.keys()) == {"metadata", "doc_vectors", "idf", "doc_count"}

    # Load into a fresh instance pointed at the same persistence path
    # (env var + module global still patched by the ``store`` fixture).
    reloaded = MarketVectorStore()
    reloaded.load_from_disk()

    # Round-trip equivalence — every persisted field matches
    assert reloaded.doc_metadata == store.doc_metadata
    assert reloaded.doc_vectors == store.doc_vectors
    assert reloaded.idf == store.idf
    assert reloaded._doc_count == store._doc_count

    # Functional equivalence — the reloaded store still serves relevant
    # results (proves the IDF table rehydrated correctly, since
    # ``search`` multiplies query-term TFs by ``self.idf.get(t, 1.0)``).
    results = reloaded.search("bitcoin crypto", top_k=3)
    assert len(results) >= 1
    top_meta, top_score = results[0]
    assert top_meta["token_id"] == "tok_btc"
    assert top_score > 0.05

    # Loading from a NON-EXISTENT path is a no-op (no exception, no state
    # mutation). Validate this against a fresh path that nothing has
    # written to.
    fresh_path = tmp_path / "never_written.json"
    monkeypatch.setattr("ml.vector_store.VECTOR_STORE_PATH", fresh_path)
    pristine = MarketVectorStore()
    pristine.add_market("tok_x", {"groupItemTitle": "Bitcoin crypto price"})
    pristine.build_index()
    snapshot_meta = dict(pristine.doc_metadata)
    snapshot_vec = {k: dict(v) for k, v in pristine.doc_vectors.items()}
    snapshot_idf = dict(pristine.idf)
    snapshot_count = pristine._doc_count

    pristine.load_from_disk()  # no file → no-op

    assert pristine.doc_metadata == snapshot_meta
    assert pristine.doc_vectors == snapshot_vec
    assert pristine.idf == snapshot_idf
    assert pristine._doc_count == snapshot_count
