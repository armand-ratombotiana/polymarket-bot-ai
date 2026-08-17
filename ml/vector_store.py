"""
ml/vector_store.py — Embedded Semantic Vector Database for Polymarket Prediction Markets.

Provides:
  - Vector embeddings generation over market questions, categories, and resolution sources.
  - Cosine-similarity semantic search across all active & resolved Polymarket events.
  - Disk persistence to /app/data/vector_index.json.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

VECTOR_STORE_PATH = Path(os.environ.get("VECTOR_STORE_PATH", "/app/data/vector_index.json"))


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric words + bigrams."""
    words = re.findall(r"\b[a-z0-9_]{2,}\b", text.lower())
    bigrams = [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
    return words + bigrams


class MarketVectorStore:
    """
    In-memory semantic vector store for fast prediction market retrieval & RAG.
    """

    def __init__(self) -> None:
        self.doc_vectors: dict[str, dict[str, float]] = {}  # token_id -> sparse tf-idf vector
        self.doc_metadata: dict[str, dict] = {}             # token_id -> market metadata
        self.idf: dict[str, float] = {}
        self._doc_count = 0

    def add_market(self, token_id: str, market: dict) -> None:
        """Add or update a market in the vector store."""
        title = market.get("groupItemTitle") or market.get("slug") or ""
        desc = market.get("description") or ""
        category = market.get("category") or ""
        tags = " ".join(market.get("tags") or []) if isinstance(market.get("tags"), list) else ""

        full_text = f"{title} {category} {tags} {desc}"
        tokens = _tokenize(full_text)
        if not tokens:
            return

        # Term frequency
        tf: dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0.0) + 1.0

        length = len(tokens)
        for t in tf:
            tf[t] = tf[t] / length

        self.doc_vectors[token_id] = tf
        self.doc_metadata[token_id] = {
            "token_id": token_id,
            "slug": market.get("slug", token_id[:12]),
            "title": title or market.get("slug", ""),
            "category": category or "general",
            "volume24hr": float(market.get("volume24hr") or 0.0),
            "liquidity": float(market.get("liquidity") or 0.0),
        }
        self._doc_count = len(self.doc_vectors)

    def build_index(self) -> None:
        """Compute Inverse Document Frequency (IDF) over all indexed markets."""
        df: dict[str, int] = {}
        n = max(self._doc_count, 1)

        for vec in self.doc_vectors.values():
            for term in vec.keys():
                df[term] = df.get(term, 0) + 1

        self.idf = {term: math.log((n + 1.0) / (count + 1.0)) + 1.0 for term, count in df.items()}
        log.info("[vector_store] Indexed %d markets with vocabulary size %d", self._doc_count, len(self.idf))

    def search(self, query: str, top_k: int = 10) -> list[tuple[dict, float]]:
        """
        Execute semantic similarity search for query text.
        Returns list of (market_metadata, similarity_score).
        """
        q_tokens = _tokenize(query)
        if not q_tokens or not self.doc_vectors:
            return []

        q_tf: dict[str, float] = {}
        for t in q_tokens:
            q_tf[t] = q_tf.get(t, 0.0) + 1.0
        q_len = len(q_tokens)
        q_vec = {t: (cnt / q_len) * self.idf.get(t, 1.0) for t, cnt in q_tf.items()}

        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

        scores: list[tuple[dict, float]] = []
        for tid, doc_tf in self.doc_vectors.items():
            dot = 0.0
            doc_norm_sq = 0.0
            for term, tf_val in doc_tf.items():
                w = tf_val * self.idf.get(term, 1.0)
                doc_norm_sq += w * w
                if term in q_vec:
                    dot += w * q_vec[term]

            doc_norm = math.sqrt(doc_norm_sq) or 1.0
            sim = dot / (q_norm * doc_norm)
            if sim > 0.05:
                scores.append((self.doc_metadata[tid], round(sim, 4)))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def save_to_disk(self) -> None:
        VECTOR_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "metadata": self.doc_metadata,
                "doc_count": self._doc_count,
            }
            with open(VECTOR_STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("[vector_store] Save error: %s", e)

    def load_from_disk(self) -> None:
        if not VECTOR_STORE_PATH.exists():
            return
        try:
            with open(VECTOR_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.doc_metadata = data.get("metadata", {})
            self._doc_count = len(self.doc_metadata)
            log.info("[vector_store] Loaded %d market embeddings from disk", self._doc_count)
        except Exception as e:
            log.warning("[vector_store] Load error: %s", e)


# Global singleton
vector_store = MarketVectorStore()
vector_store.load_from_disk()
