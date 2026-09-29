"""Reviewers must not consume provisional Runner records during recovery."""
import json

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeStore
from rsi_system.core.runtime_evidence import build_evidence_bundle
from .test_runtime_acceptance import candidate


@pytest.mark.parametrize("status", ["prepared", "running", "uncertain"])
def test_inflight_runs_do_not_block_or_contaminate_independent_review(tmp_path, status):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "review frozen results"))
        store.create_candidate("m", candidate())
        runner = store.create_task("m", "runner", "pending experiment", "test", candidate_id="c0")
        provisional = store.create_run("m", "c0", runner["task_id"], "exploration",
                                       status, None, {}, None)
        finished_task = store.create_task("m", "runner", "finished experiment", "test", candidate_id="c0")
        finished = store.create_run("m", "c0", finished_task["task_id"], "exploration",
                                    "completed", None, {}, {"metric": 1})
        reviewer = store.create_task("m", "critic", "audit frozen result", "test", candidate_id="c0")
        manifest_path = build_evidence_bundle(tmp_path, store, reviewer, tmp_path / "review")
        manifest = json.loads(manifest_path.read_text())
        assert {entry["run_id"] for entry in manifest["files"]} == {finished["run_id"]}
        assert store.run(provisional["run_id"])["status"] == status


def test_failed_run_remains_visible_to_review(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "retain failed evidence"))
        store.create_candidate("m", candidate())
        runner = store.create_task("m", "runner", "failed experiment", "test", candidate_id="c0")
        failed = store.create_run("m", "c0", runner["task_id"], "exploration",
                                   "failed", None, {}, {"error": "interrupted execution"})
        reviewer = store.create_task("m", "critic", "audit failure", "test", candidate_id="c0")
        manifest = json.loads(build_evidence_bundle(tmp_path, store, reviewer, tmp_path / "review").read_text())
        assert {entry["run_id"] for entry in manifest["files"]} == {failed["run_id"]}
