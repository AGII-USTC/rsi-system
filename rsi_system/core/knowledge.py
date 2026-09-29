"""Bounded text lookup and candidate-result writeback for the project wiki."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml

from .schema import safe_id


_WIKILINK = re.compile(r"(?<!!)\[\[([^]|]+)(?:\|[^]]+)?\]\]")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_PAGE_DIRS = ("concepts", "findings", "experiments", "queries")
_RESULT_STATUSES = {"completed", "failed", "interrupted", "analyzed"}
_MAX_FILE_BYTES = 256_000


def _project(root: Path | str) -> Path:
    project = Path(root).resolve()
    if not (project / "knowledge" / "SCHEMA.md").is_file():
        raise ValueError("root must contain knowledge/SCHEMA.md")
    return project


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_file(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not relative or Path(relative).is_absolute() or "\\" in relative or ".." in parts:
        raise ValueError("evidence path must be project-relative")
    path = root / relative
    if not _inside(root, path.resolve()) or not path.is_file() or any(
        candidate.is_symlink() for candidate in (root / Path(*parts[:index]) for index in range(1, len(parts) + 1))
    ):
        raise ValueError(f"evidence path is missing or outside project: {relative}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest()


def _frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}
    try:
        parsed = yaml.load(match.group(1), Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_paths(meta: Mapping[str, Any]) -> list[str]:
    source = meta.get("sources", "")
    if isinstance(source, list):
        return source if all(isinstance(item, str) and item for item in source) else []
    if not isinstance(source, str):
        return []
    if not (source.startswith("[") and source.endswith("]")):
        return []
    try:
        values = json.loads(source)
    except json.JSONDecodeError:
        values = [part.strip().strip("\"'") for part in source[1:-1].split(",") if part.strip()]
    return values if isinstance(values, list) and all(isinstance(item, str) and item for item in values) else []


def _search_files(root: Path) -> list[Path]:
    wiki = root / "knowledge"
    files = [path for folder in (*_PAGE_DIRS, "raw") for path in (wiki / folder).rglob("*.md")]
    files.extend((root / "docs" / "research").glob("*.md"))
    return sorted(path for path in files if path.is_file() and not path.is_symlink() and _inside(root, path.resolve()))


def search(root: Path | str, query: str, limit: int = 5) -> list[dict[str, object]]:
    """Case-insensitive substring search; results carry bounded verbatim excerpts."""
    project = _project(root)
    term = query.strip().casefold()
    if not term or len(term) > 200:
        raise ValueError("query must contain 1-200 characters")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    results: list[dict[str, object]] = []
    for path in _search_files(project):
        if path.stat().st_size > _MAX_FILE_BYTES:
            continue
        content = path.read_text(encoding="utf-8")
        lower = content.casefold()
        offset = lower.find(term)
        if offset < 0:
            continue
        meta = _frontmatter(content)
        relative = path.relative_to(project).as_posix()
        line = content.count("\n", 0, offset) + 1
        start = max(0, offset - 160)
        end = min(len(content), offset + max(len(query), 1) + 320)
        excerpt = content[start:end].replace("\n", " ").strip()
        title = meta.get("title") or next((line.lstrip("# ") for line in content.splitlines() if line.startswith("# ")), path.stem)
        results.append({
            "path": relative,
            "title": title,
            "status": meta.get("status", "source"),
            "line": line,
            "excerpt": excerpt[:500],
            "sha256": _sha256(path),
            "sources": _source_paths(meta),
            "match_count": lower.count(term),
        })
    results.sort(key=lambda item: (-int(item["match_count"]), str(item["path"])))
    for item in results:
        del item["match_count"]
    return results[:limit]


def context(root: Path | str, query: str, limit: int = 5) -> dict[str, object]:
    """Return search evidence with an explicit lexical-search boundary."""
    return {"query": query, "retrieval": "casefolded substring", "results": search(root, query, limit)}


def _one_line(value: object, label: str, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    result = " ".join(value.split())
    if len(result) > max_length:
        raise ValueError(f"{label} is too long")
    return result


def _atomic_replace(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_candidate(root: Path | str, result: Mapping[str, object]) -> dict[str, object]:
    """Record a run summary as candidate evidence, once per experiment ID.

    Required result keys: experiment_id, status, summary, evidence. Evidence is
    a list of {path, sha256} entries under the project root. Existing pages
    are immutable through this API; only review by the user or designated acceptance agent may promote findings.
    """
    project = _project(root)
    experiment_id = safe_id(result.get("experiment_id"))
    status = result.get("status")
    if status not in _RESULT_STATUSES:
        raise ValueError(f"run status must be one of {sorted(_RESULT_STATUSES)}")
    summary = _one_line(result.get("summary"), "summary")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise ValueError("evidence must contain 1-20 path/hash objects")
    checked: list[dict[str, str]] = []
    for entry in evidence:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise ValueError("each evidence entry needs a path and sha256")
        path = _safe_file(project, entry["path"])
        digest = _sha256(path)
        if entry.get("sha256") != digest:
            raise ValueError(f"evidence hash mismatch: {entry['path']}")
        checked.append({"path": path.relative_to(project).as_posix(), "sha256": digest})
    checked.sort(key=lambda item: item["path"])
    if len({entry["path"] for entry in checked}) != len(checked):
        raise ValueError("duplicate evidence paths")
    canonical = {"experiment_id": experiment_id, "status": status, "summary": summary, "evidence": checked}
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    wiki = project / "knowledge"
    relative = f"knowledge/findings/run-{experiment_id}.md"
    page = project / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    lock = project / ".rsi" / "knowledge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        index = wiki / "index.md"
        index_text = index.read_text(encoding="utf-8")
        anchor = "## Experiments\n"
        if anchor not in index_text:
            raise ValueError("knowledge index lacks Experiments section")
        created = False
        if page.exists():
            if f"result_sha256: {fingerprint}\n" not in page.read_text(encoding="utf-8"):
                raise ValueError("candidate already exists with different result")
        else:
            today = date.today().isoformat()
            source_list = ", ".join(entry["path"] for entry in checked)
            body = (
                f"---\ntitle: Candidate result {experiment_id}\ncreated: {today}\nupdated: {today}\n"
                f"type: finding\nstatus: candidate\ntags: [experiment, evidence]\n"
                f"sources: [{source_list}]\nresult_sha256: {fingerprint}\n---\n\n"
                f"# Candidate result {experiment_id}\n\n## Run record\n\n"
                f"- Experiment: `{experiment_id}`\n- Run status: `{status}`\n"
                f"- Summary: {summary}\n\n## Evidence\n\n"
                + "".join(f"- `{entry['path']}` (SHA-256 `{entry['sha256']}`)\n" for entry in checked)
                + "\nThis is an unreviewed run summary, not a confirmed finding.\n\n"
                "Related: [[findings/current-evidence]], [[experiments/rsi-first-loop]].\n"
            )
            descriptor, temporary = tempfile.mkstemp(prefix=f".{page.name}.", dir=page.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, page)
                created = True
            finally:
                os.unlink(temporary)
        today = date.today().isoformat()
        link = f"[[findings/run-{experiment_id}]]"
        if link not in index_text:
            line = f"- {link} — Candidate result of `{experiment_id}` ({status}).\n"
            index_text = index_text.replace(anchor, line + "\n" + anchor, 1)
            index_text = re.sub(r"Last updated: .*? \| Total pages: (\d+)",
                                lambda match: f"Last updated: {today} | Total pages: {int(match.group(1)) + 1}",
                                index_text, count=1)
            _atomic_replace(index, index_text)
        log = wiki / "log.md"
        marker = f"create | candidate result {experiment_id}"
        if marker not in log.read_text(encoding="utf-8"):
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## [{today}] {marker}\n\n- {relative}; status `{status}`; evidence SHA-256 verified.\n")
                stream.flush()
                os.fsync(stream.fileno())
    return {"path": relative, "created": created, "status": "candidate", "result_sha256": fingerprint}


def lint(root: Path | str) -> list[dict[str, str]]:
    """Check resolvable links, required frontmatter, sources, and index entries."""
    project = _project(root)
    wiki = project / "knowledge"
    pages = sorted(path for folder in (*_PAGE_DIRS, "raw") for path in (wiki / folder).rglob("*.md"))
    index = (wiki / "index.md").read_text(encoding="utf-8")
    issues: list[dict[str, str]] = []
    for page in pages:
        relative = page.relative_to(wiki).as_posix()
        text = page.read_text(encoding="utf-8")
        meta = _frontmatter(text)
        for key in ("title", "created", "updated", "type", "status", "tags", "sources"):
            if key not in meta or (key != "sources" and not meta[key]):
                issues.append({"path": relative, "issue": f"missing frontmatter: {key}"})
        for target in _WIKILINK.findall(text):
            candidate = (wiki / f"{target}.md").resolve()
            if not _inside(wiki.resolve(), candidate) or not candidate.is_file():
                issues.append({"path": relative, "issue": f"broken wikilink: {target}"})
        for source in _source_paths(meta):
            if source.startswith(("https://", "http://")):
                continue
            source_path = (project / source).resolve()
            if not _inside(project, source_path) or not source_path.is_file():
                issues.append({"path": relative, "issue": f"missing source: {source}"})
        if f"[[{relative.removesuffix('.md')}]]" not in index:
            issues.append({"path": relative, "issue": "not listed in index"})
    return issues
