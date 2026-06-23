# Reasoning Layer

A drop-in intelligence layer for LLM-powered agents. Route smarter, compress aggressively, remember everything.

```
Agent → POST /complete → Reasoning Layer → Anthropic API
                              ↓
                    compress · classify · route
                    log · score · remember
```

**Works with any agent platform.** OpenClaw, WorkClaw, custom pipelines — if it makes LLM calls, this layer makes them better.

---

## What it does

Most agent stacks route every call to the same model at full context. A memory filing task costs the same as a deep reasoning task. Context windows fill up with noise. Nothing is remembered between sessions.

The Reasoning Layer fixes all three:

- **Routes** each task to the right model tier — Opus for planning, Sonnet for reasoning, Haiku for memory ops
- **Compresses** context before it hits the model — real data shows 60–90% token reduction on background tasks
- **Remembers** — four-store memory architecture (episodic, semantic, procedural, epigenomic) that persists and improves across sessions
- **Learns** — k-line scoring means routing decisions get smarter with every call

---

## Architecture

```
                        ┌─────────────────────────────┐
                        │       Reasoning Layer        │
                        │                              │
Agent / WorkClaw ──────▶│  /route   /complete  /log   │──────▶ Anthropic API
                        │  /suggest /feedback          │
                        │                              │
                        │  ┌──────────┐  ┌─────────┐  │
                        │  │Compressor│  │ Context │  │
                        │  │  Node    │  │ Builder │  │
                        │  └──────────┘  └─────────┘  │
                        │         ↓           ↓        │
                        │  ┌──────────────────────┐   │
                        │  │   Memory Layer (ES)   │   │
                        │  │  episodic · semantic  │   │
                        │  │  procedural · epigeno │   │
                        │  └──────────────────────┘   │
                        │         ↓                    │
                        │  ┌──────────────────────┐   │
                        │  │  Pending Event Store  │   │
                        │  │  Redis · in-memory    │   │
                        │  └──────────────────────┘   │
                        └─────────────────────────────┘
```

---

## Quick Start

```bash
git clone https://github.com/sarahkatebyte/reasoning-layer
cd reasoning-layer

# Start Elasticsearch + Redis
docker compose up -d

# Install dependencies
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Add your key
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env

# Run the proxy
uvicorn proxy:app --reload --port 8000

# Verify
curl http://localhost:8000/health
```

```json
{
  "ok": true,
  "anthropic_ready": true,
  "es_available": true,
  "pending_events": 0,
  "pending_store": "redis"
}
```

---

## Components

### `proxy.py` — The Inference Proxy

FastAPI server. Drop-in replacement for direct Anthropic API calls. Every request is compressed, routed, logged, and remembered automatically.

**Endpoints:**

| Method | Path | What it does |
|--------|------|--------------|
| `POST` | `/complete` | Route + compress + call model + log outcome |
| `POST` | `/route` | Classify and route without calling the model |
| `POST` | `/log` | Record outcome after an external model call |
| `POST` | `/suggest` | Dry-run model suggestion, no side effects |
| `POST` | `/feedback` | Explicit quality override (`quality_ok: true/false`) |
| `GET`  | `/health` | Status of all subsystems |
| `GET`  | `/` | Chat UI |

**Adaptive thinking out of the box.** Opus and Sonnet calls automatically include `thinking: {type: "adaptive"}`. Haiku calls don't — it doesn't support it.

```python
# Before
response = anthropic.messages.create(model="claude-opus-4-8", messages=messages)

# After — routing, compression, memory, logging all happen automatically
response = requests.post("http://localhost:8000/complete", json={
    "messages": messages,
    "call_site": "my_agent"
})
```

---

### `context_builder.py` — Classifier + Router

Classifies prompts by task type and selects the right model tier. ES k-line scores override the rules when past data shows something better.

