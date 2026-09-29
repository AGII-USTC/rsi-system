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
    archive_records, pending_record_ids, publish_curator_pages)
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


def page(task, *, status="draft", base="", sources=None, link="findings/current-evidence", kind="finding"):
    return ("---\ntitle: Memory mechanism\ncreated: 2026-09-26\nupdated: 2026-09-26\n"
            f"type: {kind}\nstatus: {status}\ntags: [memory, evidence]\n"
            "sources: " + json.dumps(sources if sources is not None else [source(task)]) + "\n" +
            (f"base_sha256: {base}\n" if base else "") + "---\n\n"
            "# Memory mechanism\n\nHypothesis: response timing may confound apparent memory utility. "
            "No controlled result establishes the effect. Reading depth remains limited. "
            f"Related evidence: [[{link}]].\n")


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
        return {"payload": {"summary": "organized attributed records", "decision": "publish"},
                "tool_protocol_version": 1}

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
        marker = kwargs["prompt"].index('{\n  "role_responsibility"')
        envelope = json.JSONDecoder().raw_decode(kwargs["prompt"][marker:])[0]
        captured["context"] = envelope["context"]
        return {"payload": {"summary": "address book inspected", "decision": "no_change"},
                "tool_protocol_version": 1}

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
    assert any(item["address"].endswith("handoff.md.txt") for item in index["records"][0]["documents"])
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
        marker = kwargs["prompt"].index('{\n  "role_responsibility"')
        context = json.JSONDecoder().raw_decode(kwargs["prompt"][marker:])[0]["context"]
        index = Path(context["curation"]["document_index"])
        manifest = Path(kwargs["evidence_manifest"])
        files = json.loads(manifest.read_text())["files"]
        assert next(item for item in files if item["path"] == "wiki/curation_sources.json")["sha256"] == \
            hashlib.sha256(index.read_bytes()).hexdigest()
        inventory = manifest.parent / "wiki/inventory.json"
        other_private = root / ".rsi/runtime/tasks" / author["task_id"] / "communications/outbox/manifest.json"
        checks = [["read", str(index), False], ["read", str(manifest), False],
                  ["read", str(inventory), False], ["write", str(index), True],
                  ["edit", str(index), True], ["read", str(other_private), True],
                  ["read", str(coordinator.store_path), True],
                  ["read", str(kwargs["output_dir"] / "input.json"), True],
                  ["bash", ".", True]]
        script = '''import guard from GUARD;
import {readFileSync, writeFileSync} from 'node:fs';
let hook; guard({on: (_, fn) => { hook = fn; }});
for (const [toolName, path, blocked] of JSON.parse(process.env.CHECKS)) {
  const response = await hook({toolName, input: {path}});
  if (Boolean(response?.block) !== blocked) throw Error(JSON.stringify({toolName, path, response, blocked}));
  if (!blocked && toolName === 'read') readFileSync(path);
}
const index = process.env.INDEX;
const raw = readFileSync(index);
try {
  writeFileSync(index, Buffer.concat([raw, Buffer.from(' ')]));
  if (!(await hook({toolName: 'read', input: {path: index}}))?.block) throw Error('changed index was readable');
} finally { writeFileSync(index, raw); }
'''.replace("GUARD", json.dumps(guard.as_uri()))
        subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script],
            check=True, capture_output=True, text=True,
            env={**os.environ, "RSI_AGENT_ROOT": str(root), "RSI_AGENT_ROLE": "curator",
                 "RSI_AGENT_WORKSPACE": str(Path(kwargs["output_dir"]) / "workspace"),
                 "RSI_EVIDENCE_MANIFEST": str(manifest), "INDEX": str(index),
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
        return {"payload": {"summary": "verified document access", "decision": "no_change"},
                "tool_protocol_version": 1}

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed", result


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
        assert not pending_record_ids(store, "wiki-mission")


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
guard({on(name, fn) { if(name === 'tool_call') gate = fn; }});
let tool;
extension({on() {}, registerTool(value) { if(value.name === 'rsi_wiki_check') tool = value; }});
if (!process.env.ACTIVE_TOOLS.split(',').includes(tool.name)) throw Error('tool not enabled');
const denied = await gate({toolName:tool.name,input:{}});
if (denied?.block) throw Error(denied.reason);
console.log(JSON.stringify(await tool.execute('preflight', {})));
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
