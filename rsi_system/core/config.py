"""Public RSI defaults with optional per-user runtime configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from .pi_agent import validate_disabled_tools


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
    disabled_tools = public.setdefault("agent_disabled_tools", {})
    if not isinstance(disabled_tools, dict):
        raise ValueError("agent_disabled_tools must be an object")
    for role, tools in disabled_tools.items():
        validate_disabled_tools(role, tools)
    context_limit = public.setdefault("agent_context_limit_tokens", None)
    if context_limit is not None and (type(context_limit) is not int or context_limit <= 16384):
        raise ValueError("agent_context_limit_tokens must be null or an integer greater than the 16384 output reserve")
    for key, default in (("runtime_recovery_max_attempts", 2),
                         ("runtime_recovery_retry_seconds", 30),
                         ("runtime_recovery_max_retry_seconds", 300)):
        value = public.setdefault(key, default)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if public["runtime_recovery_max_retry_seconds"] < public["runtime_recovery_retry_seconds"]:
        raise ValueError("maximum recovery delay must be at least the initial delay")
    roots = public.setdefault("external_readonly_roots", [])
    if not isinstance(roots, list):
        raise ValueError("external_readonly_roots must be a list")
    names, paths = set(), []
    for item in roots:
        if (not isinstance(item, dict) or set(item) != {"name", "path"} or
                not isinstance(item["name"], str) or not item["name"].strip() or
                not isinstance(item["path"], str) or not Path(item["path"]).is_absolute()):
            raise ValueError("external_readonly_roots entries require a name and absolute path")
        path = Path(item["path"]).resolve()
        if item["name"] in names or any(path.is_relative_to(p) or p.is_relative_to(path) for p in paths):
            raise ValueError("external_readonly_roots must have unique names and non-overlapping paths")
        names.add(item["name"])
        paths.append(path)
    return public
