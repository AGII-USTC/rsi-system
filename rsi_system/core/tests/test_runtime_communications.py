"""Communication handoffs are durable, readable and independent of code commits."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_communications import (handoff_file_ref,
    prepare_communications, resolve_handoff_file, snapshot_communications, workspace_path)
from .test_orchestrator import project
from .test_runtime_acceptance import candidate, decision, request
from .test_runtime_hardening import expire


def context(kwargs):
    return json.loads(kwargs["prompt"].split("\n", 1)[1])["context"]


def write_note(kwargs, name, text):
    communications = context(kwargs)["communications"]
    path = Path(communications["workspace"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def inbox(kwargs):
    manifest = Path(context(kwargs)["communications"]["inbox_manifest"])
    data = json.loads(manifest.read_text())
    return [(item, (manifest.parent / item["path"]).read_text()) for item in data["files"]]


def test_research_handoff_reaches_coordinator_and_coder_after_restart(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    with RuntimeStore(runtime.store_path) as store:
        store.create_mission(MissionSpec("m", "exchange research notes", authorization={"allow_code_candidates": True}))
        store.create_candidate("m", candidate())
        research = store.create_task("m", "researcher", "explore alternatives", "open question")
    def researcher(**kwargs):
        write_note(kwargs, "drafts/handoff.md", "Unverified idea: compare mechanisms A and B.")
        write_note(kwargs, "prototypes/sketch.py", "def alternative(): return 'B'\n")
        return {"summary": "two alternatives retained", "decision": "continue"}
    assert runtime.tick(agent_runner=researcher)["status"] == "completed"
    # A fresh runtime must use the published copy, not a mutable author's draft.
    own = runtime.runtime_root / "tasks" / research["task_id"] / "workspace/drafts/handoff.md"
    own.write_text("later local draft is not the published message")
    runtime = RuntimeCoordinator(root)
    assert runtime.tick()["role"] == "coordinator"
    def coordinator(**kwargs):
        notes = inbox(kwargs)
        item, body = next((i, t) for i, t in notes if i["path"].endswith("handoff.md"))
        assert "compare mechanisms A and B" in body
        assert item["author_task_id"] == research["task_id"]
        assert item["author_role"] == "researcher"
        assert item["task_status"] == "completed"
        assert any(i["path"].endswith("sketch.py") for i, _ in notes)
        write_note(kwargs, "handoff.md", f"Coder: read {research['task_id']}/notes/drafts/handoff.md.")
        return decision([request("coder", "c0")])
    assert runtime.tick(agent_runner=coordinator)["status"] == "completed"
    def coder(**kwargs):
        notes = inbox(kwargs)
        assert {i["author_role"] for i, _ in notes} == {"researcher", "coordinator"}
        assert "later local draft" not in "\n".join(t for _, t in notes)
        assert not Path(context(kwargs)["communications"]["workspace"]).is_relative_to(kwargs["cwd"])
        write_note(kwargs, "handoff.md", "Baseline remains unchanged; alternatives retained for later work.")
        return {"summary": "unchanged baseline", "code_change": False}
    result = RuntimeCoordinator(root).tick(agent_runner=coder)
    assert result["status"] == "completed", result
    with RuntimeStore(runtime.store_path) as store:
        publications = [e for e in store.events("m") if e["kind"] == "communication_published"]
        assert [e["payload"]["author_role"] for e in publications] == ["researcher", "coordinator", "coder"]
        assert store.candidate("c0")["status"] == "ready"
        store.create_mission(MissionSpec("other", "separate research"))
        task = store.create_task("other", "researcher", "explore", "new Mission")
        other = prepare_communications(store, task, runtime.runtime_root / "tasks" / task["task_id"])
        assert json.loads(other.read_text())["files"] == []


def test_handoff_snapshot_and_input_survive_receipt_recovery(tmp_path, monkeypatch):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "durable communications"))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "researcher", "investigate", "test recovery")
    original = RuntimeStore.finish_task
    def crash(*args, **kwargs):
        raise KeyboardInterrupt("crash before task commit")
    monkeypatch.setattr(RuntimeStore, "finish_task", crash)
    def researcher(**kwargs):
        write_note(kwargs, "handoff.md", "persist before restart")
        return {"summary": "recover this handoff"}
    with pytest.raises(KeyboardInterrupt):
        runtime.tick(agent_runner=researcher)
    directory = runtime.runtime_root / "tasks" / task["task_id"]
    frozen = (directory / "input.json").read_bytes()
    monkeypatch.setattr(RuntimeStore, "finish_task", original)
    with RuntimeStore(runtime.store_path) as store:
        assert not [e for e in store.events("m") if e["kind"] == "communication_published"]
        expire(store)
    result = RuntimeCoordinator(root).tick(agent_runner=lambda **_: pytest.fail("must reuse model receipt"))
    assert result["recovered"] and result["status"] == "completed"
    assert (directory / "input.json").read_bytes() == frozen
    with RuntimeStore(runtime.store_path) as store:
        publications = [e for e in store.events("m") if e["kind"] == "communication_published"]
        assert len(publications) == 1
        assert store.db.execute("SELECT count(*) FROM runtime_agent_calls").fetchone()[0] == 1


def test_failed_task_notes_remain_visible_as_failed_work(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "preserve unfinished thinking"))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "hypothesis", "develop", "test")
    def invalid(**kwargs):
        write_note(kwargs, "handoff.md", "Need a discriminating prediction before admission.")
        return {"summary": "unfinished", "hypotheses": []}
    assert runtime.tick(agent_runner=invalid)["status"] == "task_failed"
    assert runtime.tick()["role"] == "coordinator"
    def coordinator(**kwargs):
        notes = inbox(kwargs)
        assert notes and all(i["task_status"] == "failed" for i, _ in notes)
        assert any("discriminating prediction" in text for _, text in notes)
        return decision(action="stop")
    assert runtime.tick(agent_runner=coordinator)["status"] == "completed"


def test_frozen_inbox_does_not_gain_new_messages_and_detects_changed_copies(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "consistent input"))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "researcher", "develop", "test")
    assert runtime.tick(agent_runner=lambda **_: {"summary": "original"})["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        next_task = store.create_task("m", "researcher", "continue", "test")
        directory = runtime.runtime_root / "tasks" / next_task["task_id"]
        path = prepare_communications(store, next_task, directory)
        before = path.read_bytes()
        store.event("m", "new_observation", {"summary": "arrived after input snapshot"})
        assert prepare_communications(store, next_task, directory).read_bytes() == before
        item = json.loads(before)["files"][0]
        (path.parent / item["path"]).write_text("changed")
        with pytest.raises(ValueError, match="hash mismatch"):
            prepare_communications(store, next_task, directory)


def test_published_file_id_disambiguates_nested_workspace_paths():
    manifest = {"files": [
        {"origin": "agent_workspace", "path": "notes/prototype.py", "workspace_path": "prototype.py",
         "sha256": "a" * 64, "bytes": 5},
        {"origin": "agent_workspace", "path": "notes/notes/prototype.py",
         "workspace_path": "notes/prototype.py", "sha256": "b" * 64, "bytes": 6},
    ]}
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_handoff_file(manifest, "task-author", path="notes/prototype.py", suffix=".py")
    nested = handoff_file_ref("task-author", manifest["files"][1])
    item, resolved = resolve_handoff_file(manifest, "task-author", file_id=nested["file_id"], suffix=".py")
    assert item is manifest["files"][1] and resolved["workspace_path"] == "notes/prototype.py"
    legacy = {"origin": "agent_workspace", "path": "notes/older.py", "sha256": "c" * 64, "bytes": 7}
    assert workspace_path(legacy) == "older.py"


def test_cloned_repository_handoff_excludes_git_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    clone = workspace / "github-example"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git/config").write_text("remote metadata")
    (clone / "README.md").write_text("useful source")
    manifest = snapshot_communications({"task_id": "t", "mission_id": "m", "role": "coder"},
                                       tmp_path, {"summary": "inspected source"})
    paths = [item["path"] for item in json.loads(manifest.read_text())["files"]]
    assert any(path.endswith("github-example/README.md") for path in paths)
    assert not any(".git" in path for path in paths)


def test_handoff_excludes_generated_node_cache_before_counting_authored_files(tmp_path):
    workspace = tmp_path / "workspace"
    cache = workspace / "node-compile-cache"
    cache.mkdir(parents=True)
    for index in range(260):
        (cache / f"{index:04d}").write_bytes(b"cache")
    (workspace / "probe.py").write_text("print('probe')\n")
    manifest = snapshot_communications({"task_id": "t", "mission_id": "m", "role": "coder"},
                                       tmp_path, {"summary": "probe ready"})
    data = json.loads(manifest.read_text())
    assert {item["workspace_path"] for item in data["files"] if item.get("workspace_path")} == {"probe.py"}
    assert not data["omitted"]


def test_failed_publication_does_not_leave_task_running(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "recover communication failure"))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "researcher", "write", "test")
    def damaged_snapshot(**kwargs):
        # Simulate damaged kernel storage, not an action exposed to an Agent.
        path = kwargs["output_dir"] / "communications/outbox/manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text("invalid JSON")
        return {"summary": "authored response"}
    result = runtime.tick(agent_runner=damaged_snapshot)
    assert result["status"] == "task_failed"
    with RuntimeStore(runtime.store_path) as store:
        assert store.task(task["task_id"])["status"] == "failed"
        assert any(e["kind"] == "communication_publication_failed" for e in store.events("m"))


@pytest.mark.parametrize("role", ["coordinator", "researcher", "hypothesis", "coder", "analyzer", "critic", "reviewer"])
def test_real_guard_all_roles_create_edit_and_read_handoffs(tmp_path, role):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    guard = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "all Agents communicate"))
    with RuntimeStore(runtime.store_path) as store:
        author = store.create_task("m", "researcher", "explore", "test")
    def research(**kwargs):
        write_note(kwargs, "handoff.md", "earlier finding")
        return {"summary": "read my handoff"}
    assert runtime.tick(agent_runner=research)["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "researcher", "respond", "test")
        directory = runtime.runtime_root / "tasks" / task["task_id"]
        manifest = prepare_communications(store, task, directory)
    workspace = directory / "workspace"
    own = workspace / "nested/new/handoff.md"
    received = next(item for item in json.loads(manifest.read_text())["files"] if item["path"].endswith("handoff.md"))
    note = manifest.parent / received["path"]
    receipt = directory / "agent_result.json"
    receipt.write_text('{"immutable":true}')
    script = '''import guard from GUARD;
import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';
import {dirname} from 'node:path';
let hook; guard({registerTool() {}, on:(_,fn)=>{hook=fn;}});
const checks=JSON.parse(process.env.CHECKS);
for(const [toolName,path,blocked] of checks){
  const response=await hook({toolName,input:{path,content:'written by Agent'}});
  if(Boolean(response?.block)!==blocked) throw Error(JSON.stringify({toolName,path,response,blocked}));
  if(!blocked && toolName==='write'){mkdirSync(dirname(path),{recursive:true});writeFileSync(path,'first draft');}
  if(!blocked && toolName==='edit'){writeFileSync(path,'revised draft');}
  if(!blocked && toolName==='read') readFileSync(path,'utf8');
}
'''.replace("GUARD", json.dumps(guard.as_uri()))
    checks = [["read", str(manifest), False], ["read", str(note), False],
              ["write", str(own), False], ["edit", str(own), False], ["read", str(own), False],
              ["write", str(workspace / "prototype.py"), False],
              ["write", str(note), True], ["write", str(receipt), True],
              ["write", str(runtime.runtime_root / "state.sqlite3"), True],
              ["edit", str(runtime.root / "experiments/residual_memory/memory.py"), True]]
    subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script], check=True,
                   env={**os.environ, "RSI_AGENT_ROOT": str(runtime.root), "RSI_AGENT_ROLE": role,
                        "RSI_AGENT_EXPLORATORY": "1",
                        "RSI_AGENT_WORKSPACE": str(workspace), "RSI_COMMUNICATION_MANIFEST": str(manifest),
                        "CHECKS": json.dumps(checks)}, capture_output=True, text=True)
    assert own.read_text() == "revised draft"
    assert note.read_text() == "earlier finding"
    assert json.loads(receipt.read_text()) == {"immutable": True}
