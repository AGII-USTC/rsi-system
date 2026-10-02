"""Bounded correction of a rejected coordinator acknowledgement, not new research."""
import json
from pathlib import Path

from .runtime_reframe import reframe_inventory
from .runtime_limits import recovery_parent


def repair_source(store, task):
    repairs = {event["payload"].get("repair_task_id"): event["payload"]
               for event in store.events(task["mission_id"])
               if event["kind"] == "coordinator_response_repair_scheduled"}
    seen = set()
    while task and task["task_id"] not in seen:
        seen.add(task["task_id"])
        if task["task_id"] in repairs:
            return repairs[task["task_id"]]
        task = recovery_parent(store, task)
    return None


def successful_response_repairs(store, mission_id, *, since_version=0):
    """Count protocol failures whose bounded correction actually completed."""
    repaired = 0
    for event in store.events(mission_id):
        if event["kind"] != "coordinator_response_repair_scheduled" or event["version"] < since_version:
            continue
        payload = event["payload"]
        repair_id = payload.get("repair_task_id")
        repair = store.task(repair_id) if repair_id else None
        if not repair or repair["status"] != "completed":
            continue
        acknowledged = any(item["kind"] == "coordinator_decision" and
                           item["payload"].get("task_id") == repair_id and
                           item["version"] >= event["version"]
                           for item in store.events(mission_id))
        if acknowledged:
            repaired += 1
    return repaired


def schedule_response_repair(coordinator, store, task, exc):
    if task["role"] != "coordinator" or not isinstance(exc, ValueError):
        return False
    error = str(exc)
    # These are model-facing response contract failures.  They are safe to
    # correct in a read-only continuation because no new dispatch is allowed;
    # authorization, lease, and storage failures must still fail normally.
    protocol_error = (error.startswith("challenge ") or error.startswith("challenge_")
                      or error.startswith("coordinator must respond to the completed independent problem reframe")
                      or error.startswith("coordinator decision")
                      or error.startswith("coordinator summary")
                      or error.startswith("coordinator expected_information")
                      or error.startswith("coordinator budget_impact")
                      or error.startswith("coordinator stop_condition")
                      or error.startswith("coordinator tasks")
                      or error.startswith("coordinator can only schedule")
                      or error.startswith("coordinator task references")
                      or error.startswith("coordinator task requires")
                      or error.startswith("coordinator must")
                      or error.startswith("stop_reframe_decision")
                      or error.startswith("deferred stop reframe")
                      or error.startswith("reviewed failure")
                      or error.startswith("reviewed_failures")
                      or error.startswith("decision correction")
                      or error.startswith("agent response must be one JSON object"))
    if not protocol_error:
        return False
    source = repair_source(store, task)
    # One correction attempt per rejected scientific decision. Its transport
    # retry remains separately bounded by the runtime's normal recovery policy.
    if source:
        store.event(task["mission_id"], "coordinator_response_repair_exhausted",
                    {"task_id": task["task_id"], "error": error,
                     "protocol_repair": True})
        return True
    receipt = coordinator.runtime_root / "tasks" / task["task_id"] / "agent_result.json"
    events = coordinator.runtime_root / "tasks" / task["task_id"] / "events.jsonl"
    # A parser failure can happen before agent_result.json is written.  The
    # event stream is still an immutable receipt and is enough to explain the
    # correction without replaying the provider call.
    if not receipt.is_file() and not events.is_file():
        return False
    recovery_id = coordinator._schedule_tool_continuation(
        store, task, allow_without_tools=True,
        recovery_reason=(f"Correct rejected Coordinator response: {error}; use the saved error and handoff index, "
                         "do not repeat research or dispatch work"))
    if recovery_id:
        store.event(task["mission_id"], "coordinator_response_repair_scheduled", {
            "source_task_id": task["task_id"], "repair_task_id": recovery_id,
            "error": error, "protocol_repair": True})
    return True


