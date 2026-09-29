from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rsi_system.core.runtime import MissionSpec, RuntimeStore
from rsi_system.core.runtime_lease import renew_execution_lease


def test_runner_heartbeat_has_no_secondary_wall_clock_limit(monkeypatch):
    from rsi_system.core import runtime_lease
    captured = []
    class Once:
        def __init__(self): self.calls = 0
        def wait(self, _):
            self.calls += 1
            return self.calls > 1
        def set(self): pass
    class InlineThread:
        def __init__(self, target, **_): self.target = target
        def start(self): self.target()
        def join(self, **_): pass
    monkeypatch.setattr(runtime_lease, "Event", Once)
    monkeypatch.setattr(runtime_lease, "Thread", InlineThread)
    monkeypatch.setattr(runtime_lease, "renew_execution_lease",
                        lambda *args, **kw: captured.append(kw["deadline"]) or True)
    coordinator = SimpleNamespace(_config=lambda: {"agent_timeout_seconds": 900}, store_path="unused")
    with runtime_lease.task_lease_heartbeat(coordinator, {"role": "runner"}):
        pass
    assert captured == [None]


def test_live_lease_renews_without_claiming_scientific_progress(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "bounded work"))
        store.create_task("m", "researcher", "investigate", "test")
        task = store.claim_task("m", lease_seconds=1)
        now = datetime.now(timezone.utc)
        assert renew_execution_lease(path, task, now=now, deadline=now + timedelta(seconds=100))
        renewed = store.task(task["task_id"])
        assert renewed["lease_until"] > task["lease_until"]
        assert renewed["updated_at"] == task["updated_at"]
        assert renewed["attempt"] == task["attempt"]
        assert store.remaining_agent_tasks("m") is None


def test_heartbeat_cannot_override_operator_pause_resume_or_replacement(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "controlled work"))
        store.create_task("m", "researcher", "investigate", "test")
        task = store.claim_task("m", lease_seconds=1)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=100)
        store.set_mission_status("m", "paused", reason="operator intervention")
        assert not renew_execution_lease(path, task, deadline=deadline)
        store.resume_mission("m", reason="operator permits fresh execution")
        assert not renew_execution_lease(path, task, deadline=deadline)
        assert store.task(task["task_id"])["lease_until"] == task["lease_until"]
        store.db.execute("UPDATE agent_tasks SET lease_token='replacement' WHERE task_id=?", (task["task_id"],))
        store.db.commit()
        assert not renew_execution_lease(path, task, deadline=deadline)


def test_heartbeat_stops_at_deadline_and_cannot_extend_completed_task(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "bounded work"))
        store.create_task("m", "researcher", "investigate", "test")
        task = store.claim_task("m", lease_seconds=1)
        now = datetime.now(timezone.utc)
        assert not renew_execution_lease(path, task, now=now, deadline=now)
        store.finish_task(task["task_id"], task["lease_token"], {"summary": "done"})
        assert not renew_execution_lease(path, task, now=now, deadline=now + timedelta(seconds=100))
        assert store.task(task["task_id"])["lease_until"] is None
