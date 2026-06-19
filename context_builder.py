"""
Reasoning Layer - Context Builder
-----------------------------------
Three jobs:
  1. classify_task(prompt) -> task type string
  2. build_context(prompt, **kwargs) -> trimmed context dict
  3. load_system_prompt() -> full Astrid system prompt from SOUL.md + essentials.md
"""

import os
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# System prompt loader - assembles Astrid's personality + user knowledge
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("ASTRID_WORKSPACE", Path.home() / ".astrid"))
SOUL_PATH = _WORKSPACE / "SOUL.md"
ESSENTIALS_PATH = _WORKSPACE / "pkb/essentials.md"


def _load_file(path: Path) -> str:
    """Load a file and strip comment lines (lines starting with _)."""
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    return "\n".join(l for l in lines if not l.startswith("_ ") and l != "_")


def load_system_prompt() -> str:
    """
    Assemble the full Astrid system prompt from SOUL.md and essentials.md.
    Strips internal comment lines. Falls back to a minimal prompt if files not found.
    """
    soul = _load_file(SOUL_PATH)
    essentials = _load_file(ESSENTIALS_PATH)

    if not soul and not essentials:
        return "You are Astrid. You are sharp, warm, direct, and have a dry sense of humor. You know your user Sarah well."

    parts = []
    if soul:
        parts.append(soul)
    if essentials:
        parts.append(essentials)

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Task type classifier (heuristic - gets replaced by learned classifier later)
# ---------------------------------------------------------------------------

TASK_PATTERNS = {
    "planning": [
        r"\bbuild-plan\b",
        r"\bdo-plan\b",
        r"\bbuild plan\b",
        r"\bdo plan\b",
    ],
    "memory_ops": [
        r"\bconsolidat\w*\b",
        r"\bfil(e|ing)\b",
        r"\barchiv\w*\b",
        r"\bremember\b",
        r"\bstore\b.*\bmemory\b",
        r"\bretrie(ve|val)\b.*\bmemory\b",
        r"\bextract\b.*\bmemory\b",
        r"\bmemory\b.*\bupdate\b",
    ],
    "reasoning": [
        r"\banalyze\b",
        r"\breason\w*\b",
        r"\barchitect\w*\b",
        r"\bdesign\b.*\bsystem\b",
        r"\btradeoff\w*\b",
        r"\bcompare\b.*\bapproach\w*\b",
        r"\bstep.by.step\b",
        r"\bwhy\b.*\bbecause\b",
        r"\bexplain\b.*\bhow\b",
        r"\bdebug\b",
        r"\brefactor\b",
    ],
    "simple_retrieval": [
        r"\bwhat is\b",
        r"\bwho is\b",
        r"\blook up\b",
        r"\bfetch\b",
        r"\bget\b.*\bfrom\b",
        r"\blist\b.*\ball\b",
        r"\bshow me\b",
        r"\bfind\b.*\brecord\b",
    ],
    "structured_output": [
        r"\bjson\b",
        r"\bschema\b",
        r"\bstructured\b",
        r"\bformat\b.*\bas\b",
        r"\bparse\b",
        r"\bextract\b.*\bfields?\b",
        r"\btable\b",
        r"\bcsv\b",
    ],
    "creative": [
        r"\bwrite\b",
        r"\bgenerat\w*\b.*\bstory\b",
        r"\bcreate\b.*\bnarrative\b",
        r"\bdraft\b",
        r"\bpoem\b",
        r"\bcreatif\w*\b",
        r"\bimagine\b",
        r"\bbrainstorm\b",
    ],
}


def classify_task(prompt: str) -> str:
    """
    Classify a prompt into a task type using keyword heuristics.
    Returns one of: reasoning, memory_ops, simple_retrieval, structured_output, creative
    Falls back to 'reasoning' if unclear (better to over-invest than under-invest).
    """
    lower = prompt.lower()
    scores = {task: 0 for task in TASK_PATTERNS}

    for task_type, patterns in TASK_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                scores[task_type] += 1

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "reasoning"  # safe default - sonnet handles unknown tasks well

    return best


# ---------------------------------------------------------------------------
# Context builder - trims the context dict before routing
# ---------------------------------------------------------------------------

def build_context(
    prompt: str,
    system_prompt: Optional[str] = None,
    conversation_history: Optional[list] = None,
    max_history_turns: int = 10,
) -> dict:
    """
    Builds a lean context dict for the model call.
    Trims conversation history to the last N turns.
    Strips system prompt sections that aren't needed for the task type.

    Returns a dict ready to pass to the model API.
    """
    task_type = classify_task(prompt)

    # Trim history - most tasks only need recent turns
    history = conversation_history or []
    if task_type in ("memory_ops", "simple_retrieval", "structured_output"):
        # These tasks don't need deep history
        history = history[-4:]
    else:
        history = history[-max_history_turns:]

    # Trim system prompt for memory ops - strip personality sections
    trimmed_system = system_prompt or ""
    if task_type == "memory_ops" and trimmed_system:
        trimmed_system = re.sub(
            r'#{1,2} (SOUL|IDENTITY|Personality|Vibe|Core Truths|Communication Style)[^\n]*\n.*?(?=\n#{1,2} |\Z)',
            '[personality context omitted]',
            trimmed_system,
            flags=re.DOTALL
        )

    return {
        "task_type": task_type,
        "prompt": prompt,
        "system_prompt": trimmed_system,
        "conversation_history": history,
    }


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

ROUTING_TABLE = {
    # Opus: tasks requiring multi-step planning, holding large context, structural judgment
    "planning":          ("claude-opus-4-8",   "plan creation/execution -> opus (multi-step, structural judgment)"),
    "reasoning":         ("claude-sonnet-4-6", "reasoning -> sonnet (opus reserved for planning)"),
    # Haiku: pure retrieval, no synthesis needed
    "memory_ops":        ("claude-haiku-4-5",  "memory task -> haiku (cheapest)"),
    "simple_retrieval":  ("claude-haiku-4-5",  "simple lookup -> haiku (cheapest)"),
    # Sonnet: everything else
    "creative":          ("claude-sonnet-4-6", "creative task -> sonnet"),
    "structured_output": ("claude-sonnet-4-6", "structured output -> sonnet"),
}

DEFAULT_MODEL = ("claude-sonnet-4-6", "unclassified -> sonnet (default)")


def select_model(task_type: str, similar: list) -> tuple[str, str]:
    """
    Select model based on task type. If ES has strong past signal with confirmed
    quality, prefer that. Null quality_score is treated as neutral (0.5) — not
    excluded — so early events before scoring existed still contribute signal.
    """
    for s in similar:
        # kline_score available after activation/decay was added; fall back to raw cosine
        score = s.get("kline_score") or s.get("similarity_score", 0)
        quality = s.get("quality_score")
        effective_quality = quality if quality is not None else 0.5
        past_model = s.get("model_selected")
        if score > 0.85 and effective_quality >= 0.7 and past_model:
            return past_model, f"ES k-line match (score={score:.2f}, quality={effective_quality:.2f})"

    return ROUTING_TABLE.get(task_type, DEFAULT_MODEL)
