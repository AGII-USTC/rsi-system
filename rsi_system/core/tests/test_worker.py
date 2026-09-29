import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from rsi_system.core.schema import ExperimentSpec
from rsi_system.core.store import ExperimentStore
from rsi_system.core.worker import run_once


STUB = '''import argparse, json, os, pathlib, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument("--output", type=pathlib.Path)
p.add_argument("--seed")
p.add_argument("--steps", type=int)
p.add_argument("--mode")
p.add_argument("--monitor")
p.add_argument("--switch-tick")
a = p.parse_args()
if a.seed == "1":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(30)
if a.seed == "2":
    time.sleep(30)
if a.seed == "3":
    a.output.mkdir()
    sys.exit(0)
a.output.mkdir()
(a.output / "summary.json").write_text(json.dumps({"schema_version": 1, "mode": a.mode,
    "monitor_mode": a.monitor, "environment_steps": a.steps,
    "waypoint_distance_m": 0.4, "waypoint_reached": False, "source_version": "committed"}))
'''


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "experiments" / "runtime_harness"
    module.mkdir(parents=True)
    (repo / "experiments" / "__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    (module / "__main__.py").write_text(STUB)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "-qm", "fixture"], check=True)
    return repo


def queued(store, experiment_id="test-001", *, seed=0, max_seconds=5):
    spec = ExperimentSpec(experiment_id, "smoke", "runtime-harness-smoke-v1",
                          {"seed": seed, "steps": 3}, resource_budget={"max_seconds": max_seconds})
    store.create(spec)
    for target in ("awaiting_approval", "approved", "queued"):
        store.transition(experiment_id, target)


def test_claim_is_atomic_and_only_queued_work_runs(tmp_path):
    db = tmp_path / "state.sqlite3"
    with ExperimentStore(db) as first, ExperimentStore(db) as second:
        queued(first)
        assert first.claim_next()["status"] == "running"
        assert second.claim_next() is None
        with pytest.raises(ValueError, match="invalid experiment transition"):
            first.transition("test-001", "queued")


def test_run_uses_committed_source_and_records_evidence(tmp_path):
    repo = fixture_repo(tmp_path)
    (repo / "experiments" / "runtime_harness" / "__main__.py").write_text("raise RuntimeError('dirty source ran')")
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store)
        result = run_once(store, repo, tmp_path / "runs", python=sys.executable)
        assert result["status"] == "completed"
        assert result["metrics"]["environment_steps"] == 3
        assert result["command"][0] == os.path.abspath(sys.executable)
        assert len(result["source_commit"]) == 40
        assert len(result["config_sha256"]) == 64
        assert Path(result["evidence_path"]).is_file()
        assert store.get("test-001")["run"]["evidence_sha256"] == result["evidence_sha256"]
        assert run_once(store, repo, tmp_path / "runs", python=sys.executable) is None


def test_timeout_kills_subprocess_group_and_preserves_failure(tmp_path):
    repo = fixture_repo(tmp_path)
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store, seed=1, max_seconds=1)
        result = run_once(store, repo, tmp_path / "runs", python=sys.executable)
        assert result["status"] == "failed"
        assert "max_seconds" in result["error"]
        assert store.get("test-001")["status"] == "failed"
        assert Path(result["stderr_path"]).is_file()


@pytest.mark.parametrize("seconds", [10, None])
def test_cancellation_stops_running_process(tmp_path, seconds):
    repo = fixture_repo(tmp_path)
    db = tmp_path / "state.sqlite3"
    with ExperimentStore(db) as controller:
        queued(controller, seed=2, max_seconds=seconds)
        output = {}
        def worker():
            with ExperimentStore(db) as store:
                output["result"] = run_once(store, repo, tmp_path / "runs", python=sys.executable)
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 5
        while controller.get("test-001")["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        controller.transition("test-001", "cancelled")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert output["result"]["status"] == "cancelled"
        assert controller.get("test-001")["run"]["status"] == "cancelled"


def test_unlimited_run_survives_elapsed_clock_and_preserves_null_budget(tmp_path, monkeypatch):
    repo = fixture_repo(tmp_path)
    ticks = iter(range(0, 10000000, 10000))
    monkeypatch.setattr("rsi_system.core.worker.time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(ticks), sleep=time.sleep))
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store, max_seconds=None)
        result = run_once(store, repo, tmp_path / "runs", python=sys.executable)
        assert result["status"] == "completed", result
        assert store.get("test-001")["resource_budget"]["max_seconds"] is None


def test_restart_marks_orphaned_running_record_interrupted(tmp_path):
    repo = fixture_repo(tmp_path)
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store)
        store.claim_next()
        assert run_once(store, repo, tmp_path / "runs", python=sys.executable) is None
        record = store.get("test-001")
        assert record["status"] == "interrupted"
        assert record["run"]["status"] == "interrupted"


def test_missing_output_is_failed_and_keeps_logs(tmp_path):
    repo = fixture_repo(tmp_path)
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store, seed=3)
        result = run_once(store, repo, tmp_path / "runs", python=sys.executable)
        assert result["status"] == "failed"
        assert "summary.json" in result["error"]
        assert store.get("test-001")["run"]["status"] == "failed"


def test_existing_run_directory_finishes_claim_as_failed(tmp_path):
    repo = fixture_repo(tmp_path)
    run_root = tmp_path / "runs"
    (run_root / "test-001").mkdir(parents=True)
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store)
        result = run_once(store, repo, run_root, python=sys.executable)
        assert result["status"] == "failed"
        assert store.get("test-001")["status"] == "failed"


def test_restart_with_damaged_record_recovers_without_overwriting_evidence(tmp_path):
    repo = fixture_repo(tmp_path)
    run_root = tmp_path / "runs"
    run_dir = run_root / "test-001"
    run_dir.mkdir(parents=True)
    record_path = run_dir / "run_record.json"
    record_path.write_text("{partial")
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store)
        store.claim_next()
        # Simulate interruption of recovery itself before evidence attachment.
        store.recover_running()
        assert run_once(store, repo, run_root, python=sys.executable) is None
        record = store.get("test-001")
        assert record["status"] == "interrupted"
        assert "unreadable" in record["run"]["recovery_warning"]
        assert record_path.read_text() == "{partial"
        assert (run_dir / "recovery_record.json").is_file()


def test_reviewed_commit_is_executed_when_head_moves(tmp_path):
    repo = fixture_repo(tmp_path)
    reviewed = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "experiments/runtime_harness/__main__.py").write_text("raise RuntimeError('unreviewed head')")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.org", "commit", "-qm", "unreviewed"], check=True)
    with ExperimentStore(tmp_path / "state.sqlite3") as store:
        queued(store)
        result = run_once(store, repo, tmp_path / "runs", source_commit=reviewed)
        assert result["status"] == "completed"
        assert result["source_commit"] == reviewed
