"""Per-task Docker development tests; the Agent never receives a Docker socket."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from . import runner_environments as docker
from .gpu_resources import gpu_slot
from .pi_agent import atomic_json


def container_name(task_id, call_id):
    return "rsi-dev-" + hashlib.sha256(f"{task_id}:{call_id}".encode()).hexdigest()[:28]


def _tail(path):
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 6000))
        return stream.read().decode("utf-8", errors="replace")


def run_development_test(*, runtime_root, task, mission, config, call_id, command,
                         environment=None, gpu=False, check_cancelled=None):
    """Run in an owned, disposable container, with durable logs and no total timeout."""
    runtime_root = Path(runtime_root).resolve()
    directory = runtime_root / "tasks" / task["task_id"]
    workspace = directory / "workspace"
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("development workspace must be this task's real directory")
    workspace = workspace.resolve()
    selected = docker.select_environment(config, mission,
        {} if environment is None else {"environment": environment})
    if selected is None:
        raise ValueError("no Mission-allowed Docker environment is configured")
    # Development owns its service lifecycle. Do not load an administrator policy
    # automatically, and do not grant a GPU for an ordinary CPU check.
    selected = {k: v for k, v in selected.items() if k != "policy"}
    if gpu and not selected.get("gpu", False):
        raise ValueError("this Docker environment does not allow GPU tests")
    selected["gpu"] = gpu
    pinned = docker.prepare_environment(selected)
    docker._check_workspace(pinned["mounts"], directory)
    for item in pinned["mounts"]:
        target = Path(item["target"])
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in [Path('/work'), Path('/code')]):
            raise ValueError("reference mount overlaps a development writable directory")
    name = container_name(task["task_id"], call_id)
    test_dir = directory / "development" / call_id
    test_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = test_dir / "receipt.json"
    request = {"task_id": task["task_id"], "mission_id": task["mission_id"],
               "command": command, "environment": pinned["name"], "gpu": gpu}
    request_path = test_dir / "request.json"
    if request_path.exists():
        if json.loads(request_path.read_text()) != request:
            raise ValueError("development call ID belongs to a different request")
        if receipt_path.exists():
            return json.loads(receipt_path.read_text())
        # An uncertain command may already have written files. Preserve the
        # interruption and require a fresh call ID instead of executing twice.
        result = docker._docker("rm", "--force", name, check=False)
        if result.returncode and "No such container" not in result.stderr:
            raise RuntimeError("interrupted development container could not be removed")
        receipt = {**request, "kind": "development_test", "status": "interrupted",
                   "exit_code": None, "receipt_path": str(receipt_path),
                   "scientific_evidence": False, "container": name}
        atomic_json(receipt_path, receipt)
        return receipt

    guard = (gpu_slot(runtime_root, check_cancelled=check_cancelled,
                      occupied=lambda: docker.gpu_containers_active(runtime_root)) if gpu else nullcontext())
    with guard:
        if check_cancelled:
            check_cancelled()
        # An old pre-lock Runner can still be in flight during rollout of this
        # feature. The bridge checks its live ledger before entering this call.
        atomic_json(request_path, request)
        env = {**pinned["env"], "HOME": "/work/.dev-home", "RSI_DEV_WORKSPACE": "/work",
               "RSI_DEV_PYTHON": pinned["python"], "PYTHONDONTWRITEBYTECODE": "1"}
        if pinned["pythonpath"]:
            env["PYTHONPATH"] = ":".join(["/work", *pinned["pythonpath"]])
        if not gpu:
            env["CUDA_VISIBLE_DEVICES"] = ""
        home = workspace / '.dev-home'
        if home.is_symlink():
            raise ValueError("development HOME must be task-local")
        home.mkdir(exist_ok=True)
        args = ["run", "--name", name, "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--ipc=private", "--network=none",
                "--pids-limit=512", "--user", f"{docker.os.getuid()}:{docker.os.getgid()}",
                "--log-driver=none", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                "--workdir", "/work", "--label", f"rsi.development.task={task['task_id']}"]
        if gpu:
            args += ["--gpus", "all", "--label", docker.gpu_resource_label(runtime_root)]
        args += docker._mount_args(pinned["mounts"])
        args += ["--mount", f"type=bind,src={workspace},dst=/work",
                 "--mount", f"type=bind,src={workspace},dst={workspace}"]
        # A registered Candidate also has a task-owned checkout, separate from
        # the communication workspace. Reference source remains read-only.
        candidate = directory / "candidate-workspace.json"
        if candidate.exists():
            info = json.loads(candidate.read_text())
            raw_checkout = Path(info["path"])
            checkout = raw_checkout.resolve()
            expected = runtime_root / "worktrees" / info["cycle_id"]
            if (checkout != expected.resolve() or raw_checkout.is_symlink()
                    or info.get('task_id') != task['task_id'] or not info['cycle_id'].startswith('coder-')):
                raise ValueError("Candidate checkout is not owned by this task")
            for item in pinned["mounts"]:
                if Path(item["target"]).is_relative_to(Path('/code')) or Path('/code').is_relative_to(Path(item["target"])):
                    raise ValueError("reference mount overlaps development Candidate checkout")
            args += ["--mount", f"type=bind,src={checkout},dst=/code"]
            env["RSI_DEV_CODE"] = "/code"
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        args += ["--entrypoint", "/bin/sh", pinned["image_id"], "-c", command]
        started = datetime.now(timezone.utc).isoformat()
        logs = workspace / "dev-tests" / call_id
        if logs.is_symlink() or logs.parent.is_symlink():
            raise ValueError("development logs must be task-local")
        logs.mkdir(parents=True, exist_ok=True)
        stdout, stderr = logs / "stdout.log", logs / "stderr.log"
        process = None
        error = None
        cleanup_error = None
        code = None
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                process = subprocess.Popen(["docker", *args], env=docker._host_env(),
                                           stdout=out, stderr=err)
                while process.poll() is None:
                    if check_cancelled:
                        check_cancelled()
                    time.sleep(0.5)
                code = process.returncode
        except BaseException as exc:
            error = str(exc) or type(exc).__name__
            raise
        finally:
            try:
                result = docker._docker("rm", "--force", name, check=False)
                if result.returncode and "No such container" not in result.stderr:
                    cleanup_error = result.stderr[:500]
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_error = str(exc)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            receipt = {**request, "kind": "development_test", "scientific_evidence": False,
                       "status": "interrupted" if error else "completed",
                       "container": name, "image_id": pinned["image_id"],
                       "environment_sha256": pinned["sha256"], "started_at": started,
                       "finished_at": datetime.now(timezone.utc).isoformat(),
                       "exit_code": code, "error": error, "cleanup_error": cleanup_error,
                       "receipt_path": str(receipt_path), "stdout_path": str(stdout),
                       "stderr_path": str(stderr), "stdout_tail": _tail(stdout) if stdout.exists() else '',
                       "stderr_tail": _tail(stderr) if stderr.exists() else ''}
            atomic_json(receipt_path, receipt)
            atomic_json(logs / 'receipt.json', receipt)
        return receipt
