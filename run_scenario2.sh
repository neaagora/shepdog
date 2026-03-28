#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_scenario2.sh — Draft vs Sent · 5-Agent Parallel Run
#
# Starts:
#   • email_service.py   → port 8743  (mock email service)
#   • interceptor_s2.py  → port 8744  (Shepdog proxy → 8743)
#
# Then runs agent_runner_s2.py (5 Mistral agents via Ollama, simultaneously).
# Prints the comparison report when all agents complete.
#
# Usage:
#   ./run_scenario2.sh [--model mistral]
#
# Prerequisites:
#   • Ollama running on localhost:11434 with the target model pulled
#   • Python 3.10+
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${1:-mistral}"
LOG_DIR="/tmp/shepdog-s2-logs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EMAIL_PID=""
SHEP_PID=""

cleanup() {
    echo ""
    echo "[run] Shutting down email service and Shepdog interceptor..."
    [[ -n "$EMAIL_PID" ]] && kill "$EMAIL_PID" 2>/dev/null || true
    [[ -n "$SHEP_PID"  ]] && kill "$SHEP_PID"  2>/dev/null || true
    echo "[run] Done."
}
trap cleanup EXIT

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Shepdog · Scenario 2: Draft vs Sent · Mistral via Ollama   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Model    : $MODEL"
echo "  Agents   : 5 (parallel)"
echo "  Logs     : $LOG_DIR"
echo ""

# ── Verify Ollama is up ───────────────────────────────────────────────────────
echo "[run] Checking Ollama (port 11434)..."
if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "[run] ERROR: Ollama is not running on port 11434."
    echo "      Start it first: ollama serve"
    exit 1
fi
echo "[run] Ollama OK"

# Check the model is available
if ! curl -sf http://localhost:11434/api/tags | python3 -c "
import json,sys
tags = json.load(sys.stdin)
names = [m['name'] for m in tags.get('models', [])]
model = '$MODEL'
# Accept model with or without :latest tag
found = any(n == model or n.startswith(model + ':') for n in names)
sys.exit(0 if found else 1)
" 2>/dev/null; then
    echo "[run] WARNING: model '$MODEL' not found in Ollama. Pulling..."
    ollama pull "$MODEL" || { echo "[run] ERROR: failed to pull $MODEL"; exit 1; }
fi
echo "[run] Model '$MODEL' available"
echo ""

# ── Start email service ───────────────────────────────────────────────────────
echo "[run] Starting email service (email_service.py → port 8743)..."
python3 email_service.py > "$LOG_DIR/email_service.log" 2>&1 &
EMAIL_PID=$!

# ── Start Shepdog interceptor ─────────────────────────────────────────────────
echo "[run] Starting Shepdog interceptor (interceptor_s2.py → port 8744 → 8743)..."
python3 interceptor_s2.py > "$LOG_DIR/interceptor_s2.log" 2>&1 &
SHEP_PID=$!

# ── Wait for both services to be ready ───────────────────────────────────────
echo "[run] Waiting for services..."
for i in $(seq 1 15); do
    sleep 0.4
    email_ok=false; shep_ok=false
    curl -sf http://localhost:8743/status > /dev/null 2>&1 && email_ok=true
    curl -sf http://localhost:8744/status > /dev/null 2>&1 && shep_ok=true
    if $email_ok && $shep_ok; then break; fi
    if [[ $i -eq 15 ]]; then
        echo "[run] ERROR: services did not start in time."
        echo "  Email service ready : $email_ok"
        echo "  Shepdog ready       : $shep_ok"
        echo ""
        echo "── email_service.log ──────────────────────────────────────"
        cat "$LOG_DIR/email_service.log" || true
        echo ""
        echo "── interceptor_s2.log ─────────────────────────────────────"
        cat "$LOG_DIR/interceptor_s2.log" || true
        exit 1
    fi
done

echo "[run] Email service ready (port 8743)"
echo "[run] Shepdog interceptor ready (port 8744)"
echo ""

# ── Run 5 agents simultaneously ──────────────────────────────────────────────
echo "[run] Launching 5 Mistral agents in parallel..."
echo ""
python3 agent_runner_s2.py --model "$MODEL"
RUNNER_RC=$?

# ── Show service logs ─────────────────────────────────────────────────────────
echo ""
echo "══ Email service log ════════════════════════════════════════"
cat "$LOG_DIR/email_service.log"
echo ""
echo "══ Shepdog interceptor log ══════════════════════════════════"
cat "$LOG_DIR/interceptor_s2.log"
echo ""

# ── Show any service records written to /tmp ──────────────────────────────────
echo "══ Service records ══════════════════════════════════════════"
for f in /tmp/shepdog-s2-record-*.json; do
    [[ -f "$f" ]] || continue
    agent_id=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('agent_id','?'))" 2>/dev/null || echo "?")
    lie=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('helpful_lie_detected','?'))" 2>/dev/null || echo "?")
    echo "  $f"
    echo "    agent_id             : $agent_id"
    echo "    helpful_lie_detected : $lie"
done
echo ""

# ── Show latest comparison report ─────────────────────────────────────────────
LATEST_REPORT=$(ls -t /tmp/shepdog-s2-comparison-CMP2-*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_REPORT" ]]; then
    echo "══ Latest comparison report ═════════════════════════════════"
    cat "$LATEST_REPORT"
    echo ""
fi

exit $RUNNER_RC
