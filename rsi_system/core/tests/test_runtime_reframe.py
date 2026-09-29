"""A mid-course challenge remains independent and must receive a recorded disposition."""
import json

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_feedback import new_research_feedback
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project


def decision(kind, responses=None):
    return {"decision": kind, "summary": "Assess the research tradeoff",
            "expected_information": "A challenge may change the next test",
            "budget_impact": "One bounded researcher task", "stop_condition": "No useful next check",
            "challenge_responses": responses or []}


def report():
    return {"summary": "The ranking depends on which response regime matters",
            "reframe_review": {"problem_statement": "Choose a policy across changing response regimes",
                               "alternative_explanations": ["A regime-aware switch may beat one global policy",
                                                            "The evaluator may favor one regime unfairly"],
                               "discriminating_checks": ["Use a fixed protocol to compare per-regime outcomes"],
                               "recommendation": "Test a regime-aware policy before selecting a winner"}}


def test_midcourse_reframe_is_independent_and_requires_response(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Improve adaptation across response regimes",
                              resources={"max_agent_tasks": 6}))
    with RuntimeStore(runtime.store_path) as store:
        root = store.research_node("m", "m-root")
        coordinator = store.create_task("m", "coordinator", "Choose a next direction",
                                        "Measured methods have different strengths",
                                        research_node_id="m-root", research_revision=root["revision"])
        coordinator = store.claim_task("m")
        store.event("m", "coordinator_decision", {"summary": "SECRET_FAVORED_DIRECTION"})
    bridge = ToolBridge(runtime.root, coordinator["task_id"])
    request = {"tool": "rsi_delegate", "call_id": "reframe", "args": {
        "role": "researcher", "task_kind": "problem_reframe",
        "objective": "Reconsider the Mission question at the regime tradeoff",
        "research_node_id": "m-root", "research_revision": root["revision"]}}
    delegated = bridge.call(request)
    assert delegated["ok"], delegated
    child_id = delegated["task_id"]
    duplicate = bridge.call({**request, "call_id": "duplicate"})
    assert not duplicate["ok"]
    with RuntimeStore(runtime.store_path) as store:
        child = store.task(child_id)
        assert child["scope"]["task_kind"] == "problem_reframe"
        child = store.claim_task("m")
        prompt = runtime._prompt("researcher", store.mission("m"), child, store)
        context = json.loads(prompt.split("\n", 1)[1])["context"]
        assert context["tasks"] == context["events"] == []
        assert "SECRET_FAVORED_DIRECTION" not in prompt
        assert context["independent_reframe"]["decision_point"] == child["objective"]
        store.event("m", "direct_message", {"from_task_id": coordinator["task_id"],
                    "from_role": "coordinator", "to_role": "researcher",
                    "to_task_id": child_id, "idea_id": None,
                    "text": "SECRET_FAVORED_DIRECTION"})
        inbox = ToolBridge(runtime.root, child_id)._inbox(store, child, {})
        assert inbox["messages"] == []
        with pytest.raises(ValueError, match="reframe_review"):
            runtime._handle_research(store, store.mission("m"), child, {"summary": "missing"})
        runtime._handle_research(store, store.mission("m"), child, report())
        store.finish_task(child_id, child["lease_token"], {"payload": report()})
        assert any(item["kind"] == "problem_reframe_report" for item in
                   new_research_feedback(store, "m", coordinator["input_version"]))
        runtime._handle_coordinator(store, store.mission("m"), coordinator, decision("schedule"))
        followup = store.create_task("m", "coordinator", "Review the challenge", "New feedback",
                                     research_node_id="m-root", research_revision=root["revision"])
        followup = store.claim_task("m")
        with pytest.raises(ValueError, match="must respond"):
            runtime._handle_coordinator(store, store.mission("m"), followup, decision("stop"))
        response = {"task_id": child_id, "disposition": "adopt",
                    "reason": "Regime contrast changes the next decision",
                    "next_test": "Compare each regime under a fixed evaluator"}
        runtime._handle_coordinator(store, store.mission("m"), followup, decision("stop", [response]))
        recorded = [event for event in store.events("m") if event["kind"] == "coordinator_decision"][-1]
        assert recorded["payload"]["challenge_responses"] == [response]
        from rsi_system.core.runtime_reframe import validate_challenge_responses
        with pytest.raises(ValueError, match="completed unacknowledged"):
            validate_challenge_responses(store, "m", followup, {"challenge_responses": [response]})


