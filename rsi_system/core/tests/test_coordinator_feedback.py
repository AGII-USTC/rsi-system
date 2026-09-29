"""Late evidence must reach coordination before a provisional stop is accepted."""
import pytest
import json

from rsi_system.core.runtime import RuntimeCoordinator, MissionSpec, RuntimeStore
from rsi_system.core.runtime_communications import snapshot_communications, publish_communications
from rsi_system.core.runtime_portfolio import build_portfolio_context, validate_exploration_review
from .test_orchestrator import project
from rsi_system.core.runtime_tools import ToolBridge


@pytest.mark.parametrize("kind", ["critic_report", "analyzer_report",
    "exploration_critic_report", "exploration_analyzer_report"])
def test_real_review_event_schedules_coordinator_while_other_branch_runs(tmp_path, monkeypatch, kind):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("feedback", "Compare independent explanations"))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("feedback", "researcher", "Long independent branch", "pending", idea_id="slow")
        store.claim_task("feedback")
        critic = store.create_task("feedback", "critic", "Challenge assumption", "independent", idea_id="fast")
        claimed = store.claim_task("feedback")
        store.finish_task(claimed["task_id"], claimed["lease_token"], {"summary": "counterexample"})
        store.event("feedback", kind, {"task_id": critic["task_id"], "summary": "central assumption fails"})
    monkeypatch.setattr("rsi_system.core.runtime_parallel.execute_queued", lambda *a, **k: [])
    assert runtime.tick(require_model=False)["status"] == "waiting"
    runtime.tick(require_model=False)
    with RuntimeStore(runtime.store_path) as store:
        reviews = [t for t in store.list_tasks("feedback") if t["role"] == "coordinator"]
        assert len(reviews) == 1
        assert kind in reviews[0]["reason"]


