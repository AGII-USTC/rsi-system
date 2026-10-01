"""A stop acknowledges only the task failures explicitly reviewed in that decision."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_types import MissionSpec
from rsi_system.core.runtime import RuntimeCoordinator
from .test_orchestrator import project


def _failed_task(store, mission_id="m"):
    task = store.create_task(mission_id, "researcher", "Investigate", "Audit")
    claimed = store.claim_task(mission_id)
    assert claimed["task_id"] == task["task_id"]
    return store.finish_task(task["task_id"], claimed["lease_token"],
                             {"error": "provider unavailable"}, status="failed")


def _coordinator(store, mission_id="m"):
    task = store.create_task(mission_id, "coordinator", "Review", "Decide")
    claimed = store.claim_task(mission_id)
    assert claimed["task_id"] == task["task_id"]
    return claimed


def _stop(reviewed=()):
    return {"decision": "stop", "summary": "Review complete", "reviewed_failures": [
        {"task_id": task_id, "reason": "Provider outage inspected; retry succeeded"}
        for task_id in reviewed]}


def test_reviewed_historical_failure_does_not_block_deferred_stop(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "Audit retries"))
        old = _failed_task(store)
        coordinator = _coordinator(store)
        child = store.create_task("m", "curator", "Publish", "Required before stop")
        store.apply_coordinator_decision(coordinator, _stop([old["task_id"]]), [])
        assert store.deferred_stop("m")
        assert store.unreviewed_deferred_failures("m") == []
        decision = [event for event in store.events("m") if event["kind"] == "coordinator_decision"][-1]
        assert decision["payload"]["reviewed_failed_task_ids"] == [old["task_id"]]
        assert store.task(old["task_id"])["status"] == "failed"
        claimed = store.claim_task("m")
        assert claimed["task_id"] == child["task_id"]
        store.finish_task(child["task_id"], claimed["lease_token"], {"summary": "Published"})
    with RuntimeStore(path) as store:
        assert store.unreviewed_deferred_failures("m") == []
        assert store.finish_deferred_stop("m")["status"] == "stopped"
        assert store.task(old["task_id"])["output"] == {"error": "provider unavailable"}


def test_new_child_failure_after_review_blocks_stop(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "Audit retries"))
        old = _failed_task(store)
        coordinator = _coordinator(store)
        child = store.create_task("m", "curator", "Publish", "Required before stop")
        store.apply_coordinator_decision(coordinator, _stop([old["task_id"]]), [])
        claimed = store.claim_task("m")
        store.finish_task(child["task_id"], claimed["lease_token"],
                          {"error": "publication failed"}, status="failed")
        assert store.unreviewed_deferred_failures("m") == [child["task_id"]]
        with pytest.raises(ValueError, match="unreviewed"):
            store.finish_deferred_stop("m")
        assert store.mission("m")["status"] == "active"
        assert store.deferred_stop("m")


def test_unreviewed_failure_blocks_immediate_stop_and_resume_preserves_evidence(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "Audit retries"))
        old = _failed_task(store)
        coordinator = _coordinator(store)
        store.apply_coordinator_decision(coordinator, _stop(), [])
        assert store.mission("m")["status"] == "active"
        assert store.unreviewed_deferred_failures("m") == [old["task_id"]]
        with pytest.raises(ValueError, match="unreviewed"):
            store.finish_deferred_stop("m")
        store.set_mission_status("m", "paused", reason="Unreviewed task failure")
        store.resume_mission("m", reason="Operator reviewed failure")
        assert store.deferred_stop("m") is None
        assert store.task(old["task_id"])["status"] == "failed"  # Resume preserves the original failure.
        assert store.task(old["task_id"])["output"] == {"error": "provider unavailable"}


def test_review_requires_failed_task_in_same_mission_and_reason(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "Audit"))
        store.create_mission(MissionSpec("other", "Other audit"))
        foreign = _failed_task(store, "other")
        old = _failed_task(store)
        coordinator = _coordinator(store)
        version = store.mission("m")["version"]
        bad_reviews = [
            [{"task_id": foreign["task_id"], "reason": "Reviewed"}],
            [{"task_id": old["task_id"], "reason": "  "}],
            [{"task_id": old["task_id"], "reason": "Reviewed"}] * 2,
        ]
        for reviews in bad_reviews:
            with pytest.raises(ValueError):
                store.apply_coordinator_decision(coordinator, {"decision": "stop",
                    "reviewed_failures": reviews}, [])
            assert store.mission("m")["version"] == version
            assert store.task(coordinator["task_id"])["status"] == "running"


def test_concurrent_stop_decision_and_child_failure_still_block(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "Audit"))
        old = _failed_task(store)
        coordinator = _coordinator(store)
        child = store.create_task("m", "researcher", "Continue", "Pending review")
        claimed = store.claim_task("m")
        assert claimed["task_id"] == child["task_id"]
    barrier = Barrier(2)

    def decide():
        with RuntimeStore(path) as store:
            barrier.wait(timeout=10)
            store.apply_coordinator_decision(coordinator, _stop([old["task_id"]]), [])

    def fail():
        with RuntimeStore(path) as store:
            barrier.wait(timeout=10)
            store.finish_task(child["task_id"], claimed["lease_token"],
                              {"error": "new failure"}, status="failed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(decide), pool.submit(fail)
        first.result(timeout=10)
        second.result(timeout=10)
    with RuntimeStore(path) as store:
        assert store.unreviewed_deferred_failures("m") == [child["task_id"]]
        with pytest.raises(ValueError, match="unreviewed"):
            store.finish_deferred_stop("m")


def test_old_deferred_stop_schema_migrates_without_acknowledging_failures(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE deferred_mission_stops(
            mission_id TEXT PRIMARY KEY, reason TEXT NOT NULL, requested_at TEXT NOT NULL)""")
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "Audit"))
        old = _failed_task(store)
        store.db.execute("""INSERT INTO deferred_mission_stops
            (mission_id,reason,requested_at) VALUES(?,?,?)""",
                         ("m", "Old stop", "2026-09-27T00:00:00+00:00"))
        store.db.commit()
    with RuntimeStore(path) as store:
        assert store.unreviewed_deferred_failures("m") == [old["task_id"]]