def test_stop_without_useful_reframe_is_allowed(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "A bounded question", resources={"max_agent_tasks": 1}))
    with RuntimeStore(runtime.store_path) as store:
        actor = store.create_task("m", "coordinator", "Assess", "No more useful checks")
        actor = store.claim_task("m")
        runtime._handle_coordinator(store, store.mission("m"), actor, decision("stop"))
        assert store.mission("m")["status"] == "stopped"


def stop_review(store, *, evidence_refs, disposition="defer", reframe_task_id=None):
    return {"decision": "stop", "summary": "Preserve the unresolved route comparison",
            "expected_information": "A matched test may change the ranking",
            "budget_impact": "Keep the remaining resources for a matched test",
            "stop_condition": "The present evidence does not select a winner",
            "exploration_review": {"alternatives": [],
                                   "narrowing_reason": "The current protocol cannot identify one mechanism",
                                   "remaining_unknowns": ["Whether the ranking survives a matched control"],
                                   "next_test_decision": "Defer until the control is available",
                                   "reviewed_event_version": store.mission("m")["version"]},
            "stop_reframe_decision": {
                "disposition": disposition,
                "reason": "The current sources do not make a new reframe more informative than a matched control",
                "evidence_refs": evidence_refs,
                "next_check": "Compare alternatives on the same changing stream",
                "reopen_condition": "A matched changing-stream control is available",
                "budget_impact": "One researcher task would precede that controlled Run",
                **({"reframe_task_id": reframe_task_id} if reframe_task_id else {})}}


def test_stop_with_open_findings_requires_grounded_reframe_decision(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Compare adaptation routes",
                              resources={"exploration_review": True, "max_agent_tasks": 5}))
    with RuntimeStore(runtime.store_path) as store:
        initial = store.create_task("m", "coordinator", "Plan", "Initial comparison")
        initial = store.claim_task("m")
        store.finish_task(initial["task_id"], initial["lease_token"], {"summary": "Await comparison"})
        runner = store.create_task("m", "runner", "Compare routes", "Controlled probe", idea_id="route")
        runner = store.claim_task("m")
        run = store.create_run("m", None, runner["task_id"], "exploration", "completed",
                               None, {}, {}, idea_ref="route")
        store.finish_task(runner["task_id"], runner["lease_token"], {"summary": "Run complete"})
        actor = store.create_task("m", "coordinator", "Review", "A route ranking remains unresolved")
        actor = store.claim_task("m")
        payload = stop_review(store, evidence_refs=[f"run:{run['run_id']}"])
        before = store.mission("m")["version"]
        with pytest.raises(ValueError, match="requires stop_reframe_decision"):
            runtime._handle_coordinator(store, store.mission("m"), actor,
                                        {key: value for key, value in payload.items()
                                         if key != "stop_reframe_decision"})
        assert store.mission("m")["version"] == before
        with pytest.raises(ValueError, match="not published"):
            runtime._handle_coordinator(store, store.mission("m"), actor,
                                        {**payload, "stop_reframe_decision": {
                                            **payload["stop_reframe_decision"],
                                            "evidence_refs": ["run:run-unknown"]}})
        runtime._handle_coordinator(store, store.mission("m"), actor, payload)
        event = [item for item in store.events("m") if item["kind"] == "coordinator_decision"][-1]
        assert event["payload"]["stop_reframe_decision"]["evidence_refs"] == [f"run:{run['run_id']}"]
        assert store.mission("m")["status"] == "stopped"


