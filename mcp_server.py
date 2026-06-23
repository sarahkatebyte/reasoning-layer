"""
Reasoning Layer — MCP Server
------------------------------
Exposes the reasoning layer as callable tools inside any MCP-compatible host:
Claude Code, Cursor, VS Code, custom agents, or any platform that speaks MCP.

Tools:
  complete     — Full inference: route + compress + call model + log outcome
  route        — Model recommendation for a task, without making the call
  remember     — Persist a fact to long-term semantic memory (survives across sessions)
  recall       — Retrieve relevant memories across all stores (episodic, semantic, procedural)
  consolidate  — Run the NREM pass: extract semantic/procedural from compressed episodes
  health       — Check proxy and memory layer status

Setup:
  pip install mcp httpx
  export REASONING_LAYER_URL=http://localhost:8000

MCP config (Claude Code / Cursor):
  {
    "mcpServers": {
      "reasoning-layer": {
        "command": "python3",
        "args": ["/path/to/reasoning-layer/mcp_server.py"],
        "env": { "REASONING_LAYER_URL": "http://localhost:8000" }
      }
    }
  }

Manual test:
  python3 mcp_server.py --test
"""

import os
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("REASONING_LAYER_URL", "http://localhost:8000")

mcp = FastMCP("reasoning-layer")

_PROXY_DOWN_MSG = (
    "Reasoning layer proxy is not running. Start it with:\n"
    "  cd reasoning-layer && uvicorn proxy:app --reload --port 8000"
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def complete(message: str, call_site: str = "mcp") -> str:
    """
    Send a message through the reasoning layer and get a response.

    Automatically: compresses context, routes to the right model tier,
    calls the model with adaptive thinking, logs the outcome, and stores
    the event in long-term memory.

    Use this for any task where you want intelligent model selection and
    persistent memory — reasoning, planning, analysis, creative work.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{PROXY_URL}/complete",
                json={
                    "messages": [{"role": "user", "content": message}],
                    "call_site": call_site,
                    "stream": False,
                }
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", "")
            model = data.get("model", "unknown")
            task_type = data.get("task_type", "unknown")
            tokens_saved = data.get("tokens_saved", 0)

            footer = f"\n\n---\n_model: {model} · task: {task_type} · {tokens_saved} tokens saved_"
            return content + footer

        except httpx.ConnectError:
            return _PROXY_DOWN_MSG
        except Exception as e:
            return f"Error: {e}"


@mcp.tool()
async def route(prompt: str) -> str:
    """
    Get a model recommendation for a task without making the actual call.

    Returns the recommended model, task type, confidence, and reasoning.
    Useful for cost-aware planning — know what a call will cost before making it.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(f"{PROXY_URL}/suggest", json={"prompt": prompt})
            resp.raise_for_status()
            data = resp.json()

            lines = [
                f"Model:      {data.get('model')}",
                f"Task type:  {data.get('task_type')}",
                f"Confidence: {data.get('confidence', 0):.0%}",
            ]
            based_on = data.get("based_on", [])
            if based_on:
                lines.append("\nBased on past decisions:")
                for b in based_on[:3]:
                    lines.append(f"  {b['task_type']} → {b['model']} (similarity {b['score']})")
            return "\n".join(lines)

        except httpx.ConnectError:
            return _PROXY_DOWN_MSG
        except Exception as e:
            return f"Error: {e}"


@mcp.tool()
async def remember(fact: str, subject: str = None, confidence: float = 1.0) -> str:
    """
    Persist a fact to long-term semantic memory.

    Stored in Elasticsearch — survives across sessions and is available to
    all agents sharing this reasoning layer. High-confidence facts surface
    first in future recall. Subject scopes the fact (e.g. a user ID, project
    name, or entity the fact is about).

    Examples:
      remember("This user prefers bullet points over prose", subject="user-123")
      remember("The prod database is Postgres 15 on RDS", subject="project-api")
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{PROXY_URL}/memory/remember",
                json={"fact": fact, "subject": subject, "confidence": confidence}
            )
            resp.raise_for_status()
            return f"Stored: {fact}"
        except httpx.ConnectError:
            return _PROXY_DOWN_MSG
        except Exception as e:
            return f"Failed to store memory: {e}"


@mcp.tool()
async def recall(query: str, top_k: int = 5) -> str:
    """
    Retrieve relevant memories across all stores.

    Searches episodic (past routing events), semantic (durable facts), and
    procedural (skills and routines) memory simultaneously. Results are ranked
    by k-line score — a composite of semantic similarity, activation count,
    and recency. Suppressed (backlog) memories are excluded by default.

    Examples:
      recall("user communication preferences")
      recall("database schema decisions", top_k=3)
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{PROXY_URL}/memory/recall",
                json={"query": query, "top_k": top_k}
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])

            if not results:
                return f"No memories found for: {query}"

            lines = [f"Memory recall for: {query}\n"]
            for r in results:
                mtype = r.get("memory_type", "?")
                content = r.get("content", "")[:200]
                score = r.get("kline_score", 0)
                subject = f" [{r['subject']}]" if r.get("subject") else ""
                lines.append(f"[{mtype}]{subject} (score: {score:.3f})\n  {content}")

            return "\n\n".join(lines)

        except httpx.ConnectError:
            return _PROXY_DOWN_MSG
        except Exception as e:
            return f"Recall failed: {e}"


