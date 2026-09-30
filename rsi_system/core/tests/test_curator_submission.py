"""Explicit Curator submission freezes selected drafts without publishing them."""
import hashlib
import json
from pathlib import Path

import pytest

from rsi_system.core.pi_agent import atomic_json
from rsi_system.core.runtime import RuntimeStore
from rsi_system.core.runtime_curator import (
    archive_records, load_curator_submission, pending_record_ids,
    publish_curator_pages, submit_curator_pages)
from rsi_system.core.runtime_tools import ToolBridge
from rsi_system.core.runtime_types import _json
from .test_runtime_curator import page, published, setup, source


def fixture(tmp_path, *, sources=1, role="curator"):
    root, coordinator = setup(tmp_path, max_agent_tasks=32)
    with RuntimeStore(coordinator.store_path) as store:
        authors = [published(root, store, idea=f"idea-{number}") for number in range(sources)]
        archive_records(root, store, "wiki-mission")
        store.create_task("wiki-mission", role, "Submit selected source-linked drafts", "submission fixture")
        task = store.claim_task("wiki-mission")
    directory = root / ".rsi/runtime/tasks" / task["task_id"]
    (directory / "workspace").mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "input.json", {"task_id": task["task_id"], "input_version": task["input_version"],
                                          "prompt": "Frozen engineering fixture prompt"})
    return root, coordinator, task, authors


def draft(root, task, author, relative="wiki/findings/selected.md", **changes):
    target = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page(author, **changes))
    return target


def submit(root, task, args=None, *, call_id="submit-1"):
    return ToolBridge(root, task["task_id"]).call({"tool": "rsi_wiki_submit", "call_id": call_id,
        "args": args if args is not None else {"paths": ["wiki/findings/selected.md"],
                                               "summary": "Selected evidence and uncertainty recorded"}})


