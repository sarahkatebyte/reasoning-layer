"""
Reasoning Layer - Elasticsearch Integration
--------------------------------------------
Adds semantic memory and graph-style routing intelligence to the Reasoning Layer.

Treats past routing decisions as connected entities. Find similar past requests,
surface what worked, route smarter.

Setup (local - no account needed):
    docker compose up
    ES_HOST=http://localhost:9200 python3 es_layer.py

Setup (Elastic Cloud):
    pip install elasticsearch sentence-transformers
    Set env vars:
        ES_HOST=https://<deployment-id>.<region>.gcp.cloud.es.io:443
        ES_API_KEY=your-api-key  # optional for local dev

Usage:
    from es_layer import ESMemoryNode
    es = ESMemoryNode()
    es.index_event(event_id=1, request_text="...", task_type="memory_ops", ...)
    similar = es.find_similar("consolidate recent memories", top_k=5)
"""

import math
import os
import json
import time
from typing import Optional

from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ES_API_KEY   = os.environ.get("ES_API_KEY")
INDEX_NAME   = "reasoning-events"
EMBED_MODEL  = "all-MiniLM-L6-v2"   # small, fast, runs locally, no API needed

# K-line decay: how fast unused k-lines lose weight. λ=0.02 → ~35-day half-life.
# Tune up (faster decay) or down (longer memory) based on how stale your data gets.
DECAY_LAMBDA = 0.02


# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "event_id":       {"type": "integer"},
            "ts":             {"type": "date", "format": "epoch_millis"},
            "call_site":      {"type": "keyword"},
            "task_type":      {"type": "keyword"},
            "model_selected": {"type": "keyword"},
            "routing_reason": {"type": "text"},
            "cost_usd":       {"type": "float"},
            "latency_ms":     {"type": "integer"},
            "quality_score":  {"type": "float"},
            "quality_ok":     {"type": "boolean"},
            "request_text":   {"type": "text"},
            # Dense vector for semantic similarity search
            # 384 dims = all-MiniLM-L6-v2 output size
            "request_embedding": {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine"
            },
            # K-line reinforcement fields
            "activation_count":   {"type": "integer"},
            "last_activated_ts":  {"type": "date", "format": "epoch_millis"},
        }
    }
}


# ---------------------------------------------------------------------------
# ES Memory Node
# ---------------------------------------------------------------------------

