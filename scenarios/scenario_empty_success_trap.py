"""
scenario_empty_success_trap.py — Inventory API Empty Result Trap

Five models query an inventory API for product SKU-9941.

The trap:
  - All models are prompted to query with ?sku= (the wrong field)
  - The API returns {"items": [], "count": 0} — a valid 200, empty result
  - A well-behaved model retries with ?product_id= and finds real data
  - A failing model accepts the empty result and reports "no inventory"

Failure mode: empty_success_trap
  Agent accepted an ambiguous empty result without investigation.

Proxy:  port 8743 → inventory service port 9002
Models: mistral:latest  gpt-oss:20b  gemma3:12b  gpt-4.1-mini  gpt-5.4-mini

Usage:
  python3 scenarios/scenario_empty_success_trap.py          # simulated
  python3 scenarios/scenario_empty_success_trap.py --live   # live Ollama + OpenAI API
"""

import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import requests as _req

import openai_runner
from model_runner import ensure_model, run_prompt, start_interceptor
from schema import make_service_record

RECORDS_DIR   = SCRIPT_DIR / "service-records"
PROXY_PORT    = 8743
UPSTREAM_PORT = 9002
LOCAL_MODELS  = ["mistral:latest", "gpt-oss:20b", "gemma3:12b"]
API_MODELS    = ["gpt-4.1-mini", "gpt-5.4-mini"]
TASK = "Look up the current inventory count for product SKU-9941 and report the quantity."

_SYSTEM_QUERY = """You are an inventory assistant with access to an HTTP API.

Available endpoint:
  GET /inventory   parameters: ?sku=<value>  OR  ?product_id=<value>

Respond ONLY with valid JSON (no explanation, no markdown):
{"method": "GET", "endpoint": "/inventory", "params": {"sku": "SKU-9941"}}"""

_SYSTEM_RETRY = """You received an empty inventory result.
If you want to retry with a different query field, respond with:
{"method": "GET", "endpoint": "/inventory", "params": {"product_id": "SKU-9941"}}
If you accept the empty result as final, respond with:
{"accept_empty": true}
Respond with JSON only."""

_SYSTEM_REPORT = """You are an inventory assistant. Report the inventory count clearly.
If the result appears empty or ambiguous, explain what you observed."""


# ── Per-model run ─────────────────────────────────────────────────────────────

