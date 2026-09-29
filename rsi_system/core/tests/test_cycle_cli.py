import json
import sys

from rsi_system.core.cli import main
from rsi_system.core.cycle import run_cycle, reconcile_knowledge
from rsi_system.core.knowledge import search
from rsi_system.core.store import ExperimentStore
from .test_knowledge import _project
from .test_worker import fixture_repo, queued


def test_full_run_reaches_searchable_candidate_and_retries_idempotently(tmp_path):
    repo = _project(fixture_repo(tmp_path))
    with ExperimentStore(repo / ".rsi/state.sqlite3") as store:
        queued(store)
        result = run_cycle(store, repo, repo / ".rsi/runs", python=sys.executable)
        assert result["run"]["status"] == "completed"
        assert len(result["written"]) == 1
        assert result["errors"] == []
        assert search(repo, "test-001")[0]["status"] == "candidate"
        again = run_cycle(store, repo, repo / ".rsi/runs", python=sys.executable)
        assert again == {"run": None, "written": [], "errors": []}
        # Later tampering cannot silently become new knowledge.
        evidence = repo / ".rsi/runs/test-001/output/summary.json"
        evidence.write_text("{}")
        _, errors = reconcile_knowledge(store, repo)
        assert "hash mismatch" in errors[0]["error"]


def test_failed_run_is_saved_as_negative_candidate(tmp_path):
    repo = _project(fixture_repo(tmp_path))
    with ExperimentStore(repo / ".rsi/state.sqlite3") as store:
        queued(store)
        # A preexisting run directory fails safely rather than overwriting evidence.
        (repo / ".rsi/runs/test-001").mkdir(parents=True)
        result = run_cycle(store, repo, repo / ".rsi/runs", python=sys.executable)
        assert result["run"]["status"] == "failed"
        assert result["errors"] == []
        assert "failed" in (repo / result["written"][0]["path"]).read_text()


def test_cli_submit_approve_and_fixed_contract(tmp_path, capsys):
    repo = _project(fixture_repo(tmp_path))
    contracts = repo / "contracts.json"
    contracts.write_text(json.dumps({"runtime-harness-smoke-v1": {
        "dataset_version": "dev-v1", "evaluator_id": "fixed-v1"}}))
    args = ["--root", str(repo), "--contracts", str(contracts)]
    payload = {"experiment_id": "cli-1", "hypothesis": "smoke " * 70,
               "protocol_id": "runtime-harness-smoke-v1", "config": {"steps": 2},
               "dataset_version": "dev-v1", "evaluator_id": "fixed-v1",
               "resource_budget": {"max_seconds": 5}}
    assert main(args + ["submit", json.dumps(payload)]) == 0
    assert main(args + ["approve", "cli-1", "--note", "Acceptance agent: bounded integration check"]) == 0
    with ExperimentStore(repo / ".rsi/state.sqlite3") as store:
        assert store.get("cli-1")["status"] == "queued"
        assert len(store.events("cli-1")) == 4
    payload.update(experiment_id="bad", evaluator_id="edited-evaluator")
    assert main(args + ["submit", json.dumps(payload)]) == 2
    assert "fixed evaluator_id" in capsys.readouterr().err
