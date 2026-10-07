"""Linux isolation for candidate code and its tests, independent of agent prompts."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import sys
import json


def gpu_device_paths(device_root: Path = Path('/dev')) -> list[Path]:
    """Expose NVIDIA compute devices without granting the rest of host /dev."""
    candidates = [device_root / name for name in (
        'nvidiactl', 'nvidia-uvm', 'nvidia-uvm-tools', 'nvidia-modeset')]
    candidates.extend(path for path in device_root.glob('nvidia[0-9]*')
                      if path.name.removeprefix('nvidia').isdigit())
    candidates.extend((device_root / 'nvidia-caps').glob('nvidia-cap[0-9]*'))
    return sorted(path for path in candidates if path.is_char_device())


def candidate_environment() -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "MUJOCO_GL"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(HOME="/tmp", PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    return env


def agent_command(argv: list[str], *, workspace: Path, candidate_workspace: Path | None = None,
                  agent_config: Path | None = None,
                  external_manifest: Path | None = None, gpu: bool = False) -> list[str]:
    """Keep shell exploration writable only in the task and assigned candidate workspaces."""
    bwrap = shutil.which("bwrap")
    if not bwrap or sys.platform != "linux":
        raise ValueError("agent shell execution requires Linux bubblewrap")
    args = [bwrap, "--die-with-parent", "--new-session", "--ro-bind", "/", "/",
            "--proc", "/proc", "--dev", "/dev"]
    if gpu:
        devices = gpu_device_paths()
        if any(path.parent.name == 'nvidia-caps' for path in devices):
            args += ['--dir', '/dev/nvidia-caps']
        for path in devices:
            args += ['--dev-bind', str(path), str(path)]
    for path in (workspace, candidate_workspace, agent_config):
        if path is not None:
            actual = path.resolve()
            args += ["--bind", str(actual), str(actual)]
    if external_manifest is not None:
        manifest = json.loads(Path(external_manifest).read_text())
        for item in manifest.get("roots", []):
            if item.get("mode") != "read-only":
                raise ValueError("external evidence mounts must be read-only")
            actual = Path(item["path"]).resolve(strict=True)
            if not actual.is_dir() or Path(item["path"]).is_symlink():
                raise ValueError("external evidence root must be a real directory")
            args += ["--ro-bind", str(actual), str(actual)]
    args += ["--chdir", str((candidate_workspace or workspace).resolve()), "--", *argv]
    return args


def command(argv: list[str], *, cwd: Path, writable: list[Path] = (),
            readable: list[Path] = (), protected: list[Path] = ()) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap or sys.platform != "linux":
        raise ValueError("candidate execution requires Linux bubblewrap")
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-net", "--unshare-pid",
            "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for hidden in (".ssh", ".pi", ".codex", ".config", "embodied-memory-stage2/outputs"):
        path = Path.home() / hidden
        if path.is_dir():
            args += ["--tmpfs", str(path)]
    # Mask the control plane before exposing only this job's pinned inputs.
    hidden_paths = {p.resolve() for p in protected}
    hidden_paths.update(p.resolve() for p in (cwd / ".rsi", cwd / ".git") if p.is_dir())
    for path in sorted(hidden_paths):
        if path.is_dir():
            args += ["--tmpfs", str(path)]
    for path in (cwd, *readable):
        actual = path.resolve()
        if path in readable or any(actual.is_relative_to(hidden) for hidden in hidden_paths):
            args += ["--ro-bind", str(actual), str(actual)]
    for path in writable:
        args += ["--bind", str(path.resolve()), str(path.resolve())]
    args += ["--chdir", str(cwd.resolve()), "--", *argv]
    return args
