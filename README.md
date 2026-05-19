# Spend Tracker

A continuous observability layer for Astrid's LLM spend — tracking not just how much you're spending, but **where** and **why**.

## The problem

Most LLM cost tooling looks at the receipt and says "buy cheaper cuts." But that misses the real story. Some call sites are expensive because they're on the wrong model. Others are expensive because **they're not using the cache at all** — buying fresh at full price every single time instead of stocking the freezer.

Vellum's [llm-cost-optimizer](https://github.com/vellum-ai/llm-cost-optimizer) is a great start: it's a pull-based skill that maps call sites to model profiles. This tool is the continuous layer on top — time-series snapshots, cache hit rate analysis, and before/after comparison around optimization events.

## What it surfaces

```
CALL SITE                        COST   $/CALL   CALLS   CACHE HIT
────────────────────────────────────────────────────────────────────────
Main Agent                   $  67.82  $0.0634    1070      93.3%
Conversation Summarization   $  29.74  $0.0720     413       0.0% ⚠
Memory Consolidation         $  25.89  $0.0304     852       0.0% ⚠
Memory Extraction            $  16.54  $0.0537     308      36.6% ⚠
```

The `⚠` on Conversation Summarization and Memory Consolidation isn't a model problem — it's a caching problem. $55/week burning at 0% cache hit rate. Moving those to Haiku without fixing the caching first would save ~30% while leaving the structural waste untouched.

## Commands

```bash
python3 tracker.py poll          # snapshot current spend to SQLite
python3 tracker.py show          # print call-site breakdown with cache hit rates
python3 tracker.py trend         # cost delta between last two snapshots
python3 tracker.py event "note"  # log an optimization event
python3 tracker.py compare       # before/after the last event
python3 tracker.py watch [secs]  # continuous polling daemon (default: 300s)

python3 dashboard.py             # web UI at http://localhost:7331
```

## The workflow

```
poll → event "your change" → apply change → poll → compare
```

That's the closed loop. The `llm-cost-optimizer` gives you the recommendation. This shows you whether it worked — and whether the real problem was the model or the cache.

## Install as a Vellum skill

```bash
assistant skills add sarahkatebyte/spend-tracker
```

Then ask Astrid: *"Load the spend-tracker skill and run it"*

## Requirements

- Vellum assistant (local)
- `assistant` CLI
- Python 3.x (stdlib only — no pip install needed)
