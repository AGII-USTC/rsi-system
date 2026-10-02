"""Mid-course independent problem reframes and coordinator responses."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .runtime_feedback import new_research_feedback
from .runtime_types import _text
from .schema import safe_id


def validate_reframe_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("reframe_review")
    if not isinstance(raw, Mapping):
        raise ValueError("problem reframe requires reframe_review")
    alternatives = raw.get("alternative_explanations")
    checks = raw.get("discriminating_checks")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("reframe_review.alternative_explanations must be a nonempty list")
    if not isinstance(checks, list):
        raise ValueError("reframe_review.discriminating_checks must be a list")
    if not checks:
        _text(raw.get("no_useful_check_reason"), "reframe_review.no_useful_check_reason")
    return {
        "problem_statement": _text(raw.get("problem_statement"), "reframe_review.problem_statement"),
        "alternative_explanations": [
            _text(item, "reframe_review.alternative_explanations item") for item in alternatives],
        "discriminating_checks": [
            _text(item, "reframe_review.discriminating_checks item") for item in checks],
        "no_useful_check_reason": raw.get("no_useful_check_reason") if not checks else None,
        "recommendation": _text(raw.get("recommendation"), "reframe_review.recommendation"),
    }


def reframe_inventory(store: Any, mission_id: str) -> list[dict[str, Any]]:
    acknowledged = {item.get("task_id") for event in store.events(mission_id)
                    if event["kind"] == "coordinator_decision"
                    for item in event["payload"].get("challenge_responses", [])}
    reports = {event["payload"].get("task_id"): event["version"]
               for event in store.events(mission_id) if event["kind"] == "problem_reframe_report"}
    result = []
    for task in store.list_tasks(mission_id):
        if task["scope"].get("task_kind") != "problem_reframe":
            continue
        review = ((task.get("output") or {}).get("payload") or {}).get("reframe_review")
        result.append({"task_id": task["task_id"], "status": task["status"],
                       "report_version": reports.get(task["task_id"]),
                       "acknowledged": task["task_id"] in acknowledged,
                       "problem_statement": review.get("problem_statement") if isinstance(review, Mapping) else None,
                       "recommendation": review.get("recommendation") if isinstance(review, Mapping) else None})
    return result


def validate_challenge_responses(store: Any, mission_id: str, task: Mapping[str, Any],
                                 payload: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("challenge_responses", [])
    if not isinstance(raw, list):
        raise ValueError("challenge_responses must be a list")
    normalized = []
    seen = set()
    inventory = {item["task_id"]: item for item in reframe_inventory(store, mission_id)}
    eligible = {task_id for task_id, item in inventory.items()
                if item["status"] == "completed"
                and item["report_version"] is not None
                and item["report_version"] <= task["input_version"]
                and not item["acknowledged"]}
    if raw and not eligible:
        raise ValueError(
            "challenge_responses must be [] because no completed unacknowledged reframe "
            "is visible at this input version")
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("challenge response must be an object")
        task_id = safe_id(item.get("task_id"), "challenge response task_id")
        challenge = store.task(task_id)
        prior = inventory.get(task_id)
        if (not challenge or challenge["mission_id"] != mission_id or
                challenge["scope"].get("task_kind") != "problem_reframe" or
                challenge["status"] != "completed" or task_id in seen or not prior or
                prior["acknowledged"] or prior["report_version"] is None or
                prior["report_version"] > task["input_version"]):
            raise ValueError(
                "challenge response must reference one completed unacknowledged reframe "
                "visible at this input version")
        disposition = item.get("disposition")
        if disposition not in {"adopt", "defer", "reject"}:
            raise ValueError("challenge disposition must be adopt, defer, or reject")
        normalized.append({"task_id": task_id, "disposition": disposition,
                           "reason": _text(item.get("reason"), "challenge response reason"),
                           "next_test": _text(item.get("next_test"), "challenge response next_test")})
        seen.add(task_id)
    for challenge in inventory.values():
        if (challenge["report_version"] is not None and
                challenge["report_version"] <= task["input_version"] and
                not challenge["acknowledged"] and challenge["task_id"] not in seen):
            raise ValueError("coordinator must respond to the completed independent problem reframe")
    return normalized


def stop_reframe_decision_required(store: Any, mission_id: str, task: Mapping[str, Any],
                                   review: Mapping[str, Any] | None) -> bool:
    mission = store.mission(mission_id)
    if mission["payload"]["resources"].get("exploration_review") is not True:
        return False
    remaining = store.remaining_agent_tasks(mission_id)
    if remaining == 0:
        return False
    feedback = new_research_feedback(store, mission_id, 0, final_review=True)
    return bool((review or {}).get("remaining_unknowns") or any(
        event["version"] <= task["input_version"] for event in feedback))


def validate_stop_reframe_decision(store: Any, mission_id: str, task: Mapping[str, Any],
                                   payload: Mapping[str, Any], review: Mapping[str, Any] | None,
                                   responses: list[dict[str, str]]) -> dict[str, Any] | None:
    raw = payload.get("stop_reframe_decision")
    required = (payload.get("decision") == "stop" and
                stop_reframe_decision_required(store, mission_id, task, review))
    if raw is None:
        if required:
            raise ValueError("stop requires stop_reframe_decision while findings remain open")
        return None
    if payload.get("decision") != "stop" or not isinstance(raw, Mapping):
        raise ValueError("stop_reframe_decision must be an object on stop")
    disposition = raw.get("disposition")
    if disposition not in {"reviewed", "defer"}:
        raise ValueError("stop_reframe_decision.disposition must be reviewed or defer")
    evidence_refs = raw.get("evidence_refs")
    if not isinstance(evidence_refs, list) or len(evidence_refs) != len(set(
            str(ref) for ref in evidence_refs)):
        raise ValueError("stop_reframe_decision.evidence_refs must be a unique list")
    visible_events = [event for event in store.events(mission_id)
                      if event["version"] <= task["input_version"]]
    visible_runs = {event["payload"].get("run_id") for event in visible_events
                    if event["kind"] == "run_registered"}
    visible_tasks = {event["payload"].get("task_id") for event in visible_events
                     if event["kind"] == "communication_published" and
                     event["payload"].get("task_status") == "completed"}
    checked_refs = []
    for ref in evidence_refs:
        if not isinstance(ref, str) or ":" not in ref:
            raise ValueError("stop_reframe_decision evidence ref must be run:<id> or task:<id>")
        kind, identifier = ref.split(":", 1)
        if kind == "run":
            run = store.run(identifier)
            valid = bool(run and run["mission_id"] == mission_id and identifier in visible_runs)
        elif kind == "task":
            source = store.task(identifier)
            valid = bool(source and source["mission_id"] == mission_id and
                         source["status"] == "completed" and identifier in visible_tasks)
        else:
            valid = False
        if not valid:
            raise ValueError("stop_reframe_decision evidence ref is not published in this Mission")
        checked_refs.append(ref)
    if required and visible_runs | visible_tasks and not checked_refs:
        raise ValueError("stop_reframe_decision needs a published evidence ref")
    reframe_task_id = raw.get("reframe_task_id")
    if disposition == "reviewed":
        challenge = store.task(reframe_task_id) if isinstance(reframe_task_id, str) else None
        inventory = {item["task_id"]: item for item in reframe_inventory(store, mission_id)}
        item = inventory.get(reframe_task_id)
        answered = bool(item and (item["acknowledged"] or any(
            response["task_id"] == reframe_task_id for response in responses)))
        if (not challenge or challenge["mission_id"] != mission_id or
                challenge["scope"].get("task_kind") != "problem_reframe" or
                challenge["status"] != "completed" or not item or
                item["report_version"] is None or
                item["report_version"] > task["input_version"] or not answered):
            raise ValueError("stop_reframe_decision needs a visible answered independent reframe")
        later_feedback = [event for event in new_research_feedback(
            store, mission_id, item["report_version"], final_review=True)
            if event["version"] <= task["input_version"] and
            event["payload"].get("task_id") != reframe_task_id]
        if later_feedback:
            raise ValueError("stop_reframe_decision reviewed reframe predates newer research feedback")
    elif reframe_task_id is not None:
        raise ValueError("deferred stop reframe must not name a reviewed task")
    return {"disposition": disposition,
            "reason": _text(raw.get("reason"), "stop_reframe_decision.reason"),
            "evidence_refs": checked_refs,
            "next_check": _text(raw.get("next_check"), "stop_reframe_decision.next_check"),
            "reopen_condition": _text(raw.get("reopen_condition"), "stop_reframe_decision.reopen_condition"),
            "budget_impact": _text(raw.get("budget_impact"), "stop_reframe_decision.budget_impact"),
            "reframe_task_id": reframe_task_id if disposition == "reviewed" else None}
