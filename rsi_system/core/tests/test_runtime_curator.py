"""Knowledge publication tests with simulated Agents, not scientific validation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_communications import (
    prepare_communications, publish_communications, snapshot_communications)
from rsi_system.core.runtime_curator import (
    archive_records, curator_document_index, pending_record_ids, publish_curator_pages)
from rsi_system.core.runtime_evidence import build_evidence_bundle, register_curator_index
from rsi_system.core.runtime_knowledge import reconcile_runtime_knowledge
from rsi_system.core.knowledge import _frontmatter, _source_paths
from rsi_system.core.runtime_roles import ROLE_GUIDANCE, model_policy
from rsi_system.core.runtime_tools import ToolBridge, serve_tools
from rsi_system.core.pi_agent import TOOLS
from .test_orchestrator import project
from .test_runtime_acceptance import decision


def setup(tmp_path, **resources):
    root = project(tmp_path)
    index = root / "knowledge/index.md"
    index.write_text(index.read_text() + "\n## Concepts\n\n## Queries\n\n## Sources\n")
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("wiki-mission", "study memory mechanisms",
        resources={"knowledge_curator": True, **resources}))
    return root, coordinator


def published(root, store, role="researcher", files=None, idea="idea-a"):
    task = store.create_task("wiki-mission", role, "inspect evidence", "fixture", idea_id=idea)
    task = store.claim_task("wiki-mission")
    directory = root / ".rsi/runtime/tasks" / task["task_id"]
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    for relative, content in (files or {"handoff.md": "An unverified mechanism and the literature reading limits.\n"}).items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    payload = {"summary": "fixture published", "decision": "publish"}
    manifest = snapshot_communications(task, directory, payload)
    publish_communications(store, task, manifest, "completed")
    store.finish_task(task["task_id"], task["lease_token"], {"payload": payload})
    return task


def source(task):
    return f"knowledge/raw/runtime-records/wiki-mission/{task['task_id']}/provenance.json"


def curator_prompt_context(prompt):
    decoder = json.JSONDecoder()
    for offset, character in enumerate(prompt):
        if character != "{":
            continue
        try:
            envelope, _ = decoder.raw_decode(prompt[offset:])
        except ValueError:
            continue
        context = envelope.get("context") if isinstance(envelope, dict) else None
        if isinstance(context, dict) and isinstance(context.get("curation"), dict):
            return context
    raise AssertionError("curator prompt envelope is missing")


def page(task, *, status="draft", base="", sources=None, link="findings/current-evidence", kind="finding"):
    return ("---\ntitle: Memory mechanism\ncreated: 2026-09-26\nupdated: 2026-09-26\n"
            f"type: {kind}\nstatus: {status}\ntags: [memory, evidence]\n"
            "sources: " + json.dumps(sources if sources is not None else [source(task)]) + "\n" +
            (f"base_sha256: {base}\n" if base else "") + "---\n\n"
            "# Memory mechanism\n\nHypothesis: response timing may confound apparent memory utility. "
            "No controlled result establishes the effect. Reading depth remains limited. "
            f"Related evidence: [[{link}]].\n")


def submit_fixture_draft(root, invocation, path, summary):
    context = curator_prompt_context(invocation["prompt"])
    receipt = ToolBridge(root, context["task"]["task_id"]).call({
        "tool": "rsi_wiki_submit", "call_id": "fixture-submit", "args": {"paths": [path], "summary": summary}})
    assert receipt["ok"] is True, receipt
    return {"payload": receipt["payload"], "tool_protocol_version": 1,
            "prompt_sha256": hashlib.sha256(invocation["prompt"].encode()).hexdigest(),
            "curator_submission": {key: value for key, value in receipt.items() if key != "ok"}}


def test_automatic_curation_archive_publish_and_stop(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store, files={"handoff.md": "Substantive exploratory Idea.\n",
            "literature.json": '{"url":"https://example.org/paper","access":"abstract"}',
            "paper.pdf": b"%PDF-fixture-source"})
    assert coordinator.tick(require_model=False)["role"] == "curator"

    def agent(**kwargs):
        if kwargs["role"] == "coordinator":
            return {"payload": decision(action="stop"), "tool_protocol_version": 1}
        assert kwargs["role"] == "curator"
        inventory = Path(kwargs["output_dir"]) / "evidence/wiki/inventory.json"
        assert json.loads(inventory.read_text())["pages"][0]["sha256"]
        assert "pending_source_event_ids" in kwargs["prompt"]
        workspace = Path(kwargs["output_dir"]) / "workspace/wiki/findings"
        workspace.mkdir(parents=True)
        (workspace / "memory-mechanism.md").write_text(page(author))
        return submit_fixture_draft(root, kwargs, "wiki/findings/memory-mechanism.md", "organized attributed records")

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed", result
    archive = root / source(author)
    record = json.loads(archive.read_text())
    for item in record["files"]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert (archive.parent / "notes/paper.pdf").read_bytes() == b"%PDF-fixture-source"
    assert (archive.parent / "notes/handoff.md.txt").read_text() == "Substantive exploratory Idea.\n"
    with RuntimeStore(coordinator.store_path) as store:
        assert pending_record_ids(store, "wiki-mission") == []
        before = (root / "knowledge/log.md").read_text()
        assert not reconcile_runtime_knowledge(root, store, "wiki-mission")["errors"]
        assert (root / "knowledge/log.md").read_text() == before
    assert (root / "knowledge/index.md").read_text().count("[[findings/memory-mechanism]]") == 1
    assert "status: draft" in (root / "knowledge/findings/memory-mechanism.md").read_text()
    assert coordinator.tick(require_model=False)["role"] == "coordinator"
    assert coordinator.tick(agent_runner=agent, require_model=False)["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("wiki-mission")["status"] == "stopped"


def test_curator_prompt_passes_document_addresses_without_history_payloads(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store, files={"handoff.md": "source finding\n"})
        # Simulate a large prior handoff/result in the blackboard. The curator
        # should receive its archived address, not this payload inline.
        store.db.execute("UPDATE agent_tasks SET output_json=? WHERE task_id=?",
                         (json.dumps({"payload": {"summary": "large-result-" + "x" * 50000}}),
                          author["task_id"]))
        store.db.commit()

    assert coordinator.tick(require_model=False)["role"] == "curator"
    captured = {}

    def agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["context"] = curator_prompt_context(kwargs["prompt"])
        proposal = Path(kwargs["output_dir"]) / "workspace/wiki/queries/source-questions.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text(page(author, kind="query"))
        return submit_fixture_draft(root, kwargs, "wiki/queries/source-questions.md", "Source addresses and open questions inspected")

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed", result
    assert len(captured["prompt"].encode()) < 40_000
    context = captured["context"]
    assert context["tasks"] == []
    assert context["runs"] == []
    assert context["events"] == []
    index_path = Path(context["curation"]["document_index"])
    index_text = index_path.read_text()
    assert max(map(len, index_text.splitlines())) < 4096
    index = json.loads(index_text)
    assert index["records"][0]["provenance"].endswith("provenance.json")
    reference = index["records"][0]["documents_manifest"]
    assert reference["mode"] == "read-only"
    documents = json.loads(Path(reference["path"]).read_text())["documents"]
    assert any(item["path"].endswith("handoff.md.txt") for item in documents)
    assert all(type(item["bytes"]) is int and "address" not in item for item in documents)
    assert "large-result-" not in captured["prompt"]


def test_curator_document_index_is_hash_checked_read_only_evidence(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node runtime required for real Pi guard")
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
    assert coordinator.tick(require_model=False)["role"] == "curator"
    guard = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"

    def agent(**kwargs):
        context = curator_prompt_context(kwargs["prompt"])
        index = Path(context["curation"]["document_index"])
        catalog = Path(json.loads(index.read_text())["records"][0]["documents_manifest"]["path"])
        manifest = Path(kwargs["evidence_manifest"])
        files = json.loads(manifest.read_text())["files"]
        assert next(item for item in files if item["path"] == "wiki/curation_sources.json")["sha256"] == \
            hashlib.sha256(index.read_bytes()).hexdigest()
        assert next(item for item in files if item["path"] == catalog.relative_to(manifest.parent).as_posix())["sha256"] == \
            hashlib.sha256(catalog.read_bytes()).hexdigest()
        inventory = manifest.parent / "wiki/inventory.json"
        other_private = root / ".rsi/runtime/tasks" / author["task_id"] / "communications/outbox/manifest.json"
        checks = [["read", str(index), False], ["read", str(catalog), False], ["read", str(manifest), False],
                  ["read", str(inventory), False], ["write", str(index), True],
                  ["edit", str(index), True], ["write", str(catalog), True], ["edit", str(catalog), True],
                  ["read", str(other_private), True],
                  ["read", str(coordinator.store_path), True],
                  ["read", str(kwargs["output_dir"] / "input.json"), True],
                  ["bash", ".", True]]
        script = '''import guard from GUARD;
import {readFileSync, writeFileSync} from 'node:fs';
let hook; guard({on: (_, fn) => { hook = fn; }, registerTool: () => {}});
for (const [toolName, path, blocked] of JSON.parse(process.env.CHECKS)) {
  const response = await hook({toolName, input: {path}});
  if (Boolean(response?.block) !== blocked) throw Error(JSON.stringify({toolName, path, response, blocked}));
  if (!blocked && toolName === 'read') readFileSync(path);
}
for (const path of [process.env.INDEX, process.env.CATALOG]) {
  const raw = readFileSync(path);
  try {
    writeFileSync(path, Buffer.concat([raw, Buffer.from(' ')]));
    if (!(await hook({toolName: 'read', input: {path}}))?.block) throw Error('changed catalog was readable');
  } finally { writeFileSync(path, raw); }
}
'''.replace("GUARD", json.dumps(guard.as_uri()))
        subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script],
            check=True, capture_output=True, text=True,
            env={**os.environ, "RSI_AGENT_ROOT": str(root), "RSI_AGENT_ROLE": "curator",
                 "RSI_AGENT_WORKSPACE": str(Path(kwargs["output_dir"]) / "workspace"),
                 "RSI_EVIDENCE_MANIFEST": str(manifest), "INDEX": str(index),
                 "CATALOG": str(catalog),
                 "CHECKS": json.dumps(checks)})
        before = manifest.read_bytes()
        register_curator_index(manifest, context["task"], allow_new=False)
        assert manifest.read_bytes() == before
        without_index = json.loads(before)
        without_index["files"] = [item for item in without_index["files"]
                                  if item["path"] != "wiki/curation_sources.json"]
        try:
            manifest.write_text(json.dumps(without_index))
            with pytest.raises(ValueError, match="no recorded hash"):
                register_curator_index(manifest, context["task"], allow_new=False)
        finally:
            manifest.write_bytes(before)
        proposal = Path(kwargs["output_dir"]) / "workspace/wiki/queries/source-access.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text(page(author, kind="query"))
        return submit_fixture_draft(root, kwargs, "wiki/queries/source-access.md", "Verified source access and open questions")

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed", result


def test_selector_index_keeps_all_sources_small_and_records_actual_sizes(tmp_path):
    root, coordinator = setup(tmp_path, max_agent_tasks=64)
    with RuntimeStore(coordinator.store_path) as store:
        authors = [published(root, store, files={
            f"reading-{number}.md": "full document content remains outside the selector\n"
            for number in range(15 if index == 0 else 10)}, idea=f"idea-{index}")
            for index in range(15)]
        planners = [published(root, store, "coordinator", idea=None) for _ in range(6)]
        objective = "complete research responsibility " + "x" * 6000
        reason = "complete reason " + "y" * 5000
        store.db.execute("UPDATE agent_tasks SET objective=?,reason=? WHERE task_id=?",
                         (objective, reason, authors[0]["task_id"]))
        store.db.commit()
        archive_records(root, store, "wiki-mission")
        provenance_before = {root / source(author): (root / source(author)).read_bytes()
                             for author in authors + planners}
        pending = pending_record_ids(store, "wiki-mission")
        store.create_task("wiki-mission", "curator", "organize sources", "fixture")
        curator = store.claim_task("wiki-mission")
        directory = root / ".rsi/runtime/tasks" / curator["task_id"]
        manifest_path = build_evidence_bundle(root, store, curator, directory)
        index_path = Path(curator_document_index(root, store, curator, pending_ids=pending))
        raw = index_path.read_bytes()
        selector = json.loads(raw)
        assert selector["schema_version"] == 2
        assert selector["record_count"] == 15
        assert selector["planning_record_count"] == 6
        assert selector["document_count"] == 182
        assert len(raw) < 30_000
        assert objective.encode() not in raw and reason.encode() not in raw
        assert b"full document content" not in raw
        assert [item["source_event_id"] for item in selector["records"]] == pending
        assert {item["task_id"] for item in selector["planning_records"]} == {item["task_id"] for item in planners}
        artifacts = {"wiki/curation_sources.json"}
        for record in selector["records"] + selector["planning_records"]:
            assert "documents" not in record and "reason" not in record and "objective" not in record
            reference = record["documents_manifest"]
            catalog_path = Path(reference["path"])
            catalog_raw = catalog_path.read_bytes()
            assert reference["mode"] == "read-only"
            assert reference["sha256"] == hashlib.sha256(catalog_raw).hexdigest()
            assert reference["bytes"] == len(catalog_raw)
            catalog = json.loads(catalog_raw)
            assert len(catalog["documents"]) == reference["document_count"]
            if record["task_id"] == authors[0]["task_id"]:
                assert record["objective_excerpt"] == objective[:240]
                assert catalog["objective"] == objective and catalog["reason"] == reason
            for document in catalog["documents"]:
                address = Path(document["path"])
                assert address.is_absolute() and address.is_relative_to(root)
                assert document["bytes"] == address.stat().st_size
                assert document["sha256"] == hashlib.sha256(address.read_bytes()).hexdigest()
                assert "address" not in document
            artifacts.add(catalog_path.relative_to(manifest_path.parent).as_posix())
        # Only referenced catalogs may grant access to private evidence paths.
        hidden = index_path.parent / "curation_records/hidden.json"
        hidden.write_text('{"private":true}\n')
        register_curator_index(manifest_path, curator)
        files = json.loads(manifest_path.read_text())["files"]
        registered = {item["path"] for item in files if item["source"].startswith("Curator")}
        assert registered == artifacts
        assert hidden.relative_to(manifest_path.parent).as_posix() not in registered
        receipts = manifest_path.read_bytes()
        register_curator_index(manifest_path, curator, allow_new=False)
        assert manifest_path.read_bytes() == receipts
        assert Path(curator_document_index(root, store, curator, pending_ids=[])).read_bytes() == raw
        assert all(path.read_bytes() == before for path, before in provenance_before.items())


@pytest.mark.parametrize("allow_new", [False, True])
def test_frozen_selector_requires_each_catalog_receipt(tmp_path, allow_new):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        published(root, store)
        archive_records(root, store, "wiki-mission")
        pending = pending_record_ids(store, "wiki-mission")
        store.create_task("wiki-mission", "curator", "organize sources", "fixture")
        curator = store.claim_task("wiki-mission")
        directory = root / ".rsi/runtime/tasks" / curator["task_id"]
        manifest_path = build_evidence_bundle(root, store, curator, directory)
        index_path = Path(curator_document_index(root, store, curator, pending_ids=pending))
        register_curator_index(manifest_path, curator)
        catalog = Path(json.loads(index_path.read_text())["records"][0]["documents_manifest"]["path"])
        catalog_raw = catalog.read_bytes()
        catalog.write_bytes(catalog_raw + b" ")
        with pytest.raises(ValueError, match="catalog hash or size mismatch"):
            register_curator_index(manifest_path, curator, allow_new=allow_new)
        catalog.write_bytes(catalog_raw)
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = [item for item in manifest["files"]
                             if item["path"] != catalog.relative_to(manifest_path.parent).as_posix()]
        manifest_path.write_text(json.dumps(manifest))
        before = manifest_path.read_bytes()
        with pytest.raises(ValueError, match="no recorded hash"):
            register_curator_index(manifest_path, curator, allow_new=allow_new)
        assert manifest_path.read_bytes() == before


def test_legacy_frozen_document_index_and_receipt_are_preserved(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("wiki-mission", "curator", "organize sources", "fixture")
        curator = store.claim_task("wiki-mission")
        directory = root / ".rsi/runtime/tasks" / curator["task_id"]
        manifest_path = build_evidence_bundle(root, store, curator, directory)
        index_path = manifest_path.parent / "wiki/curation_sources.json"
        legacy = json.dumps({"schema_version": 1, "mission_id": curator["mission_id"],
                             "curator_task_id": curator["task_id"], "input_version": curator["input_version"],
                             "records": [{"documents": [{"address": "/original/source", "bytes": None}]}]}, indent=2)
        index_path.write_text(legacy)
        register_curator_index(manifest_path, curator)
        receipt = manifest_path.read_bytes()
        assert curator_document_index(root, store, curator, pending_ids=[]) == str(index_path)
        register_curator_index(manifest_path, curator, allow_new=False)
        assert index_path.read_text() == legacy and manifest_path.read_bytes() == receipt
        assert not (index_path.parent / "curation_records").exists()


def test_selector_cannot_register_an_unrelated_private_file(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        published(root, store)
        archive_records(root, store, "wiki-mission")
        store.create_task("wiki-mission", "curator", "organize sources", "fixture")
        curator = store.claim_task("wiki-mission")
        directory = root / ".rsi/runtime/tasks" / curator["task_id"]
        manifest_path = build_evidence_bundle(root, store, curator, directory)
        index_path = Path(curator_document_index(root, store, curator,
                                                pending_ids=pending_record_ids(store, "wiki-mission")))
        selector = json.loads(index_path.read_text())
        selector["records"][0]["documents_manifest"]["path"] = str(manifest_path)
        index_path.write_text(json.dumps(selector))
        before = manifest_path.read_bytes()
        with pytest.raises(ValueError, match="task-scoped record catalog"):
            register_curator_index(manifest_path, curator)
        assert manifest_path.read_bytes() == before


def test_partial_coverage_and_receipt_replay(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        first = published(root, store)
        second = published(root, store, idea="idea-b")
        archive_records(root, store, "wiki-mission")
        original = pending_record_ids(store, "wiki-mission")
        curator = published(root, store, "curator", {"wiki/findings/partial.md": page(first)}, idea=None)
        initial = publish_curator_pages(root, store, curator)
        assert initial["source_event_ids"] == [original[0]]
        assert publish_curator_pages(root, store, curator) == initial
        assert not reconcile_runtime_knowledge(root, store, "wiki-mission")["errors"]
        assert pending_record_ids(store, "wiki-mission") == [original[1]]
        published(root, store, "curator", {"wiki/findings/remainder.md": page(second)}, idea=None)
        reconcile_runtime_knowledge(root, store, "wiki-mission")


@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
def test_partial_wiki_write_recovers_without_losing_acknowledgement(tmp_path, monkeypatch, error):
    from rsi_system.core import runtime_curator
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        published(root, store, "curator", {"wiki/findings/first.md": page(author),
            "wiki/findings/second.md": page(author)}, idea=None)
        replace = runtime_curator._atomic_replace

        def interrupted(path, content):
            if path.name == "second.md":
                raise error("simulated interruption during publication")
            replace(path, content)

        monkeypatch.setattr(runtime_curator, "_atomic_replace", interrupted)
        if error is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                reconcile_runtime_knowledge(root, store, "wiki-mission")
        else:
            assert reconcile_runtime_knowledge(root, store, "wiki-mission")["errors"]
        assert not [event for event in store.events("wiki-mission") if event["kind"] == "knowledge_curation_rejected"]
        assert pending_record_ids(store, "wiki-mission")
        monkeypatch.setattr(runtime_curator, "_atomic_replace", replace)
        assert not reconcile_runtime_knowledge(root, store, "wiki-mission")["errors"]
        assert not pending_record_ids(store, "wiki-mission")
        for name in ("first", "second"):
            assert (root / "knowledge/index.md").read_text().count(f"[[findings/{name}]]") == 1


@pytest.mark.parametrize("change,match", [
    ({"status": "confirmed"}, "evidence status"),
    ({"sources": []}, "traceable sources"),
    ({"sources": ["../outside.txt"]}, "project-relative"),
    ({"link": "findings/absent"}, "unresolved link"),
    ({"sources": ["https://example.org/only-url"]}, "cite pending archived"),
])
def test_invalid_proposal_leaves_wiki_untouched(tmp_path, change, match):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        curator = published(root, store, "curator", {"wiki/findings/invalid.md": page(author, **change)}, idea=None)
        index = (root / "knowledge/index.md").read_text()
        with pytest.raises(ValueError, match=match):
            publish_curator_pages(root, store, curator)
        assert not (root / "knowledge/findings/invalid.md").exists()
        assert (root / "knowledge/index.md").read_text() == index


def test_updates_require_base_and_preserve_raw_sources(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        target = root / "knowledge/findings/existing.md"
        target.write_text(page(author) + "Old text.\n")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        curator = published(root, store, "curator", {"wiki/findings/existing.md": page(author, base=digest)}, idea=None)
        target.write_text(target.read_text() + "Concurrent edit.\n")
        with pytest.raises(ValueError, match="changed since"):
            publish_curator_pages(root, store, curator)
        target.write_text(page(author) + "Old text.\n")
        publish_curator_pages(root, store, curator)
        assert "base_sha256" not in target.read_text()
        raw = root / "knowledge/raw/immutable.md"
        raw.write_text(page(author, kind="source"))
        curator = published(root, store, "curator", {"wiki/raw/immutable.md": page(author, kind="source",
            base=hashlib.sha256(raw.read_bytes()).hexdigest()) + "Replacement.\n"}, idea=None)
        with pytest.raises(ValueError, match="immutable raw"):
            publish_curator_pages(root, store, curator)


def test_confirmed_page_and_symlink_directory_are_protected(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        target = root / "knowledge/findings/current-evidence.md"
        curator = published(root, store, "curator", {"wiki/findings/current-evidence.md": page(author,
            base=hashlib.sha256(target.read_bytes()).hexdigest())}, idea=None)
        with pytest.raises(ValueError, match="confirmed"):
            publish_curator_pages(root, store, curator)
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "knowledge/concepts").symlink_to(outside, target_is_directory=True)
        curator = published(root, store, "curator", {"wiki/concepts/escape.md": page(author, kind="concept")}, idea=None)
        with pytest.raises(ValueError, match="symlink"):
            publish_curator_pages(root, store, curator)
        assert not (outside / "escape.md").exists()


def test_archive_detects_changed_manifest_and_sources(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        archived = root / source(author)
        note = archived.parent / "notes/handoff.md.txt"
        original = note.read_bytes()
        note.write_bytes(b"modified")
        with pytest.raises(ValueError, match="archived record changed"):
            archive_records(root, store, "wiki-mission")
        note.write_bytes(original)
        manifest = root / ".rsi/runtime/tasks" / author["task_id"] / "communications/outbox/manifest.json"
        manifest.write_text(manifest.read_text() + "\n")
        with pytest.raises(ValueError, match="manifest hash"):
            archive_records(root, store, "wiki-mission")


def test_archive_keeps_literal_md_and_md_txt_distinct(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store, files={
            "handoff.md": "markdown source\n",
            "handoff.md.txt": "literal text source\n",
        })
        archive_records(root, store, "wiki-mission")
        directory = root / "knowledge/raw/runtime-records/wiki-mission" / author["task_id"]
        assert (directory / "notes/handoff.md").read_text() == "markdown source\n"
        assert (directory / "notes/handoff.md.txt").read_text() == "literal text source\n"


def test_coordinator_handoffs_are_archived_without_repeated_curation(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        main = published(root, store, "coordinator", idea=None)
        archive_records(root, store, "wiki-mission")
        assert (root / source(main)).is_file()
        assert not pending_record_ids(store, "wiki-mission")


@pytest.mark.parametrize("failure", ["proposal", "model"])
def test_repeated_curation_failures_pause(tmp_path, failure):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        published(root, store)

    def broken(**kwargs):
        if failure == "model":
            raise RuntimeError("provider unavailable")
        return {"payload": {"summary": "missing source-linked pages", "decision": "no_change"}}

    for _ in range(2):
        assert coordinator.tick(require_model=False)["role"] == "curator"
        assert coordinator.tick(agent_runner=broken, require_model=False)["status"] in {"completed", "task_failed"}
    assert coordinator.tick(require_model=False)["status"] == "paused"


def test_rejected_curation_retries_despite_earlier_research_failures(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        for _ in range(2):
            task = store.create_task("wiki-mission", "researcher", "failed exploration", "fixture")
            task = store.claim_task("wiki-mission")
            store.finish_task(task["task_id"], task["lease_token"], {"error": "fixture"}, status="failed")
        archive_records(root, store, "wiki-mission")
        bad_source = source(author).replace("/provenance.json", "/missing-handoff.md.txt")
        published(root, store, "curator", {"wiki/findings/invalid.md": page(author, sources=[bad_source])}, idea=None)
        reconcile_runtime_knowledge(root, store, "wiki-mission")
        assert any(event["kind"] == "knowledge_curation_rejected"
                   for event in store.events("wiki-mission"))

    scheduled = coordinator.tick(require_model=False)
    assert scheduled["status"] == "scheduled"
    assert scheduled["role"] == "curator"


def test_wiki_preflight_checks_editable_yaml_drafts_without_publishing(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        task = store.create_task("wiki-mission", "curator", "check draft", "fixture")
        task = store.claim_task("wiki-mission")
        proposal = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace/wiki/findings/draft.md"
        proposal.parent.mkdir(parents=True)
        proposal.write_text(page(author, kind="source").replace("sources: " + json.dumps([source(author)]),
            "sources:\n  - " + source(author)))
        bridge = ToolBridge(root, task["task_id"])
        invalid = bridge.call({"tool": "rsi_wiki_check", "call_id": "invalid", "args": {}})
        assert invalid["ok"] is False and "type or evidence status" in invalid["error"]["message"]
        proposal.write_text(proposal.read_text().replace("type: source", "type: finding"))
        valid = bridge.call({"tool": "rsi_wiki_check", "call_id": "valid", "args": {}})
        assert valid["ok"] is True
        assert valid["source_event_ids"]
        explained = bridge.call({"tool": "rsi_wiki_check", "call_id": "with-reason", "args": {
            "reason": "Validate new and revised wiki proposals for the body memory reframe and internal reader execution boundary"}})
        assert explained["ok"] is True
        for args in ({"reason": 1}, {"reason": "x" * 4001}, {"path": "/tmp/other"}):
            denied = bridge.call({"tool": "rsi_wiki_check", "call_id": "bad-" + str(len(str(args))), "args": args})
            assert denied["ok"] is False
        assert not (root / "knowledge/findings/draft.md").exists()
        assert _source_paths(_frontmatter(proposal.read_text())) == [source(author)]
        proposal.write_text(proposal.read_text().replace("title: Memory mechanism",
            "title: Memory mechanism: comparison"))
        malformed = bridge.call({"tool": "rsi_wiki_check", "call_id": "malformed", "args": {}})
        assert malformed["ok"] is False and "invalid YAML" in malformed["error"]["message"]


def test_operator_resume_opens_new_bounded_curation_retries(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        archive_records(root, store, "wiki-mission")
        for _ in range(2):
            published(root, store, "curator", {"wiki/findings/bad.md": page(author,
                sources=[source(author).replace("provenance.json", "missing.json")])}, idea=None)
            reconcile_runtime_knowledge(root, store, "wiki-mission")
    assert coordinator.tick(require_model=False)["status"] == "paused"
    with RuntimeStore(coordinator.store_path) as store:
        store.set_mission_status("wiki-mission", "active", reason="reviewed rejection; correct proposal")
    result = coordinator.tick(require_model=False)
    assert result["status"] == "scheduled" and result["role"] == "curator"


def test_real_pi_guard_extension_and_bridge_allow_curator_preflight(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node or not (Path.home() / ".local/share/rsi/pi/node_modules/typebox").exists():
        pytest.skip("Pi Node runtime required")
    deployment = Path(__file__).resolve().parents[3] / "deployment/rsi"
    with tempfile.TemporaryDirectory(prefix="rsi-wiki-") as directory:
        root, coordinator = setup(Path(directory))
        with RuntimeStore(coordinator.store_path) as store:
            author = published(root, store)
            archive_records(root, store, "wiki-mission")
            store.create_task("wiki-mission", "curator", "check", "fixture")
            task = store.claim_task("wiki-mission")
            proposal = root / ".rsi/runtime/tasks" / task["task_id"] / "workspace/wiki/findings/check.md"
            proposal.parent.mkdir(parents=True)
            proposal.write_text(page(author))
        script = tmp_path / "check.mjs"
        script.write_text("import guard from " + json.dumps((deployment / "pi-guard.ts").as_uri()) + ";\n"
            + "import extension from " + json.dumps((deployment / "pi-runtime-tools.ts").as_uri()) + ";\n" + '''
let gate;
guard({on(name, fn) { if(name === 'tool_call') gate = fn; }, registerTool() {}});
let tool;
extension({on() {}, registerTool(value) { if(value.name === 'rsi_wiki_check') tool = value; }});
const { createRequire } = await import('node:module');
const { homedir } = await import('node:os');
const require = createRequire(homedir()+'/.local/share/rsi/pi/node_modules/@earendil-works/pi-coding-agent/package.json');
const { Value } = require('typebox/value');
const args = {reason:'Validate new and revised wiki proposals for the body memory reframe and internal reader execution boundary'};
if (!Value.Check(tool.parameters, args)) throw Error('original arguments rejected');
if (Value.Check(tool.parameters, {path:'/tmp/other'})) throw Error('unknown argument accepted');
if (!process.env.ACTIVE_TOOLS.split(',').includes(tool.name)) throw Error('tool not enabled');
const denied = await gate({toolName:tool.name,input:{}});
if (denied?.block) throw Error(denied.reason);
console.log(JSON.stringify(await tool.execute('preflight', args)));
''')
        with serve_tools(root, task["task_id"]) as (socket, _):
            result = subprocess.run([node, "--experimental-strip-types", str(script)],
                env={**os.environ, "RSI_AGENT_ROOT": str(root), "RSI_AGENT_ROLE": "curator",
                     "RSI_TOOL_SOCKET": str(socket), "ACTIVE_TOOLS": TOOLS["curator"]},
                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)["details"]["ok"] is True
        assert not (root / "knowledge/findings/check.md").exists()


def test_budget_exhaustion_before_curation_pauses(tmp_path):
    root, coordinator = setup(tmp_path, max_agent_tasks=1)
    with RuntimeStore(coordinator.store_path) as store:
        published(root, store)
    assert coordinator.tick(require_model=False)["reason"] == "agent_task_budget_exhausted"


def test_stop_waits_for_curation_and_explicit_dispatch_works_without_auto_flag(tmp_path):
    root, coordinator = setup(tmp_path, knowledge_curator=False)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        curator = published(root, store, "curator", {"wiki/findings/manual.md": page(author)}, idea=None)
        assert not reconcile_runtime_knowledge(root, store, "wiki-mission")["errors"]
        assert not pending_record_ids(store, "wiki-mission")
        assert (root / "knowledge/findings/manual.md").exists()
        receipt = ToolBridge(root, curator["task_id"])._result(store, store.task(curator["task_id"]))
        assert receipt["knowledge_publication"]["status"] == "published"
        store.create_task("wiki-mission", "researcher", "further research", "fixture")
        research = store.claim_task("wiki-mission")
        store.finish_task(research["task_id"], research["lease_token"], {}, status="completed")
    root2, coordinator2 = setup(tmp_path / "other")
    with RuntimeStore(coordinator2.store_path) as store:
        author2 = published(root2, store)
        store.create_task("wiki-mission", "coordinator", "stop", "fixture")
        parent = store.claim_task("wiki-mission")
        coordinator2._handle_coordinator(store, store.mission("wiki-mission"), parent, decision(action="stop"))
        assert store.mission("wiki-mission")["status"] == "active"
        assert store.deferred_stop("wiki-mission")
        assert store.queued_or_running("wiki-mission")
        assert store.list_tasks("wiki-mission")[-1]["role"] == "curator"

    def curate(**kwargs):
        assert kwargs["role"] == "curator"
        directory = Path(kwargs["output_dir"]) / "workspace/wiki/findings"
        directory.mkdir(parents=True)
        (directory / "before-stop.md").write_text(page(author2))
        return {"payload": {"summary": "curated before stop", "decision": "publish"}}

    assert coordinator2.tick(agent_runner=curate, require_model=False)["status"] == "completed"
    assert coordinator2.tick(require_model=False)["status"] == "stopped"


def test_curator_reads_cross_branch_handoffs_and_verified_inventory(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        first = published(root, store, idea="idea-a")
        second = published(root, store, idea="idea-b")
        store.create_task("wiki-mission", "curator", "organize", "fixture")
        curator = store.claim_task("wiki-mission")
        directory = root / ".rsi/runtime/tasks" / curator["task_id"]
        inbox = prepare_communications(store, curator, directory)
        authors = {item["author_task_id"] for item in json.loads(inbox.read_text())["files"]}
        assert {first["task_id"], second["task_id"]} <= authors
        manifest = build_evidence_bundle(root, store, curator, directory)
        assert any(item["path"] == "wiki/inventory.json" for item in json.loads(manifest.read_text())["files"])
    assert model_policy({"models": {"researcher": {"model": "fixture"}}}, "curator")["models"]["curator"]["model"] == "fixture"
    assert "base_sha256" in ROLE_GUIDANCE["curator"]
    assert "write" in TOOLS["curator"] and "bash" not in TOOLS["curator"]
    assert "rsi_wiki_check" in TOOLS["curator"]
    extension = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-runtime-tools.ts"
    assert '"curator"' in extension.read_text()
    assert 'register("rsi_wiki_check"' in extension.read_text()


def test_curator_retry_exposes_frozen_drafts_after_empty_failure(tmp_path):
    from rsi_system.core.runtime_curator import curator_retry_context
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
        failed = []
        for files, error in [({'progress_note.md': 'Read through source 3.',
                              'wiki/findings/retry.md': page(author)}, 'Request timed out.'),
                             ({}, 'curator did not submit Wiki paths with rsi_wiki_submit')]:
            store.create_task('wiki-mission', 'curator', 'organize', 'fixture')
            task = store.claim_task('wiki-mission')
            failed.append(task)
            directory = root / '.rsi/runtime/tasks' / task['task_id']
            for relative, content in files.items():
                target = directory / 'workspace' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            frozen = snapshot_communications(task, directory, {'error': error})
            publish_communications(store, task, frozen, 'failed')
            store.finish_task(task['task_id'], task['lease_token'], {'error': error}, status='failed')
        store.set_mission_status('wiki-mission', 'active', reason='reviewed execution failure')
        task = store.create_task('wiki-mission', 'curator', 'continue', 'fixture')
        directory = root / '.rsi/runtime/tasks' / task['task_id']
        inbox = prepare_communications(store, task, directory)
        retry = curator_retry_context(store, task, inbox)
        assert [x['task_id'] for x in retry['failed_attempts']] == [x['task_id'] for x in failed]
        drafts = retry['drafts_and_progress']
        assert {x['workspace_path'] for x in drafts} == {'progress_note.md', 'wiki/findings/retry.md'}
        assert all(x['task_id'] == failed[0]['task_id'] for x in drafts)
        assert all(Path(x['path']).is_relative_to(inbox.parent) for x in drafts)
        assert all(hashlib.sha256(Path(x['path']).read_bytes()).hexdigest() == x['sha256'] for x in drafts)
        context = curator_prompt_context(coordinator._prompt('curator', store.mission('wiki-mission'), task, store,
                                                             communication_manifest=inbox))
        assert context['curation']['retry'] == retry
        assert not list((directory / 'workspace').rglob('*.md'))
