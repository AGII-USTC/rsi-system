"""Concurrency checks with injected agents, separate connections and real handoffs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier
import sys
import time
from types import SimpleNamespace

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_parallel import execute_queued
from rsi_system.core.runtime_tools import ToolBridge
from rsi_system.core.pi_agent import run_agent
from .test_orchestrator import project
from .test_runtime_acceptance import candidate, decision


def setup(tmp_path, slots=3):
    root = project(tmp_path)
    config_path = root / "deployment/rsi/research.json"
    config = json.loads(config_path.read_text())
    config["runtime_max_parallel_agents"] = slots
    config_path.write_text(json.dumps(config))
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("parallel", "compare independent ideas",
        authorization={"allow_code_candidates": True, "allow_server_simulation": True}))
    return root, coordinator


def test_three_agents_overlap_and_all_publish_without_stale_failures(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        for index in range(3):
            store.create_task("parallel", "researcher", str(index), "independent evidence", idea_id=f"idea-{index}")
    barrier = Barrier(3)

    def agent(**kwargs):
        barrier.wait(timeout=10)
        tid = kwargs["task_id"]
        (Path(kwargs["output_dir"]) / "workspace/handoff.md").write_text(tid)
        return {"payload": {"summary": tid, "ideas": [{"idea_id": tid, "title": tid, "content": "unverified"}]},
                "tool_protocol_version": 1}

    results = execute_queued(coordinator, "parallel", agent_runner=agent, require_model=False)
    assert len(results) == 3 and all(r["status"] == "completed" for r in results)
    with RuntimeStore(coordinator.store_path) as store:
        tasks = store.list_tasks("parallel")
        assert all(t["status"] == "completed" for t in tasks)
        assert len([e for e in store.events("parallel") if e["kind"] == "idea_written_to_wiki"]) == 3
        for task in tasks:
            assert task["task_id"] in (root / "knowledge/index.md").read_text()
            assert (coordinator.runtime_root / "tasks" / task["task_id"] / "communications/outbox/manifest.json").is_file()


def test_concurrent_claims_obey_capacity_and_pause(tmp_path):
    _, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        for _ in range(6):
            store.create_task("parallel", "researcher", "read", "independent")
    barrier = Barrier(6)

    def claim(_):
        with RuntimeStore(coordinator.store_path) as store:
            barrier.wait(timeout=10)
            task = store.claim_task("parallel", max_parallel_agents=3)
            return task["task_id"] if task else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = [tid for tid in pool.map(claim, range(6)) if tid]
    assert len(claimed) == len(set(claimed)) == 3
    with RuntimeStore(coordinator.store_path) as store:
        store.set_mission_status("parallel", "paused", reason="operator changed scope")
        assert store.claim_task("parallel", max_parallel_agents=6) is None
        store.resume_mission("parallel", reason="reviewed")
        with pytest.raises(ValueError, match="stale Mission status"):
            store.validate_task(store.task(claimed[0]))


def test_candidate_writers_are_exclusive_without_blocking_other_candidates(tmp_path):
    _, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        for cid in ("a", "b"):
            store.create_candidate("parallel", candidate(cid))
        first = store.create_task("parallel", "coder", "first", "test", candidate_id="a")
        blocked = store.create_task("parallel", "coder", "same writer", "test", candidate_id="a")
        other = store.create_task("parallel", "coder", "other writer", "test", candidate_id="b")
        assert store.claim_task("parallel", max_parallel_agents=3)["task_id"] == first["task_id"]
        assert store.claim_task("parallel", max_parallel_agents=3)["task_id"] == other["task_id"]
        assert store.claim_task("parallel", max_parallel_agents=3) is None
        assert store.task(blocked["task_id"])["status"] == "queued"


def test_formal_runners_share_one_execution_slot(tmp_path):
    _, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        for _ in range(2):
            store.create_task("parallel", "runner", "probe", "independent", idea_id="idea")
        first = store.claim_task("parallel", max_parallel_agents=3, max_parallel_runs=1)
        assert first["role"] == "runner"
        assert store.claim_task("parallel", max_parallel_agents=3, max_parallel_runs=1) is None
        store.finish_task(first["task_id"], first["lease_token"], {"summary": "complete"})
        assert store.claim_task("parallel", max_parallel_agents=3, max_parallel_runs=1)["role"] == "runner"


def test_coordinator_wait_keeps_pending_collaborators_active(tmp_path):
    _, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("parallel", "coordinator", "organize", "test")
        parent = store.claim_task("parallel")
        child = store.create_task("parallel", "researcher", "explore", "test", parent_task_id=parent["task_id"])
        coordinator._handle_coordinator(store, store.mission("parallel"), parent, decision(action="wait"))
        assert store.mission("parallel")["status"] == "active"
        assert store.claim_task("parallel", max_parallel_agents=3)["task_id"] == child["task_id"]


def test_nested_wait_releases_one_slot_and_recovers_completed_child(tmp_path, monkeypatch):
    root, coordinator = setup(tmp_path, slots=1)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("parallel", "researcher", "parent", "test", idea_id="idea", delegate_budget=2)
        parent = store.claim_task("parallel", max_parallel_agents=1)
    bridge = ToolBridge(root, parent["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "child", "args": {
        "role": "researcher", "objective": "child", "idea_id": "idea", "delegate_budget": 1}})

    def agent(**kwargs):
        if kwargs["role"] == "researcher":
            inner = ToolBridge(root, kwargs["task_id"])
            grandchild = inner.call({"tool": "rsi_delegate", "call_id": "grandchild", "args": {
                "role": "coder", "objective": "prototype", "idea_id": "idea"}})
            observed = inner.call({"tool": "rsi_wait", "call_id": "grandchild-result",
                                   "args": {"task_id": grandchild["task_id"]}})
            assert observed["status"] == "completed"
        return {"payload": {"summary": "done"}, "tool_protocol_version": 1}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", agent)
    observed = bridge.call({"tool": "rsi_wait", "call_id": "child-result", "args": {"task_id": child["task_id"]}})
    assert observed["status"] == "completed"
    assert bridge.call({"tool": "rsi_wait", "call_id": "child-result", "args": {"task_id": child["task_id"]}}) == observed
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_tasks("parallel")) == 3
        assert all(t["status"] == "completed" for t in store.list_tasks("parallel") if t["task_id"] != parent["task_id"])


def test_wait_for_running_task_observes_result_and_rejects_self(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("parallel", "coordinator", "parent", "test")
        parent = store.claim_task("parallel")
        child = store.create_task("parallel", "researcher", "child", "test", parent_task_id=parent["task_id"])
        claimed = store.claim_task("parallel")
    bridge = ToolBridge(root, parent["task_id"])
    rejected = bridge.call({"tool": "rsi_wait", "call_id": "self", "args": {"task_id": parent["task_id"]}})
    assert not rejected["ok"] and "yourself" in rejected["error"]["message"]

    def finish():
        with RuntimeStore(coordinator.store_path) as store:
            store.finish_task(claimed["task_id"], claimed["lease_token"], {"payload": {"summary": "independent result"}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(bridge.call, {"tool": "rsi_wait", "call_id": "already-running", "args": {"task_id": child["task_id"]}})
        pool.submit(finish).result(timeout=10)
        assert waiting.result(timeout=10)["response"]["summary"] == "independent result"


def test_old_tool_server_cannot_use_replacement_lease(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("parallel", "coordinator", "parent", "test")
        owner = store.claim_task("parallel")
    bridge = ToolBridge(root, owner["task_id"])
    with RuntimeStore(coordinator.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_token='replacement' WHERE task_id=?", (owner["task_id"],))
        store.db.commit()
    response = bridge.call({"tool": "rsi_delegate", "call_id": "stale", "args": {"role": "researcher", "objective": "new"}})
    assert not response["ok"] and "lease changed" in response["error"]["message"]
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_tasks("parallel")) == 1


@pytest.mark.parametrize("timeout_seconds", [None, 5])
def test_agent_can_finish_without_a_wall_clock_deadline(tmp_path, monkeypatch, timeout_seconds):
    executable = tmp_path / "fake-pi"
    executable.write_text(f"#!{sys.executable}\n" + '''import json,sys,time
sys.stdin.read()
time.sleep(0.2)
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps({"summary":"finished normally"})}]}}))
''')
    executable.chmod(0o755)
    ticks = iter(range(0, 100000, 2000))
    monkeypatch.setattr("rsi_system.core.pi_agent.time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(ticks), sleep=time.sleep))
    options = {"executable": str(executable), "role": "coordinator", "prompt": "{}", "cwd": tmp_path,
               "output_dir": tmp_path / "receipt", "timeout_seconds": timeout_seconds}
    if timeout_seconds is None:
        assert run_agent(**options)["payload"]["summary"] == "finished normally"
    else:
        with pytest.raises(TimeoutError, match="time budget"):
            run_agent(**options)
