"""An accepted file submission survives lost or malformed final chat output."""
import hashlib
import json

import pytest

from rsi_system.core.pi_agent import recover_curator_result, run_agent
from rsi_system.core.runtime import RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge
from .test_runtime_curator import setup, published, page


FAILED_FINAL = 'rsi_wiki_check{}\n{"summary":"contract example"}\n{"decision":"publish","summary":"actual"}'


def commit_submission(root, kwargs, author, *, final=FAILED_FINAL):
    directory = kwargs["output_dir"]
    draft = directory / "workspace/wiki/findings/submitted.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(page(author))
    budget = directory / "workspace/.rsi-context-budget.jsonl"
    budget.parent.mkdir(parents=True, exist_ok=True)
    budget.write_text(json.dumps({"stage": "provider_request", "allowed": True}) + "\n")
    (directory / "events.jsonl").write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": final}],
        "usage": {"input": 23, "output": 7}}}) + "\n")
    response = ToolBridge(root, kwargs["task_id"]).call({"tool": "rsi_wiki_submit", "call_id": "commit",
        "args": {"paths": ["wiki/findings/submitted.md"], "summary": "validated source-linked draft"}})
    assert response["ok"] is True, response
    return response


@pytest.mark.parametrize("final", [FAILED_FINAL, "The draft is ready.", "", "call:default_api:rsi_wiki_check{}"])
def test_submission_completes_without_parsing_final_text(tmp_path, final):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
    assert coordinator.tick(require_model=False)["role"] == "curator"

    def agent(**kwargs):
        receipt = commit_submission(root, kwargs, author, final=final)
        with RuntimeStore(coordinator.store_path) as store:
            task = store.task(kwargs["task_id"])
        result = recover_curator_result(root, task, kwargs["prompt"])
        assert result["payload"] == receipt["payload"]
        return result

    outcome = coordinator.tick(agent_runner=agent, require_model=False)
    assert outcome["status"] == "completed", outcome
    with RuntimeStore(coordinator.store_path) as store:
        task = store.task(outcome["task_id"])
        assert task["output"]["result"]["completion_source"] == "curator_submission"
        assert task["output"]["result"]["usage"] == {"input": 23, "output": 7}
    assert (root / "knowledge/findings/submitted.md").is_file()


def test_expired_task_recovers_commit_without_reinvocation_or_reservation(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        author = published(root, store)
    assert coordinator.tick(require_model=False)["role"] == "curator"
    captured = {}

    def interrupted(**kwargs):
        captured.update(kwargs)
        commit_submission(root, kwargs, author)
        with (kwargs["output_dir"] / "events.jsonl").open("a") as stream:
            stream.write('{"type":"interrupted')
        raise SystemExit("simulate Python interruption after tool commit")

    with pytest.raises(SystemExit):
        coordinator.tick(agent_runner=interrupted, require_model=False)
    directory = captured["output_dir"]
    assert not (directory / "agent_result.json").exists()
    with RuntimeStore(coordinator.store_path) as store:
        remaining = store.remaining_agent_tasks("wiki-mission")
        call_count = store.db.execute("SELECT count(*) FROM runtime_agent_calls").fetchone()[0]
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                         (captured["task_id"],))
        store.db.commit()

    def must_not_invoke(**kwargs):
        raise AssertionError("accepted submission cannot invoke another model")

    outcome = coordinator.tick(agent_runner=must_not_invoke, require_model=False)
    assert outcome["status"] == "completed", outcome
    with RuntimeStore(coordinator.store_path) as store:
        assert store.remaining_agent_tasks("wiki-mission") == remaining
        assert store.db.execute("SELECT count(*) FROM runtime_agent_calls").fetchone()[0] == call_count
        task = store.task(captured["task_id"])
    result = run_agent(executable="must-not-run", role="curator", prompt=captured["prompt"], cwd=root,
                       output_dir=directory, timeout_seconds=None, runtime_root=coordinator.runtime_root,
                       task_id=task["task_id"])
    assert result["completion_source"] == "curator_submission"
    assert result["events_sha256"] == hashlib.sha256((directory / "events.jsonl").read_bytes()).hexdigest()
    log = (root / "knowledge/log.md").read_text()
    assert log.count("findings/submitted.md") == 1


def test_successful_final_json_without_submission_is_not_completion(tmp_path):
    root, coordinator = setup(tmp_path)
    with RuntimeStore(coordinator.store_path) as store:
        published(root, store)
    assert coordinator.tick(require_model=False)["role"] == "curator"
    outcome = coordinator.tick(agent_runner=lambda **_: {
        "payload": {"decision": "publish", "summary": "no tool submission"}, "tool_protocol_version": 1},
        require_model=False)
    assert outcome["status"] == "task_failed"
    assert "validated rsi_wiki_submit receipt" in outcome["error"]
    with RuntimeStore(coordinator.store_path) as store:
        assert not any(task["status"] == "queued" for task in store.list_tasks("wiki-mission"))
