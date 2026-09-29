"""Task-scoped shell checks over frozen review evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import resource
import shutil
import signal
import subprocess
import sys
import time

from .pi_agent import atomic_json

MAX_COMMAND_SECONDS = 60
MAX_OUTPUT_BYTES = 8_000
MAX_WORK_BYTES = 32_000_000


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path, *, allow_symlinks: bool = False) -> list[dict]:
    result = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not allow_symlinks:
                raise ValueError("audit workspace contains a link")
            result.append({"path": path.relative_to(root).as_posix(), "unsafe": "symlink"})
            continue
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("audit workspace contains a link")
        if path.is_file():
            size = path.stat().st_size
            total += size
            if total > MAX_WORK_BYTES or len(result) >= 256:
                raise ValueError("audit workspace size or file count exceeded")
            result.append({"path": path.relative_to(root).as_posix(), "sha256": _hash(path), "bytes": size})
    return result


def _snapshot(manifest_path: Path, destination: Path, task_id: str, mission_id: str) -> dict:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("review evidence manifest is missing")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("task_id") != task_id or manifest.get("mission_id") != mission_id:
        raise ValueError("review evidence identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("review evidence file list is invalid")
    destination.mkdir(mode=0o700)
    total = 0
    seen = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("review evidence entry is invalid")
        relative = PurePosixPath(item["path"])
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("review evidence path is invalid")
        previous_hash = seen.get(str(relative))
        if previous_hash is not None:
            if previous_hash != item.get("sha256"):
                raise ValueError("conflicting duplicate review evidence path")
            continue
        seen[str(relative)] = item.get("sha256")
        source = manifest_path.parent.joinpath(*relative.parts)
        if (source.is_symlink() or any(parent.is_symlink() for parent in source.parents
                                       if parent.is_relative_to(manifest_path.parent)) or
                not source.is_file() or not source.resolve().is_relative_to(manifest_path.parent)):
            raise ValueError("review evidence contains an unsafe path")
        raw = source.read_bytes()
        total += len(raw)
        if total > 32_000_000 or hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise ValueError("review evidence hash or size mismatch")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (destination / "manifest.json").write_bytes(manifest_path.read_bytes())
    return manifest


def _sandbox(command: str, evidence: Path, workspace: Path, python_root: Path | None) -> list[str]:
    bwrap = shutil.which("bwrap")
    if sys.platform != "linux" or not bwrap or not Path("/usr/bin/python3").is_file():
        raise ValueError("review shell requires Linux bubblewrap and system Python")
    path = "/opt/audit-python/bin:/usr/bin:/bin" if python_root else "/usr/bin:/bin"
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-net", "--unshare-pid",
            "--unshare-ipc", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", path, "--setenv", "HOME", "/work",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "PYTHONNOUSERSITE", "1",
            "--tmpfs", "/", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--dir", "/etc", "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    if python_root:
        args += ["--dir", "/opt", "--dir", "/opt/audit-python",
                 "--ro-bind", str(python_root / "bin"), "/opt/audit-python/bin",
                 "--ro-bind", str(python_root / "lib"), "/opt/audit-python/lib"]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--ro-bind", str(evidence), "/evidence", "--bind", str(workspace), "/work",
             "--remount-ro", "/", "--chdir", "/work", "--", "/bin/bash", "--noprofile", "--norc", "-c", command]
    return args


def run_audit_shell(*, task_id: str, mission_id: str, manifest_path: Path, workspace: Path,
                    audit_dir: Path, command: str, timeout_seconds: int,
                    python_executable: Path | None = None) -> dict:
    if not isinstance(command, str) or not command.strip() or len(command.encode()) > 12_000:
        raise ValueError("audit bash command must be nonempty and at most 12000 bytes")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_COMMAND_SECONDS:
        raise ValueError("audit bash timeout must be 1 to 60 seconds")
    interpreter = Path(python_executable).resolve(strict=True) if python_executable else Path("/usr/bin/python3")
    python_root = interpreter.parent.parent if python_executable else None
    if (not interpreter.is_file() or (python_root is not None and
            (interpreter.parent.name != "bin" or not (python_root / "lib").is_dir()))):
        raise ValueError("configured audit Python is not an executable environment")
    audit_dir.mkdir(parents=True, exist_ok=False)
    workspace.mkdir(parents=True, exist_ok=True)
    before = _files(workspace, allow_symlinks=True)
    evidence = audit_dir / "evidence"
    manifest = _snapshot(manifest_path, evidence, task_id, mission_id)
    (audit_dir / "command.sh").write_text(command + "\n")
    inputs = {"task_id": task_id, "mission_id": mission_id,
              "manifest_sha256": _hash(evidence / "manifest.json"),
              "evidence": [{"path": item["path"], "sha256": item["sha256"]} for item in manifest["files"]],
              "workspace_before": before, "command_sha256": _hash(audit_dir / "command.sh"),
              "python_executable": str(interpreter), "python_sha256": _hash(interpreter)}
    atomic_json(audit_dir / "inputs.json", inputs)
    stdout_path, stderr_path = audit_dir / "stdout.log", audit_dir / "stderr.log"

    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8_000_000, 8_000_000))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

    started = time.time()
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        child = subprocess.Popen(_sandbox(command, evidence, workspace, python_root), stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, env={"PATH": "/usr/bin:/bin"},
                                 start_new_session=True, preexec_fn=limits)
        try:
            child.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    try:
        after = _files(workspace)
        workspace_error = None
    except ValueError as exc:
        after = []
        workspace_error = str(exc)
    # A writable source must never be accepted as the frozen input, even if a
    # host process changed it while the isolated command was running.
    evidence_error = None
    if _hash(evidence / "manifest.json") != inputs["manifest_sha256"] or any(
            _hash(evidence / item["path"]) != item["sha256"] for item in inputs["evidence"]):
        evidence_error = "review evidence changed during audit execution"
    output = stdout_path.read_bytes()
    error = stderr_path.read_bytes()
    receipt = {"task_id": task_id, "mission_id": mission_id, "kind": "diagnostic_audit",
               "command_sha256": inputs["command_sha256"], "inputs_sha256": _hash(audit_dir / "inputs.json"),
               "exit_code": child.returncode, "timed_out": timed_out,
               "stdout_sha256": _hash(stdout_path), "stderr_sha256": _hash(stderr_path),
               "workspace_after": after, "started_at_unix": started, "finished_at_unix": time.time(),
               "integrity_error": workspace_error or evidence_error,
               "stdout": output[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
               "stderr": error[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
               "output_truncated": len(output) > MAX_OUTPUT_BYTES or len(error) > MAX_OUTPUT_BYTES}
    atomic_json(audit_dir / "receipt.json", receipt)
    return receipt
