#!/usr/bin/env python3
"""
rl - Reasoning Layer CLI
------------------------
Inspect your reasoning layer from the terminal.

Usage:
    python3 rl.py stats          # overall stats from SQLite + ES
    python3 rl.py recent         # last 10 logged events
    python3 rl.py search <text>  # find similar past requests in ES
    python3 rl.py suggest <text> # what model would the layer pick?
    python3 rl.py cost           # cost breakdown by task type + model
"""

import sys
import os
from datetime import datetime

# ---- optional color support ----
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    RED    = Fore.RED
    GREEN  = Fore.GREEN
    YELLOW = Fore.YELLOW
    CYAN   = Fore.CYAN
    BOLD   = Style.BRIGHT
    RESET  = Style.RESET_ALL
except ImportError:
    RED = GREEN = YELLOW = CYAN = BOLD = RESET = ""

from reasoning_layer import ReasoningLogger

os.environ.setdefault("ES_HOST", "http://localhost:9200")
try:
    from es_layer import ESMemoryNode
    es = ESMemoryNode()
    ES_AVAILABLE = True
except Exception as e:
    es = None
    ES_AVAILABLE = False
    ES_ERROR = str(e)


def fmt_cost(val):
    if val is None:
        return "-"
    if val < 0.001:
        return f"{YELLOW}${val:.6f}{RESET}"
    if val > 0.01:
        return f"{RED}${val:.4f}{RESET}"
    return f"${val:.4f}"

def fmt_model(val):
    if not val:
        return "-"
    if "haiku" in val:
        return f"{GREEN}{val}{RESET}"
    if "opus" in val:
        return f"{RED}{val}{RESET}"
    return f"{CYAN}{val}{RESET}"

def fmt_ts(ts_ms):
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%m/%d %H:%M")

def fmt_quality(q):
    if q is None:
        return "-"
    if q >= 0.8:
        return f"{GREEN}{q:.2f}{RESET}"
    if q >= 0.5:
        return f"{YELLOW}{q:.2f}{RESET}"
    return f"{RED}{q:.2f}{RESET}"


def cmd_stats():
    logger = ReasoningLogger()

    # SQLite stats
    recent = logger.recent(limit=1000)
    total = len(recent)
    total_cost = sum(r["cost_usd"] or 0 for r in recent)
    avg_quality = sum(r["quality_score"] or 0 for r in recent if r["quality_score"]) / max(1, sum(1 for r in recent if r["quality_score"]))

    print(f"\n{BOLD}✦ Reasoning Layer — Stats{RESET}")
    print(f"  Events logged : {BOLD}{total}{RESET}")
    print(f"  Total cost    : {fmt_cost(total_cost)}")
    print(f"  Avg quality   : {fmt_quality(avg_quality)}")

    # ES stats if available
    if ES_AVAILABLE:
        try:
            stats = es.stats()
            print(f"\n{BOLD}Elasticsearch Index{RESET}")
            print(f"  Indexed events: {BOLD}{stats['total_events']}{RESET}")
            if stats["by_model"]:
                print(f"  By model:")
                for model, count in stats["by_model"].items():
                    print(f"    {fmt_model(model)}: {count} events")
            if stats["by_task"]:
                print(f"  By task type:")
                for task, count in stats["by_task"].items():
                    print(f"    {CYAN}{task}{RESET}: {count} events")
            if stats["avg_quality"]:
                print(f"  Avg quality   : {fmt_quality(stats['avg_quality'])}")
        except Exception as e:
            print(f"\n  {YELLOW}ES error: {e}{RESET}")
    else:
        print(f"\n  {YELLOW}ES unavailable: {ES_ERROR}{RESET}")

    print()


def cmd_recent():
    logger = ReasoningLogger()
    rows = logger.recent(limit=10)

    if not rows:
        print("No events logged yet.")
        return

    print(f"\n{BOLD}✦ Recent Events{RESET}")
    print(f"  {'TIME':<12} {'CALL SITE':<28} {'MODEL':<25} {'COST':<12} {'Q':<6} {'TASK'}")
    print(f"  {'-'*105}")
    for r in rows:
        print(f"  {fmt_ts(r['ts']):<12} {(r['call_site'] or '-'):<28} {fmt_model(r['model_selected']):<34} {fmt_cost(r['cost_usd']):<20} {fmt_quality(r['quality_score']):<14} {r['task_type'] or '-'}")
    print()


