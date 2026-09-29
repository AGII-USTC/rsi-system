from rsi_system.core.runtime import write_idea


def test_observed_idea_content_alias_preserves_findings_and_sources(tmp_path):
    idea = {"idea_id": "unresolved", "title": "Unresolved mechanism",
            "substantive_content": "Probe failed before measurement. Mechanism remains untested.",
            "sources": ["knowledge/raw/record.json"], "relations": ["competing-explanation"]}
    path = tmp_path / write_idea(tmp_path, "m", idea)
    text = path.read_text()
    assert idea["substantive_content"] in text
    assert idea["sources"][0] in text
    assert idea["relations"][0] in text
    assert "status: draft" in text


def test_explicit_content_takes_precedence_over_legacy_alias(tmp_path):
    path = tmp_path / write_idea(tmp_path, "m", {"idea_id": "i", "content": "Current finding",
                                                "substantive_content": "Obsolete finding"})
    assert "Current finding" in path.read_text()
    assert "Obsolete finding" not in path.read_text()