def test_selected_only_snapshot_deferred_publication_and_partial_coverage(tmp_path):
    root, coordinator, task, authors = fixture(tmp_path, sources=2)
    chosen = draft(root, task, authors[0])
    original = chosen.read_bytes()
    unselected = draft(root, task, authors[1], "wiki/findings/unselected.md")
    invalid = chosen.parent / "invalid.md"
    invalid.write_text("Malformed unfinished workspace draft")
    provenance = root / source(authors[0])
    source_before = provenance.read_bytes()
    wiki_before = (root / "knowledge/index.md").read_bytes()
    result = submit(root, task, {"paths": [str(chosen)], "summary": "First source curated; second remains open"})
    assert result["ok"] and result["status"] == "accepted"
    assert result["publication"] == {"status": "pending"}
    assert result["payload"] == {"decision": "publish", "summary": "First source curated; second remains open"}
    assert result["paths"] == ["wiki/findings/selected.md"]
    assert result["files"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert Path(result["submission_manifest"]).is_file()
    assert result["submission_sha256"] == hashlib.sha256(Path(result["submission_manifest"]).read_bytes()).hexdigest()
    assert load_curator_submission(root, task) == {key: value for key, value in result.items() if key != "ok"}
    assert not (root / "knowledge/findings/selected.md").exists()
    assert (root / "knowledge/index.md").read_bytes() == wiki_before
    chosen.write_text("Changes after submission must never be published")
    unselected.unlink()
    invalid.unlink()
    with RuntimeStore(coordinator.store_path) as store:
        original_pending = pending_record_ids(store, "wiki-mission")
        assert result["source_event_ids"] == original_pending[:1]
        store.finish_task(task["task_id"], task["lease_token"], {"payload": result["payload"]})
        completed = store.task(task["task_id"])
        first = publish_curator_pages(root, store, completed)
        assert first["pages"] == ["knowledge/findings/selected.md"]
        assert (root / first["pages"][0]).read_bytes() == original
        store.event("wiki-mission", "knowledge_curated", {"task_id": task["task_id"], **first})
        assert pending_record_ids(store, "wiki-mission") == original_pending[1:]
        assert publish_curator_pages(root, store, completed) == first
    assert not (root / "knowledge/findings/unselected.md").exists()
    assert provenance.read_bytes() == source_before
    assert (root / "knowledge/index.md").read_text().count("[[findings/selected]]") == 1
    assert (root / "knowledge/log.md").read_text().count(f"curator {task['task_id']}") == 1


def test_call_replay_and_second_distinct_submission_are_rejected(tmp_path):
    root, coordinator, task, authors = fixture(tmp_path)
    selected = draft(root, task, authors[0])
    args = {"paths": ["wiki/findings/selected.md"], "summary": "Selected evidence recorded"}
    first = submit(root, task, args)
    assert first["ok"]
    selected.unlink()
    assert submit(root, task, args) == first
    assert submit(root, task, args, call_id="same-submit-new-call") == first
    different = {**args, "summary": "A different submission"}
    replay = submit(root, task, different)
    assert replay["ok"] is False and "reused with different arguments" in replay["error"]["message"]
    second = submit(root, task, different, call_id="second-submit")
    assert second["ok"] is False and "different submission" in second["error"]["message"]
    assert load_curator_submission(root, task)["submission_id"] == first["submission_id"]
    with RuntimeStore(coordinator.store_path) as store:
        assert not [event for event in store.events("wiki-mission") if event["kind"] == "knowledge_curated"]


@pytest.mark.parametrize("args", [
    {"paths": [], "summary": "Publish empty"},
    {"paths": ["wiki/findings/selected.md"], "summary": "", "decision": "publish"},
    {"paths": [], "summary": "", "decision": "no_change"},
    {"paths": ["wiki/findings/selected.md"], "summary": "No change", "decision": "no_change"},
    {"paths": [], "summary": "Invalid decision", "decision": "stop"},
    {"paths": "wiki/findings/selected.md", "summary": "Bad paths"},
    {"paths": ["wiki/findings/selected.md"], "summary": "Unknown field", "extra": True},
])
def test_empty_or_invalid_submission_has_no_commit(tmp_path, args):
    root, _, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    result = submit(root, task, args)
    assert result["ok"] is False
    assert load_curator_submission(root, task) is None
    assert not (root / ".rsi/runtime/tasks" / task["task_id"] / "curator-submission/intent.json").exists()


def test_no_change_needs_no_pending_sources_and_keeps_workspace_unselected(tmp_path):
    root, coordinator, task, _ = fixture(tmp_path, sources=0)
    leftover = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace/wiki/findings/unfinished.md"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("Invalid leftover draft; not selected")
    before = (root / "knowledge/index.md").read_bytes()
    result = submit(root, task, {"paths": [], "decision": "no_change", "summary": "No pending archived records exist"})
    assert result["ok"] and result["paths"] == [] and result["source_event_ids"] == []
    assert result["publication"] == {"status": "no_change"}
    with RuntimeStore(coordinator.store_path) as store:
        assert publish_curator_pages(root, store, task) == {"pages": [], "source_event_ids": []}
    assert (root / "knowledge/index.md").read_bytes() == before
    assert load_curator_submission(root, task)["payload"]["decision"] == "no_change"


def test_no_change_with_pending_sources_returns_incremental_repair(tmp_path):
    root, coordinator, task, _ = fixture(tmp_path)
    result = submit(root, task, {"paths": [], "decision": "no_change", "summary": "Source review remains inconclusive"})
    assert result["ok"] is False
    assert "at least one source-linked" in result["error"]["message"]
    assert "without completing the whole batch" in result["error"]["message"]
    assert load_curator_submission(root, task) is None
    with RuntimeStore(coordinator.store_path) as store:
        assert len(pending_record_ids(store, "wiki-mission")) == 1


@pytest.mark.parametrize("kind", ["duplicate", "escape", "private", "outside", "nonmarkdown", "symlink", "symlink-parent"])
def test_only_regular_own_workspace_wiki_files_can_be_selected(tmp_path, kind):
    root, _, task, authors = fixture(tmp_path)
    chosen = draft(root, task, authors[0])
    if kind == "duplicate":
        paths = [str(chosen), "wiki/findings/selected.md"]
    elif kind == "escape":
        paths = ["wiki/findings/../selected.md"]
    elif kind == "private":
        paths = [str(root / ".rsi/runtime/tasks" / task["task_id"] / "input.json")]
    elif kind == "outside":
        paths = [str(root / source(authors[0]))]
    elif kind == "nonmarkdown":
        paths = ["wiki/findings/selected.txt"]
    elif kind == "symlink":
        link = chosen.parent / "linked.md"
        link.symlink_to(chosen)
        paths = [str(link)]
    else:
        chosen.unlink()
        chosen.parent.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / chosen.name).write_text(page(authors[0]))
        chosen.parent.symlink_to(outside, target_is_directory=True)
        paths = [str(chosen)]
    result = submit(root, task, {"paths": paths, "summary": "Attempt invalid path selection"})
    assert result["ok"] is False
    assert load_curator_submission(root, task) is None


