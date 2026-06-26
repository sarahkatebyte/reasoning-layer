"""
Reasoning Layer - FastAPI Proxy
--------------------------------
Sits between OpenClaw (or any agent) and the model APIs.
Exposes four endpoints:

  POST /route    - classify + suggest model for a prompt
  POST /log      - record outcome after a call completes
  POST /suggest  - dry-run model suggestion without logging
  POST /feedback - explicit quality override (quality_ok true/false)

Runs compressor on every incoming prompt before routing.
Logs everything to SQLite + ES.

Usage:
    cd ~/reasoning-layer
    docker compose up -d elasticsearch
    uvicorn proxy:app --reload --port 8000
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("proxy")
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
import anthropic as anthropic_sdk
from pydantic import BaseModel

from compressor import CompressorNode
from context_builder import classify_task, load_system_prompt, select_model
from reasoning_layer import ReasoningLogger, ReasoningEvent
import napper

load_dotenv()

# System prompt loaded once; nap context is injected fresh per-request
SYSTEM_PROMPT = load_system_prompt()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic_sdk.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Models that support adaptive thinking. Haiku 4.5 does not.
_THINKING_MODELS = {"claude-opus-4-8", "claude-sonnet-4-6"}

# ES layer is optional - if ES isn't running, routing still works via SQLite
try:
    from es_layer import ESMemoryNode as ESReasoningLayer
    os.environ.setdefault("ES_HOST", "http://localhost:9200")
    es = ESReasoningLayer()
    ES_AVAILABLE = True
except Exception as e:
    log.warning("Elasticsearch unavailable — routing will use SQLite only: %s", e)
    es = None
    ES_AVAILABLE = False


app = FastAPI(title="Reasoning Layer Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

compressor = CompressorNode()
logger = ReasoningLogger()


@app.on_event("startup")
async def _start_nap_watcher():
    consolidator = None
    try:
        from consolidator import Consolidator
        consolidator = Consolidator()
    except Exception:
        pass
    asyncio.create_task(napper.inactivity_watcher(client, consolidator))


# ---------------------------------------------------------------------------
# Pending event store — Redis if available, in-memory fallback for local dev
# ---------------------------------------------------------------------------

_PENDING_TTL_SECS = 3600  # 1 hour


class _PendingStore:
    """
    Stores routing events between /route and /log calls.

    Redis mode  — shared across all proxy workers. Uses SETEX (atomic set+TTL)
                  and GETDEL (atomic get+delete). TTL auto-expires stale events,
                  no prune loop needed. Safe under concurrent writes.

    Fallback    — single in-memory dict. Fine for local dev. Breaks under
                  multiple workers or concurrent agents (use Redis in production).
    """

    def __init__(self, redis_url: str = None, ttl: int = _PENDING_TTL_SECS):
        self._ttl = ttl
        self._redis = None
        self._mem: dict[str, dict] = {}

        if redis_url:
            try:
                import redis as redis_lib
                client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._redis = client
                log.info("Redis connected (%s) — pending events are shared across workers", redis_url)
            except Exception as e:
                log.warning("Redis unavailable (%s) — falling back to in-memory store (single-process only)", e)

    @property
    def backend(self) -> str:
        return "redis" if self._redis else "memory"

    def put(self, event_id: str, data: dict):
        """Store event. TTL starts now — if /log never comes, it auto-expires."""
        if self._redis:
            self._redis.setex(f"pending:{event_id}", self._ttl, json.dumps(data))
        else:
            self._mem[event_id] = {**data, "_started_at": time.time()}

    def pop(self, event_id: str) -> dict | None:
        """Atomically retrieve and delete. Returns None if expired or never existed."""
        if self._redis:
            raw = self._redis.getdel(f"pending:{event_id}")
            return json.loads(raw) if raw else None
        return self._mem.pop(event_id, None)

    def size(self) -> int:
        if self._redis:
            return len(self._redis.keys("pending:*"))
        return len(self._mem)

    def prune(self):
        """Prune stale entries. Only does work in memory fallback — Redis TTL handles it."""
        if self._redis:
            return
        cutoff = time.time() - self._ttl
        stale = [eid for eid, ev in self._mem.items() if ev.get("_started_at", 0) < cutoff]
        for eid in stale:
            log.warning("Pruning stale pending event %s (no /log within TTL)", eid)
            del self._mem[eid]


_pending = _PendingStore(redis_url=os.environ.get("REDIS_URL"))


# ---------------------------------------------------------------------------
# Conversation store — persists chat history across page refreshes
# ---------------------------------------------------------------------------

_SESSION_TTL_SECS = 30 * 24 * 3600   # 30 days
_SESSION_MAX_MSGS  = 200               # trim oldest messages beyond this

class _ConversationStore:
    """Redis-backed conversation history. Falls back to in-memory."""

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._mem: dict[str, list] = {}

    def get(self, session_id: str) -> list:
        if self._redis:
            raw = self._redis.get(f"session:{session_id}")
            return json.loads(raw) if raw else []
        return list(self._mem.get(session_id, []))

    def append(self, session_id: str, new_messages: list):
        history = self.get(session_id)
        history.extend(new_messages)
        history = history[-_SESSION_MAX_MSGS:]
        if self._redis:
            self._redis.setex(f"session:{session_id}", _SESSION_TTL_SECS, json.dumps(history))
        else:
            self._mem[session_id] = history

    def clear(self, session_id: str):
        if self._redis:
            self._redis.delete(f"session:{session_id}")
        else:
            self._mem.pop(session_id, None)


_conversations = _ConversationStore(
    redis_client=_pending._redis   # reuse the same Redis connection
)


def score_response_quality(response_text: str, output_tokens: Optional[int] = None) -> float:
    """
    Heuristic quality score (0.0–1.0) based on observable response signals.
    Not ground truth — good enough to drive model routing decisions over time.
    """
    if not response_text:
        return 0.0

    # Penalize very short responses (likely a refusal or error)
    length_score = 1.0 if len(response_text) >= 50 else 0.3

    # Penalize refusal / error language
    refusal_signals = ["i cannot", "i can't", "i'm unable", "i am unable", "i apologize",
                       "error occurred", "something went wrong"]
    refusal_score = 0.2 if any(s in response_text.lower() for s in refusal_signals) else 1.0

    # Penalize likely truncation (used >95% of max_tokens budget)
    truncation_score = 0.6 if output_tokens and output_tokens >= 3890 else 1.0

    return round(length_score * 0.3 + refusal_score * 0.5 + truncation_score * 0.2, 3)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RouteRequest(BaseModel):
    prompt: str
    call_site: str = "default"

class RouteResponse(BaseModel):
    event_id: str
    model: str
    task_type: str
    reason: str
    compressed_prompt: str
    tokens_saved: int
    similar_past_decisions: list = []
    blocked: bool = False

class LogRequest(BaseModel):
    event_id: str
    quality_score: float
    latency_ms: int
    output_tokens: Optional[int] = None

class FeedbackRequest(BaseModel):
    event_id: str
    quality_ok: bool

class SuggestRequest(BaseModel):
    prompt: str

class SuggestResponse(BaseModel):
    model: str
    task_type: str
    confidence: float
    based_on: list = []

class RememberRequest(BaseModel):
    fact: str
    subject: Optional[str] = None
    confidence: float = 1.0

class RecallRequest(BaseModel):
    query: str
    top_k: int = 5


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/route", response_model=RouteResponse)
async def route_request(req: RouteRequest):
    """
    Main routing endpoint. Called by OpenClaw before every model call.
    1. Compress the prompt
    2. Classify the task type
    3. Query ES for similar past decisions
    4. Select the best model
    5. Return routing decision + compressed prompt
    """
    _pending.prune()

    # Step 1: compress
    compression = compressor.compress(
        text=req.prompt,
        call_site=req.call_site,
    )

    if compression.blocked:
        return RouteResponse(
            event_id="",
            model="",
            task_type="",
            reason=f"call_site '{req.call_site}' is blocked — skip this LLM call",
            compressed_prompt="",
            tokens_saved=compression.tokens_saved,
            blocked=True,
        )

    compressed = compression.compressed_text
    task_weights = classify_task(compressed)
    task_type = max(task_weights, key=task_weights.get)

    # Step 2: query ES for similar past decisions
    similar = []
    if ES_AVAILABLE:
        try:
            similar = es.find_similar(compressed, top_k=3)
        except Exception as e:
            log.warning("ES find_similar failed, falling back to SQLite routing: %s", e)

    # Step 3: select model
    model, reason = select_model(task_weights, similar)

    # Step 4: store pending event so /log can close the loop
    event_id = str(uuid.uuid4())[:8]
    _pending.put(event_id, {
        "call_site": req.call_site,
        "task_type": task_type,
        "model_selected": model,
        "routing_reason": reason,
        "input_tokens": compression.compressed_tokens,
        "request_text": compressed,
    })

    return RouteResponse(
        event_id=event_id,
        model=model,
        task_type=task_type,
        reason=reason,
        compressed_prompt=compressed,
        tokens_saved=compression.tokens_saved,
        similar_past_decisions=[
            {"task_type": s["task_type"], "model": s["model_selected"], "score": round(s["similarity_score"], 3)}
            for s in similar
        ],
    )


@app.post("/log")
async def log_outcome(req: LogRequest):
    """
    Close the feedback loop after a model call completes.
    Records outcome to SQLite and indexes to ES for future routing.
    """
    pending = _pending.pop(req.event_id)
    if not pending:
        raise HTTPException(status_code=404, detail=f"No pending event with id '{req.event_id}'")

    event = ReasoningEvent(
        call_site=pending["call_site"],
        task_type=pending["task_type"],
        model_selected=pending["model_selected"],
        routing_reason=pending["routing_reason"],
        input_tokens=pending["input_tokens"],
        output_tokens=req.output_tokens,
        latency_ms=req.latency_ms,
        quality_score=req.quality_score,
        request_text=pending["request_text"],
    )

    db_id = logger.log(event)

    # Index to ES if available
    if ES_AVAILABLE:
        try:
            es.index_event(
                event_id=req.event_id,
                request_text=pending["request_text"],
                task_type=pending["task_type"],
                model_selected=pending["model_selected"],
                quality_score=req.quality_score,
                latency_ms=req.latency_ms,
            )
        except Exception as e:
            log.error("ES index_event failed — event %s not indexed: %s", req.event_id, e)

    return {"ok": True, "db_id": db_id}


@app.post("/feedback")
async def record_feedback(req: FeedbackRequest):
    """
    Explicit user feedback on a completed response.
    Overrides the heuristic quality score with ground truth:
      quality_ok=true  → 1.0 (confirmed good)
      quality_ok=false → 0.1 (confirmed bad — low but not zero, preserves model info)
    """
    if not ES_AVAILABLE:
        raise HTTPException(status_code=503, detail="ES not available — feedback cannot be stored")

    score = 1.0 if req.quality_ok else 0.1
    try:
        es.update_feedback(event_id=req.event_id, quality_ok=req.quality_ok, quality_score=score)
    except Exception as e:
        log.error("ES update_feedback failed for event %s: %s", req.event_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to record feedback: {e}")

    log.info("Feedback recorded: event=%s quality_ok=%s score=%s", req.event_id, req.quality_ok, score)
    return {"ok": True, "event_id": req.event_id, "quality_score": score}


@app.post("/suggest", response_model=SuggestResponse)
async def suggest_model(req: SuggestRequest):
    """
    Dry-run model suggestion. No logging, no side effects.
    Useful for pre-flight checks or UI previews.
    """
    task_weights = classify_task(req.prompt)
    task_type = max(task_weights, key=task_weights.get)

    similar = []
    suggestion = None
    confidence = 0.5

    if ES_AVAILABLE:
        try:
            candidates = es.find_similar(req.prompt, top_k=10)
            good = [e for e in candidates if (e.get("quality_score") or 0.5) >= 0.7]
            model_votes: dict[str, float] = {}
            for e in good:
                m = e.get("model_selected")
                if m:
                    model_votes[m] = model_votes.get(m, 0.0) + e.get("kline_score", 1.0)
            if model_votes:
                suggestion = max(model_votes, key=lambda m: model_votes[m])
                confidence = 0.85 if len(good) >= 3 else 0.65
            similar = candidates[:3]
        except Exception as e:
            log.warning("ES find_similar failed, falling back to task-type routing: %s", e)

    model, _ = select_model(task_weights, similar)
    if suggestion:
        model = suggestion

    return SuggestResponse(
        model=model,
        task_type=task_type,
        confidence=confidence,
        based_on=[
            {"task_type": s["task_type"], "model": s["model_selected"], "score": round(s["similarity_score"], 3)}
            for s in similar
        ],
    )


class Message(BaseModel):
    role: str
    content: str

class CompleteRequest(BaseModel):
    messages: List[Message]
    call_site: str = "default"
    stream: bool = False
    session_id: Optional[str] = None

def _prepare_messages(req: CompleteRequest, compressed_prompt: str) -> tuple[list, str]:
    """Split system message, inject compressed prompt, return (messages, system_prompt)."""
    system_msg = ""
    non_system = []
    for m in [m.dict() for m in req.messages]:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            non_system.append(m)

    if not system_msg:
        system_msg = SYSTEM_PROMPT

    for i in range(len(non_system) - 1, -1, -1):
        if non_system[i]["role"] == "user":
            non_system[i]["content"] = compressed_prompt
            break

    return non_system, system_msg


async def _stream_response(kwargs: dict, event_id: str, t0: float) -> StreamingResponse:
    async def _stream():
        chunks = []
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                chunks.append(text)
                yield f"data: {text}\n\n"
            final = await stream.get_final_message()
        full_text = "".join(chunks)
        output_tokens = final.usage.output_tokens if final.usage else None
        await log_outcome(LogRequest(
            event_id=event_id,
            quality_score=score_response_quality(full_text, output_tokens),
            latency_ms=int((time.time() - t0) * 1000),
            output_tokens=output_tokens,
        ))
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Filesystem + shell tools — available in every /complete call
# ---------------------------------------------------------------------------

import subprocess as _subprocess

FILESYSTEM_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the full contents of a file at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating it (and any parent dirs) if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "bash",
        "description": "Execute a bash command and return stdout + stderr. 30-second timeout.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def _execute_tool(name: str, inputs: dict) -> str:
    try:
        if name == "read_file":
            path = os.path.expanduser(inputs["path"])
            with open(path) as f:
                return f.read()
        elif name == "write_file":
            path = os.path.expanduser(inputs["path"])
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as f:
                f.write(inputs["content"])
            return f"ok: wrote {len(inputs['content'])} chars to {path}"
        elif name == "list_directory":
            path = os.path.expanduser(inputs["path"])
            return "\n".join(sorted(os.listdir(path)))
        elif name == "bash":
            result = _subprocess.run(
                inputs["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=os.path.expanduser("~"),
            )
            out = (result.stdout + result.stderr).strip()
            return out[:8000] if out else "(no output)"
        else:
            return f"unknown tool: {name}"
    except Exception as e:
        return f"error: {e}"


@app.post("/complete")
async def complete(req: CompleteRequest):
    """
    Full inference endpoint. Route + compress + call model + log outcome.
    Drop-in replacement for direct model API calls.
    """
    if client is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")

    user_msg = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )
    route = await route_request(RouteRequest(prompt=user_msg, call_site=req.call_site))
    messages, system_msg = _prepare_messages(req, route.compressed_prompt)

    # If the compressor discarded tokens, phosphorylate the event so the NREM
    # consolidation pass knows to extract semantic/procedural value before those
    # episodic details are gone from the context window.
    if route.tokens_saved > 0 and ES_AVAILABLE and route.event_id:
        try:
            es.phosphorylate(route.event_id)
        except Exception as e:
            log.warning("phosphorylate failed for event %s: %s", route.event_id, e)

    t0 = time.time()
    kwargs = dict(model=route.model, messages=messages, max_tokens=4096, system=system_msg)
    if route.model in _THINKING_MODELS:
        kwargs["thinking"] = {"type": "adaptive"}

    # Inject fresh nap context into system message on each request
    nap_ctx = napper.load_nap_context()
    if nap_ctx:
        system_msg = system_msg + nap_ctx

    if req.stream:
        return await _stream_response(kwargs, route.event_id, t0)

    # Agentic tool loop — runs until model stops calling tools
    kwargs["tools"] = FILESYSTEM_TOOLS
    tool_messages = list(messages)
    response = None
    while True:
        kwargs["messages"] = tool_messages
        response = await client.messages.create(**kwargs)
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:
            break
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": _execute_tool(b.name, b.input),
            }
            for b in tool_blocks
        ]
        tool_messages.append({"role": "assistant", "content": response.content})
        tool_messages.append({"role": "user",      "content": tool_results})

    content = next((b.text for b in response.content if b.type == "text"), "")
    output_tokens = response.usage.output_tokens if response.usage else None

    await log_outcome(LogRequest(
        event_id=route.event_id,
        quality_score=score_response_quality(content, output_tokens),
        latency_ms=int((time.time() - t0) * 1000),
        output_tokens=output_tokens,
    ))

    if req.session_id:
        _conversations.append(req.session_id, [
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": content},
        ])

    # Record activity and write micro nap snapshot in background
    napper.touch_active()
    nap_messages = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if isinstance(m.get("content"), str)
    ]
    nap_messages.append({"role": "assistant", "content": content})
    if client:
        asyncio.create_task(napper.write_micro_nap(nap_messages, client))

    return {
        "content": content,
        "model": route.model,
        "tokens_saved": route.tokens_saved,
        "task_type": route.task_type,
    }


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Load conversation history for a session."""
    return {"session_id": session_id, "messages": _conversations.get(session_id)}


