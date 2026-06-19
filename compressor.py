"""
Reasoning Layer - Compressor Node
-----------------------------------
Strips unnecessary context from requests before they hit the model.
Can also block call sites entirely - returning blocked=True tells the
pipeline to skip the LLM call altogether.

The problem: background tasks like Memory Consolidation, Conversation Title,
and Notification Decision are sending 25k-160k input tokens when they only
need a fraction of that context to do their job. Some (Reply Suggestion)
provide so little signal they shouldn't run at all.

This node intercepts the request, identifies the task type, and either:
  (a) Returns a compressed version of the context, or
  (b) Returns blocked=True — caller must check this and skip the LLM call.

Real data from spend tracker (avg input tokens per call site):
  Conversation Summarization : 160,827 tokens  → target: 20,000
  Reply Suggestion           : 125,077 tokens  → BLOCKED (disabled entirely)
  Conversation Title         :  91,816 tokens  → target:  2,000
  Notification Decision      :  55,781 tokens  → target:  5,000
  Memory Consolidation       :  25,041 tokens  → target:  3,000
  Memory Retrieval           :   8,639 tokens  → target:  2,000
  Memory Extraction          :   7,917 tokens  → target:  2,000

Estimated weekly savings at current volume: significant.
"""

from dataclasses import dataclass
import re
import json
import os

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Token estimation and trimming — tiktoken if available, char/4 fallback
# ---------------------------------------------------------------------------

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(text: str) -> int:
        return len(_enc.encode(text))

    def _trim_to_tokens(text: str, max_tokens: int) -> str:
        """Trim from the front to at most max_tokens."""
        tokens = _enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _enc.decode(tokens[:max_tokens])

    def _trim_tail_to_tokens(text: str, max_tokens: int) -> str:
        """Keep only the last max_tokens (trim leading content)."""
        tokens = _enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _enc.decode(tokens[-max_tokens:])

except ImportError:
    def estimate_tokens(text: str) -> int:
        return len(text) // 4

    def _trim_to_tokens(text: str, max_tokens: int) -> str:
        max_chars = max_tokens * 4
        return text[:max_chars] if len(text) > max_chars else text

    def _trim_tail_to_tokens(text: str, max_tokens: int) -> str:
        max_chars = max_tokens * 4
        return text[-max_chars:] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Compression result
# ---------------------------------------------------------------------------

@dataclass
class CompressionResult:
    original_text: str
    compressed_text: str
    task_type: str
    call_site: str
    original_tokens: int
    compressed_tokens: int
    tokens_saved: int
    compression_ratio: float
    strategy_applied: str
    blocked: bool = False  # True = caller must skip the LLM call entirely

    def summary(self) -> str:
        if self.blocked:
            return f"[{self.call_site}] BLOCKED — {self.original_tokens:,} tokens saved (call skipped entirely)"
        return (
            f"[{self.call_site}] {self.strategy_applied}: "
            f"{self.original_tokens:,} → {self.compressed_tokens:,} tokens "
            f"({self.tokens_saved:,} saved, {self.compression_ratio:.0%} reduction)"
        )


# ---------------------------------------------------------------------------
# Compression strategies
# ---------------------------------------------------------------------------

class CompressionStrategy:
    """Base class. Each strategy knows how to compress a specific call site."""

    name = "base"

    def compress(self, text: str) -> str:
        raise NotImplementedError


class TruncateStrategy(CompressionStrategy):
    """Hard truncate to a max token budget. Blunt but effective."""

    name = "truncate"

    def __init__(self, max_tokens: int = 5000):
        self.max_tokens = max_tokens

    def compress(self, text: str) -> str:
        if estimate_tokens(text) <= self.max_tokens:
            return text
        return _trim_to_tokens(text, self.max_tokens) + "\n\n[... context truncated by Reasoning Layer compressor ...]"


