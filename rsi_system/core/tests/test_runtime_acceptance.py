"""Regression evidence for the coordinator; injected agents are not scientific validation."""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_knowledge import reconcile_runtime_knowledge
from .test_orchestrator import project


def decision(tasks=(), action="schedule"):
    return {"decision": action, "summary": "Audit fixture decision",
            "tasks": list(tasks), "expected_information": "Test the next mechanism",
            "budget_impact": "One bounded task", "stop_condition": "After evidence is recorded"}


def request(role="researcher", candidate_id=None):
    return {"role": role, "objective": "Investigate fixture", "reason": "Resolve a known question",
            "candidate_id": candidate_id}


def candidate(identifier="c0", **extra):
    return {"candidate_id": identifier, "hypothesis": "fixture mechanism",
            "kind": "baseline", "baseline_reason": "unchanged fixture reference", "expected_observation": "fixture measurement",
            "protocol_id": "runtime-harness-smoke-v1", "config": {"steps": 3}, **extra}


def test_invalid_dispatch_batch_has_no_partial_effects(tmp_path):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit"))
        store.create_task("m", "coordinator", "choose", "audit")
        task = store.claim_task("m")
        original = store.mission("m")
        with pytest.raises(ValueError, match="registered collaborator"):
            coordinator._handle_coordinator(store, original, task,
                                             decision([request(), request("unregistered")]))
        assert len(store.list_tasks("m")) == 1
        assert store.mission("m")["version"] == original["version"]


def test_coordinator_commit_is_atomic_and_rejects_replay(tmp_path):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit"))
        store.create_task("m", "coordinator", "choose", "audit")
        task = store.claim_task("m")
        mission = store.mission("m")
        payload = decision([request(), request("hypothesis")])
        coordinator._handle_coordinator(store, mission, task, payload)
        assert store.task(task["task_id"])["status"] == "completed"
        assert len(store.list_tasks("m")) == 3
        saved = store.events("m")[-1]["payload"]
        assert saved["base_state_version"] == mission["version"]
        assert len(saved["scheduled_task_ids"]) == 2
        with pytest.raises(ValueError, match="lease"):
            coordinator._handle_coordinator(store, mission, task, payload)
        assert len(store.list_tasks("m")) == 3


def test_pausing_while_coordinator_thinks_rejects_late_schedule(tmp_path):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit"))
        store.create_task("m", "coordinator", "choose", "audit")
        task = store.claim_task("m")
        mission = store.mission("m")
        store.set_mission_status("m", "paused", reason="operator intervened")
        with pytest.raises(ValueError, match="stale"):
            coordinator._handle_coordinator(store, mission, task, decision([request()]))
        assert len(store.list_tasks("m")) == 1
        assert store.mission("m")["status"] == "paused"


def test_dispatch_budget_includes_failed_and_cancelled_calls(tmp_path):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit", resources={"max_agent_tasks": 2}))
        store.create_task("m", "researcher", "read", "audit")
        old = store.claim_task("m")
        store.finish_task(old["task_id"], old["lease_token"], {"error": "provider failure"}, status="failed")
        store.resume_mission("m", reason="reviewed failure")
        store.create_task("m", "coordinator", "choose", "audit")
        task = store.claim_task("m")
        with pytest.raises(ValueError, match="budget"):
            coordinator._handle_coordinator(store, store.mission("m"), task, decision([request()]))
        assert len(store.list_tasks("m")) == 2
        assert store.remaining_agent_tasks("m") == 0


def test_concurrent_task_creation_reserves_budget_once(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "audit", resources={"max_agent_tasks": 1}))
    barrier = Barrier(2)

    def create():
        with RuntimeStore(path) as store:
            barrier.wait(timeout=10)
            try:
                store.create_task("m", "researcher", "read", "audit")
                return "created"
            except ValueError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results.count("created") == 1
    assert any("budget" in result for result in results)


