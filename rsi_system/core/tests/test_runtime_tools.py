"""Durable tool dispatch tests; injected Agent output is not research validation."""
from pathlib import Path
import hashlib
import json
import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.pi_agent import final_payload
from rsi_system.core.runtime_tools import ToolBridge
from rsi_system.core.runtime_communications import prepare_communications
from .test_orchestrator import project


def decision():
    return {"decision": "schedule", "summary": "dispatched a bounded collaborator",
            "tasks": [], "expected_information": "implementation evidence",
            "budget_impact": "one Agent task", "stop_condition": "after comparing the evidence"}


def setup_parent(root, *, budget=3):
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("tool-mission", "compare an Idea with a small probe",
        resources={"max_agent_tasks": 8, "max_runs": 2, "max_seconds_per_run": 5},
        authorization={"allow_code_candidates": True, "allow_server_simulation": True}))
    with RuntimeStore(coordinator.store_path) as store:
        parent = store.create_task("tool-mission", "researcher", "investigate", "test",
                                   idea_id="idea-a", delegate_budget=budget)
        return coordinator, store.claim_task("tool-mission"), parent


def test_audit_bash_is_role_scoped_bounded_and_idempotent(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    with RuntimeStore(coordinator.store_path) as store:
        child = store.create_task("tool-mission", "critic", "independently recompute",
                                  "test", parent_task_id=parent["task_id"], idea_id="idea-a")
        claimed = store.claim_task("tool-mission")
        assert claimed["task_id"] == child["task_id"]
    calls = []

    def fake_audit(**kwargs):
        calls.append(kwargs["command"])
        return {"kind": "diagnostic_audit", "exit_code": 0,
                "command_sha256": hashlib.sha256(kwargs["command"].encode()).hexdigest()}

    monkeypatch.setattr("rsi_system.core.audit_shell.run_audit_shell", fake_audit)
    critic = ToolBridge(root, child["task_id"])
    first = {"tool": "rsi_audit_bash", "call_id": "audit-0", "args": {"command": "python3 audit.py"}}
    result = critic.call(first)
    assert result["ok"] and result["exit_code"] == 0
    assert critic.call(first) == result
    assert calls == ["python3 audit.py"]
    assert not critic.call({**first, "args": {"command": "different"}})["ok"]
    researcher = ToolBridge(root, parent["task_id"])
    assert not researcher.call({"tool": "rsi_audit_bash", "call_id": "unauthorized",
                                "args": {"command": "pwd"}})["ok"]
    for index in range(1, 8):
        assert critic.call({"tool": "rsi_audit_bash", "call_id": f"audit-{index}",
                            "args": {"command": "pwd"}})["ok"]
    assert not critic.call({"tool": "rsi_audit_bash", "call_id": "audit-8",
                            "args": {"command": "pwd"}})["ok"]
    assert len(calls) == 8


def test_delegate_wait_probe_returns_child_notes_and_raw_run(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)

    def child_agent(**kwargs):
        assert kwargs["role"] == "coder"
        workspace = Path(kwargs["output_dir"]) / "workspace"
        (workspace / "probe.py").write_text("import os\nprint('probe-evidence=7')\n")
        (workspace / "handoff.md").write_text("The mechanism predicts a difference; run the probe.\n")
        return {"payload": {"summary": "probe implementation ready", "code_change": False}, "tool_calls": []}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", child_agent)
    bridge = ToolBridge(root, parent["task_id"])
    args = {"role": "coder", "objective": "implement a discriminating probe", "idea_id": "idea-a",
            "candidate_id": ""}
    delegated = bridge.call({"tool": "rsi_delegate", "call_id": "call-coder|fc_123", "args": args})
    assert delegated["ok"] and delegated["status"] == "queued"
    assert bridge.call({"tool": "rsi_delegate", "call_id": "call-coder|fc_123", "args": args}) == delegated
    assert not bridge.call({"tool": "rsi_delegate", "call_id": "call-coder|fc_123",
                            "args": {**args, "objective": "different"}})["ok"]
    coded = bridge.call({"tool": "rsi_wait", "call_id": "call-wait-coder",
                         "args": {"task_id": delegated["task_id"]}})
    assert coded["ok"] and coded["status"] == "completed"
    assert "mechanism predicts" in coded["handoff"][0]["text"]
    script_ref = next(item for item in coded["handoff_files"] if item["workspace_path"] == "probe.py")
    assert script_ref["path"] == "notes/probe.py"
    opened = bridge.call({"tool": "rsi_read_handoff", "call_id": "read-coder-probe",
                          "args": {"task_id": delegated["task_id"], "file_id": script_ref["file_id"]}})
    assert opened["ok"] and "probe-evidence=7" in opened["text"]
    assert opened["next_offset"] is None
    assert bridge.call({"tool": "rsi_status", "call_id": "call-status",
                        "args": {"task_id": delegated["task_id"]}})["status"] == "completed"

    experiment = {"source_task_id": delegated["task_id"], "script_file_id": script_ref["file_id"],
                  "question": "Does the probe run?", "baseline": "no measurement",
                  "expected_difference": "prints a diagnostic", "falsifier": "no diagnostic",
                  "source_reason": "small authored probe"}
    scheduled = bridge.call({"tool": "rsi_run_probe", "call_id": "call-probe",
                             "args": {"objective": "execute controlled probe", "idea_id": "idea-a",
                                      "experiment": experiment}})
    assert scheduled["ok"] and scheduled["role"] == "runner"
    observed = bridge.call({"tool": "rsi_wait", "call_id": "call-wait-probe",
                            "args": {"task_id": scheduled["task_id"]}})
    assert observed["ok"] and observed["status"] == "completed"
    assert observed["run"]["status"] == "completed"
    assert "probe-evidence=7" in observed["run"]["logs"]["stdout.log"]
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_runs("tool-mission")) == 1
        assert len([t for t in store.list_tasks("tool-mission") if t["role"] == "researcher"]) == 1
        store.validate_task(parent)
        store.finish_task(parent["task_id"], parent["lease_token"], {"summary": "evidence inspected"})