class ConversationTitleStrategy(CompressionStrategy):
    """
    Conversation Title only needs the first and last few exchanges.
    It does NOT need the full conversation history.
    Target: 2,000 tokens from 91,816.
    """

    name = "conversation_title"
    MAX_TOKENS = 2000
    # Head+tail: 1000 tokens each — title signal comes from opening topic and recent turn
    _HEAD_TOKENS = 1000
    _TAIL_TOKENS = 1000

    def compress(self, text: str) -> str:
        if estimate_tokens(text) <= self.MAX_TOKENS:
            return text

        head = _trim_to_tokens(text, self._HEAD_TOKENS)
        tail = _trim_tail_to_tokens(text, self._TAIL_TOKENS)
        return f"{head}\n\n[... middle of conversation omitted ...]\n\n{tail}"


class NotificationDecisionStrategy(CompressionStrategy):
    """
    Notification Decision needs: recent message + user prefs.
    Does NOT need: full conversation history, memory context, system details.
    Target: 5,000 tokens from 55,781.
    """

    name = "notification_decision"
    MAX_TOKENS = 5000

    def compress(self, text: str) -> str:
        if estimate_tokens(text) <= self.MAX_TOKENS:
            return text
        # Keep the last portion — most recent context is what matters for notifications
        return _trim_tail_to_tokens(text, self.MAX_TOKENS) + "\n\n[... earlier context omitted by compressor ...]"


class MemoryOpsStrategy(CompressionStrategy):
    """
    Memory Consolidation, Extraction, Retrieval.
    Needs: recent memory entries + filing schema.
    Does NOT need: full conversation, system prompt personality sections.
    Target: 3,000 tokens from 25,041.
    """

    name = "memory_ops"
    MAX_TOKENS = 3000

    def compress(self, text: str) -> str:
        if estimate_tokens(text) <= self.MAX_TOKENS:
            return text

        # Strip anything that looks like large personality/soul sections
        stripped = re.sub(
            r'#{1,2} (SOUL|IDENTITY|Personality|Vibe|Core Truths|Communication Style)[^\n]*\n.*?(?=\n#{1,2} |\Z)',
            '[personality context omitted]',
            text,
            flags=re.DOTALL
        )

        if estimate_tokens(stripped) > self.MAX_TOKENS:
            stripped = _trim_to_tokens(stripped, self.MAX_TOKENS) + "\n[... truncated ...]"

        return stripped


class ReplyAndSummaryStrategy(CompressionStrategy):
    """
    Reply Suggestion and Conversation Summarization.
    Still needs substantial context but not 125k-160k tokens worth.
    Target: 10,000-20,000 tokens.
    """

    name = "reply_summary"

    def __init__(self, max_tokens: int = 15000):
        self.max_tokens = max_tokens

    def compress(self, text: str) -> str:
        total_tokens = estimate_tokens(text)
        if total_tokens <= self.max_tokens:
            return text

        # Keep head (system context, ~20%) and tail (recent conversation, ~80%)
        head_tokens = self.max_tokens // 5
        tail_tokens = self.max_tokens - head_tokens

        head = _trim_to_tokens(text, head_tokens)
        tail = _trim_tail_to_tokens(text, tail_tokens)
        omitted = total_tokens - head_tokens - tail_tokens
        return f"{head}\n\n[... ~{omitted:,} tokens of middle context omitted ...]\n\n{tail}"


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

STRATEGIES = {
    "Conversation Title":         ConversationTitleStrategy(),
    "Notification Decision":      NotificationDecisionStrategy(),
    "Memory Consolidation":       MemoryOpsStrategy(),
    "Memory Retrieval":           MemoryOpsStrategy(),
    "Memory Extraction":          MemoryOpsStrategy(),
    "Filing Agent":               MemoryOpsStrategy(),
    "Conversation Summarization": ReplyAndSummaryStrategy(max_tokens=20000),
    "Recall":                     TruncateStrategy(max_tokens=5000),
    "Heartbeat Agent":            TruncateStrategy(max_tokens=3000),
    "Pattern Scan":               TruncateStrategy(max_tokens=3000),
}

# Call sites that should not run at all.
# compress() returns blocked=True — the caller is responsible for skipping the LLM call.
BLOCKED_CALL_SITES: set[str] = {
    "Reply Suggestion",
}

