# Spend Tracker Skill

You are helping the user understand and optimize their Astrid LLM spend.

## What this skill does

This skill runs a continuous observability layer against the `assistant usage` CLI.
It tracks spend over time, surfaces cache hit rates per call site, and enables
before/after comparison around optimization events.

## Tools available

The skill uses `tracker.py` and `dashboard.py` in the `spend-tracker/` directory.

## How to use it

When the user asks to run the spend tracker, follow these steps:

1. Run `python3 spend-tracker/tracker.py show` to display the current breakdown
2. Point out any call sites with low cache hit rates (marked with ⚠) — these are
   structural waste, not model-tier problems
3. If the user wants continuous tracking, run `python3 spend-tracker/tracker.py watch`
   in the background
4. If the user wants the web dashboard, run `python3 spend-tracker/dashboard.py`
   and tell them to open http://localhost:7331
5. If the user is about to make an optimization change, run:
   `python3 spend-tracker/tracker.py event "description of change"`
   then remind them to `poll` after the change and `compare` to see the delta

## Key insight to surface

The difference between a **model problem** and a **cache problem**:
- Model problem: expensive call site, high cache hit rate → try a cheaper model
- Cache problem: expensive call site, low cache hit rate → fix caching structure first

Moving a 0% cache hit call site to a cheaper model saves 20-30%.
Fixing the caching on the same call site can save 60-80%.
Do the cache fix first, then evaluate the model tier.