def test_researcher_registers_hypothesis_and_candidate_then_dispatches_coder(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    bridge = ToolBridge(root, parent["task_id"])
    hypothesis_args = {
        "hypothesis_id": "h-live",
        "idea_id": "idea-a",
        "statement": "A selective update reduces prediction error.",
        "mechanism": "Updates retain informative transitions and discard noisy ones.",
        "assumptions": ["The transition stream is observable at decision time."],
        "predictions": ["Selective updates reduce post-change RMSE."],
        "experimental_design": {"arms": ["selective", "matched_random"]},
        "prior_art_status": {"searched": True, "sources": ["https://example.test/paper"]},
        "status": "candidate_ready", "expected_version": 0,
    }
    registered_h = bridge.call({"tool": "rsi_register_hypothesis", "call_id": "register-h",
                                "args": hypothesis_args})
    assert registered_h["ok"] and registered_h["version"] == 1
    assert bridge.call({"tool": "rsi_register_hypothesis", "call_id": "register-h",
                        "args": hypothesis_args}) == registered_h
    candidate_args = {"candidate_id": "cand-live", "idea_id": "idea-a",
                      "hypothesis_id": "h-live", "hypothesis_version": 1,
                      "protocol_id": "protocol-a", "config": {"update": "selective"},
                      "purpose": "controlled candidate comparison"}
    registered_c = bridge.call({"tool": "rsi_register_candidate", "call_id": "register-c",
                                "args": candidate_args})
    assert registered_c["ok"] and registered_c["candidate_id"] == "cand-live"
    coder = bridge.call({"tool": "rsi_delegate", "call_id": "dispatch-coder",
                         "args": {"role": "coder", "objective": "implement the candidate",
                                  "idea_id": "idea-a", "candidate_id": "cand-live"}})
    assert coder["ok"] and coder["role"] == "coder"
    assert bridge.call({"tool": "rsi_status", "call_id": "candidate-status",
                        "args": {"task_id": coder["task_id"]}})["candidate_id"] == "cand-live"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.hypothesis("h-live")["version"] == 1
        assert store.candidate("cand-live")["hypothesis_version"] == 1
        assert store.task(coder["task_id"])["scope"]["parent_task_id"] == parent["task_id"]


def test_candidate_registration_is_branch_scoped_and_role_scoped(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    bridge = ToolBridge(root, parent["task_id"])
    bad_h = bridge.call({"tool": "rsi_register_hypothesis", "call_id": "bad-idea",
                         "args": {"hypothesis_id": "h-other", "idea_id": "other",
                                  "statement": "x", "mechanism": "y", "assumptions": ["a"],
                                  "predictions": ["p"], "experimental_design": "d",
                                  "prior_art_status": "s", "expected_version": 0}})
    assert not bad_h["ok"] and "assigned Idea" in bad_h["error"]["message"]
    good_h = bridge.call({"tool": "rsi_register_hypothesis", "call_id": "good-h",
                          "args": {"hypothesis_id": "h-role", "idea_id": "idea-a",
                                   "statement": "x", "mechanism": "y", "assumptions": ["a"],
                                   "predictions": ["p"], "experimental_design": "d",
                                   "prior_art_status": "s", "status": "candidate_ready",
                                   "expected_version": 0}})
    assert good_h["ok"]
    candidate = bridge.call({"tool": "rsi_register_candidate", "call_id": "candidate-role",
                             "args": {"candidate_id": "c-role", "idea_id": "idea-a",
                                      "hypothesis_id": "h-role", "hypothesis_version": 1,
                                      "protocol_id": "p", "config": {}}})
    assert candidate["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        child = store.create_task("tool-mission", "coder", "coder", "test",
                                  idea_id="idea-a", candidate_id="c-role",
                                  parent_task_id=parent["task_id"])
        claimed = store.claim_task("tool-mission")
    assert claimed["task_id"] == child["task_id"]
    coder_bridge = ToolBridge(root, claimed["task_id"])
    rejected = coder_bridge.call({"tool": "rsi_register_candidate", "call_id": "coder-register",
                                  "args": {"candidate_id": "c-two", "idea_id": "idea-a",
                                           "hypothesis_id": "h-role", "hypothesis_version": 1,
                                           "protocol_id": "p", "config": {}}})
    assert not rejected["ok"] and "research roles" in rejected["error"]["message"]


def test_registration_repairs_unambiguous_shapes_and_persists_rejected_calls(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    bridge = ToolBridge(root, parent["task_id"])
    repaired = bridge.call({"tool": "rsi_register_hypothesis", "call_id": "stringified",
        "args": {"hypothesis_id": "h-repair", "idea_id": "idea-a", "statement": "claim",
                 "mechanism": "mechanism", "assumptions": "[\"observable input\"]",
                 "predictions": "[\"lower error\"]", "experimental_design": "{\"arms\":[\"a\",\"b\"]}",
                 "prior_art_status": "searched", "status": "candidate-ready", "expected_version": "0"}})
    assert repaired["ok"] and repaired["normalizations"]
    rejected = bridge.call({"tool": "rsi_register_hypothesis", "call_id": "bad-shape",
        "args": {"hypothesis_id": "h-bad", "idea_id": "idea-a", "statement": "claim",
                 "assumptions": ["a"], "predictions": ["p"], "experimental_design": "d",
                 "prior_art_status": "s", "expected_version": 0}})
    assert not rejected["ok"] and rejected["error"]["repair"]
    with RuntimeStore(coordinator.store_path) as store:
        operation = store.tool_operation(parent["task_id"], hashlib.sha256(b"bad-shape").hexdigest())
        assert operation["status"] == "failed"


def test_agent_json_parser_accepts_fence_and_short_preamble(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "content": [{"type": "text",
            "text": "Here is the result:\n```json\n{\"summary\":\"ok\"}\n```\n"}]}}) + "\n")
    payload, _ = final_payload(events)
    assert payload == {"summary": "ok"}