DEFAULT_STRATEGY = TruncateStrategy(max_tokens=10000)


# ---------------------------------------------------------------------------
# Compressor Node - the public interface
# ---------------------------------------------------------------------------

class CompressorNode:
    """
    Sits between the Classifier and the Router.
    Takes a request, applies the right compression strategy, returns
    a CompressionResult with the lean version ready to send.

    Usage:
        compressor = CompressorNode()
        result = compressor.compress(
            text=full_request_text,
            call_site="Conversation Title"
        )
        # use result.compressed_text for the actual LLM call
        print(result.summary())
    """

    def __init__(
        self,
        strategies: dict = None,
        blocked_call_sites: set = None,
        enabled: bool = True,
    ):
        self.strategies = strategies or STRATEGIES
        self.blocked_call_sites = blocked_call_sites if blocked_call_sites is not None else BLOCKED_CALL_SITES
        self.enabled = enabled

    @classmethod
    def from_config(cls, path: str) -> "CompressorNode":
        """
        Load a CompressorNode from a YAML or JSON config file.

        Supported strategy names:
          head_tail     - ConversationTitleStrategy (good for title/label tasks)
          notification  - NotificationDecisionStrategy (keep recent context only)
          memory_ops    - MemoryOpsStrategy (strip personality sections)
          reply_summary - ReplyAndSummaryStrategy (head+tail balance)
          truncate      - TruncateStrategy (hard cut, default fallback)

        Example config (YAML):
          call_sites:
            "My Title Generator":
              strategy: head_tail
              max_tokens: 2000
            "My Reply Bot":
              blocked: true
            "My Memory Agent":
              strategy: memory_ops
              max_tokens: 3000
        """
        _, ext = os.path.splitext(path)

        with open(path, "r") as f:
            if ext in (".yaml", ".yml"):
                if not _YAML_AVAILABLE:
                    raise ImportError("pyyaml is required for YAML configs: pip install pyyaml")
                raw = yaml.safe_load(f)
            else:
                raw = json.load(f)

        strategy_map = {
            "head_tail":     lambda max_tokens=2000, **_: ConversationTitleStrategy(),
            "notification":  lambda max_tokens=5000, **_: NotificationDecisionStrategy(),
            "memory_ops":    lambda max_tokens=3000, **_: MemoryOpsStrategy(),
            "reply_summary": lambda max_tokens=15000, **kw: ReplyAndSummaryStrategy(max_tokens=kw.get("max_tokens", max_tokens)),
            "truncate":      lambda max_tokens=5000, **kw: TruncateStrategy(max_tokens=kw.get("max_tokens", max_tokens)),
        }

        strategies = {}
        blocked_call_sites = set()

        for call_site, cfg in raw.get("call_sites", {}).items():
            if cfg.get("blocked", False):
                blocked_call_sites.add(call_site)
                continue
            strategy_name = cfg.get("strategy", "truncate")
            factory = strategy_map.get(strategy_name)
            if factory is None:
                raise ValueError(f"Unknown strategy '{strategy_name}' for call site '{call_site}'. "
                                 f"Valid options: {list(strategy_map.keys())}")
            strategies[call_site] = factory(**cfg)

        return cls(
            strategies=strategies,
            blocked_call_sites=blocked_call_sites,
            enabled=raw.get("enabled", True),
        )

    def compress(
        self,
        text: str,
        call_site: str,
        task_type: str = None,
        min_savings_tokens: int = 500,   # don't compress unless we save at least this many tokens
    ) -> CompressionResult:

        original_tokens = estimate_tokens(text)

        # Check blocked list first - return early, caller must skip the LLM call
        if self.enabled and call_site in self.blocked_call_sites:
            return CompressionResult(
                original_text=text,
                compressed_text="",
                task_type=task_type or "unknown",
                call_site=call_site,
                original_tokens=original_tokens,
                compressed_tokens=0,
                tokens_saved=original_tokens,
                compression_ratio=1.0,
                strategy_applied="blocked",
                blocked=True,
            )

        if not self.enabled:
            return CompressionResult(
                original_text=text,
                compressed_text=text,
                task_type=task_type or "unknown",
                call_site=call_site,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                tokens_saved=0,
                compression_ratio=0.0,
                strategy_applied="disabled"
            )

        strategy = self.strategies.get(call_site, DEFAULT_STRATEGY)
        compressed_text = strategy.compress(text)
        compressed_tokens = estimate_tokens(compressed_text)
        tokens_saved = original_tokens - compressed_tokens
        ratio = tokens_saved / original_tokens if original_tokens > 0 else 0.0

        # If savings are below threshold, don't bother
        if tokens_saved < min_savings_tokens:
            compressed_text = text
            compressed_tokens = original_tokens
            tokens_saved = 0
            ratio = 0.0
            strategy_name = "passthrough (savings below threshold)"
        else:
            strategy_name = strategy.name

        return CompressionResult(
            original_text=text,
            compressed_text=compressed_text,
            task_type=task_type or "unknown",
            call_site=call_site,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            tokens_saved=tokens_saved,
            compression_ratio=ratio,
            strategy_applied=strategy_name,
        )

    def savings_projection(self, avg_tokens_by_callsite: dict, calls_per_week: int = 482) -> dict:
        """
        Project weekly token savings based on real averages.
        Pass in the dict from your spend tracker query.
        """
        results = {}
        total_saved = 0

        for call_site, avg_input_tokens in avg_tokens_by_callsite.items():
            # Simulate compression on a fake string of that length
            fake_text = "x" * (avg_input_tokens * 4)
            result = self.compress(fake_text, call_site)
            weekly_saved = result.tokens_saved * calls_per_week
            results[call_site] = {
                "avg_input_tokens": avg_input_tokens,
                "compressed_tokens": result.compressed_tokens,
                "tokens_saved_per_call": result.tokens_saved,
                "weekly_tokens_saved": weekly_saved,
                "compression_ratio": result.compression_ratio,
                "strategy": result.strategy_applied,
            }
            total_saved += weekly_saved

        results["__total_weekly_tokens_saved__"] = total_saved
        return results


