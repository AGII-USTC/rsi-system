"""Isolated candidate branches with fixed code and evaluation boundaries."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path
import subprocess
import os
import signal

from .schema import safe_id
from . import sandbox as isolation

ALLOWED_FILES = {"experiments/residual_memory/memory.py", "experiments/runtime_harness/scheduler.py"}
CHECKS = ("experiments/residual_memory/tests/test_core.py", "experiments/runtime_harness/test_core.py")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@dataclass
class WorkspaceInfo:
    path: Path
    repo_root: Path
    base_commit: str
    branch: str
    cycle_id: str

    def to_dict(self):
        return {key: str(value) for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "path": Path(value["path"]), "repo_root": Path(value["repo_root"])})


def create_workspace(repo_root: Path, workspace_root: Path, cycle_id: str, base_commit: str) -> WorkspaceInfo:
    safe_id(cycle_id)
    commit = git(repo_root, "rev-parse", "--verify", f"{base_commit}^{{commit}}")
    path, branch = workspace_root.resolve() / cycle_id, f"rsi/{cycle_id}"
    workspace_root.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        subprocess.run(["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(path), commit],
                       check=True, capture_output=True, text=True)
    if git(path, "branch", "--show-current") != branch:
        raise ValueError("candidate workspace identity mismatch")
    subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", commit, "HEAD"], check=True)
    return WorkspaceInfo(path, repo_root, commit, branch, cycle_id)


def inspect_candidate(info: WorkspaceInfo) -> dict:
    paths = set(git(info.path, "diff", "--name-only", info.base_commit).splitlines())
    paths |= set(git(info.path, "ls-files", "--others", "--exclude-standard").splitlines())
    if paths - ALLOWED_FILES:
        raise ValueError(f"candidate modified protected paths: {sorted(paths - ALLOWED_FILES)}")
    for path in paths:
        target = info.path / path
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(info.path.resolve()):
            raise ValueError("candidate file must be a regular implementation file")
    diff = git(info.path, "diff", info.base_commit, "--", *sorted(ALLOWED_FILES))
    if len(diff.encode()) > 150_000:
        raise ValueError("candidate diff exceeds review budget")
    return {"files": sorted(paths), "diff": diff, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "base_commit": info.base_commit, "head": git(info.path, "rev-parse", "HEAD")}


def commit_candidate(info: WorkspaceInfo, *, python: str, evidence_dir: Path, sandbox: bool = True) -> dict:
    evidence = inspect_candidate(info)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out, err = evidence_dir / "tests.stdout", evidence_dir / "tests.stderr"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        env = isolation.candidate_environment() if sandbox else os.environ.copy()
        env.update(PYTHONPATH=str(info.path), CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1")
        argv = [python, "-m", "pytest", *CHECKS, "-q", "--import-mode=importlib", "-p", "no:cacheprovider"]
        if sandbox:
            argv = isolation.command(argv, cwd=info.path,
                                     protected=[info.repo_root / ".rsi", info.repo_root / ".git"])
        child = subprocess.Popen(argv, cwd=info.path,
                                 stdout=stdout, stderr=stderr, env=env, start_new_session=True)
        try:
            code = child.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise ValueError("candidate tests timed out")
    if code != 0:
        raise ValueError("candidate failed fixed tests; inspect test evidence")
    if inspect_candidate(info)["diff_sha256"] != evidence["diff_sha256"]:
        raise ValueError("candidate changed during tests")
    changed = git(info.path, "status", "--porcelain", "--untracked-files=no")
    if changed:
        subprocess.run(["git", "-C", str(info.path), "add", "--", *sorted(ALLOWED_FILES)], check=True)
        subprocess.run(["git", "-C", str(info.path), "-c", "user.name=RSI coding agent",
                        "-c", "user.email=rsi@localhost", "commit", "-m", f"RSI candidate {info.cycle_id}"],
                       check=True, capture_output=True)
    if inspect_candidate(info)["diff_sha256"] != evidence["diff_sha256"]:
        raise ValueError("committed candidate differs from tested source")
    evidence.update(source_commit=git(info.path, "rev-parse", "HEAD"), test_exit_code=code,
                    test_stdout=str(out), test_stderr=str(err))
    return evidence
