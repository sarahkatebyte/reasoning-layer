"""
Astrid MCP Server
-----------------
Exposes Astrid as callable tools inside Cursor (or any MCP-compatible host).
Runs as a stdio process — Cursor spawns it automatically via ~/.cursor/mcp.json.

Tools:
  chat            — Full conversation with Astrid (memory, routing, SOUL.md injected)
  get_context     — Pull relevant context from Astrid's knowledge base
  remember        — Persist a fact to Astrid's memory buffer
  route           — Get model recommendation for a task

Requires:
  pip install mcp httpx

Usage (manual test):
  python3 mcp_server.py         # stdio mode (what Cursor uses)
  python3 mcp_server.py --test  # quick smoke test against live proxy
"""

import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Load .env from the reasoning-layer directory so env vars are available
# when spawned as a subprocess (e.g. by Claude Code, Cursor) without a shell.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on environment

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("ASTRID_PROXY_URL", "http://localhost:8000")
WORKSPACE = Path(os.environ.get("ASTRID_WORKSPACE", Path.home() / ".astrid"))
PKB_ROOT = WORKSPACE / "pkb"
BUFFER_FILE = PKB_ROOT / "buffer.md"

mcp = FastMCP("Astrid")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def chat(message: str, call_site: str = "cursor") -> str:
    """
    Send a message to Astrid and get a full response.
    Astrid has memory, context about Sarah's work, and routes to the right model automatically.
    Use this for anything you'd want to ask Astrid directly — code review, architectural advice,
    context about a project, or anything requiring her knowledge of Sarah's full situation.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
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

            return f"{content}\n\n---\n_routed to {model} ({task_type}), {tokens_saved} tokens saved_"

        except httpx.ConnectError:
            return (
                "Astrid proxy is not running. Start it with:\n"
                "  cd ~/reasoning-layer && uvicorn proxy:app --reload --port 8000"
            )
        except Exception as e:
            return f"Error reaching Astrid: {e}"


@mcp.tool()
async def get_context(query: str) -> str:
    """
    Pull relevant context from Astrid's knowledge base for a given query.
    Returns essentials, current state (NOW.md), active threads, and any matching PKB files.
    Use this at the start of a coding session to orient Astrid on what you're working on.
    """
    sections = []

    # Always include essentials + current state
    for label, path in [
        ("Essentials", WORKSPACE / "pkb" / "essentials.md"),
        ("Current State", WORKSPACE / "NOW.md"),
        ("Active Threads", WORKSPACE / "pkb" / "threads.md"),
    ]:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"## {label}\n{content}")

    # Search PKB files for query terms
    query_lower = query.lower()
    query_words = set(query_lower.split())
    matches = []

    if PKB_ROOT.exists():
        for md_file in PKB_ROOT.rglob("*.md"):
            if md_file.name in ("buffer.md", "essentials.md", "threads.md", "INDEX.md"):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                text_lower = text.lower()
                # Score by how many query words appear
                score = sum(1 for w in query_words if w in text_lower)
                if score > 0:
                    matches.append((score, md_file.stem, text))
            except Exception:
                continue

    # Include top 2 matches
    matches.sort(reverse=True)
    for score, name, text in matches[:2]:
        sections.append(f"## {name} (PKB)\n{text.strip()}")

    if not sections:
        return f"No context found for '{query}' — PKB may be empty or workspace path is wrong."

    return "\n\n---\n\n".join(sections)


@mcp.tool()
async def remember(fact: str) -> str:
    """
    Persist a fact to Astrid's memory buffer so she remembers it going forward.
    Use this when you learn something important during a coding session — a decision,
    a preference, a technical detail about a project, or anything Astrid should carry forward.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{timestamp}] (from Cursor) {fact}\n"

    try:
        BUFFER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUFFER_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        return f"Remembered: {fact}"
    except Exception as e:
        return f"Failed to write to memory buffer: {e}"


@mcp.tool()
async def route(prompt: str) -> str:
    """
    Get Astrid's model recommendation for a given task without making the full call.
    Returns the recommended model, task type, confidence, and reasoning.
    Useful for cost-aware prompt engineering — know what you're about to spend before spending it.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{PROXY_URL}/suggest",
                json={"prompt": prompt}
            )
            resp.raise_for_status()
            data = resp.json()
            lines = [
                f"Model: {data.get('model')}",
                f"Task type: {data.get('task_type')}",
                f"Confidence: {data.get('confidence', 0):.0%}",
            ]
            based_on = data.get("based_on", [])
            if based_on:
                lines.append("Based on past decisions:")
                for b in based_on[:3]:
                    lines.append(f"  - {b['task_type']} -> {b['model']} (similarity {b['score']})")
            return "\n".join(lines)

        except httpx.ConnectError:
            return (
                "Astrid proxy is not running. Start it with:\n"
                "  cd ~/reasoning-layer && uvicorn proxy:app --reload --port 8000"
            )
        except Exception as e:
            return f"Error: {e}"


@mcp.tool()
async def proxy_health() -> str:
    """Check if the Astrid proxy is running and ready."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{PROXY_URL}/health")
            resp.raise_for_status()
            data = resp.json()
            lines = [
                f"Proxy: {'OK' if data.get('ok') else 'ERROR'}",
                f"Anthropic client: {'ready' if data.get('anthropic_ready') else 'NOT SET - check ANTHROPIC_API_KEY'}",
                f"Elasticsearch: {'available' if data.get('es_available') else 'not running (routing still works)'}",
                f"Pending events: {data.get('pending_events', 0)}",
            ]
            return "\n".join(lines)
        except httpx.ConnectError:
            return (
                "Proxy is DOWN. Start it:\n"
                "  cd ~/reasoning-layer && uvicorn proxy:app --reload --port 8000"
            )
        except Exception as e:
            return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