def test_reviewed_stop_requires_answered_independent_reframe(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Compare adaptation routes",
                              resources={"exploration_review": True, "max_agent_tasks": 4}))
    with RuntimeStore(runtime.store_path) as store:
        initial = store.create_task("m", "coordinator", "Plan", "Initial comparison")
        initial = store.claim_task("m")
        child = store.create_task("m", "researcher", "Reconsider the whole problem",
                                  "Conflicting route evidence", parent_task_id=initial["task_id"],
                                  research_node_id="m-root",
                                  research_revision=1, task_kind="problem_reframe")
        store.finish_task(initial["task_id"], initial["lease_token"],
                          {"summary": "Requested independent view"})
        child = store.claim_task("m")
        runtime._handle_research(store, store.mission("m"), child, report())
        store.finish_task(child["task_id"], child["lease_token"], {"payload": report()})
        actor = store.create_task("m", "coordinator", "Review independent challenge", "Final choice")
        actor = store.claim_task("m")
        payload = stop_review(store, evidence_refs=[], disposition="reviewed",
                              reframe_task_id=child["task_id"])
        with pytest.raises(ValueError, match="must respond"):
            runtime._handle_coordinator(store, store.mission("m"), actor, payload)
        response = {"task_id": child["task_id"], "disposition": "defer",
                    "reason": "A matched control must come first", "next_test": "Use a changing stream"}
        runtime._handle_coordinator(store, store.mission("m"), actor,
                                    {**payload, "challenge_responses": [response]})
        event = [item for item in store.events("m") if item["kind"] == "coordinator_decision"][-1]
        assert event["payload"]["stop_reframe_decision"]["reframe_task_id"] == child["task_id"]


def test_budget_exhaustion_does_not_require_another_reframe(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "A bounded question", resources={
        "exploration_review": True, "max_agent_tasks": 1}))
    with RuntimeStore(runtime.store_path) as store:
        actor = store.create_task("m", "coordinator", "Assess", "No task budget remains")
        actor = store.claim_task("m")
        payload = stop_review(store, evidence_refs=[])
        del payload["stop_reframe_decision"]
        runtime._handle_coordinator(store, store.mission("m"), actor, payload)
        assert store.mission("m")["status"] == "stopped"


def test_old_reframe_cannot_cover_later_counterexample(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("m", "Compare routes", resources={"exploration_review": True}))
    with RuntimeStore(runtime.store_path) as store:
        initial = store.create_task("m", "coordinator", "Plan", "Initial question")
        initial = store.claim_task("m")
        child = store.create_task("m", "researcher", "Reconsider", "Early uncertainty",
                                  parent_task_id=initial["task_id"], research_node_id="m-root",
                                  research_revision=1, task_kind="problem_reframe")
        store.finish_task(initial["task_id"], initial["lease_token"], {"summary": "Delegated"})
        child = store.claim_task("m")
        runtime._handle_research(store, store.mission("m"), child, report())
        store.finish_task(child["task_id"], child["lease_token"], {"payload": report()})
        critic = store.create_task("m", "critic", "Check later result", "Ranking reversed", idea_id="route")
        critic = store.claim_task("m")
        store.finish_task(critic["task_id"], critic["lease_token"], {"summary": "Ranking reversed"})
        store.event("m", "critic_report", {"task_id": critic["task_id"], "summary": "Ranking reversed"})
        actor = store.create_task("m", "coordinator", "Stop or reconsider", "Later counterexample")
        actor = store.claim_task("m")
        payload = stop_review(store, evidence_refs=[], disposition="reviewed",
                              reframe_task_id=child["task_id"])
        response = {"task_id": child["task_id"], "disposition": "defer",
                    "reason": "An independent source now matters", "next_test": "Compare sources"}
        with pytest.raises(ValueError, match="predates newer research feedback"):
            runtime._handle_coordinator(store, store.mission("m"), actor,
                                        {**payload, "challenge_responses": [response]})
