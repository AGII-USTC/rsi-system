import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rsi_system.core import orchestrator
from rsi_system.core.pi_agent import atomic_json, final_payload, run_agent
from rsi_system.core.workspace import create_workspace, inspect_candidate
from .test_worker import fixture_repo
from .test_knowledge import _project

FAKE = '''#!PYTHON
import json, pathlib, sys, time
prompt = sys.stdin.read()
request = json.loads(prompt.split("\\n", 1)[1])
role, context = request["role"], request["context"]
if role == "researcher":
    first = context["first_cycle_requires_unchanged_baseline"]
    payload = {"decision":"experiment", "reason":"bounded fixture experiment", "hypothesis":"fixture",
        "protocol_id":"runtime-harness-smoke-v1", "config":{"steps":3},
        "implementation":"Keep baseline unchanged, then set CANDIDATE to 2", "code_change":not first}
elif role == "coder":
    if context["proposal"]["code_change"]:
        pathlib.Path("experiments/residual_memory/memory.py").write_text("CANDIDATE = 2\\n")
    payload = {"summary":"fixture coding stage"}
else:
    payload = {"verdict":"accept", "summary":"Fixture metrics checked; this is simulated agent output",
        "reasons":["fixed checks passed"], "next_direction":"test next bounded candidate"}
message = {"role":"assistant", "stopReason":"stop", "content":[{"type":"text", "text":json.dumps(payload)}]}
print(json.dumps({"type":"message_end", "message":message}))
'''


def project(tmp_path, cycles=2):
    root = _project(fixture_repo(tmp_path))
    (root / ".gitignore").write_text(".rsi/\n__pycache__/\n.pytest_cache/\n")
    residual = root / "experiments/residual_memory"
    (residual / "tests").mkdir(parents=True)
    (residual / "__init__.py").write_text("")
    (residual / "memory.py").write_text("CANDIDATE = 1\n")
    (residual / "tests/test_core.py").write_text("from experiments.residual_memory.memory import CANDIDATE\ndef test_value(): assert 0 < CANDIDATE < 10\n")
    (root / "experiments/runtime_harness/scheduler.py").write_text("PASS = True\n")
    (root / "experiments/runtime_harness/test_core.py").write_text("def test_fixed(): assert True\n")
    deployment = root / "deployment/rsi"
    deployment.mkdir(parents=True)
    (deployment / "pi-guard.ts").write_text("// fixture guard, fake CLI does not load extensions\n")
    fake = tmp_path / "fake-pi"
    fake.write_text(FAKE.replace("PYTHON", sys.executable, 1))
    fake.chmod(0o755)
    config = {"objective":"fixture research", "pi_executable":str(fake), "python":sys.executable,
              "models":{}, "thinking":"high", "max_cycles":cycles, "max_repairs":2,
              "max_agent_calls_per_day":30, "agent_timeout_seconds":10, "sandbox":False}
    (deployment / "research.json").write_text(json.dumps(config))
    (deployment / "contracts.json").write_text(json.dumps({"runtime-harness-smoke-v1":{
        "dataset_version":"fixture-v1", "evaluator_id":"fixture-v1"}}))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=t@example.org", "commit", "-qm", "orchestration fixture"], check=True)
    return root


def test_two_complete_cycles_have_separate_roles_candidate_commit_and_knowledge(tmp_path):
    root = project(tmp_path)
    initial = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    orchestrator.start(root)
    stages = []
    for _ in range(13):
        result = orchestrator.tick(root, require_model=False)
        assert result["status"] != "error", result
        stages.append(result.get("processed_stage"))
    state = orchestrator.read_state(root)
    assert state["status"] == "completed"
    assert stages[:6] == list(orchestrator.STAGES)
    assert len(state["completed_cycles"]) == 2
    first, second = state["completed_cycles"]
    assert first["source_commit"] == initial
    assert second["source_commit"] != initial
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == initial
    assert "status: candidate" in (root / second["knowledge"]).read_text()
    assert sum(state["calls_by_day"].values()) == 8
    assert second["run"]["source_commit"] == second["source_commit"]


def test_completed_agent_receipt_is_reused_after_crash(tmp_path):
    root = project(tmp_path)
    orchestrator.start(root)
    def crash_after_receipt(**kwargs):
        run_agent(**kwargs)
        raise SystemExit("simulated worker crash")
    with pytest.raises(SystemExit):
        orchestrator.tick(root, runner=crash_after_receipt, require_model=False)
    result = orchestrator.tick(root, runner=lambda **_: pytest.fail("repeated billable call"), require_model=False)
    assert result["next_stage"] == "code"
    assert sum(orchestrator.read_state(root)["calls_by_day"].values()) == 1


def test_daily_budget_waits_without_repeating_agent_calls(tmp_path):
    root = project(tmp_path)
    config_path = root / "deployment/rsi/research.json"
    config = json.loads(config_path.read_text()); config["max_agent_calls_per_day"] = 1
    config_path.write_text(json.dumps(config))
    orchestrator.start(root)
    assert orchestrator.tick(root, require_model=False)["next_stage"] == "code"
    assert orchestrator.tick(root, require_model=False)["status"] == "waiting_budget"
    assert orchestrator.tick(root, require_model=False)["status"] == "waiting_budget"
    assert sum(orchestrator.read_state(root)["calls_by_day"].values()) == 1


def test_pause_during_agent_call_is_honored_after_stage(tmp_path):
    root = project(tmp_path)
    orchestrator.start(root)
    def pausing(**kwargs):
        assert orchestrator.pause(root)["status"] == "pause_requested"
        return run_agent(**kwargs)
    assert orchestrator.tick(root, runner=pausing, require_model=False)["status"] == "paused"
    assert orchestrator.tick(root, require_model=False)["status"] == "paused"
    assert orchestrator.start(root)["cycle"]["stage"] == "code"


