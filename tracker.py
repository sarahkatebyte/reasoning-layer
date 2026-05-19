#!/usr/bin/env python3
"""
Astrid Spend Tracker
A visibility layer into Astrid's LLM spend over time.

Commands:
  python tracker.py poll          - snapshot current spend to SQLite
  python tracker.py show          - print current call-site breakdown
  python tracker.py trend         - show cost delta since last poll
  python tracker.py event <note>  - log an optimization event (for before/after)
  python tracker.py compare       - show spend before/after the last event
"""

import sqlite3
import subprocess
import json
import sys
import time
from datetime import datetime

DB_PATH = "spend.db"


# ─── Schema ────────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,           -- epoch ms
            call_site   TEXT NOT NULL,
            cost_usd    REAL NOT NULL,
            input_tok   INTEGER NOT NULL,
            output_tok  INTEGER NOT NULL,
            cache_write INTEGER NOT NULL,
            cache_read  INTEGER NOT NULL,
            events      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS opt_events (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    INTEGER NOT NULL,
            note  TEXT NOT NULL
        );
    """)
    conn.commit()


# ─── Data fetching ──────────────────────────────────────────────────────────────

ASSISTANT_BIN = "/Users/sarahkate/.local/bin/assistant"

def fetch_breakdown():
    result = subprocess.run(
        [ASSISTANT_BIN, "usage", "breakdown", "--group-by", "call_site", "--range", "week", "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    return data.get("breakdown", [])


# ─── Commands ──────────────────────────────────────────────────────────────────

def cmd_poll(conn):
    rows = fetch_breakdown()
    ts = int(time.time() * 1000)
    for r in rows:
        conn.execute("""
            INSERT INTO snapshots (ts, call_site, cost_usd, input_tok, output_tok, cache_write, cache_read, events)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ts,
            r["group"],
            r["totalEstimatedCostUsd"],
            r["totalInputTokens"],
            r["totalOutputTokens"],
            r["totalCacheCreationTokens"],
            r["totalCacheReadTokens"],
            r["eventCount"],
        ))
    conn.commit()
    print(f"✓ Snapshot stored — {len(rows)} call sites @ {datetime.now().strftime('%H:%M:%S')}")


def cache_hit_rate(r):
    reads = r["totalCacheReadTokens"]
    writes = r["totalCacheCreationTokens"]
    total = reads + writes
    return (reads / total * 100) if total > 0 else 0.0

def cost_per_call(r):
    return r["totalEstimatedCostUsd"] / r["eventCount"] if r["eventCount"] > 0 else 0.0

def cmd_show(conn):
    rows = fetch_breakdown()
    total = sum(r["totalEstimatedCostUsd"] for r in rows)
    print(f"\n{'CALL SITE':<32} {'COST':>8}  {'$/CALL':>7}  {'CALLS':>6}  {'CACHE HIT':>10}")
    print("─" * 74)
    for r in sorted(rows, key=lambda x: x["totalEstimatedCostUsd"], reverse=True):
        hit = cache_hit_rate(r)
        cpc = cost_per_call(r)
        # Flag low cache hit rate (< 50%) as a waste signal
        cache_flag = " ⚠" if hit < 50 and (r["totalCacheCreationTokens"] + r["totalCacheReadTokens"]) > 0 else ""
        print(f"{r['group']:<32} ${r['totalEstimatedCostUsd']:>7.2f}  ${cpc:>6.4f}  {r['eventCount']:>6}  {hit:>8.1f}%{cache_flag}")
    print("─" * 74)
    print(f"{'TOTAL':<32} ${total:>7.2f}")
    print()


