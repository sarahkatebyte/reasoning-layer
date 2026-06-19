"""
The Reasoning Layer
-------------------
A platform-agnostic logger and classifier scaffold for LLM call routing.
Sits in front of any model call, records the decision, enables feedback loops.

Phase 1: Logger (instrument everything)
Phase 2: Classifier (task_type detection)
Phase 3: Router (model selection by task type)
Phase 4: Feedback loop (tune classifier from outcome data)

Backend is swappable - SQLite today, Postgres tomorrow.
"""

import sqlite3
import time
import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

DB_PATH = Path(__file__).parent / "spend.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS reasoning_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               INTEGER NOT NULL,          -- epoch ms
    request_hash     TEXT NOT NULL,             -- sha256 of input (first 64 chars)
    call_site        TEXT,                      -- e.g. "memory_consolidation", "main_agent"
    task_type        TEXT,                      -- null until classifier exists
    model_selected   TEXT,                      -- e.g. "claude-opus-4-6", "claude-haiku-4-5"
    routing_reason   TEXT,                      -- why this model was chosen
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    latency_ms       INTEGER,
    quality_score    REAL,                      -- null until scorer exists (0.0 - 1.0)
    metadata         TEXT                       -- json blob for anything else
);

CREATE TABLE IF NOT EXISTS classifier_feedback (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               INTEGER NOT NULL,
    reasoning_event_id INTEGER REFERENCES reasoning_events(id),
    predicted_type   TEXT NOT NULL,             -- what the classifier said
    actual_type      TEXT,                      -- corrected label (manual or automated)
    quality_ok       INTEGER,                   -- 1 if output quality was acceptable
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_reasoning_ts       ON reasoning_events(ts);
CREATE INDEX IF NOT EXISTS idx_reasoning_callsite ON reasoning_events(call_site);
CREATE INDEX IF NOT EXISTS idx_reasoning_model    ON reasoning_events(model_selected);
CREATE INDEX IF NOT EXISTS idx_feedback_event     ON classifier_feedback(reasoning_event_id);
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ReasoningEvent:
    call_site:      Optional[str]   = None
    task_type:      Optional[str]   = None      # reasoning_heavy | memory_ops | simple_retrieval | creative | structured_output
    model_selected: Optional[str]   = None
    routing_reason: Optional[str]   = None
    input_tokens:   Optional[int]   = None
    output_tokens:  Optional[int]   = None
    cost_usd:       Optional[float] = None
    latency_ms:     Optional[int]   = None
    quality_score:  Optional[float] = None
    request_text:   Optional[str]   = None      # used to generate hash, not stored raw
    metadata:       dict            = field(default_factory=dict)

    @property
    def request_hash(self) -> str:
        raw = (self.request_text or "")[:64]
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Backend abstraction (swap this for Postgres later)
# ---------------------------------------------------------------------------

class SQLiteBackend:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def log(self, event: ReasoningEvent) -> int:
        ts = int(time.time() * 1000)
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO reasoning_events
                    (ts, request_hash, call_site, task_type, model_selected,
                     routing_reason, input_tokens, output_tokens, cost_usd,
                     latency_ms, quality_score, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ts,
                event.request_hash,
                event.call_site,
                event.task_type,
                event.model_selected,
                event.routing_reason,
                event.input_tokens,
                event.output_tokens,
                event.cost_usd,
                event.latency_ms,
                event.quality_score,
                json.dumps(event.metadata) if event.metadata else None,
            ))
            return cursor.lastrowid

    def log_feedback(self, event_id: int, predicted: str, actual: str = None,
                     quality_ok: bool = None, notes: str = None):
        ts = int(time.time() * 1000)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO classifier_feedback
                    (ts, reasoning_event_id, predicted_type, actual_type, quality_ok, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts, event_id, predicted, actual, int(quality_ok) if quality_ok is not None else None, notes))

    def query_recent(self, limit: int = 50):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("""
                SELECT * FROM reasoning_events
                ORDER BY ts DESC LIMIT ?
            """, (limit,)).fetchall()

    def cost_by_task_type(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("""
                SELECT task_type, model_selected,
                       COUNT(*) as calls,
                       SUM(cost_usd) as total_cost,
                       AVG(cost_usd) as avg_cost,
                       AVG(quality_score) as avg_quality
                FROM reasoning_events
                WHERE cost_usd IS NOT NULL
                GROUP BY task_type, model_selected
                ORDER BY total_cost DESC
            """).fetchall()


# ---------------------------------------------------------------------------
# ReasoningLogger - the public interface
# ---------------------------------------------------------------------------

class ReasoningLogger:
    """
    Main entry point. Backend-agnostic.

    Usage:
        logger = ReasoningLogger()
        event_id = logger.log(ReasoningEvent(
            call_site="memory_consolidation",
            model_selected="claude-haiku-4-5",
            input_tokens=1200,
            output_tokens=300,
            cost_usd=0.0004,
            latency_ms=820,
            routing_reason="memory_ops task type -> haiku tier"
        ))
    """

    def __init__(self, backend=None):
        self.backend = backend or SQLiteBackend()

    def log(self, event: ReasoningEvent) -> int:
        return self.backend.log(event)

    def feedback(self, event_id: int, **kwargs):
        return self.backend.log_feedback(event_id, **kwargs)

    def recent(self, limit: int = 50):
        return self.backend.query_recent(limit)

    def cost_by_task(self):
        return self.backend.cost_by_task_type()


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger = ReasoningLogger()

    # Log a test event
    event_id = logger.log(ReasoningEvent(
        call_site="memory_consolidation",
        task_type="memory_ops",
        model_selected="claude-haiku-4-5",
        routing_reason="memory_ops -> haiku tier (low complexity)",
        input_tokens=1200,
        output_tokens=300,
        cost_usd=0.0004,
        latency_ms=820,
        request_text="consolidate recent memory entries for user sarahkate",
        metadata={"session": "test"}
    ))

    print(f"Logged event ID: {event_id}")

    # Log feedback
    logger.feedback(event_id, predicted="memory_ops", quality_ok=True)

    # Print recent
    print("\nRecent events:")
    for row in logger.recent(5):
        print(f"  [{row['call_site']}] {row['model_selected']} | ${row['cost_usd']} | {row['task_type']}")

    print("\nCost by task type:")
    for row in logger.cost_by_task():
        print(f"  {row['task_type']} / {row['model_selected']}: {row['calls']} calls, ${row['total_cost']:.4f} total")