@app.delete("/session/{session_id}")
async def clear_session(session_id: str):
    """Clear a session's conversation history."""
    _conversations.clear(session_id)
    return {"ok": True, "session_id": session_id}


@app.post("/consolidate")
async def consolidate(batch_size: int = 50):
    """
    Trigger the NREM consolidation pass on-demand.
    Reads phosphorylated episodic events, extracts semantic/procedural value,
    then runs tier decay across all memory stores.
    """
    if not ES_AVAILABLE:
        raise HTTPException(status_code=503, detail="Memory store unavailable — ES not running")
    if client is None:
        raise HTTPException(status_code=503, detail="Anthropic client not configured")
    try:
        from consolidator import Consolidator
        c = Consolidator(es=es, anthropic_client=anthropic_sdk.AsyncAnthropic(api_key=ANTHROPIC_API_KEY))
        result = await c.run(batch_size=batch_size)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Consolidation failed: {e}")


@app.post("/memory/remember")
async def memory_remember(req: RememberRequest):
    """
    Store a semantic fact in long-term memory.
    Persists across sessions — available to all agents that share this reasoning layer.
    """
    if not ES_AVAILABLE:
        raise HTTPException(status_code=503, detail="Memory store unavailable — ES not running")
    try:
        doc_id = es.store_semantic(
            fact_text=req.fact,
            subject=req.subject,
            confidence=req.confidence,
        )
        return {"ok": True, "id": doc_id, "fact": req.fact}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store memory: {e}")


@app.post("/memory/recall")
async def memory_recall(req: RecallRequest):
    """
    Retrieve relevant memories across all stores (episodic, semantic, procedural).
    Returns results ranked by k-line score — recency × activation × similarity.
    """
    if not ES_AVAILABLE:
        raise HTTPException(status_code=503, detail="Memory store unavailable — ES not running")
    try:
        results = es.recall_all(req.query, top_k=req.top_k)
        return {
            "query": req.query,
            "results": [
                {
                    "memory_type": r.get("memory_type"),
                    "content": r.get("fact_text") or r.get("skill_description") or r.get("request_text", ""),
                    "subject": r.get("subject"),
                    "tier": r.get("tier"),
                    "kline_score": round(r.get("kline_score", 0), 4),
                    "similarity_score": round(r.get("similarity_score", 0), 4),
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recall failed: {e}")


@app.get("/")
async def chat_ui():
    return FileResponse(Path(__file__).parent / "chat.html")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "anthropic_ready": client is not None,
        "es_available": ES_AVAILABLE,
        "pending_events": _pending.size(),
        "pending_store": _pending.backend,
    }


# ---------------------------------------------------------------------------