| Task type | Model | Why |
|-----------|-------|-----|
| `planning` | `claude-opus-4-8` | Multi-step planning needs the best judgment |
| `reasoning` | `claude-sonnet-4-6` | Strong reasoning, half the cost of Opus |
| `creative` | `claude-sonnet-4-6` | Creative tasks need nuance |
| `structured_output` | `claude-sonnet-4-6` | Reliable JSON and schema adherence |
| `memory_ops` | `claude-haiku-4-5` | Fast, cheap, good enough for retrieval |
| `simple_retrieval` | `claude-haiku-4-5` | Lookups don't need Opus |

---

### `compressor.py` — Context Compressor

Strips unnecessary context before it hits the model. Uses tiktoken for accurate token counting — budget limits are enforced, not approximated.

Real numbers from production data:

| Call site | Avg input | Compressed | Savings |
|-----------|-----------|------------|---------|
| Conversation Summarization | 160,827 | 20,000 | 88% |
| Reply Suggestion | 125,077 | 0 | **BLOCKED** |
| Conversation Title | 91,816 | 2,000 | 98% |
| Notification Decision | 55,781 | 5,000 | 91% |
| Memory Consolidation | 25,041 | 3,000 | 88% |

**Strategies:**

| Strategy | Best for |
|----------|----------|
| `head_tail` | Title generation — keeps opening topic + recent turn |
| `notification` | Push decisions — keeps most recent context only |
| `memory_ops` | Filing/retrieval — strips personality sections |
| `reply_summary` | Summarization — 20% head + 80% tail |
| `truncate` | Everything else — hard cut at token limit |

Customise via YAML — no Python required:

```yaml
# compressor_config.yaml
call_sites:
  "My Title Generator":
    strategy: head_tail
  "My Reply Bot":
    blocked: true
  "My Memory Agent":
    strategy: memory_ops
    max_tokens: 3000
```

```python
compressor = CompressorNode.from_config("compressor_config.yaml")
```

---

### `es_layer.py` — Memory Layer

Four-store architecture. Every agent starts amnesiac. This one doesn't.

#### The four stores

**Episodic** — specific past events. Every routing decision, with quality scores and k-line activation. Gets smarter with every call.

**Semantic** — durable facts extracted from experience. `"This user prefers concise responses over detailed explanations"`. Survives long after the original episode is forgotten.

**Procedural** — skills and routines. `"always lead with the answer, details follow"`. The rule without the story.

**Epigenomic** — marks on the other three stores. Controls what gets recalled vs. suppressed. Not a store of memories — a gate on them.

#### The epigenomic layer

Inspired by histone modification in biology — the mechanism by which cells remember state without changing their DNA. Every document in every store carries epigenomic marks:

```
tier:           active | warm | backlog
phosphorylated: bool   — pending NREM consolidation
decay_rate:     float  — per-memory override
```

- `active` → surfaces in normal recall
- `warm` → accessible when queried directly, penalised in passive routing
- `backlog` → suppressed. "Haven't played viola in 15 years" — still there, just loses every competition

**Rich-gets-richer:** every access increments `activation_count` and resets the decay clock. Unused memories decay toward warm → backlog naturally. This is k-line reinforcement generalised across all memory types.

**Phosphorylation:** when the compressor truncates content, it flags the event `phosphorylated=True`. That's the signal for the NREM consolidation pass — extract semantic and procedural value before the episodic details are gone.

```python
from es_layer import ESMemoryLayer

es = ESMemoryLayer()

# Store a durable fact
es.store_semantic(
    "This user prefers concise bullet-point responses over prose",
    subject="user-123",
    confidence=0.95,
)

# Store a procedural rule
es.store_procedural(
    "lead-with-answer",
    "Always state the conclusion first, then supporting detail",
)

# Recall across all stores — merged by k-line score
results = es.recall_all("footwear preferences", top_k=5)

# Backlog an unused skill
es.set_tier(skill_id, "backlog")

# Check what's pending consolidation
pending = es.get_phosphorylated()
```