class ESMemoryNode:
    """
    The memory node in the reasoning graph.

    Two jobs:
    1. Index every reasoning event so past decisions are searchable
    2. Find similar past requests to inform routing decisions

    This is the experience layer - the router stops being a rules engine
    and starts asking "what worked before for requests like this?"
    """

    def __init__(self):
        es_host = os.environ.get("ES_HOST")
        if not es_host:
            raise EnvironmentError(
                "Set ES_HOST environment variable.\n"
                "  Local (docker compose): ES_HOST=http://localhost:9200\n"
                "  Elastic Cloud: ES_HOST=https://<deployment-id>.<region>.gcp.cloud.es.io:443"
            )
        # API key is optional - not needed for local dev (security disabled)
        kwargs = {"hosts": [es_host]}
        if ES_API_KEY:
            kwargs["api_key"] = ES_API_KEY
        self.client = Elasticsearch(**kwargs)

        self.encoder = SentenceTransformer(EMBED_MODEL)
        self._ensure_index()

    def _ensure_index(self):
        if not self.client.indices.exists(index=INDEX_NAME):
            self.client.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
            print(f"Created index: {INDEX_NAME}")
        else:
            # Safe to call on existing indices — ES only errors if you change a field's type.
            # Adds activation_count + last_activated_ts to indices created before this change.
            kline_fields = {
                "properties": {
                    "activation_count":  {"type": "integer"},
                    "last_activated_ts": {"type": "date", "format": "epoch_millis"},
                }
            }
            self.client.indices.put_mapping(index=INDEX_NAME, body=kline_fields)

    def _embed(self, text: str) -> list[float]:
        return self.encoder.encode(text, normalize_embeddings=True).tolist()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def index_event(
        self,
        event_id: int,
        request_text: str,
        call_site: str = None,
        task_type: str = None,
        model_selected: str = None,
        routing_reason: str = None,
        cost_usd: float = None,
        latency_ms: int = None,
        quality_score: float = None,
        quality_ok: bool = None,
        ts: int = None,
    ):
        """Index a reasoning event with its embedding for future similarity search."""
        now = ts or int(time.time() * 1000)
        doc = {
            "event_id":          event_id,
            "ts":                now,
            "call_site":         call_site,
            "task_type":         task_type,
            "model_selected":    model_selected,
            "routing_reason":    routing_reason,
            "cost_usd":          cost_usd,
            "latency_ms":        latency_ms,
            "quality_score":     quality_score,
            "quality_ok":        quality_ok,
            "request_text":      request_text[:500],
            "request_embedding": self._embed(request_text),
            "activation_count":  0,
            "last_activated_ts": now,
        }
        self.client.index(index=INDEX_NAME, id=str(event_id), document=doc)

    def update_feedback(self, event_id: str, quality_ok: bool, quality_score: float):
        """Patch an existing event with explicit user feedback."""
        self.client.update(
            index=INDEX_NAME,
            id=str(event_id),
            body={"doc": {"quality_ok": quality_ok, "quality_score": quality_score}},
        )

    def activate_kline(self, event_id: str):
        """
        Call when a k-line fires successfully. Increments activation_count and
        refreshes last_activated_ts, boosting this k-line's recall probability
        for similar future requests via the composite score in find_similar().
        """
        self.client.update(
            index=INDEX_NAME,
            id=str(event_id),
            body={
                "script": {
                    "source": (
                        "ctx._source.activation_count = "
                        "(ctx._source.containsKey('activation_count') ? ctx._source.activation_count : 0) + 1; "
                        "ctx._source.last_activated_ts = params.now;"
                    ),
                    "params": {"now": int(time.time() * 1000)},
                }
            },
        )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _kline_score(self, similarity: float, source: dict) -> float:
        """
        Composite score: similarity × activation_weight × recency_weight.

        activation_weight = 1 + log(1 + activation_count)
          → new k-lines start at 1.0; repeated successful use compounds logarithmically.

        recency_weight = e^(-λ × days_since_last_use)
          → unused k-lines decay exponentially; stale configs lose relevance automatically.
        """
        activation_count = source.get("activation_count") or 0
        last_ts = source.get("last_activated_ts") or source.get("ts") or int(time.time() * 1000)
        days_since_use = max(0.0, (time.time() * 1000 - last_ts) / (1000 * 86400))

        activation_weight = 1.0 + math.log1p(activation_count)
        recency_weight = math.exp(-DECAY_LAMBDA * days_since_use)
        return similarity * activation_weight * recency_weight

    def find_similar(
        self,
        request_text: str,
        top_k: int = 5,
        min_quality: float = None,
        task_type: str = None,
    ) -> list[dict]:
        """
        Find past routing decisions similar to this request, re-ranked by
        composite k-line score (similarity × activation_weight × recency_weight).

        Fetches 3× candidates from ES so re-ranking has room to promote
        frequently-activated / recently-used k-lines above pure cosine matches.
        """
        embedding = self._embed(request_text)
        fetch_k = top_k * 3

        filters = []
        if min_quality is not None:
            filters.append({"range": {"quality_score": {"gte": min_quality}}})
        if task_type:
            filters.append({"term": {"task_type": task_type}})

        knn = {
            "field": "request_embedding",
            "query_vector": embedding,
            "k": fetch_k,
            "num_candidates": fetch_k * 5,
        }
        if filters:
            knn["filter"] = {"bool": {"must": filters}}

        resp = self.client.search(
            index=INDEX_NAME,
            body={"knn": knn, "_source": {"excludes": ["request_embedding"]}},
        )

        hits = resp["hits"]["hits"]
        scored = [(h, self._kline_score(h["_score"], h["_source"])) for h in hits]
        scored.sort(key=lambda x: x[1], reverse=True)

        return [
            {**h["_source"], "similarity_score": h["_score"], "kline_score": ks}
            for h, ks in scored[:top_k]
        ]

    def suggest_model(self, request_text: str) -> Optional[str]:
        """
        Ask the graph: what model should I use for this request?

        Returns the model from the top k-line match — already ranked by
        composite score (recency × activation × similarity), so the winner
        is the most-practiced, recently-used, semantically-relevant precedent.
        Falls back to majority vote among top-5 if the best has no quality signal.
        """
        similar = self.find_similar(request_text, top_k=10)
        good = [e for e in similar if (e.get("quality_score") or 0.5) >= 0.7]
        if not good:
            return None

        models = [e["model_selected"] for e in good if e.get("model_selected")]
        if not models:
            return None

        # Weight each vote by kline_score so high-activation results dominate
        weights: dict[str, float] = {}
        for e in good:
            m = e.get("model_selected")
            if m:
                weights[m] = weights.get(m, 0.0) + e.get("kline_score", 1.0)

        return max(weights, key=lambda m: weights[m])

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Quick stats on the index."""
        count = self.client.count(index=INDEX_NAME)["count"]
        aggs = self.client.search(index=INDEX_NAME, body={
            "size": 0,
            "aggs": {
                "by_model": {"terms": {"field": "model_selected"}},
                "by_task":  {"terms": {"field": "task_type"}},
                "avg_quality": {"avg": {"field": "quality_score"}},
            }
        })
        return {
            "total_events": count,
            "by_model": {b["key"]: b["doc_count"] for b in aggs["aggregations"]["by_model"]["buckets"]},
            "by_task":  {b["key"]: b["doc_count"] for b in aggs["aggregations"]["by_task"]["buckets"]},
            "avg_quality": aggs["aggregations"]["avg_quality"]["value"],
        }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    es = ESMemoryNode()

    # Index a few test events
    test_events = [
        {
            "event_id": 9001,
            "request_text": "consolidate recent memory entries and file them",
            "call_site": "memory_consolidation",
            "task_type": "memory_ops",
            "model_selected": "claude-haiku-4-5",
            "routing_reason": "memory_ops -> haiku tier",
            "cost_usd": 0.0004,
            "quality_score": 0.9,
            "quality_ok": True,
        },
        {
            "event_id": 9002,
            "request_text": "reason through a complex architectural tradeoff between graph databases",
            "call_site": "main_agent",
            "task_type": "reasoning_heavy",
            "model_selected": "claude-opus-4-6",
            "routing_reason": "reasoning_heavy -> opus tier",
            "cost_usd": 0.018,
            "quality_score": 0.95,
            "quality_ok": True,
        },
        {
            "event_id": 9003,
            "request_text": "retrieve the last 5 memory entries for this user",
            "call_site": "memory_retrieval",
            "task_type": "simple_retrieval",
            "model_selected": "claude-haiku-4-5",
            "routing_reason": "simple_retrieval -> haiku tier",
            "cost_usd": 0.0002,
            "quality_score": 0.85,
            "quality_ok": True,
        },
    ]

    for e in test_events:
        es.index_event(**e)
        print(f"Indexed event {e['event_id']}: {e['task_type']}")

    time.sleep(1)  # let ES index settle

    # Simulate 9001 being activated twice (memory consolidation path is well-practiced)
    es.activate_kline("9001")
    es.activate_kline("9001")

    # Test similarity search — kline_score should now rank 9001 above a raw cosine tie
    print("\nSimilar to 'file and organize memory notes':")
    for r in es.find_similar("file and organize memory notes", top_k=3):
        print(
            f"  [{r['task_type']}] {r['model_selected']} "
            f"| similarity: {r['similarity_score']:.3f} "
            f"| kline_score: {r['kline_score']:.3f} "
            f"| activations: {r.get('activation_count', 0)}"
        )

    # Test model suggestion
    suggestion = es.suggest_model("store these memory entries for later")
    print(f"\nSuggested model: {suggestion}")

    # Stats
    print(f"\nIndex stats: {json.dumps(es.stats(), indent=2)}")