@pytest.mark.parametrize("change", [{"sources": []}, {"link": "findings/missing"}, {"status": "confirmed"}])
def test_selected_drafts_reuse_schema_source_and_link_validation(tmp_path, change):
    root, _, task, authors = fixture(tmp_path)
    draft(root, task, authors[0], **change)
    assert submit(root, task)["ok"] is False
    assert load_curator_submission(root, task) is None


@pytest.mark.parametrize("failure", ["role", "lease", "mission", "input-version", "missing-input"])
def test_submission_requires_current_curator_lease_and_frozen_input(tmp_path, failure):
    root, coordinator, task, authors = fixture(tmp_path, role="researcher" if failure == "role" else "curator")
    draft(root, task, authors[0])
    frozen = root / ".rsi/runtime/tasks" / task["task_id"] / "input.json"
    if failure == "lease":
        with RuntimeStore(coordinator.store_path) as store:
            store.db.execute("UPDATE agent_tasks SET lease_until=? WHERE task_id=?", ("2000-01-01", task["task_id"]))
            store.db.commit()
    elif failure == "mission":
        with RuntimeStore(coordinator.store_path) as store:
            store.set_mission_status("wiki-mission", "paused", reason="fixture paused")
    elif failure == "input-version":
        saved = json.loads(frozen.read_text())
        saved["input_version"] += 1
        atomic_json(frozen, saved)
    elif failure == "missing-input":
        frozen.unlink()
    assert submit(root, task)["ok"] is False
    assert load_curator_submission(root, task) is None


@pytest.mark.parametrize("artifact", ["snapshot", "manifest", "intent", "prompt", "version"])
def test_loading_and_publication_reject_tampering_or_stale_identity(tmp_path, artifact):
    root, coordinator, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    receipt = submit(root, task)
    manifest = Path(receipt["submission_manifest"])
    if artifact == "snapshot":
        target = manifest.parent / "snapshots/wiki/findings/selected.md"
        target.write_bytes(target.read_bytes() + b"changed")
    elif artifact == "manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    elif artifact == "intent":
        target = manifest.parent / "intent.json"
        intent = json.loads(target.read_text())
        intent["manifest_sha256"] = "0" * 64
        atomic_json(target, intent)
    elif artifact == "prompt":
        frozen = manifest.parent.parent / "input.json"
        saved = json.loads(frozen.read_text())
        saved["prompt"] += " silently edited"
        atomic_json(frozen, saved)
    else:
        task = {**task, "input_version": task["input_version"] + 1}
    with pytest.raises(ValueError):
        load_curator_submission(root, task)
    with RuntimeStore(coordinator.store_path) as store:
        with pytest.raises(ValueError):
            publish_curator_pages(root, store, task)
    assert not (root / "knowledge/findings/selected.md").exists()


def test_manifest_and_intent_rewrite_cannot_replace_the_committed_receipt(tmp_path):
    root, _, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    receipt = submit(root, task)
    path = Path(receipt["submission_manifest"])
    manifest = json.loads(path.read_text())
    manifest["payload"]["summary"] = "Coherently replaced summary"
    manifest["request_sha256"] = hashlib.sha256(_json({"paths": manifest["paths"], **manifest["payload"]}).encode()).hexdigest()
    manifest["submission_id"] = hashlib.sha256(_json({key: value for key, value in manifest.items()
                                                      if key != "submission_id"}).encode()).hexdigest()
    encoded = _json(manifest) + "\n"
    path.write_text(encoded)
    atomic_json(path.parent / "intent.json", {"schema_version": 1, "manifest": manifest,
        "request_sha256": manifest["request_sha256"], "manifest_sha256": hashlib.sha256(encoded.encode()).hexdigest()})
    with pytest.raises(ValueError, match="committed tool receipt"):
        load_curator_submission(root, task)
    repeated = submit(root, task, {"paths": manifest["paths"], "summary": manifest["payload"]["summary"]},
                      call_id="tampered-resubmit")
    assert repeated["ok"] is False


def test_accepted_submission_is_invisible_before_bridge_operation_commit(tmp_path, monkeypatch):
    root, _, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    put = RuntimeStore.put_tool_operation
    observed = []

    def before_commit(store, task_id, call_id, tool, request_hash, status, response=None):
        if tool == "rsi_wiki_submit" and status == "completed":
            assert response["status"] == "accepted"
            assert Path(response["submission_manifest"]).is_file()
            assert load_curator_submission(root, task) is None
            observed.append(True)
        return put(store, task_id, call_id, tool, request_hash, status, response)

    monkeypatch.setattr(RuntimeStore, "put_tool_operation", before_commit)
    result = submit(root, task)
    assert result["ok"] and observed == [True]
    assert load_curator_submission(root, task)["submission_id"] == result["submission_id"]


