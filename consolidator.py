"""
Reasoning Layer - Consolidator
--------------------------------
The NREM pass. Reads phosphorylated episodic events and extracts durable
semantic facts and procedural patterns before the episodic details fade.

Two phases per run:
  1. NREM pass   — extract semantic/procedural value from phosphorylated events
  2. Tier decay  — demote cold memories to warm / backlog

Run manually:
    python3 consolidator.py [--batch-size 50] [--dry-run]

Schedule (cron example — every hour):
    0 * * * * cd /path/to/reasoning-layer && .venv/bin/python3 consolidator.py

Or trigger on-demand:
    POST /consolidate
    curl -X POST http://localhost:8000/consolidate
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import anthropic as anthropic_sdk

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

log = logging.getLogger("consolidator")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

EXTRACTION_MODEL = "claude-haiku-4-5"
DEFAULT_BATCH_SIZE = 50
MIN_TEXT_LENGTH = 50


class Consolidator:
    """
    NREM consolidation pass.

    Reads phosphorylated episodic events, calls Haiku to extract semantic
    facts and procedural patterns, writes them to their respective stores,
    and clears the phosphorylation flag.

    Also runs a tier decay pass — demotes memories that haven't been
    activated within their type-specific thresholds.
    """

    def __init__(self, es, anthropic_client):
        self.es = es
        self.client = anthropic_client

    async def run(self, batch_size: int = DEFAULT_BATCH_SIZE, dry_run: bool = False) -> dict:
        """Full consolidation pass. Returns a summary of what was done."""
        log.info("Starting consolidation pass (batch_size=%d, dry_run=%s)", batch_size, dry_run)
        t0 = time.time()

        nrem = await self._nrem_pass(batch_size, dry_run)
        decay = self.es.decay_tiers() if not dry_run else {"dry_run": True}

        elapsed = round(time.time() - t0, 2)
        summary = {"nrem": nrem, "tier_decay": decay, "elapsed_seconds": elapsed}
        log.info("Consolidation complete in %ss: %s", elapsed, summary)
        return summary

    # ------------------------------------------------------------------
    # NREM pass
    # ------------------------------------------------------------------

    async def _nrem_pass(self, batch_size: int, dry_run: bool) -> dict:
        pending = self.es.get_phosphorylated(limit=batch_size)

        if not pending:
            log.info("No phosphorylated events pending consolidation")
            return {"processed": 0, "facts_stored": 0, "skills_stored": 0, "skipped": 0}

        log.info("Processing %d phosphorylated events", len(pending))
        processed = facts_stored = skills_stored = skipped = 0

        for event in pending:
            event_id = str(event.get("_id") or event.get("event_id", ""))
            text = event.get("request_text", "").strip()

            if len(text) < MIN_TEXT_LENGTH:
                if not dry_run:
                    self.es.clear_phosphorylation(event_id)
                skipped += 1
                continue

            try:
                extraction = await self._extract(text, event)
                facts  = [f for f in extraction.get("facts",  []) if f.get("text")]
                skills = [s for s in extraction.get("skills", []) if s.get("name") and s.get("description")]

                if not dry_run:
                    for fact in facts:
                        self.es.store_semantic(
                            fact_text=fact["text"],
                            subject=fact.get("subject"),
                            confidence=float(fact.get("confidence", 0.8)),
                            source_event_ids=[event_id],
                        )
                    for skill in skills:
                        self.es.store_procedural(
                            skill_name=skill["name"],
                            skill_description=skill["description"],
                            proficiency_score=float(skill.get("proficiency", 0.7)),
                            source_event_ids=[event_id],
                        )
                    self.es.clear_phosphorylation(event_id)

                facts_stored  += len(facts)
                skills_stored += len(skills)
                processed += 1

                log.debug("event %s → %d facts, %d skills", event_id, len(facts), len(skills))

            except Exception as e:
                log.error("Failed to consolidate event %s: %s", event_id, e)
                # Leave phosphorylation flag set — will be retried next pass

        return {
            "processed": processed,
            "facts_stored": facts_stored,
            "skills_stored": skills_stored,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Extraction (Haiku — cheap, fast, sufficient for structured extraction)
    # ------------------------------------------------------------------

    async def _extract(self, text: str, event: dict) -> dict:
        """
        Ask Claude Haiku to extract durable semantic facts and procedural
        patterns from a single episodic event.
        """
        task_type  = event.get("task_type", "unknown")
        call_site  = event.get("call_site", "unknown")

        prompt = f"""Extract durable knowledge from this agent interaction.

Task type: {task_type}
Call site: {call_site}
Content:
{text[:2000]}

Extract ONLY knowledge that is worth remembering across future sessions.
Skip anything specific to this single moment.

Return JSON:
{{
  "facts": [
    {{"text": "...", "subject": "...", "confidence": 0.0-1.0}}
  ],
  "skills": [
    {{"name": "slug-style-name", "description": "...", "proficiency": 0.0-1.0}}
  ]
}}

If nothing is worth extracting, return {{"facts": [], "skills": []}}"""

        resp = await self.client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = next((b.text for b in resp.content if b.type == "text"), "{}")

        # Strip markdown fences if present
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {"facts": [], "skills": []}

        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            log.warning("Failed to parse extraction JSON from event %s", event.get("_id"))
            return {"facts": [], "skills": []}


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the NREM consolidation pass")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be extracted, don't write")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    try:
        from es_layer import ESMemoryLayer
        es = ESMemoryLayer()
    except Exception as e:
        print(f"ERROR: Could not connect to Elasticsearch: {e}")
        sys.exit(1)

    anthropic_client = anthropic_sdk.AsyncAnthropic(api_key=api_key)
    consolidator = Consolidator(es=es, anthropic_client=anthropic_client)
    result = await consolidator.run(batch_size=args.batch_size, dry_run=args.dry_run)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
