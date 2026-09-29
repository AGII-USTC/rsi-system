from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi_system.core.proposals import export_proposal, import_proposal, proposal_context


FIXED = {"fixed_protocol_id": "nominal-v1", "fixed_dataset_version": "dev-2026-09", "fixed_evaluator_id": "reach-evaluator-v1"}


def _payload() -> dict[str, object]:
    return {
        "experiment_id": "nominal-001",
        "hypothesis": "A qualified nominal policy enables a fair adaptation comparison.",
        "protocol_id": "nominal-v1",
        "dataset_version": "dev-2026-09",
        "evaluator_id": "reach-evaluator-v1",
        "config": {"training_steps": 100},
        "execution_target": "server",
        "resource_budget": {"max_runs": 1},
    }


def test_structured_roundtrip_and_fixed_contract() -> None:
    spec = import_proposal(json.dumps(_payload()), **FIXED)
    exported = export_proposal(spec)
    assert exported["protocol_id"] == "nominal-v1"
    assert exported["command"] == []
    assert import_proposal(exported, **FIXED) == spec
    changed = _payload()
    changed["evaluator_id"] = "my-evaluator"
    with pytest.raises(ValueError, match="fixed evaluator_id"):
        import_proposal(changed, **FIXED)
    changed = _payload()
    changed["test_split"] = "easier"
    with pytest.raises(ValueError, match="unknown"):
        import_proposal(changed, **FIXED)
    changed = _payload()
    changed["config"] = {"training": {"evaluator_id": "other"}}
    with pytest.raises(ValueError, match="fixed field"):
        import_proposal(changed, **FIXED)
    changed = _payload()
    changed["command"] = ["python", "run.py", "--eval", "easy"]
    with pytest.raises(ValueError, match="fixed command"):
        import_proposal(changed, **FIXED)


def test_context_names_priorities_without_claiming_semantic_retrieval(tmp_path: Path) -> None:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "SCHEMA.md").write_text("# Schema", encoding="utf-8")
    (tmp_path / "knowledge" / "findings").mkdir()
    (tmp_path / "knowledge" / "findings" / "note.md").write_text("名义策略 qualification", encoding="utf-8")
    value = proposal_context(tmp_path, "名义策略")
    assert value["knowledge"]["results"][0]["path"] == "knowledge/findings/note.md"
    assert any("closed-loop" in item for item in value["priorities"])
    assert any("paused" in item for item in value["priorities"])
    assert any("fixed protocol" in item for item in value["constraints"])