def cmd_search(query: str):
    if not ES_AVAILABLE:
        print(f"ES unavailable: {ES_ERROR}")
        return

    print(f"\n{BOLD}✦ Similar to: \"{query}\"{RESET}")
    results = es.find_similar(query, top_k=5)
    if not results:
        print("  No similar events found yet.")
        return

    print(f"  {'SCORE':<8} {'TASK':<22} {'MODEL':<25} {'CALL SITE'}")
    print(f"  {'-'*80}")
    for r in results:
        score = f"{r['similarity_score']:.3f}"
        print(f"  {CYAN}{score:<8}{RESET} {(r.get('task_type') or '-'):<22} {fmt_model(r.get('model_selected')):<34} {r.get('call_site') or '-'}")
    print()


def cmd_suggest(query: str):
    if not ES_AVAILABLE:
        print(f"ES unavailable: {ES_ERROR}")
        return

    print(f"\n{BOLD}✦ Model suggestion for: \"{query}\"{RESET}")
    suggestion = es.suggest_model(query)
    if suggestion:
        print(f"  Suggested model: {fmt_model(suggestion)}")
    else:
        print(f"  {YELLOW}Not enough data yet - log more events to train the layer.{RESET}")
    print()


def cmd_cost():
    logger = ReasoningLogger()
    rows = logger.cost_by_task()

    if not rows:
        print("No cost data yet.")
        return

    print(f"\n{BOLD}✦ Cost by Task + Model{RESET}")
    print(f"  {'TASK':<25} {'MODEL':<25} {'CALLS':<8} {'TOTAL':<14} {'AVG':<12} {'AVG QUALITY'}")
    print(f"  {'-'*100}")
    for r in rows:
        print(f"  {(r['task_type'] or 'unknown'):<25} {fmt_model(r['model_selected']):<34} {r['calls']:<8} {fmt_cost(r['total_cost']):<20} {fmt_cost(r['avg_cost']):<20} {fmt_quality(r['avg_quality'])}")
    print()


def bar(value, max_value, width=30, color=None):
    """Render an ASCII bar."""
    filled = int((value / max_value) * width) if max_value > 0 else 0
    b = "█" * filled + "░" * (width - filled)
    if color:
        return f"{color}{b}{RESET}"
    return b


# Model cost per 1k tokens (input) - used for savings calculation
MODEL_COST_PER_1K = {
    "claude-opus-4-6":   0.015,
    "claude-sonnet-4-5": 0.003,
    "claude-haiku-4-5":  0.00025,
}
BASELINE_MODEL = "claude-opus-4-6"


