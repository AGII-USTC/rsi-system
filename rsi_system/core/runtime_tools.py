"""Local Pi tool bridge backed by the Mission task ledger."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socketserver
from threading import Event, Lock, Thread
import time
from typing import Any, Mapping

from .runtime_communications import (checked_file, handoff_file_ref, load_manifest,
                                     publish_direct_messages, resolve_handoff_file)
from .runtime_store import RuntimeStore
from .runtime_research import research_root_id
from .runtime_curator import preflight_curator_pages
from .runtime_types import COLLABORATOR_ROLES, _json, _now, _text
from .schema import safe_id
from .runtime_research_contract import normalize_checkpoint, normalize_continuation, normalize_research_mandate

BRIDGE_HEARTBEAT_SECONDS = 10


def _bounded(value: Any, limit: int = 24000) -> Any:
    encoded = _json(value)
    return value if len(encoded) <= limit else {"excerpt": encoded[:limit], "truncated": True}


def _repair_args(tool: str, args: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Accept a few unambiguous JSON-shape slips before validating science data.

    Pi normally validates the TypeBox schema first. This second boundary also
    protects direct socket clients and recovered calls from harmless stringified
    JSON or numeric versions, while leaving ambiguous prose as a correctable
    error instead of guessing.
    """
    data = dict(args)
    changes: list[str] = []

    def parse_json_field(name: str, expected: type | tuple[type, ...]) -> None:
        value = data.get(name)
        if not isinstance(value, str):
            return
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return
        if isinstance(parsed, expected):
            data[name] = parsed
            changes.append(f"parsed {name} from JSON text")

    if tool == "rsi_register_hypothesis":
        for name in ("assumptions", "predictions"):
            parse_json_field(name, list)
        for name in ("experimental_design", "prior_art_status"):
            parse_json_field(name, (dict, str))
        version = data.get("expected_version")
        if isinstance(version, str) and version.strip().isdigit():
            data["expected_version"] = int(version.strip())
            changes.append("parsed expected_version as an integer")
        aliases = {"candidate-ready": "candidate_ready", "candidate ready": "candidate_ready"}
        if data.get("status") in aliases:
            data["status"] = aliases[data["status"]]
            changes.append("normalized hypothesis status")
    elif tool == "rsi_register_candidate":
        parse_json_field("config", dict)
        version = data.get("hypothesis_version")
        if isinstance(version, str) and version.strip().isdigit():
            data["hypothesis_version"] = int(version.strip())
            changes.append("parsed hypothesis_version as an integer")
    elif tool == "rsi_research" and data.get("action") in {
            "create", "revise", "relate", "compose", "revoke_composition", "rollback", "revoke"}:
        payload = data.get("data")
        if isinstance(payload, dict):
            if "reason" in data:
                legacy_reason = data.pop("reason")
                if "reason" in payload and legacy_reason != payload["reason"]:
                    raise ValueError("reason conflicts with data.reason; use only data.reason")
                if "reason" not in payload:
                    payload = {**payload, "reason": legacy_reason}
                    data["data"] = payload
                    changes.append("moved top-level reason to data.reason")
            _text(payload.get("reason"), "data.reason")
    return data, changes


