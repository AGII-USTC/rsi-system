"""An early Idea can obtain code, measured feedback and a direct reply."""
import json
from pathlib import Path
import shutil
import sys

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_communications import prepare_communications
from rsi_system.core.runtime_exploration import exploration_run_id
from rsi_system.core.runtime_tools import ToolBridge
from rsi_system.core.pi_agent import tool_receipts
from .test_orchestrator import project
from .test_runtime_acceptance import candidate


def context(kwargs):
    return json.loads(kwargs["prompt"].split("\n", 1)[1])["context"]


@pytest.mark.parametrize("exit_code", [0, 2])
@pytest.mark.parametrize("sandbox", [False, True])
def test_idea_probe_delegates_without_candidate_and_returns_evidence(tmp_path, exit_code, sandbox):
    if sandbox and (sys.platform != "linux" or shutil.which("bwrap") is None):
        pytest.skip("Linux bubblewrap is required")
    runtime = RuntimeCoordinator(project(tmp_path))
    config_path = runtime.root / "deployment/rsi/research.json"
    config = json.loads(config_path.read_text())
    config["sandbox"] = sandbox
    config_path.write_text(json.dumps(config))
    runtime.start(MissionSpec("m", "test a competing explanation",
                              authorization={"allow_code_candidates": True, "allow_server_simulation": True},
                              resources={"max_agent_tasks": 6, "max_runs": 1, "max_seconds_per_run": 5}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "researcher", "explore an Idea", "open question", delegate_budget=4)
    seen = []

    def agent(**kwargs):
        ctx = context(kwargs)
        task = ctx["task"]
        seen.append(task["role"])
        workspace = Path(ctx["communications"]["workspace"])
        if task["role"] == "researcher" and not ctx["idea_runs"]:
            (workspace / "handoff.md").write_text("Need a discriminating probe for idea-a.")
            return {"summary": "uncertain mechanism", "ideas": [{"idea_id": "idea-a", "title": "A", "content": "Test A"}],
                    "messages": [{"idea_id": "idea-a", "to_role": "coder", "text": "Probe the failure condition."},
                                 {"to_role": "coordinator", "text": "A probe may resolve this unknown."}],
                    "delegate_tasks": [{"role": "coder", "idea_id": "idea-a", "delegate_budget": 2,
                                        "objective": "Write a small probe", "reason": "Need observed result"}]}
        if task["role"] == "coder":
            inbox = Path(ctx["communications"]["inbox_manifest"])
            files = json.loads(inbox.read_text())["files"]
            assert any(item.get("origin") == "direct_message" and item.get("idea_id") == "idea-a" for item in files)
            (workspace / "probe.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['RSI_EXPLORATION_OUTPUT'], 'result.json').write_text('{\"observed\": true}')\n"
                f"print('observed probe result')\nraise SystemExit({exit_code})\n")
            return {"summary": "prototype only", "code_change": False,
                    "messages": [{"to_role": "researcher", "text": "The probe tests the failure condition."}],
                    "delegate_tasks": [{"role": "runner", "delegate_budget": 0,
                                        "objective": "Measure probe",
                                        "experiment": {"source_task_id": task["task_id"], "script": "probe.py",
                                                       "question": "Does A survive?", "baseline": "unchanged fixture",
                                                       "expected_difference": "A differs", "falsifier": "no difference",
                                                       "source_reason": "generated minimal probe",
                                                       "review_roles": ["analyzer"]}}]}
        assert task["role"] in {"researcher", "analyzer"}
        assert len(ctx["idea_runs"]) == 1
        assert ctx["idea_runs"][0]["status"] == ("completed" if exit_code == 0 else "failed")
        manifest = Path(ctx["evidence_manifest"])
        files = json.loads(manifest.read_text())["files"]
        assert any(item["path"].endswith("stdout.log") for item in files)
        assert any(item["path"].endswith("output/result.json") for item in files)
        inbox = Path(ctx["communications"]["inbox_manifest"])
        if task["role"] == "researcher":
            assert any(item.get("origin") == "direct_message" for item in json.loads(inbox.read_text())["files"])
        return {"summary": "probe evidence does not confirm a Candidate", "decision": "continue"}

    assert runtime.tick(agent_runner=agent)["role"] == "researcher"
    assert runtime.tick(agent_runner=agent)["role"] == "coder"
    run_result = runtime.tick(agent_runner=agent)
    assert run_result["role"] == "runner" and run_result["status"] == "completed", run_result
    followup = runtime.tick(agent_runner=agent)
    assert followup["status"] == "scheduled" and followup["role"] == "coordinator", followup
    assert seen == ["researcher", "coder"]
    with RuntimeStore(runtime.store_path) as store:
        assert store.list_candidates("m") == []
        run = store.list_runs("m")[0]
        assert run["candidate_id"] is None and run["idea_ref"] == "idea-a"
        assert run["kind"] == "exploration"
        assert len([task for task in store.list_tasks("m") if task["role"] == "runner"]) == 1
        assert not [task for task in store.list_tasks("m") if task["role"] in {"analyzer", "critic"}]
        assert run["evidence"]["execution_status"] == ("completed" if exit_code == 0 else "failed_during_program")
        assert any(event["kind"] == "direct_message" and "Runner finished Run" in event["payload"]["text"]
                   for event in store.events("m"))


def test_runner_returns_code_contract_failure_to_coder(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "repair a broken probe",
                              authorization={"allow_code_candidates": True, "allow_server_simulation": True},
                              resources={"max_agent_tasks": 8, "max_runs": 1, "max_seconds_per_run": 5}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task("m", "coder", "write a broken probe", "test", idea_id="idea-a")

    def author(**kwargs):
        workspace = Path(context(kwargs)["communications"]["workspace"])
        (workspace / "probe.py").write_text("import experiments.this_module_does_not_exist\\n")
        (workspace / "implementation_note.md").write_text("entrypoint=probe.py\\nself_test=python -I probe.py\\n")
        return {"summary": "broken prototype", "code_change": False}

    assert runtime.tick(agent_runner=author)["status"] == "completed"
    description = {"source_task_id": source["task_id"], "script": "probe.py", "question": "Does it run?",
                   "baseline": "unchanged", "expected_difference": "prints", "falsifier": "no output",
                   "source_reason": "generated fixture"}
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "runner", "run broken probe", "test", idea_id="idea-a",
                          experiment=description)
    result = runtime.tick()
    assert result["role"] == "runner" and result["status"] == "completed", result
    with RuntimeStore(runtime.store_path) as store:
        run = store.list_runs("m")[0]
        assert run["evidence"]["execution_status"] == "preflight_failed"
        assert run["evidence"]["failure_class"] == "code_contract"
        repairs = [item for item in store.list_tasks("m")
                    if item["role"] == "coder" and item["scope"]["reply_to_task_id"] == source["task_id"]]
        assert len(repairs) == 1
        events = store.events("m")
        assert any(event["kind"] == "runner_repair_requested" for event in events)
        messages = [event for event in events if event["kind"] == "direct_message"]
        assert any(repairs[0]["task_id"] in event["payload"]["text"] for event in messages)


def test_runner_bootstrap_imports_frozen_project_source(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "run an importable probe",
                              authorization={"allow_server_simulation": True},
                              resources={"max_runs": 1, "max_seconds_per_run": 5}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task("m", "researcher", "write import probe", "test", idea_id="idea-a")

    def author(**kwargs):
        workspace = Path(context(kwargs)["communications"]["workspace"])
        (workspace / "probe.py").write_text(
            "from experiments.residual_memory.memory import CANDIDATE\n"
            "from pathlib import Path\nimport os\n"
            "Path(os.environ['RSI_EXPLORATION_OUTPUT'], 'result.json').write_text(str(CANDIDATE))\n")
        return {"summary": "importable probe"}

    assert runtime.tick(agent_runner=author)["status"] == "completed"
    description = {"source_task_id": source["task_id"], "script": "probe.py", "question": "Does it import?",
                   "baseline": "no import", "expected_difference": "result file", "falsifier": "import fails",
                   "source_reason": "bootstrap fixture"}
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "runner", "run import probe", "test", idea_id="idea-a",
                          experiment=description)
    assert runtime.tick()["role"] == "runner"
    with RuntimeStore(runtime.store_path) as store:
        run = store.list_runs("m")[0]
        assert run["status"] == "completed"
        assert run["evidence"]["measurement_status"] == "complete"


def test_explicit_mission_budget_rolls_back_every_requested_task(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "bounded collaboration", resources={"max_agent_tasks": 2}))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "researcher", "start", "test", delegate_budget=1)
    def oversized(**kwargs):
        return {"summary": "ask for two", "delegate_tasks": [
            {"role": "hypothesis", "idea_id": "idea-a", "objective": "first", "reason": "test"},
            {"role": "critic", "idea_id": "idea-a", "objective": "second", "reason": "test"}]}
    assert runtime.tick(agent_runner=oversized)["status"] == "task_failed"
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.list_tasks("m")) == 1
        assert store.task(task["task_id"])["status"] == "failed"


