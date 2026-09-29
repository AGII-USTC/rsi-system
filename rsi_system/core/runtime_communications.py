"""Task-owned writing and immutable, attributed handoffs between RSI Agents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .pi_agent import atomic_json
from .runtime_types import ROLES, _now, _text
from .schema import safe_id

MAX_FILE_BYTES = 1_000_000
MAX_OUTBOX_BYTES = 8_000_000
MAX_INBOX_BYTES = 32_000_000
MAX_FILES = 256


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def workspace_path(item):
    if item.get("origin") != "agent_workspace":
        return None
    relative = item.get("workspace_path")
    if relative is None:
        # Version-one outboxes placed authored files below notes/.
        parts = Path(item["path"]).parts
        if len(parts) < 2 or parts[0] != "notes":
            raise ValueError("invalid legacy handoff path")
        relative = Path(*parts[1:]).as_posix()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or (
            any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("invalid handoff workspace path")
    return relative


def handoff_file_ref(author_task_id, item):
    relative = workspace_path(item)
    if relative is None:
        raise ValueError("handoff file must come from an Agent workspace")
    identity = "\0".join((author_task_id, item["path"], item["sha256"]))
    return {"file_id": digest(identity.encode()), "path": item["path"],
            "workspace_path": relative, "sha256": item["sha256"],
            "bytes": item["bytes"]}


def resolve_handoff_file(manifest, author_task_id, *, file_id=None, path=None, suffix=None):
    if bool(file_id) == bool(path):
        raise ValueError("provide exactly one handoff file ID or path")
    found, available = [], []
    for item in manifest["files"]:
        if item.get("origin") != "agent_workspace":
            continue
        ref = handoff_file_ref(author_task_id, item)
        if suffix and not ref["workspace_path"].endswith(suffix):
            continue
        available.append(ref)
        if file_id == ref["file_id"] or (path and path in {ref["path"], ref["workspace_path"]}):
            found.append((item, ref))
    if len(found) != 1:
        choices = [{"file_id": ref["file_id"], "path": ref["path"]} for ref in available[:12]]
        reason = "ambiguous" if found else "absent"
        raise ValueError(f"handoff file is {reason}; use a file_id from the completed task: {choices}")
    return found[0]


def checked_file(directory, relative, expected=None):
    directory = Path(directory).resolve()
    path = directory / relative
    if (path.is_symlink() or not path.resolve().is_relative_to(directory)
            or any(p.is_symlink() for p in path.parents if p.is_relative_to(directory))
            or not path.is_file()):
        raise ValueError("communication file is outside its snapshot")
    raw = path.read_bytes()
    if expected is not None and digest(raw) != expected:
        raise ValueError("communication snapshot hash mismatch")
    return raw


def load_manifest(path, task):
    data = json.loads(path.read_text())
    if data["task_id"] != task["task_id"] or data["mission_id"] != task["mission_id"]:
        raise ValueError("communication manifest identity mismatch")
    for item in data["files"]:
        workspace_path(item)
        checked_file(path.parent, item["path"], item["sha256"])
    return data


def prepare_communications(store, task, output_dir):
    """Freeze the published same-Mission inbox once, alongside the task input."""
    output_dir = Path(output_dir).resolve()
    workspace = output_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "communications" / "inbox" / "manifest.json"
    if manifest_path.exists():
        load_manifest(manifest_path, task)
        return manifest_path
    files, omitted, total = [], [], 0
    for event in reversed(store.events(task["mission_id"])):
        if event["version"] > task["input_version"]:
            continue
        if event["kind"] == "direct_message":
            message = event["payload"]
            if (task["scope"].get("task_kind") == "problem_reframe"
                    and message.get("from_role") == "coordinator"):
                continue
            same_idea = task["scope"]["idea_id"] == message["idea_id"]
            exact = message.get("to_task_id") is not None and message["to_task_id"] in {
                task["task_id"], task["scope"]["reply_to_task_id"]}
            addressed = message.get("to_role") == task["role"] and (same_idea or task["role"] == "coordinator")
            if not (exact or addressed):
                continue
            raw = json.dumps(message, ensure_ascii=False, sort_keys=True).encode()
            if total + len(raw) > MAX_INBOX_BYTES or len(files) >= MAX_FILES:
                omitted.append({"event_id": event["event_id"], "reason": "inbox size limit"})
                continue
            target = manifest_path.parent / "direct" / f"{event['event_id']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            total += len(raw)
            files.append({"path": target.relative_to(manifest_path.parent).as_posix(),
                          "sha256": digest(raw), "bytes": len(raw), "origin": "direct_message",
                          "author_task_id": message["from_task_id"], "author_role": message["from_role"],
                          "idea_id": message["idea_id"], "in_reply_to": message.get("in_reply_to")})
            continue
        if event["kind"] != "communication_published":
            continue
        publication = event["payload"]
        author = store.task(publication["task_id"])
        if not author or author["mission_id"] != task["mission_id"]:
            raise ValueError("communication author belongs to another Mission")
        if task["scope"].get("task_kind") == "problem_reframe" and author["role"] == "coordinator":
            continue
        author_idea = author["scope"]["idea_id"]
        if author_idea and task["role"] not in {"coordinator", "curator"} and task["scope"]["idea_id"] != author_idea:
            continue
        source_manifest = output_dir.parent / author["task_id"] / "communications/outbox/manifest.json"
        if digest(source_manifest.read_bytes()) != publication["manifest_sha256"]:
            raise ValueError("published communication manifest hash mismatch")
        source = load_manifest(source_manifest, author)
        for item in source["files"]:
            raw = checked_file(source_manifest.parent, item["path"], item["sha256"])
            if total + len(raw) > MAX_INBOX_BYTES or len(files) >= MAX_FILES:
                omitted.append({"task_id": author["task_id"], "path": item["path"], "reason": "inbox size limit"})
                continue
            target = manifest_path.parent / author["task_id"] / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            total += len(raw)
            files.append({**item, "path": target.relative_to(manifest_path.parent).as_posix(),
                          "author_task_id": author["task_id"], "author_role": author["role"],
                          "workspace_path": workspace_path(item),
                          "file_id": handoff_file_ref(author["task_id"], item)["file_id"] if item.get("origin") == "agent_workspace" else None,
                          "idea_id": author_idea,
                          "candidate_id": author.get("candidate_id"), "task_status": publication["task_status"],
                          "published_version": event["version"], "objective": author["objective"]})
    atomic_json(manifest_path, {"schema_version": 1, "mission_id": task["mission_id"],
                               "task_id": task["task_id"], "input_version": task["input_version"],
                               "workspace": str(workspace), "files": files, "omitted": omitted})
    return manifest_path


def communication_context(manifest_path):
    if manifest_path is None:
        return None
    data = json.loads(manifest_path.read_text())
    return {"workspace": data["workspace"], "inbox_manifest": str(manifest_path),
            "file_count": len(data["files"]), "omitted_count": len(data["omitted"]),
            "available_files": [{"path": str(manifest_path.parent / item["path"]),
                                 "author_task_id": item.get("author_task_id"),
                                 "author_role": item.get("author_role"),
                                 "file_id": item.get("file_id"),
                                 "workspace_path": item.get("workspace_path"),
                                 "sha256": item["sha256"]}
                                for item in data["files"][:20]],
            "input_version": data["input_version"], "evidence_status": "agent-authored, unverified"}


def snapshot_communications(task, output_dir, payload):
    """Freeze authored files and final response; recovery reuses this snapshot."""
    output_dir = Path(output_dir).resolve()
    workspace = output_dir / "workspace"
    manifest_path = output_dir / "communications/outbox/manifest.json"
    if manifest_path.exists():
        load_manifest(manifest_path, task)
        return manifest_path
    files, omitted, total = [], [], 0
    if workspace.exists():
        for source in sorted(workspace.rglob("*")):
            if any(part in {".git", "node-compile-cache"} for part in source.relative_to(workspace).parts):
                continue
            if source.is_dir() and not source.is_symlink():
                continue
            relative = source.relative_to(workspace).as_posix()
            if source.is_symlink() or not source.is_file():
                omitted.append({"path": relative, "reason": "not a regular authored file"})
                continue
            size = source.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_OUTBOX_BYTES or len(files) >= MAX_FILES - 1:
                omitted.append({"path": relative, "reason": "handoff size limit"})
                continue
            raw = checked_file(workspace, relative)
            target = manifest_path.parent / "notes" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            total += len(raw)
            files.append({"path": target.relative_to(manifest_path.parent).as_posix(),
                          "workspace_path": relative, "sha256": digest(raw),
                          "bytes": len(raw), "origin": "agent_workspace"})
    response = manifest_path.parent / "response.json"
    atomic_json(response, payload)
    files.append({"path": "response.json", "sha256": digest(response.read_bytes()),
                  "bytes": response.stat().st_size, "origin": "agent_final_response"})
    atomic_json(manifest_path, {"schema_version": 1, "mission_id": task["mission_id"],
                               "task_id": task["task_id"], "author_role": task["role"],
                               "created_at": _now(), "evidence_status": "agent-authored, unverified",
                               "files": files, "omitted": omitted})
    return manifest_path


def publish_communications(store, task, manifest_path, status):
    """Call inside the task-result transaction, after state validation/handling."""
    data = load_manifest(manifest_path, task)
    store.event(task["mission_id"], "communication_published", {
        "task_id": task["task_id"], "author_role": task["role"], "task_status": status,
        "manifest": str(manifest_path), "manifest_sha256": digest(manifest_path.read_bytes()),
        "files": data["files"], "omitted": data["omitted"],
        "evidence_status": "agent-authored, unverified"})


def publish_direct_messages(store, task, payload):
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or len(messages) > 8:
        raise ValueError("messages must be a list of at most eight items")
    for raw in messages:
        if not isinstance(raw, dict):
            raise ValueError("direct message must be an object")
        idea_id = raw.get("idea_id") or task["scope"]["idea_id"]
        if idea_id is None:
            ideas = {item.get("idea_id") for item in payload.get("ideas", [])
                     if isinstance(item, dict) and item.get("idea_id")}
            if len(ideas) == 1:
                idea_id = next(iter(ideas))
        if idea_id is None:
            raise ValueError("direct message needs an Idea scope")
        safe_id(idea_id, "idea_id")
        if task["scope"]["idea_id"] and idea_id != task["scope"]["idea_id"]:
            raise ValueError("message leaves the assigned Idea")
        to_task_id, to_role = raw.get("to_task_id"), raw.get("to_role")
        if bool(to_task_id) == bool(to_role):
            raise ValueError("message needs exactly one task or role recipient")
        if to_task_id:
            recipient = store.task(to_task_id)
            if not recipient or recipient["mission_id"] != task["mission_id"] or (
                    recipient["scope"]["idea_id"] and recipient["scope"]["idea_id"] != idea_id):
                raise ValueError("message recipient is outside the Idea branch")
        elif to_role not in ROLES:
            raise ValueError("message recipient role is invalid")
        in_reply_to = raw.get("in_reply_to")
        if in_reply_to:
            previous = store.task(in_reply_to)
            if not previous or previous["mission_id"] != task["mission_id"]:
                raise ValueError("reply target belongs to another Mission")
        store.event(task["mission_id"], "direct_message", {
            "from_task_id": task["task_id"], "from_role": task["role"], "idea_id": idea_id,
            "to_task_id": to_task_id, "to_role": to_role, "in_reply_to": in_reply_to,
            "text": _text(raw.get("text"), "message text")[:4000],
            "input_version": task["input_version"]})