def _provisional_stop_runtime(tmp_path, budget=8):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Review failures", resources={"max_agent_tasks": budget}))
    with RuntimeStore(runtime.store_path) as store:
        failed = _failed_task(store)
        actor = _coordinator(store)
        store.apply_coordinator_decision(actor, _stop(), [])
    return runtime, failed


def _review_response(reviewed=()):
    return {**_stop(reviewed), "expected_information": "none",
            "budget_impact": "one review, no repeated experiment", "stop_condition": "review complete"}


def test_missing_stop_review_gets_indexed_feedback_then_can_stop(tmp_path):
    runtime, failed = _provisional_stop_runtime(tmp_path)
    scheduled = runtime.tick(require_model=False)
    assert scheduled["reason"] == "stop_failure_review"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("m")["status"] == "active"
        assert store.unreviewed_deferred_failures("m") == [failed["task_id"]]
        assert store.remaining_agent_tasks("m") == 5

    def review(**kwargs):
        context = json.loads(kwargs["prompt"].split("\n", 1)[1])["context"]
        inventory = context["stop_failure_review"]
        assert inventory["eligible_failed_task_ids"] == [failed["task_id"]]
        assert inventory["unreviewed_failed_task_ids"] == [failed["task_id"]]
        assert inventory["recent_failures"][0]["task_id"] == failed["task_id"]
        return _review_response([failed["task_id"]])

    assert runtime.tick(agent_runner=review, require_model=False)["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("m")["status"] == "stopped"
        assert store.task(failed["task_id"]) == failed
        assert store.remaining_agent_tasks("m") == 5


def test_completed_runner_is_not_eligible_failed_task(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Distinguish task and Run outcomes"))
    with RuntimeStore(runtime.store_path) as store:
        failed = _failed_task(store)
        runner = store.create_task("m", "runner", "Run diagnostic", "Test")
        claimed = store.claim_task("m")
        assert claimed["task_id"] == runner["task_id"]
        store.finish_task(runner["task_id"], claimed["lease_token"],
                          {"run_id": "run-failed", "status": "failed"})
        actor = _coordinator(store)
        context = json.loads(runtime._prompt("coordinator", store.mission("m"), actor, store)
                             .split("\n", 1)[1])["context"]["stop_failure_review"]
        assert context["eligible_failed_task_ids"] == [failed["task_id"]]
        assert context["unreviewed_failed_task_ids"] == []


def test_unresolved_stop_review_is_bounded_and_preserves_failures(tmp_path):
    runtime, failed = _provisional_stop_runtime(tmp_path)
    assert runtime.tick(require_model=False)["reason"] == "stop_failure_review"
    assert runtime.tick(agent_runner=lambda **_: _review_response(), require_model=False)["status"] == "completed"
    result = runtime.tick(require_model=False)
    assert result["reason"] == "deferred_stop_task_failed"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("m")["status"] == "paused"
        assert store.task(failed["task_id"]) == failed
        assert len([e for e in store.events("m") if e["kind"] == "stop_failure_review_requested"]) == 1
        # Explicit operator review opens a new bounded window and retains the provisional stop.
        store.set_mission_status("m", "active", reason="Operator approved one further review")
    assert runtime.tick(require_model=False)["reason"] == "stop_failure_review"


def test_stop_failure_feedback_does_not_exceed_budget(tmp_path):
    runtime, failed = _provisional_stop_runtime(tmp_path, budget=2)
    assert runtime.tick(require_model=False)["reason"] == "deferred_stop_task_failed"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("m")["status"] == "paused"
        assert store.remaining_agent_tasks("m") == 0
        assert len(store.list_tasks("m")) == 2
        assert store.task(failed["task_id"]) == failed


@pytest.mark.parametrize("decision", ["wait", "schedule"])
def test_failure_review_coordinator_can_choose_to_continue(tmp_path, decision):
    runtime, failed = _provisional_stop_runtime(tmp_path)
    assert runtime.tick(require_model=False)["reason"] == "stop_failure_review"
    response = {**_review_response(), "decision": decision,
                "summary": "More evidence is needed before deciding to stop"}
    if decision == "schedule":
        response["tasks"] = [{"role": "researcher", "objective": "Review the unresolved question",
                               "reason": "Coordinator chooses further research"}]
    assert runtime.tick(agent_runner=lambda **_: response, require_model=False)["status"] == "completed"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("m")["status"] == ("active" if decision == "schedule" else "paused")
        assert store.deferred_stop("m") is None
        assert store.task(failed["task_id"]) == failed
