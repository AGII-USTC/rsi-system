"""Renew a live executor's lease without overriding Mission control or time limits."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Event, Thread


def renew_execution_lease(path, task, *, deadline, now=None, horizon_seconds=90):
    now = now or datetime.now(timezone.utc)
    if deadline is not None and now >= deadline:
        return False
    until = now + timedelta(seconds=horizon_seconds)
    if deadline is not None:
        until = min(until, deadline)
    encoded = until.replace(microsecond=0).isoformat()
    # A heartbeat must never wait behind a long scientific transaction.
    with sqlite3.connect(path, timeout=0.2) as db:
        updated = db.execute("""UPDATE agent_tasks
            SET lease_until=CASE WHEN lease_until > ? THEN lease_until ELSE ? END
            WHERE task_id=? AND status='running' AND lease_token=?
              AND EXISTS (SELECT 1 FROM missions m
                          WHERE m.mission_id=agent_tasks.mission_id AND m.status='active')
              AND NOT EXISTS (SELECT 1 FROM blackboard_events e
                              WHERE e.mission_id=agent_tasks.mission_id
                                AND e.kind='mission_status' AND e.version>?)""",
            (encoded, encoded, task["task_id"], task["lease_token"], task["input_version"]))
        return updated.rowcount == 1


@contextmanager
def task_lease_heartbeat(coordinator, task):
    config = coordinator._config()
    seconds = config.get("agent_timeout_seconds")
    if task["role"] == "runner":
        # Includes bounded preparation/receipt publication time. Runner itself
        # continues enforcing the separately frozen experiment time limit.
        seconds = max(seconds or 0, 1800)
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds + 180)
                if seconds is not None else None)
    stopped = Event()

    def heartbeat():
        while not stopped.wait(30):
            try:
                if not renew_execution_lease(coordinator.store_path, task, deadline=deadline):
                    return
            except sqlite3.OperationalError:
                # A transient busy database grants no extra permission; retry
                # only while the executor still holds its execution lock.
                continue

    thread = Thread(target=heartbeat, name="rsi-task-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
