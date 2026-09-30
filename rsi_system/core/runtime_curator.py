"""Archive published research records and publish source-checked Wiki drafts."""
from __future__ import annotations

from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .knowledge import _WIKILINK, _atomic_replace, _frontmatter, _inside, _safe_file, _source_paths
from .runtime_communications import checked_file, load_manifest, workspace_path
from .runtime_types import _json, _text


_FOLDERS = {"concepts": "concept", "findings": "finding", "experiments": "experiment",
            "queries": "query", "raw": "source"}
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,100}\.md\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PAGES = 20


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_bytes(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _checked_target(root: Path, path: Path) -> Path:
    if not _inside(root, path.resolve()) or any(
            item.is_symlink() for item in (path, *path.parents) if _inside(root, item)):
        raise ValueError("Wiki or archive path escaped project or contains a symlink")
    return path


def _published_manifest(root, store, task):
    path = _safe_file(root, f".rsi/runtime/tasks/{task['task_id']}/communications/outbox/manifest.json")
    events = [event for event in store.events(task["mission_id"])
              if event["kind"] == "communication_published" and event["payload"]["task_id"] == task["task_id"]]
    if not events or _digest(path.read_bytes()) != events[-1]["payload"]["manifest_sha256"]:
        raise ValueError("published communication manifest hash mismatch")
    return path, load_manifest(path, task)


def wiki_inventory(root: Path, task: dict) -> str:
    """Expose publisher-computed base hashes to an Agent without shell access."""
    path = root / ".rsi/runtime/tasks" / task["task_id"] / "evidence/wiki/inventory.json"
    _checked_target(root, path)
    if not path.exists():
        pages = []
        for folder in _FOLDERS:
            for page in sorted((root / "knowledge" / folder).rglob("*.md")):
                _safe_file(root, page.relative_to(root).as_posix())
                raw = page.read_bytes()
                pages.append({"path": page.relative_to(root).as_posix(), "sha256": _digest(raw),
                              "status": _frontmatter(raw.decode("utf-8")).get("status", "source")})
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(path, _json({"pages": pages}) + "\n")
    return str(path)


def _record_events(store, mission_id: str, *, include_coordinator: bool = False) -> list[dict]:
    events = []
    roles = {"researcher", "hypothesis", "coder", "analyzer", "critic"}
    if include_coordinator:
        roles.add("coordinator")
    for event in store.events(mission_id):
        if event["kind"] != "communication_published":
            continue
        task = store.task(event["payload"]["task_id"])
        if task and task["role"] in roles:
            events.append(event)
    return events


def pending_record_ids(store, mission_id: str, *, through_version: int | None = None) -> list[int]:
    curated = {source_id for event in store.events(mission_id) if event["kind"] == "knowledge_curated"
               for source_id in event["payload"]["source_event_ids"]}
    return [event["event_id"] for event in _record_events(store, mission_id)
            if event["event_id"] not in curated and
            (through_version is None or event["version"] <= through_version)]


def curator_document_index(root: Path, store, task: dict, *, pending_ids: list[int]) -> str:
    """Index every available record, with document addresses in separate catalogs."""
    root = Path(root).resolve()
    mission_id = task["mission_id"]
    index = root / ".rsi/runtime/tasks" / task["task_id"] / "evidence/wiki/curation_sources.json"
    _checked_target(root, index)
    if index.exists():
        saved = json.loads(index.read_text(encoding="utf-8"))
        if (saved.get("mission_id") != mission_id or
                saved.get("curator_task_id") != task["task_id"] or
                saved.get("input_version") != task["input_version"]):
            raise ValueError("curator document index identity mismatch")
        return str(index)
    events = _record_events(store, mission_id, include_coordinator=True)
    by_id = {event["event_id"]: event for event in events}
    pending = set(pending_ids)
    if not pending.issubset(by_id):
        raise ValueError("curator pending source event is missing")
    index.parent.mkdir(parents=True, exist_ok=True)

    def entry(event: dict, *, planning: bool) -> dict:
        source_task = store.task(event["payload"]["task_id"])
        if source_task is None:
            raise ValueError("curator source task is missing")
        archive_dir = root / "knowledge/raw/runtime-records" / mission_id / source_task["task_id"]
        provenance = archive_dir / "provenance.json"
        _checked_target(root, provenance)
        if not provenance.is_file():
            raise ValueError(f"curator archive provenance is missing: {provenance}")
        record = json.loads(provenance.read_text(encoding="utf-8"))
        if (record.get("mission_id") != mission_id or record.get("task_id") != source_task["task_id"] or
                record.get("source_event_id") != event["event_id"]):
            raise ValueError("curator archive provenance identity mismatch")
        documents = []
        for item in record.get("files", []):
            relative = item.get("path")
            if not isinstance(relative, str) or not _HASH.fullmatch(item.get("sha256", "")):
                raise ValueError("curator archived document address or hash is invalid")
            address = root / relative
            _checked_target(root, address)
            if not address.is_file() or not _inside(archive_dir, address.resolve()):
                raise ValueError("curator archived document is missing or outside its record")
            documents.append({"path": str(address.resolve()),
                              "sha256": item["sha256"], "bytes": address.stat().st_size,
                              "kind": "response" if relative.endswith("/response.json") else "handoff"})
        identity = {"source_event_id": event["event_id"], "event_version": event["version"],
                    "task_id": source_task["task_id"], "role": source_task["role"],
                    "idea_id": source_task["scope"].get("idea_id"),
                    "research_node_id": source_task["scope"].get("research_node_id"),
                    "research_revision": source_task["scope"].get("research_revision"),
                    "planning": planning, "provenance": str(provenance)}
        catalog = index.parent / "curation_records" / f"{event['event_id']}.json"
        _checked_target(root, catalog)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps({"schema_version": 2, "mission_id": mission_id,
                              "curator_task_id": task["task_id"], "input_version": task["input_version"],
                              **identity, "objective": source_task["objective"],
                              "reason": source_task["reason"], "documents": documents},
                             ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        raw = encoded.encode("utf-8")
        if catalog.exists():
            if catalog.read_bytes() != raw:
                raise ValueError("curator record catalog changed")
        else:
            _atomic_replace(catalog, encoded)
        return {**identity, "objective_excerpt": source_task["objective"][:240],
                "documents_manifest": {"path": str(catalog), "sha256": _digest(raw),
                                       "bytes": len(raw), "document_count": len(documents),
                                       "mode": "read-only"}}

    records = [entry(by_id[event_id], planning=False) for event_id in dict.fromkeys(pending_ids)]
    planning_events = [event for event in events
                       if event["event_id"] not in pending and
                       event["version"] <= task["input_version"] and
                       event["payload"]["task_id"] != task["task_id"] and
                       (store.task(event["payload"]["task_id"]) or {}).get("role") == "coordinator"]
    planning = [entry(event, planning=True) for event in planning_events]
    _atomic_replace(index, json.dumps({
        "schema_version": 2,
        "mission_id": mission_id,
        "curator_task_id": task["task_id"],
        "input_version": task["input_version"],
        "project_root": str(root),
        "record_count": len(records),
        "planning_record_count": len(planning),
        "document_count": sum(item["documents_manifest"]["document_count"] for item in records + planning),
        "purpose": "Choose records freely, then read their documents_manifest for full source addresses. "
                   "Objective excerpts are verbatim prefixes; catalogs retain complete objectives. "
                   "All paths are absolute addresses under project_root.",
        "records": records,
        "planning_records": planning,
        "raw_archive": str(root / "knowledge/raw/runtime-records" / mission_id),
    }, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return str(index)


def archive_records(root: Path, store, mission_id: str) -> list[str]:
    lock_path = Path(root) / ".rsi/knowledge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _archive_records(root, store, mission_id)


def _archive_records(root: Path, store, mission_id: str) -> list[str]:
    """Copy immutable published text/JSON into Git-portable raw source paths."""
    root = Path(root).resolve()
    archived = []
    for event in _record_events(store, mission_id, include_coordinator=True):
        task_id = event["payload"]["task_id"]
        task = store.task(task_id)
        manifest_path, manifest = _published_manifest(root, store, task)
        directory = root / "knowledge/raw/runtime-records" / mission_id / task_id
        provenance = []
        for item in manifest["files"]:
            relative = workspace_path(item)
            if item["origin"] != "agent_final_response" and relative is None:
                continue
            raw = checked_file(manifest_path.parent, item["path"], item["sha256"])
            target_name = "response.json" if item["origin"] == "agent_final_response" else f"notes/{relative}"
            target = directory / target_name
            if target.suffix == ".md":
                target = target.with_suffix(".md.txt")
            _checked_target(root, target)
            if not _inside(directory, target.resolve()):
                raise ValueError("archive path escaped task directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _digest(target.read_bytes()) != item["sha256"]:
                    raise ValueError(f"archived record changed: {target}")
            else:
                _atomic_bytes(target, raw)
            provenance.append({"path": target.relative_to(root).as_posix(),
                               "published_path": item["path"], "sha256": item["sha256"]})
        identity = {"mission_id": mission_id, "task_id": task_id, "role": task["role"],
                    "task_status": event["payload"]["task_status"],
                    "source_event_id": event["event_id"], "files": provenance,
                    "omitted": manifest.get("omitted", [])}
        meta = directory / "provenance.json"
        _checked_target(root, meta)
        encoded = _json(identity) + "\n"
        if meta.exists():
            if meta.read_text(encoding="utf-8") != encoded:
                raise ValueError(f"archive provenance changed: {meta}")
        else:
            _atomic_replace(meta, encoded)
        archived.append(meta.relative_to(root).as_posix())
    return archived


def _submission_directory(root: Path, task: dict) -> Path:
    return root / ".rsi/runtime/tasks" / task["task_id"] / "curator-submission"


def _submission_prompt_hash(root: Path, task: dict) -> str:
    frozen = root / ".rsi/runtime/tasks" / task["task_id"] / "input.json"
    _checked_target(root, frozen)
    if not frozen.is_file():
        raise ValueError("curator submission requires its frozen task input")
    saved = json.loads(frozen.read_text(encoding="utf-8"))
    if (not isinstance(saved, dict) or saved.get("task_id") != task["task_id"] or
            saved.get("input_version") != task["input_version"] or not isinstance(saved.get("prompt"), str)):
        raise ValueError("curator submission frozen input identity mismatch")
    return _digest(saved["prompt"].encode("utf-8"))


def _submission_args(root: Path, task: dict, args: dict) -> dict:
    if not isinstance(args, dict) or set(args) - {"paths", "summary", "decision"}:
        raise ValueError("curator submission accepts only paths, summary, and decision")
    paths = args.get("paths")
    if not isinstance(paths, list) or len(paths) > _MAX_PAGES:
        raise ValueError("curator submission paths must be a list of at most 20 files")
    decision = args.get("decision", "publish")
    if decision not in {"publish", "no_change"}:
        raise ValueError("curator submission decision must be publish or no_change")
    summary = _text(args.get("summary"), "curator submission summary")
    if len(summary.encode("utf-8")) > 16000:
        raise ValueError("curator submission summary exceeds size limit")
    if (decision == "publish" and not paths) or (decision == "no_change" and paths):
        raise ValueError("publish needs selected paths; no_change requires empty paths and an explanation")
    workspace = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace"
    normalized = []
    for value in paths:
        if not isinstance(value, str) or not value or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("invalid curator submission path")
        path = Path(value)
        try:
            relative = path.relative_to(workspace) if path.is_absolute() else path
        except ValueError as exc:
            raise ValueError("curator submission path must be inside its own workspace") from exc
        parts = relative.parts
        if (len(parts) != 3 or parts[0] != "wiki" or parts[1] not in _FOLDERS or
                not _SLUG.fullmatch(parts[2])):
            raise ValueError("curator submission path must be wiki/<section>/<slug>.md")
        selected = relative.as_posix()
        if selected in normalized:
            raise ValueError("duplicate curator submission path")
        normalized.append(selected)
    return {"paths": normalized, "summary": summary, "decision": decision}


def _validate_submission_plan(root: Path, task: dict, intent: dict, manifest: dict, raw: bytes) -> None:
    if not isinstance(intent, dict) or intent.get("manifest_sha256") != _digest(raw):
        raise ValueError("curator submission manifest hash mismatch")
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or
            manifest.get("task_id") != task["task_id"] or manifest.get("mission_id") != task["mission_id"] or
            manifest.get("input_version") != task["input_version"]):
        raise ValueError("curator submission identity or input version mismatch")
    if manifest.get("prompt_sha256") != _submission_prompt_hash(root, task):
        raise ValueError("curator submission frozen prompt hash mismatch")
    if task["role"] != "curator":
        raise ValueError("only curator tasks may load a submission")
    submission_id = manifest.get("submission_id")
    body = {key: value for key, value in manifest.items() if key != "submission_id"}
    if submission_id != _digest(_json(body).encode("utf-8")) or intent.get("manifest") != manifest:
        raise ValueError("curator submission identity hash mismatch")
    payload = manifest.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"summary", "decision"}:
        raise ValueError("curator submission payload is invalid")
    normalized = _submission_args(root, task, {"paths": manifest.get("paths"), **payload})
    request_hash = _digest(_json(normalized).encode("utf-8"))
    if manifest.get("request_sha256") != request_hash or intent.get("request_sha256") != request_hash:
        raise ValueError("curator submission request hash mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(normalized["paths"]):
        raise ValueError("curator submission files are invalid")
    for item, relative in zip(files, normalized["paths"]):
        if (not isinstance(item, dict) or item.get("path") != relative or
                item.get("snapshot_path") != f"snapshots/{relative}" or
                not isinstance(item.get("sha256"), str) or not _HASH.fullmatch(item["sha256"])):
            raise ValueError("curator submission snapshot address or hash is invalid")
        if type(item.get("bytes")) is not int or not 0 <= item["bytes"] <= 256_000:
            raise ValueError("curator submission snapshot size is invalid")
    source_ids = manifest.get("source_event_ids")
    if (not isinstance(source_ids, list) or any(type(value) is not int or value < 1 for value in source_ids) or
            source_ids != sorted(set(source_ids)) or (payload["decision"] == "no_change" and source_ids)):
        raise ValueError("curator submission source event IDs are invalid")


def _submission_manifest(root: Path, task: dict) -> tuple[Path, dict] | None:
    directory = _submission_directory(root, task)
    manifest_path = directory / "manifest.json"
    _checked_target(root, manifest_path)
    if not manifest_path.exists():
        return None
    intent_path = directory / "intent.json"
    _checked_target(root, intent_path)
    if not intent_path.is_file() or not manifest_path.is_file():
        raise ValueError("curator submission manifest or intent is missing")
    raw = manifest_path.read_bytes()
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    manifest = json.loads(raw)
    _validate_submission_plan(root, task, intent, manifest, raw)
    for item in manifest["files"]:
        snapshot = directory / item["snapshot_path"]
        _checked_target(root, snapshot)
        raw = checked_file(directory, item["snapshot_path"], item["sha256"])
        if item["bytes"] != len(raw):
            raise ValueError("curator submission snapshot size mismatch")
    return manifest_path, manifest


def _submission_receipt(manifest_path: Path, manifest: dict) -> dict:
    return {"status": "accepted", "payload": manifest["payload"], "paths": manifest["paths"],
            "files": [{key: item[key] for key in ("path", "sha256", "bytes")} for item in manifest["files"]],
            "source_event_ids": manifest["source_event_ids"], "submission_id": manifest["submission_id"],
            "submission_manifest": str(manifest_path), "submission_sha256": _digest(manifest_path.read_bytes()),
            "publication": {"status": "pending" if manifest["payload"]["decision"] == "publish" else "no_change"}}


def load_curator_submission(root: Path, task: dict) -> dict | None:
    """Load an accepted submission only after its bridge receipt was committed."""
    root = Path(root).resolve()
    saved = _submission_manifest(root, task)
    if saved is None:
        return None
    receipt = _submission_receipt(*saved)
    database = root / ".rsi/runtime/state.sqlite3"
    _checked_target(root, database)
    if not database.is_file():
        raise ValueError("curator submission receipt database is missing")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT response_json FROM rsi_tool_operations
            WHERE task_id=? AND tool='rsi_wiki_submit' AND status='completed'""", (task["task_id"],)).fetchall()
    if not rows:
        return None
    for (encoded,) in rows:
        response = json.loads(encoded)
        if (isinstance(response, dict) and response.get("ok") is True and
                all(response.get(key) == value for key, value in receipt.items())):
            return receipt
    raise ValueError("curator submission differs from its committed tool receipt")


def submit_curator_pages(root: Path, store, task: dict, args: dict) -> dict:
    """Validate selected drafts and freeze one submission inside the bridge transaction."""
    root = Path(root).resolve()
    if task["role"] != "curator":
        raise ValueError("Wiki submission belongs to the current curator")
    if not store._atomic_depth:
        raise ValueError("curator submission requires the current bridge transaction")
    store.validate_task(task)
    current = store.task(task["task_id"])
    if (current["mission_id"] != task["mission_id"] or current["input_version"] != task["input_version"] or
            current["role"] != task["role"]):
        raise ValueError("curator submission task identity or input version is stale")
    prompt_hash = _submission_prompt_hash(root, task)
    normalized = _submission_args(root, task, args)
    if (normalized["decision"] == "no_change" and
            pending_record_ids(store, task["mission_id"], through_version=task["input_version"])):
        raise ValueError("no_change cannot leave pending source records uncovered; submit at least one source-linked "
                         "local draft or query recording uncertainty, without completing the whole batch")
    request_hash = _digest(_json(normalized).encode("utf-8"))
    directory = _submission_directory(root, task)
    intent_path = directory / "intent.json"
    _checked_target(root, intent_path)
    if intent_path.exists():
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if not isinstance(intent, dict) or intent.get("request_sha256") != request_hash:
            raise ValueError("this curator already selected a different submission")
        saved = _submission_manifest(root, task)
        if saved is not None:
            accepted = load_curator_submission(root, task)
            return accepted if accepted is not None else _submission_receipt(*saved)
        manifest = intent.get("manifest")
        encoded = _json(manifest) + "\n"
        _validate_submission_plan(root, task, intent, manifest, encoded.encode("utf-8"))
        selected = []
        workspace = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace"
        for item in manifest["files"]:
            snapshot = directory / item["snapshot_path"]
            _checked_target(root, snapshot)
            source = snapshot if snapshot.exists() else workspace / item["path"]
            _checked_target(root, source)
            if not source.is_file() or source.stat().st_nlink != 1:
                raise ValueError("selected curator draft is missing")
            raw = source.read_bytes()
            if _digest(raw) != item["sha256"] or len(raw) != item["bytes"]:
                raise ValueError("selected curator draft changed during interrupted submission")
            selected.append((item["path"], raw))
    else:
        workspace = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace"
        selected = []
        for relative in normalized["paths"]:
            path = workspace / relative
            _checked_target(root, path)
            if not path.is_file() or path.stat().st_nlink != 1 or not _inside(workspace, path.resolve()):
                raise ValueError("selected curator draft is missing or outside its workspace")
            if path.stat().st_size > 256_000:
                raise ValueError("Wiki proposal exceeds page size limit")
            selected.append((relative, path.read_bytes()))
        preflight = (_publish_curator_pages(root, store, task, draft=True, proposals=selected)
                     if normalized["decision"] == "publish" else {"source_event_ids": []})
        manifest = {"schema_version": 1, "mission_id": task["mission_id"], "task_id": task["task_id"],
                    "input_version": task["input_version"], "prompt_sha256": prompt_hash,
                    "request_sha256": request_hash,
                    "payload": {"summary": normalized["summary"], "decision": normalized["decision"]},
                    "paths": normalized["paths"], "source_event_ids": preflight["source_event_ids"],
                    "files": [{"path": relative, "snapshot_path": f"snapshots/{relative}",
                               "sha256": _digest(raw), "bytes": len(raw)} for relative, raw in selected]}
        manifest["submission_id"] = _digest(_json(manifest).encode("utf-8"))
        encoded = _json(manifest) + "\n"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_replace(intent_path, _json({"schema_version": 1, "request_sha256": request_hash,
                        "manifest_sha256": _digest(encoded.encode("utf-8")), "manifest": manifest}) + "\n")
    # A manifest is the commit marker. Retries use saved snapshot bytes and the original intent hashes.
    for item, (_, raw) in zip(manifest["files"], selected):
        snapshot = directory / item["snapshot_path"]
        _checked_target(root, snapshot)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            _atomic_bytes(snapshot, raw)
    manifest_path = directory / "manifest.json"
    _checked_target(root, manifest_path)
    _atomic_replace(manifest_path, encoded)
    return _submission_receipt(*_submission_manifest(root, task))


def _proposal_pages(root: Path, store, task: dict, *, draft: bool = False,
                    proposals: list[tuple[str, bytes]] | None = None) -> list[tuple[Path, str, str, str]]:
    submission = None if draft or proposals is not None else load_curator_submission(root, task)
    if submission is not None:
        manifest_path = Path(submission["submission_manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        proposals = [(item["path"], checked_file(manifest_path.parent, item["snapshot_path"], item["sha256"]))
                     for item in manifest["files"]]
    elif proposals is not None:
        pass
    elif draft:
        workspace = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace"
        proposals = []
        for path in sorted((workspace / "wiki").rglob("*.md")):
            _checked_target(root, path)
            if not path.is_file() or not _inside(workspace, path.resolve()):
                raise ValueError("Wiki proposal escaped the curator workspace")
            proposals.append((path.relative_to(workspace).as_posix(), path.read_bytes()))
    else:
        manifest_path, manifest = _published_manifest(root, store, task)
        proposals = [(relative, checked_file(manifest_path.parent, item["path"], item["sha256"]))
                     for item in manifest["files"]
                     if (relative := workspace_path(item)) is not None and
                     relative.startswith("wiki/") and relative.endswith(".md")]
    pages = []
    for relative, raw in proposals:
        parts = Path(relative).parts
        if len(parts) != 3 or parts[1] not in _FOLDERS or not _SLUG.fullmatch(parts[2]):
            raise ValueError(f"invalid Wiki proposal path: {relative}")
        if len(raw) > 256_000:
            raise ValueError("Wiki proposal exceeds page size limit")
        text = raw.decode("utf-8")
        meta = _frontmatter(text)
        if not meta:
            raise ValueError(f"Wiki proposal frontmatter is missing or invalid YAML: {relative}")
        for key in ("title", "created", "updated", "type", "status", "tags"):
            if not meta.get(key):
                raise ValueError(f"Wiki proposal missing {key}: {relative}")
        if meta["type"] != _FOLDERS[parts[1]] or meta["status"] not in {"draft", "candidate"}:
            raise ValueError(f"Wiki proposal type or evidence status is invalid: {relative}")
        if len(text.split("---\n", 2)[-1].strip()) < 80:
            raise ValueError(f"Wiki proposal lacks substantive content: {relative}")
        source_paths = _source_paths(meta)
        if not source_paths:
            raise ValueError(f"Wiki proposal needs traceable sources: {relative}")
        for source in source_paths:
            if source.startswith(("https://", "http://")):
                continue
            _safe_file(root, source)
        base = meta.get("base_sha256", "")
        if base and not _HASH.fullmatch(base):
            raise ValueError(f"Wiki proposal base hash is invalid: {relative}")
        content = re.sub(r"^base_sha256: [0-9a-f]{64}\n", "", text, count=1, flags=re.MULTILINE)
        target = root / "knowledge" / parts[1] / parts[2]
        pages.append((target, content, base, meta["title"]))
    if len(pages) > _MAX_PAGES:
        raise ValueError("too many Wiki proposals in one curator task")
    return pages


def publish_curator_pages(root: Path, store, task: dict) -> dict:
    lock_path = Path(root) / ".rsi/knowledge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish_curator_pages(root, store, task)


def preflight_curator_pages(root: Path, store, task: dict) -> dict:
    """Check the curator's editable drafts with the final publication rules."""
    return _publish_curator_pages(root, store, task, draft=True)


def _publish_curator_pages(root: Path, store, task: dict, *, draft: bool = False,
                           proposals: list[tuple[str, bytes]] | None = None) -> dict:
    """Validate the whole batch, then idempotently publish curator-authored drafts."""
    root = Path(root).resolve()
    submission = None if draft else load_curator_submission(root, task)
    if (not draft and submission is None and
            (_submission_directory(root, task) / "intent.json").exists()):
        raise ValueError("curator submission has no committed tool receipt")
    if submission is not None and submission["payload"]["decision"] == "no_change":
        return {"pages": [], "source_event_ids": []}
    pages = _proposal_pages(root, store, task, draft=draft, proposals=proposals)
    wiki = root / "knowledge"
    targets = {target.relative_to(wiki).with_suffix("").as_posix() for target, _, _, _ in pages}
    if len(targets) != len(pages):
        raise ValueError("duplicate Wiki proposal path")
    for target, content, base, _ in pages:
        _checked_target(root, target)
        if target.exists():
            current = target.read_text(encoding="utf-8")
            if current != content:
                if target.parent.name == "raw":
                    raise ValueError("curator cannot modify an immutable raw source page")
                if not base or _digest(current.encode()) != base:
                    raise ValueError(f"Wiki draft changed since curator read it: {target}")
                if _frontmatter(current).get("status") not in {"draft", "candidate"}:
                    raise ValueError("curator cannot modify a confirmed Wiki page")
        elif base:
            raise ValueError("Wiki proposal names a missing base page")
        for link in _WIKILINK.findall(content):
            linked = (wiki / f"{link}.md").resolve()
            if not _inside(wiki, linked) or (not linked.is_file() and link not in targets):
                raise ValueError(f"Wiki proposal has unresolved link: {link}")
    index = wiki / "index.md"
    log = wiki / "log.md"
    _safe_file(root, "knowledge/index.md")
    _safe_file(root, "knowledge/log.md")
    index_text = index.read_text(encoding="utf-8")
    log_text = log.read_text(encoding="utf-8")
    for target, _, _, _ in pages:
        section = target.relative_to(wiki).parts[0].title()
        if section == "Raw":
            section = "Sources"
        if f"## {section}\n" not in index_text:
            raise ValueError(f"Wiki index lacks {section} section")
    pending = set(pending_record_ids(store, task["mission_id"], through_version=task["input_version"]))
    covered = set()
    sources = {source for _, content, _, _ in pages for source in _source_paths(_frontmatter(content))}
    for event in _record_events(store, task["mission_id"]):
        prefix = f"knowledge/raw/runtime-records/{task['mission_id']}/{event['payload']['task_id']}/"
        if event["event_id"] in pending and any(source.startswith(prefix) for source in sources):
            covered.add(event["event_id"])
    if submission is not None:
        covered = set(submission["source_event_ids"])
    if pending and not covered:
        raise ValueError("curator must cite pending archived records in its Wiki proposals")
    if draft:
        return {"pages": [target.relative_to(root).as_posix() for target, _, _, _ in pages],
                "source_event_ids": sorted(covered)}
    published = []
    for target, content, _, title in pages:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            _atomic_replace(target, content)
        relative = target.relative_to(wiki).with_suffix("").as_posix()
        section = relative.split("/", 1)[0].title()
        if section == "Raw":
            section = "Sources"
        marker = f"## {section}\n"
        link = f"[[{relative}]]"
        if link not in index_text:
            index_text = index_text.replace(marker, marker + f"\n- {link} - {title} (curator draft).\n", 1)
        entry = f"curator {task['task_id']}: {target.relative_to(root).as_posix()}"
        if entry not in log_text:
            log_text += f"\n- [{date.today().isoformat()}] {entry}\n"
        published.append(target.relative_to(root).as_posix())
    count = sum(1 for folder in _FOLDERS for _ in (wiki / folder).rglob("*.md"))
    index_text = re.sub(r"Last updated: .*? \| Total pages: \d+",
                        f"Last updated: {date.today().isoformat()} | Total pages: {count}", index_text, count=1)
    _atomic_replace(index, index_text)
    _atomic_replace(log, log_text)
    return {"pages": published, "source_event_ids": sorted(covered)}