class ToolBridge:
    def __init__(self, root: Path, task_id: str):
        from .runtime import RuntimeCoordinator
        self.coordinator = RuntimeCoordinator(root)
        self.task_id = safe_id(task_id, "task_id")
        with RuntimeStore(self.coordinator.store_path) as store:
            owner = store.task(self.task_id)
            self.lease_token = owner["lease_token"] if owner else None
        self.wait_started: float | None = None
        self.wait_total = 0.0
        self.mutex = Lock()
        self.worker: Thread | None = None
        self.worker_error: Exception | None = None

    def wait_elapsed(self) -> float:
        return self.wait_total + (time.monotonic() - self.wait_started if self.wait_started else 0.0)

    def _audit_bash(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from .audit_shell import MAX_COMMAND_SECONDS, run_audit_shell

        args = request["args"]
        raw_call_id = request.get("call_id")
        if not isinstance(raw_call_id, str) or not 1 <= len(raw_call_id) <= 256:
            raise ValueError("tool call ID must be a nonempty bounded string")
        call_id = hashlib.sha256(raw_call_id.encode()).hexdigest()
        command = args.get("command")
        timeout = args.get("timeout", MAX_COMMAND_SECONDS)
        if not isinstance(command, str) or not command.strip() or len(command.encode()) > 12_000:
            raise ValueError("audit bash command must be nonempty and at most 12000 bytes")
        if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_SECONDS:
            raise ValueError("audit bash timeout must be 1 to 60 seconds")
        if set(args) - {"command", "timeout"}:
            raise ValueError("audit bash accepts only command and timeout")
        digest = hashlib.sha256(_json({"tool": "rsi_audit_bash", "args": args}).encode()).hexdigest()
        with RuntimeStore(self.coordinator.store_path) as store:
            with store.atomic():
                task = self._current(store)
                if task["role"] not in {"analyzer", "critic"}:
                    raise ValueError("audit bash belongs to Analyzer and Critic")
                operation = store.tool_operation(self.task_id, call_id)
                if operation:
                    if operation["tool"] != "rsi_audit_bash" or operation["request_sha256"] != digest:
                        raise ValueError("tool call ID was reused with different arguments")
                    if operation["response_json"]:
                        return json.loads(operation["response_json"])
                    return {"ok": False, "error": {"code": "pending", "message":
                            "Audit command has a pending receipt; it will not be executed twice"}}
                used = sum(item["tool"] == "rsi_audit_bash" for item in store.tool_operations(self.task_id))
                if used >= 8:
                    raise ValueError("audit bash task limit of 8 commands reached")
                store.put_tool_operation(self.task_id, call_id, "rsi_audit_bash", digest, "pending")
                mission_id = task["mission_id"]
        task_dir = self.coordinator.runtime_root / "tasks" / self.task_id
        try:
            receipt = run_audit_shell(task_id=self.task_id, mission_id=mission_id,
                manifest_path=task_dir / "evidence/manifest.json", workspace=task_dir / "workspace",
                audit_dir=task_dir / "audit" / call_id, command=command, timeout_seconds=timeout,
                python_executable=self.coordinator._config().get("python"))
            response = {"ok": not bool(receipt.get("integrity_error")),
                        "receipt_path": str(task_dir / "audit" / call_id / "receipt.json"),
                        **receipt}
        except Exception as exc:
            response = {"ok": False, "error": {"code": "audit_failed", "message": str(exc)}}
        with RuntimeStore(self.coordinator.store_path) as store:
            with store.atomic():
                operation = store.tool_operation(self.task_id, call_id)
                if operation and operation["status"] == "pending":
                    store.put_tool_operation(self.task_id, call_id, "rsi_audit_bash", digest,
                                             "completed" if response["ok"] else "failed", response)
        return response

    def _current(self, store: RuntimeStore) -> dict[str, Any]:
        task = store.task(self.task_id)
        if not task:
            raise ValueError("unknown tool owner task")
        if not self.lease_token or task["lease_token"] != self.lease_token:
            raise ValueError("tool owner lease changed")
        store.validate_task(task)
        return task

    def _check_child(self, store: RuntimeStore, parent: dict[str, Any], target_id: str) -> dict[str, Any]:
        target = store.task(target_id)
        if not target or target["mission_id"] != parent["mission_id"]:
            raise ValueError("unknown child task in this Mission")
        if parent["role"] == "coordinator":
            return target
        node_id = parent["scope"].get("research_node_id")
        if node_id:
            graph = store.research_snapshot(parent["mission_id"])
            for composition in graph["compositions"]:
                if (composition["node_id"] == node_id
                        and composition.get("status") not in {"revoked", "rolled_back"}
                        and target_id in composition["plan"].get("frozen_source_task_ids", [])):
                    return target
        target_ancestors = set()
        ancestor = target["scope"]["parent_task_id"]
        while ancestor and ancestor not in target_ancestors:
            target_ancestors.add(ancestor)
            linked = store.task(ancestor)
            ancestor = linked["scope"]["parent_task_id"] if linked else None
        current = parent
        seen = set()
        while current and current["task_id"] not in seen:
            seen.add(current["task_id"])
            if (current["mission_id"] != parent["mission_id"]
                    or current["scope"].get("idea_id") != parent["scope"].get("idea_id")
                    or current["scope"].get("research_node_id") != node_id):
                break
            explicit = current["scope"].get("continuation") or {}
            if current["task_id"] in target_ancestors:
                return target
            if target_id == current["task_id"] or target_id in explicit.get("after_task_ids", []):
                if target["scope"].get("idea_id") == parent["scope"].get("idea_id") and (
                        not node_id or target["scope"].get("research_node_id") == node_id):
                    return target
            source_id = explicit.get("source_task_id") or current["scope"].get("reply_to_task_id")
            current = store.task(source_id) if source_id else None
        raise ValueError("task is not a descendant or a recorded research source of this Agent")

    def _result(self, store: RuntimeStore, target: dict[str, Any], checkpoint_id: str | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {"task_id": target["task_id"], "role": target["role"],
                                 "status": target["status"], "idea_id": target["scope"]["idea_id"],
                                 "candidate_id": target.get("candidate_id")}
        output = target.get("output") or {}
        if target["status"] == "failed":
            value["error"] = output.get("error") or target.get("error")
        checkpoints = store.research_checkpoints(target["mission_id"], target["task_id"])
        selected = None
        if checkpoint_id is not None:
            selected = next((item for item in checkpoints if item["checkpoint_id"] == checkpoint_id), None)
            if selected is None:
                raise ValueError("checkpoint_id is not attached to this task")
        if checkpoints:
            value["research_checkpoint_count"] = len(checkpoints)
            value["research_checkpoints"] = [{"checkpoint_id": item["checkpoint_id"],
                                              "event_version": item["event_version"],
                                              "problem_statement": item["payload"].get("problem_statement", "")[:1000],
                                              "summary_excerpt": item["payload"].get("summary", "")[:1600],
                                              "source_refs": [ref for change in item["payload"].get("changes", [])
                                                              for ref in change.get("source_refs", []) if len(ref) <= 1000][:4],
                                              "status": "published" if target["status"] == "completed" else "unpublished"}
                                             for item in checkpoints[-8:]]
            if selected is not None:
                value["research_checkpoint"] = {"checkpoint_id": selected["checkpoint_id"],
                                                 "event_version": selected["event_version"],
                                                 "payload": selected["payload"],
                                                 "status": "published" if target["status"] == "completed" else "unpublished",
                                                 "evidence_status": "agent-authored checkpoint, not an independently verified finding"}
        if target["status"] not in {"completed", "failed", "cancelled"}:
            return value
        payload = output.get("payload") or {}
        if payload:
            value["response"] = _bounded({key: item for key, item in payload.items()
                                          if key != "research_checkpoints"})
        if target["role"] == "curator":
            publications = [event for event in store.events(target["mission_id"])
                            if event["kind"] in {"knowledge_curated", "knowledge_curation_rejected"}
                            and event["payload"].get("task_id") == target["task_id"]]
            value["knowledge_publication"] = (
                {"status": "published" if publications[-1]["kind"] == "knowledge_curated" else "rejected",
                 **publications[-1]["payload"]} if publications else {"status": "pending"})
        drafted = [event["payload"] for event in store.events(target["mission_id"])
                   if event["kind"] == "idea_drafted" and event["payload"].get("task_id") == target["task_id"]]
        if drafted:
            value["ideas"] = [{"idea_id": item["idea_id"],
                               "title": item.get("idea", {}).get("title", "")}
                              for item in drafted]
        manifest_path = self.coordinator.runtime_root / "tasks" / target["task_id"] / "communications/outbox/manifest.json"
        if manifest_path.is_file():
            manifest = load_manifest(manifest_path, target)
            notes = []
            value["handoff_files"] = [handoff_file_ref(target["task_id"], item)
                                      for item in manifest["files"] if item.get("origin") == "agent_workspace"]
            for item in manifest["files"]:
                if item["path"].startswith("notes/") and item["path"].endswith((".md", ".txt")):
                    raw = checked_file(manifest_path.parent, item["path"], item["sha256"])
                    notes.append({"path": item["path"], "sha256": item["sha256"],
                                  "text": raw.decode("utf-8", "replace")[:8000]})
                if len(notes) >= 4:
                    break
            if notes:
                value["handoff"] = notes
        run = output.get("run")
        if run:
            evidence = dict(run.get("evidence") or {})
            directory = evidence.pop("artifact_dir", None)
            value["run"] = {"run_id": run["run_id"], "status": run["status"],
                            "evidence_sha256": run.get("evidence_sha256"), "evidence": _bounded(evidence)}
            if directory:
                logs = {}
                for name in ("stdout.log", "stderr.log"):
                    path = Path(directory) / name
                    expected = evidence.get("artifact_hashes", {}).get(name)
                    if expected and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                        logs[name] = path.read_text(errors="replace")[:8000]
                value["run"]["logs"] = logs
        return value

    def _messages(self, store: RuntimeStore, task: dict[str, Any], after: int, limit: int):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("inbox needs a nonnegative after_version and limit from 1 to 20")
        addressed = []
        rows = store.db.execute("SELECT * FROM blackboard_events WHERE mission_id=? "
                                "AND kind='direct_message' AND version>? ORDER BY version",
                                (task["mission_id"], after))
        for row in rows:
            event = dict(row)
            message = json.loads(event["payload_json"])
            if (task["scope"].get("task_kind") == "problem_reframe"
                    and message.get("from_role") == "coordinator"):
                continue
            same_idea = task["scope"]["idea_id"] == message["idea_id"]
            exact = message.get("to_task_id") is not None and message["to_task_id"] in {
                task["task_id"], task["scope"]["reply_to_task_id"]}
            role = message.get("to_role") == task["role"] and (same_idea or task["role"] == "coordinator")
            if exact or role:
                addressed.append({"event_id": event["event_id"], "version": event["version"],
                                  "created_at": event["created_at"], **message})
                if len(addressed) > limit:
                    break
        return addressed

    def _inbox(self, store: RuntimeStore, task: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        """Read messages after the frozen input without mutating its provenance.

        A durable receipt proves the response was saved, not received or understood.
        Actual exposure must be checked in the Pi session's tool-result records.
        Callers advance the cursor only after receiving the result; replay is safe.
        """
        after, limit = args.get("after_version", task["input_version"]), args.get("limit", 12)
        addressed = self._messages(store, task, after, limit)
        page = addressed[:limit]
        tasks = [item for item in store.list_tasks(task["mission_id"])
                 if item["status"] in {"queued", "running"} and (
                     task["role"] == "coordinator" or item["scope"]["idea_id"] == task["scope"]["idea_id"])]
        if task["scope"].get("task_kind") == "problem_reframe":
            tasks = [item for item in tasks if item["role"] != "coordinator"]
        return {"messages": page, "next_version": page[-1]["version"] if page else after,
                "has_more": len(addressed) > limit,
                "active_tasks": [{"task_id": item["task_id"], "role": item["role"],
                                  "status": item["status"], "parent_task_id": item["scope"]["parent_task_id"],
                                  "idea_id": item["scope"]["idea_id"], "objective": item["objective"][:300]}
                                 for item in tasks[:32]],
                "active_tasks_truncated": len(tasks) > 32}

    def _read_handoff(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        target = self._check_child(store, parent, safe_id(args.get("task_id"), "task_id"))
        if target["status"] not in {"completed", "failed", "cancelled"}:
            raise ValueError("handoff is available after the source task finishes")
        manifest_path = self.coordinator.runtime_root / "tasks" / target["task_id"] / "communications/outbox/manifest.json"
        manifest = load_manifest(manifest_path, target)
        item, ref = resolve_handoff_file(manifest, target["task_id"], file_id=args.get("file_id"))
        raw = checked_file(manifest_path.parent, item["path"], item["sha256"])
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("handoff file is not UTF-8 text") from exc
        offset, limit = args.get("offset", 0), args.get("limit", 12000)
        if type(offset) is not int or offset < 0 or offset > len(content) or (
                type(limit) is not int or not 1 <= limit <= 12000):
            raise ValueError("handoff offset or limit is outside the file")
        end = min(len(content), offset + limit)
        return {"task_id": target["task_id"], **ref, "text": content[offset:end],
                "next_offset": end if end < len(content) else None}

    def _delegate(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        role = args.get("role")
        if role not in COLLABORATOR_ROLES:
            raise ValueError("choose a registered collaborator role")
        task_kind = args.get("task_kind", "standard")
        if task_kind not in {"standard", "problem_reframe"}:
            raise ValueError("unknown task kind")
        if task_kind == "problem_reframe" and parent["role"] != "coordinator":
            raise ValueError("only the coordinator may request an independent problem reframe")
        research_node_id = args.get("research_node_id") or parent["scope"]["research_node_id"]
        research_revision = args.get("research_revision")
        if research_node_id:
            if (research_revision is None and
                    research_node_id == parent["scope"].get("research_node_id")):
                research_revision = parent["scope"].get("research_revision")
            if research_revision is None:
                raise ValueError("delegation to a different research node requires its exact revision")
            node = store.research_node(parent["mission_id"], research_node_id, research_revision)
            if not node:
                raise ValueError("research node revision is outside this Mission")
            research_revision = node["revision"]
            if parent["role"] != "coordinator" and research_node_id != parent["scope"]["research_node_id"]:
                snapshot = store.research_snapshot(parent["mission_id"])
                descendants = {parent["scope"]["research_node_id"]}
                changed = True
                while changed:
                    changed = False
                    for relation in snapshot["relations"]:
                        child = relation["target"]["node_id"]
                        if (relation["kind"] == "decomposes" and relation["source"]["node_id"] in descendants
                                and child not in descendants):
                            descendants.add(child)
                            changed = True
                if research_node_id not in descendants:
                    raise ValueError("delegation research node is outside the assigned research branch")
        elif research_revision is not None:
            raise ValueError("research_revision requires research_node_id")
        if role == "curator" and parent["role"] != "coordinator":
            raise ValueError("only the coordinator dispatches the cross-branch curator")
        idea_id = args.get("idea_id") or parent["scope"]["idea_id"]
        if parent["role"] != "coordinator" and not idea_id and not research_node_id:
            raise ValueError("delegation needs an Idea ID")
        if parent["scope"]["idea_id"] and idea_id != parent["scope"]["idea_id"]:
            raise ValueError("delegation must stay in the assigned Idea")
        candidate_id = args.get("candidate_id") or None
        candidate = store.candidate(candidate_id) if candidate_id else None
        mission = store.mission(parent["mission_id"])
        if candidate_id and (not candidate or candidate["mission_id"] != parent["mission_id"]):
            raise ValueError("Candidate is outside this Mission")
        if candidate and parent["role"] != "coordinator" and candidate_id != parent.get("candidate_id") and (
                candidate.get("idea_ref") != idea_id):
            raise ValueError("Candidate is outside this Idea")
        if task_kind == "problem_reframe" and (role != "researcher" or idea_id or candidate_id or
                args.get("reply_to_task_id")):
            raise ValueError("problem reframe needs an unanchored researcher task")
        if task_kind == "problem_reframe" and research_node_id != research_root_id(parent["mission_id"]):
            raise ValueError("problem reframe must address the Mission root question")
        if task_kind == "problem_reframe" and any(
                item["status"] in {"queued", "running"} and item["scope"].get("task_kind") == "problem_reframe"
                for item in store.list_tasks(parent["mission_id"])):
            raise ValueError("an independent problem reframe is already in progress")
        if role in {"coder", "runner", "analyzer", "critic"} and not (candidate_id or idea_id):
            raise ValueError(f"{role} needs a Candidate or Idea ID")
        if role == "coder" and mission["payload"]["authorization"].get("allow_code_candidates") is not True:
            raise ValueError("Mission does not authorize coding")
        experiment = args.get("experiment")
        research_mandate = normalize_research_mandate(args.get("research_mandate"))
        if research_mandate is not None and role != "researcher":
            raise ValueError("research_mandate is only valid for researcher tasks")
        exploration_validation = None
        review_run_id = args.get("review_run_id") or args.get("run_id")
        if review_run_id is not None and role not in {"analyzer", "critic"}:
            raise ValueError("review_run_id is only valid for analyzer or critic tasks")
        if role == "runner":
            if candidate:
                self.coordinator._validate_run(mission, candidate)
            else:
                exploration_validation = self.coordinator._validate_exploration(
                    store, mission, {"idea_id": idea_id, "experiment": experiment,
                                     "research_node_id": research_node_id})
        elif experiment is not None:
            raise ValueError("experiment belongs to a Runner task")
        child = store.create_task(parent["mission_id"], role, _text(args.get("objective"), "objective"),
                                  f"Requested by {parent['role']} task {parent['task_id']}",
                                  candidate_id=candidate_id, parent_task_id=parent["task_id"],
                                  idea_id=idea_id, reply_to_task_id=args.get("reply_to_task_id") or None,
                                  experiment=experiment, research_node_id=research_node_id,
                                  research_revision=research_revision, review_run_id=review_run_id,
                                  task_kind=task_kind, research_mandate=research_mandate)
        if exploration_validation and exploration_validation["duplicates"]:
            store.event(parent["mission_id"], "exploration_duplicate_recheck", {
                "source_task_id": experiment.get("source_task_id"),
                "duplicate_refs": exploration_validation["duplicates"],
                "recheck_reason": experiment.get("recheck_reason") or experiment.get("repeat_reason"),
                "requested_by_task_id": parent["task_id"], "task_id": child["task_id"]})
        return {"task_id": child["task_id"], "role": role, "status": "queued",
                "idea_id": idea_id, "research_node_id": research_node_id,
                "research_revision": research_revision, "delegation_limit": None,
                "task_kind": task_kind,
                "research_mandate": research_mandate,
                "review_run_id": review_run_id,
                "remaining_agent_tasks": store.remaining_agent_tasks(parent["mission_id"]),
                "remaining_runs": store.remaining_runs(parent["mission_id"])}

    def _research_checkpoint(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if parent["role"] != "researcher":
            raise ValueError("research checkpoints are available to researchers")
        checkpoint = normalize_checkpoint(args)
        continuation = normalize_continuation(args.get("continuation"))
        child = None
        if continuation is not None:
            after_ids = [safe_id(item, "after_task_id") for item in continuation.get("after_task_ids", [])]
            continuation = {**continuation, "after_task_ids": after_ids}
            stopped = [event for event in store.events(parent["mission_id"])
                       if event["kind"] == "branch_intervention"
                       and (event["payload"].get("target_task_id") == parent["task_id"]
                            or parent["task_id"] in event["payload"].get("stop_requested_running", []))]
            if stopped:
                raise ValueError("stop was requested for this research branch; checkpoint continuation is blocked")
            for dependency_id in after_ids:
                dependency = store.task(dependency_id)
                if dependency is None or dependency["mission_id"] != parent["mission_id"]:
                    raise ValueError("continuation dependency belongs to another Mission")
                if dependency["scope"].get("idea_id") != parent["scope"].get("idea_id"):
                    raise ValueError("continuation dependency must stay in the assigned Idea")
                if parent["scope"].get("research_node_id") and dependency["scope"].get("research_node_id") != parent["scope"].get("research_node_id"):
                    raise ValueError("continuation dependency must stay on the assigned research node")
                ancestor = parent["scope"].get("parent_task_id")
                while ancestor:
                    if ancestor == dependency_id:
                        raise ValueError("continuation dependency cannot be a source ancestor")
                    linked = store.task(ancestor)
                    ancestor = linked["scope"].get("parent_task_id") if linked else None
            existing = [item for item in store.list_tasks(parent["mission_id"])
                        if (item["scope"].get("continuation") or {}).get("source_task_id") == parent["task_id"]]
            if existing:
                raise ValueError("a continuation has already been requested for this task")
        payload = dict(checkpoint)
        with store.atomic():
            if continuation is not None:
                if store.remaining_agent_tasks(parent["mission_id"]) == 0:
                    raise ValueError("continuation exceeds Mission agent task budget")
                child = store.create_task(parent["mission_id"], "researcher", continuation["objective"],
                    f"Explicit continuation requested by researcher task {parent['task_id']}",
                    parent_task_id=parent["task_id"], idea_id=parent["scope"].get("idea_id"),
                    candidate_id=parent.get("candidate_id"),
                    research_node_id=parent["scope"].get("research_node_id"),
                    research_revision=parent["scope"].get("research_revision"), continuation=continuation)
                payload["continuation"] = {**continuation, "task_id": child["task_id"], "status": "queued"}
            recorded = store.record_research_checkpoint(parent["mission_id"], parent["task_id"], payload)
        return {"checkpoint": recorded, "task_id": child["task_id"] if child else None,
                "task_status": child["status"] if child else None}

    def _research(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action in {"get", "snapshot"}:
            at_version = args.get("at_version")
            result = {"research": store.research_snapshot(parent["mission_id"], at_version=at_version)}
            # Historical reads must not leak later task projections into their snapshot.
            if parent["role"] == "coordinator" and at_version is None:
                from .runtime_portfolio import build_portfolio_context
                result["exploration_portfolio"] = build_portfolio_context(store, parent["mission_id"])
            return result
        if action not in {"create", "revise", "relate", "compose",
                          "revoke_composition", "rollback", "revoke"}:
            raise ValueError("research action must be create, revise, relate, compose, revoke_composition, rollback, revoke, or snapshot")
        data = args.get("data")
        if not isinstance(data, dict):
            raise ValueError("research data must be an object")
        if action == "compose":
            source_ids = []
            source_run_ids = []
            inputs = data.get("inputs")
            if not isinstance(inputs, list) or len(inputs) < 2 or any(not isinstance(ref, dict) for ref in inputs):
                raise ValueError("composition requires at least two exact input references")
            for ref in inputs:
                node = store.research_node(parent["mission_id"], ref.get("node_id"), ref.get("revision"))
                if not node or node["revision"] != ref.get("revision"):
                    raise ValueError("composition input must be an exact same-Mission node revision")
                source_run_ids.extend(node.get("run_refs", []))
                for earlier in store.research_snapshot(parent["mission_id"])["compositions"]:
                    if earlier["node_id"] == node["node_id"]:
                        source_ids.extend(task_id for task_id in earlier["plan"].get("frozen_source_task_ids", [])
                                          if (source := store.task(task_id)) and source["status"] == "completed")
                        source_run_ids.extend(earlier["plan"].get("frozen_source_run_ids", []))
                author_events = [event for event in store.events(parent["mission_id"])
                                 if event["version"] == node["event_version"] and
                                 event["kind"] in {"research_create", "research_revise"}]
                for event in author_events:
                    author_id = event["payload"].get("actor_task_id")
                    author = (store.task(author_id) if isinstance(author_id, str) and author_id
                              and author_id != "system:mission_start" else None)
                    if author and author["status"] == "completed":
                        source_ids.append(author_id)
                source_ids.extend(item["task_id"] for item in store.list_tasks(parent["mission_id"])
                                  if item["status"] == "completed" and
                                  item["scope"].get("research_node_id") == node["node_id"] and
                                  item["scope"].get("research_revision") == node["revision"])
            plan = data.get("plan", {})
            if not isinstance(plan, dict):
                raise ValueError("composition plan must be an object")
            source_ids = list(dict.fromkeys(source_ids))
            source_run_ids.extend(run["run_id"] for run in store.list_runs(parent["mission_id"])
                                  if run["task_id"] in source_ids)
            data = {**data, "plan": {**plan, "frozen_source_task_ids": source_ids,
                                     "frozen_source_run_ids": list(dict.fromkeys(source_run_ids))}}
        result = store.apply_research_operation(parent["mission_id"], action, data,
                                                actor_task_id=parent["task_id"])
        if action == "compose":
            node = result["node"]
            if store.remaining_agent_tasks(parent["mission_id"]) == 0:
                raise ValueError("composition needs budget for a follow-up researcher")
            task = store.create_task(parent["mission_id"], "researcher", node["question"],
                                     f"Investigate composition {node['node_id']} from fixed input revisions",
                                     idea_id=parent["scope"]["idea_id"] or node["node_id"],
                                     parent_task_id=parent["task_id"],
                                     research_node_id=node["node_id"], research_revision=node["revision"])
            result = {**result, "task_id": task["task_id"], "task_status": task["status"]}
        return result
    def _branch_idea(self, parent: dict[str, Any], args: Mapping[str, Any]) -> str | None:
        """Resolve and constrain the Idea branch for a live research mutation."""
        idea_id = args.get("idea_id") or args.get("idea_ref") or parent["scope"]["idea_id"]
        if parent["role"] != "coordinator" and not idea_id:
            raise ValueError("operation needs an Idea ID")
        if parent["scope"]["idea_id"] and idea_id != parent["scope"]["idea_id"]:
            raise ValueError("operation must stay in the assigned Idea")
        if idea_id is not None:
            safe_id(idea_id, "idea_id")
        return idea_id

    def _register_hypothesis(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if parent["role"] not in {"researcher", "hypothesis", "coordinator"}:
            raise ValueError("only research roles can register a Hypothesis")
        idea_id = self._branch_idea(parent, args)
        data = dict(args)
        data.pop("idea_id", None)
        data.pop("idea_ref", None)
        if idea_id is not None:
            data["idea_ref"] = idea_id
        hypothesis_id = safe_id(data.get("hypothesis_id") or data.get("id"), "hypothesis_id")
        previous = store.hypothesis(hypothesis_id)
        if previous and parent["role"] != "coordinator" and previous.get("idea_ref") != idea_id:
            raise ValueError("hypothesis belongs to another research branch")
        result = store.create_hypothesis(parent["mission_id"], data)
        return {"status": "registered", "hypothesis_id": result["hypothesis_id"],
                "version": result["version"], "idea_id": idea_id, "hypothesis": result}

    def _register_candidate_now(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if parent["role"] not in {"researcher", "hypothesis", "coordinator"}:
            raise ValueError("only research roles can register a Candidate")
        idea_id = self._branch_idea(parent, args)
        data = dict(args)
        data.pop("idea_id", None)
        data.pop("idea_ref", None)
        if idea_id is not None:
            data["idea_ref"] = idea_id
        if data.get("kind", "hypothesis") != "baseline":
            hypothesis_id = safe_id(data.get("hypothesis_id"), "hypothesis_id")
            version = data.get("hypothesis_version")
            if type(version) is not int or version < 1:
                raise ValueError("Candidate registration needs a positive hypothesis_version")
            linked = store.hypothesis(hypothesis_id, version)
            if linked is None or linked.get("mission_id") != parent["mission_id"]:
                raise ValueError("Candidate hypothesis revision is outside this Mission")
            if linked.get("idea_ref") != idea_id:
                raise ValueError("Candidate hypothesis belongs to another research branch")
            if linked.get("status") != "candidate_ready":
                raise ValueError("Candidate requires a candidate_ready hypothesis revision")
            data.setdefault("hypothesis", linked["statement"])
            data.setdefault("expected_observation", "; ".join(linked["predictions"]))
        mission = store.mission(parent["mission_id"])
        registered = self.coordinator._register_candidate(store, mission, data, research_task=parent)
        return {"status": "registered", "candidate_id": registered["candidate_id"],
                "idea_id": idea_id, "candidate": registered,
                "next_action": "delegate rsi_delegate with role=coder and this candidate_id, then wait for its result"}

    def _stop_branch(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if parent["role"] != "coordinator":
            raise ValueError("only the coordinator can stop a research branch")
        target_id = safe_id(args.get("task_id"), "task_id")
        reason = _text(args.get("reason"), "reason")
        target = self._check_child(store, parent, target_id)
        descendants = []
        for item in store.list_tasks(parent["mission_id"]):
            if item["task_id"] == target_id:
                descendants.append(item)
                continue
            ancestor = item["scope"]["parent_task_id"]
            while ancestor:
                if ancestor == target_id:
                    descendants.append(item)
                    break
                linked = store.task(ancestor)
                ancestor = linked["scope"]["parent_task_id"] if linked else None
        cancelled, requested = [], []
        now = _now()
        for item in descendants:
            if item["status"] == "queued":
                store.db.execute("UPDATE agent_tasks SET status='cancelled',output_json=?,error=?,updated_at=? WHERE task_id=? AND status='queued'",
                                 (_json({"error": "branch stopped by coordinator", "reason": reason}), reason, now, item["task_id"]))
                cancelled.append(item["task_id"])
            elif item["status"] == "running":
                publish_direct_messages(store, parent, {"messages": [{
                    "text": f"Coordinator stop request for this branch: {reason}. Finish only a safe checkpoint, then stop.",
                    "idea_id": item["scope"]["idea_id"], "to_task_id": item["task_id"]}]})
                requested.append(item["task_id"])
        store.event(parent["mission_id"], "branch_intervention", {
            "task_id": parent["task_id"], "target_task_id": target["task_id"], "reason": reason,
            "cancelled_queued": cancelled, "stop_requested_running": requested})
        return {"status": "stop_requested", "target_task_id": target["task_id"],
                "cancelled_queued": cancelled, "stop_requested_running": requested,
                "note": "Running Agents receive a durable stop request and are not killed mid-write."}

    def _renew_ancestors(self, store: RuntimeStore, task: dict[str, Any]) -> None:
        if task["task_id"] != self.task_id or task["lease_token"] != self.lease_token:
            raise ValueError("tool owner lease changed")
        until = (datetime.now(timezone.utc) + timedelta(seconds=180)).replace(microsecond=0).isoformat()
        current = task
        while current and current["status"] == "running":
            store.db.execute("UPDATE agent_tasks SET lease_until=max(lease_until,?),updated_at=? WHERE task_id=? AND lease_token=?",
                             (until, _now(), current["task_id"], current["lease_token"]))
            parent_id = current["scope"]["parent_task_id"]
            current = store.task(parent_id) if parent_id else None
        store._commit()

    def _validate_wait(self, store: RuntimeStore, parent: dict[str, Any], target_id: str) -> None:
        target = self._check_child(store, parent, target_id)
        if (target["scope"].get("continuation") or {}).get("source_task_id") == parent["task_id"]:
            raise ValueError("cannot wait for an explicit continuation from the current task; finish this task first")
        ancestor = parent
        candidate_roles = {"coder", "runner", "analyzer", "critic"}
        while ancestor:
            if ancestor["task_id"] == target_id:
                raise ValueError("cannot wait for yourself or an ancestor")
            if target["status"] in {"queued", "running"} and ancestor["role"] in candidate_roles and ancestor.get("candidate_id") and (
                    target["candidate_id"] == ancestor["candidate_id"] and target["role"] in candidate_roles):
                raise ValueError("finish the owning Candidate task before waiting for another task that updates its state")
            parent_id = ancestor["scope"]["parent_task_id"]
            ancestor = store.task(parent_id) if parent_id else None

    def _wait(self, store: RuntimeStore, parent: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        target_id = safe_id(args.get("task_id"), "task_id")
        self._validate_wait(store, parent, target_id)
        after_message = args.get("after_message_version", parent["input_version"])
        self._messages(store, parent, after_message, 1)  # Validate before starting a worker.
        self._renew_ancestors(store, parent)
        self.wait_started = time.monotonic()
        stop_heartbeat = Event()

        def heartbeat():
            while not stop_heartbeat.wait(30):
                try:
                    with RuntimeStore(self.coordinator.store_path) as fresh:
                        current = fresh.task(parent["task_id"])
                        if not current or current["status"] != "running" or current["lease_token"] != self.lease_token:
                            return
                        self._renew_ancestors(fresh, current)
                except Exception:
                    continue

        heart = Thread(target=heartbeat, daemon=True)
        heart.start()
        try:
            while True:
                target = self._check_child(store, parent, target_id)
                expired = target["status"] == "running" and target["lease_until"] <= _now()
                messages = self._messages(store, parent, after_message, 12)
                if target["status"] in {"completed", "failed", "cancelled"} or expired or messages:
                    with store.atomic():
                        self._current(store)
                        from .runtime_parallel import parallel_limits
                        agents, _ = parallel_limits(self.coordinator, store.mission(parent["mission_id"]))
                        active = store.db.execute("""SELECT count(*) FROM agent_tasks t
                            WHERE t.mission_id=? AND t.role NOT IN ('coordinator','runner') AND t.status='running'
                            AND t.task_id!=? AND NOT EXISTS(SELECT 1 FROM rsi_tool_operations o
                                WHERE o.task_id=t.task_id AND o.tool='rsi_wait' AND o.status='pending')""",
                            (parent["mission_id"], parent["task_id"])).fetchone()[0]
                        if parent["role"] == "coordinator" or active < agents:
                            store.db.execute("""UPDATE rsi_tool_operations SET status='resuming'
                                WHERE task_id=? AND tool='rsi_wait' AND status='pending'""", (parent["task_id"],))
                            result = self._result(store, target)
                            if messages:
                                return {**result, "wait_reason": "message_available",
                                        "inbox": self._inbox(store, parent, {"after_version": after_message})}
                            return {**result, "note": "expired task awaits durable recovery"} if expired else result
                    time.sleep(0.1)
                    continue
                self._current(store)
                if self.worker_error:
                    error, self.worker_error = self.worker_error, None
                    raise error
                if self.worker is None or not self.worker.is_alive():
                    def advance():
                        try:
                            from .runtime_parallel import execute_queued
                            from .pi_agent import run_agent
                            execute_queued(self.coordinator, parent["mission_id"],
                                           agent_runner=run_agent, collaborators_only=True)
                        except Exception as exc:
                            self.worker_error = exc
                    # Keep children alive after a message wakes their parent. Each worker
                    # owns its DB connection; leases still enforce execution limits.
                    self.worker = Thread(target=advance, daemon=False)
                    self.worker.start()
                time.sleep(0.1)
                self._renew_ancestors(store, store.task(parent["task_id"]))
        finally:
            stop_heartbeat.set()
            heart.join(timeout=2)
            self.wait_total += time.monotonic() - self.wait_started
            self.wait_started = None

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        tool = request.get("tool")
        args = request.get("args")
        if tool not in {"rsi_delegate", "rsi_run_probe", "rsi_register_hypothesis", "rsi_register_candidate",
                        "rsi_branch_control",
                        "rsi_message", "rsi_inbox", "rsi_status", "rsi_wait", "rsi_read_handoff",
                        "rsi_wiki_check", "rsi_research", "rsi_research_checkpoint", "rsi_audit_bash"} or not isinstance(args, dict):
            return {"ok": False, "error": {"code": "invalid_request", "message": "invalid RSI tool request"}}
        try:
            if tool == "rsi_audit_bash":
                return self._audit_bash(request)
            raw_call_id = request.get("call_id")
            if not isinstance(raw_call_id, str) or not 1 <= len(raw_call_id) <= 256:
                raise ValueError("tool call ID must be a nonempty bounded string")
            call_id = hashlib.sha256(raw_call_id.encode()).hexdigest()
            operation_args, normalizations = _repair_args(tool, args)
            # The extension's live cursor is transport metadata, not the model's
            # wait request. Replaying the same call must return its frozen receipt.
            digest_args = {key: value for key, value in operation_args.items()
                           if not (tool == "rsi_wait" and key == "after_message_version")}
            digest = hashlib.sha256(_json({"tool": tool, "args": digest_args}).encode()).hexdigest()
            with RuntimeStore(self.coordinator.store_path) as store:
                with store.atomic():
                    parent = self._current(store)
                    previous = store.tool_operation(self.task_id, call_id)
                    if previous:
                        if previous["request_sha256"] != digest or previous["tool"] != tool:
                            raise ValueError("tool call ID was reused with different arguments")
                        if previous["status"] == "completed":
                            return json.loads(previous["response_json"])
                    if tool in {"rsi_delegate", "rsi_run_probe"}:
                        spec = dict(operation_args)
                        if tool == "rsi_run_probe":
                            spec["role"] = "runner"
                        response = {"ok": True, **self._delegate(store, parent, spec)}
                    elif tool == "rsi_register_hypothesis":
                        response = {"ok": True, **self._register_hypothesis(store, parent, operation_args)}
                    elif tool == "rsi_register_candidate":
                        response = {"ok": True, **self._register_candidate_now(store, parent, operation_args)}
                    elif tool == "rsi_branch_control":
                        if operation_args.get("action", "stop") != "stop":
                            raise ValueError("branch control currently supports action=stop")
                        response = {"ok": True, **self._stop_branch(store, parent, operation_args)}
                    elif tool == "rsi_message":
                        publish_direct_messages(store, parent, {"messages": [operation_args]})
                        response = {"ok": True, "status": "recorded", "to_task_id": operation_args.get("to_task_id"),
                                    "to_role": operation_args.get("to_role")}
                    elif tool == "rsi_inbox":
                        response = {"ok": True, **self._inbox(store, parent, operation_args)}
                    elif tool == "rsi_status":
                        target = self._check_child(store, parent, safe_id(operation_args.get("task_id"), "task_id"))
                        response = {"ok": True, **self._result(store, target, operation_args.get("checkpoint_id"))}
                    elif tool == "rsi_read_handoff":
                        response = {"ok": True, **self._read_handoff(store, parent, operation_args)}
                    elif tool == "rsi_research":
                        response = {"ok": True, **self._research(store, parent, operation_args)}
                    elif tool == "rsi_research_checkpoint":
                        response = {"ok": True, **self._research_checkpoint(store, parent, operation_args)}
                    elif tool == "rsi_wiki_check":
                        if parent["role"] != "curator" or operation_args:
                            raise ValueError("Wiki preflight is available to curators without arguments")
                        response = {"ok": True, **preflight_curator_pages(self.coordinator.root, store, parent)}
                    else:
                        self._validate_wait(store, parent, safe_id(operation_args.get("task_id"), "task_id"))
                        store.put_tool_operation(self.task_id, call_id, tool, digest, "pending")
                        response = None
                    if response is not None:
                        if normalizations:
                            response["normalizations"] = normalizations
                        store.put_tool_operation(self.task_id, call_id, tool, digest, "completed", response)
                        return response
                response = {"ok": True, **self._wait(store, parent, operation_args)}
                if normalizations:
                    response["normalizations"] = normalizations
                with store.atomic():
                    store.put_tool_operation(self.task_id, call_id, tool, digest, "completed", response)
                return response
        except Exception as exc:
            if tool == "rsi_research" and "reason conflicts with data.reason" in str(exc):
                repair = ("Remove top-level reason or make it identical to data.reason and retry; "
                          "a rejected call did not mutate research state.")
            elif tool == "rsi_research" and "data.reason" in str(exc):
                repair = ("Put a nonempty reason inside data.reason and retry; "
                          "a rejected call did not mutate research state.")
            else:
                repair = ("Correct only the fields named in this error and retry; "
                          "a rejected call did not mutate research state.")
            response = {"ok": False, "error": {"code": "rejected", "message": str(exc), "retryable": True,
                                                 "repair": repair}}
            if "call_id" in locals() and "digest" in locals():
                with RuntimeStore(self.coordinator.store_path) as fresh:
                    with fresh.atomic():
                        owner = fresh.task(self.task_id)
                        operation = fresh.tool_operation(self.task_id, call_id)
                        if owner and owner["lease_token"] == self.lease_token:
                            if operation is None:
                                fresh.put_tool_operation(self.task_id, call_id, tool, digest, "failed", response)
                            elif operation["status"] == "pending":
                                fresh.put_tool_operation(self.task_id, call_id, tool, digest, "failed", response)
            return response


@contextmanager
def serve_tools(root: Path, task_id: str):
    bridge = ToolBridge(root, task_id)
    directory = bridge.coordinator.runtime_root / "sockets"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(task_id.encode()).hexdigest()[:20] + ".sock")
    if path.exists():
        path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline(256_000)
            if not line:
                return
            try:
                request = json.loads(line)
            except (ValueError, TypeError) as exc:
                self.wfile.write((_json({"ok": False, "error": {
                    "code": "invalid_request", "message": str(exc)}}) + "\n").encode())
                return
            if not isinstance(request, dict):
                self.wfile.write((_json({"ok": False, "error": {
                    "code": "invalid_request", "message": "invalid RSI tool request"}}) + "\n").encode())
                return
            supports_heartbeats = request.get("bridge_protocol") == 2
            finished = Event()
            write_lock = Lock()

            def send(frame: dict[str, Any]) -> bool:
                with write_lock:
                    try:
                        self.wfile.write((_json(frame) + "\n").encode())
                        self.wfile.flush()
                        return True
                    except OSError:
                        return False

            def keep_alive():
                while not finished.wait(BRIDGE_HEARTBEAT_SECONDS):
                    if not send({"type": "heartbeat"}):
                        return

            heart = Thread(target=keep_alive, daemon=True) if supports_heartbeats else None
            if heart:
                heart.start()
            try:
                try:
                    with bridge.mutex:
                        response = bridge.call(request)
                except (ValueError, TypeError) as exc:
                    response = {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}
            finally:
                finished.set()
                if heart:
                    heart.join()
            send(response)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stop_heartbeat = Event()

    def heartbeat():
        while not stop_heartbeat.wait(30):
            try:
                with RuntimeStore(bridge.coordinator.store_path) as fresh:
                    owner = fresh.task(task_id)
                    if not owner or owner["status"] != "running" or owner["lease_token"] != bridge.lease_token:
                        return
                    bridge._renew_ancestors(fresh, owner)
            except Exception:
                continue

    heart = Thread(target=heartbeat, daemon=True)
    heart.start()
    try:
        yield path, bridge
    finally:
        stop_heartbeat.set()
        heart.join(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        path.unlink(missing_ok=True)