def test_legacy_formal_fields_are_best_effort_after_tool_registration(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    bridge = ToolBridge(root, parent["task_id"])
    args = {"hypothesis_id": "h-dup", "idea_id": "idea-a", "statement": "claim",
            "mechanism": "mechanism", "assumptions": ["a"], "predictions": ["p"],
            "experimental_design": "design", "prior_art_status": "searched",
            "status": "candidate_ready", "expected_version": 0}
    assert bridge.call({"tool": "rsi_register_hypothesis", "call_id": "h-dup", "args": args})["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        coordinator._handle_research(store, store.mission("tool-mission"), parent,
                                      {"summary": "final summary", "hypotheses": [args]}, best_effort=True)
        errors = [event for event in store.events("tool-mission") if event["kind"] == "research_record_rejected"]
        assert errors and errors[-1]["payload"]["kind"] == "hypothesis"


def test_coordinator_can_cancel_queued_branch_and_request_safe_running_stop(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("control-mission", "intervene", authorization={"allow_code_candidates": True}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("control-mission", "coordinator", "choose", "test")
        owner = store.claim_task("control-mission")
        branch = store.create_task("control-mission", "researcher", "branch", "test",
                                   idea_id="idea-a", parent_task_id=owner["task_id"])
        live_branch = store.claim_task("control-mission")
        child = store.create_task("control-mission", "coder", "child", "test",
                                  idea_id="idea-a", parent_task_id=live_branch["task_id"])
    bridge = ToolBridge(root, owner["task_id"])
    stopped = bridge.call({"tool": "rsi_branch_control", "call_id": "stop-running",
                           "args": {"task_id": branch["task_id"], "reason": "duplicate direction"}})
    assert stopped["ok"] and stopped["cancelled_queued"] == [child["task_id"]]
    assert stopped["stop_requested_running"] == [branch["task_id"]]
    with RuntimeStore(coordinator.store_path) as store:
        assert store.task(child["task_id"])["status"] == "cancelled"
        queued_branch = store.create_task("control-mission", "researcher", "queued branch", "test",
                                          idea_id="idea-c", parent_task_id=owner["task_id"])
        assert store.task(queued_branch["task_id"])["status"] == "queued"
    stopped_queued = bridge.call({"tool": "rsi_branch_control", "call_id": "stop-queued",
                                  "args": {"task_id": queued_branch["task_id"], "reason": "no value"}})
    assert stopped_queued["ok"] and stopped_queued["cancelled_queued"] == [queued_branch["task_id"]]
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("control-mission", "researcher", "live branch", "test",
                          idea_id="idea-b", parent_task_id=owner["task_id"])
        live = store.claim_task("control-mission")
    requested = bridge.call({"tool": "rsi_branch_control", "call_id": "stop-running-2",
                             "args": {"task_id": live["task_id"], "reason": "evidence is already decisive"}})
    assert requested["ok"] and requested["stop_requested_running"] == [live["task_id"]]
    with RuntimeStore(coordinator.store_path) as store:
        assert any(event["kind"] == "direct_message" and event["payload"]["to_task_id"] == live["task_id"]
                   for event in store.events("control-mission"))


def test_probe_rejects_missing_source_file_before_queueing(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)

    def child_agent(**kwargs):
        (Path(kwargs["output_dir"]) / "workspace" / "probe.py").write_text("print('ready')\n")
        return {"payload": {"summary": "script ready"}, "tool_calls": []}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", child_agent)
    bridge = ToolBridge(root, parent["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "coder",
                         "args": {"role": "coder", "objective": "write a probe", "idea_id": "idea-a"}})
    assert bridge.call({"tool": "rsi_wait", "call_id": "coder-result",
                        "args": {"task_id": child["task_id"]}})["status"] == "completed"
    bad = bridge.call({"tool": "rsi_run_probe", "call_id": "missing-script",
                       "args": {"objective": "run probe", "idea_id": "idea-a",
                                "experiment": {"source_task_id": child["task_id"],
                                               "script": "notes/missing.py", "question": "Does it run?",
                                               "baseline": "unchanged", "expected_difference": "output",
                                               "falsifier": "no output", "source_reason": "test"}}})
    assert not bad["ok"] and "file_id" in bad["error"]["message"]
    with RuntimeStore(coordinator.store_path) as store:
        assert not [task for task in store.list_tasks("tool-mission") if task["role"] == "runner"]
        assert store.remaining_runs("tool-mission") == 2


def test_rejected_delegate_can_be_corrected_and_legacy_budget_is_ignored(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root, budget=1)
    bridge = ToolBridge(root, parent["task_id"])
    bad = bridge.call({"tool": "rsi_delegate", "call_id": "bad",
                       "args": {"role": "coder", "objective": "probe", "idea_id": "other"}})
    assert not bad["ok"] and "Idea" in bad["error"]["message"]
    good = bridge.call({"tool": "rsi_delegate", "call_id": "good",
                        "args": {"role": "coder", "objective": "probe", "idea_id": "idea-a"}})
    assert good["ok"]
    message = {"text": "Please compare the planned probe with the baseline.",
               "idea_id": "idea-a", "to_task_id": good["task_id"]}
    delivered = bridge.call({"tool": "rsi_message", "call_id": "message|fc_456", "args": message})
    assert delivered["ok"]
    assert bridge.call({"tool": "rsi_message", "call_id": "message|fc_456", "args": message}) == delivered
    second = bridge.call({"tool": "rsi_delegate", "call_id": "extra",
                          "args": {"role": "coder", "objective": "extra", "idea_id": "idea-a"}})
    assert second["ok"] and second["delegation_limit"] is None
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_tasks("tool-mission")) == 3
        assert len(store.tool_operations(parent["task_id"])) == 4
        assert sum(item["status"] == "failed" for item in store.tool_operations(parent["task_id"])) == 1
        assert len([event for event in store.events("tool-mission") if event["kind"] == "direct_message"]) == 1


@pytest.mark.parametrize("role", ["researcher", "coder", "analyzer", "critic", "hypothesis"])
def test_assignment_brief_reaches_collaborator_without_changing_role(tmp_path, role):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    mandate = {"mode": "direction", "responsibility": "Repair and freeze the qualification script",
               "constraints": [{"statement": "Keep pi0.5 weights frozen", "source_ref": "mission"}],
               "observations": [{"statement": "The prior script failed before measurement",
                                  "source_refs": ["run:failed-probe"]}],
               "suggestions": ["Persist diagnostics before assertions"]}
    request = {"tool": "rsi_delegate", "call_id": "scoped-work",
               "args": {"role": role, "objective": "Scoped work", "idea_id": "idea-a",
                        "research_mandate": mandate}}
    bridge = ToolBridge(root, parent["task_id"])
    result = bridge.call(request)
    assert result["ok"], result
    assert bridge.call(request) == result  # Replayed calls do not create another child.
    with RuntimeStore(coordinator.store_path) as store:
        child = store.task(result["task_id"])
        assert child["role"] == role and child["scope"]["research_mandate"] == mandate
        assert len(store.list_tasks("tool-mission")) == 2
        claimed = store.claim_task("tool-mission")
        prompt = coordinator._prompt(role, store.mission("tool-mission"), claimed, store)
        assert "Repair and freeze the qualification script" in prompt
    invalid = {**request, "call_id": "invalid", "args": {**request["args"],
        "research_mandate": {**mandate, "mode": "unsupported"}}}
    assert not bridge.call(invalid)["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_tasks("tool-mission")) == 2


def test_coordinator_decision_can_assign_coder_with_source_linked_brief(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "implement", authorization={"allow_code_candidates": True}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "coordinator", "dispatch", "test")
    mandate = {"mode": "check", "responsibility": "Implement the assigned repair",
               "constraints": [], "observations": [], "suggestions": []}
    result = runtime.tick(agent_runner=lambda **_: {**decision(), "tasks": [
        {"role": "coder", "idea_id": "idea-a", "objective": "repair", "reason": "code failure",
         "research_mandate": mandate}]})
    assert result["status"] == "completed", result
    with RuntimeStore(runtime.store_path) as store:
        child = next(t for t in store.list_tasks("m") if t["role"] == "coder")
        assert child["scope"]["research_mandate"] == mandate


@pytest.mark.parametrize("role", ["coordinator", "researcher", "hypothesis", "coder", "runner", "analyzer", "critic"])
@pytest.mark.parametrize("legacy_budget", [None, 0, 6])
def test_every_agent_can_delegate_more_than_six_children(tmp_path, role, legacy_budget):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "unlimited delegation",
                             authorization={"allow_code_candidates": True}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", role, "explore", "test", idea_id="idea-a",
                          delegate_budget=legacy_budget)
        parent = store.claim_task("m")
    bridge = ToolBridge(root, parent["task_id"])
    children = []
    for index in range(10):
        request = {"tool": "rsi_delegate", "call_id": f"child-{index}",
                   "args": {"role": "coder", "objective": f"probe {index}", "idea_id": "idea-a",
                            "delegate_budget": 0}}
        result = bridge.call(request)
        assert result["ok"] and result["delegation_limit"] is None
        assert result["remaining_agent_tasks"] is None
        assert bridge.call(request) == result
        children.append(result["task_id"])
    with RuntimeStore(runtime.store_path) as store:
        assert len(set(children)) == 10
        assert len(store.list_tasks("m")) == 11
        assert all(task["scope"]["delegate_budget"] is None for task in store.list_tasks("m"))


def test_unlimited_nested_delegation_survives_legacy_scope_migration(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "nested collaboration"))
    with RuntimeStore(runtime.store_path) as store:
        root_task = store.create_task("m", "researcher", "root", "test", idea_id="idea-a")
        store.db.execute("UPDATE task_scopes SET delegate_budget=0 WHERE task_id=?", (root_task["task_id"],))
        store.db.commit()
    with RuntimeStore(runtime.store_path) as store:
        assert store.db.execute("SELECT delegate_budget FROM task_scopes").fetchone()[0] == -1
        parent = store.claim_task("m")
    for depth in range(1, 9):
        result = ToolBridge(root, parent["task_id"]).call({
            "tool": "rsi_delegate", "call_id": f"level-{depth}",
            "args": {"role": "researcher", "objective": f"level {depth}", "idea_id": "idea-a"}})
        assert result["ok"]
        with RuntimeStore(runtime.store_path) as store:
            store.finish_task(parent["task_id"], parent["lease_token"], {"summary": "delegated"})
            child = store.claim_task("m")
            assert child["task_id"] == result["task_id"]
            assert child["scope"]["parent_task_id"] == parent["task_id"]
            assert child["scope"]["depth"] == depth
            assert child["scope"]["delegate_budget"] is None
            parent = child


def test_expired_parent_continues_without_duplicate_child(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root, budget=3)
    bridge = ToolBridge(root, parent["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "spawn",
                         "args": {"role": "coder", "objective": "make probe", "idea_id": "idea-a"}})
    assert child["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                         (parent["task_id"],))
        store.db.commit()
    result = coordinator.tick(require_model=False)
    assert result["reason"] == "tool_result_recovery"
    with RuntimeStore(coordinator.store_path) as store:
        tasks = store.list_tasks("tool-mission")
        assert len(tasks) == 3
        assert store.task(child["task_id"])["status"] == "queued"
        continued = store.task(result["task_ids"][0])
        assert continued["scope"]["reply_to_task_id"] == parent["task_id"]
        assert continued["scope"]["delegate_budget"] is None
    with RuntimeStore(coordinator.store_path) as store:
        child_task = store.claim_task("tool-mission")
        assert child_task["task_id"] == child["task_id"]
        store.finish_task(child_task["task_id"], child_task["lease_token"],
                          {"summary": "cancelled for recovery test"}, status="cancelled")
        resumed_parent = store.claim_task("tool-mission")
        assert resumed_parent["task_id"] == continued["task_id"]
    resumed = ToolBridge(root, continued["task_id"])
    assert resumed.call({"tool": "rsi_delegate", "call_id": "after-recovery",
                         "args": {"role": "critic", "objective": "review recovered evidence",
                                  "idea_id": "idea-a"}})["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        assert len([t for t in store.list_tasks("tool-mission") if t["objective"] == "make probe"]) == 1


def test_coordinator_finishes_after_tool_dispatch_without_json_task_list(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "audit", resources={"max_agent_tasks": 3}))
    assert coordinator.tick(require_model=False)["status"] == "scheduled"

    def agent(**kwargs):
        bridge = ToolBridge(root, kwargs["task_id"])
        sent = bridge.call({"tool": "rsi_delegate", "call_id": "coordinator-spawn",
                            "args": {"role": "researcher", "objective": "check the Idea literature",
                                     "idea_id": "idea-a", "delegate_budget": 1}})
        assert sent["ok"]
        return decision()

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        tasks = store.list_tasks("m")
        assert [task["role"] for task in tasks] == ["coordinator", "researcher"]
        assert tasks[1]["scope"]["parent_task_id"] == tasks[0]["task_id"]
        assert store.events("m")[-1]["kind"] == "communication_published"


def test_completed_child_result_is_in_interrupted_parent_continuation(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root, budget=3)

    def child_agent(**kwargs):
        workspace = Path(kwargs["output_dir"]) / "workspace"
        (workspace / "handoff.md").write_text("Completed child finding survives the parent crash.\n")
        return {"payload": {"summary": "measured child result"}, "tool_calls": []}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", child_agent)
    bridge = ToolBridge(root, parent["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "spawn-child",
                         "args": {"role": "critic", "objective": "review Idea", "idea_id": "idea-a"}})
    result = bridge.call({"tool": "rsi_wait", "call_id": "wait-child",
                          "args": {"task_id": child["task_id"]}})
    assert result["status"] == "completed" and result["response"]["summary"] == "measured child result"
    with RuntimeStore(coordinator.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                         (parent["task_id"],))
        store.db.commit()
    recovery = coordinator.tick(require_model=False)
    assert recovery["reason"] == "tool_result_recovery"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.task(child["task_id"])["status"] == "completed"
        assert len([task for task in store.list_tasks("tool-mission") if task["objective"] == "review Idea"]) == 1
        continuation = store.task(recovery["task_ids"][0])
        claimed = store.claim_task("tool-mission")
        assert claimed["task_id"] == continuation["task_id"]
        manifest = prepare_communications(store, claimed,
            coordinator.runtime_root / "tasks" / continuation["task_id"])
        assert child["task_id"] in manifest.read_text()


def test_simultaneous_parent_and_child_expiry_creates_one_continuation(tmp_path):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root, budget=4)
    parent_bridge = ToolBridge(root, parent["task_id"])
    child = parent_bridge.call({"tool": "rsi_delegate", "call_id": "child",
        "args": {"role": "researcher", "objective": "analyze Idea", "idea_id": "idea-a", "delegate_budget": 2}})
    assert child["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        running_child = store.claim_task("tool-mission")
        assert running_child["task_id"] == child["task_id"]
    child_bridge = ToolBridge(root, child["task_id"])
    grandchild = child_bridge.call({"tool": "rsi_delegate", "call_id": "grandchild",
        "args": {"role": "critic", "objective": "critique Idea", "idea_id": "idea-a"}})
    assert grandchild["ok"]
    with RuntimeStore(coordinator.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE status='running'")
        store.db.commit()
    result = coordinator.tick(require_model=False)
    assert result["reason"] == "tool_result_recovery" and len(result["task_ids"]) == 1
    with RuntimeStore(coordinator.store_path) as store:
        assert store.task(parent["task_id"])["status"] == "failed"
        assert store.task(child["task_id"])["status"] == "failed"
        assert store.task(grandchild["task_id"])["status"] == "queued"
        assert len(store.list_tasks("tool-mission")) == 4


def test_new_tool_protocol_does_not_dispatch_final_json_control_fields(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "audit", resources={"max_agent_tasks": 3}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "analyze Idea", "test", idea_id="idea-a", delegate_budget=1)

    def agent(**_kwargs):
        return {"tool_protocol_version": 1, "payload": {"summary": "research interpretation",
            "delegate_tasks": [{"role": "critic", "objective": "duplicate task", "idea_id": "idea-a"}],
            "messages": [{"to_role": "critic", "idea_id": "idea-a", "text": "duplicate message"}]}}

    result = coordinator.tick(agent_runner=agent, require_model=False)
    assert result["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert len(store.list_tasks("m")) == 1
        assert len([event for event in store.events("m") if event["kind"] == "direct_message"]) == 0
        assert [event["kind"] for event in store.events("m")].count("legacy_control_ignored") == 1


def test_runtime_daily_call_override_can_be_unlimited(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "audit"))
    config = coordinator._config()
    config["max_agent_calls_per_day"] = 1
    config["runtime_max_agent_calls_per_day"] = None
    for index in range(2):
        with RuntimeStore(coordinator.store_path) as store:
            store.create_task("m", "researcher", f"question {index}", "test")
            task = store.claim_task("m")
            store.reserve_agent_call(task, config, "researcher")
            store.finish_task(task["task_id"], task["lease_token"], {"summary": "reserved"})
    with RuntimeStore(coordinator.store_path) as store:
        assert store.db.execute("SELECT count(*) FROM runtime_agent_calls").fetchone()[0] == 2


def test_wait_returns_ids_generated_for_researcher_ideas(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "explore sparse reward", resources={"max_agent_tasks": 3}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "coordinator", "choose", "test")
        parent = store.claim_task("m")

    def researcher(**_kwargs):
        return {"payload": {"summary": "two possible mechanisms", "ideas": [
            {"title": "Informative grouping", "content": "Choose groups with reward contrast"},
            {"title": "Novelty shaping", "content": "Test whether extra reward misleads"}]}}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", researcher)
    bridge = ToolBridge(root, parent["task_id"])
    child = bridge.call({"tool": "rsi_delegate", "call_id": "research",
        "args": {"role": "researcher", "objective": "find mechanisms"}})
    assert child["ok"]
    result = bridge.call({"tool": "rsi_wait", "call_id": "research-wait",
        "args": {"task_id": child["task_id"]}})
    assert result["status"] == "completed"
    assert len(result["ideas"]) == 2
    assert all(item["idea_id"].startswith("task-") for item in result["ideas"])


def test_new_coordinator_can_read_prior_mission_task_result(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "continue research"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "literature", "test", idea_id="idea-a")
        finished = store.claim_task("m")
        store.finish_task(finished["task_id"], finished["lease_token"],
                          {"payload": {"summary": "prior finding"}})
        store.create_task("m", "coordinator", "interpret", "test")
        coordinator_task = store.claim_task("m")
    result = ToolBridge(root, coordinator_task["task_id"]).call({
        "tool": "rsi_status", "call_id": "prior-status", "args": {"task_id": finished["task_id"]}})
    assert result["ok"] and result["response"]["summary"] == "prior finding"
