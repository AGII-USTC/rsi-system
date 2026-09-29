import json

import pytest

from rsi_system.core.schema import ExperimentSpec
from rsi_system.core.store import ExperimentStore


def spec(experiment_id="e-001", parent_id=None):
    return ExperimentSpec(
        experiment_id=experiment_id,
        hypothesis="test hypothesis",
        protocol_id="protocol-v1",
        config={"forgetting_factor": 0.98},
        parent_id=parent_id,
        execution_target="server",
        command=("python3", "-m", "example"),
        resource_budget={"max_minutes": 5},
    )


def test_store_preserves_provenance_and_lineage(tmp_path):
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        root = store.create(spec())
        child = store.create(spec("e-002", "e-001"))
        assert root["status"] == "proposed"
        assert child["parent_id"] == "e-001"
        assert child["config"] == {"forgetting_factor": 0.98}
        assert child["command"] == ("python3", "-m", "example")
        assert json.loads(json.dumps(child["resource_budget"])) == {"max_minutes": 5}

        store.transition("e-001", "awaiting_approval")
        store.transition("e-001", "approved", note="reviewed")
        history = store.events("e-001")
        assert [event["to_status"] for event in history] == ["proposed", "awaiting_approval", "approved"]
        assert history[-1]["note"] == "reviewed"


def test_invalid_transition_and_unknown_parent_are_rejected(tmp_path):
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(ValueError, match="parent"):
            store.create(spec(parent_id="missing"))
        store.create(spec())
        with pytest.raises(ValueError, match="invalid experiment transition"):
            store.transition("e-001", "running")
        with pytest.raises(ValueError, match="start as proposed"):
            store.create(spec("e-002"), status="queued")