def test_uncommitted_submission_cannot_fall_back_to_legacy_publication(tmp_path, monkeypatch):
    root, coordinator, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    put = RuntimeStore.put_tool_operation
    failures = []

    def fail_operation_once(store, task_id, call_id, tool, request_hash, status, response=None):
        if tool == "rsi_wiki_submit" and status == "completed" and not failures:
            failures.append(True)
            raise OSError("simulated database commit interruption")
        return put(store, task_id, call_id, tool, request_hash, status, response)

    monkeypatch.setattr(RuntimeStore, "put_tool_operation", fail_operation_once)
    assert submit(root, task)["ok"] is False
    assert load_curator_submission(root, task) is None
    with RuntimeStore(coordinator.store_path) as store:
        with pytest.raises(ValueError, match="no committed tool receipt"):
            publish_curator_pages(root, store, task)
    assert submit(root, task)["ok"] is True


def test_interrupted_file_commit_reuses_frozen_selection_on_retry(tmp_path, monkeypatch):
    from rsi_system.core import runtime_curator
    root, coordinator, task, authors = fixture(tmp_path)
    chosen = draft(root, task, authors[0])
    original = chosen.read_bytes()
    replace = runtime_curator._atomic_replace
    interrupted = []

    def fail_manifest_once(path, content):
        if path.name == "manifest.json" and path.parent.name == "curator-submission" and not interrupted:
            interrupted.append(True)
            raise OSError("simulated manifest commit interruption")
        return replace(path, content)

    monkeypatch.setattr(runtime_curator, "_atomic_replace", fail_manifest_once)
    first = submit(root, task)
    assert first["ok"] is False and load_curator_submission(root, task) is None
    chosen.write_text("workspace changes after the failed file commit")
    different = submit(root, task, {"paths": [], "summary": "Changed intent", "decision": "publish"}, call_id="different")
    assert different["ok"] is False
    retry = submit(root, task)
    assert retry["ok"] and load_curator_submission(root, task)["submission_id"] == retry["submission_id"]
    with RuntimeStore(coordinator.store_path) as store:
        assert publish_curator_pages(root, store, task)["pages"] == ["knowledge/findings/selected.md"]
    assert (root / "knowledge/findings/selected.md").read_bytes() == original


def test_missing_partial_snapshot_cannot_freeze_changed_workspace_on_retry(tmp_path, monkeypatch):
    from rsi_system.core import runtime_curator
    root, _, task, authors = fixture(tmp_path)
    chosen = draft(root, task, authors[0])
    write = runtime_curator._atomic_bytes
    failures = []

    def fail_snapshot_once(path, raw):
        if "curator-submission" in path.parts and not failures:
            failures.append(True)
            raise OSError("simulated snapshot interruption")
        return write(path, raw)

    monkeypatch.setattr(runtime_curator, "_atomic_bytes", fail_snapshot_once)
    assert submit(root, task)["ok"] is False
    chosen.write_text(page(authors[0]) + "Changed after interrupted selection.\n")
    retry = submit(root, task)
    assert retry["ok"] is False and "changed during interrupted submission" in retry["error"]["message"]
    assert load_curator_submission(root, task) is None


def test_publication_rechecks_current_wiki_base_hash_after_acceptance(tmp_path):
    root, coordinator, task, authors = fixture(tmp_path)
    target = root / "knowledge/findings/selected.md"
    target.write_text(page(authors[0]) + "Earlier content.\n")
    base = hashlib.sha256(target.read_bytes()).hexdigest()
    draft(root, task, authors[0], base=base)
    assert submit(root, task)["ok"]
    target.write_text(target.read_text() + "Concurrent Wiki change.\n")
    with RuntimeStore(coordinator.store_path) as store:
        with pytest.raises(ValueError, match="changed since curator read"):
            publish_curator_pages(root, store, task)
    assert "Concurrent Wiki change" in target.read_text()


def test_direct_submission_requires_a_transaction(tmp_path):
    root, coordinator, task, authors = fixture(tmp_path)
    draft(root, task, authors[0])
    with RuntimeStore(coordinator.store_path) as store:
        with pytest.raises(ValueError, match="bridge transaction"):
            submit_curator_pages(root, store, task, {"paths": ["wiki/findings/selected.md"], "summary": "Evidence"})
