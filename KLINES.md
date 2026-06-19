# K-Lines: Andrew's Feedback (Society of Mind as spec)
> Marvin Minsky, *The Society of Mind*

---

## What a K-Line Is

A **K-line** (knowledge line) is the full configuration of agents/state that was active when a goal was successfully achieved. Not just the answer — the entire cognitive setup that produced it.

**Current system captures:**
- request text (embedded)
- model selected
- quality score

**K-line captures:**
- model selected
- tools active (file access, web search, memory lookup, etc.)
- context loaded (which pkb files, which memory entries)
- MCP skills that fired
- turn number in conversation
- task type + complexity

When a similar future request comes in, you don't just recall "what model worked" — you re-activate the whole configuration that worked.

---

## Negative K-Lines

Save what *didn't* work, not just what did. Active avoidance, not just low weight.

- Positive K-line: this configuration → good outcome → increase recall probability
- Negative K-line: this configuration → bad outcome → route around it next time

Current `quality_ok=False` is too passive (just sets score to 0.1). A real negative K-line should actively suppress that configuration in future routing, even if the model was otherwise fine.

**Example:** web search fired + memory ops task = bad outcome. Future memory ops requests should avoid web search, independent of model quality.

---

## Activation Strengthens Recall

When a K-line gets activated (used successfully), it becomes **more likely to be recalled again**. Like practicing an instrument — repetition builds the pathway.

**Implementation:** boost a K-line's weight/score each time it successfully fires. The more a configuration works, the more confidently the router recommends it.

---

## Unused K-Lines Decay

Inverse must also be true: K-lines that go unused decay over time. Otherwise you accumulate permanent relevance from stale configurations — 5-year-old data bloating the search space.

**Implementation:** time-weighted scoring. Recent successful activations count more than old ones. Score = `quality_score * recency_weight` where recency drops off with age.

---

## Architectural Implications

### `select_model` → `select_configuration`

Returns the full K-line recommendation, not just a model:

```python
{
    "model": "claude-sonnet-4-5",
    "suggested_tools": ["file_access", "memory_lookup"],
    "avoid_tools": ["web_search"],        # negative K-line signal
    "load_context": ["career.md", "threads.md"],
    "confidence": 0.85
}
```

### ES Index Expansion

Add to `index_event`:
- `tools_used: list[str]`
- `context_sources: list[str]`
- `skills_fired: list[str]`
- `turn_number: int`
- `is_negative: bool`
- `activation_count: int` — incremented each time this K-line fires successfully
- `last_activated_ts: epoch_ms` — for decay calculation

### Scoring Formula

```
kline_score = base_quality * activation_weight * recency_weight

activation_weight = log(1 + activation_count)   # diminishing returns on repetition
recency_weight    = e^(-λ * days_since_use)      # exponential decay, tune λ
```

Negative K-lines: store separately or flag with `is_negative=True` and use as exclusion filters in routing queries.

---

## Reference

Minsky, M. (1986). *The Society of Mind.* Simon & Schuster.
— Andrew's recommended reading. He's using it as the spec.