def cmd_viz():
    logger = ReasoningLogger()
    rows = logger.recent(limit=1000)

    if not rows:
        print("No events logged yet. Log some events first.")
        return

    print(f"\n{BOLD}✦ Reasoning Layer — Visualization{RESET}\n")

    # ── 1. Cost by model ──────────────────────────────────────────────
    print(f"{BOLD}  Cost by Model{RESET}")
    model_costs = {}
    model_tokens = {}
    for r in rows:
        m = r["model_selected"] or "unknown"
        model_costs[m] = model_costs.get(m, 0) + (r["cost_usd"] or 0)
        model_tokens[m] = model_tokens.get(m, 0) + (r["input_tokens"] or 0) + (r["output_tokens"] or 0)

    max_cost = max(model_costs.values()) if model_costs else 1
    for model, cost in sorted(model_costs.items(), key=lambda x: -x[1]):
        color = RED if "opus" in model else (GREEN if "haiku" in model else CYAN)
        b = bar(cost, max_cost, width=25, color=color)
        print(f"  {model:<28} {b}  {fmt_cost(cost)}")

    # ── 2. Task type distribution ─────────────────────────────────────
    print(f"\n{BOLD}  Task Type Distribution{RESET}")
    task_counts = {}
    for r in rows:
        t = r["task_type"] or "unclassified"
        task_counts[t] = task_counts.get(t, 0) + 1

    max_count = max(task_counts.values()) if task_counts else 1
    total_events = len(rows)
    for task, count in sorted(task_counts.items(), key=lambda x: -x[1]):
        pct = count / total_events * 100
        b = bar(count, max_count, width=25, color=CYAN)
        print(f"  {task:<28} {b}  {count} ({pct:.0f}%)")

    # ── 3. Quality over time (bucketed into 5 windows) ────────────────
    quality_rows = [r for r in rows if r["quality_score"] is not None]
    if quality_rows:
        print(f"\n{BOLD}  Quality Over Time{RESET}")
        quality_rows_sorted = sorted(quality_rows, key=lambda r: r["ts"])
        bucket_size = max(1, len(quality_rows_sorted) // 5)
        buckets = [quality_rows_sorted[i:i+bucket_size] for i in range(0, len(quality_rows_sorted), bucket_size)][:5]
        for i, bucket in enumerate(buckets):
            avg_q = sum(r["quality_score"] for r in bucket) / len(bucket)
            ts_label = fmt_ts(bucket[0]["ts"])
            color = GREEN if avg_q >= 0.8 else (YELLOW if avg_q >= 0.5 else RED)
            b = bar(avg_q, 1.0, width=25, color=color)
            print(f"  {ts_label:<12} {b}  {fmt_quality(avg_q)}")
    else:
        print(f"\n{YELLOW}  Quality over time: no quality scores logged yet.{RESET}")

    # ── 4. Top call sites by spend ────────────────────────────────────
    print(f"\n{BOLD}  Top Call Sites by Spend{RESET}")
    site_costs = {}
    for r in rows:
        s = r["call_site"] or "unknown"
        site_costs[s] = site_costs.get(s, 0) + (r["cost_usd"] or 0)

    max_site_cost = max(site_costs.values()) if site_costs else 1
    for site, cost in sorted(site_costs.items(), key=lambda x: -x[1])[:8]:
        b = bar(cost, max_site_cost, width=25, color=YELLOW)
        print(f"  {site:<28} {b}  {fmt_cost(cost)}")

    # ── 5. Tokens saved vs Opus baseline ─────────────────────────────
    print(f"\n{BOLD}  Tokens Saved vs Opus Baseline{RESET}")
    baseline_cost_per_1k = MODEL_COST_PER_1K[BASELINE_MODEL]
    actual_total = 0
    baseline_total = 0
    savings_by_model = {}

    for r in rows:
        tokens = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        actual = r["cost_usd"] or 0
        baseline = (tokens / 1000) * baseline_cost_per_1k
        actual_total += actual
        baseline_total += baseline
        m = r["model_selected"] or "unknown"
        if m not in savings_by_model:
            savings_by_model[m] = {"actual": 0, "baseline": 0, "tokens": 0}
        savings_by_model[m]["actual"] += actual
        savings_by_model[m]["baseline"] += baseline
        savings_by_model[m]["tokens"] += tokens

    total_saved = baseline_total - actual_total
    pct_saved = (total_saved / baseline_total * 100) if baseline_total > 0 else 0

    print(f"  Baseline (all Opus)  : {fmt_cost(baseline_total)}")
    print(f"  Actual spend         : {fmt_cost(actual_total)}")
    print(f"  {GREEN}Saved                : {fmt_cost(total_saved)} ({pct_saved:.0f}%){RESET}")
    print()
    print(f"  {'MODEL':<28} {'TOKENS':<12} {'ACTUAL':<14} {'BASELINE':<14} {'SAVED'}")
    print(f"  {'-'*80}")
    for model, d in sorted(savings_by_model.items(), key=lambda x: -x[1]["baseline"]):
        saved = d["baseline"] - d["actual"]
        print(f"  {fmt_model(model):<37} {d['tokens']:<12,} {fmt_cost(d['actual']):<20} {fmt_cost(d['baseline']):<20} {GREEN}{fmt_cost(saved)}{RESET}")

    print()


def print_help():
    print(f"""
{BOLD}✦ rl — Reasoning Layer CLI{RESET}

  {CYAN}python3 rl.py stats{RESET}              Overall stats (SQLite + ES)
  {CYAN}python3 rl.py recent{RESET}             Last 10 logged events
  {CYAN}python3 rl.py search <text>{RESET}      Find semantically similar past requests
  {CYAN}python3 rl.py suggest <text>{RESET}     Get model suggestion for a request
  {CYAN}python3 rl.py cost{RESET}               Cost breakdown by task type + model
  {CYAN}python3 rl.py viz{RESET}                Visual charts: cost, tasks, quality, savings
""")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
    elif args[0] == "stats":
        cmd_stats()
    elif args[0] == "recent":
        cmd_recent()
    elif args[0] == "search":
        if len(args) < 2:
            print("Usage: python3 rl.py search <text>")
        else:
            cmd_search(" ".join(args[1:]))
    elif args[0] == "suggest":
        if len(args) < 2:
            print("Usage: python3 rl.py suggest <text>")
        else:
            cmd_suggest(" ".join(args[1:]))
    elif args[0] == "cost":
        cmd_cost()
    elif args[0] == "viz":
        cmd_viz()
    else:
        print(f"Unknown command: {args[0]}")
        print_help()
