"""Small durable continuation gate shared by the store and coordinator."""
from __future__ import annotations

from typing import Any


TERMINAL = {"completed", "failed", "cancelled"}


def continuation_ready(store: Any, task: dict[str, Any]) -> tuple[bool, str | None]:
    continuation = task["scope"].get("continuation")
    if not continuation:
        return True, None
    source = store.task(continuation["source_task_id"])
    mode = continuation.get("mode", "research")
    if source is None:
        return False, "continuation source task is missing"
    if mode == "research" and source["status"] != "completed":
        return False, "source task has not completed successfully"
    if mode != "research" and source["status"] not in {"completed", "failed", "cancelled"}:
        return False, "recovery source task is still running"
    for task_id in continuation.get("after_task_ids", []):
        dependency = store.task(task_id)
        if dependency is None:
            return False, "continuation dependency is missing"
        if dependency["status"] not in TERMINAL:
            return False, "continuation dependency is not terminal"
    return True, None