def test_late_counterexample_cannot_auto_stop_when_final_review_budget_exhausted(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("limited", "Test competing explanations", resources={"max_agent_tasks": 2}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("limited", "coordinator", "Plan", "start")
        main = store.claim_task("limited")
        critic = store.create_task("limited", "critic", "Review", "pending", idea_id="challenge")
        store.apply_coordinator_decision(main, {"decision": "stop", "summary": "provisional"}, [])
        claimed = store.claim_task("limited")
        store.finish_task(claimed["task_id"], claimed["lease_token"], {"summary": "counterexample"})
        store.event("limited", "exploration_critic_report", {"task_id": critic["task_id"], "summary": "invalid inference"})
    assert runtime.tick(require_model=False)["reason"] == "final_review_budget_exhausted"
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission("limited")["status"] == "paused"
        assert store.deferred_stop("limited")


def test_portfolio_is_live_and_validated_stop_is_persisted(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("portfolio", "Compare mechanisms", resources={"exploration_review": True}))
    runtime.tick(require_model=False)
    with RuntimeStore(runtime.store_path) as store:
        task = store.claim_task("portfolio")
        prompt = json.loads(runtime._prompt("coordinator", store.mission("portfolio"), task, store).split("\n", 1)[1])
        assert "exploration_portfolio" in prompt["context"]
        contract = prompt["response_contract"]["exploration_review"]["reviewed_event_version"]
        assert "latest_final_review_feedback_version" in contract
        assert "final_review_feedback" in contract
    bridge = ToolBridge(runtime.root, task["task_id"])
    snapshot = bridge.call({"tool": "rsi_research", "call_id": "inventory", "args": {"action": "snapshot"}})
    assert snapshot["ok"] and "exploration_portfolio" in snapshot
    historical = bridge.call({"tool": "rsi_research", "call_id": "historical", "args": {"action": "snapshot", "at_version": 2}})
    assert historical["ok"] and "exploration_portfolio" not in historical
    base = {"decision": "stop", "summary": "Bounded question cannot be tested with available inputs",
            "expected_information": "none without missing data", "budget_impact": "preserve resources",
            "stop_condition": "data unavailable"}
    with RuntimeStore(runtime.store_path) as store:
        with pytest.raises(ValueError, match="requires exploration_review"):
            runtime._handle_coordinator(store, store.mission("portfolio"), task, base)
        assert store.task(task["task_id"])["status"] == "running"
        review = {"alternatives": [], "narrowing_reason": "No feasible measurement from available inputs",
                  "remaining_unknowns": ["mechanism cannot be identified"],
                  "next_test_decision": "obtain inputs before spending execution resources",
                  "reviewed_event_version": store.mission("portfolio")["version"]}
        stop_reframe = {"disposition": "defer", "reason": "No evidence exists to reinterpret yet; inputs are missing",
                        "evidence_refs": [], "next_check": "Obtain the missing measurements",
                        "reopen_condition": "The required inputs become available",
                        "budget_impact": "One reframe now cannot inspect the missing inputs"}
        runtime._handle_coordinator(store, store.mission("portfolio"), task,
                                    {**base, "exploration_review": review,
                                     "stop_reframe_decision": stop_reframe})
        decision = [e for e in store.events("portfolio") if e["kind"] == "coordinator_decision"][-1]
        assert decision["payload"]["exploration_review"] == review
        assert decision["payload"]["stop_reframe_decision"] == {**stop_reframe, "reframe_task_id": None}
        assert store.mission("portfolio")["status"] == "stopped"


def test_final_review_includes_research_publication_after_last_report(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("published", "Review all scientific feedback",
                              resources={"exploration_review": True}))
    with RuntimeStore(runtime.store_path) as store:
        critic = store.create_task("published", "critic", "challenge", "test")
        critic = store.claim_task("published")
        store.finish_task(critic["task_id"], critic["lease_token"], {"summary": "uncertainty"})
        report_version = store.event("published", "critic_report", {"task_id": critic["task_id"]})
        researcher = store.create_task("published", "researcher", "follow up", "answer challenge")
        researcher = store.claim_task("published")
        directory = runtime.runtime_root / "tasks" / researcher["task_id"]
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "handoff.md").write_text("New scientific counterexample after the critic report.\n")
        manifest = snapshot_communications(researcher, directory, {"summary": "new finding"})
        publish_communications(store, researcher, manifest, "completed")
        publication_version = store.mission("published")["version"]
        store.finish_task(researcher["task_id"], researcher["lease_token"], {"summary": "new finding"})
        review_task = store.create_task("published", "coordinator", "final review", "read new finding")
        review_task = store.claim_task("published")
        portfolio = build_portfolio_context(store, "published")
        assert portfolio["latest_review_event_version"] == report_version
        assert portfolio["latest_final_review_feedback_version"] == publication_version
        assert portfolio["final_review_feedback"][-1] == {
            "version": publication_version, "kind": "communication_published",
            "task_id": researcher["task_id"],
            "ref": {"manifest_sha256": next(event["payload"]["manifest_sha256"]
                    for event in store.events("published") if event["version"] == publication_version)}}
        assert "counterexample" not in json.dumps(portfolio["final_review_feedback"])
        response = {"decision": "stop", "summary": "Reviewed the later handoff",
                    "expected_information": "none pending", "budget_impact": "no new calls",
                    "stop_condition": "later handoff reviewed",
                    "exploration_review": {"alternatives": [], "narrowing_reason": "No further controlled test",
                        "remaining_unknowns": ["Generality"], "next_test_decision": "Defer",
                        "reviewed_event_version": report_version}}
        with pytest.raises(ValueError, match="latest final-review feedback"):
            validate_exploration_review(store, "published", response, review_task)
        response["exploration_review"]["reviewed_event_version"] = publication_version
        response["stop_reframe_decision"] = {
            "disposition": "defer", "reason": "The newly published source is insufficient for a new test",
            "evidence_refs": [f"task:{researcher['task_id']}"],
            "next_check": "Acquire an independent source",
            "reopen_condition": "A source with a matched control is available",
            "budget_impact": "One researcher call now lacks a usable control"}
        runtime._handle_coordinator(store, store.mission("published"), review_task, response)
        assert store.mission("published")["status"] == "stopped"


def test_curator_and_coordinator_bookkeeping_do_not_keep_final_review_open(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("quiet", "Stop after review", resources={"exploration_review": True}))
    with RuntimeStore(runtime.store_path) as store:
        store.event("quiet", "communication_published", {"task_id": "curator-1",
            "author_role": "curator", "task_status": "completed", "files": [{"large": "x" * 10000}]})
        store.event("quiet", "communication_published", {"task_id": "coordinator-1",
            "author_role": "coordinator", "task_status": "completed"})
        store.event("quiet", "coordinator_decision", {"decision": "wait", "reviewed_feedback_version": 0})
        task = store.create_task("quiet", "coordinator", "finish", "nothing new")
        task = store.claim_task("quiet")
        portfolio = build_portfolio_context(store, "quiet")
        assert portfolio["latest_final_review_feedback_version"] == 0
        assert portfolio["final_review_feedback"] == []
        runtime._handle_coordinator(store, store.mission("quiet"), task, {
            "decision": "stop", "summary": "No new scientific feedback",
            "expected_information": "none pending", "budget_impact": "no new calls",
            "stop_condition": "no scientific feedback remains",
            "exploration_review": {"alternatives": [], "narrowing_reason": "No new work",
                "remaining_unknowns": [], "next_test_decision": "Stop", "reviewed_event_version": 0}})
        assert store.mission("quiet")["status"] == "stopped"
        assert store.deferred_stop("quiet") is None


@pytest.mark.parametrize("kind", ["critic_report", "run_registered", "exploration_feedback",
                                 "communication_published"])
def test_feedback_arriving_during_coordinator_thinking_requires_review_even_with_empty_queue(tmp_path, kind):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("late", "Do not miss counterexamples"))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("late", "coordinator", "Decide", "initial")
        main = store.claim_task("late")
        payload = {"summary": "a result arrived after the frozen prompt"}
        if kind == "communication_published":
            payload.update({"author_role": "researcher", "task_status": "completed"})
        store.event("late", kind, payload)
        store.apply_coordinator_decision(main, {"decision": "stop", "summary": "stale stop"}, [])
        assert store.mission("late")["status"] == "active"
        assert store.deferred_stop("late")
    result = runtime.tick(require_model=False)
    assert result["reason"] == "final_research_review"
    with RuntimeStore(runtime.store_path) as store:
        final = store.claim_task("late")
        assert final["task_id"] == result["task_id"]
        store.apply_coordinator_decision(final, {"decision": "stop", "summary": "now reviewed"}, [])
        assert store.mission("late")["status"] == "stopped"
