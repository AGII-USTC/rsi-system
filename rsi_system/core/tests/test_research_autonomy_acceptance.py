"""Exercise real task persistence and handoffs with injected model choices.

This test does not establish scientific improvement or invoke a model.
"""
from pathlib import Path
import json
import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_curator import archive_records
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project
from .test_runtime_research_autonomy import setup, checkpoint


def test_researcher_resumes_interprets_child_and_publishes_traceable_checkpoint(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    mission_id = "autonomy-handoff"
    runtime.start(MissionSpec(mission_id, "Identify what limits adaptation",
        resources={"max_agent_tasks": 6, "max_runs": 1},
        authorization={"allow_code_candidates": True}))
    mandate = {"mode": "direction", "responsibility": "Determine whether the observations identify the change",
               "constraints": [], "observations": [], "suggestions": ["Consider competing mechanisms"]}
    with RuntimeStore(runtime.store_path) as store:
        planner = store.create_task(mission_id, "coordinator", "Map the research responsibility",
            "Assign an unknown rather than an algorithm", research_node_id=f"{mission_id}-root", research_revision=1)
    ids = {"planner": planner["task_id"]}

    def call(bridge, tool, call_id, args):
        result = bridge.call({"tool": tool, "call_id": call_id, "args": args})
        assert result["ok"], result
        return result

    def agent(**kwargs):
        bridge = ToolBridge(root, kwargs["task_id"])
        workspace = Path(kwargs["output_dir"]) / "workspace"
        if kwargs["task_id"] == ids["planner"]:
            source = call(bridge, "rsi_delegate", "research-responsibility", {
                "role": "researcher", "objective": mandate["responsibility"],
                "idea_id": "idea-a", "research_mandate": mandate})
            ids["source"] = source["task_id"]
            return {"payload": {"decision": "schedule", "summary": "Responsibility assigned",
                "expected_information": "A researcher-chosen diagnostic to distinguish explanations",
                "budget_impact": "One researcher, with later work bounded by the Mission budget",
                "stop_condition": "Review the diagnostic and remaining unknowns"}, "tool_protocol_version": 1}
        if kwargs["task_id"] == ids["source"]:
            assert mandate["responsibility"] in kwargs["prompt"]
            coder = call(bridge, "rsi_delegate", "prototype", {"role": "coder",
                "objective": "Produce a small identifiability diagnostic", "idea_id": "idea-a"})
            ids["coder"] = coder["task_id"]
            checkpoint = call(bridge, "rsi_research_checkpoint", "checkpoint-source", {
                "problem_statement": "Is weak excitation the unresolved cause?",
                "summary": "Await the diagnostic before selecting an update mechanism",
                "changes": [],
                "continuation": {"objective": "Interpret the diagnostic and revise the explanation",
                                 "after_task_ids": [coder["task_id"]]}})
            ids["continuation"] = checkpoint["task_id"]
            return {"payload": {"decision": "continue", "summary": "Diagnostic requested"},
                    "tool_protocol_version": 1}
        if kwargs["role"] == "coder":
            (workspace / "diagnostic.json").write_text(json.dumps({
                "identifiable": False, "evidence_status": "test fixture, not measured science"}))
            return {"payload": {"summary": "Diagnostic fixture prepared", "code_change": False},
                    "tool_protocol_version": 1}
        assert kwargs["task_id"] == ids["continuation"]
        assert "Await the diagnostic before selecting an update mechanism" in kwargs["prompt"]
        prior = call(bridge, "rsi_status", "source-checkpoints", {"task_id": ids["source"]})
        checkpoint_ref = prior["research_checkpoints"][0]["checkpoint_id"]
        full = call(bridge, "rsi_status", "read-source-checkpoint", {
            "task_id": ids["source"], "checkpoint_id": checkpoint_ref})
        assert full["research_checkpoint"]["payload"]["problem_statement"] == "Is weak excitation the unresolved cause?"
        assert "research_checkpoints" not in prior["response"]
        status = call(bridge, "rsi_status", "prior-coder-status", {"task_id": ids["coder"]})
        file = next(item for item in status["handoff_files"] if item["workspace_path"] == "diagnostic.json")
        opened = call(bridge, "rsi_read_handoff", "read-diagnostic",
                      {"task_id": ids["coder"], "file_id": file["file_id"]})
        assert json.loads(opened["text"])["identifiable"] is False
        call(bridge, "rsi_research_checkpoint", "checkpoint-interpretation", {
            "problem_statement": "Which observations distinguish the mechanisms?",
            "summary": "The fixture changes the next question; no scientific claim",
            "changes": [{"source_refs": [file["file_id"]],
                         "previous_view": "Tune the update mechanism first",
                         "current_view": "First test whether the change is identifiable",
                         "reason": "The declared diagnostic reported an identifiability gap"}],
            "unresolved": "Needs an actual controlled experiment"})
        return {"payload": {"decision": "continue", "summary": "Question revised; no automatic follow-up"},
                "tool_protocol_version": 1}

    # Recreate coordinator and database connections at every boundary.
    for expected in ("planner", "source", "coder", "continuation"):
        restarted = RuntimeCoordinator(root)
        with RuntimeStore(restarted.store_path) as store:
            claimed = store.claim_task(mission_id)
            assert claimed and claimed["task_id"] == ids[expected]
            outcome = restarted._execute(store, store.mission(mission_id), claimed,
                                         agent_runner=agent, require_model=False)
            assert outcome["status"] == "completed", outcome
    with RuntimeStore(runtime.store_path) as store:
        assert store.claim_task(mission_id) is None
        tasks = store.list_tasks(mission_id)
        assert len(tasks) == 4
        continued = store.task(ids["continuation"])
        assert continued["scope"]["research_mandate"] == mandate
        assert continued["scope"]["research_node_id"] == f"{mission_id}-root"
        archive_records(root, store, mission_id)
    archive = root / "knowledge/raw/runtime-records" / mission_id / ids["continuation"]
    response = json.loads((archive / "response.json").read_text())
    assert "First test whether the change is identifiable" in json.dumps(response)
    assert (archive / "provenance.json").is_file()


def test_budget_failure_rolls_back_checkpoint_and_continuation_together(tmp_path):
    runtime, researcher = setup(tmp_path, budget=2)
    with RuntimeStore(runtime.store_path) as store:
        before_version = store.mission("autonomy")["version"]
    result = checkpoint(ToolBridge(runtime.root, researcher["task_id"]), "over-budget",
                        {"objective": "interpret a later result"})
    assert not result["ok"] and "budget" in result["error"]["message"]
    with RuntimeStore(runtime.store_path) as store:
        assert store.research_checkpoints("autonomy") == []
        assert len(store.list_tasks("autonomy")) == 2
        assert store.mission("autonomy")["version"] == before_version
        assert store.tool_operations(researcher["task_id"])[0]["status"] == "failed"


def test_continuation_replay_is_idempotent_and_second_request_is_rejected(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    request = {"objective": "interpret"}
    first = checkpoint(bridge, "once", request)
    assert first["ok"]
    assert checkpoint(bridge, "once", request) == first
    rejected = checkpoint(bridge, "second", request)
    assert not rejected["ok"]
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.research_checkpoints("autonomy")) == 1
        assert len(store.list_tasks("autonomy")) == 3


@pytest.mark.parametrize("status", ["paused", "stopped"])
def test_mission_pause_or_stop_prevents_continuation_claim(tmp_path, status):
    runtime, researcher = setup(tmp_path)
    result = checkpoint(ToolBridge(runtime.root, researcher["task_id"]), "next", {"objective": "interpret"})
    assert result["ok"]
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(researcher["task_id"], researcher["lease_token"], {"payload": {"summary": "done"}})
        store.set_mission_status("autonomy", status, reason="operator requested boundary")
        assert store.claim_task("autonomy") is None


@pytest.mark.parametrize("dependency_status", ["failed", "cancelled"])
def test_failed_or_cancelled_dependency_can_be_interpreted(tmp_path, dependency_status):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    delegated = bridge.call({"tool": "rsi_delegate", "call_id": "check", "args": {
        "role": "researcher", "objective": "check", "idea_id": "idea"}})
    assert delegated["ok"]
    continuation = checkpoint(bridge, "next", {"objective": "interpret any outcome",
        "after_task_ids": [delegated["task_id"]]})
    assert continuation["ok"]
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(researcher["task_id"], researcher["lease_token"], {"payload": {"summary": "done"}})
        dependency = store.claim_task("autonomy")
        assert dependency["task_id"] == delegated["task_id"]
        assert store.claim_task("autonomy") is None
        store.finish_task(dependency["task_id"], dependency["lease_token"], {"error": "diagnostic failed"},
                          status=dependency_status)
        resumed = store.claim_task("autonomy")
        assert resumed["task_id"] == continuation["task_id"]
    result = ToolBridge(runtime.root, resumed["task_id"]).call({"tool": "rsi_status", "call_id": "read-failure",
        "args": {"task_id": dependency["task_id"]}})
    assert result["ok"] and result["status"] == dependency_status


def test_branch_stop_keeps_checkpoint_but_prevents_new_continuation(tmp_path):
    runtime, researcher = setup(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        coordinator = next(task for task in store.list_tasks("autonomy") if task["role"] == "coordinator")
    stopped = ToolBridge(runtime.root, coordinator["task_id"]).call({"tool": "rsi_branch_control", "call_id": "stop",
        "args": {"task_id": researcher["task_id"], "reason": "budget should go to another question"}})
    assert stopped["ok"]
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    assert checkpoint(bridge, "save-last-state")["ok"]
    rejected = checkpoint(bridge, "resurrect", {"objective": "continue anyway"})
    assert not rejected["ok"] and "stop" in rejected["error"]["message"]
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.research_checkpoints("autonomy")) == 1
        assert len(store.list_tasks("autonomy")) == 2


@pytest.mark.parametrize("mismatch", ["mission", "idea", "node", "ancestor", "self"])
def test_invalid_continuation_dependency_does_not_write_research_state(tmp_path, mismatch):
    runtime, researcher = setup(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        if mismatch == "mission":
            store.create_mission(MissionSpec("other", "Another question"))
            dependency = store.create_task("other", "researcher", "other", "scope test", idea_id="idea")
        elif mismatch == "idea":
            dependency = store.create_task("autonomy", "researcher", "other", "scope test", idea_id="other",
                                           research_node_id="autonomy-root")
        elif mismatch == "node":
            store.apply_research_operation("autonomy", "create", {"node_id": "other-node", "question": "other",
                "scope": "Other responsibility", "reason": "Independent question"},
                actor_task_id=researcher["scope"]["parent_task_id"])
            dependency = store.create_task("autonomy", "researcher", "other", "scope test", idea_id="idea",
                                           research_node_id="other-node")
        elif mismatch == "ancestor":
            dependency = researcher
            store.create_task("autonomy", "researcher", "nested", "scope test", idea_id="idea",
                              parent_task_id=researcher["task_id"])
            researcher = store.claim_task("autonomy")
        else:
            dependency = researcher
        before = store.mission("autonomy")["version"]
        count = len(store.list_tasks("autonomy"))
    result = checkpoint(ToolBridge(runtime.root, researcher["task_id"]), "invalid-scope", {
        "objective": "interpret", "after_task_ids": [dependency["task_id"]]})
    assert not result["ok"], result
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("autonomy")["version"] == before
        assert len(store.list_tasks("autonomy")) == count
        assert store.research_checkpoints("autonomy") == []


def test_old_scope_schema_is_migrated_without_rewriting_legacy_tasks(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("legacy", "old task"))
    with RuntimeStore(runtime.store_path) as store:
        legacy = store.create_task("legacy", "researcher", "old", "legacy record")
        store.db.execute("ALTER TABLE task_scopes DROP COLUMN research_mandate_json")
        store.db.execute("ALTER TABLE task_scopes DROP COLUMN continuation_json")
        store.db.execute("DROP TABLE research_checkpoints")
        store.db.commit()
    with RuntimeStore(runtime.store_path) as store:
        migrated = store.task(legacy["task_id"])
        assert migrated["objective"] == "old" and migrated["status"] == "queued"
        assert migrated["scope"]["research_mandate"] is None
        assert migrated["scope"]["continuation"] is None
        assert store.research_checkpoints("legacy") == []


def test_failure_recovery_can_read_previous_checkpoint_and_child_without_rescheduling(tmp_path):
    runtime, researcher = setup(tmp_path)
    bridge = ToolBridge(runtime.root, researcher["task_id"])
    saved = checkpoint(bridge, "save-before-interruption")
    child = bridge.call({"tool": "rsi_delegate", "call_id": "child", "args": {
        "role": "researcher", "objective": "diagnostic", "idea_id": "idea"}})
    assert saved["ok"] and child["ok"]
    with RuntimeStore(runtime.store_path) as store:
        coordinator = store.task(researcher["scope"]["parent_task_id"])
        store.finish_task(coordinator["task_id"], coordinator["lease_token"], {"payload": {"summary": "delegated"}})
        store.finish_task(researcher["task_id"], researcher["lease_token"], {"error": "provider interrupted"}, status="failed")
        recovery_id = runtime._schedule_tool_continuation(store, store.task(researcher["task_id"]))
        dependency = store.claim_task("autonomy")
        assert dependency["task_id"] == child["task_id"]
        store.finish_task(dependency["task_id"], dependency["lease_token"], {"payload": {"summary": "diagnostic ready"}})
        recovered = store.claim_task("autonomy")
        assert recovered["task_id"] == recovery_id
        assert recovered["scope"]["research_mandate"] == researcher["scope"]["research_mandate"]
        context = runtime._checkpoint_context(store, recovered)
        saved_id = store.research_checkpoints("autonomy", researcher["task_id"])[0]["checkpoint_id"]
        assert context[0]["checkpoint_id"] == saved_id
    resumed = ToolBridge(runtime.root, recovery_id)
    previous = resumed.call({"tool": "rsi_status", "call_id": "read-checkpoint", "args": {
        "task_id": researcher["task_id"], "checkpoint_id": saved_id}})
    assert previous["ok"] and previous["research_checkpoint"]["payload"]["summary"] == "changed"
    result = resumed.call({"tool": "rsi_status", "call_id": "read-child", "args": {"task_id": child["task_id"]}})
    assert result["ok"] and result["response"]["summary"] == "diagnostic ready"