def test_expired_task_pauses_for_reconciliation_instead_of_waiting_forever(tmp_path):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit"))
        store.create_task("m", "researcher", "read", "audit")
        task = store.claim_task("m")
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00'")
        store.db.commit()
    result = coordinator.tick(require_model=False)
    assert result["reason"] == "expired_task_lease"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("m")["status"] == "paused"
        assert store.task(task["task_id"])["status"] == "failed"
        with pytest.raises(ValueError, match="lease"):
            store.finish_task(task["task_id"], task["lease_token"], {"summary": "late success"})


def test_descendant_uses_parent_commit_and_rejects_cross_mission_parent(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        mission = store.create_mission(MissionSpec("m", "audit"))
        store.create_mission(MissionSpec("other", "other"))
        original = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        store.create_candidate("m", candidate("parent"), code_commit=original)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.org", "commit", "--allow-empty", "-qm", "new deployment"], check=True)
        h = store.create_hypothesis("m", {"hypothesis_id": "child-h", "statement": "fixture claim",
            "mechanism": "fixture", "assumptions": ["fixed fixture"], "predictions": ["improvement"],
            "experimental_design": "compare to parent", "prior_art_status": "fixture", "status": "candidate_ready"})
        child = coordinator._register_candidate(store, mission, candidate("child", parent_id="parent", kind="hypothesis",
            hypothesis_id=h["hypothesis_id"], hypothesis_version=1, parent_selection_reason="parent fixture evidence"))
        assert child["code_commit"] == original
        with pytest.raises(ValueError, match="Mission"):
            store.create_candidate("other", candidate("illegal", parent_id="parent"))


@pytest.mark.parametrize("authorization,evaluator,error", [
    ({}, {"protocol_id": "runtime-harness-smoke-v1"}, "authorize"),
    ({"allow_server_simulation": True}, {"protocol_id": "other"}, "evaluator"),
])
def test_runner_checks_mission_authorization_and_protocol(tmp_path, authorization, evaluator, error):
    coordinator = RuntimeCoordinator(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        mission = store.create_mission(MissionSpec("m", "audit", authorization=authorization, evaluator=evaluator))
        c = store.create_candidate("m", candidate(), code_commit="a" * 40)
        c = store.update_candidate(c["candidate_id"], status="ready")
        with pytest.raises(ValueError, match=error):
            coordinator._execute_runner(store, mission, {"candidate_id": c["candidate_id"]})


def test_run_writeback_retries_without_duplicates_and_rejects_damaged_evidence(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "audit"))
        store.create_candidate("m", candidate())
        run = store.create_run("m", "c0", None, "exploration", "failed", None, {}, {"error": "negative fixture"})
        first = reconcile_runtime_knowledge(root, store, "m")
        assert len(first["written"]) == 1 and not first["errors"]
        page = root / first["written"][0]["path"]
        assert run["run_id"] in page.read_text()
        assert "status: candidate" in page.read_text()
        assert reconcile_runtime_knowledge(root, store, "m") == {"written": [], "errors": []}
        damaged = store.create_run("m", "c0", None, "exploration", "failed", None, {}, {"error": "another fixture"})
        store.db.execute("UPDATE runs SET evidence_sha256=? WHERE run_id=?", ("0" * 64, damaged["run_id"]))
        store.db.commit()
        result = reconcile_runtime_knowledge(root, store, "m")
        assert "hash mismatch" in result["errors"][0]["error"]
        assert len(list((root / "knowledge/findings").glob("run-*.md"))) == 1


def test_three_candidate_feedback_loop_with_real_worktrees_and_fixture_evaluator(tmp_path):
    """Runs real subprocesses; LLM decisions and the measurement task are controlled fixtures."""
    root = project(tmp_path)
    evaluator_path = root / "experiments/runtime_harness/__main__.py"
    evaluator_path.write_text(evaluator_path.read_text().replace(
        '"waypoint_distance_m": 0.4', '"waypoint_distance_m": 0.4 / __import__("experiments.residual_memory.memory", fromlist=["CANDIDATE"]).CANDIDATE'))
    subprocess.run(["git", "-C", str(root), "add", str(evaluator_path)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                    "user.email=test@example.org", "commit", "-qm", "fixed fixture evaluator"], check=True)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("loop", "audit feedback loop", resources={"max_agent_tasks": 30, "max_seconds_per_run": 5},
                                 authorization={"allow_code_candidates": True, "allow_server_simulation": True},
                                 evaluator={"protocol_id": "runtime-harness-smoke-v1"}))

    def agent(**kwargs):
        context = json.loads(kwargs["prompt"].split("\n", 1)[1])["context"]
        role, candidates = context["task"]["role"], context["candidates"]
        if role == "coordinator":
            if not candidates:
                return decision([request()])
            last = candidates[-1]
            if last["status"] == "analyzed":
                return decision([request()]) if len(candidates) < 3 else decision(action="stop")
            role = {"proposed": "coder", "ready": "runner", "running": "analyzer"}[last["status"]]
            return decision([request(role, last["candidate_id"])])
        if role == "researcher":
            index = len(candidates)
            raw = {"hypothesis_id": f"h{index}", "candidate_id": f"c{index}", "statement": "fixture claim",
                   "mechanism": "increase the fixture value", "assumptions": ["fixed fixture evaluator"],
                   "predictions": ["lower fixture distance"], "experimental_design": {"control": "c0"},
                   "prior_art_status": {"status": "fixture"}, "status": "candidate_ready",
                   "protocol_id": "runtime-harness-smoke-v1", "config": {"steps": 3},
                   "parent_id": candidates[-1]["candidate_id"] if candidates else None,
                   "parent_selection_reason": "measured fixture distance improved in previous Run"}
            if index:
                assert context["runs"][-1]["evidence"]["metrics"]["waypoint_distance_m"] > 0
                assert list((root / "knowledge/findings").glob("run-*.md"))
            return {"decision": "continue", "summary": "fixture proposal", "hypotheses": [raw],
                    "ideas": [{"idea_id": f"open{index}", "title": "Unfinished alternate mechanism", "content": "Still untested"}]}
        if role == "coder":
            index = int(context["candidate"]["candidate_id"][1:])
            path = kwargs["cwd"] / "experiments/residual_memory/memory.py"
            if index == 2:
                assert "CANDIDATE = 2" in path.read_text()
            if index:
                path.write_text(f"CANDIDATE = {index + 1}\n")
            return {"summary": "fixture implementation", "code_change": bool(index)}
        assert role == "analyzer"
        return {"summary": "fixture measurement checked", "observations": [context["latest_run"]["evidence"]["metrics"]],
                "recommendation": "continue"}

    for _ in range(50):
        result = coordinator.tick(agent_runner=agent, require_model=False)
        assert result["status"] not in {"task_failed", "writeback_failed"}, result
        if coordinator.status()["active"] is None:
            break
        coordinator = RuntimeCoordinator(root)  # State survives process/object replacement.
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("loop")["status"] == "stopped"
        candidates = store.list_candidates("loop")
        runs = store.list_runs("loop")
        assert len(candidates) == len(runs) == 3
        assert [c["parent_id"] for c in candidates] == [None, "c0", "c1"]
        assert len({c["worktree"] for c in candidates}) == 3
        assert len({r["source_commit"] for r in runs}) == 3
        distances = [r["evidence"]["metrics"]["waypoint_distance_m"] for r in runs]
        assert distances == pytest.approx([0.4, 0.2, 0.4 / 3])
        assert len(list((root / "knowledge/findings").glob("run-*.md"))) == 3
        assert len(list((root / "knowledge/queries").glob("runtime-open*.md"))) == 3
        assert len([e for e in store.events("loop") if e["kind"] == "coordinator_decision"]) == 13
