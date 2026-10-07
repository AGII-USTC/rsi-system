"""Frozen, bounded Python probes for Ideas that have no formal Candidate."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import time

from .pi_agent import atomic_json
from .runtime_communications import (checked_file, load_manifest, publish_direct_messages,
                                     resolve_handoff_file)
from .runtime_types import _json
from .runtime_evaluation_summary import persist_evaluation_summary
from .runner_environments import select_environment, prepare_environment, launch_environment, cleanup_environment
from .sandbox import candidate_environment, command
from .worker import _linux_start_token, _stop_group
from .provenance import snapshot_source, sha256_tree
from .runtime_runner_durability import (classify_runner_recovery, mark_receipt_ready,
                                         mark_started, mark_uncertain, stable_runner_id,
                                         write_intent)

MAX_OUTPUT_FILES = 32
MAX_ARTIFACT_BYTES = 2_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_LOG_BYTES = 4_000_000


def exploration_limits(max_seconds=120):
    return {"max_seconds": min(max_seconds, 120) if max_seconds is not None else None, "max_output_files": MAX_OUTPUT_FILES,
            "max_artifact_bytes": MAX_ARTIFACT_BYTES, "max_total_bytes_including_logs": MAX_TOTAL_BYTES,
            "max_combined_log_bytes": MAX_LOG_BYTES}


def artifact_diagnostics(output, stdout_path, stderr_path, max_seconds=120):
    """Bounded metadata explains failure without ingesting oversized output."""
    files = [p for p in output.rglob("*") if not p.is_dir() or p.is_symlink()]
    sizes = [(p.relative_to(output).as_posix(), p.stat().st_size) for p in files
             if not p.is_symlink() and p.is_file()]
    logs = {p.name: p.stat().st_size for p in (stdout_path, stderr_path)}
    return {"output_file_count": len(files), "output_bytes": sum(size for _, size in sizes),
            "log_bytes": logs, "total_bytes_including_logs": sum(size for _, size in sizes) + sum(logs.values()),
            "largest_output_files": sorted(sizes, key=lambda item: item[1], reverse=True)[:8],
            "limits": exploration_limits(max_seconds),
            "raw_output_preserved": True}


def exploration_run_id(task_id):
    return "run-" + hashlib.sha256(task_id.encode()).hexdigest()[:28]


def runner_bootstrap_text(script_workspace_path):
    """Resolve authored modules from the same frozen workspace as the probe."""
    parent = Path(script_workspace_path).parent.as_posix()
    return (
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "source = os.environ.get('RSI_EXPLORATION_SOURCE')\n"
        "inputs = os.environ.get('RSI_EXPLORATION_INPUTS')\n"
        "paths = [source]\n"
        "if inputs:\n"
        "    paths.extend([inputs, str(Path(inputs) / " + repr(parent) + ")])\n"
        "for path in paths:\n"
        "    if path and path not in sys.path:\n"
        "        sys.path.insert(0, path)\n"
        "script = sys.argv[1]\n"
        "sys.argv = [script, *sys.argv[2:]]\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )


def bounded_artifacts(directory, output, stdout_path, stderr_path):
    files = [stdout_path, stderr_path, *sorted(p for p in output.rglob("*")
                                               if not p.is_dir() or p.is_symlink())]
    if len(files) > MAX_OUTPUT_FILES + 2:
        raise ValueError("exploration produced too many artifacts")
    total, hashes = 0, {}
    for path in files:
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory):
            raise ValueError("exploration artifact is not a regular file")
        size = path.stat().st_size
        total += size
        if size > MAX_ARTIFACT_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("exploration output budget exhausted")
        hashes[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def output_within_budget(output, log_bytes=0):
    files = [path for path in output.rglob("*") if not path.is_dir() or path.is_symlink()]
    return len(files) <= MAX_OUTPUT_FILES and all(not path.is_symlink() and path.is_file() and
                                     path.stat().st_size <= MAX_ARTIFACT_BYTES for path in files) and (
                                         sum(path.stat().st_size for path in files) + log_bytes <= MAX_TOTAL_BYTES)


def _classify_failure(returncode, error, stderr_path, output):
    """Classify a Runner failure without pretending it is a scientific result."""
    if returncode == 0 and error is None:
        return {"execution_status": "completed", "measurement_status": "complete",
                "failure_class": None, "runner_action": "report_to_researcher"}
    try:
        stderr = stderr_path.read_text(errors="replace")[-12000:]
    except OSError:
        stderr = ""
    output_files = [path for path in output.rglob("*") if path.is_file() and not path.is_symlink()]
    text = "\n".join(part for part in (error or "", stderr) if part)
    if any(marker in text.lower() for marker in ("time budget", "log budget", "artifact budget")):
        execution_status, failure_class = "timeout", "resource_limit"
    elif not output_files and any(marker in text for marker in (
            "Traceback", "ModuleNotFoundError:", "ImportError:", "SyntaxError:",
            "NameError:", "TypeError:", "FileNotFoundError:")):
        execution_status, failure_class = "preflight_failed", "code_contract"
    elif not output_files:
        execution_status, failure_class = "preflight_failed", "environment"
    else:
        execution_status, failure_class = "failed_during_program", "program"
    measurement_status = "partial" if output_files or stderr else "none"
    return {"execution_status": execution_status, "measurement_status": measurement_status,
            "failure_class": failure_class,
            "runner_action": "repair_coder" if failure_class == "code_contract" else "report_to_researcher"}


def _request_coder_repair(store, mission, task, source, run, evidence):
    """Return a code-contract failure to the source Coder and notify its owner."""
    if evidence.get("failure_class") != "code_contract" or source.get("role") != "coder":
        return None
    mission_id = mission["mission_id"]
    active_repair = [item for item in store.list_tasks(mission_id)
                     if item["role"] == "coder" and item["status"] in {"queued", "running"}
                     and item["scope"].get("reply_to_task_id") == source["task_id"]]
    if active_repair:
        return active_repair[0]
    detail = (evidence.get("error") or "Runner rejected the source script during preflight")[:1600]
    parent_id = task["scope"].get("parent_task_id") or task["task_id"]
    repair = store.create_task(
        mission_id, "coder",
        f"Repair Runner preflight failure for {run['run_id']}",
        f"Runner returned this source script for repair: {detail}",
        candidate_id=task.get("candidate_id"), parent_task_id=parent_id,
        idea_id=task["scope"].get("idea_id"), reply_to_task_id=source["task_id"],
        research_node_id=task["scope"].get("research_node_id"),
        research_revision=task["scope"].get("research_revision"))
    text = (f"Runner returned Run {run['run_id']} before scientific measurement. "
            f"Class={evidence.get('failure_class')}; error={detail}. "
            f"Repair task {repair['task_id']} must publish a corrected script and "
            "implementation_note.md, rerun the local Runner-contract smoke check, and "
            "report the new file IDs. This is an execution repair, not a scientific result.")
    messages = [{"idea_id": task["scope"].get("idea_id"), "to_task_id": source["task_id"], "text": text,
                 "in_reply_to": task["task_id"]}]
    owner_id = task["scope"].get("parent_task_id")
    if owner_id and owner_id != source["task_id"]:
        messages.append({"idea_id": task["scope"].get("idea_id"), "to_task_id": owner_id,
                         "text": text, "in_reply_to": task["task_id"]})
    else:
        messages.append({"idea_id": task["scope"].get("idea_id"), "to_role": "coordinator",
                         "text": text, "in_reply_to": task["task_id"]})
    publish_direct_messages(store, task, {"messages": messages})
    store.event(mission_id, "runner_repair_requested", {
        "runner_task_id": task["task_id"], "source_task_id": source["task_id"],
        "repair_task_id": repair["task_id"], "run_id": run["run_id"],
        "failure_class": evidence.get("failure_class"), "execution_status": evidence.get("execution_status")})
    return repair


def _research_owner(store, task):
    """Find the nearest branch owner who can decide the next scientific action."""
    parent_id = task["scope"].get("parent_task_id")
    while parent_id:
        parent = store.task(parent_id)
        if not parent:
            break
        if parent["role"] in {"researcher", "hypothesis", "coordinator"}:
            return parent
        parent_id = parent["scope"].get("parent_task_id")
    return None


def execute_exploration(coordinator, store, mission, task):
    description = task["scope"]["experiment"]
    coordinator._validate_exploration(store, mission, {"idea_id": task["scope"]["idea_id"],
                                                       "research_node_id": task["scope"].get("research_node_id"),
                                                       "experiment": description},
                                             current_task_id=task["task_id"])
    source = store.task(description["source_task_id"])
    compositions = [item for item in store.research_snapshot(mission["mission_id"])["compositions"]
                    if item["node_id"] == task["scope"].get("research_node_id")]
    composition = compositions[0] if compositions else None
    source_manifest = coordinator.runtime_root / "tasks" / source["task_id"] / "communications/outbox/manifest.json"
    manifest = load_manifest(source_manifest, source)
    entry, ref = resolve_handoff_file(manifest, source["task_id"],
        file_id=description.get("script_file_id"), path=description.get("script"), suffix=".py")
    path = ref["path"]
    raw = checked_file(source_manifest.parent, path, entry["sha256"])
    if len(raw) > 1_000_000:
        raise ValueError("exploration script exceeds the input size limit")
    run_id = exploration_run_id(task["task_id"])
    directory = coordinator.runtime_root / "runs" / run_id
    fresh_directory = not directory.exists() or not any(directory.iterdir())
    directory.mkdir(parents=True, exist_ok=True)
    inputs_directory = directory / "inputs"
    inputs_directory.mkdir(exist_ok=True)
    for item in manifest["files"]:
        if not item["path"].startswith("notes/"):
            continue
        relative = Path(item["path"]).relative_to("notes")
        destination = inputs_directory / relative
        contents = checked_file(source_manifest.parent, item["path"], item["sha256"])
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != contents:
                raise ValueError("frozen exploration supporting input changed")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
    script = directory / "probe.py"
    source_directory = directory / "source"
    frozen = directory / "input.json"
    if frozen.exists():
        saved = json.loads(frozen.read_text())
        source_commit = saved["source_commit"]
        if sha256_tree(source_directory) != saved["source_sha256"]:
            raise ValueError("frozen project source changed")
    else:
        source_commit = snapshot_source(coordinator.root, source_directory)
    bootstrap = directory / "runner_bootstrap.py"
    if frozen.exists() and "bootstrap_sha256" in saved:
        # A software upgrade must not change the identity of an existing Run.
        # Reuse its pinned launcher, validating bytes before receipt recovery.
        if not bootstrap.is_file() or bootstrap.is_symlink():
            raise ValueError("frozen Runner bootstrap changed")
        bootstrap_raw = bootstrap.read_bytes()
        if hashlib.sha256(bootstrap_raw).hexdigest() != saved["bootstrap_sha256"]:
            raise ValueError("frozen Runner bootstrap changed")
    else:
        bootstrap_text = runner_bootstrap_text(ref["workspace_path"])
        bootstrap_raw = bootstrap_text.encode()
    bootstrap_sha256 = hashlib.sha256(bootstrap_raw).hexdigest()
    pinned = {"run_id": run_id, "task_id": task["task_id"], "idea_id": task["scope"]["idea_id"],
              "description": description, "script_sha256": hashlib.sha256(raw).hexdigest(),
              "source_commit": source_commit, "source_sha256": sha256_tree(source_directory),
              "inputs_sha256": sha256_tree(inputs_directory), "bootstrap_sha256": bootstrap_sha256,
              "source_manifest_sha256": hashlib.sha256(source_manifest.read_bytes()).hexdigest()}
    if frozen.exists():
        # Existing receipts and intents retain their original identity.
        if "resource_budget" in saved:
            pinned["resource_budget"] = saved["resource_budget"]
    else:
        pinned["resource_budget"] = {"max_seconds": exploration_limits(
            mission["payload"]["resources"].get("max_seconds_per_run", 120))["max_seconds"]}
    if frozen.exists():
        if "runner_environment" in saved:
            pinned["runner_environment"] = saved["runner_environment"]
    else:
        selected = select_environment(coordinator._config(), mission, description)
        if selected is not None:
            pinned["runner_environment"] = prepare_environment(selected)
    if task["scope"].get("research_node_id"):
        pinned.update({"research_node": {"node_id": task["scope"]["research_node_id"],
                                         "revision": task["scope"]["research_revision"]},
                       "composition_inputs": composition["inputs"] if composition else [],
                       "source_run_ids": composition["plan"].get("frozen_source_run_ids", []) if composition else []})
    legacy_frozen = False
    if frozen.exists():
        saved = json.loads(frozen.read_text())
        legacy_frozen = "bootstrap_sha256" not in saved
        expected = dict(pinned)
        if legacy_frozen:
            expected.pop("bootstrap_sha256")
        if saved != expected or script.read_bytes() != raw:
            raise ValueError("exploration input changed since it was frozen")
        if not legacy_frozen and (
                not bootstrap.is_file() or hashlib.sha256(bootstrap.read_bytes()).hexdigest() != bootstrap_sha256):
            raise ValueError("frozen Runner bootstrap changed")
    else:
        script.write_bytes(raw)
        bootstrap.write_bytes(bootstrap_raw)
        atomic_json(frozen, pinned)
    receipt = directory / "run_record.json"
    input_sha256 = hashlib.sha256(_json(pinned).encode()).hexdigest()
    # Register the execution intent before any child process can be launched.
    # Existing legacy receipts are adopted below; a legacy directory with no
    # receipt is treated as ambiguous and cannot be replayed automatically.
    if fresh_directory and not (directory / "execution_intent.json").exists():
        write_intent(directory, run_id=run_id, task_id=task["task_id"],
                     input_sha256=input_sha256, executor="local", attempt=1,
                     stage="prepared", source_commit=source_commit)
    recovery = classify_runner_recovery(directory, receipt=receipt)
    identity = recovery.get("record")
    if identity and (identity.get("task_id"), identity.get("run_id"), identity.get("input_sha256")) != (
            task["task_id"], run_id, input_sha256):
        raise ValueError("Runner intent identity does not match frozen inputs")
    # Reserve the Mission Run before launch.  The task unique index makes this
    # idempotent on recovery; evidence remains provisional until a receipt.
    with store.atomic():
        store.validate_task(task)
        store.create_run(mission["mission_id"], None, task["task_id"], "exploration",
                         "prepared", source_commit, pinned,
                         {"status": "prepared", "kind": "exploration", "artifact_dir": str(directory),
                          "run_id": run_id, "input_sha256": input_sha256,
                          "execution_status": "prepared", "measurement_status": "none"},
                         idea_ref=task["scope"]["idea_id"])
    if receipt.exists():
        if recovery["state"] == "invalid_receipt" and recovery.get("record", {}).get("receipt_sha256"):
            raise ValueError("exploration receipt hash does not match durable Runner intent")
        evidence = json.loads(receipt.read_text())
        if evidence.get("status") not in {"completed", "failed"}:
            raise ValueError("exploration receipt is nonterminal or invalid")
        if evidence.get("input_sha256") != input_sha256:
            raise ValueError("exploration receipt belongs to different inputs")
        for relative, expected in evidence.get("artifact_hashes", {}).items():
            artifact = directory / relative
            if artifact.is_symlink() or not artifact.resolve().is_relative_to(directory) or (
                    not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected):
                raise ValueError("exploration receipt artifact changed")
        if recovery.get("record") and recovery["record"].get("stage") != "completed":
            mark_receipt_ready(directory, receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
    elif recovery["state"] == "active":
        raise RuntimeError("local Runner executor is still active; refusing duplicate execution")
    elif recovery["state"] in {"uncertain", "invalid_intent", "missing_intent", "invalid_receipt"}:
        reason = "Runner intent is interrupted or ambiguous; execution was not repeated"
        if recovery["state"] == "invalid_intent":
            reason = "Runner intent is corrupt; execution was not repeated"
        if pinned.get("runner_environment"):
            cleanup_environment(pinned["runner_environment"], directory)
        if recovery.get("record") and recovery["state"] == "uncertain":
            mark_uncertain(directory, reason=reason)
        evidence = {"status": "failed", "kind": "exploration", "artifact_dir": str(directory),
                    "source_commit": source_commit, "exit_code": None, "error": reason,
                    "input_sha256": input_sha256, "script_sha256": pinned["script_sha256"],
                    "execution_status": "uncertain_interrupted", "measurement_status": "unknown",
                    "failure_class": "interrupted_execution", "runner_action": "report_to_researcher",
                    "interpretation": "execution state is uncertain; no automatic retry was performed"}
    else:
        if not recovery["safe_to_launch"]:
            raise ValueError("Runner execution state does not authorize launch")
        max_seconds = pinned.get("resource_budget", {"max_seconds": 120})["max_seconds"]
        output = directory / "output"
        output.mkdir(exist_ok=True)
        stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
        python = str(coordinator._config()["python"])
        argv = [python, "-I", str(bootstrap if not legacy_frozen else script),
                *([] if legacy_frozen else [str(script)])]
        if not pinned.get("runner_environment") and coordinator._config().get("sandbox", True):
            argv = command(argv, cwd=directory, writable=[output], readable=[directory],
                           protected=[coordinator.root / ".rsi", coordinator.root / ".git"])
        env = candidate_environment()
        env["RSI_EXPLORATION_OUTPUT"] = str(output)
        env["RSI_EXPLORATION_SOURCE"] = str(source_directory)
        env["RSI_EXPLORATION_INPUTS"] = str(inputs_directory)
        started = time.time()
        error = None
        returncode = None
        execution = ({"kind": "docker", "environment": pinned["runner_environment"]["name"],
                      "image_id": pinned["runner_environment"]["image_id"], "state": "preparing"}
                     if pinned.get("runner_environment") else {"kind": "host", "python": python})
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            # Persist intent before creating containers or a policy sidecar.
            mark_started(directory)
            boundary = (launch_environment(pinned["runner_environment"], directory, argv, env,
                                           resource_root=coordinator.runtime_root,
                                           check_cancelled=lambda: store.validate_task(task))
                        if pinned.get("runner_environment") else nullcontext((argv, env, execution)))
            process = None
            try:
                with boundary as (launch, process_env, execution):
                    atomic_json(directory / "execution_environment.json", execution)
                    process = subprocess.Popen(launch, cwd=directory, env=process_env, stdout=stdout, stderr=stderr,
                                               start_new_session=True)
                    mark_started(directory, pid=process.pid,
                                 process_start_token=_linux_start_token(process.pid))
                    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
                    try:
                        while process.poll() is None:
                            store.validate_task(task)
                            if deadline is not None and time.monotonic() >= deadline:
                                error = "exploration time budget exhausted"
                                _stop_group(process)
                            elif stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_LOG_BYTES:
                                error = "exploration log budget exhausted"
                                _stop_group(process)
                            elif not output_within_budget(output, stdout_path.stat().st_size + stderr_path.stat().st_size):
                                error = "exploration artifact budget exhausted"
                                _stop_group(process)
                            else:
                                time.sleep(0.1)
                        returncode = process.returncode
                    finally:
                        if process.poll() is None:
                            _stop_group(process)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                error = f"Runner environment/launch failed: {type(exc).__name__}: {exc}"
                docker_record = directory / "docker_environment.json"
                if docker_record.is_file():
                    execution = json.loads(docker_record.read_text())
        # Cleanup metadata may be completed by the environment context manager.
        if execution.get("cleanup_errors"):
            error = (error + "; " if error else "") + "Runner environment cleanup failed; operator reconciliation required"
        atomic_json(directory / "execution_environment.json", execution)
        try:
            artifact_hashes = bounded_artifacts(directory, output, stdout_path, stderr_path)
            hashes_complete = True
        except ValueError as exc:
            hashes_complete = False
            artifact_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (stdout_path, stderr_path) if p.stat().st_size <= MAX_ARTIFACT_BYTES}
            error = str(exc)
        classification = _classify_failure(returncode, error, stderr_path, output)
        status = "completed" if classification["execution_status"] == "completed" else "failed"
        evidence = {"status": status, "kind": "exploration", "artifact_dir": str(directory),
                    "source_commit": source_commit, "exit_code": returncode,
                    "error": error,
                    "execution_environment": execution,
                    "artifact_diagnostics": artifact_diagnostics(output, stdout_path, stderr_path, max_seconds),
                    "artifact_hashes_complete": hashes_complete,
                    "started_at_unix": started, "finished_at_unix": time.time(),
                    "input_sha256": hashlib.sha256(_json(pinned).encode()).hexdigest(),
                    "script_sha256": pinned["script_sha256"],
                    "artifact_hashes": {**artifact_hashes, **{
                        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in ("input.json", "probe.py", "runner_bootstrap.py", "execution_environment.json",
                                     "docker_environment.json", "policy.log", "docker_cleanup_errors.json")
                        if (directory / name).is_file()}},
                    **classification,
                    "interpretation": "exploratory probe only; not a confirmed Candidate result"}
        atomic_json(receipt, evidence)
        mark_receipt_ready(directory, receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
    with store.atomic():
        store.validate_task(task)
        run = store.create_run(mission["mission_id"], None, task["task_id"], "exploration",
                               evidence["status"], source_commit, pinned, evidence,
                               idea_ref=task["scope"]["idea_id"])
        run = store.finalize_run(run["run_id"], evidence["status"], evidence)
        if run["status"] == "completed" and evidence.get("artifact_dir"):
            summary_path, summary = persist_evaluation_summary(
                run, artifact_dir=evidence["artifact_dir"],
                protocol_metrics={key: value for key, value in evidence.items()
                                  if key in {"execution_status", "measurement_status", "failure_class", "exit_code"}})
            evidence = {**evidence, "evaluation_summary_path": str(summary_path),
                        "evaluation_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                        "artifact_hashes": {**evidence.get("artifact_hashes", {}),
                                            "evaluation_summary.json": hashlib.sha256(summary_path.read_bytes()).hexdigest()}}
            run = store.update_run_evidence(run["run_id"], evidence)
        source = store.task(description["source_task_id"])
        repair = _request_coder_repair(store, mission, task, source, run, evidence) if source else None
        followup = source["task_id"]
        owner = _research_owner(store, task)
        if repair is None:
            owner_message = (
                f"Runner finished Run {run['run_id']}: execution_status={evidence.get('execution_status')}, "
                f"measurement_status={evidence.get('measurement_status')}, "
                f"failure_class={evidence.get('failure_class')}. "
                "Receipt and raw artifacts are available through rsi_wait/rsi_status. "
                "This message reports execution state; it is not a scientific assessment.")
            if owner:
                publish_direct_messages(store, task, {"messages": [{
                    "idea_id": task["scope"].get("idea_id"), "to_task_id": owner["task_id"],
                    "text": owner_message, "in_reply_to": task["task_id"]}]})
            else:
                publish_direct_messages(store, task, {"messages": [{
                    "idea_id": task["scope"].get("idea_id"), "to_role": "coordinator",
                    "text": owner_message, "in_reply_to": task["task_id"]}]})
        store.event(mission["mission_id"], "exploration_feedback", {
            "run_id": run["run_id"], "idea_id": task["scope"]["idea_id"],
            "source_task_id": source["task_id"], "reply_to_task_id": followup,
            "status": run["status"], "execution_status": evidence.get("execution_status"),
            "measurement_status": evidence.get("measurement_status"),
            "repair_task_id": repair["task_id"] if repair else None})
        if repair:
            run["repair_task_id"] = repair["task_id"]
        store.finish_task(task["task_id"], task["lease_token"], {
            "run": run, "repair_task_id": repair["task_id"] if repair else None})
        if evidence.get("execution_environment", {}).get("cleanup_errors"):
            store.set_mission_status(mission["mission_id"], "paused",
                                     reason="Runner container cleanup needs operator reconciliation")
    return run
