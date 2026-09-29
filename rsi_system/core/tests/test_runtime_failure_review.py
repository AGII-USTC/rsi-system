"""A stop acknowledges only the task failures explicitly reviewed in that decision."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_types import MissionSpec


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
        assert store.task(old["task_id"])["status"] == "cancelled"
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
