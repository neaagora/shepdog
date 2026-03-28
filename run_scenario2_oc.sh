#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_scenario2_oc.sh — Draft vs Sent · Autonomous OC Agent
#
# Infrastructure (unchanged from run_scenario2.sh):
#   • email_service.py   → port 8743  (mock email service)
#   • interceptor_s2.py  → port 8744  (Shepdog proxy → 8743)
#
# New: instead of the scaffolded agent_runner_s2.py decision loop,
# uses oc_task_s2.py which gives Mistral the raw task and raw API access
# via `openclaw agent --message` and observes what actually hits the proxy.
#
# Usage:
#   ./run_scenario2_oc.sh [--timeout 300]
#
# Prerequisites:
#   • Ollama running on localhost:11434 with mistral:latest pulled
#   • OpenClaw gateway running  (openclaw health)
#   • Python 3.10+
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TIMEOUT="${1:-300}"
LOG_DIR="/tmp/shepdog-s2-oc-logs"
RECORD_DIR="/tmp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EMAIL_PID=""
SHEP_PID=""

cleanup() {
    echo ""
    echo "[run] Shutting down services…"
    [[ -n "$EMAIL_PID" ]] && kill "$EMAIL_PID" 2>/dev/null || true
    [[ -n "$SHEP_PID"  ]] && kill "$SHEP_PID"  2>/dev/null || true
    echo "[run] Done."
}
trap cleanup EXIT

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Shepdog · Scenario 2: Draft vs Sent · Autonomous OC Agent  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Runs      : 3 (sequential)"
echo "  Timeout   : ${TIMEOUT}s per run"
echo "  Task      : raw prompt, no endpoint hints"
echo "  Logs      : $LOG_DIR"
echo ""

# ── Verify prerequisites ──────────────────────────────────────────────────────
echo "[run] Checking Ollama (port 11434)…"
if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "[run] ERROR: Ollama not running. Start it: ollama serve"
    exit 1
fi
echo "[run] Ollama OK"

echo "[run] Checking OpenClaw gateway…"
if ! openclaw health &>/dev/null; then
    echo "[run] ERROR: OpenClaw gateway not running."
    echo "      Start: openclaw gateway --port 18789"
    exit 1
fi
echo "[run] OpenClaw OK"
echo ""

# ── Start email service ───────────────────────────────────────────────────────
echo "[run] Starting email service (port 8743)…"
python3 email_service.py > "$LOG_DIR/email_service.log" 2>&1 &
EMAIL_PID=$!

# ── Start Shepdog interceptor ─────────────────────────────────────────────────
echo "[run] Starting Shepdog interceptor (port 8744 → 8743)…"
python3 interceptor_s2.py > "$LOG_DIR/interceptor_s2.log" 2>&1 &
SHEP_PID=$!

# ── Wait for both ready ───────────────────────────────────────────────────────
echo "[run] Waiting for services…"
for i in $(seq 1 15); do
    sleep 0.4
    email_ok=false; shep_ok=false
    curl -sf http://localhost:8743/status > /dev/null 2>&1 && email_ok=true
    curl -sf http://localhost:8744/status > /dev/null 2>&1 && shep_ok=true
    if $email_ok && $shep_ok; then break; fi
    if [[ $i -eq 15 ]]; then
        echo "[run] ERROR: services did not start."
        cat "$LOG_DIR/email_service.log" 2>/dev/null || true
        cat "$LOG_DIR/interceptor_s2.log" 2>/dev/null || true
        exit 1
    fi
done
echo "[run] Email service ready (port 8743)"
echo "[run] Shepdog interceptor ready (port 8744)"
echo ""

# ── Run 3 autonomous OC agents ────────────────────────────────────────────────
echo "[run] Launching 3 autonomous OC agents (sequential)…"
echo ""
python3 oc_task_s2.py --timeout "$TIMEOUT" --record-dir "$RECORD_DIR"
RUNNER_RC=$?

# ── Show logs ─────────────────────────────────────────────────────────────────
echo ""
echo "══ Shepdog interceptor log ══════════════════════════════════"
cat "$LOG_DIR/interceptor_s2.log"
echo ""

# ── Latest comparison report ──────────────────────────────────────────────────
LATEST=$(ls -t /tmp/shepdog-s2-oc-comparison-CMP2-OC-*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST" ]]; then
    echo "══ Comparison report ════════════════════════════════════════"
    cat "$LATEST"
fi

exit $RUNNER_RC
