"""Factual exploration inventory and an optional coordinator stop review."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .runtime_feedback import REPORT_EVENT_KINDS, new_research_feedback
from .runtime_reframe import reframe_inventory
from .runtime_types import _text
from .schema import safe_id


OPEN_FINDING_STATUSES = frozenset({"unknown", "inconclusive", "mixed"})


def _review_reports(store: Any, mission_id: str, tasks: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    reports = []
    for event in store.events(mission_id):
        if event["kind"] not in REPORT_EVENT_KINDS:
            continue
        payload = event["payload"]
        task = tasks.get(payload.get("task_id"))
        reports.append({
            "version": event["version"], "kind": event["kind"],
            "task_id": payload.get("task_id"),
            "research_node_id": task["scope"].get("research_node_id") if task else None,
            "research_revision": task["scope"].get("research_revision") if task else None,
            "candidate_id": payload.get("candidate_id"),
            "idea_id": payload.get("idea_id") or (task["scope"].get("idea_id") if task else None),
        })
    return reports


def build_portfolio_context(store: Any, mission_id: str) -> dict[str, Any]:
    """Describe recorded work without inferring novelty or mechanism diversity."""
    mission = store.mission(mission_id)
    if mission is None:
        raise ValueError("unknown mission")
    graph = store.research_snapshot(mission_id)
    tasks = store.list_tasks(mission_id)
    task_by_id = {task["task_id"]: task for task in tasks}
    runs = store.list_runs(mission_id)
    nodes = {node["node_id"]: {
        "node_id": node["node_id"], "revision": node["revision"],
        "question": node["question"], "execution_status": node["execution_status"],
        "finding_status": node["finding_status"],
        "task_ids": [], "tasks": [], "task_status_counts": {}, "pending_task_ids": [],
        "run_ids": [], "runs": [], "run_status_counts": {},
    } for node in graph["nodes"]}
    unbound_task_ids = []
    for task in tasks:
        node = nodes.get(task["scope"].get("research_node_id"))
        if node is None:
            unbound_task_ids.append(task["task_id"])
            continue
        node["task_ids"].append(task["task_id"])
        node["tasks"].append({"task_id": task["task_id"], "role": task["role"],
                              "status": task["status"],
                              "research_revision": task["scope"].get("research_revision")})
        if task["status"] in {"queued", "running"}:
            node["pending_task_ids"].append(task["task_id"])
        counts = Counter(node["task_status_counts"])
        counts[task["status"]] += 1
        node["task_status_counts"] = dict(counts)
    unbound_run_ids = []
    for run in runs:
        task = task_by_id.get(run["task_id"])
        node_id = task["scope"].get("research_node_id") if task else None
        if node_id is None:
            ref = run.get("inputs", {}).get("research_node")
            node_id = ref.get("node_id") if isinstance(ref, Mapping) else None
        node = nodes.get(node_id)
        if node is None:
            unbound_run_ids.append(run["run_id"])
            continue
        node["run_ids"].append(run["run_id"])
        ref = run.get("inputs", {}).get("research_node")
        node["runs"].append({"run_id": run["run_id"], "status": run["status"],
                             "task_id": run["task_id"],
                             "research_revision": (task["scope"].get("research_revision") if task else
                                                   ref.get("revision") if isinstance(ref, Mapping) else None)})
        counts = Counter(node["run_status_counts"])
        counts[run["status"]] += 1
        node["run_status_counts"] = dict(counts)
    reports = _review_reports(store, mission_id, task_by_id)
    feedback = new_research_feedback(store, mission_id, 0, final_review=True)
    feedback_index = []
    for event in feedback:
        payload = event["payload"]
        run_id = payload.get("run_id")
        run = store.run(run_id) if run_id else None
        ref = {key: payload[key] for key in ("run_id", "candidate_id", "idea_id", "idea_ref")
               if payload.get(key) is not None}
        if event["kind"] == "communication_published" and payload.get("manifest_sha256"):
            ref["manifest_sha256"] = payload["manifest_sha256"]
        feedback_index.append({
            "version": event["version"], "kind": event["kind"],
            "task_id": payload.get("task_id") or (run.get("task_id") if run else None),
            "ref": ref,
        })
    return {
        "mission_id": mission_id, "cursor": mission["version"],
        "nodes": list(nodes.values()),
        "nodes_without_runs": [node_id for node_id, node in nodes.items() if not node["run_ids"]],
        "nodes_with_open_findings": [node_id for node_id, node in nodes.items()
                                     if node["finding_status"] in OPEN_FINDING_STATUSES],
        "unbound_task_ids": unbound_task_ids, "unbound_run_ids": unbound_run_ids,
        "review_reports": reports,
        "problem_reframes": reframe_inventory(store, mission_id),
        "latest_review_event_version": max((report["version"] for report in reports), default=0),
        "final_review_feedback": feedback_index,
        "latest_final_review_feedback_version": feedback[-1]["version"] if feedback else 0,
        "remaining_agent_tasks": store.remaining_agent_tasks(mission_id),
        "remaining_runs": store.remaining_runs(mission_id),
    }


def validate_exploration_review(store: Any, mission_id: str, payload: Mapping[str, Any],
                                task: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate review provenance; content remains the coordinator's judgment."""
    mission = store.mission(mission_id)
    if mission is None:
        raise ValueError("unknown mission")
    if task.get("mission_id") != mission_id or task.get("role") != "coordinator":
        raise ValueError("exploration review requires a coordinator task in this Mission")
    raw = payload.get("exploration_review")
    if raw is None:
        if (mission["payload"]["resources"].get("exploration_review") is True
                and payload.get("decision") == "stop"):
            raise ValueError("stop requires exploration_review for this Mission")
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("exploration_review must be an object")
    alternatives = raw.get("alternatives")
    if not isinstance(alternatives, list):
        raise ValueError("exploration_review.alternatives must be a list")
    normalized = []
    for index, alternative in enumerate(alternatives):
        label = f"exploration_review.alternatives[{index}]"
        if not isinstance(alternative, Mapping):
            raise ValueError(f"{label} must be an object")
        node_id = safe_id(alternative.get("node_id"), f"{label}.node_id")
        revision = alternative.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError(f"{label}.revision must be a positive integer")
        if store.research_node(mission_id, node_id, revision) is None:
            raise ValueError(f"{label} references a node revision outside this Mission")
        disposition = alternative.get("disposition")
        if disposition not in {"continue", "defer", "reject"}:
            raise ValueError(f"{label}.disposition must be continue, defer, or reject")
        normalized.append({
            "node_id": node_id, "revision": revision,
            "mechanism": _text(alternative.get("mechanism"), f"{label}.mechanism"),
            "disposition": disposition,
            "reason": _text(alternative.get("reason"), f"{label}.reason"),
            "next_test": _text(alternative.get("next_test"), f"{label}.next_test"),
        })
    unknowns = raw.get("remaining_unknowns")
    if not isinstance(unknowns, list):
        raise ValueError("exploration_review.remaining_unknowns must be a list")
    unknowns = [_text(item, "exploration_review.remaining_unknowns item") for item in unknowns]
    version = raw.get("reviewed_event_version")
    if type(version) is not int or not 0 <= version <= mission["version"]:
        raise ValueError("exploration_review.reviewed_event_version is invalid")
    if payload.get("decision") == "stop":
        latest_feedback = max((event["version"] for event in new_research_feedback(
            store, mission_id, 0, final_review=True) if event["version"] <= task["input_version"]), default=0)
        if version < latest_feedback:
            raise ValueError("exploration_review does not cover the latest final-review feedback")
    else:
        latest_report = max((event["version"] for event in store.events(mission_id)
                             if event["kind"] in REPORT_EVENT_KINDS), default=0)
        if version < latest_report:
            raise ValueError("exploration_review does not cover the latest review report")
    return {
        "alternatives": normalized,
        "narrowing_reason": _text(raw.get("narrowing_reason"), "exploration_review.narrowing_reason"),
        "remaining_unknowns": unknowns,
        "next_test_decision": _text(raw.get("next_test_decision"), "exploration_review.next_test_decision"),
        "reviewed_event_version": version,
    }
