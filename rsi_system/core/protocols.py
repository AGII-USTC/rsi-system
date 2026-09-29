"""Closed protocol registry for bounded CPU experiments."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .provenance import sha256_file


PI05_PROTOCOL = "pi05-libero-gain-switch-index5-v1"
# Optional legacy protocol inputs are supplied by a deployment.  No model or
# checkpoint path is bundled in this standalone package.
PI05_SCALE = Path("/path/to/response_scale_dev.json")
PI05_SCALE_SHA256 = ""
PI05_CONDITIONS = ("no_memory", "frozen_memory", "continuous_memory")
PERIOD_PROTOCOL = "runtime-harness-period-v1"
PROTOCOLS = ("runtime-harness-smoke-v1", "residual-memory-dev-v1", PI05_PROTOCOL, PERIOD_PROTOCOL)


def max_seconds(record: dict[str, Any]) -> int | None:
    budget = record["resource_budget"]
    if set(budget) != {"max_seconds"}:
        raise ValueError("resource_budget requires only max_seconds")
    value = budget["max_seconds"]
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 1800:
        raise ValueError("max_seconds must be an integer in [1, 1800]")
    return value


def _path(value: object, label: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"{label} must exist at an absolute path")
    return path.resolve(strict=True)


def input_paths(record: dict[str, Any]) -> dict[str, Path]:
    if record["protocol_id"] == PI05_PROTOCOL:
        scale = _path(str(PI05_SCALE), "scale_file", directory=False)
        if sha256_file(scale) != PI05_SCALE_SHA256:
            raise ValueError("pi05 response scale does not match registered artifact")
        return {"scale_file": scale}
    if record["protocol_id"] != "residual-memory-dev-v1":
        return {}
    config = record["config"]
    data = _path(config["data_dir"], "data_dir", directory=True)
    model = _path(config["model_path"], "model_path", directory=False)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("split") != "dev" or manifest.get("kind") != "residual_memory_public_trajectories_v1":
        raise ValueError("data_dir must contain a development manifest")
    if model.name != "nominal_model.json" or not (model.parent / "fit.json").is_file():
        raise ValueError("model_path must reference a nominal model with fit provenance")
    return {"data_dir": data, "model_path": model, "fit_path": model.parent / "fit.json"}


def build_command(record: dict[str, Any], output_dir: Path, python: str | Path,
                  *, input_overrides: dict[str, Path] | None = None) -> list[str]:
    """Validate a record and build argv. Stored proposal commands are never executed."""
    if record["protocol_id"] not in PROTOCOLS:
        raise ValueError("unregistered protocol")
    if record["execution_target"] != "server" or record["command"]:
        raise ValueError("server protocols require an empty proposal command")
    max_seconds(record)
    config = record["config"]
    if record["protocol_id"] == PERIOD_PROTOCOL:
        if set(config) != {"period_ticks"} or type(config["period_ticks"]) is not int or config["period_ticks"] not in {4, 6, 8, 12, 16, 24}:
            raise ValueError("period demo requires only period_ticks in {4,6,8,12,16,24}")
        return [str(python), "-m", "experiments.runtime_harness", "--output", str(output_dir),
                "--seed", "0", "--steps", "64", "--mode", "periodic", "--monitor", "none",
                "--switch-tick", "-1", "--latency-ticks", "4", "--chunk-ticks", "16",
                "--period-ticks", str(config["period_ticks"])]
    if record["protocol_id"] == PI05_PROTOCOL:
        if set(config) != {"condition"} or config["condition"] not in PI05_CONDITIONS:
            raise ValueError("pi05 config requires only a registered condition")
        scale = input_paths(record)["scale_file"]
        if input_overrides is not None:
            if set(input_overrides) != {"scale_file"}:
                raise ValueError("unexpected pi05 input override")
            scale = input_overrides["scale_file"]
        return [str(python), "-m", "experiments.pi05_memory.rsi_gain_switch_runner",
                "--condition", config["condition"], "--scale-file", str(scale),
                "--output", str(output_dir), "--run-id", record["experiment_id"]]
    if record["protocol_id"] == "runtime-harness-smoke-v1":
        if set(config) - {"seed", "steps"}:
            raise ValueError("unknown runtime harness config parameter")
        seed, steps = config.get("seed", 0), config.get("steps", 32)
        if type(seed) is not int or not 0 <= seed <= 1_000_000:
            raise ValueError("seed must be an integer in [0, 1000000]")
        if type(steps) is not int or not 1 <= steps <= 160:
            raise ValueError("steps must be an integer in [1, 160]")
        return [str(python), "-m", "experiments.runtime_harness", "--output", str(output_dir),
                "--seed", str(seed), "--steps", str(steps), "--mode", "periodic",
                "--monitor", "none", "--switch-tick", "-1"]
    if set(config) != {"data_dir", "model_path"}:
        raise ValueError("residual dev config requires only data_dir and model_path")
    paths = input_paths(record)
    return [str(python), "-m", "experiments.residual_memory", "evaluate", "--split", "dev",
            "--data", str(paths["data_dir"]), "--model", str(paths["model_path"]),
            "--output", str(output_dir)]


def read_metrics(record: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    if record["protocol_id"] == PERIOD_PROTOCOL:
        data = json.loads((output_dir / "summary.json").read_text())
        config = json.loads((output_dir / "config.json").read_text())
        fixed = {"seed": 0, "steps": 64, "mode": "periodic", "monitor": "none", "switch_tick": -1,
                 "latency_ticks": 4, "chunk_ticks": 16, "period_ticks": record["config"]["period_ticks"]}
        if any(config.get(k) != v for k, v in fixed.items()) or data.get("environment_steps") != 64:
            raise ValueError("period demo output differs from frozen evaluation")
        base = read_metrics({**record, "protocol_id": "runtime-harness-smoke-v1"}, output_dir)
        if type(data.get("requests")) is not int or data["requests"] < 1 or data.get("gain_switch_tick") is not None:
            raise ValueError("period demo has invalid request or gain-switch metrics")
        if base["waypoint_reached"] != (base["waypoint_distance_m"] <= 0.05):
            raise ValueError("period demo waypoint predicate differs from distance")
        return {**base, "period_ticks": config["period_ticks"], "requests": data["requests"],
                "accepted_responses": data["accepted_responses"],
                "fallback_steps": data.get("executed_actions_by_source", {}).get("fallback", 0),
                "feasible": base["waypoint_reached"], "metaworld_task_success": data["metaworld_task_success"]}
    if record["protocol_id"] == PI05_PROTOCOL:
        data = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
        expected = {"suite": "libero_spatial", "seed": 7, "initial_state_index": 5,
                    "condition": record["config"]["condition"], "xyz_gain": 1.0,
                    "correction_power": 0.5, "selected_task_id": None,
                    "trials_per_task": 1, "replan_steps": 1, "warmup_steps": 10}
        if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
            raise ValueError("pi05 result differs from fixed evaluation contract")
        schedule = data.get("gain_schedule")
        if schedule != {"gain_before": 1.0, "gain_after": 0.6,
                        "switch_controlled_step": 80,
                        "indexing": "zero-based controlled actions; warmup excluded"}:
            raise ValueError("pi05 gain schedule mismatch")
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 10 or {item.get("task_id") for item in tasks if isinstance(item, dict)} != set(range(10)):
            raise ValueError("pi05 result requires ten distinct tasks")
        for task in tasks:
            if (task.get("error") is not None or type(task.get("success")) is not bool
                    or type(task.get("controlled_steps")) is not int
                    or not 1 <= task["controlled_steps"] <= 220):
                raise ValueError("pi05 task has an error or invalid outcome")
        if data.get("successes") != sum(task["success"] for task in tasks):
            raise ValueError("pi05 success count mismatch")
        if data.get("scale_file_sha256") != PI05_SCALE_SHA256:
            raise ValueError("pi05 scale artifact mismatch")
        return {"successes": data["successes"], "tasks": tasks,
                "scale_file_sha256": data["scale_file_sha256"]}
    path = output_dir / ("summary.json" if record["protocol_id"] == "runtime-harness-smoke-v1" else "run.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("protocol result schema mismatch")
    if record["protocol_id"] == "runtime-harness-smoke-v1":
        if data.get("monitor_mode") != "none" or data.get("mode") != "periodic":
            raise ValueError("unexpected harness mode")
        if type(data.get("environment_steps")) is not int or data["environment_steps"] < 1:
            raise ValueError("harness result missing executed steps")
        if (type(data.get("waypoint_distance_m")) not in {int, float}
                or not math.isfinite(data["waypoint_distance_m"])
                or data["waypoint_distance_m"] < 0
                or type(data.get("waypoint_reached")) is not bool):
            raise ValueError("harness result missing distance metrics")
        return {"environment_steps": data["environment_steps"],
                "waypoint_distance_m": data["waypoint_distance_m"],
                "waypoint_reached": data["waypoint_reached"]}
    if data.get("split") != "dev" or data.get("kind") != "prequential_residual_memory_diagnostic":
        raise ValueError("unexpected residual evaluation result")
    episodes = data.get("episodes")
    if not isinstance(episodes, list) or len(episodes) != 4:
        raise ValueError("residual result requires four development episodes")
    for item in episodes:
        if (not isinstance(item, dict) or not isinstance(item.get("episode_id"), str)
                or type(item.get("recorded_steps")) is not int or item["recorded_steps"] < 1):
            raise ValueError("residual episode metrics malformed")
        full = item.get("metrics", {}).get("full")
        if not isinstance(full, dict):
            raise ValueError("residual full-window metrics missing")
        for method in ("nominal", "online"):
            value = full.get(method, {})
            if (type(value.get("count")) is not int or value["count"] < 1
                    or type(value.get("rmse_m")) not in {int, float}
                    or not math.isfinite(value["rmse_m"]) or value["rmse_m"] < 0):
                raise ValueError("residual RMSE metrics malformed")
    return {"episode_count": len(episodes), "episodes": [
        {"episode_id": item["episode_id"], "metrics": item["metrics"]} for item in episodes]}
