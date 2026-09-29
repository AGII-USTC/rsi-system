"""Run one approved, bounded experiment from a pinned Git source tree."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from .protocols import PI05_PROTOCOL, PERIOD_PROTOCOL, build_command, input_paths, max_seconds, read_metrics
from .provenance import sha256_file, snapshot_inputs, snapshot_source
from .store import ExperimentStore
from . import sandbox as isolation


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _stop_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def _linux_start_token(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def _stop_orphan(evidence: dict[str, Any]) -> None:
    _cleanup_containers(evidence)
    pid, token = evidence.get("process_pid"), evidence.get("process_start_token")
    if type(pid) is not int or not isinstance(token, str) or _linux_start_token(pid) != token:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.2)
        if _linux_start_token(pid) == token:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _cleanup_containers(evidence: dict[str, Any]) -> None:
    if evidence.get("protocol_id") != PI05_PROTOCOL:
        return
    experiment_id = evidence.get("experiment_id")
    if not isinstance(experiment_id, str):
        return
    for role in ("eval", "server"):
        try:
            subprocess.run(["docker", "rm", "-f", f"rsi-pi05-{experiment_id}-{role}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_claimed(store: ExperimentStore, record: dict[str, Any], repo_root: Path,
                 run_root: Path, python: str | Path, sandbox: bool = False, source_commit: str | None = None) -> dict[str, Any]:
    run_dir = run_root / record["experiment_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    runtime_dir = run_dir / "runtime" if sandbox else run_dir
    runtime_dir.mkdir(exist_ok=True)
    output_dir = runtime_dir / "output"
    result: dict[str, Any] = {"schema_version": 1, "experiment_id": record["experiment_id"],
                              "protocol_id": record["protocol_id"], "artifact_dir": str(run_dir),
                              "started_at_unix": time.time(), "status": "running"}
    _write_json(run_dir / "run_record.json", result)
    process = None
    try:
        # Validate the original proposal before taking snapshots or launching a child.
        build_command(record, output_dir, python)
        paths = input_paths(record)
        commit = snapshot_source(repo_root, run_dir / "source", source_commit)
        copied, input_hashes = snapshot_inputs(paths, run_dir / "inputs")
        source_hashes = {}
        if record["protocol_id"] == PI05_PROTOCOL:
            for name in ("libero_calibrated_eval.py", "calibrated_memory.py", "fixed_noise_server.py",
                         "libero_gain_eval.py", "gain_switch_schedule.py", "rsi_gain_switch_runner.py"):
                source_file = run_dir / "source" / "experiments" / "pi05_memory" / name
                source_hashes[name] = sha256_file(source_file)
        execution_record = dict(record)
        if record["protocol_id"] == PI05_PROTOCOL:
            argv = build_command(execution_record, output_dir, python, input_overrides=copied)
        else:
            execution_record["config"] = {**record["config"], **{key: str(value) for key, value in copied.items()}}
            argv = build_command(execution_record, output_dir, python)
        config_json = json.dumps({"protocol_id": record["protocol_id"], "config": record["config"],
                                  "resource_budget": record["resource_budget"]}, sort_keys=True, allow_nan=False)
        result.update({"source_commit": commit, "config_sha256": hashlib.sha256(config_json.encode()).hexdigest(),
                       "input_hashes": input_hashes, "source_file_hashes": source_hashes, "command": argv,
                       "stdout_path": str(run_dir / "stdout.log"), "stderr_path": str(run_dir / "stderr.log")})
        _write_json(run_dir / "run_record.json", result)
        env = isolation.candidate_environment() if sandbox else os.environ.copy()
        env["PYTHONPATH"] = str(run_dir / "source")
        with (run_dir / "stdout.log").open("wb") as stdout, (run_dir / "stderr.log").open("wb") as stderr:
            # The pi05 runner owns a fixed Docker/GPU boundary and must see the
            # host Docker socket; the CPU protocols retain bubblewrap isolation.
            use_sandbox = sandbox and record["protocol_id"] != PI05_PROTOCOL
            launch = isolation.command(argv, cwd=run_dir / "source", writable=[runtime_dir],
                                       readable=[run_dir / "inputs"] if (run_dir / "inputs").exists() else [],
                                       protected=[repo_root / ".rsi", repo_root / ".git"]) if use_sandbox else argv
            process = subprocess.Popen(launch, cwd=run_dir / "source", env=env,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            result.update({"process_pid": process.pid, "process_start_token": _linux_start_token(process.pid)})
            _write_json(run_dir / "run_record.json", result)
            deadline = time.monotonic() + max_seconds(record)
            while process.poll() is None:
                current = store.get(record["experiment_id"])
                if current is None or current["status"] != "running":
                    result["status"] = "cancelled"
                    _stop_group(process)
                    break
                if time.monotonic() >= deadline:
                    result["status"] = "failed"
                    result["error"] = "max_seconds exceeded"
                    _stop_group(process)
                    break
                time.sleep(0.1)
            result["exit_code"] = process.returncode
        if result["status"] == "running":
            if process.returncode != 0:
                raise RuntimeError(f"protocol exited with code {process.returncode}")
            metrics = read_metrics(record, output_dir)
            if not _finite(metrics):
                raise ValueError("nonfinite protocol metrics")
            evidence = output_dir / ("summary.json" if record["protocol_id"] in {"runtime-harness-smoke-v1", PI05_PROTOCOL, PERIOD_PROTOCOL} else "run.json")
            if record["protocol_id"] == PI05_PROTOCOL:
                if metrics["scale_file_sha256"] != input_hashes["scale_file_sha256"]:
                    raise ValueError("pi05 result scale hash differs from snapshot")
                result["docker_manifest_sha256"] = sha256_file(runtime_dir / "docker_manifest.json")
                result["steps_sha256"] = sha256_file(output_dir / "steps.jsonl")
            result.update({"status": "completed", "metrics": metrics,
                           "evidence_path": str(evidence), "evidence_sha256": sha256_file(evidence)})
            _write_json(run_dir / "analysis.json", {"schema_version": 1, "experiment_id": record["experiment_id"],
                                                     "metrics": metrics, "evidence_path": str(evidence),
                                                     "claim": "measured protocol output; no scientific success claim"})
            _write_json(run_dir / "candidate.json", {"schema_version": 1, "experiment_id": record["experiment_id"],
                                                      "status": "awaiting reviewer analysis", "evidence_path": str(evidence)})
    except KeyboardInterrupt:
        if process is not None:
            _stop_group(process)
        result.update({"status": "interrupted", "error": "worker interrupted"})
    except Exception as exc:
        if process is not None:
            _stop_group(process)
        result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    _cleanup_containers(result)
    result["finished_at_unix"] = time.time()
    from .runtime_evidence import RAW_FILES
    result["artifact_hashes"] = {name: sha256_file(run_dir / name) for name in RAW_FILES
                                 if name != "run_record.json" and (run_dir / name).is_file()
                                 and not (run_dir / name).is_symlink()}
    _write_json(run_dir / "run_record.json", result)
    current = store.get(record["experiment_id"])
    if current is not None and current["status"] == "cancelled":
        store.attach_run(record["experiment_id"], result)
    elif current is not None and current["status"] == "running":
        store.finish_run(record["experiment_id"], result["status"], result, note=result.get("error"))
    return result


def run_once(store: ExperimentStore, repo_root: Path | str, run_root: Path | str,
             *, python: str | Path = sys.executable, sandbox: bool = False, source_commit: str | None = None) -> dict[str, Any] | None:
    """Recover interrupted work and run at most one queued server experiment.

    A nonblocking file lock allows one worker per registry. None means no work or
    another worker holds the lock.
    """
    run_root = Path(run_root).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    invocation = shutil.which(str(python)) if Path(python).parent == Path(".") else str(python)
    if invocation is None:
        raise ValueError(f"python executable not found: {python}")
    executable = Path(os.path.abspath(invocation))
    if not executable.is_file():
        raise ValueError(f"python executable not found: {python}")
    lock_path = store.path.with_suffix(store.path.suffix + ".worker.lock")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        # The terminal file is written before the experiment registry transaction.
        # Recover that narrow crash window without downgrading verified measurements.
        for pending in store.list(status="running"):
            run_dir = run_root / pending["experiment_id"]
            try:
                saved = json.loads((run_dir / "run_record.json").read_text())
                if saved.get("experiment_id") != pending["experiment_id"] or saved.get("status") != "completed":
                    continue
                summary = Path(saved["evidence_path"])
                if not summary.resolve().is_relative_to(run_dir) or sha256_file(summary) != saved["evidence_sha256"]:
                    continue
                if any(not (run_dir / name).resolve().is_relative_to(run_dir) or sha256_file(run_dir / name) != digest
                       for name, digest in saved.get("artifact_hashes", {}).items()):
                    continue
                _stop_orphan(saved)
                store.finish_run(pending["experiment_id"], "completed", saved, note="recovered verified terminal receipt")
            except (OSError, ValueError, KeyError, TypeError):
                continue
        recovered = store.recover_running()
        # Recovery may itself have stopped between the state update and evidence attachment.
        recovered += [item["experiment_id"] for item in store.list(status="interrupted")
                      if item.get("run") is None and item["experiment_id"] not in recovered]
        for experiment_id in recovered:
            run_dir = run_root / experiment_id
            record_path = run_dir / "run_record.json"
            evidence = {"schema_version": 1, "experiment_id": experiment_id, "artifact_dir": str(run_dir)}
            try:
                if record_path.is_file():
                    saved = json.loads(record_path.read_text(encoding="utf-8"))
                    if not isinstance(saved, dict) or saved.get("experiment_id") != experiment_id:
                        raise ValueError("recovery record identity mismatch")
                    evidence = saved
            except (OSError, ValueError) as exc:
                evidence["recovery_warning"] = f"run record unreadable: {exc}"
                # Preserve malformed evidence instead of overwriting it.
                record_path = run_dir / "recovery_record.json"
            _stop_orphan(evidence)
            evidence.update({"status": "interrupted", "error": "worker restarted before completion"})
            store.attach_run(experiment_id, evidence)
            if run_dir.is_dir():
                try:
                    _write_json(record_path, evidence)
                except OSError:
                    pass  # The database retains the terminal recovery evidence.
        record = store.claim_next()
        if record is None:
            return None
        try:
            return _run_claimed(store, record, Path(repo_root), run_root, executable, sandbox, source_commit)
        except Exception as exc:
            evidence = {"schema_version": 1, "experiment_id": record["experiment_id"],
                        "status": "failed", "error": f"worker setup failed: {type(exc).__name__}: {exc}",
                        "artifact_dir": str(run_root / record["experiment_id"])}
            if store.get(record["experiment_id"])["status"] == "running":
                store.finish_run(record["experiment_id"], "failed", evidence, note=evidence["error"])
            return evidence
