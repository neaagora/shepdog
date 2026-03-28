#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_scenario.sh — Shepdog Multi-Model Scenario Runner
#
# Orchestrates all three scenarios in order:
#   Step 1  scenario1_reference.py          reference failure mode records
#   Step 2  scenario2_multimodel.py         autonomous email task (dry-run trap)
#   Step 3  scenario_empty_success_trap.py  inventory API empty result trap
#   Step 4  generate_report.py              aggregate → report.json
#
# Usage:
#   ./run_scenario.sh              simulated mode (no Ollama needed)
#   ./run_scenario.sh --live       live Ollama run (requires models pulled)
#
# Prerequisites (--live only):
#   ollama serve                       Ollama listening on port 11434
#   ollama pull mistral:latest
#   ollama pull gpt-oss:20b
#   ollama pull gemma3:12b
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

LIVE=""
for arg in "$@"; do [[ "$arg" == "--live" ]] && LIVE="--live"; done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="/tmp/shepdog-logs"
EMAIL_PID=""
TRAP_PID=""

# ── Cleanup ───────────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "[run] Stopping mock services..."
    [[ -n "$EMAIL_PID" ]] && kill "$EMAIL_PID" 2>/dev/null || true
    [[ -n "$TRAP_PID"  ]] && kill "$TRAP_PID"  2>/dev/null || true
    echo "[run] Done."
}
trap cleanup EXIT

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
mkdir -p "$SCRIPT_DIR/service-records"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Shepdog · Multi-Model Scenario Runner                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Mode    : ${LIVE:---simulated}"
echo "  Records : $SCRIPT_DIR/service-records/"
echo "  Logs    : $LOG_DIR/"
echo ""

# ── 1. Check Ollama (live mode only) ──────────────────────────────────────────
if [[ -n "$LIVE" ]]; then
    echo "[run] Checking Ollama (port 11434)..."
    if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "[run] ERROR: Ollama is not running."
        echo "      Start it:  ollama serve"
        exit 1
    fi
    echo "[run] Ollama OK"

    if [ -z "$OPENAI_API_KEY" ]; then
        echo "[run] Warning: OPENAI_API_KEY not set — API models will be skipped"
    else
        echo "[run] OpenAI API key: OK"
    fi
    echo ""
fi

# ── 2. Start email mock service on port 9001 ──────────────────────────────────
echo "[run] Starting email mock service (port 9001)..."
python3 "$SCRIPT_DIR/email_service.py" 9001 > "$LOG_DIR/email_service.log" 2>&1 &
EMAIL_PID=$!

# ── 3. Start inventory trap service on port 9002 ──────────────────────────────
echo "[run] Starting inventory trap service (port 9002)..."
python3 "$SCRIPT_DIR/empty_success_trap_service.py" 9002 > "$LOG_DIR/trap_service.log" 2>&1 &
TRAP_PID=$!

# ── Wait for services ─────────────────────────────────────────────────────────
echo "[run] Waiting for mock services..."
email_ok=false; trap_ok=false
for i in $(seq 1 20); do
    sleep 0.3
    curl -sf http://localhost:9001/status > /dev/null 2>&1 && email_ok=true
    curl -sf http://localhost:9002/status > /dev/null 2>&1 && trap_ok=true
    if $email_ok && $trap_ok; then break; fi
done

if $email_ok; then
    echo "[run] Email service   : OK  (port 9001)"
else
    echo "[run] Email service   : TIMEOUT — continuing (simulated mode may not need it)"
fi
if $trap_ok; then
    echo "[run] Inventory trap  : OK  (port 9002)"
else
    echo "[run] Inventory trap  : TIMEOUT — continuing"
fi
echo ""

# ── Step 1: Reference records ─────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 1 / 4 · Reference Failure Mode Records                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
python3 "$SCRIPT_DIR/scenarios/scenario1_reference.py"
echo ""

# ── Step 2: Multi-model email task ────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 2 / 4 · Autonomous Email Task (Dry-Run Trap)           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
python3 "$SCRIPT_DIR/scenarios/scenario2_multimodel.py" $LIVE
echo ""

# ── Step 3: Empty success trap ────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 3 / 4 · Inventory Empty Success Trap                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
python3 "$SCRIPT_DIR/scenarios/scenario_empty_success_trap.py" $LIVE
echo ""

# ── Step 4: Generate report ───────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 4 / 4 · Generate Report                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
python3 "$SCRIPT_DIR/generate_report.py"
echo ""

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  All done.                                                    ║"
echo "║  · service-records/   individual JSON records                 ║"
echo "║  · report.json        aggregated summary                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
