"""Explicit, bounded dashboard actions for the existing Mission runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets
import shutil
import sys
import threading
from typing import Callable

from rsi_system.core.config import load_research_config
from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.pi_agent import settings_status
from rsi_system.core.reasoning_capabilities import THINKING_LEVELS
from rsi_system.core.runtime_roles import model_policy
from rsi_system.core.schema import safe_id


class RuntimeUnavailable(ValueError):
    pass


def _executable(value: object) -> bool:
    return isinstance(value, str) and bool(shutil.which(value))


def availability(root: Path) -> dict:
    config = load_research_config(root)
    pi_ready = _executable(config.get("pi_executable"))
    python_ready = _executable(config.get("python"))
    # The Mission ledger, SQLite store, Python workers, and dashboard are
    # portable.  bubblewrap is an optional hardening layer for researcher and
    # coder shell work; its absence must not make the whole RSI runtime
    # unavailable on macOS or Windows.
    sandbox_ready = sys.platform == "linux" and bool(shutil.which("bwrap"))
    defaults = settings_status(str(config.get("pi_executable", "")))
    routes = config.get("models", {})
    selected = [model_policy(config, role)["models"][role]
                for role in ("coordinator", "researcher", "hypothesis", "coder", "analyzer", "critic")
                ] if isinstance(routes, dict) else []
    model_ready = bool(defaults["default_provider"] and defaults["default_model"]) or (
        len(selected) == 6 and all(isinstance(route, dict) and route.get("model") and route.get("provider")
                                  for route in selected))
    reasons = []
    if not pi_ready:
        reasons.append("未找到 Pi。安装 Pi，或在私有 research.json 设置 pi_executable。")
    if not python_ready:
        reasons.append("未找到 Python。请在私有 research.json 设置 python。")
    if not sandbox_ready:
        reasons.append("当前平台没有 bubblewrap；核心 RSI 仍可运行，但 researcher/coder 的隔离 shell 将不可用。")
    if not model_ready:
        reasons.append("请在模型配置页设置所有角色路由，或先设置 Pi 默认模型和供应商。")
    return {"available": pi_ready and python_ready and model_ready,
            "pi_available": pi_ready, "models_available": model_ready,
            "python_available": python_ready, "sandbox_available": sandbox_ready,
            "reasons": reasons}


class MissionControl:
    def __init__(self, root: Path, *, coordinator_factory: Callable = RuntimeCoordinator,
                 readiness: Callable = availability):
        self.root = root.resolve()
        self.coordinator_factory = coordinator_factory
        self.readiness = readiness
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}

    def public(self) -> dict:
        with self.lock:
            return {"runtime": self.readiness(self.root),
                    "jobs": [dict(value) for value in self.jobs.values()]}

    def create(self, body: dict) -> dict:
        fields = {"objective", "background", "known_evidence", "stop_conditions",
                  "mission_id", "mode", "resources", "authorization"}
        if set(body) - fields:
            raise ValueError("Mission 包含未知字段")
        if not isinstance(body.get("objective"), str) or not 1 <= len(body["objective"].strip()) <= 8000:
            raise ValueError("请输入 1 至 8000 字的任务目标")
        if not isinstance(body.get("background", ""), str) or len(body.get("background", "")) > 8000:
            raise ValueError("任务背景过长或格式无效")
        for field in ("known_evidence", "stop_conditions"):
            values = body.get(field, [])
            if (not isinstance(values, list) or len(values) > 30 or
                    any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in values)):
                raise ValueError(f"{field} 必须是简短文字列表")
        resources = {"max_agent_tasks": 12, "max_runs": 2, "max_seconds_per_run": 120,
                     "max_parallel_agents": 2, "max_parallel_runs": 1}
        supplied = body.get("resources", {})
        if not isinstance(supplied, dict) or set(supplied) - {*resources, "agent_thinking"}:
            raise ValueError("任务资源配置无效")
        resources.update(supplied)
        bounds = {"max_agent_tasks": 200, "max_runs": 50, "max_seconds_per_run": 120,
                  "max_parallel_agents": 16, "max_parallel_runs": 16}
        if any(type(resources[key]) is not int or not 1 <= resources[key] <= maximum
               for key, maximum in bounds.items()):
            raise ValueError("任务资源超出范围")
        if resources.get("agent_thinking", "xhigh") not in set(THINKING_LEVELS):
            raise ValueError("任务推理档位无效")
        authorization = body.get("authorization", {})
        allowed = {"allow_code_candidates", "allow_server_simulation"}
        if (not isinstance(authorization, dict) or set(authorization) - allowed or
                any(type(value) is not bool for value in authorization.values())):
            raise ValueError("任务授权配置无效")
        authorization = {key: authorization.get(key, False) for key in allowed}
        payload = {**body, "resources": resources, "authorization": authorization,
                   "mission_id": body.get("mission_id") or f"mission-{secrets.token_hex(8)}"}
        mission = MissionSpec.from_payload(payload)
        with self.lock:
            coordinator = self.coordinator_factory(self.root)
            result = coordinator.start(mission, paused=True)
            return {"mission_id": result["mission_id"], "status": "paused"}

    def action(self, mission_id: str, action: str, body: dict) -> dict:
        mission_id = safe_id(mission_id, "mission_id")
        if action not in {"run", "pause", "resume"}:
            raise ValueError("不支持的任务操作")
        if set(body) - ({"steps"} if action == "run" else set()):
            raise ValueError("任务操作包含未知字段")
        with self.lock:
            coordinator = self.coordinator_factory(self.root)
            with RuntimeStore(coordinator.store_path) as store:
                mission = store.mission(mission_id)
            if mission is None:
                raise ValueError("未找到这个任务")
            if action == "pause":
                coordinator.pause(mission_id, reason="Paused explicitly in dashboard")
                return {"mission_id": mission_id, "status": "paused"}
            job = self.jobs.get(mission_id)
            if job and job["status"] == "running":
                return dict(job)
            if action == "resume":
                coordinator.resume(mission_id, reason="Resumed explicitly in dashboard; no worker started")
                return {"mission_id": mission_id, "status": "active"}
            steps = body.get("steps", 1)
            if type(steps) is not int or not 1 <= steps <= 20:
                raise ValueError("每次运行需要 1 至 20 个步骤")
            ready = self.readiness(self.root)
            if not ready["available"]:
                raise RuntimeUnavailable("；".join(ready["reasons"]))
            if mission["status"] == "paused":
                coordinator.resume(mission_id, reason="Run explicitly requested in dashboard")
            elif mission["status"] != "active":
                raise ValueError("请先显式恢复已停止的 Mission，再运行")
            job = {"mission_id": mission_id, "status": "running", "steps": steps,
                   "completed_steps": 0, "started_at": datetime.now(timezone.utc).isoformat(),
                   "result": None}
            self.jobs[mission_id] = job
            thread = threading.Thread(target=self._run, args=(mission_id, steps), daemon=True,
                                      name=f"rsi-dashboard-{mission_id}")
            thread.start()
            return dict(job)

    def _run(self, mission_id: str, steps: int) -> None:
        try:
            coordinator = self.coordinator_factory(self.root)
            for index in range(steps):
                result = coordinator.tick(mission_id=mission_id)
                with self.lock:
                    self.jobs[mission_id].update(completed_steps=index + 1, result=result)
                if result.get("status") in {"idle", "paused", "stopped", "task_failed", "waiting_model",
                                          "waiting", "busy", "writeback_failed"}:
                    break
            with self.lock:
                self.jobs[mission_id]["status"] = ("failed" if result.get("status") in
                                                  {"task_failed", "writeback_failed"} else "finished")
        except Exception:
            with self.lock:
                self.jobs[mission_id].update(status="failed", result={
                    "status": "failed", "reason": "运行失败，请检查本地运行时日志和配置"})