# ---------------------------------------------------------------------------
# Smoke test + savings projection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    compressor = CompressorNode()

    # Real averages from spend tracker
    real_averages = {
        "Conversation Summarization": 160827,
        "Reply Suggestion":           125077,  # blocked - will show as full savings
        "Conversation Title":          91816,
        "Notification Decision":       55781,
        "Memory Consolidation":        25041,
        "Memory Retrieval":             8639,
        "Memory Extraction":            7917,
        "Recall":                       2169,
        "Heartbeat Agent":              1093,
        "Filing Agent":                  270,
    }

    print("\n✦ Compressor Node — Savings Projection\n")
    print(f"  {'CALL SITE':<30} {'AVG IN':<10} {'COMPRESSED':<12} {'SAVED/CALL':<12} {'WEEKLY SAVED':<14} {'RATIO'}")
    print(f"  {'-'*90}")

    projection = compressor.savings_projection(real_averages)
    for call_site, data in projection.items():
        if call_site.startswith("__"):
            continue
        if data['strategy'] == 'blocked':
            ratio_str = "BLOCKED"
            compressed_str = "0 (skipped)"
        else:
            ratio_str = f"{data['compression_ratio']:.0%}" if data['compression_ratio'] > 0 else "-"
            compressed_str = f"{data['compressed_tokens']:,}"
        print(
            f"  {call_site:<30} "
            f"{data['avg_input_tokens']:<10,} "
            f"{compressed_str:<12} "
            f"{data['tokens_saved_per_call']:<12,} "
            f"{data['weekly_tokens_saved']:<14,} "
            f"{ratio_str}"
        )

    total = projection["__total_weekly_tokens_saved__"]
    print(f"\n  {'Total weekly tokens saved':<30} {total:,}")
    print(f"  {'Approx weekly cost saved (Opus)':<30} ${total / 1000 * 0.015:.2f}")
    print(f"  {'Approx weekly cost saved (Haiku)':<30} ${total / 1000 * 0.00025:.2f}")
    print()
