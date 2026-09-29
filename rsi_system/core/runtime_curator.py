"""Archive published research records and publish source-checked Wiki drafts."""
from __future__ import annotations

from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .knowledge import _WIKILINK, _atomic_replace, _frontmatter, _inside, _safe_file, _source_paths
from .runtime_communications import checked_file, load_manifest, workspace_path
from .runtime_types import _json


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
    """Write a compact address book for a curator's source documents.

    The index contains provenance and file addresses, never the contents of the
    archived responses. This lets the model choose which documents to read while
    keeping the full immutable records available on disk.
    """
    root = Path(root).resolve()
    mission_id = task["mission_id"]
    events = _record_events(store, mission_id, include_coordinator=True)
    by_id = {event["event_id"]: event for event in events}
    pending = set(pending_ids)

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
        documents = []
        for item in record.get("files", []):
            relative = item.get("path")
            if not isinstance(relative, str):
                continue
            address = (root / relative).resolve()
            _checked_target(root, address)
            documents.append({"address": str(address), "path": relative,
                              "sha256": item.get("sha256"), "bytes": item.get("bytes"),
                              "kind": "response" if relative.endswith("/response.json") else "handoff"})
        return {"source_event_id": event["event_id"], "event_version": event["version"],
                "task_id": source_task["task_id"], "role": source_task["role"],
                "objective": source_task["objective"], "reason": source_task["reason"],
                "idea_id": source_task["scope"].get("idea_id"),
                "research_node_id": source_task["scope"].get("research_node_id"),
                "research_revision": source_task["scope"].get("research_revision"),
                "planning": planning, "provenance": str(provenance), "documents": documents}

    records = [entry(by_id[event_id], planning=False) for event_id in pending_ids if event_id in by_id]
    planning_events = [event for event in events
                       if event["event_id"] not in pending and
                       event["payload"]["task_id"] != task["task_id"] and
                       (store.task(event["payload"]["task_id"]) or {}).get("role") == "coordinator"]
    planning = [entry(event, planning=True) for event in planning_events[-4:]]
    index = root / ".rsi/runtime/tasks" / task["task_id"] / "evidence/wiki/curation_sources.json"
    _checked_target(root, index)
    index.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace(index, json.dumps({
        "schema_version": 1,
        "mission_id": mission_id,
        "curator_task_id": task["task_id"],
        "input_version": task["input_version"],
        "purpose": "Read this address book first, then selectively read source documents before writing Wiki proposals.",
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


def _proposal_pages(root: Path, store, task: dict, *, draft: bool = False) -> list[tuple[Path, str, str, str]]:
    if draft:
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


def _publish_curator_pages(root: Path, store, task: dict, *, draft: bool = False) -> dict:
    """Validate the whole batch, then idempotently publish curator-authored drafts."""
    root = Path(root).resolve()
    pages = _proposal_pages(root, store, task, draft=draft)
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
