"""Durable Agent-session and interrupted-tool recovery contracts."""
import json
from pathlib import Path

from rsi_system.core.runtime import MissionSpec, RuntimeStore
from rsi_system.core.runtime_agent_recovery import continuation_metadata, session_dir_for, session_id_for
from rsi_system.core.runtime_research_continuation import continuation_ready


def test_interrupted_tool_becomes_replayable_error_result(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "test", resources={"max_agent_tasks": 8}))
        source = store.create_task("m", "researcher", "read", "test")
        claimed = store.claim_task("m")
        store.put_tool_operation(claimed["task_id"], "call-1", "rsi_read_source", "hash", "pending")
        assert store.interrupt_pending_tools(claimed["task_id"], reason="worker exited") == ["call-1"]
        operation = store.tool_operation(claimed["task_id"], "call-1")
        assert operation["status"] == "failed"
        result = json.loads(operation["response_json"])
        assert result["error"]["code"] == "interrupted_tool_call"


def test_recovery_continuation_keeps_logical_session_and_accepts_failed_source(tmp_path):
    with RuntimeStore(tmp_path / "state.db") as store:
        store.create_mission(MissionSpec("m", "test", resources={"max_agent_tasks": 8}))
        source = store.create_task("m", "coordinator", "choose", "test")
        claimed = store.claim_task("m")
        store.finish_task(claimed["task_id"], claimed["lease_token"], {"error": "bad JSON"}, status="failed")
        metadata = continuation_metadata(claimed, mode="protocol_correction",
                                         failure={"kind": "output_parse_error"}, event_cursor=2)
        child = store.create_task("m", "coordinator", "continue", "repair",
                                  reply_to_task_id=claimed["task_id"], continuation=metadata)
        assert session_id_for(child) == session_id_for(claimed)
        assert continuation_ready(store, child) == (True, None)
        assert child["scope"]["continuation"]["mode"] == "protocol_correction"


def test_native_session_directory_is_writable_and_continuations_reuse_it(tmp_path):
    task = {"task_id": "source", "scope": {"continuation": None}}
    first = session_dir_for(task, tmp_path / ".rsi/runtime")
    assert first == (tmp_path / ".rsi/runtime/tasks/source/workspace/.pi-session").resolve()
    continuation = {"task_id": "child", "scope": {"continuation": {"session_dir": str(first)}}}
    assert session_dir_for(continuation, tmp_path / ".rsi/runtime") == first
    first.mkdir(parents=True)
    probe = first / "write-probe"
    probe.write_text("ok")
    assert probe.read_text() == "ok"
