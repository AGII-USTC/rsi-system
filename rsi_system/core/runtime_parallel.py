"""Bounded parallel execution; SQLite leases arbitrate across processes."""
from concurrent.futures import ThreadPoolExecutor

from .runtime_store import RuntimeStore


def parallel_limits(coordinator, mission):
    config = coordinator._config()
    resources = mission["payload"]["resources"]
    limits = (resources.get("max_parallel_agents", config.get("runtime_max_parallel_agents", 1)),
              resources.get("max_parallel_runs", config.get("runtime_max_parallel_runs", 1)))
    if any(type(value) is not int or not 1 <= value <= 16 for value in limits):
        raise ValueError("parallel task limits must be integers between 1 and 16")
    return limits


def execute_queued(coordinator, mission_id, *, agent_runner, require_model=True, collaborators_only=False):
    with RuntimeStore(coordinator.store_path) as store:
        mission = store.mission(mission_id)
        if not mission or mission["status"] != "active":
            return []
        agents, runs = parallel_limits(coordinator, mission)
        tasks = []
        for _ in range(agents + runs + 1):
            task = store.claim_task(mission_id,
                lease_seconds=max(900, (coordinator._config()["agent_timeout_seconds"] or 0) + 180),
                max_parallel_agents=agents, max_parallel_runs=runs, collaborators_only=collaborators_only)
            if task is None:
                break
            tasks.append(task)
    if not tasks:
        return []

    def execute(task):
        with RuntimeStore(coordinator.store_path) as fresh:
            return coordinator._execute(fresh, fresh.mission(mission_id), task,
                                        agent_runner=agent_runner, require_model=require_model)

    # Each worker owns its connection and task directory; claims are atomic.
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return list(pool.map(execute, tasks))
