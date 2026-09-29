"""Administrator-selected Docker environments for exploration Runner probes."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Mapping


_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RUN_ENV = frozenset({"RSI_EXPLORATION_OUTPUT", "RSI_EXPLORATION_SOURCE",
                      "RSI_EXPLORATION_INPUTS"})
_HOST_ENV = frozenset({"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT",
                       "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR"})
_FORBIDDEN_ENV = re.compile(r"(?:SECRET|TOKEN|PASSWORD|PASSWD|(?:^|_)KEY(?:_|$)|CREDENTIAL|AUTH)", re.I)
_FORBIDDEN_ROOTS = (Path("/dev"), Path("/proc"), Path("/sys"), Path("/run"),
                    Path("/var"))
_LOG_LIMIT = 256_000
_FILE_HASH_LIMIT = 16_000_000


def _object(value, label):
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _keys(value, allowed, required, label):
    _object(value, label)
    extra = set(value) - set(allowed)
    missing = set(required) - set(value)
    if extra or missing:
        raise ValueError(f"{label} has unsupported or missing fields: {sorted(extra | missing)}")


def _absolute(value, label, *, host=False):
    if (not isinstance(value, str) or not value or not Path(value).is_absolute() or
            "\x00" in value or "," in value or ".." in Path(value).parts):
        raise ValueError(f"{label} must be an absolute path")
    path = Path(value)
    return str(path.resolve() if host else path)


def _unsafe(path):
    return path == Path("/") or any(path == root or path.is_relative_to(root)
                                    for root in _FORBIDDEN_ROOTS)


def _mounts(value):
    if not isinstance(value, list):
        raise ValueError("mounts must be a list")
    mounts = []
    targets = set()
    for item in value:
        _keys(item, {"source", "target"}, {"source", "target"}, "mount")
        raw_source = Path(_absolute(item["source"], "mount source"))
        source = Path(_absolute(item["source"], "mount source", host=True))
        target = Path(_absolute(item["target"], "mount target"))
        if _unsafe(raw_source) or _unsafe(source) or _unsafe(target) or not source.exists() or source.is_socket():
            raise ValueError("unsafe or nonexistent Docker mount")
        if target in targets or any(target.is_relative_to(other) or other.is_relative_to(target)
                                    for other in targets):
            raise ValueError("Docker mount targets must not overlap")
        stat = source.stat()
        if not (source.is_file() or source.is_dir()):
            raise ValueError("Docker mount source must be a regular file or directory")
        targets.add(target)
        metadata = {"source": str(source), "target": str(target), "readonly": True,
                    "source_device": stat.st_dev, "source_inode": stat.st_ino,
                    "source_type": "directory" if source.is_dir() else "file",
                    "content": "live_directory" if source.is_dir() else "live_large_file"}
        if source.is_file() and stat.st_size <= _FILE_HASH_LIMIT:
            metadata["source_size"] = stat.st_size
            metadata["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            metadata["content"] = "sha256_pinned"
        mounts.append(metadata)
    return mounts


def _env(value, label):
    _object(value, label)
    result = {}
    for key, entry in value.items():
        if (not isinstance(key, str) or not _ENV_KEY.fullmatch(key) or
                _FORBIDDEN_ENV.search(key) or key.startswith("RSI_") or
                key in {"HOME", "PATH", "PYTHONPATH"} or not isinstance(entry, str) or
                "\x00" in entry):
            raise ValueError(f"{label} contains an unsafe environment variable")
        result[key] = entry
    return result


def _pythonpath(value):
    if not isinstance(value, list):
        raise ValueError("pythonpath must be a list")
    return [_absolute(entry, "pythonpath entry") for entry in value]


def _docker(*args, timeout=30, check=True):
    result = subprocess.run(["docker", *args], env=_host_env(), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip()[:500]}")
    return result


def _image_id(image):
    if not isinstance(image, str) or not image.strip() or "\x00" in image:
        raise ValueError("Docker image must be a nonempty reference")
    result = _docker("image", "inspect", "--format", "{{.Id}}", image.strip())
    image_id = result.stdout.strip()
    if not _IMAGE_ID.fullmatch(image_id):
        raise ValueError("Docker image inspect did not return a SHA-256 image ID")
    return image_id


def _host_env():
    return {key: value for key, value in os.environ.items() if key in _HOST_ENV}


def select_environment(config, mission, description):
    """Choose only a Mission-allowed administrator profile; absent means host Runner."""
    _object(config, "config")
    _object(mission, "mission")
    _object(description, "description")
    resources = mission.get("payload", mission).get("resources", {})
    _object(resources, "mission resources")
    name = description.get("environment", resources.get("runner_environment"))
    if name is None:
        return None
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("runner environment must be a configured name")
    allowed = resources.get("allowed_runner_environments", [])
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        raise ValueError("allowed_runner_environments must be a list of names")
    if name not in allowed:
        raise ValueError("runner environment is not allowed by this Mission")
    profiles = _object(config.get("runner_environments", {}), "runner_environments")
    profile = _object(profiles.get(name), f"runner environment {name}")
    if "name" in profile:
        raise ValueError("runner profile must not define its name")
    return {"name": name, **profile}


def prepare_environment(profile):
    """Validate and pin local Docker images without pulling or copying data."""
    _keys(profile, {"name", "image", "python", "mounts", "env", "gpu", "pythonpath", "policy"},
          {"name", "image"}, "runner profile")
    name = profile["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid runner profile name")
    python = _absolute(profile.get("python", "/usr/bin/python3"), "container python")
    gpu = profile.get("gpu", False)
    if type(gpu) is not bool:
        raise ValueError("gpu must be a boolean")
    pinned = {"kind": "docker", "name": name, "image_id": _image_id(profile["image"]),
              "python": python, "mounts": _mounts(profile.get("mounts", [])),
              "env": _env(profile.get("env", {}), "runner env"), "gpu": gpu,
              "pythonpath": _pythonpath(profile.get("pythonpath", []))}
    if "policy" in profile:
        policy = profile["policy"]
        _keys(policy, {"image", "python", "command", "env", "port", "gpu", "pythonpath"},
              {"image", "python", "command", "port"}, "policy")
        command = policy["command"]
        if (not isinstance(command, list) or not command or
                any(not isinstance(item, str) or not item or "\x00" in item for item in command)):
            raise ValueError("policy command must be a nonempty argument list")
        port = policy["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("policy port must be an integer TCP port")
        policy_gpu = policy.get("gpu", False)
        if type(policy_gpu) is not bool:
            raise ValueError("policy gpu must be a boolean")
        pinned["policy"] = {"image_id": _image_id(policy["image"]),
                            "python": _absolute(policy["python"], "policy python"),
                            "command": list(command), "env": _env(policy.get("env", {}), "policy env"),
                            "port": port, "gpu": policy_gpu,
                            "pythonpath": _pythonpath(policy.get("pythonpath", []))}
    encoded = json.dumps(pinned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    pinned["sha256"] = hashlib.sha256(encoded).hexdigest()
    return pinned


def _names(directory):
    digest = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
    base = f"rsi-run-{digest}"
    return base + "-eval", base + "-policy", base + "-net"


def _mount_args(mounts):
    args = []
    for item in mounts:
        args.extend(["--mount", f"type=bind,src={item['source']},dst={item['target']},readonly"])
    return args


def _container_args(name, image_id, directory, mounts, env, gpu, network, alias=None,
                    writable_output=False):
    args = ["run", "--detach", "--name", name, "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--ipc=private",
            "--pids-limit=512", "--user", f"{os.getuid()}:{os.getgid()}",
            "--log-driver=json-file", "--log-opt", "max-size=8m", "--log-opt", "max-file=1",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
            "--workdir", str(directory), "--network", network]
    if alias:
        args.extend(["--network-alias", alias])
    if gpu:
        args.extend(["--gpus", "all"])
    for key, value in {"HOME": "/tmp/home", **env}.items():
        args.extend(["--env", f"{key}={value}"])
    args.extend(_mount_args(mounts))
    args.extend(["--mount", f"type=bind,src={directory},dst={directory},readonly"])
    if writable_output:
        output = directory / "output"
        args.extend(["--mount", f"type=bind,src={output},dst={output}"])
    args.append(image_id)
    return args


def _check_workspace(mounts, directory):
    output = directory / "output"
    for item in mounts:
        for key in ("source", "target"):
            path = Path(item[key])
            if path.is_relative_to(directory) or directory.is_relative_to(path):
                raise ValueError("Docker data mount overlaps the Runner workspace")
            if path.is_relative_to(output) or output.is_relative_to(path):
                raise ValueError("Docker data mount overlaps writable output")
        source = Path(item["source"])
        stat = source.stat()
        if (stat.st_dev != item["source_device"] or stat.st_ino != item["source_inode"] or
                (source.is_dir() and item["source_type"] != "directory") or
                (source.is_file() and item["source_type"] != "file")):
            raise ValueError("Docker data mount changed since preparation")
        if "sha256" in item:
            if stat.st_size != item["source_size"] or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("pinned Docker input file changed")


def _policy_logs(name, directory):
    path = directory / "policy.log"
    try:
        with path.open("wb") as output:
            proc = subprocess.Popen(["docker", "logs", "--tail", "200", name],
                                    env=_host_env(), stdout=output, stderr=subprocess.STDOUT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            output.flush()
            output.truncate(min(path.stat().st_size, _LOG_LIMIT))
    except (OSError, subprocess.SubprocessError):
        pass


def cleanup_environment(pinned, directory):
    """Remove resources named for this run, including after uncertain recovery."""
    if pinned is None:
        return []
    if pinned.get("kind") != "docker":
        raise ValueError("unsupported runner environment")
    directory = Path(directory).resolve()
    evaluation, policy, network = _names(directory)
    errors = []
    if pinned.get("policy") and directory.is_dir():
        _policy_logs(policy, directory)
    for name in (evaluation, policy):
        try:
            result = _docker("rm", "--force", name, timeout=10, check=False)
            if result.returncode and "No such container" not in result.stderr:
                errors.append(f"container {name}: {result.stderr.strip()[:300]}")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"container {name}: {type(exc).__name__}: {exc}")
    try:
        result = _docker("network", "rm", network, timeout=10, check=False)
        if result.returncode and "not found" not in result.stderr and "No such network" not in result.stderr:
            errors.append(f"network {network}: {result.stderr.strip()[:300]}")
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(f"network {network}: {type(exc).__name__}: {exc}")
    if errors and directory.is_dir():
        try:
            (directory / "docker_cleanup_errors.json").write_text(
                json.dumps({"errors": errors}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return errors


@contextmanager
def launch_environment(pinned, directory, argv, env, *, check_cancelled=None):
    """Start isolated containers and yield a Docker exec command for the probe."""
    if check_cancelled is not None and not callable(check_cancelled):
        raise ValueError("check_cancelled must be callable")
    if pinned.get("kind") != "docker" or not _IMAGE_ID.fullmatch(pinned.get("image_id", "")):
        raise ValueError("invalid pinned Docker environment")
    verified = dict(pinned)
    expected = verified.pop("sha256", None)
    actual = hashlib.sha256(json.dumps(verified, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode()).hexdigest()
    if expected != actual:
        raise ValueError("pinned Docker environment hash mismatch")
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(x, str) for x in argv):
        raise ValueError("Runner argv must be a nonempty string list")
    _object(env, "Runner environment")
    directory = Path(directory).resolve()
    output = directory / "output"
    if not directory.is_dir() or not output.is_dir() or output.is_symlink():
        raise ValueError("Runner directory and output directory must exist")
    _check_workspace(pinned["mounts"], directory)
    evaluation, policy_name, network = _names(directory)
    policy = pinned.get("policy")
    provenance = {"executor": "docker", "environment": pinned["name"],
                  "environment_sha256": pinned["sha256"], "image_id": pinned["image_id"],
                  "policy_image_id": policy["image_id"] if policy else None,
                  "network": "internal" if policy else "none"}
    (directory / "docker_environment.json").write_text(
        json.dumps({**provenance, "status": "preparing"}, sort_keys=True), encoding="utf-8")
    launched = False
    try:
        if policy:
            _docker("network", "create", "--internal", network)
            policy_env = dict(policy["env"])
            if policy["pythonpath"]:
                policy_env["PYTHONPATH"] = os.pathsep.join(policy["pythonpath"])
            args = _container_args(policy_name, policy["image_id"], directory, pinned["mounts"],
                                   policy_env, policy["gpu"], network, alias="policy")
            _docker(*args, *policy["command"])
            # Each check has a timeout; model loading itself has no artificial deadline.
            check = ("import socket; socket.create_connection(('127.0.0.1', "
                     f"{policy['port']}), timeout=2).close()")
            while True:
                if check_cancelled is not None:
                    check_cancelled()
                ready = _docker("exec", policy_name, policy["python"], "-c", check,
                                timeout=5, check=False)
                if ready.returncode == 0:
                    break
                if ready.returncode != 1 or not any(marker in ready.stderr for marker in (
                        "ConnectionRefusedError", "TimeoutError", "timed out", "ConnectionResetError")):
                    raise RuntimeError(f"policy readiness check failed: {ready.stderr.strip()[:300]}")
                running = _docker("inspect", "--format", "{{.State.Running}}", policy_name,
                                  timeout=5, check=False)
                if running.returncode or running.stdout.strip() != "true":
                    raise RuntimeError("policy container exited before it became ready")
                time.sleep(0.5)
        container_env = dict(pinned["env"])
        if pinned["pythonpath"]:
            container_env["PYTHONPATH"] = os.pathsep.join(pinned["pythonpath"])
        for key in _RUN_ENV:
            value = env.get(key)
            if isinstance(value, str):
                container_env[key] = value
        if policy:
            container_env.update({"RSI_POLICY_HOST": "policy",
                                  "RSI_POLICY_PORT": str(policy["port"])})
        args = _container_args(evaluation, pinned["image_id"], directory, pinned["mounts"],
                               container_env, pinned["gpu"], network if policy else "none",
                               writable_output=True)
        _docker(*args, pinned["python"], "-c", "import time; time.sleep(2147483647)")
        python_args = ["-s" if item == "-I" else item for item in argv[1:]]
        command = ["docker", "exec", "--workdir", str(directory), evaluation,
                   pinned["python"], *python_args]
        (directory / "docker_environment.json").write_text(
            json.dumps({**provenance, "status": "ready"}, sort_keys=True), encoding="utf-8")
        launched = True
        yield command, _host_env(), provenance
    finally:
        errors = cleanup_environment(pinned, directory)
        provenance["cleanup_errors"] = errors
        try:
            (directory / "docker_environment.json").write_text(
                json.dumps({**provenance, "status": "cleaned" if launched else "setup_failed"},
                           sort_keys=True), encoding="utf-8")
        except OSError:
            pass