#### Decay rates by memory type

| Type | Half-life | Why |
|------|-----------|-----|
| Episodic | ~35 days | Specific events fade |
| Procedural | ~140 days | Skills decay without practice |
| Semantic | ~350 days | Facts outlast the events that taught them |

---

### Pending Event Store — Redis

The `/route` → `/log` feedback loop stores events between calls. In a multi-agent environment (multiple WorkClaw agents, multiple uvicorn workers), an in-memory dict breaks — each process has its own copy.

Redis solves this with two atomic operations:
- `SETEX` — write event + 1-hour TTL in one call. No partial-write window.
- `GETDEL` — read and delete atomically. Two workers can't both process the same `/log`.

Set `REDIS_URL` to enable. Falls back gracefully to in-memory if unset (fine for local dev, not for production).

```bash
export REDIS_URL=redis://localhost:6379
```

---

## Setup

### Environment variables

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
REDIS_URL=redis://localhost:6379
ES_HOST=http://localhost:9200
ASTRID_WORKSPACE=/path/to/.astrid    # optional — loads system prompt from SOUL.md
```

### Docker Compose

```bash
docker compose up -d   # starts Elasticsearch on :9200
```

Add Redis alongside it:

```yaml
redis:
  image: redis:alpine
  ports:
    - "6379:6379"
```

### Running the proxy

```bash
# Development
uvicorn proxy:app --reload --port 8000

# Production — multiple workers are safe with Redis
uvicorn proxy:app --workers 4 --port 8000
```

---

## Wiring to your agent

```python
import requests

# Route a prompt — classify + suggest model, no model call
route = requests.post("http://localhost:8000/route", json={
    "prompt": "analyse the tradeoffs between these two approaches",
    "call_site": "main_agent"
}).json()

print(route["model"])        # claude-sonnet-4-6
print(route["task_type"])    # reasoning
print(route["tokens_saved"]) # tokens saved by compression

# Full inference — route + compress + call + log in one shot
response = requests.post("http://localhost:8000/complete", json={
    "messages": [{"role": "user", "content": "..."}],
    "call_site": "main_agent",
    "stream": False
}).json()

print(response["content"])
print(response["model"])
print(response["tokens_saved"])

# Explicit quality feedback — feeds the learning loop
requests.post("http://localhost:8000/feedback", json={
    "event_id": route["event_id"],
    "quality_ok": True
})
```

---

## Roadmap

- [x] Compressor node — tiktoken-accurate compression per call site
- [x] Classifier node — keyword heuristic task type detection
- [x] Router node — model selection with k-line ES override
- [x] Quality scorer — heuristic response quality (0.0–1.0)
- [x] Inference proxy — FastAPI `/complete` endpoint
- [x] Four-store memory — episodic, semantic, procedural, epigenomic
- [x] Epigenomic layer — tier gating, phosphorylation, per-type decay
- [x] Redis pending store — multi-agent safe, atomic SETEX/GETDEL
- [x] Adaptive thinking — auto-enabled for Opus and Sonnet
- [x] MCP server — reasoning layer tools over MCP
- [ ] NREM consolidation pass — episodic → semantic/procedural extraction
- [ ] Multi-tenant memory — user_id/team_id scoping for team deployments
- [ ] Probabilistic routing — multi-label task classification
- [ ] GitHub Actions CI
- [ ] Postgres backend option

---

## Built with

- [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude API
- [Elasticsearch](https://elastic.co) — semantic memory and k-line search
- [sentence-transformers](https://www.sbert.net/) — local embeddings, no API cost
- [Redis](https://redis.io) — multi-agent pending event store
- [FastAPI](https://fastapi.tiangolo.com) — inference proxy
- [tiktoken](https://github.com/openai/tiktoken) — accurate token counting

---

*Built by Sarah Haddon.*