def test_legacy_result_can_delegate_more_than_six_tasks_without_allocation(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "unlimited collaboration"))
    with RuntimeStore(runtime.store_path) as store:
        parent = store.create_task("m", "researcher", "start", "test", delegate_budget=0)

    def delegate_many(**kwargs):
        return {"summary": "independent questions", "delegate_tasks": [
            {"role": "hypothesis", "idea_id": "idea-a", "objective": f"question {index}", "reason": "test"}
            for index in range(8)]}

    assert runtime.tick(agent_runner=delegate_many)["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        children = [task for task in store.list_tasks("m") if task["scope"]["parent_task_id"] == parent["task_id"]]
        assert len(children) == 8
        assert all(task["scope"]["delegate_budget"] is None for task in children)


def test_probe_accepts_published_handoff_path(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "run a published probe",
                              authorization={"allow_server_simulation": True},
                              resources={"max_runs": 1, "max_seconds_per_run": 5}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task("m", "researcher", "write probe", "test", idea_id="idea-a")

    def author(**kwargs):
        workspace = Path(context(kwargs)["communications"]["workspace"])
        (workspace / "prototype.py").write_text("print('published probe')\n")
        return {"summary": "probe ready"}

    assert runtime.tick(agent_runner=author)["status"] == "completed"
    description = {"source_task_id": source["task_id"], "script": "notes/prototype.py",
                   "question": "Does it run?", "baseline": "unchanged",
                   "expected_difference": "prints a result", "falsifier": "no output",
                   "source_reason": "authored in the source task"}
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "runner", "measure", "test", idea_id="idea-a", experiment=description)
    result = runtime.tick()
    assert result["role"] == "runner" and result["status"] == "completed", result
    with RuntimeStore(runtime.store_path) as store:
        assert store.list_runs("m")[0]["status"] == "completed"


def test_direct_message_stays_in_its_idea_branch(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "separate branches"))
    with RuntimeStore(runtime.store_path) as store:
        author = store.create_task("m", "researcher", "A", "test", idea_id="idea-a")
    result = runtime.tick(agent_runner=lambda **_: {"summary": "A", "messages": [
        {"to_role": "coder", "text": "Only A should see this."}]})
    assert result["status"] == "completed", result
    with RuntimeStore(runtime.store_path) as store:
        task_a = store.create_task("m", "coder", "A", "test", idea_id="idea-a")
        task_b = store.create_task("m", "coder", "B", "test", idea_id="idea-b")
        inbox_a = prepare_communications(store, task_a, runtime.runtime_root / "tasks" / task_a["task_id"])
        inbox_b = prepare_communications(store, task_b, runtime.runtime_root / "tasks" / task_b["task_id"])
        assert any(item.get("origin") == "direct_message" for item in json.loads(inbox_a.read_text())["files"])
        assert json.loads(inbox_b.read_text())["files"] == []


