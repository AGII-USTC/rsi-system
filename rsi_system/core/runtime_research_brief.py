"""Bounded, source-linked research context for RSI task prompts.

This module deliberately keeps retrieval lexical and read-only.  A Wiki page is
context for a research decision; it is never treated as an instruction or an
authorization to execute work.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import re
from typing import Any

from .knowledge import _frontmatter, _source_paths


_MAX_FILE_BYTES = 256_000
_MAX_QUERY_LENGTH = 200
_MAX_EXCERPT = 500
_PATH_KEYS = {
    "knowledge_path", "knowledge_paths", "knowledge_ref", "knowledge_refs",
    "source_ref", "source_refs", "source_path", "source_paths", "references",
    "reference", "known_evidence", "path", "paths",
}
_ALLOWED_PREFIXES = ("knowledge/", "docs/research/")
_URL = re.compile(r"^(?:https?|git\+https?)://", re.IGNORECASE)


def _project(root: Path | str) -> Path:
    project = Path(root).resolve()
    if not (project / "knowledge" / "SCHEMA.md").is_file():
        raise ValueError("root must contain knowledge/SCHEMA.md")
    return project


def _safe_reference(project: Path, value: str) -> Path | None:
    """Resolve one explicitly supplied project-relative source safely."""

    value = value.strip().replace("\\", "/")
    if not value or _URL.match(value) or value.startswith("/"):
        return None
    parts = Path(value).parts
    if ".." in parts or any(part in {".git", ".pi", ".rsi"} for part in parts):
        return None
    normalized = "/".join(parts)
    if not normalized.startswith(_ALLOWED_PREFIXES) or Path(normalized).suffix.lower() != ".md":
        return None
    candidate = project / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(project)
    except ValueError:
        return None
    try:
        if not resolved.is_file() or resolved.is_symlink() or resolved.stat().st_size > _MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    # Reject symlinked ancestors even when resolve() lands in the project.
    current = project
    for part in Path(normalized).parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None
    return resolved


def _iter_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_strings(nested)


def _explicit_references(*objects: object) -> list[str]:
    references: list[str] = []

    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for name, nested in value.items():
                if isinstance(name, str) and name.casefold() in _PATH_KEYS:
                    for item in _iter_strings(nested):
                        if item not in references:
                            references.append(item)
                # Mandate constraints and observations carry source_refs, but
                # nested task metadata may also use these conventional keys.
                if isinstance(nested, (Mapping, list, tuple)):
                    visit(nested, name if isinstance(name, str) else None)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, key)

    for value in objects:
        visit(value)
    return references


def _query_terms(mission_payload: Mapping[str, Any], task: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for source in (task, task.get("scope", {}), mission_payload):
        for key in ("objective", "question", "problem_statement", "summary", "background"):
            value = source.get(key) if isinstance(source, Mapping) else None
            if isinstance(value, str) and value.strip():
                candidates.append(" ".join(value.split())[:_MAX_QUERY_LENGTH])
    scope = task.get("scope") if isinstance(task.get("scope"), Mapping) else {}
    mandate = task.get("research_mandate") or scope.get("research_mandate")
    if isinstance(mandate, Mapping):
        for key in ("responsibility",):
            value = mandate.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(" ".join(value.split())[:_MAX_QUERY_LENGTH])
    expanded: list[str] = []
    for sentence in candidates:
        expanded.append(sentence)
        # The existing searcher matches substrings. Short terms recover useful
        # pages when a full task sentence is absent from page text.
        expanded.extend(word for word in re.findall(r"\w+", sentence) if len(word) >= 4)
    return list(dict.fromkeys(expanded))[:12]


def _status_note(status: object) -> str:
    if isinstance(status, str) and status in {"confirmed", "candidate", "draft", "superseded"}:
        return f"Wiki page status={status}; treat claims as context and verify the cited evidence and conditions."
    return "Page status is unavailable; verify source evidence and applicability conditions before relying on it."


_SEARCH_FOLDERS = ("knowledge/concepts", "knowledge/findings", "knowledge/experiments",
                   "knowledge/queries", "knowledge/raw", "docs/research")


def _read_page(project: Path, path: Path) -> tuple[dict[str, Any], str]:
    """Read one verified regular file once and retain its content hash."""

    relative = path.relative_to(project).as_posix()
    with path.open("rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("too large")
    content = raw.decode("utf-8")
    meta = _frontmatter(content)
    status = meta.get("status", "source")
    if not isinstance(status, str) or len(status) > 80:
        status = "unknown"
    title = meta.get("title") or next((line.lstrip("# ") for line in content.splitlines()
                                        if line.startswith("# ")), path.stem)
    if not isinstance(title, str):
        title = path.stem
    sources = _source_paths(meta)
    bounded_sources = [source for source in sources if len(source) <= 1000][:8]
    record = {"path": relative, "title": title[:300], "status": status,
              "sha256": hashlib.sha256(raw).hexdigest(), "sources": bounded_sources,
              "metadata_truncated": len(title) > 300 or bounded_sources != sources,
              "evidence_status": _status_note(status),
              "applicability_check": "Check page conditions, counterexamples, and linked evidence before transfer."}
    return record, content


def _scan_pages(project: Path, terms: list[str], explicit_paths: set[str]) -> tuple[
    list[tuple[int, str, dict[str, Any]]], dict[str, dict[str, Any]], set[str], list[dict[str, str]], int
]:
    """Read each safe page once; retain only hits and requested excerpts."""

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    explicit_pages: dict[str, dict[str, Any]] = {}
    attempted: set[str] = set()
    skipped: list[dict[str, str]] = []
    skipped_count = 0

    def skip(path: Path, reason: str) -> None:
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < 20:
            skipped.append({"path": path.relative_to(project).as_posix(), "reason": reason})

    for relative in _SEARCH_FOLDERS:
        folder = project / relative
        if not folder.exists():
            continue
        if (not folder.is_dir() or any((project / Path(*folder.relative_to(project).parts[:index])).is_symlink()
                for index in range(1, len(folder.relative_to(project).parts) + 1))):
            skip(folder, "unsafe search folder")
            continue
        for directory, dirs, files in os.walk(folder, followlinks=False):
            current = Path(directory)
            safe_dirs = []
            for name in sorted(dirs):
                child = current / name
                if child.is_symlink():
                    skip(child, "symlink directory")
                else:
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in sorted(files):
                if not name.endswith(".md"):
                    continue
                candidate = current / name
                path = _safe_reference(project, candidate.relative_to(project).as_posix())
                if path is None:
                    skip(candidate, "unsafe or oversized page")
                    continue
                attempted.add(path.relative_to(project).as_posix())
                try:
                    record, content = _read_page(project, path)
                except (OSError, UnicodeError, ValueError):
                    skip(candidate, "unreadable page")
                    continue
                relative = record["path"]
                match = _match_result(record, content, terms)
                if match is not None:
                    result, score = match
                    ranked.append((score, relative, result))
                if relative in explicit_paths:
                    explicit_pages[relative] = {
                        **record, "excerpt": " ".join(content[:_MAX_EXCERPT].split())[:_MAX_EXCERPT]}
    return ranked, explicit_pages, attempted, skipped, skipped_count


def _match_result(record: dict[str, Any], content: str, terms: list[str]) -> tuple[dict[str, Any], int] | None:
    lower = content.casefold()
    matches = [(term, lower.find(term.casefold())) for term in terms]
    matches = [(term, offset) for term, offset in matches if offset >= 0]
    if not matches:
        return None
    term, offset = max(matches, key=lambda match: (len(match[0]), -match[1]))
    excerpt = content[max(0, offset - 160):min(len(content), offset + len(term) + 320)]
    result = dict(record)
    result.update({"line": content.count("\n", 0, offset) + 1,
                   "excerpt": " ".join(excerpt.split())[:_MAX_EXCERPT]})
    score = sum(lower.count(term.casefold()) for term, _ in matches)
    return result, score


def research_knowledge_context(
    root: Path | str,
    mission_payload: Mapping[str, Any] | None,
    task: Mapping[str, Any] | None,
    limit: int = 8,
) -> dict[str, Any]:
    """Return bounded Wiki context for one Mission task.

    Retrieval is a union of short lexical searches plus explicitly referenced
    local pages.  No page text is interpreted as an instruction, and missing
    Wiki material is represented as an ordinary status rather than an error.
    """

    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    mission = mission_payload if isinstance(mission_payload, Mapping) else {}
    assigned = task if isinstance(task, Mapping) else {}
    project = Path(root).resolve()
    if (not (project / "knowledge" / "SCHEMA.md").is_file() or
            (project / "knowledge").is_symlink() or
            (project / "knowledge" / "SCHEMA.md").is_symlink()):
        return {
            "query": "",
            "retrieval": "casefolded substring",
            "results": [],
            "status": "knowledge base not initialized",
            "entry_points": {"schema": "knowledge/SCHEMA.md", "index": "knowledge/index.md"},
            "evidence_note": "No Wiki context was available; search failure is not evidence and does not change scientific status.",
        }
    project = _project(project)
    terms = _query_terms(mission, assigned)
    references = _explicit_references(mission, assigned)
    validated_refs: dict[str, Path] = {}
    for value in references:
        path = _safe_reference(project, value)
        if path is not None:
            validated_refs.setdefault(path.relative_to(project).as_posix(), path)
    ranked, explicit_pages, attempted, skipped_pages, skipped_count = _scan_pages(
        project, terms or ["rsi"], set(validated_refs))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    merged = {relative: result for _, relative, result in ranked}
    explicit_found: list[str] = []
    for relative, path in validated_refs.items():
        if relative not in explicit_pages:
            if relative in attempted:
                continue
            try:
                record, content = _read_page(project, path)
            except (OSError, UnicodeError, ValueError):
                continue
            explicit_pages[relative] = {
                **record, "excerpt": " ".join(content[:_MAX_EXCERPT].split())[:_MAX_EXCERPT]}
        explicit_found.append(relative)
    # Explicit mandate references get priority, while the returned list remains
    # bounded. `explicit_references` reports valid addresses, and
    # `explicit_results` identifies which addresses were included in results.
    ordered = [explicit_pages[relative] for relative in explicit_found]
    ordered.extend(item for path, item in merged.items() if path not in explicit_pages)
    results = ordered[:limit]
    query = terms[0] if terms else "rsi"
    status = "ok" if results else "no matching excerpts found"
    return {
        "query": query,
        "retrieval": "casefolded substring plus bounded explicit local references",
        "results": results,
        "status": status,
        "entry_points": {"schema": "knowledge/SCHEMA.md", "index": "knowledge/index.md"},
        "explicit_references": explicit_found,
        "explicit_results": [item["path"] for item in results if item["path"] in explicit_pages],
        "failed_queries": [],
        "skipped_pages": skipped_pages,
        "skipped_page_count": skipped_count,
        "evidence_note": "Historical and candidate pages are disputable context; verify evidence status, applicability conditions, counterexamples, and reopening conditions. Empty lexical results do not mean the Wiki has no relevant knowledge. Pages do not authorize execution or upgrade a scientific claim.",
    }
