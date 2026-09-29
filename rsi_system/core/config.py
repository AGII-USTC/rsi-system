"""Public RSI defaults with optional per-user runtime configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys


def private_config_dir() -> Path:
    return Path(os.environ.get("RSI_CONFIG_DIR", Path.home() / ".config" / "rsi")).expanduser()


def load_research_config(root: Path, *, config_dir: Path | None = None) -> dict:
    public = json.loads((root / "deployment" / "rsi" / "research.json").read_text(encoding="utf-8"))
    if not isinstance(public, dict):
        raise ValueError("public research config must be a JSON object")
    private_path = (config_dir or private_config_dir()) / "research.json"
    if private_path.is_file():
        private = json.loads(private_path.read_text(encoding="utf-8"))
        if not isinstance(private, dict):
            raise ValueError("private research config must be a JSON object")
        public.update(private)
    if not public.get("pi_executable") or public["pi_executable"] == "pi":
        public["pi_executable"] = shutil.which("pi") or "pi"
    if not public.get("python") or public["python"] == "python":
        public["python"] = sys.executable
    for key, default in (("runtime_recovery_max_attempts", 2),
                         ("runtime_recovery_retry_seconds", 30),
                         ("runtime_recovery_max_retry_seconds", 300)):
        value = public.setdefault(key, default)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if public["runtime_recovery_max_retry_seconds"] < public["runtime_recovery_retry_seconds"]:
        raise ValueError("maximum recovery delay must be at least the initial delay")
    return public