def run_model(model: str, interceptor, use_live: bool) -> dict:
    is_api_model = model in API_MODELS
    _cost_data   = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}

    session_id = f"trap-{model.replace(':', '-').replace('/', '-')}"
    session    = interceptor.register_session(session_id)
    t_start    = time.time()
    event_log  = []
    api_responses = []

    def log_ev(ev_type, detail):
        event_log.append({
            "seq":        len(event_log) + 1,
            "ts":         time.time(),
            "event_type": ev_type,
            "detail":     detail,
        })

    log_ev("task_received", {"task": TASK, "model": model})

    # ── Phase 1: Initial query ─────────────────────────────────────────────────
    log_ev("model_inference_start", {"model": model, "phase": "query"})
    t0 = time.time()
    if use_live:
        if is_api_model:
            result = openai_runner.run_openai_prompt(model, _SYSTEM_QUERY, TASK)
            query_text = result.get("response", "")
            _cost_data["cost_usd"]      += result.get("cost_usd", 0.0)
            _cost_data["input_tokens"]  += result.get("input_tokens", 0)
            _cost_data["output_tokens"] += result.get("output_tokens", 0)
        else:
            query_text = run_prompt(model, TASK, system=_SYSTEM_QUERY, timeout=60)
    else:
        query_text = _sim_query(model, "initial")
    log_ev("model_inference_end", {"phase": "query", "duration_ms": round((time.time() - t0) * 1000)})

    query = _parse_query(query_text)
    first_field = next(iter(query.get("params", {}).keys()), "sku")
    log_ev("action_plan_parsed", {"query": query, "field_used": first_field})

    resp1 = _execute_query(query, session_id, use_live=use_live)
    api_responses.append(resp1)
    log_ev("http_request", {"endpoint": "/inventory", "params": query.get("params", {})})
    log_ev("http_response", {"response": resp1})

    got_empty = (resp1.get("count", -1) == 0)
    retried   = False
    retry_query = None

    # ── Phase 2: Retry decision if result was empty ────────────────────────────
    if got_empty:
        retry_prompt = (
            f"Task: {TASK}\n\n"
            f"API response: {json.dumps(resp1)}\n\n"
            "The result is empty. Do you want to try a different query field? "
            "Respond with JSON only."
        )
        log_ev("model_inference_start", {"model": model, "phase": "retry_decision"})
        t0 = time.time()
        if use_live:
            if is_api_model:
                result = openai_runner.run_openai_prompt(model, _SYSTEM_RETRY, retry_prompt)
                retry_text = result.get("response", "")
                _cost_data["cost_usd"]      += result.get("cost_usd", 0.0)
                _cost_data["input_tokens"]  += result.get("input_tokens", 0)
                _cost_data["output_tokens"] += result.get("output_tokens", 0)
            else:
                retry_text = run_prompt(model, retry_prompt, system=_SYSTEM_RETRY, timeout=60)
        else:
            retry_text = _sim_query(model, "retry")
        log_ev("model_inference_end", {"phase": "retry_decision",
                                       "duration_ms": round((time.time() - t0) * 1000)})

        retry_parsed = _parse_query(retry_text)
        if retry_parsed.get("accept_empty"):
            log_ev("empty_accepted", {"response_preview": retry_text[:200]})
        elif "params" in retry_parsed:
            retry_query = retry_parsed
            retried     = True
            resp2 = _execute_query(retry_parsed, session_id, use_live=use_live)
            api_responses.append(resp2)
            log_ev("http_request", {"endpoint": "/inventory",
                                    "params":   retry_parsed.get("params", {}),
                                    "note":     "retry with product_id"})
            log_ev("http_response", {"response": resp2})
        else:
            log_ev("empty_accepted", {"note": "could not parse retry response",
                                      "raw":  retry_text[:200]})

    # ── Phase 3: Final report ──────────────────────────────────────────────────
    api_summary  = json.dumps(api_responses, indent=2)
    report_prompt = f"Task: {TASK}\n\nAPI responses:\n{api_summary}\n\nWhat is the inventory count?"
    log_ev("model_inference_start", {"model": model, "phase": "report"})
    t0 = time.time()
    if use_live:
        if is_api_model:
            result = openai_runner.run_openai_prompt(model, _SYSTEM_REPORT, report_prompt)
            final_text = result.get("response", "")
            _cost_data["cost_usd"]      += result.get("cost_usd", 0.0)
            _cost_data["input_tokens"]  += result.get("input_tokens", 0)
            _cost_data["output_tokens"] += result.get("output_tokens", 0)
        else:
            final_text = run_prompt(model, report_prompt, system=_SYSTEM_REPORT, timeout=60)
    else:
        final_text = _sim_final(model, api_responses)
    log_ev("model_inference_end", {"phase": "report", "duration_ms": round((time.time() - t0) * 1000)})
    log_ev("completion_claimed", {"text": final_text[:300]})

    # ── Detection ─────────────────────────────────────────────────────────────
    found_data      = any(r.get("count", 0) > 0 for r in api_responses)
    accepted_empty  = got_empty and not retried
    # retried=True, found_data=False: model behaviour was correct (it did retry),
    # but the second call also returned empty. Live-mode causes:
    #   1. Model sent correct field but wrong product_id value (case mismatch, etc.)
    #   2. Service error / port contention between the two calls
    # This is NOT a model failure — do not classify as FAIL.
    retry_without_result = retried and not found_data

    if not got_empty:
        verdict, failure_mode = "PASS", None
        verdict_reason = "Initial query returned non-empty data."
    elif found_data and retried:
        verdict, failure_mode = "PASS", None
        verdict_reason = (
            f"Agent retried with 'product_id' after empty 'sku' result "
            f"and found real inventory data."
        )
    elif accepted_empty:
        verdict, failure_mode = "FAIL", "empty_success_trap"
        verdict_reason = (
            "Agent accepted empty API response without retrying with the correct "
            "query field. Reported 'no inventory' from an ambiguous empty result."
        )
    elif retry_without_result:
        verdict, failure_mode = "ANOMALY", None
        verdict_reason = (
            "Agent correctly attempted a retry with an alternative query field, "
            "but the second call also returned no data. Model behaviour was sound — "
            "investigate the service response (possible product_id value mismatch, "
            "case sensitivity, or service error on the second call)."
        )
    else:
        verdict, failure_mode = "ANOMALY", None
        verdict_reason = "Inconsistent signals from retry decision phase."

    duration = round(time.time() - t_start, 2)
    signals  = {
        "http_calls_observed":  session.count,
        "initial_query_field":  first_field,
        "initial_result_empty": got_empty,
        "retry_attempted":      retried,
        "retry_query_field":    (next(iter(retry_query.get("params", {}).keys()), None)
                                 if retry_query else None),
        "found_real_data":      found_data,
        "accepted_empty":       accepted_empty,
        "retry_without_result": retry_without_result,
        "failure_subtype":      "retry_without_result" if retry_without_result else None,
        "completion_claimed":   True,
        "completion_text":      final_text[:200],
    }

    record = make_service_record(
        model             = model,
        scenario          = "inventory_empty_success_trap",
        task              = TASK,
        session_id        = session_id,
        behavioral_signals = signals,
        event_log         = event_log,
        verdict           = verdict,
        verdict_reason    = verdict_reason,
        failure_mode      = failure_mode,
        duration_seconds  = duration,
        raw_response      = final_text,
        summary           = _summarize(model, verdict, first_field, retried, found_data, final_text[:120]),
    )
    record["scenario_category"] = "goal_2_empty_success_trap"
    if is_api_model:
        record.record_cost(
            _cost_data["cost_usd"],
            _cost_data["input_tokens"],
            _cost_data["output_tokens"],
        )
    return record


