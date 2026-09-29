"""Task-scoped copies of Run evidence; the control database is never shared."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .pi_agent import atomic_json
from .runtime_types import _json
from .runtime_curator import wiki_inventory

RAW_FILES = ("run_record.json", "analysis.json", "candidate.json", "stdout.log", "stderr.log",
             "evaluation_summary.json",
             "output/config.json", "output/summary.json", "output/run.json", "output/events.jsonl",
             "output/steps.jsonl", "runtime/output/config.json", "runtime/output/summary.json",
             "runtime/output/run.json", "runtime/output/events.jsonl", "runtime/output/steps.jsonl")

# No host-specific roots are mounted by default.  A deployment can pass its
# own read-only roots to ``build_external_readonly_manifest`` explicitly.
EXTERNAL_READONLY_ROOTS: tuple[tuple[str, str], ...] = ()


def build_external_readonly_manifest(task, output_dir, *, roots=EXTERNAL_READONLY_ROOTS):
    """Write a small task manifest for server roots that stay mounted read-only."""
    output_dir = Path(output_dir).resolve()
    manifest_path = output_dir / "external" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get("task_id") != task["task_id"] or
                manifest.get("mission_id") != task["mission_id"]):
            raise ValueError("external evidence manifest identity mismatch")
        _validate_external_manifest(manifest)
        return manifest_path
    entries = []
    for name, path in roots:
        if not isinstance(name, str) or not name or not isinstance(path, (str, Path)):
            raise ValueError("external evidence root is invalid")
        candidate = Path(path).expanduser()
        # A local checkout may not have the server-only roots.  Keep the
        # manifest portable by recording only roots present on this host.
        if candidate.exists():
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir() or candidate.is_symlink():
                raise ValueError("external evidence root must be a real directory")
            entries.append({"name": name, "path": str(resolved), "mode": "read-only"})
    manifest = {"schema_version": 1, "task_id": task["task_id"],
                "mission_id": task["mission_id"], "roots": entries}
    _validate_external_manifest(manifest)
    atomic_json(manifest_path, manifest)
    return manifest_path


def _validate_external_manifest(manifest):
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("roots"), list):
        raise ValueError("external evidence manifest is invalid")
    seen = set()
    for item in manifest["roots"]:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or
                not isinstance(item.get("path"), str) or item.get("mode") != "read-only"):
            raise ValueError("external evidence root is invalid")
        path = Path(item["path"])
        if not path.is_absolute() or path in seen or path.is_symlink() or not path.is_dir():
            raise ValueError("external evidence root must be an absolute real directory")
        seen.add(path)


def build_evidence_bundle(root, store, task, output_dir):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    destination = output_dir / "evidence"
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["task_id"] != task["task_id"] or manifest["mission_id"] != task["mission_id"]:
            raise ValueError("evidence manifest identity mismatch")
        for item in manifest["files"]:
            path = destination / item["path"]
            if path.is_symlink() or not path.resolve().is_relative_to(destination) or digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("evidence bundle hash mismatch")
        return manifest_path
    destination.mkdir(parents=True, exist_ok=True)
    candidate_id = task.get("candidate_id")
    idea_id = task.get("scope", {}).get("idea_id")
    research_node_id = task.get("scope", {}).get("research_node_id")
    compositions = [item for item in store.research_snapshot(task["mission_id"])["compositions"]
                    if item["node_id"] == research_node_id] if research_node_id else []
    source_run_ids = set(compositions[0]["plan"].get("frozen_source_run_ids", [])) if compositions else set()
    selected = {candidate_id} if candidate_id else set()
    if candidate_id:
        candidate = store.candidate(candidate_id)
        if candidate["parent_id"]:
            selected.add(candidate["parent_id"])
    files, omitted, total = [], [], 0

    def copy_bytes(run_id, relative, raw, source, verification):
        nonlocal total
        if total + len(raw) > 32_000_000 or len(raw) > 16_000_000:
            omitted.append({"run_id": run_id, "source": source, "reason": "evidence size limit"})
            return
        target = destination / run_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        total += len(raw)
        files.append({"run_id": run_id, "path": target.relative_to(destination).as_posix(),
                      "source": source, "sha256": digest(raw), "verification": verification})

    if task["role"] == "curator":
        inventory = Path(wiki_inventory(root, task))
        copy_bytes("wiki", "inventory.json", inventory.read_bytes(), "Wiki page inventory", "publisher hashes")

    for run in store.list_runs(task["mission_id"]):
        if compositions and run["run_id"] not in source_run_ids:
            producer = store.task(run["task_id"]) if run.get("task_id") else None
            if not producer or producer["scope"].get("research_node_id") != research_node_id:
                continue
        # Candidate reviewers also need their explicitly assigned Idea's
        # exploratory evidence. A missing parent is never a wildcard for all
        # candidate-free Runs; their original exploration provenance is kept.
        assigned_exploration = (bool(idea_id) and run.get("kind") == "exploration"
                                and run.get("idea_ref") == idea_id)
        if (candidate_id and run["candidate_id"] not in selected
                and not assigned_exploration and run["run_id"] not in source_run_ids):
            continue
        if idea_id and not candidate_id and run.get("idea_ref") != idea_id and run["run_id"] not in source_run_ids:
            continue
        if digest(_json(run["evidence"]).encode()) != run["evidence_sha256"]:
            raise ValueError("runtime Run evidence hash mismatch")
        copy_bytes(run["run_id"], "runtime_run.json", _json(run).encode(), "runtime Run projection", "runtime record hash")
        evidence = run["evidence"] or {}
        if not evidence.get("artifact_dir"):
            continue
        artifact = Path(evidence["artifact_dir"])
        if artifact.is_symlink() or not artifact.resolve().is_relative_to(root / ".rsi/runtime/runs"):
            raise ValueError("Run artifact is outside the runtime evidence boundary")
        artifact = artifact.resolve()
        summary = Path(evidence["evidence_path"]).resolve() if evidence.get("evidence_path") else None
        if summary and (not summary.is_relative_to(artifact) or not summary.is_file()
                        or digest(summary.read_bytes()) != evidence["evidence_sha256"]):
            raise ValueError("Run summary is missing, outside its artifact directory, or has a hash mismatch")
        for relative in RAW_FILES:
            path = artifact / relative
            if not path.exists():
                if path.is_symlink():
                    raise ValueError("Run artifact symlink is forbidden")
                continue
            if any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(artifact)) or not path.resolve().is_relative_to(artifact):
                raise ValueError("Run artifact symlink is forbidden")
            if path.stat().st_size > 16_000_000:
                omitted.append({"run_id": run["run_id"], "source": str(path), "reason": "evidence size limit"})
                continue
            raw = path.read_bytes()
            expected = evidence.get("artifact_hashes", {}).get(relative)
            if path.resolve() == summary:
                expected = evidence["evidence_sha256"]
            if expected and digest(raw) != expected:
                raise ValueError(f"Run artifact hash mismatch: {relative}")
            copy_bytes(run["run_id"], relative, raw, str(path),
                       "recorded at execution" if expected else "captured now; no execution-time hash")
        if run.get("kind") == "exploration":
            for relative in ("input.json", "probe.py", "stdout.log", "stderr.log"):
                path = artifact / relative
                if relative not in evidence.get("artifact_hashes", {}):
                    omitted.append({"run_id": run["run_id"], "source": relative,
                                    "reason": "artifact exceeded exploration output limit"})
                    continue
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(artifact):
                    raise ValueError("exploration input is missing or outside its artifact directory")
                raw = path.read_bytes()
                if digest(raw) != evidence.get("artifact_hashes", {}).get(relative):
                    raise ValueError("exploration input hash mismatch")
                copy_bytes(run["run_id"], relative, raw, str(path), "recorded at execution")
            for relative, expected in evidence.get("artifact_hashes", {}).items():
                if not relative.startswith("output/"):
                    continue
                path = artifact / relative
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(artifact):
                    raise ValueError("exploration output is outside its artifact directory")
                raw = path.read_bytes()
                if digest(raw) != expected:
                    raise ValueError("exploration output hash mismatch")
                copy_bytes(run["run_id"], relative, raw, str(path), "recorded at execution")
    atomic_json(manifest_path, {"schema_version": 1, "task_id": task["task_id"], "mission_id": task["mission_id"],
                               "candidate_id": candidate_id, "composition_source_run_ids": sorted(source_run_ids),
                               "files": files, "omitted": omitted})
    return manifest_path


def register_curator_index(manifest_path, task, *, allow_new=True):
    """Seal the prompt's task-local document index into its read-only evidence."""
    if task["role"] != "curator":
        raise ValueError("only curator tasks have a document index")
    manifest_path = Path(manifest_path).resolve()
    destination = manifest_path.parent
    index = destination / "wiki/curation_sources.json"
    if index.is_symlink() or not index.is_file() or not index.resolve().is_relative_to(destination):
        raise ValueError("curator document index is missing or outside its evidence bundle")
    raw = index.read_bytes()
    identity = json.loads(raw)
    if (not isinstance(identity, dict) or identity.get("mission_id") != task["mission_id"] or
            identity.get("curator_task_id") != task["task_id"] or
            identity.get("input_version") != task["input_version"]):
        raise ValueError("curator document index identity mismatch")
    manifest = json.loads(manifest_path.read_text())
    if manifest["task_id"] != task["task_id"] or manifest["mission_id"] != task["mission_id"]:
        raise ValueError("evidence manifest identity mismatch")
    relative = index.relative_to(destination).as_posix()
    existing = [item for item in manifest["files"] if item["path"] == relative]
    current_hash = digest(raw)
    if existing:
        if len(existing) != 1 or existing[0]["sha256"] != current_hash:
            raise ValueError("curator document index hash mismatch")
        return manifest_path
    if not allow_new:
        raise ValueError("frozen curator document index has no recorded hash")
    manifest["files"].append({"run_id": "wiki", "path": relative,
                              "source": "Curator document index", "sha256": current_hash,
                              "verification": "task-scoped snapshot"})
    atomic_json(manifest_path, manifest)
    return manifest_path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()
