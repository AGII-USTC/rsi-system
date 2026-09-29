"""Compact Run summaries and independent read-only reviewer scheduling."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rsi_system.core.runtime_evaluation_summary import (
    build_evaluation_summary,
    persist_evaluation_summary,
    validate_evaluation_summary,
)
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_types import MissionSpec


def candidate(identifier="c0"):
    return {"candidate_id": identifier, "hypothesis": "fixture mechanism",
            "kind": "baseline", "baseline_reason": "unchanged fixture reference",
            "expected_observation": "fixture measurement", "protocol_id": "runtime-harness-smoke-v1",
            "config": {"steps": 3}}


def _run_fixture(tmp_path: Path):
    artifact = tmp_path / "run"
    output = artifact / "output"
    output.mkdir(parents=True)
    raw = output / "summary.json"
    raw.write_text(json.dumps({"schema_version": 1, "environment_steps": 3,
                               "waypoint_distance_m": 0.2, "waypoint_reached": False}))
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    evidence = {"artifact_dir": str(artifact), "evidence_path": str(raw),
                "evidence_sha256": digest, "artifact_hashes": {"output/summary.json": digest},
                "started_at_unix": 10.0, "finished_at_unix": 11.25,
                "artifact_diagnostics": {"limits": {"max_seconds": 20},
                                          "stdout_bytes": 4, "stderr_bytes": 0}}
    run = {"run_id": "run-1", "mission_id": "m", "status": "completed",
           "source_commit": "a" * 40, "inputs": {"protocol_id": "runtime-harness-smoke-v1",
           "config": {"seed": 1}, "algorithm": {"family": "fixture"},
           "resource_budget": {"max_seconds": 20}}, "evidence": evidence}
    return artifact, run


def test_summary_is_compact_durable_and_hash_checked(tmp_path):
    artifact, run = _run_fixture(tmp_path)
    candidate_record = {"protocol_id": "runtime-harness-smoke-v1", "config": {"seed": 1},
                        "algorithm": {"family": "fixture"}}
    path, summary = persist_evaluation_summary(run, artifact_dir=artifact,
                                               candidate=candidate_record,
                                               protocol_metrics={"environment_steps": 3,
                                                                 "waypoint_reached": False})
    assert path.name == "evaluation_summary.json"
    assert summary["protocol"]["sha256"]
    assert summary["source"]["commit"] == "a" * 40
    assert summary["metrics"]["condition_by_method"]["default"]["default"]["environment_steps"] == 3
    assert summary["resource_cost"] == {"wall_seconds": 1.25, "max_seconds": 20,
                                        "stdout_bytes": 4, "stderr_bytes": 0,
                                        "budget": {"max_seconds": 20}}
    assert summary["raw_artifacts"] == [{"path": "output/summary.json", "sha256":
                                          hashlib.sha256((artifact / "output/summary.json").read_bytes()).hexdigest()}]
    assert validate_evaluation_summary(path)["run_id"] == "run-1"
    (artifact / "output/summary.json").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_evaluation_summary(path)


def test_summary_requires_completed_run(tmp_path):
    artifact, run = _run_fixture(tmp_path)
    with pytest.raises(ValueError, match="completed"):
        build_evaluation_summary({**run, "status": "failed"}, artifact_dir=artifact)


def test_analyzer_and_critic_share_frozen_run_but_writer_stays_exclusive(tmp_path):
    with RuntimeStore(tmp_path / "state.sqlite3") as store:
        store.create_mission(MissionSpec("m", "review"))
        store.create_candidate("m", candidate())
        run = store.create_run("m", "c0", None, "fixture", "completed", "a" * 40, {}, {})
        analyzer = store.create_task("m", "analyzer", "analyze", "independent", candidate_id="c0",
                                     review_run_id=run["run_id"])
        critic = store.create_task("m", "critic", "criticize", "independent", candidate_id="c0",
                                   review_run_id=run["run_id"])
        coder = store.create_task("m", "coder", "write", "follow-up", candidate_id="c0")
        first = store.claim_task("m", max_parallel_agents=3)
        second = store.claim_task("m", max_parallel_agents=3)
        assert {first["task_id"], second["task_id"]} == {analyzer["task_id"], critic["task_id"]}
        assert first["scope"]["review_run_id"] == run["run_id"]
        assert second["scope"]["review_run_id"] == run["run_id"]
        assert store.claim_task("m", max_parallel_agents=3) is None
        store.finish_task(first["task_id"], first["lease_token"], {"summary": "done"})
        store.finish_task(second["task_id"], second["lease_token"], {"summary": "done"})
        assert store.claim_task("m", max_parallel_agents=3)["task_id"] == coder["task_id"]


def test_review_binding_rejects_foreign_or_unfinished_run(tmp_path):
    with RuntimeStore(tmp_path / "state.sqlite3") as store:
        store.create_mission(MissionSpec("m", "review"))
        store.create_mission(MissionSpec("other", "other"))
        store.create_candidate("m", candidate())
        pending = store.create_run("m", "c0", None, "fixture", "running", "a" * 40, {}, {})
        with pytest.raises(ValueError, match="completed Run"):
            store.create_task("m", "critic", "criticize", "test", candidate_id="c0",
                              review_run_id=pending["run_id"])