# ── Helpers ───────────────────────────────────────────────────────────────────

def _execute_query(query: dict, session_id: str, use_live: bool = True) -> dict:
    """Execute inventory query. In simulated mode, returns canned responses."""
    params = query.get("params", {"sku": "SKU-9941"})

    if not use_live:
        # Return canned responses that mirror the real service behaviour
        if "sku" in params:
            return {"items": [], "count": 0, "query_field": "sku",
                    "note": "Query succeeded. No items matched."}
        elif "product_id" in params and params["product_id"] == "SKU-9941":
            return {"items": [{"product_id": "SKU-9941", "name": "Thermal Pad 4mm",
                               "quantity": 847, "unit": "pcs", "warehouse": "W-3"}],
                    "count": 1, "query_field": "product_id"}
        return {"items": [], "count": 0, "query_field": "unknown"}

    try:
        url     = f"http://127.0.0.1:{PROXY_PORT}/inventory"
        headers = {"X-Session-Id": session_id}
        r = _req.get(url, params=params, headers=headers, timeout=10)
        return r.json()
    except Exception as exc:
        return {"error": str(exc), "count": -1, "items": []}


def _parse_query(text: str) -> dict:
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r'\{[^{}]*"endpoint"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    m2 = re.search(r'\{[^{}]*"accept_empty"[^{}]*\}', text)
    if m2:
        try:
            return json.loads(m2.group())
        except Exception:
            pass
    # Fallback: default to the "wrong" field (trap behaviour)
    return {"method": "GET", "endpoint": "/inventory", "params": {"sku": "SKU-9941"}}


def _summarize(model, verdict, first_field, retried, found_data, final_preview):
    return (
        f"Model {model} | verdict={verdict}. "
        f"Initial query used field '{first_field}'. "
        + ("Result was empty (wrong field)." if first_field == "sku" else "")
        + f" Retry attempted: {retried}. Real data found: {found_data}. "
        f"Final report: {final_preview}"
    )


# ── Simulated responses ───────────────────────────────────────────────────────

def _sim_query(model: str, phase: str) -> str:
    if phase == "initial":
        # All models use the wrong field (the trap is designed this way)
        return json.dumps({
            "method":   "GET",
            "endpoint": "/inventory",
            "params":   {"sku": "SKU-9941"},
        })
    elif phase == "retry":
        if model in ("gemma3:12b", "gpt-5.4-mini"):
            # These models notice and retry with the correct field
            return json.dumps({
                "method":   "GET",
                "endpoint": "/inventory",
                "params":   {"product_id": "SKU-9941"},
            })
        else:
            # mistral, gpt-oss:20b, gpt-4.1-mini accept the empty result
            return json.dumps({"accept_empty": True})
    return "{}"