def repair_prompt(coordinator, store, task, communication_manifest=None):
    source = repair_source(store, task)
    if not source:
        return None
    source_dir = coordinator.runtime_root / "tasks" / source["source_task_id"]
    receipt = source_dir / "agent_result.json"
    events = source_dir / "events.jsonl"
    original = None
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        raw = saved.get("payload", saved)
        if isinstance(raw, dict):
            # Keep the correction prompt small.  The complete receipt remains
            # frozen in the source task and can be inspected by an auditor.
            original = {"top_level_keys": sorted(raw)[:64],
                        "decision": raw.get("decision"),
                        "error": raw.get("error")}
        else:
            original = {"type": type(raw).__name__}
    inbox = Path(communication_manifest) if communication_manifest else None
    indexed_files = []
    if inbox and inbox.is_file():
        try:
            data = json.loads(inbox.read_text())
            indexed_files = [{"path": item.get("path"), "file_id": item.get("file_id"),
                              "sha256": item.get("sha256")}
                             for item in data.get("files", []) if item.get("path")]
        except (OSError, ValueError, TypeError):
            indexed_files = []
    return "Correct one rejected coordinator response. Research and dispatch are disabled.\n" + json.dumps({
        "role": "coordinator",
        "response_contract": {"decision": "wait (the runtime will reject schedule or stop during repair)",
                              "summary": "Explain the corrected acknowledgement and what remains pending",
                              "expected_information": "string",
                              "budget_impact": "string",
                              "stop_condition": "string",
                              "challenge_responses": "List of {task_id, disposition: adopt|defer|reject, reason, next_test}",
                              "tasks": "must be []",
                              "messages": "must be []"},
        "context": {"mission_id": task["mission_id"], "input_version": task["input_version"],
                    "rejection": source["error"], "original_response": original,
                    "source_task_id": source["source_task_id"],
                    "receipt_files": {"communications_index": str(inbox) if inbox else None,
                                      "available_files": indexed_files},
                    "source_audit_receipts": {"agent_result": str(receipt) if receipt.is_file() else None,
                                               "events": str(events) if events.is_file() else None,
                                               "access": "preserved for audit; use communications_index for model-readable copies"},
                    "reframes": reframe_inventory(store, task["mission_id"]),
                    "read_policy": "Use the listed receipt paths and communications index as read-only evidence. Do not replay the full Mission history or repeat the provider request.",
                    "instructions": "The original response was rejected by the runtime. Describe the exact rejection and correct only the outer response contract. Acknowledge only completed, unacknowledged reports with report_version <= input_version. Pending/failed reports cannot be acknowledged; describe them as pending in summary. Return a JSON object with decision=wait, all required top-level fields, challenge_responses, tasks=[], and messages=[]. Do not call delegation, research, runner, or messaging tools. The next normal coordinator turn owns further research decisions."}},
        ensure_ascii=False)


def normalize_repaired_response(payload):
    # A rejected normal response may have carried reviewed_failures. During a
    # wait-only repair this is historical acknowledgement metadata, not a new
    # stop decision; accept and ignore it instead of turning a repair into a
    # second protocol failure.
    allowed = {"decision", "summary", "expected_information", "budget_impact", "stop_condition",
               "challenge_responses", "reviewed_failures"}
    if not isinstance(payload, dict) or set(payload) - (allowed | {"tasks", "messages"}):
        raise ValueError("decision correction can only supply the read-only coordinator response fields")
    if payload.get("decision") not in {None, "wait"}:
        raise ValueError("decision correction must use decision=wait")
    if not isinstance(payload.get("challenge_responses", []), list):
        raise ValueError("decision correction challenge_responses must be a list")
    if payload.get("tasks", []) != [] or payload.get("messages", []) != []:
        raise ValueError("decision correction must not dispatch tasks or messages")
    return {"decision": "wait",
            "summary": payload.get("summary", "Corrected response; no new research dispatch"),
            "challenge_responses": payload.get("challenge_responses", []),
            "tasks": [], "messages": [],
            "expected_information": payload.get("expected_information", "Review available branch results in a subsequent normal coordinator turn"),
            "budget_impact": payload.get("budget_impact", "One bounded response correction; no research tasks or experiments dispatched"),
            "stop_condition": payload.get("stop_condition", "No Mission stop requested by response correction")}