def test_new_campaign_archives_paused_state_and_restricts_protocol(tmp_path):
    root = project(tmp_path)
    old = orchestrator.start(root)
    orchestrator.pause(root)
    with pytest.raises(ValueError, match="requires a new campaign"):
        orchestrator.start(root, protocol="runtime-harness-smoke-v1")
    new = orchestrator.start(root, objective="pi05 task", new_campaign=True,
                             protocol="runtime-harness-smoke-v1")
    assert new["campaign_id"] != old["campaign_id"]
    assert new["objective"] == "pi05 task"
    assert set(new["contracts"]) == {"runtime-harness-smoke-v1"}
    archived = json.loads((root / ".rsi/research/archive" / f"{old['campaign_id']}.json").read_text())
    assert archived["status"] == "paused"
    assert orchestrator.read_state(root)["campaign_id"] == new["campaign_id"]


def test_fixed_contract_values_are_implicit_in_research_config(tmp_path):
    root = project(tmp_path)
    contracts = json.loads((root / "deployment/rsi/contracts.json").read_text())
    contracts["runtime-harness-smoke-v1"]["fixed_config"] = {"seed": 3}
    (root / "deployment/rsi/contracts.json").write_text(json.dumps(contracts))
    payload = {"decision": "experiment", "reason": "fixed", "hypothesis": "fixed",
               "protocol_id": "runtime-harness-smoke-v1", "config": {"steps": 3},
               "implementation": "none", "code_change": False}
    assert orchestrator._research(payload, contracts, first=True) == payload


def test_research_preflight_record_carries_cycle_experiment_id(tmp_path, monkeypatch):
    root = project(tmp_path)
    config_path = root / "deployment/rsi/research.json"
    config = json.loads(config_path.read_text())
    config["max_cycles"] = 1
    config_path.write_text(json.dumps(config))
    contracts_path = root / "deployment/rsi/contracts.json"
    contracts_path.write_text(json.dumps({"pi05-libero-gain-switch-index5-v1": {
        "dataset_version": "fixed-v1", "evaluator_id": "fixed-v1"}}))
    captured = {}

    def researcher(**kwargs):
        return {"payload": {
            "decision": "experiment", "reason": "preflight identity",
            "hypothesis": "preflight identity", "protocol_id": "pi05-libero-gain-switch-index5-v1",
            "config": {"condition": "no_memory"}, "implementation": "baseline",
            "code_change": False,
        }}

    def preflight(record, output_dir, python, **kwargs):
        captured.update(record)
        return [str(python), "-c", "pass"]

    monkeypatch.setattr(orchestrator, "build_command", preflight)
    orchestrator.start(root)
    result = orchestrator.tick(root, runner=researcher, require_model=False)
    assert result["status"] == "active"
    assert captured["experiment_id"] == result["cycle_id"]
    assert captured["protocol_id"] == "pi05-libero-gain-switch-index5-v1"


def test_workspace_rejects_evaluator_changes_and_symlinks(tmp_path):
    root = project(tmp_path)
    info = create_workspace(root, root / ".rsi/worktrees", "invalid", "HEAD")
    (info.path / "experiments/runtime_harness/__main__.py").write_text("print('fake metrics')")
    with pytest.raises(ValueError, match="protected"):
        inspect_candidate(info)
    subprocess.run(["git", "-C", str(info.path), "restore", "."], check=True)
    path = info.path / "experiments/residual_memory/memory.py"
    path.unlink(); path.symlink_to(root / "experiments/residual_memory/memory.py")
    with pytest.raises(ValueError, match="regular"):
        inspect_candidate(info)


def test_pi_parser_rejects_error_after_earlier_valid_message(tmp_path):
    path = tmp_path / "events.jsonl"
    good = {"type":"message_end", "message":{"role":"assistant", "stopReason":"stop", "content":[{"type":"text", "text":"{}"}]}}
    bad = {"type":"message_end", "message":{"role":"assistant", "stopReason":"error", "content":[]}}
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad))
    with pytest.raises(ValueError, match="successful"):
        final_payload(path)


def test_pi_parser_preserves_provider_error(tmp_path):
    path = tmp_path / "events.jsonl"
    error = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
        "errorMessage": "aihub API error (503): service unavailable", "content": []}}
    path.write_text(json.dumps(error) + "\n")
    with pytest.raises(ValueError, match=r"aihub API error \(503\)"):
        final_payload(path)


def test_pi_timeout_stops_process(tmp_path):
    fake = tmp_path / "slow-pi"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake.chmod(0o755)
    with pytest.raises(TimeoutError):
        run_agent(executable=str(fake), role="reviewer", prompt="{}", cwd=tmp_path,
                  output_dir=tmp_path / "out", timeout_seconds=1)
    assert not (tmp_path / "out/agent_result.json").exists()


def test_review_repairs_are_bounded_and_rejection_is_written_without_experiment(tmp_path):
    root = project(tmp_path, cycles=1)
    orchestrator.start(root)
    def repair_reviewer(**kwargs):
        result = run_agent(**kwargs)
        if kwargs["role"] == "reviewer":
            result["payload"]["verdict"] = "repair"
            atomic_json(kwargs["output_dir"] / "agent_result.json", result)
        return result
    for _ in range(9):
        result = orchestrator.tick(root, runner=repair_reviewer, require_model=False)
        assert result["status"] != "error", result
    state = orchestrator.read_state(root)
    assert state["status"] == "completed"
    completed = state["completed_cycles"][0]
    assert completed["verdict"] == "reject"
    assert completed["run"] is None
    assert "Repair budget exhausted" in (root / completed["knowledge"]).read_text()