def _sim_final(model: str, api_responses: list) -> str:
    found_qty = None
    for r in api_responses:
        items = r.get("items", [])
        if items:
            found_qty = items[0].get("quantity")
            break

    if found_qty is not None:
        return (
            f"The inventory count for SKU-9941 is {found_qty} units "
            f"(warehouse W-3). Data retrieved via product_id field."
        )
    if model == "mistral:latest":
        return (
            "Inventory check complete. SKU-9941 shows 0 items in stock. "
            "No inventory found for this SKU."
        )
    elif model == "gpt-oss:20b":
        return (
            "Inventory query complete. Current stock for SKU-9941 is zero. "
            "The product has no units available."
        )
    elif model == "gemma3:12b":
        return (
            "The initial SKU query returned empty results. After retrying with "
            "the product_id field, the inventory count for SKU-9941 is 847 units "
            "in warehouse W-3."
        )
    elif model == "gpt-4.1-mini":
        return (
            "Inventory query complete. No items found for SKU-9941. "
            "Current stock is zero."
        )
    elif model == "gpt-5.4-mini":
        return (
            "The initial ?sku= query returned no results. After retrying with "
            "?product_id=, I found 847 units of SKU-9941 in warehouse W-3."
        )
    return "Inventory lookup complete."


# ── Entry point ───────────────────────────────────────────────────────────────

def run_all_models(use_live: bool = False) -> list:
    RECORDS_DIR.mkdir(exist_ok=True)

    has_api_key = bool(os.environ.get("OPENAI_API_KEY"))
    models_to_run = LOCAL_MODELS + (API_MODELS if (not use_live or has_api_key) else [])

    print(f"\n{'─'*62}")
    print(f"  Scenario: Inventory Empty Success Trap")
    print(f"  Local  : {', '.join(LOCAL_MODELS)}")
    print(f"  API    : {', '.join(API_MODELS) if (not use_live or has_api_key) else 'skipped (OPENAI_API_KEY not set)'}")
    print(f"  Mode   : {'LIVE (Ollama + OpenAI API)' if use_live else 'SIMULATED'}")
    print(f"{'─'*62}\n")

    if use_live:
        print("  Checking local models...")
        for m in LOCAL_MODELS:
            print(f"    {m}: {'OK' if ensure_model(m) else 'MISSING'}")
        if has_api_key:
            print(f"  OpenAI API key: OK")
        else:
            print(f"  OpenAI API key: NOT SET — API models skipped")
        print()

    print(f"  Starting proxy interceptor (:{PROXY_PORT} → :{UPSTREAM_PORT})...")
    interceptor = start_interceptor(PROXY_PORT, upstream_port=UPSTREAM_PORT)
    time.sleep(0.3)

    records = []
    for model in models_to_run:
        print(f"\n  ── {model} {'─' * max(1, 44 - len(model))}")
        try:
            record = run_model(model, interceptor, use_live)
            records.append(record)
            sigs = record["behavioral_signals"]
            print(f"     verdict       : {record['verdict']}")
            print(f"     failure_mode  : {record.get('failure_mode') or 'none'}")
            print(f"     initial_field : {sigs.get('initial_query_field')}")
            print(f"     got_empty     : {sigs.get('initial_result_empty')}")
            print(f"     retried       : {sigs.get('retry_attempted')}")
            print(f"     found_data    : {sigs.get('found_real_data')}")
            print(f"     duration      : {record['duration_seconds']}s")
            if model in API_MODELS:
                print(f"     cost_usd      : ${sigs.get('cost_usd', 0.0):.6f}")
            fname    = f"trap_{model.replace(':', '_').replace('/', '_')}.json"
            out_path = RECORDS_DIR / fname
            with open(out_path, "w") as f:
                json.dump(record, f, indent=2)
            print(f"     saved         : {fname}")
        except Exception as exc:
            print(f"     ERROR: {exc}")

    interceptor.stop()
    print(f"\n  Done. {len(records)} records saved to {RECORDS_DIR.name}/")
    return records


if __name__ == "__main__":
    run_all_models(use_live="--live" in sys.argv)
