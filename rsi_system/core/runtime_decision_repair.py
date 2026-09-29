"""Bounded correction of a rejected coordinator acknowledgement, not new research."""
import json

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


def schedule_response_repair(coordinator, store, task, exc):
    if task["role"] != "coordinator" or not isinstance(exc, ValueError):
        return False
    if not (str(exc).startswith("challenge ") or str(exc).startswith("challenge_")
            or str(exc).startswith("coordinator must respond to the completed independent problem reframe")):
        return False
    source = repair_source(store, task)
    # One correction attempt per rejected scientific decision. Its transport
    # retry remains separately bounded by the runtime's normal recovery policy.
    if source:
        store.event(task["mission_id"], "coordinator_response_repair_exhausted",
                    {"task_id": task["task_id"], "error": str(exc)})
        return True
    receipt = coordinator.runtime_root / "tasks" / task["task_id"] / "agent_result.json"
    if not receipt.is_file():
        return False
    recovery_id = coordinator._schedule_tool_continuation(
        store, task, allow_without_tools=True,
        recovery_reason="Correct only rejected challenge acknowledgements using current completed reports; do not repeat research or dispatch work")
    if recovery_id:
        store.event(task["mission_id"], "coordinator_response_repair_scheduled", {
            "source_task_id": task["task_id"], "repair_task_id": recovery_id,
            "error": str(exc)})
    return True


def repair_prompt(coordinator, store, task):
    source = repair_source(store, task)
    if not source:
        return None
    receipt = coordinator.runtime_root / "tasks" / source["source_task_id"] / "agent_result.json"
    saved = json.loads(receipt.read_text())
    original = saved.get("payload", saved)
    return "Correct one rejected coordinator response. Research and dispatch are disabled.\n" + json.dumps({
        "role": "coordinator",
        "response_contract": {"summary": "Explain the corrected acknowledgement and what remains pending",
                              "challenge_responses": "List of {task_id, disposition: adopt|defer|reject, reason, next_test}"},
        "context": {"mission_id": task["mission_id"], "input_version": task["input_version"],
                    "rejection": source["error"], "original_response": original,
                    "reframes": reframe_inventory(store, task["mission_id"]),
                    "instructions": "Acknowledge only completed, unacknowledged reports with report_version <= input_version. Pending/failed reports cannot be acknowledged; describe them as pending in summary. Return only summary and challenge_responses. The runtime records a wait decision and schedules no new work; the next normal coordinator turn owns further research decisions."}},
        ensure_ascii=False)


def normalize_repaired_response(payload):
    if not isinstance(payload, dict) or set(payload) - {"summary", "challenge_responses"}:
        raise ValueError("decision correction can only supply summary and challenge_responses")
    return {**payload, "decision": "wait", "tasks": [], "messages": [],
            "expected_information": "Review available branch results in a subsequent normal coordinator turn",
            "budget_impact": "One bounded response correction; no research tasks or experiments dispatched",
            "stop_condition": "No Mission stop requested by response correction"}
