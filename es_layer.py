"""
Reasoning Layer - Elasticsearch Memory Layer
---------------------------------------------
Four-store memory architecture:

  Episodic   — routing events and specific interactions (the raw events)
  Semantic   — extracted facts, preferences, relationships (durable knowledge)
  Procedural — skills, routines, patterns (what to do and how)
  Epigenomic — marks ON all three stores; controls what gets expressed vs. suppressed

Epigenomic fields (present on every document in every index):

  tier:           active | warm | backlog
                  active  → surfaces in normal recall
                  warm    → accessible when queried directly, penalized in passive routing
                  backlog → suppressed ("haven't played the viola in 15 years")

  phosphorylated: bool — pending NREM consolidation pass
                  Set when the compressor is about to truncate unseen content.
                  Cleared by the consolidation pass after extraction.

  decay_rate:     float override (null = use type default)
                  Episodic decays fastest (~35-day half-life).
                  Semantic decays slowest (~350-day). Procedural in between.

Rich-gets-richer: every access via activate_kline() increments activation_count
and resets the decay clock. Unused memories decay toward warm → backlog naturally.

Setup:
    docker compose up -d elasticsearch
    ES_HOST=http://localhost:9200 python3 es_layer.py

Elastic Cloud:
    ES_HOST=https://<deployment>.<region>.gcp.cloud.es.io:443
    ES_API_KEY=your-key
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

ES_API_KEY = os.environ.get("ES_API_KEY")
EMBED_MODEL = "all-MiniLM-L6-v2"

EPISODIC_INDEX   = "memory-episodic"
SEMANTIC_INDEX   = "memory-semantic"
PROCEDURAL_INDEX = "memory-procedural"

INDEX_NAME = EPISODIC_INDEX  # backward compat alias

# Decay lambda by memory type (λ in e^(-λ × days))
DECAY_LAMBDA = {
    "episodic":   0.02,   # ~35-day half-life
    "semantic":   0.002,  # ~350-day half-life — facts outlast specific events
    "procedural": 0.005,  # ~140-day half-life — skills decay without practice
}

# Tier suppression multipliers applied to recall score
TIER_MULTIPLIERS = {
    "active":  1.0,
    "warm":    0.5,
    "backlog": 0.05,  # still findable if explicitly queried, won't win passive routing
}

# Activation threshold: below this score, promote to warm; below warm_threshold, backlog
WARM_THRESHOLD    = 0.3
BACKLOG_THRESHOLD = 0.1


# ---------------------------------------------------------------------------
# Shared field blocks — mixed into every index mapping
# ---------------------------------------------------------------------------

_EPIGENOMIC_FIELDS = {
    "memory_type":    {"type": "keyword"},  # episodic | semantic | procedural
    "tier":           {"type": "keyword"},  # active | warm | backlog
    "phosphorylated": {"type": "boolean"},  # pending NREM consolidation pass
    "decay_rate":     {"type": "float"},    # per-memory override; null = use type default
}

_KLINE_FIELDS = {
    "activation_count":  {"type": "integer"},
    "last_activated_ts": {"type": "date", "format": "epoch_millis"},
}


# ---------------------------------------------------------------------------
# Index mappings
# ---------------------------------------------------------------------------

EPISODIC_MAPPING = {
    "mappings": {
        "properties": {
            "event_id":          {"type": "integer"},
            "ts":                {"type": "date", "format": "epoch_millis"},
            "call_site":         {"type": "keyword"},
            "task_type":         {"type": "keyword"},
            "model_selected":    {"type": "keyword"},
            "routing_reason":    {"type": "text"},
            "cost_usd":          {"type": "float"},
            "latency_ms":        {"type": "integer"},
            "quality_score":     {"type": "float"},
            "quality_ok":        {"type": "boolean"},
            "request_text":      {"type": "text"},
            "request_embedding": {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine",
            },
            **_KLINE_FIELDS,
            **_EPIGENOMIC_FIELDS,
        }
    }
}

SEMANTIC_MAPPING = {
    "mappings": {
        "properties": {
            "ts":               {"type": "date", "format": "epoch_millis"},
            "subject":          {"type": "keyword"},   # who/what this fact is about
            "fact_text":        {"type": "text"},
            "fact_embedding":   {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine",
            },
            "confidence":       {"type": "float"},     # 0-1
            "source_event_ids": {"type": "keyword"},   # episodic events this was distilled from
            **_KLINE_FIELDS,
            **_EPIGENOMIC_FIELDS,
        }
    }
}

PROCEDURAL_MAPPING = {
    "mappings": {
        "properties": {
            "ts":                {"type": "date", "format": "epoch_millis"},
            "skill_name":        {"type": "keyword"},
            "skill_description": {"type": "text"},
            "skill_embedding":   {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine",
            },
            "proficiency_score": {"type": "float"},    # 0-1, increases with use
            "last_practiced_ts": {"type": "date", "format": "epoch_millis"},
            "source_event_ids":  {"type": "keyword"},
            **_KLINE_FIELDS,
            **_EPIGENOMIC_FIELDS,
        }
    }
}


# ---------------------------------------------------------------------------
# ESMemoryLayer
# ---------------------------------------------------------------------------

class ESMemoryLayer:
    """
    Unified memory layer over three ES indices (episodic, semantic, procedural).
    Epigenomic marks (tier, phosphorylated, decay_rate) live on every document.

    Backward-compatible with the old ESMemoryNode API:
      es.index_event(...)       → episodic write
      es.find_similar(...)      → episodic read (tier-aware)
      es.update_feedback(...)   → episodic patch
      es.activate_kline(...)    → episodic activation

    New API:
      es.phosphorylate(id)                     → mark for NREM consolidation
      es.get_phosphorylated()                  → fetch pending consolidation queue
      es.clear_phosphorylation(id)             → mark consolidated
      es.set_tier(id, tier)                    → promote/demote a memory
      es.store_semantic(fact, subject, ...)    → write to semantic store
      es.recall_semantic(query)                → read from semantic store
      es.store_procedural(name, desc, ...)     → write to procedural store
      es.recall_procedural(query)              → read from procedural store
      es.recall_all(query)                     → search all three stores, merged
    """

    def __init__(self):
        es_host = os.environ.get("ES_HOST")
        if not es_host:
            raise EnvironmentError(
                "Set ES_HOST environment variable.\n"
                "  Local: ES_HOST=http://localhost:9200\n"
                "  Elastic Cloud: ES_HOST=https://<deployment>.<region>.gcp.cloud.es.io:443"
            )
        kwargs = {"hosts": [es_host]}
        if ES_API_KEY:
            kwargs["api_key"] = ES_API_KEY
        self.client = Elasticsearch(**kwargs)
        self.encoder = SentenceTransformer(EMBED_MODEL)
        self._ensure_indices()

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_indices(self):
        for index, mapping in [
            (EPISODIC_INDEX,   EPISODIC_MAPPING),
            (SEMANTIC_INDEX,   SEMANTIC_MAPPING),
            (PROCEDURAL_INDEX, PROCEDURAL_MAPPING),
        ]:
            if not self.client.indices.exists(index=index):
                self.client.indices.create(index=index, body=mapping)
                print(f"Created index: {index}")
            else:
                # Safe: ES only errors on type changes, not additions
                self.client.indices.put_mapping(
                    index=index,
                    body={"properties": {**_KLINE_FIELDS, **_EPIGENOMIC_FIELDS}},
                )

    def _embed(self, text: str) -> list[float]:
        return self.encoder.encode(text, normalize_embeddings=True).tolist()

    # ------------------------------------------------------------------
    # K-line scoring (shared across all stores)
    # ------------------------------------------------------------------

    def _kline_score(self, similarity: float, source: dict) -> float:
        """
        composite = similarity × activation_weight × recency_weight × tier_multiplier

        activation_weight = 1 + log(1 + count)   rich-gets-richer
        recency_weight    = e^(-λ × days)          exponential decay, rate by type
        tier_multiplier   = TIER_MULTIPLIERS[tier]  epigenomic suppression
        """
        memory_type = source.get("memory_type", "episodic")
        lam = source.get("decay_rate") or DECAY_LAMBDA.get(memory_type, 0.02)

        activation_count = source.get("activation_count") or 0
        last_ts = source.get("last_activated_ts") or source.get("ts") or int(time.time() * 1000)
        days_since = max(0.0, (time.time() * 1000 - last_ts) / (1000 * 86400))

        tier = source.get("tier", "active")
        activation_weight = 1.0 + math.log1p(activation_count)
        recency_weight    = math.exp(-lam * days_since)
        tier_mult         = TIER_MULTIPLIERS.get(tier, 1.0)

        return similarity * activation_weight * recency_weight * tier_mult

    # ------------------------------------------------------------------
    # Episodic store — write path
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
        """Index a routing event into the episodic store."""
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
            # Epigenomic defaults
            "memory_type":       "episodic",
            "tier":              "active",
            "phosphorylated":    False,
        }
        self.client.index(index=EPISODIC_INDEX, id=str(event_id), document=doc)

    def update_feedback(self, event_id: str, quality_ok: bool, quality_score: float):
        """Patch an episodic event with explicit user feedback."""
        self.client.update(
            index=EPISODIC_INDEX,
            id=str(event_id),
            body={"doc": {"quality_ok": quality_ok, "quality_score": quality_score}},
        )

    def activate_kline(self, event_id: str):
        """
        Record a successful k-line activation. Increments activation_count and
        refreshes the decay clock — rich-gets-richer.
        """
        self.client.update(
            index=EPISODIC_INDEX,
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
    # Episodic store — read path
    # ------------------------------------------------------------------

    def find_similar(
        self,
        request_text: str,
        top_k: int = 5,
        min_quality: float = None,
        task_type: str = None,
        include_backlog: bool = False,
    ) -> list[dict]:
        """
        Find past episodic events similar to this request, re-ranked by composite
        k-line score. Backlog items suppressed by default (epigenomic gating).
        """
        embedding = self._embed(request_text)
        fetch_k = top_k * 3

        filters = []
        if min_quality is not None:
            filters.append({"range": {"quality_score": {"gte": min_quality}}})
        if task_type:
            filters.append({"term": {"task_type": task_type}})
        if not include_backlog:
            filters.append({"terms": {"tier": ["active", "warm"]}})

        knn = {
            "field": "request_embedding",
            "query_vector": embedding,
            "k": fetch_k,
            "num_candidates": fetch_k * 5,
        }
        if filters:
            knn["filter"] = {"bool": {"must": filters}}

        resp = self.client.search(
            index=EPISODIC_INDEX,
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
        Ask episodic memory: what model worked best for requests like this?
        Weighted by kline_score so high-activation, recently-used precedents dominate.
        """
        similar = self.find_similar(request_text, top_k=10)
        good = [e for e in similar if (e.get("quality_score") or 0.5) >= 0.7]
        if not good:
            return None
        weights: dict[str, float] = {}
        for e in good:
            m = e.get("model_selected")
            if m:
                weights[m] = weights.get(m, 0.0) + e.get("kline_score", 1.0)
        return max(weights, key=lambda m: weights[m]) if weights else None

    # ------------------------------------------------------------------
    # Epigenomic operations (work across all indices)
    # ------------------------------------------------------------------

    def phosphorylate(self, event_id: str, index: str = EPISODIC_INDEX):
        """
        Mark a memory as pending NREM consolidation.
        Called by the compressor before truncating content that hasn't been
        consolidated yet — ensures semantic/procedural value is extracted before
        the episodic details are discarded.
        """
        self.client.update(
            index=index,
            id=str(event_id),
            body={"doc": {"phosphorylated": True}},
        )

    def clear_phosphorylation(self, event_id: str, index: str = EPISODIC_INDEX):
        """Mark a memory as consolidated — clear the pending flag."""
        self.client.update(
            index=index,
            id=str(event_id),
            body={"doc": {"phosphorylated": False}},
        )

    def get_phosphorylated(self, limit: int = 50, index: str = EPISODIC_INDEX) -> list[dict]:
        """
        Return memories pending consolidation, oldest first.
        This is the input queue for the NREM consolidation pass.
        """
        resp = self.client.search(
            index=index,
            body={
                "query": {"term": {"phosphorylated": True}},
                "sort":  [{"ts": "asc"}],
                "size":  limit,
                "_source": {"excludes": ["request_embedding", "fact_embedding", "skill_embedding"]},
            },
        )
        return [h["_source"] | {"_id": h["_id"]} for h in resp["hits"]["hits"]]

    def set_tier(self, event_id: str, tier: str, index: str = EPISODIC_INDEX):
        """
        Promote or demote a memory between active / warm / backlog.
        Backlog = epigenomic suppression. Still accessible if explicitly queried.
        """
        if tier not in TIER_MULTIPLIERS:
            raise ValueError(f"tier must be one of {list(TIER_MULTIPLIERS)}")
        self.client.update(
            index=index,
            id=str(event_id),
            body={"doc": {"tier": tier}},
        )

    def decay_tiers(self) -> dict:
        """
        Demote memories that haven't been activated within type-specific thresholds.

        Thresholds (days without activation before demotion):
          Episodic:   active→warm 30d,  warm→backlog 60d
          Procedural: active→warm 90d,  warm→backlog 180d
          Semantic:   active→warm 180d, warm→backlog 365d

        Returns counts of demoted documents per transition.
        """
        now_ms = int(time.time() * 1000)
        DAY_MS = 86_400_000

        THRESHOLDS = {
            EPISODIC_INDEX:   {"active": 30  * DAY_MS, "warm": 60  * DAY_MS},
            PROCEDURAL_INDEX: {"active": 90  * DAY_MS, "warm": 180 * DAY_MS},
            SEMANTIC_INDEX:   {"active": 180 * DAY_MS, "warm": 365 * DAY_MS},
        }

        demoted = {"active_to_warm": 0, "warm_to_backlog": 0}

        for index, thresholds in THRESHOLDS.items():
            for from_tier, to_tier, threshold_ms in [
                ("active", "warm",    thresholds["active"]),
                ("warm",   "backlog", thresholds["warm"]),
            ]:
                cutoff_ms = now_ms - threshold_ms
                try:
                    resp = self.client.update_by_query(
                        index=index,
                        body={
                            "query": {
                                "bool": {
                                    "must": [
                                        {"term": {"tier": from_tier}},
                                        {"range": {"last_activated_ts": {"lt": cutoff_ms}}},
                                    ]
                                }
                            },
                            "script": {
                                "source": f"ctx._source.tier = '{to_tier}'",
                            },
                        },
                    )
                    count = resp.get("updated", 0)
                    key = f"{from_tier}_to_{to_tier}"
                    demoted[key] = demoted.get(key, 0) + count
                except Exception:
                    pass  # index may not exist yet on a fresh install

        return demoted

    # ------------------------------------------------------------------
    # Semantic store
    # ------------------------------------------------------------------

    def store_semantic(
        self,
        fact_text: str,
        subject: str = None,
        confidence: float = 1.0,
        source_event_ids: list = None,
        ts: int = None,
    ) -> str:
        """
        Store a durable fact extracted from episodic experience.
        Returns the ES document ID.

        Example:
            es.store_semantic(
                "Sarah does not like open-toed shoes in public spaces",
                subject="Sarah",
                confidence=0.95,
                source_event_ids=["seaworld-2008-episode"],
            )
        """
        now = ts or int(time.time() * 1000)
        doc = {
            "ts":               now,
            "subject":          subject,
            "fact_text":        fact_text,
            "fact_embedding":   self._embed(fact_text),
            "confidence":       confidence,
            "source_event_ids": source_event_ids or [],
            "activation_count": 0,
            "last_activated_ts": now,
            "memory_type":      "semantic",
            "tier":             "active",
            "phosphorylated":   False,
        }
        resp = self.client.index(index=SEMANTIC_INDEX, document=doc)
        return resp["_id"]

    def recall_semantic(
        self,
        query: str,
        top_k: int = 5,
        subject: str = None,
        include_backlog: bool = False,
    ) -> list[dict]:
        """Retrieve facts semantically similar to query, epigenomically gated."""
        embedding = self._embed(query)
        fetch_k = top_k * 3

        filters = []
        if subject:
            filters.append({"term": {"subject": subject}})
        if not include_backlog:
            filters.append({"terms": {"tier": ["active", "warm"]}})

        knn = {
            "field": "fact_embedding",
            "query_vector": embedding,
            "k": fetch_k,
            "num_candidates": fetch_k * 5,
        }
        if filters:
            knn["filter"] = {"bool": {"must": filters}}

        resp = self.client.search(
            index=SEMANTIC_INDEX,
            body={"knn": knn, "_source": {"excludes": ["fact_embedding"]}},
        )
        hits = resp["hits"]["hits"]
        scored = [(h, self._kline_score(h["_score"], h["_source"])) for h in hits]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {**h["_source"], "similarity_score": h["_score"], "kline_score": ks}
            for h, ks in scored[:top_k]
        ]

    # ------------------------------------------------------------------
    # Procedural store
    # ------------------------------------------------------------------

    def store_procedural(
        self,
        skill_name: str,
        skill_description: str,
        proficiency_score: float = 1.0,
        source_event_ids: list = None,
        ts: int = None,
    ) -> str:
        """
        Store a skill or routine extracted from episodic experience.
        Returns the ES document ID.

        Example:
            es.store_procedural(
                "no-open-toed-shoes",
                "Always wear closed-toe shoes in public spaces like theme parks",
                proficiency_score=1.0,
                source_event_ids=["seaworld-2008-episode"],
            )
        """
        now = ts or int(time.time() * 1000)
        doc = {
            "ts":                now,
            "skill_name":        skill_name,
            "skill_description": skill_description,
            "skill_embedding":   self._embed(skill_description),
            "proficiency_score": proficiency_score,
            "last_practiced_ts": now,
            "source_event_ids":  source_event_ids or [],
            "activation_count":  0,
            "last_activated_ts": now,
            "memory_type":       "procedural",
            "tier":              "active",
            "phosphorylated":    False,
        }
        resp = self.client.index(index=PROCEDURAL_INDEX, document=doc)
        return resp["_id"]

    def recall_procedural(
        self,
        query: str,
        top_k: int = 5,
        include_backlog: bool = False,
    ) -> list[dict]:
        """Retrieve skills relevant to this query, epigenomically gated."""
        embedding = self._embed(query)
        fetch_k = top_k * 3

        filters = []
        if not include_backlog:
            filters.append({"terms": {"tier": ["active", "warm"]}})

        knn = {
            "field": "skill_embedding",
            "query_vector": embedding,
            "k": fetch_k,
            "num_candidates": fetch_k * 5,
        }
        if filters:
            knn["filter"] = {"bool": {"must": filters}}

        resp = self.client.search(
            index=PROCEDURAL_INDEX,
            body={"knn": knn, "_source": {"excludes": ["skill_embedding"]}},
        )
        hits = resp["hits"]["hits"]
        scored = [(h, self._kline_score(h["_score"], h["_source"])) for h in hits]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {**h["_source"], "similarity_score": h["_score"], "kline_score": ks}
            for h, ks in scored[:top_k]
        ]

    def practice_skill(self, skill_id: str):
        """Record skill use — increments activation and resets decay clock."""
        self.client.update(
            index=PROCEDURAL_INDEX,
            id=skill_id,
            body={
                "script": {
                    "source": (
                        "ctx._source.activation_count = "
                        "(ctx._source.containsKey('activation_count') ? ctx._source.activation_count : 0) + 1; "
                        "ctx._source.last_activated_ts = params.now; "
                        "ctx._source.last_practiced_ts = params.now;"
                    ),
                    "params": {"now": int(time.time() * 1000)},
                }
            },
        )

    # ------------------------------------------------------------------
    # Cross-store recall
    # ------------------------------------------------------------------

    def recall_all(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Search episodic, semantic, and procedural stores simultaneously.
        Returns merged results sorted by kline_score, with memory_type tagged.
        """
        results = []
        for fn in [self.find_similar, self.recall_semantic, self.recall_procedural]:
            try:
                results.extend(fn(query, top_k=top_k))
            except Exception:
                pass
        results.sort(key=lambda r: r.get("kline_score", 0), reverse=True)
        return results[:top_k]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        out = {}
        for index in [EPISODIC_INDEX, SEMANTIC_INDEX, PROCEDURAL_INDEX]:
            try:
                count = self.client.count(index=index)["count"]
                phospho = self.client.count(
                    index=index,
                    body={"query": {"term": {"phosphorylated": True}}}
                )["count"]
                tiers = self.client.search(index=index, body={
                    "size": 0,
                    "aggs": {"by_tier": {"terms": {"field": "tier"}}},
                })
                out[index] = {
                    "total": count,
                    "phosphorylated_pending": phospho,
                    "by_tier": {
                        b["key"]: b["doc_count"]
                        for b in tiers["aggregations"]["by_tier"]["buckets"]
                    },
                }
            except Exception as e:
                out[index] = {"error": str(e)}
        return out


# Backward compat alias — proxy.py imports ESMemoryNode
ESMemoryNode = ESMemoryLayer


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    es = ESMemoryLayer()

    print("Testing episodic store...")
    es.index_event(
        event_id=9001,
        request_text="consolidate recent memory entries and file them",
        call_site="memory_consolidation",
        task_type="memory_ops",
        model_selected="claude-haiku-4-5",
        quality_score=0.9,
        quality_ok=True,
    )
    es.index_event(
        event_id=9002,
        request_text="reason through a complex architectural tradeoff",
        call_site="main_agent",
        task_type="reasoning",
        model_selected="claude-sonnet-4-6",
        quality_score=0.95,
        quality_ok=True,
    )

    print("Testing semantic store...")
    sem_id = es.store_semantic(
        "Sarah does not wear open-toed shoes in public spaces like theme parks",
        subject="Sarah",
        confidence=0.98,
        source_event_ids=["seaworld-2008-episode"],
    )
    print(f"  Stored semantic fact: {sem_id}")

    print("Testing procedural store...")
    proc_id = es.store_procedural(
        "no-open-toed-shoes",
        "Always choose closed-toe shoes when going to public venues",
        proficiency_score=1.0,
        source_event_ids=["seaworld-2008-episode"],
    )
    print(f"  Stored procedural skill: {proc_id}")

    time.sleep(1)

    print("\nTesting phosphorylation...")
    es.phosphorylate("9001")
    pending = es.get_phosphorylated()
    print(f"  Pending consolidation: {len(pending)} events")
    es.clear_phosphorylation("9001")

    print("\nTesting tier demotion (backlog)...")
    es.set_tier("9001", "backlog")
    results = es.find_similar("consolidate memory")
    backlogged = [r for r in results if r.get("tier") == "backlog"]
    print(f"  Backlogged items in passive recall: {len(backlogged)} (should be 0)")

    print("\nTesting cross-store recall...")
    for r in es.recall_all("footwear and public spaces", top_k=3):
        print(f"  [{r.get('memory_type')}] {r.get('fact_text') or r.get('skill_name') or r.get('request_text', '')[:60]}")

    print(f"\nStats: {json.dumps(es.stats(), indent=2)}")
