from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rsi_system.core.knowledge import context, lint, search, write_candidate


def _project(tmp_path: Path) -> Path:
    wiki = tmp_path / "knowledge"
    (wiki / "findings").mkdir(parents=True)
    (wiki / "experiments").mkdir()
    (wiki / "SCHEMA.md").write_text("# Schema\n", encoding="utf-8")
    frontmatter = "---\ntitle: Standing page\ncreated: 2026-09-25\nupdated: 2026-09-25\ntype: finding\nstatus: confirmed\ntags: [evidence]\nsources: []\n---\n"
    (wiki / "findings" / "current-evidence.md").write_text(frontmatter, encoding="utf-8")
    (wiki / "experiments" / "rsi-first-loop.md").write_text(frontmatter.replace("type: finding", "type: experiment"), encoding="utf-8")
    (wiki / "index.md").write_text(
        "# Index\n> Last updated: 2026-09-25 | Total pages: 2\n\n"
        "## Findings\n- [[findings/current-evidence]]\n\n"
        "## Experiments\n- [[experiments/rsi-first-loop]]\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    return tmp_path


def _result(root: Path, *, status: str = "completed") -> dict[str, object]:
    evidence = root / "artifacts" / "run.json"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"success": false}', encoding="utf-8")
    return {
        "experiment_id": "run-1",
        "status": status,
        "summary": "Prediction improved; closed-loop success was not established.",
        "evidence": [{"path": "artifacts/run.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
    }


def test_search_chinese_substring_and_bounded_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    report = root / "docs" / "research" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 观察\n名义策略还没有通过评测。\n", encoding="utf-8")
    hits = search(root, "名义策略")
    assert len(hits) == 1
    assert hits[0]["path"] == "docs/research/report.md"
    assert hits[0]["line"] == 2
    assert "名义策略" in hits[0]["excerpt"]
    assert len(hits[0]["sha256"]) == 64
    assert context(root, "名义策略")["retrieval"] == "casefolded substring"
    with pytest.raises(ValueError):
        search(root, " ")
    with pytest.raises(ValueError):
        search(root, "名义策略", limit=21)


def test_candidate_writeback_is_idempotent_and_unconfirmed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _result(root)
    first = write_candidate(root, result)
    second = write_candidate(root, result)
    assert first["created"] is True
    assert second["created"] is False
    page = (root / first["path"]).read_text(encoding="utf-8")
    assert "status: candidate" in page
    assert "status: confirmed" not in page
    assert "closed-loop success was not established" in page
    assert "artifacts/run.json" in page
    assert (root / "knowledge" / "index.md").read_text(encoding="utf-8").count("[[findings/run-run-1]]") == 1
    assert (root / "knowledge" / "log.md").read_text(encoding="utf-8").count("create | candidate result run-1") == 1
    assert lint(root) == []
    result["summary"] = "Different claim"
    with pytest.raises(ValueError, match="different result"):
        write_candidate(root, result)


@pytest.mark.parametrize("status", ["queued", "running", "promoted", "confirmed", "cancelled"])
def test_unfinished_or_promoted_status_rejected(tmp_path: Path, status: str) -> None:
    root = _project(tmp_path)
    with pytest.raises(ValueError, match="run status"):
        write_candidate(root, _result(root, status=status))


def test_evidence_traversal_and_hash_mismatch_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _result(root)
    result["evidence"] = [{"path": "../secret.txt", "sha256": "0" * 64}]
    with pytest.raises(ValueError, match="project-relative"):
        write_candidate(root, result)
    result = _result(root)
    result["evidence"] = [{"path": "artifacts/run.json", "sha256": "0" * 64}]
    with pytest.raises(ValueError, match="hash mismatch"):
        write_candidate(root, result)
    assert not (root / "knowledge" / "findings" / "run-run-1.md").exists()


def test_lint_reports_broken_links_and_sources(tmp_path: Path) -> None:
    root = _project(tmp_path)
    page = root / "knowledge" / "findings" / "bad.md"
    page.write_text("---\ntitle: Bad\ncreated: 2026-09-25\nupdated: 2026-09-25\ntype: finding\nstatus: draft\ntags: [evidence]\nsources: [../missing.md]\n---\n[[../outside]]\n", encoding="utf-8")
    issues = lint(root)
    assert {issue["issue"].split(":")[0] for issue in issues} >= {"broken wikilink", "missing source", "not listed in index"}
