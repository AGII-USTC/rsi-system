"""A real Pi child process completes through its committed ToolBridge submission."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import pytest

from rsi_system.core.pi_agent import run_agent
from rsi_system.core.runtime import RuntimeStore
from rsi_system.core.runtime_curator import load_curator_submission, pending_record_ids
from .test_runtime_curator import page, published, setup


FAKE_PI = '''#!INTERPRETER
import json, os, pathlib, signal, socket, sys, time
mode = MODE
workspace = pathlib.Path(os.environ['RSI_AGENT_WORKSPACE'])
prompt = sys.stdin.read()
frozen = json.loads((workspace.parent / 'input.json').read_text())
assert prompt == frozen['prompt'], 'Pi stdin must use the frozen input.json prompt'
assert os.environ['RSI_AGENT_ROLE'] == 'curator'
with (workspace / 'launches.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid':os.getpid(),'prompt':prompt,'task_id':frozen['task_id']})+'\\n')
def emit(event):
    print(json.dumps(event), flush=True)
if mode == 'none':
    sys.exit(7)
target = workspace / 'wiki/findings/submitted.md'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('Malformed unfinished draft' if mode == 'invalid' else PAGE, encoding='utf-8')
arguments = {'paths':['wiki/findings/submitted.md'],'summary':'Validated source-linked draft from actual Pi child'}
emit({'type':'tool_execution_start','toolName':'rsi_wiki_submit','toolCallId':'process-submit','args':arguments})
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(10)
    client.connect(os.environ['RSI_TOOL_SOCKET'])
    client.sendall((json.dumps({'tool':'rsi_wiki_submit','call_id':'process-submit','args':arguments})+'\\n').encode())
    receipt = json.loads(client.makefile('rb').readline())
(workspace / 'socket-receipt.json').write_text(json.dumps(receipt))
if mode == 'invalid':
    assert receipt['ok'] is False
    sys.exit(7)
assert receipt['ok'] is True and receipt['status'] == 'accepted', receipt
accepted_at = time.monotonic()
(workspace / 'accepted-at.json').write_text(json.dumps({'monotonic':accepted_at,'pid':os.getpid()}))
result = {'content':[{'type':'text','text':json.dumps(receipt)}],'details':receipt,'terminate':True}
emit({'type':'tool_execution_end','toolName':'rsi_wiki_submit','toolCallId':'process-submit','isError':False,'result':result})
emit({'type':'message_end','message':{'role':'toolResult','toolName':'rsi_wiki_submit','toolCallId':'process-submit',**result}})
if mode == 'exit7':
    sys.exit(7)
if mode == 'hang':
    def ignore_term(_signal, _frame):
        (workspace / 'sigterm.json').write_text(json.dumps({'monotonic':time.monotonic(),'pid':os.getpid()}))
    signal.signal(signal.SIGTERM, ignore_term)
    while True:
        time.sleep(0.1)
emit({'type':'message_end','message':{'role':'assistant','stopReason':'stop',
    'content':[{'type':'text','text':'The selected evidence is organized; remaining sources can be reviewed later.'}],
    'usage':{'input':23,'output':7}}})
'''


@pytest.fixture
def process_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("RSI_CONFIG_DIR", str(tmp_path / "private-config"))
    # Unix socket paths have a platform length limit; keep this real bridge fixture short.
    with tempfile.TemporaryDirectory(prefix="rsi-submit-proc-", dir="/tmp") as directory:
        root, coordinator = setup(Path(directory), max_agent_tasks=32)
        config_path = root / "deployment/rsi/research.json"
        config = json.loads(config_path.read_text())
        config.update(agent_context_limit_tokens=None, agent_timeout_seconds=20)
        config_path.write_text(json.dumps(config))
        with RuntimeStore(coordinator.store_path) as store:
            author = published(root, store)
        assert coordinator.tick(require_model=False)["role"] == "curator"
        yield root, coordinator, author


def fake_pi(root, author, mode):
    executable = root / f"fake-pi-{mode}"
    executable.write_text(FAKE_PI.replace("INTERPRETER", sys.executable, 1)
        .replace("MODE", repr(mode), 1).replace("PAGE", repr(page(author)), 1), encoding="utf-8")
    executable.chmod(0o755)
    return executable


def calls_and_remaining(coordinator):
    with RuntimeStore(coordinator.store_path) as store:
        calls = [dict(row) for row in store.db.execute("SELECT * FROM runtime_agent_calls ORDER BY task_id")]
        return calls, store.remaining_agent_tasks("wiki-mission")


def run_process(coordinator, executable, captured, *, crash_after=False):
    def runner(**kwargs):
        captured.update(kwargs)
        frozen = json.loads((kwargs["output_dir"] / "input.json").read_text())
        assert frozen["prompt"] == kwargs["prompt"]
        assert frozen["task_id"] == kwargs["task_id"]
        assert frozen["curator_submission_required"] is True
        result = run_agent(**{**kwargs, "executable": str(executable)})
        if crash_after:
            raise SystemExit("worker interrupted after actual Pi accepted submission")
        return result
    return coordinator.tick(agent_runner=runner, require_model=False)


def assert_no_child(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("mode", ["exit7", "hang", "prose"])
def test_real_pi_submission_completes_despite_exit_hang_or_nonjson_final(process_fixture, monkeypatch, mode):
    root, coordinator, author = process_fixture
    executable = fake_pi(root, author, mode)
    monkeypatch.setattr("rsi_system.core.pi_agent.final_payload",
                        lambda *_: pytest.fail("accepted Curator submission must not parse a final payload"))
    before_calls, _remaining = calls_and_remaining(coordinator)
    captured = {}
    started = time.monotonic()
    outcome = run_process(coordinator, executable, captured)
    elapsed = time.monotonic() - started
    assert outcome["status"] == "completed", outcome
    directory = captured["output_dir"]
    workspace = directory / "workspace"
    socket_receipt = json.loads((workspace / "socket-receipt.json").read_text())
    agent_receipt = json.loads((directory / "agent_result.json").read_text())
    launches = [json.loads(line) for line in (workspace / "launches.jsonl").read_text().splitlines()]
    assert len(launches) == 1
    assert launches[0]["prompt"] == captured["prompt"]
    assert_no_child(launches[0]["pid"])
    assert socket_receipt["ok"] is True
    assert agent_receipt["completion_source"] == "curator_submission"
    assert agent_receipt["payload"] == socket_receipt["payload"]
    assert agent_receipt["curator_submission"] == {key: value for key, value in socket_receipt.items() if key != "ok"}
    assert agent_receipt["prompt_sha256"] == hashlib.sha256(captured["prompt"].encode()).hexdigest()
    assert agent_receipt["events_sha256"] == hashlib.sha256((directory / "events.jsonl").read_bytes()).hexdigest()
    assert agent_receipt["tool_calls"] == [{"tool": "rsi_wiki_submit", "succeeded": True}]
    if mode == "hang":
        accepted = json.loads((workspace / "accepted-at.json").read_text())
        terminated = json.loads((workspace / "sigterm.json").read_text())
        assert 1.8 <= terminated["monotonic"] - accepted["monotonic"] < 6
        # The child ignores SIGTERM, so run_agent must reap it using its SIGKILL fallback.
        assert 3.8 <= elapsed < 15
    if mode == "prose":
        assert agent_receipt["usage"] == {"input": 23, "output": 7}
    with RuntimeStore(coordinator.store_path) as store:
        task = store.task(outcome["task_id"])
        assert task["status"] == "completed"
        operations = store.tool_operations(task["task_id"])
        assert len(operations) == 1 and operations[0]["status"] == "completed"
        assert json.loads(operations[0]["response_json"]) == socket_receipt
        assert load_curator_submission(root, task) == agent_receipt["curator_submission"]
        assert pending_record_ids(store, "wiki-mission") == []
    assert (root / "knowledge/findings/submitted.md").read_text() == page(author)
    after_calls, after_remaining = calls_and_remaining(coordinator)
    assert len(after_calls) == len(before_calls) + 1
    frozen_result = (directory / "agent_result.json").read_bytes()
    frozen_events = (directory / "events.jsonl").read_bytes()
    reused = run_agent(**{**captured, "executable": "must-not-relaunch"})
    assert reused == agent_receipt
    assert (directory / "agent_result.json").read_bytes() == frozen_result
    assert (directory / "events.jsonl").read_bytes() == frozen_events
    assert (workspace / "launches.jsonl").read_text().count("\n") == 1
    assert calls_and_remaining(coordinator) == (after_calls, after_remaining)


def test_real_process_submission_recovers_runtime_without_another_reservation(process_fixture, monkeypatch):
    root, coordinator, author = process_fixture
    executable = fake_pi(root, author, "exit7")
    monkeypatch.setattr("rsi_system.core.pi_agent.final_payload",
                        lambda *_: pytest.fail("accepted submission must not parse a final payload"))
    captured = {}
    with pytest.raises(SystemExit, match="actual Pi accepted"):
        run_process(coordinator, executable, captured, crash_after=True)
    directory = captured["output_dir"]
    receipt_before = (directory / "agent_result.json").read_bytes()
    calls_before = calls_and_remaining(coordinator)
    with RuntimeStore(coordinator.store_path) as store:
        assert store.task(captured["task_id"])["status"] == "running"
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                         (captured["task_id"],))
        store.db.commit()
    outcome = coordinator.tick(agent_runner=lambda **_: pytest.fail("submitted task cannot invoke Pi again"),
                               require_model=False)
    assert outcome["status"] == "completed", outcome
    assert outcome["task_id"] == captured["task_id"]
    assert calls_and_remaining(coordinator) == calls_before
    assert (directory / "workspace/launches.jsonl").read_text().count("\n") == 1
    assert (directory / "agent_result.json").read_bytes() == receipt_before
    assert (root / "knowledge/index.md").read_text().count("[[findings/submitted]]") == 1
    assert (root / "knowledge/log.md").read_text().count("findings/submitted.md") == 1


@pytest.mark.parametrize("mode", ["invalid", "none"])
def test_nonzero_pi_exit_without_a_valid_submission_cannot_complete(process_fixture, monkeypatch, mode):
    root, coordinator, author = process_fixture
    executable = fake_pi(root, author, mode)
    monkeypatch.setattr("rsi_system.core.pi_agent.final_payload",
                        lambda *_: pytest.fail("nonzero Pi exit must fail before final payload parsing"))
    captured = {}
    outcome = run_process(coordinator, executable, captured)
    assert outcome["status"] == "task_failed", outcome
    assert "Pi exited with status 7" in outcome["error"]
    directory = captured["output_dir"]
    assert not (directory / "agent_result.json").exists()
    assert not (directory / "curator-submission/manifest.json").exists()
    with RuntimeStore(coordinator.store_path) as store:
        task = store.task(outcome["task_id"])
        assert task["status"] == "failed"
        assert load_curator_submission(root, task) is None
        operations = store.tool_operations(task["task_id"])
        assert len(operations) == (1 if mode == "invalid" else 0)
        if operations:
            assert operations[0]["status"] == "failed"
            assert json.loads(operations[0]["response_json"])["ok"] is False
        assert pending_record_ids(store, "wiki-mission")
    assert not (root / "knowledge/findings/submitted.md").exists()


def test_curator_zero_exit_retains_provider_timeout(process_fixture):
    root, coordinator, author = process_fixture
    executable = fake_pi(root, author, 'none')
    executable.write_text(executable.read_text().replace("if mode == 'none':\n    sys.exit(7)",
        "if mode == 'none':\n    emit({'type':'message_end','message':{'role':'assistant','stopReason':'error','errorMessage':'Request timed out.'}})\n    sys.exit(0)"))
    outcome = run_process(coordinator, executable, {})
    assert outcome['status'] == 'task_failed'
    assert 'Pi provider invocation failed before Wiki submission: Request timed out.' in outcome['error']
