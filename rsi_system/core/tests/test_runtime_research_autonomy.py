"""Focused persistence and gate tests for explicit research continuation."""
from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project
import pytest


def setup(tmp_path, *, budget=8):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("autonomy", "Investigate", resources={"max_agent_tasks": budget}))
    with RuntimeStore(runtime.store_path) as store:
        parent = store.create_task("autonomy", "coordinator", "Plan", "initial")
        parent = store.claim_task("autonomy")
        researcher = store.create_task("autonomy", "researcher", "Study", "assigned",
                                       parent_task_id=parent["task_id"], idea_id="idea",
                                       research_node_id="autonomy-root", research_revision=1,
                                       research_mandate={"mode": "direction", "responsibility": "study",
                                                         "constraints": [{"statement": "hard", "source_ref": "mission"}],
                                                         "observations": [], "suggestions": ["compare"]})
        researcher = store.claim_task("autonomy")
    return runtime, researcher


def checkpoint(bridge, call_id, continuation=None):
    args = {"problem_statement": "q", "summary": "changed", "changes": [{
        "source_refs": ["docs/research/a.md"], "previous_view": "old",
        "current_view": "new", "reason": "evidence", "next_test": "probe"}]}
    if continuation is not None:
        args["continuation"] = continuation
    return bridge.call({"tool": "rsi_research_checkpoint", "call_id": call_id, "args": args})


def test_explicit_checkpoint_queues_gated_child_and_is_persistent(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "coder", "args": {
        "role": "researcher", "objective": "source", "idea_id": "idea"}})
    assert child["ok"]
    result = checkpoint(bridge, "cp", {"objective": "continue", "after_task_ids": [child["task_id"]]})
    assert result["ok"] and result["task_status"] == "queued"
    with RuntimeStore(runtime.store_path) as store:
        queued = store.task(result["task_id"])
        assert queued["scope"]["continuation"]["source_task_id"] == researcher["task_id"]
        assert store.research_checkpoints("autonomy", researcher["task_id"])[0]["payload"]["summary"] == "changed"


def test_old_continue_does_not_schedule_and_checkpoint_is_idempotent_by_call(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    first = checkpoint(bridge, "cp", None)
    assert first["ok"] and first["task_id"] is None
    assert bridge.call({"tool": "rsi_research_checkpoint", "call_id": "cp", "args": {
        "problem_statement": "q", "summary": "changed", "changes": [{
            "source_refs": ["docs/research/a.md"], "previous_view": "old",
            "current_view": "new", "reason": "evidence", "next_test": "probe"}]}}) == first
    with RuntimeStore(runtime.store_path) as store:
        assert not [task for task in store.list_tasks("autonomy") if task["scope"].get("continuation")]


def test_failed_source_cancels_queued_continuation_and_budget_is_reserved(tmp_path):
    runtime, researcher = setup(tmp_path, budget=3)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    result = checkpoint(bridge, "cp", {"objective": "continue"})
    assert result["ok"]
    with RuntimeStore(runtime.store_path) as store:
        current = store.task(researcher["task_id"])
        store.finish_task(researcher["task_id"], current["lease_token"], {"error": "failed"}, status="failed")
        assert store.task(result["task_id"])["status"] == "cancelled"
        assert store.remaining_agent_tasks("autonomy") == 0


def test_source_cannot_wait_for_its_own_explicit_continuation(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    result = checkpoint(bridge, "cp", {"objective": "continue"})
    waited = bridge.call({"tool": "rsi_wait", "call_id": "self-continuation", "args": {"task_id": result["task_id"]}})
    assert not waited["ok"] and "finish this task first" in waited["error"]["message"]


def test_second_continuation_can_read_original_explicit_dependency(tmp_path):
    runtime, researcher = setup(tmp_path)
    first_bridge = ToolBridge(runtime.root, researcher["task_id"])
    child = first_bridge.call({"tool": "rsi_delegate", "call_id": "source", "args": {
        "role": "researcher", "objective": "source", "idea_id": "idea"}})
    first = checkpoint(first_bridge, "first", {"objective": "continue once", "after_task_ids": [child["task_id"]]})
    with RuntimeStore(runtime.store_path) as store:
        source = store.task(researcher["task_id"])
        store.finish_task(source["task_id"], source["lease_token"], {"payload": {"summary": "done"}})
        dependency = store.claim_task("autonomy")
        assert dependency["task_id"] == child["task_id"]
        store.finish_task(dependency["task_id"], dependency["lease_token"], {"payload": {"summary": "evidence"}})
        once = store.claim_task("autonomy")
        assert once["task_id"] == first["task_id"]
    second = checkpoint(ToolBridge(runtime.root, first["task_id"]), "second", {"objective": "continue twice"})
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(once["task_id"], once["lease_token"], {"payload": {"summary": "next"}})
        twice = store.claim_task("autonomy")
        assert twice["task_id"] == second["task_id"]
    read = ToolBridge(runtime.root, second["task_id"]).call({
        "tool": "rsi_status", "call_id": "old-source", "args": {"task_id": child["task_id"]}})
    assert read["ok"] and read["response"]["summary"] == "evidence"


def test_mandate_is_bounded_and_child_can_restate_local_responsibility(tmp_path):
    runtime, researcher = setup(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        child = store.create_task("autonomy", "researcher", "local", "local",
                                  parent_task_id=researcher["task_id"], idea_id="idea",
                                  research_node_id="autonomy-root", research_revision=1,
                                  research_mandate={"mode": "check", "responsibility": "local check",
                                                    "constraints": [], "observations": [], "suggestions": []})
        assert child["scope"]["research_mandate"]["mode"] == "check"
        with pytest.raises(ValueError, match="exceeds"):
            store.create_task("autonomy", "researcher", "too long", "long",
                              parent_task_id=researcher["task_id"], idea_id="idea",
                              research_node_id="autonomy-root", research_revision=1,
                              research_mandate={"mode": "direction", "responsibility": "x" * 8001,
                                                "constraints": [], "observations": [], "suggestions": []})


def test_long_checkpoint_stays_out_of_prompt_projection_but_is_readable_by_status(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    marker = "TAIL_MARKER_SHOULD_NOT_ENTER_PROMPT"
    args = {"problem_statement": "q", "summary": "a" * 7000 + marker, "changes": []}
    result = bridge.call({"tool": "rsi_research_checkpoint", "call_id": "long", "args": args})
    assert result["ok"]
    with RuntimeStore(runtime.store_path) as store:
        prompt = runtime._prompt("researcher", store.mission("autonomy"), store.task(researcher["task_id"]), store)
        assert marker not in prompt
        assert marker in store.research_checkpoints("autonomy", researcher["task_id"])[0]["payload"]["summary"]
        current = store.task(researcher["task_id"])
        store.finish_task(current["task_id"], current["lease_token"], {"payload": {"summary": "finished"}})
    with RuntimeStore(runtime.store_path) as store:
        coordinator = next(task for task in store.list_tasks("autonomy") if task["role"] == "coordinator")
    with RuntimeStore(runtime.store_path) as store:
        checkpoint_id = store.research_checkpoints("autonomy", researcher["task_id"])[0]["checkpoint_id"]
    status = ToolBridge(runtime.root, coordinator["task_id"]).call({"tool": "rsi_status", "call_id": "status", "args": {"task_id": researcher["task_id"], "checkpoint_id": checkpoint_id}})
    assert status["ok"], status
    assert marker in status["research_checkpoint"]["payload"]["summary"]