def cmd_trend(conn):
    # Compare the two most recent distinct snapshots
    cursor = conn.execute("SELECT DISTINCT ts FROM snapshots ORDER BY ts DESC LIMIT 2")
    timestamps = [r[0] for r in cursor.fetchall()]
    if len(timestamps) < 2:
        print("Need at least 2 snapshots. Run `poll` again later.")
        return

    ts_new, ts_old = timestamps[0], timestamps[1]

    def get_snap(ts):
        rows = conn.execute(
            "SELECT call_site, cost_usd, events FROM snapshots WHERE ts = ?", (ts,)
        ).fetchall()
        return {r[0]: {"cost": r[1], "events": r[2]} for r in rows}

    old = get_snap(ts_old)
    new = get_snap(ts_new)
    all_sites = sorted(set(list(old.keys()) + list(new.keys())))

    dt_old = datetime.fromtimestamp(ts_old / 1000).strftime("%H:%M:%S")
    dt_new = datetime.fromtimestamp(ts_new / 1000).strftime("%H:%M:%S")

    print(f"\nDelta: {dt_old} → {dt_new}")
    print(f"\n{'CALL SITE':<32} {'BEFORE':>8}  {'AFTER':>8}  {'DELTA':>8}")
    print("─" * 64)
    total_delta = 0
    for site in all_sites:
        c_old = old.get(site, {}).get("cost", 0)
        c_new = new.get(site, {}).get("cost", 0)
        delta = c_new - c_old
        total_delta += delta
        marker = " ▲" if delta > 0.01 else (" ▼" if delta < -0.01 else "")
        print(f"{site:<32} ${c_old:>7.2f}  ${c_new:>7.2f}  ${delta:>+7.2f}{marker}")
    print("─" * 64)
    print(f"{'TOTAL DELTA':<32} {'':>8}  {'':>8}  ${total_delta:>+7.2f}")
    print()


def cmd_event(conn, note):
    ts = int(time.time() * 1000)
    conn.execute("INSERT INTO opt_events (ts, note) VALUES (?, ?)", (ts, note))
    conn.commit()
    print(f"✓ Event logged: \"{note}\"")
    print("  Run `poll` now (before), apply your change, `poll` again (after), then `compare`.")


def cmd_compare(conn):
    # Find the most recent opt_event, then compare snapshots before/after it
    row = conn.execute("SELECT ts, note FROM opt_events ORDER BY ts DESC LIMIT 1").fetchone()
    if not row:
        print("No optimization events logged. Use: python tracker.py event 'your note'")
        return

    event_ts, note = row
    dt_event = datetime.fromtimestamp(event_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")

    before = conn.execute(
        "SELECT call_site, cost_usd FROM snapshots WHERE ts <= ? ORDER BY ts DESC",
        (event_ts,)
    ).fetchall()
    after = conn.execute(
        "SELECT call_site, cost_usd FROM snapshots WHERE ts > ? ORDER BY ts ASC",
        (event_ts,)
    ).fetchall()

    if not before or not after:
        print("Need snapshots both before and after the event. Run `poll` on each side.")
        return

    def to_dict(rows):
        d = {}
        for site, cost in rows:
            if site not in d:  # take first (closest to event)
                d[site] = cost
        return d

    b = to_dict(before)
    a = to_dict(after)
    all_sites = sorted(set(list(b.keys()) + list(a.keys())))

    print(f"\nOptimization event: \"{note}\" @ {dt_event}")
    print(f"\n{'CALL SITE':<32} {'BEFORE':>8}  {'AFTER':>8}  {'DELTA':>8}  {'SAVED':>8}")
    print("─" * 72)
    total_before = total_after = 0
    for site in all_sites:
        c_b = b.get(site, 0)
        c_a = a.get(site, 0)
        delta = c_a - c_b
        total_before += c_b
        total_after += c_a
        marker = " ▲" if delta > 0.01 else (" ✓" if delta < -0.01 else "")
        print(f"{site:<32} ${c_b:>7.2f}  ${c_a:>7.2f}  ${delta:>+7.2f}{marker}")
    print("─" * 72)
    saved = total_before - total_after
    print(f"{'TOTAL':<32} ${total_before:>7.2f}  ${total_after:>7.2f}  ${saved:>+7.2f}  {'saved' if saved > 0 else 'increase'}")
    print()


# ─── Entry point ───────────────────────────────────────────────────────────────

def cmd_watch(conn, args):
    """Poll on an interval. Default: every 5 minutes. Usage: watch [interval_seconds]"""
    interval = int(args[0]) if args else 300
    print(f"Watching — polling every {interval}s. Ctrl+C to stop.\n")
    while True:
        conn2 = sqlite3.connect(DB_PATH)
        init_db(conn2)
        cmd_poll(conn2)
        conn2.close()
        time.sleep(interval)

COMMANDS = {
    "poll":    lambda conn, _: cmd_poll(conn),
    "show":    lambda conn, _: cmd_show(conn),
    "trend":   lambda conn, _: cmd_trend(conn),
    "event":   lambda conn, args: cmd_event(conn, " ".join(args) if args else "unnamed"),
    "compare": lambda conn, _: cmd_compare(conn),
    "watch":   lambda conn, args: cmd_watch(conn, args),
}

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "show"
    rest = args[1:]

    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    COMMANDS[cmd](conn, rest)
    conn.close()
