#!/bin/bash
# start.sh — boot the full Astrid coding environment
# Usage: ./start.sh [--no-es] [--no-cursor]
#
# What it does:
#   1. Checks / installs dependencies
#   2. Starts Elasticsearch (Docker) unless --no-es
#   3. Starts the FastAPI proxy on :8000
#   4. Opens Cursor unless --no-cursor
#
set -euo pipefail

NO_ES=false
NO_CURSOR=false
for arg in "$@"; do
  case "$arg" in
    --no-es) NO_ES=true ;;
    --no-cursor) NO_CURSOR=true ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1"; }

echo ""
echo "  Astrid Environment"
echo "  ==================="
echo ""

# ---------------------------------------------------------------------------
# 1. Python deps
# ---------------------------------------------------------------------------
echo "Checking dependencies..."

MCP_PYTHON="/Users/sarahkate/reasoning-layer/.mcp-venv/bin/python3"
MCP_VENV="/Users/sarahkate/reasoning-layer/.mcp-venv"

if [ ! -f "$MCP_PYTHON" ]; then
  warn "MCP venv not found — creating..."
  /opt/homebrew/bin/python3 -m venv "$MCP_VENV"
  "$MCP_VENV/bin/pip" install "mcp[cli]" httpx --quiet
fi

if ! python3 -c "import fastapi" 2>/dev/null; then
  warn "fastapi not installed — installing..."
  pip3 install fastapi uvicorn anthropic python-dotenv pydantic httpx --quiet
fi

ok "Python deps ready"

# ---------------------------------------------------------------------------
# 2. .env check
# ---------------------------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  warn ".env not found — proxy will run without Anthropic API key"
  warn "Create $SCRIPT_DIR/.env with: ANTHROPIC_API_KEY=sk-ant-..."
else
  ok ".env found"
fi

# ---------------------------------------------------------------------------
# 3. Elasticsearch
# ---------------------------------------------------------------------------
if [ "$NO_ES" = false ]; then
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    if ! docker ps | grep -q elasticsearch; then
      echo "Starting Elasticsearch..."
      docker compose up -d 2>/dev/null || warn "docker compose failed — routing works without ES"
    else
      ok "Elasticsearch already running"
    fi
  else
    warn "Docker not running — starting without Elasticsearch (routing still works)"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Kill any existing proxy on :8000
# ---------------------------------------------------------------------------
EXISTING=$(lsof -ti:8000 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  warn "Killing existing process on :8000 (pid $EXISTING)"
  kill "$EXISTING" 2>/dev/null || true
  sleep 1
fi

# ---------------------------------------------------------------------------
# 5. Start proxy in background, log to file
# ---------------------------------------------------------------------------
LOG="$SCRIPT_DIR/proxy.log"
echo "Starting Astrid proxy..."
nohup python3 -m uvicorn proxy:app --host 0.0.0.0 --port 8000 --reload > "$LOG" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" > "$SCRIPT_DIR/proxy.pid"

# Wait for it to be ready
READY=false
for i in $(seq 1 15); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 1
done

if [ "$READY" = true ]; then
  ok "Astrid proxy running on http://localhost:8000 (pid $PROXY_PID)"
  ok "MCP server will auto-start when Cursor connects"
  ok "Logs: $LOG"
else
  err "Proxy didn't start — check $LOG"
  exit 1
fi

# ---------------------------------------------------------------------------
# 6. Open Cursor
# ---------------------------------------------------------------------------
if [ "$NO_CURSOR" = false ]; then
  if command -v cursor &>/dev/null; then
    echo "Opening Cursor..."
    cursor . &
    ok "Cursor launched"
  elif [ -d "/Applications/Cursor.app" ]; then
    open -a Cursor . &
    ok "Cursor launched"
  else
    warn "Cursor not installed — skipping"
    warn "Install: brew install --cask cursor"
  fi
fi

echo ""
echo "  Ready."
echo ""
echo "  Proxy:  http://localhost:8000"
echo "  Health: http://localhost:8000/health"
echo "  Logs:   tail -f $LOG"
echo ""
echo "  In Cursor: @astrid → chat, get_context, remember, route"
echo ""
