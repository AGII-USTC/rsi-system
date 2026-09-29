"""Command line entry point for the Mission-driven RSI runtime."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .runtime import MissionSpec, RuntimeCoordinator, read_mission


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="register a Mission from JSON")
    start.add_argument("mission", help="JSON object or JSON file")
    sub.add_parser("status", help="show Mission and blackboard state")
    pause = sub.add_parser("pause", help="pause the active Mission")
    pause.add_argument("mission_id")
    pause.add_argument("--reason", default="paused by operator")
    resume = sub.add_parser("resume", help="resume a paused Mission after reviewing failures")
    resume.add_argument("mission_id")
    resume.add_argument("--reason", default="resumed after operator review")
    tick = sub.add_parser("tick", help="schedule or execute one durable task")
    tick.add_argument("--quiet-empty", action="store_true")
    work = sub.add_parser("work", help="execute queued collaborators without scheduling a coordinator")
    work.add_argument("--follow", action="store_true", help="keep serving while active tasks remain")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    coordinator = RuntimeCoordinator(args.root.resolve())
    try:
        if args.action == "start":
            _emit(coordinator.start(read_mission(args.mission)))
        elif args.action == "status":
            _emit(coordinator.status())
        elif args.action == "pause":
            _emit(coordinator.pause(args.mission_id, args.reason))
        elif args.action == "resume":
            _emit(coordinator.resume(args.mission_id, args.reason))
        elif args.action == "work":
            from .pi_agent import run_agent
            from .runtime_parallel import execute_queued
            from .runtime_store import RuntimeStore
            while True:
                with RuntimeStore(coordinator.store_path) as store:
                    mission = store.active_mission()
                    pending = mission and store.queued_or_running(mission["mission_id"])
                if not pending:
                    return 0
                results = execute_queued(coordinator, mission["mission_id"], agent_runner=run_agent,
                                         collaborators_only=True)
                if results:
                    _emit({"tasks": results})
                if not args.follow:
                    return int(any(r["status"] == "task_failed" for r in results))
                time.sleep(1)
        else:
            result = coordinator.tick()
            if not (args.quiet_empty and result.get("status") in {"idle", "waiting", "busy"}):
                _emit(result)
            return int(result.get("status") == "task_failed")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"rsi-runtime: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
