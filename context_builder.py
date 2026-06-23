"""
Reasoning Layer - Context Builder
-----------------------------------
Three jobs:
  1. classify_task(prompt) -> weighted dict over task types (multi-label)
  2. build_context(prompt, **kwargs) -> trimmed context dict
  3. select_model(weights, similar) -> (model_id, reason)
"""

import os
import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("REASONING_LAYER_WORKSPACE", Path.home() / ".reasoning-layer"))
_SYSTEM_PROMPT_PATH = _WORKSPACE / "system_prompt.md"


def _load_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text().strip()


def load_system_prompt() -> str:
    """
    Load the agent system prompt from $REASONING_LAYER_WORKSPACE/system_prompt.md.
    Falls back to a generic agent prompt if the file doesn't exist.
    """
    content = _load_file(_SYSTEM_PROMPT_PATH)
    if content:
        return content
    return "You are a helpful AI assistant."


# ---------------------------------------------------------------------------
# Multi-label task classifier
# ---------------------------------------------------------------------------

TASK_PATTERNS = {
    "planning": [
        r"\bbuild.plan\b",
        r"\bdo.plan\b",
        r"\broadmap\b",
        r"\bstrateg\w*\b",
        r"\bstep.by.step\b",
        r"\bbreakdown\b",
        r"\bmilestone\w*\b",
        r"\bschedule\b.*\btask\w*\b",
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
        r"\bwhy\b.*\bbecause\b",
        r"\bexplain\b.*\bhow\b",
        r"\bdebug\b",
        r"\brefactor\b",
        r"\bdiagnos\w*\b",
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
        r"\bcreativ\w*\b",
        r"\bimagine\b",
        r"\bbrainstorm\b",
    ],
}


def classify_task(prompt: str) -> dict[str, float]:
    """
    Classify a prompt into a weighted distribution over task types.

    Returns a normalised dict, e.g.:
        {"reasoning": 0.6, "creative": 0.4}

    Rather than forcing one label, this exposes the true ambiguity so the
    router can make a blended or dominant-weighted decision downstream.
    Falls back to {"reasoning": 1.0} when nothing matches.
    """
    lower = prompt.lower()
    raw: dict[str, float] = {}

    for task_type, patterns in TASK_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                raw[task_type] = raw.get(task_type, 0.0) + 1.0

    total = sum(raw.values())
    if total == 0:
        return {"reasoning": 1.0}  # safest default — sonnet handles unknown tasks well

    return {k: v / total for k, v in sorted(raw.items(), key=lambda x: -x[1])}


def dominant_task(weights: dict[str, float]) -> str:
    """Return the highest-weight task type from a classify_task() result."""
    return max(weights, key=weights.get)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_context(
    prompt: str,
    system_prompt: Optional[str] = None,
    conversation_history: Optional[list] = None,
    max_history_turns: int = 10,
) -> dict:
    """
    Builds a lean context dict for the model call.
    Trims conversation history and system prompt based on dominant task type.

    Returns a dict ready to pass to the model API, including the full
    task_weights distribution for downstream use.
    """
    weights = classify_task(prompt)
    dom = dominant_task(weights)

    history = conversation_history or []
    if dom in ("memory_ops", "simple_retrieval", "structured_output"):
        history = history[-4:]
    else:
        history = history[-max_history_turns:]

    trimmed_system = system_prompt or ""
    if dom == "memory_ops" and trimmed_system:
        trimmed_system = re.sub(
            r'#{1,2} (SOUL|IDENTITY|Personality|Vibe|Core Truths|Communication Style)[^\n]*\n.*?(?=\n#{1,2} |\Z)',
            '[personality context omitted]',
            trimmed_system,
            flags=re.DOTALL,
        )

    return {
        "task_type":    dom,        # dominant label — used for logging / routing
        "task_weights": weights,    # full distribution — exposed for inspection
        "prompt": prompt,
        "system_prompt": trimmed_system,
        "conversation_history": history,
    }


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

ROUTING_TABLE = {
    "planning":          ("claude-opus-4-8",   "planning → opus (multi-step structural judgment)"),
    "reasoning":         ("claude-sonnet-4-6", "reasoning → sonnet (opus reserved for planning)"),
    "memory_ops":        ("claude-haiku-4-5",  "memory ops → haiku (cheapest, fast enough)"),
    "simple_retrieval":  ("claude-haiku-4-5",  "simple retrieval → haiku (cheapest)"),
    "creative":          ("claude-sonnet-4-6", "creative → sonnet (nuance without opus cost)"),
    "structured_output": ("claude-sonnet-4-6", "structured output → sonnet (reliable schema adherence)"),
}

DEFAULT_MODEL = ("claude-sonnet-4-6", "unclassified → sonnet (default)")


def select_model(weights: dict[str, float], similar: list) -> tuple[str, str]:
    """
    Select a model from a multi-label task weight distribution.

    ES k-line override fires when a past decision scored above 0.85 similarity
    with confirmed quality. Otherwise routes by dominant task type.

    Surfaces ambiguity in the reason string when no single label dominates.
    """
    # ES override — same logic as before, but now explicit about weights
    for s in similar:
        score   = s.get("kline_score") or s.get("similarity_score", 0)
        quality = s.get("quality_score")
        eff_q   = quality if quality is not None else 0.5
        past_model = s.get("model_selected")
        if score > 0.85 and eff_q >= 0.7 and past_model:
            return past_model, f"ES k-line match (score={score:.2f}, quality={eff_q:.2f})"

    dom = dominant_task(weights)
    model, base_reason = ROUTING_TABLE.get(dom, DEFAULT_MODEL)

    # Surface ambiguity when dominant weight is below 50% — useful for debugging
    if weights.get(dom, 1.0) < 0.5:
        dist = ", ".join(f"{t}={w:.0%}" for t, w in list(weights.items())[:3])
        reason = f"multi-label [{dist}] → dominant={dom} → {model}"
    else:
        reason = base_reason

    return model, reason
