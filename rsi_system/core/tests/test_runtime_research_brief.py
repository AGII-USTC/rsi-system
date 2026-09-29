from __future__ import annotations

import hashlib
from pathlib import Path

from rsi_system.core.pi_agent import TOOLS
from rsi_system.core.runtime_research_brief import research_knowledge_context
from rsi_system.core.runtime_roles import ROLE_GUIDANCE


def _project(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    for folder in ("concepts", "findings", "experiments", "queries", "raw"):
        (knowledge / folder).mkdir(parents=True)
    (knowledge / "SCHEMA.md").write_text("# Schema\n", encoding="utf-8")
    (knowledge / "index.md").write_text("# Index\n", encoding="utf-8")
    page = knowledge / "queries" / "open-question.md"
    page.write_text(
        "---\ntitle: Open question\nstatus: draft\n---\n"
        "Sparse reward and delayed memory updates remain competing explanations.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "research").mkdir(parents=True)
    (tmp_path / "docs" / "research" / "conditions.md").write_text(
        "---\ntitle: Conditions\nstatus: candidate\n---\n"
        "Only compare under the fixed evaluator and record counterexamples.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_research_context_is_targeted_and_preserves_hashes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    context = research_knowledge_context(
        root,
        {"objective": "Sparse reward and delayed memory updates"},
        {"question": "which explanation survives?"},
        limit=8,
    )
    assert context["retrieval"].startswith("casefolded substring")
    assert context["entry_points"] == {"schema": "knowledge/SCHEMA.md", "index": "knowledge/index.md"}
    assert context["results"]
    for result in context["results"]:
        path = root / result["path"]
        assert path.is_file()
        assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert "conditions" in result["applicability_check"]
        assert "verify" in result["evidence_status"].lower()


def test_explicit_references_are_discoverable_but_traversal_and_symlinks_are_ignored(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (root / "knowledge" / "queries" / "link.md").symlink_to(outside)
    context = research_knowledge_context(
        root,
        {"objective": "unrelated", "research_mandate": {
            "observations": [
                {"statement": "old result", "source_refs": ["knowledge/queries/open-question.md"]},
                {"statement": "conditions", "source_refs": ["docs/research/conditions.md"]},
            ],
            "suggestions": ["read docs/research/conditions.md"],
        }},
        {"source_refs": ["knowledge/queries/link.md", "../outside.md", "/etc/passwd"]},
    )
    assert "knowledge/queries/open-question.md" in context["explicit_references"]
    assert "docs/research/conditions.md" in context["explicit_references"]
    assert "knowledge/queries/link.md" not in context["explicit_references"]
    assert all(item["path"] not in {"knowledge/queries/link.md", "../outside.md"} for item in context["results"])

    prioritized = research_knowledge_context(
        root,
        {"objective": "Sparse reward and delayed memory updates", "knowledge_refs": ["docs/research/conditions.md"]},
        {},
        limit=1,
    )
    assert prioritized["results"][0]["path"] == "docs/research/conditions.md"
    assert prioritized["explicit_results"] == ["docs/research/conditions.md"]


def test_invalid_encoding_and_oversized_pages_are_skipped(tmp_path: Path) -> None:
    root = _project(tmp_path)
    broken = root / "knowledge" / "queries" / "broken.md"
    broken.write_bytes(b"\xff\xfe")
    large = root / "knowledge" / "queries" / "large.md"
    large.write_text("x" * 256_001, encoding="utf-8")
    context = research_knowledge_context(
        root,
        {"objective": "Sparse reward and delayed memory updates"},
        {"source_refs": ["knowledge/queries/broken.md", "knowledge/queries/large.md"]},
    )
    assert "knowledge/queries/broken.md" not in context["explicit_references"]
    assert "knowledge/queries/large.md" not in context["explicit_references"]
    assert all(item["path"] not in {"knowledge/queries/broken.md", "knowledge/queries/large.md"}
               for item in context["results"])
    assert "knowledge/queries/open-question.md" in [item["path"] for item in context["results"]]
    assert context["skipped_page_count"] == 2


def test_mission_known_evidence_is_prioritized_without_a_keyword_match(tmp_path: Path) -> None:
    root = _project(tmp_path)
    context = research_knowledge_context(
        root,
        {"objective": "unmatched", "known_evidence": [
            "docs/research/conditions.md", "knowledge/queries/open-question.md", "../outside.md"]},
        {"objective": "another unrelated question"}, limit=1,
    )
    assert context["explicit_references"] == [
        "docs/research/conditions.md", "knowledge/queries/open-question.md"]
    assert context["explicit_results"] == ["docs/research/conditions.md"]
    assert context["results"][0]["path"] == "docs/research/conditions.md"


def test_search_skips_symlinked_wiki_directory_without_hiding_safe_pages(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret unique phrase", encoding="utf-8")
    (root / "knowledge" / "queries" / "outside").symlink_to(outside, target_is_directory=True)
    context = research_knowledge_context(root, {"objective": "Sparse reward and secret unique phrase"}, {})
    assert "knowledge/queries/open-question.md" in [item["path"] for item in context["results"]]
    assert all("secret.md" not in item["path"] for item in context["results"])
    assert context["skipped_page_count"] == 1
    assert context["skipped_pages"][0]["reason"] == "symlink directory"
    assert "no relevant knowledge" in context["evidence_note"]


def test_scope_mandate_explicit_paths_and_excess_references_are_bounded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    for number in range(4):
        (root / "knowledge" / "queries" / f"ref-{number}.md").write_text(
            f"---\ntitle: Reference {number}\nstatus: candidate\n---\nUnrelated page {number}\n",
            encoding="utf-8",
        )
    references = [f"knowledge/queries/ref-{number}.md" for number in range(4)]
    context = research_knowledge_context(root, {"objective": "unmatched"},
                                         {"scope": {"research_mandate": {"mode": "direction",
                                          "responsibility": "Explore", "observations": [
                                              {"statement": "available", "source_refs": references}]}}}, limit=2)
    assert len(context["results"]) == 2
    assert context["explicit_results"] == references[:2]
    assert context["explicit_references"] == references


def test_malformed_search_status_falls_back_to_warning(tmp_path: Path) -> None:
    root = _project(tmp_path)
    page = root / "knowledge" / "queries" / "open-question.md"
    page.write_text("---\ntitle: Open question\nstatus: [untrusted]\n---\nSparse reward", encoding="utf-8")
    context = research_knowledge_context(root, {"objective": "Sparse reward"}, {})
    assert context["results"]
    assert "unavailable" in context["results"][0]["evidence_status"]


def test_multiple_terms_read_each_safe_page_only_once(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    broken = root / "knowledge" / "queries" / "broken.md"
    broken.write_bytes(b"\xff\xfe")
    reads: dict[str, int] = {}
    original_open = Path.open

    def counted_open(path: Path, *args, **kwargs):
        if path.suffix == ".md" and str(path).startswith(str(root)) and args and args[0] == "rb":
            reads[str(path)] = reads.get(str(path), 0) + 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    context = research_knowledge_context(root, {"objective": "Investigate sparse feedback and delayed memory updates",
                                                "known_evidence": ["knowledge/queries/open-question.md",
                                                                   "knowledge/queries/broken.md"]},
                                         {"objective": "Compare sparse reward with memory updates"})
    assert "knowledge/queries/open-question.md" in [item["path"] for item in context["results"]]
    assert "knowledge/queries/broken.md" not in context["explicit_references"]
    assert reads
    assert max(reads.values()) == 1


def test_large_page_metadata_cannot_expand_the_context_unboundedly(tmp_path: Path) -> None:
    import json
    root = _project(tmp_path)
    page = root / "knowledge" / "queries" / "metadata.md"
    page.write_text("---\ntitle: " + "t" * 30_000 + "\nstatus: " + "s" * 30_000 +
                    "\nsources: " + json.dumps(["https://example.com/" + "x" * 4000] * 10) +
                    "\n---\nmatchphrase\n", encoding="utf-8")
    context = research_knowledge_context(root, {"objective": "matchphrase"}, {}, limit=1)
    assert len(json.dumps(context)) < 3000
    result = context["results"][0]
    assert result["metadata_truncated"]
    assert result["sources"] == []
    assert result["status"] == "unknown"
    assert result["sha256"] == hashlib.sha256(page.read_bytes()).hexdigest()


def test_symlinked_schema_is_not_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    schema = root / "knowledge" / "SCHEMA.md"
    target = root / "schema-copy.md"
    target.write_bytes(schema.read_bytes())
    schema.unlink()
    schema.symlink_to(target)
    context = research_knowledge_context(root, {"objective": "Sparse reward"}, {})
    assert context["status"] == "knowledge base not initialized"


def test_missing_wiki_is_a_normal_status_and_does_not_invent_evidence(tmp_path: Path) -> None:
    context = research_knowledge_context(tmp_path, {"objective": "research"}, {}, limit=8)
    assert context["results"] == []
    assert context["status"] == "knowledge base not initialized"
    assert "not evidence" in context["evidence_note"]


def test_role_boundaries_distinguish_direction_checks_and_tools() -> None:
    assert "direction work maps domains" in ROLE_GUIDANCE["researcher"]
    assert "concrete check tests" in ROLE_GUIDANCE["researcher"]
    assert "rsi_research_checkpoint" in ROLE_GUIDANCE["researcher"]
    assert "rsi_research_checkpoint" not in TOOLS["coordinator"]
    assert "rsi_research_checkpoint" in TOOLS["researcher"]
    assert "bash" not in TOOLS["coordinator"]
    assert {"web_search", "fetch_content", "github_search", "mcp"} <= set(TOOLS["coordinator"].split(","))
