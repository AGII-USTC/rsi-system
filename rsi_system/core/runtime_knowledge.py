"""Retryable publication of immutable runtime Runs as unconfirmed Wiki evidence."""
from __future__ import annotations
import hashlib
import fcntl
from pathlib import Path
from .knowledge import write_candidate
from .provenance import sha256_file
from .runtime_types import _json
from .runtime_curator import archive_records, pending_record_ids, publish_curator_pages


def reconcile_runtime_knowledge(root, store, mission_id):
    lock_path = Path(root) / ".rsi/runtime/publication.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _reconcile_runtime_knowledge(root, store, mission_id)


def _reconcile_runtime_knowledge(root, store, mission_id):
    root = Path(root).resolve()
    written, errors = [], []
    mission = store.mission(mission_id)
    if (mission["payload"]["resources"].get("knowledge_curator") is True or
            any(task["role"] == "curator" for task in store.list_tasks(mission_id))):
        try:
            archive_records(root, store, mission_id)
            curated = {event["payload"]["task_id"] for event in store.events(mission_id)
                       if event["kind"] == "knowledge_curated"}
            rejected = {event["payload"]["task_id"] for event in store.events(mission_id)
                        if event["kind"] == "knowledge_curation_rejected"}
            for task in store.list_tasks(mission_id):
                if (task["role"] != "curator" or task["status"] != "completed" or
                        task["task_id"] in curated | rejected):
                    continue
                source_ids = pending_record_ids(store, mission_id, through_version=task["input_version"])
                try:
                    publication = publish_curator_pages(root, store, task)
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    store.event(mission_id, "knowledge_curation_rejected", {
                        "task_id": task["task_id"], "source_event_ids": source_ids, "error": str(exc)})
                else:
                    store.event(mission_id, "knowledge_curated", {
                        "task_id": task["task_id"], **publication})
                    written.append({"task_id": task["task_id"], **publication})
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            errors.append({"curator": mission_id, "error": str(exc)})
    events = store.events(mission_id)
    published_ideas = {e["payload"]["source_event_id"] for e in events if e["kind"] == "idea_written_to_wiki"}
    for event in events:
        if event["kind"] != "idea_drafted" or "idea" not in event["payload"] or event["event_id"] in published_ideas:
            continue
        try:
            from .runtime import write_idea
            page = write_idea(root, mission_id, event["payload"]["idea"])
            task_id = event["payload"].get("task_id")
            task = store.task(task_id) if task_id else None
            store.event(mission_id, "idea_written_to_wiki", {
                "source_event_id": event["event_id"], "path": page, "task_id": task_id,
                "role": task["role"] if task else None, "evidence_status": "draft, unverified"})
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"idea_event_id": event["event_id"], "error": str(exc)})
    recorded = {event["payload"].get("run_id") for event in store.events(mission_id)
                if event["kind"] == "run_written_to_wiki"}
    for run in store.list_runs(mission_id):
        if run["run_id"] in recorded or run["status"] not in {"completed", "failed", "interrupted", "analyzed"}:
            continue
        try:
            if hashlib.sha256(_json(run["evidence"]).encode()).hexdigest() != run["evidence_sha256"]:
                raise ValueError("runtime Run evidence hash mismatch")
            evidence_file = root / ".rsi/runtime/knowledge-evidence" / f"{run['run_id']}.json"
            evidence_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = _json(run)
            if evidence_file.exists():
                if evidence_file.read_text(encoding="utf-8") != encoded:
                    raise ValueError("immutable runtime evidence differs from saved record")
            else:
                # Exclusive creation preserves any conflicting existing evidence.
                with evidence_file.open("x", encoding="utf-8") as handle:
                    handle.write(encoded)
            evidence = [{"path": evidence_file.relative_to(root).as_posix(),
                         "sha256": sha256_file(evidence_file)}]
            raw = run["evidence"] or {}
            if raw.get("evidence_path"):
                artifact = Path(raw["evidence_path"]).resolve()
                if not artifact.is_relative_to(root):
                    raise ValueError("Run artifact is outside the project")
                evidence.append({"path": artifact.relative_to(root).as_posix(),
                                 "sha256": raw["evidence_sha256"]})
            if run.get("kind") == "exploration":
                store.event(mission_id, "run_written_to_wiki", {
                    "run_id": run["run_id"], "kind": "exploration", "evidence": evidence,
                    "task_id": run.get("task_id"), "role": "runner",
                    "evidence_status": "execution record only; no wiki finding page created"})
                continue
            result = write_candidate(root, {
                "experiment_id": run["run_id"], "status": run["status"],
                "summary": f"Mission {mission_id}; Candidate {run['candidate_id']}; "
                           f"Run {run['run_id']}: {run['status']}. Recorded execution evidence; "
                           "scientific interpretation and confirmation remain separate.",
                "evidence": evidence})
            store.event(mission_id, "run_written_to_wiki", {
                "run_id": run["run_id"], "task_id": run.get("task_id"), "role": "runner",
                "evidence_status": "candidate, unconfirmed", **result})
            written.append(result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"run_id": run["run_id"], "error": str(exc)})
    return {"written": written, "errors": errors}
