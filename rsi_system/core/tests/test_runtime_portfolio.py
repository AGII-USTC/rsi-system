"""The exploration inventory reports facts and validates traceable stop reviews."""
import pytest

from rsi_system.core.runtime import RuntimeCoordinator
from rsi_system.core.runtime_portfolio import (
    build_portfolio_context, validate_exploration_review,
)
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_types import MissionSpec


def setup_mission(tmp_path, *, strict=False, max_agent_tasks=None):
    runtime = RuntimeCoordinator(tmp_path)
    resources = {"exploration_review": strict}
    if max_agent_tasks is not None:
        resources["max_agent_tasks"] = max_agent_tasks
    runtime.start(MissionSpec("m", "Investigate a mechanism", resources=resources))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "coordinator", "plan", "review research")
        actor = store.claim_task("m")
        root = store.research_node("m", "m-root")
        store.apply_research_operation("m", "create", {
            "node_id": "alternative", "question": "Could a different update rule work?",
            "scope": "Investigate update timing", "reason": "Unresolved mechanism",
            "parent": {"node_id": root["node_id"], "revision": root["revision"]},
        }, actor_task_id=actor["task_id"])
    return runtime, actor


def review(version, alternatives=None):
    return {"decision": "stop", "exploration_review": {
        "alternatives": [] if alternatives is None else alternatives,
        "narrowing_reason": "Remaining checks are outside this Mission budget",
        "remaining_unknowns": ["Whether delayed updates generalize"],
        "next_test_decision": "Preserve a controlled delayed-update comparison for the next Mission",
        "reviewed_event_version": version,
    }}


def alternative(node_id="alternative", revision=1):
    return {"node_id": node_id, "revision": revision,
            "mechanism": "Delay the update until a stable observation",
            "disposition": "defer", "reason": "Needs another controlled Run",
            "next_test": "Compare update timing under the same evaluator"}


def test_portfolio_reports_nodes_work_runs_and_review_cursor(tmp_path):
    runtime, actor = setup_mission(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        researcher = store.create_task("m", "researcher", "study alternative", "unresolved",
                                       research_node_id="alternative", research_revision=1)
        runner = store.create_task("m", "runner", "probe", "control", idea_id="idea-a",
                                   research_node_id="alternative", research_revision=1)
        run = store.create_run("m", None, runner["task_id"], "exploration", "completed",
                               None, {}, {}, idea_ref="idea-a")
        critic = store.create_task("m", "critic", "review", "independent check", idea_id="idea-a",
                                   research_node_id="alternative", research_revision=1)
        event_version = store.event("m", "exploration_critic_report", {
            "task_id": critic["task_id"], "idea_id": "idea-a", "payload": {"risks": ["weak control"]}})
        context = build_portfolio_context(store, "m")
        branch = next(item for item in context["nodes"] if item["node_id"] == "alternative")
        assert {researcher["task_id"], runner["task_id"], critic["task_id"]} <= set(branch["task_ids"])
        assert branch["run_ids"] == [run["run_id"]]
        assert branch["run_status_counts"] == {"completed": 1}
        assert branch["runs"][0]["research_revision"] == 1
        assert {item["research_revision"] for item in branch["tasks"]} == {1}
        assert "alternative" not in context["nodes_without_runs"]
        assert "alternative" in context["nodes_with_open_findings"]
        assert context["latest_review_event_version"] == event_version
        assert context["latest_final_review_feedback_version"] == event_version
        assert context["final_review_feedback"][-1] == {
            "version": event_version, "kind": "exploration_critic_report", "task_id": critic["task_id"],
            "ref": {"idea_id": "idea-a"}}
        assert context["review_reports"][-1]["research_node_id"] == "alternative"
        assert context["remaining_agent_tasks"] is None


def test_strict_stop_requires_review_but_legacy_decisions_remain_valid(tmp_path):
    runtime, actor = setup_mission(tmp_path, strict=True)
    with RuntimeStore(runtime.store_path) as store:
        with pytest.raises(ValueError, match="stop requires exploration_review"):
            validate_exploration_review(store, "m", {"decision": "stop"}, actor)
        assert validate_exploration_review(store, "m", {"decision": "schedule"}, actor) is None
    other, other_actor = setup_mission(tmp_path / "other")
    with RuntimeStore(other.store_path) as store:
        assert validate_exploration_review(store, "m", {"decision": "stop"}, other_actor) is None


def test_review_requires_current_report_cursor_and_integer_version(tmp_path):
    runtime, actor = setup_mission(tmp_path, strict=True)
    with RuntimeStore(runtime.store_path) as store:
        critic = store.create_task("m", "critic", "challenge", "test", idea_id="idea-a")
        critic = store.claim_task("m")
        store.finish_task(critic["task_id"], critic["lease_token"], {"summary": "challenge"})
        report_version = store.event("m", "critic_report", {"task_id": critic["task_id"], "payload": {}})
        store.finish_task(actor["task_id"], actor["lease_token"], {"summary": "initial plan"})
        fresh = store.create_task("m", "coordinator", "review", "after report")
        fresh = store.claim_task("m")
        with pytest.raises(ValueError, match="latest final-review feedback"):
            validate_exploration_review(store, "m", review(report_version - 1), fresh)
        with pytest.raises(ValueError, match="reviewed_event_version is invalid"):
            validate_exploration_review(store, "m", review(True), fresh)
        store.event("m", "direct_message", {"task_id": critic["task_id"], "text": "status ping"})
        assert validate_exploration_review(store, "m", review(report_version), fresh)[
            "reviewed_event_version"] == report_version


def test_low_budget_and_empty_alternatives_do_not_block_reasoned_stop(tmp_path):
    runtime, actor = setup_mission(tmp_path, strict=True, max_agent_tasks=1)
    with RuntimeStore(runtime.store_path) as store:
        assert store.remaining_agent_tasks("m") == 0
        normalized = validate_exploration_review(store, "m", review(store.mission("m")["version"]), actor)
        assert normalized["alternatives"] == []
        assert normalized["narrowing_reason"]
        missing_reason = review(store.mission("m")["version"])
        missing_reason["exploration_review"]["narrowing_reason"] = ""
        with pytest.raises(ValueError, match="narrowing_reason"):
            validate_exploration_review(store, "m", missing_reason, actor)


def test_alternative_refs_are_exact_and_mission_scoped(tmp_path):
    runtime, actor = setup_mission(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        store.create_mission(MissionSpec("other", "Investigate another mechanism"))
        other_actor = store.create_task("other", "coordinator", "plan", "research")
        claimed_other = store.claim_task("other")
        assert claimed_other["task_id"] == other_actor["task_id"]
        store.apply_research_operation("other", "create", {
            "node_id": "foreign", "question": "A separate mechanism?",
            "scope": "Other Mission", "reason": "Separate scope",
        }, actor_task_id=claimed_other["task_id"])
        version = store.mission("m")["version"]
        normalized = validate_exploration_review(store, "m", review(version, [alternative()]), actor)
        assert normalized["alternatives"][0]["node_id"] == "alternative"
        for bad in (alternative("foreign"), alternative(revision=2)):
            with pytest.raises(ValueError, match="outside this Mission"):
                validate_exploration_review(store, "m", review(version, [bad]), actor)
        with pytest.raises(ValueError, match="positive integer"):
            validate_exploration_review(store, "m", review(version, [alternative(revision=True)]), actor)
