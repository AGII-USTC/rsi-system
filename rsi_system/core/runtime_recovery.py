"""Recover only persisted outcomes, preserving the original task budget and input."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import secrets
from .runtime_types import _now


def recover_expired(coordinator, store, mission, task, *, agent_runner, require_model):
    directory = coordinator.runtime_root / "tasks" / task["task_id"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "execution.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "waiting", "task_id": task["task_id"], "reason": "task_executor_active"}
        return _recover_expired(coordinator, store, mission, task,
                                agent_runner=agent_runner, require_model=require_model)


def _recover_expired(coordinator, store, mission, task, *, agent_runner, require_model):
    directory = coordinator.runtime_root / "tasks" / task["task_id"]
    if task["role"] == "runner":
        if task.get("candidate_id"):
            from .runtime_runner_durability import stable_runner_id
            run_directory = coordinator.runtime_root / "runs" / stable_runner_id(task["task_id"])
            recoverable = (directory / "runner_input.json").is_file() or not run_directory.exists()
        else:
            from .runtime_exploration import exploration_run_id
            from .runtime_runner_durability import classify_runner_recovery
            run_directory = coordinator.runtime_root / "runs" / exploration_run_id(task["task_id"])
            recovery = classify_runner_recovery(run_directory)
            if recovery["state"] == "active":
                return {"status": "waiting", "task_id": task["task_id"], "reason": "runner_executor_active"}
            # The exploration entry point validates receipts or records an
            # uncertain failure. It launches only with an unstarted intent.
            recoverable = True
    else:
        recoverable = False
        if (directory / "input.json").is_file():
            try:
                saved = json.loads((directory / "input.json").read_text())
                valid_input = saved["task_id"] == task["task_id"] and saved["input_version"] == task["input_version"]
                recoverable = valid_input and (directory / "agent_result.json").is_file()
                if valid_input and task["role"] == "curator":
                    from .runtime_curator import load_curator_submission
                    recoverable = recoverable or load_curator_submission(coordinator.root, task) is not None
            except (OSError, ValueError, KeyError, TypeError):
                recoverable = False
    status_events = store.db.execute("""SELECT 1 FROM blackboard_events WHERE mission_id=? AND version>?
        AND kind='mission_status' LIMIT 1""", (task["mission_id"], task["input_version"])).fetchone()
    if not recoverable or status_events:
        return None
    with store.atomic():
        current = store.task(task["task_id"])
        if current["lease_token"] != task["lease_token"] or current["status"] != "running" or current["lease_until"] > _now():
            raise ValueError("expired task identity changed during recovery")
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=2100)).replace(microsecond=0).isoformat()
        store.db.execute("UPDATE agent_tasks SET lease_token=?,lease_until=?,updated_at=? WHERE task_id=?",
                         (secrets.token_urlsafe(18), deadline, _now(), task["task_id"]))
    # An accepted Curator submission can replace a lost agent_result receipt.
    return coordinator._execute_claimed(store, mission, store.task(task["task_id"]),
                                        agent_runner=agent_runner, require_model=require_model)