@mcp.tool()
async def consolidate(batch_size: int = 50) -> str:
    """
    Run the NREM consolidation pass.

    Reads episodic events that were flagged during context compression
    (phosphorylated), calls Claude Haiku to extract durable semantic facts
    and procedural patterns, writes them to their respective memory stores,
    then runs a tier decay pass to demote cold memories to warm/backlog.

    Run this periodically to prevent information loss from compression.
    Safe to call manually at any time — idempotent if nothing is pending.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{PROXY_URL}/consolidate",
                params={"batch_size": batch_size}
            )
            resp.raise_for_status()
            d = resp.json()

            nrem  = d.get("nrem", {})
            decay = d.get("tier_decay", {})
            lines = [
                f"Consolidation complete ({d.get('elapsed_seconds', '?')}s)",
                f"  Episodes processed:  {nrem.get('processed', 0)}",
                f"  Facts extracted:     {nrem.get('facts_stored', 0)}",
                f"  Skills extracted:    {nrem.get('skills_stored', 0)}",
                f"  Skipped (too short): {nrem.get('skipped', 0)}",
                f"  active → warm:       {decay.get('active_to_warm', 0)}",
                f"  warm → backlog:      {decay.get('warm_to_backlog', 0)}",
            ]
            return "\n".join(lines)

        except httpx.ConnectError:
            return _PROXY_DOWN_MSG
        except Exception as e:
            return f"Consolidation failed: {e}"


@mcp.tool()
async def health() -> str:
    """Check whether the reasoning layer proxy and memory stores are running."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{PROXY_URL}/health")
            resp.raise_for_status()
            d = resp.json()
            lines = [
                f"Proxy:         {'OK' if d.get('ok') else 'ERROR'}",
                f"Anthropic:     {'ready' if d.get('anthropic_ready') else 'NOT SET — check ANTHROPIC_API_KEY'}",
                f"Memory (ES):   {'available' if d.get('es_available') else 'not running'}",
                f"Pending store: {d.get('pending_store', 'unknown')} ({d.get('pending_events', 0)} pending)",
            ]
            return "\n".join(lines)
        except httpx.ConnectError:
            return f"Proxy is DOWN at {PROXY_URL}\n{_PROXY_DOWN_MSG}"
        except Exception as e:
            return f"Error: {e}"


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

async def _run_test():
    print(f"Testing against {PROXY_URL}\n")

    print("--- health ---")
    print(await health())

    print("\n--- route ---")
    print(await route("analyse the architectural tradeoffs between these two database options"))

    print("\n--- remember ---")
    print(await remember("This agent prefers concise responses", subject="test-agent", confidence=0.9))

    print("\n--- recall ---")
    print(await recall("response style preferences"))

    print("\n--- complete ---")
    print(await complete("What is 2 + 2?", call_site="mcp-test"))


if __name__ == "__main__":
    if "--test" in sys.argv:
        import asyncio
        asyncio.run(_run_test())
    else:
        mcp.run()
