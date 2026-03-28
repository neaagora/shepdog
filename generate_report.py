"""
generate_report.py — Aggregate service records into report.json

Reads all JSON files from service-records/, prints a summary table to stdout,
and writes report.json.

Output structure:
{
  "generated_at": "...",
  "record_count": N,
  "scenario_2_multimodel":   { model: { signals, verdict, ... } },
  "goal_2_empty_success_trap": { model: { signals, verdict, ... } },
  "failure_mode_reference":  { "helpful_lie": {...}, "hallucination": {...}, ... }
}

Usage: python3 generate_report.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RECORDS_DIR = Path(__file__).parent / "service-records"
REPORT_OUT  = Path(__file__).parent / "report.json"


def load_records() -> list:
    if not RECORDS_DIR.exists():
        return []
    records = []
    for path in sorted(RECORDS_DIR.glob("*.json")):
        try:
            with open(path) as f:
                r = json.load(f)
            r["_source_file"] = path.name
            records.append(r)
        except Exception as exc:
            print(f"  [report] WARNING: could not read {path.name}: {exc}", file=sys.stderr)
    return records


def _cost_entry(sig: dict) -> dict:
    return {
        "cost_usd":      sig.get("cost_usd", 0.0),
        "input_tokens":  sig.get("input_tokens", 0),
        "output_tokens": sig.get("output_tokens", 0),
        "model_type":    sig.get("model_type", "local"),
    }


def build_report(records: list) -> dict:
    report = {
        "generated_at":             datetime.now(timezone.utc).isoformat(),
        "record_count":             len(records),
        "scenario_2_multimodel":    {},
        "goal_2_empty_success_trap": {},
        "failure_mode_reference":   {},
        "cost_summary":             {},
    }

    for r in records:
        cat      = r.get("scenario_category", "")
        scenario = r.get("scenario", "")
        model    = r.get("model", "unknown")
        sig      = r.get("behavioral_signals", {})

        entry = {
            "record_id":          r.get("record_id"),
            "model":              model,
            "verdict":            r.get("verdict"),
            "failure_mode":       r.get("failure_mode"),
            "duration_seconds":   r.get("duration_seconds"),
            "behavioral_signals": sig,
            "summary":            r.get("summary", ""),
            "source_file":        r.get("_source_file"),
            **_cost_entry(sig),
        }

        if cat == "scenario_2_multimodel" or scenario == "draft_vs_sent_multimodel":
            report["scenario_2_multimodel"][model] = entry

        elif cat == "goal_2_empty_success_trap" or scenario == "inventory_empty_success_trap":
            report["goal_2_empty_success_trap"][model] = entry

        elif cat == "failure_mode_reference":
            fm = r.get("failure_mode")
            if fm == "helpful_lie":
                report["failure_mode_reference"]["helpful_lie"] = entry
            elif fm == "hallucinated_tool_use":
                report["failure_mode_reference"]["hallucination"] = entry
            elif fm is None and r.get("verdict") == "PASS":
                report["failure_mode_reference"]["correct_behavior"] = entry

    # ── Cost summary ──────────────────────────────────────────────────────────
    s2_costs   = {m: e["cost_usd"] for m, e in report["scenario_2_multimodel"].items()}
    trap_costs = {m: e["cost_usd"] for m, e in report["goal_2_empty_success_trap"].items()}
    total_api  = sum(
        e["cost_usd"]
        for section in (report["scenario_2_multimodel"], report["goal_2_empty_success_trap"])
        for e in section.values()
        if e.get("model_type") == "api"
    )
    report["cost_summary"] = {
        "total_api_cost_usd":        round(total_api, 8),
        "local_model_cost_usd":      0.0,
        "scenario_2_cost_by_model":  s2_costs,
        "trap_cost_by_model":        trap_costs,
    }

    return report


def print_summary(report: dict) -> None:
    W = 64
    div = "═" * W

    def row(cols, widths):
        return "  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))

    print()
    print(div)
    print("  Shepdog Behavioral Monitoring — Run Summary")
    print(div)
    print(f"  Generated  : {report['generated_at'][:19]} UTC")
    print(f"  Records    : {report['record_count']}")
    print()

    # ── Scenario 2: Multi-Model Comparison ────────────────────────────────────
    s2 = report["scenario_2_multimodel"]
    if s2:
        print("  ── Scenario 2: Autonomous Email Task ─────────────────────")
        hdrs   = ["Model", "Observed", "Claimed", "Delta", "Verdict"]
        widths = [22, 9, 8, 6, 10]
        print(row(hdrs, widths))
        print("  " + "  ".join("─" * w for w in widths))
        for model, e in s2.items():
            sig = e.get("behavioral_signals", {})
            print(row([
                model,
                sig.get("http_calls_observed", "?"),
                sig.get("http_calls_claimed",  "?"),
                sig.get("call_delta",          "?"),
                e.get("verdict", "?"),
            ], widths))
        print()

    # ── Empty Success Trap ─────────────────────────────────────────────────────
    trap = report["goal_2_empty_success_trap"]
    if trap:
        print("  ── Empty Success Trap: Inventory API ─────────────────────")
        hdrs   = ["Model", "Init Field", "Retried", "Found Data", "Verdict"]
        widths = [22, 11, 8, 11, 10]
        print(row(hdrs, widths))
        print("  " + "  ".join("─" * w for w in widths))
        for model, e in trap.items():
            sig = e.get("behavioral_signals", {})
            print(row([
                model,
                sig.get("initial_query_field", "?"),
                str(sig.get("retry_attempted",  "?")),
                str(sig.get("found_real_data",  "?")),
                e.get("verdict", "?"),
            ], widths))
        print()

    # ── Failure Mode Reference ─────────────────────────────────────────────────
    ref = report["failure_mode_reference"]
    if ref:
        print("  ── Failure Mode Reference ────────────────────────────────")
        hdrs   = ["Name", "Failure Mode", "Verdict"]
        widths = [22, 26, 10]
        print(row(hdrs, widths))
        print("  " + "  ".join("─" * w for w in widths))
        for name, e in ref.items():
            print(row([
                name,
                e.get("failure_mode") or "none",
                e.get("verdict", "?"),
            ], widths))
        print()

    print(div)


def main() -> None:
    print("[generate_report] Loading records from service-records/ ...")
    records = load_records()
    print(f"[generate_report] Loaded {len(records)} record(s).")

    if not records:
        print("[generate_report] WARNING: No records found. Run the scenarios first.")

    report = build_report(records)
    print_summary(report)

    with open(REPORT_OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[generate_report] report.json → {REPORT_OUT}")


if __name__ == "__main__":
    main()
