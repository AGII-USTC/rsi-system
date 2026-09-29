"""Immutable source and input snapshots for one run."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
import shutil
import subprocess
import tarfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file() or p.is_symlink())
    for file in files:
        if file.is_symlink():
            raise ValueError(f"symlink in input data: {file}")
        digest.update(str(file.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(file)))
    return digest.hexdigest()


def snapshot_source(repo_root: Path, destination: Path, source_commit: str | None = None) -> str:
    root = Path(repo_root).resolve(strict=True)
    if source_commit is not None and re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("source_commit must be an exact Git commit SHA")
    revision = (source_commit or "HEAD") + "^{commit}"
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--verify", revision], text=True).strip()
    archive = subprocess.check_output(["git", "-C", str(root), "archive", "--format=tar", commit])
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("unsupported path in committed source archive")
        tar.extractall(destination, filter="data")
    return commit


def snapshot_inputs(paths: dict[str, Path], destination: Path) -> tuple[dict[str, Path], dict[str, str]]:
    if not paths:
        return {}, {}
    destination.mkdir(parents=True, exist_ok=False)
    if set(paths) == {"scale_file"}:
        source = paths["scale_file"]
        before = sha256_file(source)
        copied = destination / "response_scale_dev.json"
        shutil.copy2(source, copied)
        if sha256_file(copied) != before or sha256_file(source) != before:
            raise ValueError("pi05 response scale changed while snapshotting")
        return {"scale_file": copied}, {"scale_file_sha256": before}
    data = paths["data_dir"]
    data_hash_before = sha256_tree(data)
    copied_data = destination / "dev_data"
    shutil.copytree(data, copied_data)
    data_hash = sha256_tree(copied_data)
    if data_hash != data_hash_before:
        raise ValueError("development data changed while snapshotting")
    model_dir = destination / "model"
    model_dir.mkdir()
    model = model_dir / "nominal_model.json"
    fit = model_dir / "fit.json"
    shutil.copy2(paths["model_path"], model)
    shutil.copy2(paths["fit_path"], fit)
    hashes = {"dev_data_sha256": data_hash, "model_sha256": sha256_file(model),
              "fit_sha256": sha256_file(fit)}
    if hashes["model_sha256"] != sha256_file(paths["model_path"]) or hashes["fit_sha256"] != sha256_file(paths["fit_path"]):
        raise ValueError("nominal model changed while snapshotting")
    return {"data_dir": copied_data, "model_path": model}, hashes
