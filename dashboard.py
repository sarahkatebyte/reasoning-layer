#!/usr/bin/env python3
"""
Astrid Spend Dashboard
Serves a local web UI showing total system spend - why and where.

Usage: python3 dashboard.py
Then open: http://localhost:7331
"""

import sqlite3
import subprocess
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = "spend.db"

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>Astrid Spend</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0f;
    color: #e2e8f0;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px;
    padding: 32px;
  }
  h1 { font-size: 18px; font-weight: 600; color: #a78bfa; margin-bottom: 4px; letter-spacing: 0.05em; }
  .subtitle { color: #64748b; margin-bottom: 32px; font-size: 11px; }
  .total-card {
    background: #13131a;
    border: 1px solid #1e1e2e;
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 24px;
    display: flex;
    gap: 48px;
    align-items: flex-end;
  }
  .metric { display: flex; flex-direction: column; gap: 4px; }
  .metric-value { font-size: 32px; font-weight: 700; color: #f8fafc; }
  .metric-label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.08em; }
  .metric-value.warn { color: #f59e0b; }
  .metric-value.good { color: #34d399; }
  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left; padding: 8px 12px;
    font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; color: #64748b;
    border-bottom: 1px solid #1e1e2e;
  }
  thead th.right { text-align: right; }
  tbody tr:hover { background: #13131a; }
  td { padding: 10px 12px; border-bottom: 1px solid #0f0f17; vertical-align: middle; }
  td.right { text-align: right; }
  .bar-wrap { width: 100px; background: #1e1e2e; border-radius: 2px; height: 6px; display: inline-block; vertical-align: middle; }
  .bar-fill { height: 6px; border-radius: 2px; transition: width 0.3s; }
  .bar-cost { background: #7c3aed; }
  .bar-cache-good { background: #34d399; }
  .bar-cache-bad { background: #f59e0b; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; }
  .badge-warn { background: #451a03; color: #f59e0b; }
  .badge-ok { background: #022c22; color: #34d399; }
  .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: #64748b; margin: 24px 0 12px; }
</style>
</head>
<body>
<h1>✦ Astrid Spend</h1>
<div class="subtitle">Observability layer · refreshes every 60s · {snapshot_age}</div>

<div class="total-card">
  <div class="metric">
    <span class="metric-value">{total_cost}</span>
    <span class="metric-label">This week</span>
  </div>
  <div class="metric">
    <span class="metric-value warn">{waste_cost}</span>
    <span class="metric-label">Low-cache waste</span>
  </div>
  <div class="metric">
    <span class="metric-value good">{top_cache}%</span>
    <span class="metric-label">Best cache hit</span>
  </div>
  <div class="metric">
    <span class="metric-value">{call_count}</span>
    <span class="metric-label">Total calls</span>
  </div>
</div>

<div class="section-title">Where it goes — and why</div>
<table>
  <thead>
    <tr>
      <th>Call Site</th>
      <th class="right">Cost</th>
      <th class="right">$/call</th>
      <th class="right">Calls</th>
      <th>Cost share</th>
      <th class="right">Cache hit</th>
      <th>Cache bar</th>
      <th>Signal</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
</body>
</html>"""

ROW = """    <tr>
      <td>{name}</td>
      <td class="right">${cost:.2f}</td>
      <td class="right">${cpc:.4f}</td>
      <td class="right">{events:,}</td>
      <td><span class="bar-wrap"><span class="bar-fill bar-cost" style="width:{cost_pct:.0f}px"></span></span></td>
      <td class="right">{cache_pct:.1f}%</td>
      <td><span class="bar-wrap"><span class="bar-fill {cache_class}" style="width:{cache_bar:.0f}px"></span></span></td>
      <td>{badge}</td>
    </tr>"""


ASSISTANT_BIN = "/Users/sarahkate/.local/bin/assistant"

def fetch_data():
    result = subprocess.run(
        [ASSISTANT_BIN, "usage", "breakdown", "--group-by", "call_site", "--range", "week", "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    return data.get("breakdown", [])


def build_page():
    rows_data = fetch_data()
    if not rows_data:
        return "<html><body>No data</body></html>"

    total = sum(r["totalEstimatedCostUsd"] for r in rows_data)
    total_calls = sum(r["eventCount"] for r in rows_data)

    # Waste = call sites with < 10% cache hit rate and meaningful volume
    waste = sum(
        r["totalEstimatedCostUsd"] for r in rows_data
        if _cache_hit(r) < 10 and r["eventCount"] > 5
    )

    top_cache = max(_cache_hit(r) for r in rows_data)

    rows_html = ""
    for r in sorted(rows_data, key=lambda x: x["totalEstimatedCostUsd"], reverse=True):
        cost = r["totalEstimatedCostUsd"]
        cpc = cost / r["eventCount"] if r["eventCount"] else 0
        cache = _cache_hit(r)
        cost_bar = min((cost / total) * 100, 100)
        cache_class = "bar-cache-good" if cache >= 50 else "bar-cache-bad"
        has_cache_data = (r["totalCacheCreationTokens"] + r["totalCacheReadTokens"]) > 0

        if not has_cache_data:
            badge = ""
        elif cache < 10:
            badge = '<span class="badge badge-warn">⚠ low cache</span>'
        elif cache >= 80:
            badge = '<span class="badge badge-ok">✓ cached</span>'
        else:
            badge = ""

        rows_html += ROW.format(
            name=r["group"],
            cost=cost,
            cpc=cpc,
            events=r["eventCount"],
            cost_pct=cost_bar,
            cache_pct=cache,
            cache_class=cache_class,
            cache_bar=cache,
            badge=badge,
        )

    return HTML.format(
        total_cost=f"${total:.2f}",
        waste_cost=f"${waste:.2f}",
        top_cache=f"{top_cache:.0f}",
        call_count=f"{total_calls:,}",
        rows=rows_html,
        snapshot_age=f"fetched {time.strftime('%H:%M:%S')}",
    )


def _cache_hit(r):
    reads = r["totalCacheReadTokens"]
    writes = r["totalCacheCreationTokens"]
    total = reads + writes
    return (reads / total * 100) if total > 0 else 0.0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = build_page().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass  # suppress request logs


if __name__ == "__main__":
    print("✦ Astrid Spend Dashboard")
    print("  → http://localhost:7331")
    print("  Ctrl+C to stop\n")
    HTTPServer(("localhost", 7331), Handler).serve_forever()
