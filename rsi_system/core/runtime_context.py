"""Source-addressed prompt views; complete records remain in read-only evidence."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .pi_agent import atomic_json


def _excerpt(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def compile_prompt_context(context: dict, evidence_manifest: Path, task: dict,
                           *, max_inline_bytes: int = 96000) -> dict:
    manifest_path = Path(evidence_manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["task_id"] != task["task_id"] or manifest["mission_id"] != task["mission_id"]:
        raise ValueError("context evidence manifest identity mismatch")
    source = manifest_path.parent / "context/full.json"
    snapshot = {"schema_version": 1, "task_id": task["task_id"],
                "mission_id": task["mission_id"], "input_version": task["input_version"],
                "context": context}
    raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if source.is_symlink() or source.parent.is_symlink():
        raise ValueError("context evidence symlink is forbidden")
    if source.exists():
        if source.read_bytes() != raw:
            raise ValueError("context evidence snapshot is already frozen")
    else:
        atomic_json(source, snapshot)
    relative = source.relative_to(manifest_path.parent).as_posix()
    existing = [item for item in manifest["files"] if item["path"] == relative]
    if existing:
        if len(existing) != 1 or existing[0]["sha256"] != digest:
            raise ValueError("context evidence snapshot hash mismatch")
    else:
        manifest["files"].append({"run_id": "context", "path": relative,
                                  "source": "Task prompt context snapshot", "sha256": digest,
                                  "verification": "task-scoped snapshot; recorded claims, not verified findings"})
        atomic_json(manifest_path, manifest)

    def ref(pointer: str) -> dict:
        return {"path": str(source), "sha256": digest, "json_pointer": "/context" + pointer}

    compact = copy.deepcopy(context)
    compact["context_sources"] = {
        "full_context_ref": ref(""),
        "note": "Verbatim excerpts and record addresses only. Read full records as needed with read; "
                "json_pointer identifies the record within JSON; read uses line offset/limit for paging. "
                "Array order is recorded order, not a scientific ranking. Runtime database records are unchanged."}
    tasks = []
    all_tasks = context.get("tasks", [])
    for index, item in enumerate(all_tasks):
        projected = {key: item[key] for key in
                     ("task_id", "role", "status", "parent_task_id", "input_version", "created_at") if key in item}
        projected["scope"] = {key: item.get("scope", {}).get(key) for key in
                              ("idea_id", "research_node_id", "research_revision")}
        projected["source_ref"] = ref(f"/tasks/{index}")
        if item.get("status") in {"queued", "running"}:
            projected["objective_excerpt"] = _excerpt(item.get("objective", ""), 512)
        if index >= len(all_tasks) - 24:
            output = item.get("output") or {}
            payload = output.get("payload") or {}
            if isinstance(payload, dict):
                projected["summary_excerpt"] = _excerpt(payload.get("summary", ""), 512)
                if payload.get("decision") is not None:
                    projected["decision"] = payload["decision"]
        tasks.append(projected)
    compact["tasks"] = tasks
    for index in range(len(all_tasks) - 1, -1, -1):
        item = all_tasks[index]
        payload = (item.get("output") or {}).get("payload") or {}
        if item.get("role") == "coordinator" and isinstance(payload, dict) and payload.get("decision"):
            compact["latest_coordinator_decision"] = {
                "task_id": item["task_id"], "decision": payload["decision"],
                "summary_excerpt": _excerpt(payload.get("summary", ""), 512),
                "source_ref": ref(f"/tasks/{index}")}
            break

    identities = {
        "events": ("event_id", "version", "kind", "created_at"),
        "hypotheses": ("hypothesis_id", "version", "idea_ref", "status"),
        "candidates": ("candidate_id", "hypothesis_id", "hypothesis_version", "idea_ref", "status"),
        "runs": ("run_id", "task_id", "candidate_id", "idea_ref", "status", "kind", "evidence_sha256"),
        "idea_runs": ("run_id", "task_id", "candidate_id", "idea_ref", "status", "kind", "evidence_sha256"),
    }
    for field, keys in identities.items():
        items = context.get(field, [])
        compact[field] = []
        for index, item in enumerate(items):
            projected = {key: item[key] for key in keys if key in item}
            projected["source_ref"] = ref(f"/{field}/{index}")
            if field == "events":
                payload = item.get("payload") or {}
                projected["payload_excerpt"] = _excerpt(payload, 384)
                if isinstance(payload, dict):
                    projected.update({key: payload[key] for key in ("task_id", "run_id", "decision") if key in payload})
            compact[field].append(projected)

    graph = context.get("research_graph") or {}
    graph_view = ({key: graph[key] for key in ("mission_id", "cursor", "root_node_id") if key in graph}
                  if "nodes" in graph else copy.deepcopy(graph))
    graph_fields = {
        "nodes": ("node_id", "revision", "event_version", "execution_status", "finding_status", "owner"),
        "relations": ("relation_id", "edge_id", "revision", "event_version", "kind", "source", "target", "status"),
        "compositions": ("composition_id", "node_id", "event_version", "status", "inputs", "lifecycle_revision"),
    } if "nodes" in graph else {}
    for field, keys in graph_fields.items():
        graph_view[field] = []
        for index, item in enumerate(graph.get(field, [])):
            projected = {key: item[key] for key in keys if key in item}
            projected["source_ref"] = ref(f"/research_graph/{field}/{index}")
            if field == "nodes":
                projected["question_excerpt"] = _excerpt(item.get("question", ""), 384)
                projected["summary_excerpt"] = _excerpt(item.get("summary", ""), 512)
            graph_view[field].append(projected)
    graph_view["source_ref"] = ref("/research_graph")
    compact["research_graph"] = graph_view
    if "exploration_portfolio" in context:
        portfolio = context["exploration_portfolio"]
        compact["exploration_portfolio"] = {
            key: portfolio[key] for key in ("mission_id", "cursor", "remaining_agent_tasks", "remaining_runs",
                                            "latest_review_event_version", "latest_final_review_feedback_version")
            if key in portfolio}
        compact["exploration_portfolio"]["source_ref"] = ref("/exploration_portfolio")

    # Recency and pending status bound presentation without assigning scientific value.
    def size() -> int:
        return len(json.dumps(compact, ensure_ascii=False).encode("utf-8"))

    if size() > max_inline_bytes:
        compact["tasks"] = [item for index, item in enumerate(tasks)
                            if item["status"] in {"queued", "running"} or index >= len(tasks) - 24]
        compact["context_sources"]["task_count"] = len(tasks)
        compact["context_sources"]["tasks_ref"] = ref("/tasks")
        for field in identities:
            if len(compact[field]) > 24:
                compact["context_sources"][field + "_count"] = len(compact[field])
                compact["context_sources"][field + "_ref"] = ref("/" + field)
                compact[field] = compact[field][-24:]
    if size() > max_inline_bytes:
        for field in graph_fields:
            items = graph_view[field]
            if len(items) > 24:
                graph_view[field + "_count"] = len(items)
                graph_view[field + "_ref"] = ref("/research_graph/" + field)
                graph_view[field] = items[-24:]
    if size() > max_inline_bytes:
        compact["context_sources"]["tasks_ref"] = ref("/tasks")
        compact["context_sources"]["task_count"] = len(tasks)
        for recent_limit in (12, 6, 0):
            compact["tasks"] = [item for index, item in enumerate(tasks)
                                if item["status"] in {"queued", "running"}
                                or (recent_limit > 0 and index >= len(tasks) - recent_limit)]
            for field in identities:
                items = compact[field]
                if len(items) > recent_limit:
                    compact["context_sources"][field + "_count"] = len(context.get(field, []))
                    compact["context_sources"][field + "_ref"] = ref("/" + field)
                    compact[field] = items[-recent_limit:] if recent_limit else []
            for field in graph_fields:
                items = graph_view[field]
                if len(items) > recent_limit:
                    graph_view[field + "_count"] = len(graph.get(field, []))
                    graph_view[field + "_ref"] = ref("/research_graph/" + field)
                    graph_view[field] = items[-recent_limit:] if recent_limit else []
            if size() <= max_inline_bytes:
                break
    return compact