def test_v2_run_rows_survive_nullable_candidate_migration(tmp_path):
    path = tmp_path / "state.sqlite3"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "preserve older Runs"))
        store.create_candidate("m", candidate())
        old = store.create_run("m", "c0", None, "exploration", "completed", None, {}, {"metric": 1})
        store.db.execute("DROP INDEX runs_task_once")
        store.db.execute("""CREATE TABLE runs_old(
            run_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
            candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id), task_id TEXT,
            purpose TEXT NOT NULL, status TEXT NOT NULL, source_commit TEXT, input_json TEXT NOT NULL,
            evidence_json TEXT, evidence_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        store.db.execute("""INSERT INTO runs_old SELECT run_id,mission_id,candidate_id,task_id,purpose,status,
            source_commit,input_json,evidence_json,evidence_sha256,created_at,updated_at FROM runs""")
        store.db.execute("DROP TABLE runs")
        store.db.execute("ALTER TABLE runs_old RENAME TO runs")
        store.db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        store.db.commit()
    with RuntimeStore(path) as store:
        assert store.run(old["run_id"])["candidate_id"] == "c0"
        assert store.run(old["run_id"])["evidence"] == {"metric": 1}
        assert store.db.execute("PRAGMA table_info(runs)").fetchall()[2][3] == 0
        assert store.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "4"


def test_probe_receipt_recovered_without_rerunning_script(tmp_path, monkeypatch):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "recover a finished probe",
                              authorization={"allow_server_simulation": True}, resources={"max_runs": 1}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task("m", "researcher", "write probe", "test", idea_id="idea-a")
    def author(**kwargs):
        workspace = Path(context(kwargs)["communications"]["workspace"])
        (workspace / "probe.py").write_text("print('once')\n")
        return {"summary": "ready"}
    assert runtime.tick(agent_runner=author)["status"] == "completed"
    description = {"source_task_id": source["task_id"], "script": "probe.py", "question": "Does it run?",
                   "baseline": "unchanged", "expected_difference": "prints once", "falsifier": "no output",
                   "source_reason": "generated fixture"}
    with RuntimeStore(runtime.store_path) as store:
        runner = store.create_task("m", "runner", "measure", "test", idea_id="idea-a", experiment=description)
    original = RuntimeStore.finish_task
    def crash(*args, **kwargs):
        if args[1] == runner["task_id"]:
            raise KeyboardInterrupt("crash after process exit")
        return original(*args, **kwargs)
    monkeypatch.setattr(RuntimeStore, "finish_task", crash)
    with pytest.raises(KeyboardInterrupt):
        runtime.tick()
    receipt = runtime.runtime_root / "runs" / exploration_run_id(runner["task_id"]) / "run_record.json"
    before = receipt.read_bytes()
    monkeypatch.setattr(RuntimeStore, "finish_task", original)
    with RuntimeStore(runtime.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                         (runner["task_id"],))
        store.db.commit()
    assert RuntimeCoordinator(runtime.root).tick()["recovered"] is True
    assert receipt.read_bytes() == before
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.list_runs("m")) == 1


def test_identical_exploration_requires_explicit_recheck_reason(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "avoid duplicate probes",
                              authorization={"allow_code_candidates": True, "allow_server_simulation": True},
                              resources={"max_agent_tasks": 8, "max_runs": 3}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task("m", "coder", "publish a probe", "test", idea_id="idea-a")

    def author(**kwargs):
        workspace = Path(context(kwargs)["communications"]["workspace"])
        (workspace / "probe.py").write_text("print('probe')\n")
        return {"summary": "probe ready", "code_change": False}

    assert runtime.tick(agent_runner=author)["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        parent = store.create_task("m", "coordinator", "queue probes", "test", idea_id="idea-a")
        parent = store.claim_task("m")
    bridge = ToolBridge(runtime.root, parent["task_id"])
    experiment = {"source_task_id": source["task_id"], "script": "probe.py",
                  "question": "Does it run?", "baseline": "no probe", "expected_difference": "output",
                  "falsifier": "no output", "source_reason": "duplicate regression"}
    first = bridge.call({"tool": "rsi_run_probe", "call_id": "first", "args": {
        "objective": "Run probe", "idea_id": "idea-a", "experiment": experiment}})
    assert first["ok"], first
    duplicate = bridge.call({"tool": "rsi_run_probe", "call_id": "duplicate", "args": {
        "objective": "Run probe again", "idea_id": "idea-a", "experiment": experiment}})
    assert not duplicate["ok"] and "recheck_reason" in duplicate["error"]["message"]
    repeat = {**experiment, "recheck_reason": "Verify the first receipt after an external interruption"}
    allowed = bridge.call({"tool": "rsi_run_probe", "call_id": "intentional-repeat", "args": {
        "objective": "Replay probe", "idea_id": "idea-a", "experiment": repeat}})
    assert allowed["ok"], allowed
    with RuntimeStore(runtime.store_path) as store:
        events = [event for event in store.events("m") if event["kind"] == "exploration_duplicate_recheck"]
        assert len(events) == 1 and events[0]["payload"]["recheck_reason"].startswith("Verify")


def test_retryable_model_failure_queues_one_bounded_continuation(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "recover provider outage", resources={"max_agent_tasks": 4}))
    with RuntimeStore(runtime.store_path) as store:
        original = store.create_task("m", "researcher", "investigate", "test")
    attempts = []

    def agent(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("provider unavailable")
        return {"summary": "recovered", "decision": "wait"}

    first = runtime.tick(agent_runner=agent)
    assert first["status"] == "task_failed"
    with RuntimeStore(runtime.store_path) as store:
        queued = [item for item in store.list_tasks("m") if item["status"] == "queued"]
        assert len(queued) == 1 and queued[0]["scope"]["reply_to_task_id"] == original["task_id"]
        assert any(event["kind"] == "model_task_continued" for event in store.events("m"))
    second = runtime.tick(agent_runner=agent)
    assert second["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        assert len([item for item in store.list_tasks("m") if item["role"] == "researcher"]) == 2


def test_tool_audit_uses_execution_status_not_agent_claim(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(item) for item in [
        {"type": "tool_execution_end", "toolName": "web_search", "isError": True},
        {"type": "tool_execution_end", "toolName": "web_search", "isError": False,
         "result": {"details": {"error": "provider failed"}}},
        {"type": "tool_execution_end", "toolName": "fetch_content", "isError": False,
         "result": {"details": {}}},
    ]))
    assert tool_receipts(events) == [
        {"tool": "web_search", "succeeded": False},
        {"tool": "web_search", "succeeded": False},
        {"tool": "fetch_content", "succeeded": True},
    ]
