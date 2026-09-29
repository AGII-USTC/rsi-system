"""Failure-boundary tests: immutable science records, evidence scopes, and recovery."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_evidence import (build_evidence_bundle,
                                                             build_external_readonly_manifest)
from rsi_system.core import runtime_runs
from .test_orchestrator import project
from .test_runtime_acceptance import candidate


def hypothesis(**extra):
    return {"hypothesis_id": "h", "statement": "a testable claim", "mechanism": "a causal mechanism",
            "assumptions": ["controlled intervention"], "predictions": ["lower error"],
            "experimental_design": "compare to unchanged control", "prior_art_status": "unverified",
            "status": "developing", **extra}


def test_revision_history_admission_and_pinned_parent_reason(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "research"))
        store.create_hypothesis("m", hypothesis())
        data = candidate(kind="hypothesis", hypothesis_id="h", hypothesis_version=1)
        with pytest.raises(ValueError, match="candidate_ready"):
            store.create_candidate("m", data)
        with pytest.raises(ValueError, match="expected_version"):
            store.create_hypothesis("m", hypothesis(status="candidate_ready"))
        store.create_hypothesis("m", hypothesis(status="testable", expected_version=1, revision_reason="defined intervention"))
        store.create_hypothesis("m", hypothesis(status="candidate_ready", expected_version=2, revision_reason="design reviewed"))
        c = store.create_candidate("m", {**data, "hypothesis_version": 3})
        store.create_hypothesis("m", hypothesis(status="paused", expected_version=3, revision_reason="contradictory evidence"))
        assert store.hypothesis("h", c["hypothesis_version"])["status"] == "candidate_ready"
        assert store.hypothesis("h")["version"] == 4
        with pytest.raises(ValueError, match="parent_selection_reason"):
            store.create_candidate("m", {**data, "candidate_id": "child", "hypothesis_version": 3, "parent_id": "c0"})
        with pytest.raises(ValueError, match="explicit baseline"):
            store.create_candidate("m", candidate("unlinked", kind="hypothesis"))
        with pytest.raises(ValueError, match="unchanged root"):
            store.create_candidate("m", candidate("fake-baseline", parent_id="c0"))


def test_migration_preserves_legacy_candidate_and_hypothesis(tmp_path):
    path = tmp_path / "old.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "research"))
        store.create_hypothesis("m", hypothesis())
        store.create_candidate("m", candidate())
        store.db.execute("DROP TABLE hypothesis_revisions")
        store.db.execute("ALTER TABLE hypotheses DROP COLUMN version")
        for column in ("hypothesis_version", "kind", "admission_reason"):
            store.db.execute(f"ALTER TABLE candidates DROP COLUMN {column}")
        store.db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        store.db.commit()
    with RuntimeStore(path) as store:
        assert store.candidate("c0")["kind"] == "legacy"
        assert store.hypothesis("h", 1)["statement"] == "a testable claim"
        assert store.mission("m")["payload"]["objective"] == "research"


def make_evidence(root, store):
    store.create_mission(MissionSpec("m", "research"))
    store.create_candidate("m", candidate())
    store.create_candidate("m", candidate("private"))
    artifact = root / ".rsi/runtime/runs/raw"
    output = artifact / "runtime/output"
    output.mkdir(parents=True)
    summary = output / "summary.json"
    summary.write_text('{"measurement": 2}')
    (output / "config.json").write_text('{"seed": 1}')
    (artifact / "secret.sqlite3").write_text("private")
    evidence = {"artifact_dir": str(artifact), "evidence_path": str(summary),
                "evidence_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()}
    run = store.create_run("m", "c0", None, "exploration", "completed", None, {}, evidence)
    private = store.create_run("m", "private", None, "exploration", "failed", None, {}, {"error": "private run"})
    task = store.create_task("m", "critic", "verify raw evidence", "test", candidate_id="c0")
    return task, run, private, artifact


def test_evidence_scoped_to_assigned_candidate_and_detects_corruption(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        task, run, private, artifact = make_evidence(tmp_path, store)
        manifest = build_evidence_bundle(tmp_path, store, task, tmp_path / "bundle")
        data = json.loads(manifest.read_text())
        assert {f["run_id"] for f in data["files"]} == {run["run_id"]}
        assert all("secret.sqlite3" not in f["source"] for f in data["files"])
        assert any(f["verification"].startswith("captured now") for f in data["files"])
        (artifact / "runtime/output/summary.json").write_text('{"measurement": 999}')
        with pytest.raises(ValueError, match="hash mismatch"):
            build_evidence_bundle(tmp_path, store, task, tmp_path / "fresh")
        copied = manifest.parent / data["files"][0]["path"]
        copied.write_text("tampered")
        with pytest.raises(ValueError, match="bundle hash"):
            build_evidence_bundle(tmp_path, store, task, tmp_path / "bundle")


def test_evidence_rejects_symlink_into_control_state(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        task, _, _, artifact = make_evidence(tmp_path, store)
        config = artifact / "runtime/output/config.json"
        config.unlink()
        config.symlink_to(store.path)
        with pytest.raises(ValueError, match="symlink"):
            build_evidence_bundle(tmp_path, store, task, tmp_path / "bundle")


def test_candidate_evidence_never_uses_missing_parent_as_exploration_wildcard(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        unscoped, candidate_run, _, _ = make_evidence(tmp_path, store)
        probes = {}
        for idea in ("assigned", "unrelated"):
            runner = store.create_task("m", "runner", "probe", "test", idea_id=idea)
            probes[idea] = store.create_run("m", None, runner["task_id"], "exploration",
                "completed", None, {}, {"diagnostic": idea}, idea_ref=idea)
        manifest = json.loads(build_evidence_bundle(tmp_path, store, unscoped,
            tmp_path / "unscoped").read_text())
        assert {item["run_id"] for item in manifest["files"]} == {candidate_run["run_id"]}

        reviewer = store.create_task("m", "critic", "review Idea evidence", "test",
                                    candidate_id="c0", idea_id="assigned")
        manifest = json.loads(build_evidence_bundle(tmp_path, store, reviewer,
            tmp_path / "assigned").read_text())
        assert {item["run_id"] for item in manifest["files"]} == {
            candidate_run["run_id"], probes["assigned"]["run_id"]}
        projection = next(item for item in manifest["files"]
            if item["run_id"] == probes["assigned"]["run_id"])
        record = json.loads((tmp_path / "assigned/evidence" / projection["path"]).read_text())
        assert record["kind"] == "exploration" and record["candidate_id"] is None


def expire(store):
    store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE status='running'")
    store.db.commit()


def test_agent_receipt_recovers_without_reinvocation_or_duplicate_revision(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "research", resources={"max_agent_tasks": 1}))
    with RuntimeStore(coordinator.store_path) as store:
        task = store.create_task("m", "hypothesis", "develop", "test")
    original = RuntimeStore.finish_task
    def crash(*args, **kwargs):
        if kwargs.get("status", "completed") == "completed":
            raise KeyboardInterrupt("crash after applying hypothesis, before task commit")
        return original(*args, **kwargs)
    monkeypatch.setattr(RuntimeStore, "finish_task", crash)
    with pytest.raises(KeyboardInterrupt):
        coordinator.tick(agent_runner=lambda **_: {"hypotheses": [hypothesis()]})
    monkeypatch.setattr(RuntimeStore, "finish_task", original)
    with RuntimeStore(coordinator.store_path) as store:
        assert store.list_hypotheses("m") == []
        expire(store)
    def no_model(**_):
        pytest.fail("recovery must not invoke the model")
    result = RuntimeCoordinator(root).tick(agent_runner=no_model)
    assert result["recovered"] and result["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.hypothesis("h")["version"] == 1
        assert store.task(task["task_id"])["status"] == "completed"
        assert store.remaining_agent_tasks("m") == 0


def test_invalid_collaborator_batch_rolls_back_ideas_and_hypotheses(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "research"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "develop", "test")
    result = coordinator.tick(agent_runner=lambda **_: {"ideas": [{"title": "early draft"}],
        "hypotheses": [hypothesis(), hypothesis(hypothesis_id="bad", predictions=[])]})
    assert result["status"] == "task_failed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.list_hypotheses("m") == []
        assert not [e for e in store.events("m") if e["kind"] == "idea_drafted"]
    assert not list((root / "knowledge/queries").glob("runtime-*.md"))


def test_runner_reuses_completed_experiment_after_crash_before_import(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "measure", resources={"max_seconds_per_run": 5},
        authorization={"allow_server_simulation": True}, evaluator={"protocol_id": "runtime-harness-smoke-v1"}))
    with RuntimeStore(coordinator.store_path) as store:
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        store.create_candidate("m", candidate(), code_commit=commit)
        store.update_candidate("c0", status="ready")
        store.create_task("m", "runner", "measure", "test", candidate_id="c0")
    actual = runtime_runs.run_once
    def crash(*args, **kwargs):
        actual(*args, **kwargs)
        raise KeyboardInterrupt("completed experiment, runtime not yet updated")
    monkeypatch.setattr(runtime_runs, "run_once", crash)
    with pytest.raises(KeyboardInterrupt):
        coordinator.tick()
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_runs("m")) == 1
        assert store.list_runs("m")[0]["status"] == "prepared"  # Launch is registered before execution.
        expire(store)
    monkeypatch.setattr(runtime_runs, "run_once", lambda *_, **__: pytest.fail("must not execute twice"))
    result = RuntimeCoordinator(root).tick()
    assert result["recovered"] and result["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_runs("m")) == 1
        assert store.list_runs("m")[0]["status"] == "completed"
    assert len(list((root / ".rsi/runtime/runs").iterdir())) == 1


def test_real_guard_allows_only_manifest_files_and_blocks_recursive_leaks(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    guard = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"
    with RuntimeStore(tmp_path / ".rsi/state.db") as store:
        task, _, _, artifact = make_evidence(tmp_path, store)
        manifest = build_evidence_bundle(tmp_path, store, task, tmp_path / ".rsi/tasks/review")
    source = tmp_path / "experiments"
    source.mkdir()
    (source / "ok.py").write_text("safe")
    (source / "escape").symlink_to(tmp_path / ".rsi/state.db")
    script = '''import guard from GUARD;
let hook; guard({on: (_, fn) => { hook=fn; }});
const checks = JSON.parse(process.env.CHECKS);
for (const [toolName,path,blocked] of checks) {
  const response = await hook({toolName,input:{path}});
  if (Boolean(response?.block) !== blocked) throw Error(JSON.stringify({toolName,path,response,blocked}));
}
'''.replace("GUARD", json.dumps(guard.as_uri()))
    copied = manifest.parent / json.loads(manifest.read_text())["files"][0]["path"]
    checks = [["read", str(manifest), False], ["read", str(copied), False], ["write", str(copied), True],
              ["read", str(artifact / "secret.sqlite3"), True], ["read", str(tmp_path / ".rsi/state.db"), True],
              ["read", str(source / "escape"), True], ["read", "experiments/ok.py", False],
              ["grep", ".", True], ["find", "experiments", True]]
    subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script], check=True,
                   env={**os.environ, "RSI_AGENT_ROOT": str(tmp_path), "RSI_AGENT_ROLE": "reviewer",
                   "RSI_EVIDENCE_MANIFEST": str(manifest), "CHECKS": json.dumps(checks)}, capture_output=True, text=True)


def test_external_readonly_manifest_is_root_scoped_and_does_not_copy_model(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node runtime required for real Pi guard")
    external = tmp_path / "pi05"
    research, model = external / "research", external / "model"
    research.mkdir(parents=True)
    model.mkdir()
    (research / "paper.md").write_text("source")
    task = {"task_id": "review", "mission_id": "m"}
    manifest = build_external_readonly_manifest(task, tmp_path / ".rsi" / "task",
                                                roots=(("research", research), ("model", model)))
    data = json.loads(manifest.read_text())
    assert {item["name"] for item in data["roots"]} == {"research", "model"}
    assert not any(item["path"].endswith("secret.txt") for item in data["roots"])
    guard = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"
    script = '''import guard from GUARD;
let hook; guard({on: (_, fn) => { hook=fn; }});
for (const [toolName,path,blocked] of JSON.parse(process.env.CHECKS)) {
  const response = await hook({toolName,input:{path}});
  if (Boolean(response?.block) !== blocked) throw Error(JSON.stringify({toolName,path,response,blocked}));
}
const {appendFileSync} = await import('node:fs');
appendFileSync(process.env.RSI_EXTERNAL_READONLY_MANIFEST, ' ');
const tampered = await hook({toolName:'read',input:{path:process.env.RSI_EXTERNAL_READONLY_MANIFEST}});
if (!tampered?.block) throw Error('Changed external manifest must be rejected');
'''.replace("GUARD", json.dumps(guard.as_uri()))
    checks = [["read", str(research / "paper.md"), False], ["ls", str(model), False],
              ["write", str(research / "paper.md"), True], ["read", "/etc/passwd", True],
              ["find", str(research), False], ["edit", str(model), True],
              ["read", str(manifest), False], ["write", str(manifest), True],
              ["edit", str(manifest), True]]
    subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script], check=True,
                   env={**os.environ, "RSI_AGENT_ROOT": str(tmp_path), "RSI_AGENT_ROLE": "researcher",
                        "RSI_EXTERNAL_READONLY_MANIFEST": str(manifest), "CHECKS": json.dumps(checks)},
                   capture_output=True, text=True)


def test_configured_external_roots_reach_agent_and_frozen_manifest(tmp_path, monkeypatch):
    root = project(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("RSI_CONFIG_DIR", str(private))
    external = tmp_path / "weights"
    external.mkdir()
    (external / "checkpoint").write_text("large files stay at source")
    (private / "research.json").write_text(json.dumps({
        "external_readonly_roots": [{"name": "weights", "path": str(external)}]}))
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "inspect resources"))
        store.create_task("m", "researcher", "inspect resources", "test")
    manifests = []
    def agent(**kwargs):
        manifest = kwargs["external_manifest"]
        manifests.append(manifest)
        assert json.loads(manifest.read_text())["roots"] == [
            {"name": "weights", "path": str(external), "mode": "read-only"}]
        assert list(manifest.parent.iterdir()) == [manifest]
        return {"summary": "resource manifest inspected"}
    assert coordinator.tick(agent_runner=agent)["status"] == "completed"
    manifest = manifests[0]
    original = manifest.read_bytes()
    identity = json.loads(original)
    build_external_readonly_manifest(identity, manifest.parent.parent, roots=())
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("roots", ["bad", [{"name": "x", "path": "relative"}],
    [{"name": "x", "path": "/data"}, {"name": "y", "path": "/data/nested"}],
    [{"name": "x", "path": "/a"}, {"name": "x", "path": "/b"}]])
def test_external_root_configuration_rejects_ambiguous_scope(tmp_path, roots):
    from rsi_system.core.config import load_research_config
    root = project(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    (private / "research.json").write_text(json.dumps({"external_readonly_roots": roots}))
    with pytest.raises(ValueError, match="external_readonly_roots"):
        load_research_config(root, config_dir=private)


def test_daily_calls_include_failed_invocations_and_pin_model_policy(tmp_path):
    root = project(tmp_path)
    config_path = root / "deployment/rsi/research.json"
    config = json.loads(config_path.read_text())
    config["max_agent_calls_per_day"] = 1
    config_path.write_text(json.dumps(config))
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "research", resources={"max_agent_tasks": 5}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "develop", "test")
    def provider_failure(**_):
        raise ValueError("provider failed")
    assert coordinator.tick(agent_runner=provider_failure)["status"] == "task_failed"
    with RuntimeStore(coordinator.store_path) as store:
        store.resume_mission("m", reason="outage reviewed")
        store.create_task("m", "researcher", "develop", "test")
    result = coordinator.tick(agent_runner=lambda **_: pytest.fail("daily quota must block the call"))
    assert "daily agent invocation budget" in result["error"]
    with RuntimeStore(coordinator.store_path) as store:
        policies = list(store.db.execute("SELECT policy_json FROM runtime_agent_calls"))
        assert len(policies) == 1
        assert json.loads(policies[0][0])["role"] == "researcher"
        assert store.remaining_agent_tasks("m") == 3


def test_run_quota_and_evaluator_binding_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "measure", resources={"max_runs": 1}))
        store.create_candidate("m", candidate())
        store.create_task("m", "runner", "measure", "test", candidate_id="c0")
        digest = store.bind_evaluator("m", {"dataset_version": "fixed", "metric": "error"})
    with RuntimeStore(path) as store:
        with pytest.raises(ValueError, match="run budget"):
            store.create_task("m", "runner", "again", "test", candidate_id="c0")
        assert store.bind_evaluator("m", {"dataset_version": "fixed", "metric": "error"}) == digest
        with pytest.raises(ValueError, match="contract changed"):
            store.bind_evaluator("m", {"dataset_version": "changed", "metric": "error"})


def test_coder_commit_survives_crash_without_second_model_call(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "implement", authorization={"allow_code_candidates": True}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_hypothesis("m", hypothesis(status="candidate_ready"))
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        store.create_candidate("m", candidate(kind="hypothesis", hypothesis_id="h", hypothesis_version=1), code_commit=commit)
        store.create_task("m", "coder", "implement", "test", candidate_id="c0")
    def coding(**kwargs):
        (kwargs["cwd"] / "experiments/residual_memory/memory.py").write_text("CANDIDATE = 2\n")
        return {"summary": "implemented fixture"}
    original = RuntimeStore.finish_task
    monkeypatch.setattr(RuntimeStore, "finish_task", lambda *_, **__: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        coordinator.tick(agent_runner=coding)
    monkeypatch.setattr(RuntimeStore, "finish_task", original)
    with RuntimeStore(coordinator.store_path) as store:
        expire(store)
    worktree = root / ".rsi/runtime/worktrees/c0"
    committed = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
    assert committed != commit
    result = coordinator.tick(agent_runner=lambda **_: pytest.fail("coder must reuse receipt"))
    assert result["recovered"] and result["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.candidate("c0")["code_commit"] == committed
        assert store.candidate("c0")["status"] == "ready"


def test_worker_restores_verified_terminal_file_before_registry_commit(tmp_path, monkeypatch):
    from .test_worker import fixture_repo, queued
    from rsi_system.core.store import ExperimentStore
    from rsi_system.core.worker import run_once
    import sys
    root = fixture_repo(tmp_path)
    with ExperimentStore(tmp_path / "experiment.db") as store:
        queued(store)
        finish = store.finish_run
        monkeypatch.setattr(store, "finish_run", lambda *_, **__: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run_once(store, root, tmp_path / "runs", python=sys.executable)
        monkeypatch.setattr(store, "finish_run", finish)
        assert store.get("test-001")["status"] == "running"
        assert run_once(store, root, tmp_path / "runs", python=sys.executable) is None
        assert store.get("test-001")["status"] == "completed"
        assert len(list((tmp_path / "runs").iterdir())) == 1
