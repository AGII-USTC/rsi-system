from pathlib import Path
import json

import pytest

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore, write_idea
from rsi_system.core.hypothesis import HypothesisSpec


def test_runtime_store_rejects_stale_task_lease(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.create_mission(MissionSpec("m1", "bounded research"))
        task = store.create_task("m1", "researcher", "formulate a question", "start")
        claimed = store.claim_task("m1")
        assert claimed["task_id"] == task["task_id"]
        with pytest.raises(ValueError, match="lease"):
            store.finish_task(task["task_id"], "stale-token", {"summary": "late"})
        finished = store.finish_task(task["task_id"], claimed["lease_token"], {"summary": "done"})
        assert finished["status"] == "completed"


def test_runtime_coordinator_has_dynamic_stop_without_fixed_stages(tmp_path):
    from .test_orchestrator import project
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        runtime_root = Path(directory)
        coordinator.runtime_root = runtime_root
        coordinator.store_path = runtime_root / "state.sqlite3"
        coordinator.lock_path = runtime_root / "coordinator.lock"
        coordinator.start(MissionSpec("m2", "keep competing explanations"))
        assert coordinator.tick(require_model=False)["status"] == "scheduled"

        def fake_agent(**_kwargs):
            return {"payload": {"decision": "stop", "summary": "evidence is currently insufficient",
                                 "expected_information": "none", "budget_impact": "no further calls", "stop_condition": "insufficient evidence",
                                 "ideas": [], "candidates": [], "tasks": []}}

        assert coordinator.tick(agent_runner=fake_agent, require_model=False)["status"] == "completed"
        assert coordinator.status()["active"] is None


def test_coordinator_prompt_contains_live_mission_and_resource_snapshot(tmp_path):
    from .test_orchestrator import project
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "show live resources", resources={"max_agent_tasks": 4, "max_runs": 2}))
    with RuntimeStore(coordinator.store_path) as store:
        task = store.create_task("m", "coordinator", "inspect resources", "test")
        task = store.claim_task("m")
        prompt = coordinator._prompt("coordinator", store.mission("m"), task, store)
    context = json.loads(prompt.split("\n", 1)[1])["context"]
    assert context["runtime_status"]["mission_version"] == context["blackboard_version"]
    assert context["runtime_status"]["remaining_agent_tasks"] == 3
    assert context["runtime_status"]["remaining_runs"] == 2
    assert context["runtime_status"]["queued_or_running_tasks"] == 1


def test_stop_waits_for_queued_collaborator(tmp_path):
    from .test_orchestrator import project
    from rsi_system.core.runtime_tools import ToolBridge

    coordinator = RuntimeCoordinator(project(tmp_path))
    coordinator.start(MissionSpec("m", "review a result"))
    assert coordinator.tick(require_model=False)["status"] == "scheduled"
    seen = []

    def agent(**kwargs):
        if kwargs["role"] == "coordinator":
            if seen:
                return {"payload": {"decision": "stop", "summary": "Reviewed completed collaborator evidence",
                                    "expected_information": "none", "budget_impact": "no further tasks",
                                    "stop_condition": "bounded question answered"}, "tool_protocol_version": 1}
            sent = ToolBridge(coordinator.root, kwargs["task_id"]).call({
                "tool": "rsi_delegate", "call_id": "queue-review",
                "args": {"role": "researcher", "objective": "review the evidence"}})
            assert sent["ok"]
            return {"payload": {"decision": "stop", "summary": "review already queued",
                                "expected_information": "review", "budget_impact": "one task",
                                "stop_condition": "after review"}, "tool_protocol_version": 1}
        seen.append(kwargs["role"])
        return {"summary": "review completed", "literature_review": {"searched": False,
                "queries": [], "sources": [], "findings": [], "disagreements": [], "gaps": [],
                "failure_reason": "queue ordering test"}}

    assert coordinator.tick(agent_runner=agent, require_model=False)["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("m")["status"] == "active"
        assert store.deferred_stop("m")
    assert coordinator.tick(agent_runner=agent, require_model=False)["status"] == "completed"
    assert seen == ["researcher"]
    final_review = coordinator.tick(require_model=False)
    assert final_review["reason"] == "final_research_review"
    assert coordinator.tick(agent_runner=agent, require_model=False)["status"] == "completed"
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("m")["status"] == "stopped"
        assert store.deferred_stop("m") is None


def test_resuming_legacy_stop_drains_existing_queue(tmp_path):
    from .test_orchestrator import project

    coordinator = RuntimeCoordinator(project(tmp_path))
    coordinator.start(MissionSpec("m", "review an old result"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "critic", "review", "previously queued", idea_id="idea-a")
        store.set_mission_status("m", "stopped", reason="old runtime stopped early")
    coordinator.resume("m", "finish the queued review")
    assert coordinator.tick(agent_runner=lambda **_: {"summary": "reviewed", "risks": [],
        "minimum_next_check": "none"}, require_model=False)["status"] == "completed"
    assert coordinator.tick(require_model=False)["reason"] == "final_research_review"


def test_idea_is_written_as_unverified_draft(tmp_path):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "log.md").write_text("# log\n", encoding="utf-8")
    page = write_idea(tmp_path, "m3", {"idea_id": "cache-analogy", "title": "Cache invalidation analogy",
                                       "content": "A hypothesis", "relations": [{"type": "possible_analogy"}]})
    text = (tmp_path / page).read_text(encoding="utf-8")
    assert "status: draft" in text
    assert "evidence_status: unverified" in text


def test_hypothesis_layer_is_structured_before_candidate(tmp_path):
    spec = HypothesisSpec.from_payload("m4", {
        "hypothesis_id": "h1", "statement": "A cache snapshot prevents stale plans",
        "mechanism": "Bind each response to its originating snapshot",
        "assumptions": ["request IDs are preserved"], "predictions": ["mismatch is rejected"],
        "experimental_design": {"control": "baseline", "perturbation": "mismatch"},
        "prior_art_status": {"status": "unverified"}, "status": "testable"})
    assert spec.to_payload()["predictions"] == ["mismatch is rejected"]
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.create_mission(MissionSpec("m4", "develop a mechanism"))
        hypothesis = store.create_hypothesis("m4", spec.to_payload())
        assert hypothesis["status"] == "testable"
        with pytest.raises(ValueError, match="hypothesis"):
            store.create_candidate("m4", {"candidate_id": "c4", "hypothesis_id": "missing",
                                           "hypothesis": "h", "protocol_id": "p", "config": {}})


def test_research_output_can_stop_at_hypothesis_layer(tmp_path):
    from .test_orchestrator import project
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        runtime_root = Path(directory)
        coordinator.runtime_root = runtime_root
        coordinator.store_path = runtime_root / "state.sqlite3"
        coordinator.lock_path = runtime_root / "coordinator.lock"
        coordinator.start(MissionSpec("m5", "develop before implementing"))
        assert coordinator.tick(require_model=False)["status"] == "scheduled"

        def fake_coordinator(**_kwargs):
            return {"payload": {"decision": "schedule", "summary": "explore before formalizing",
                                 "expected_information": "alternative mechanisms", "budget_impact": "one researcher", "stop_condition": "after report",
                                 "tasks": [{"role": "researcher", "objective": "explore the open question",
                                            "reason": "the coordinator is delegating Idea Exploration"}]}}

        assert coordinator.tick(agent_runner=fake_coordinator, require_model=False)["status"] == "completed"

        def fake_researcher(**_kwargs):
            return {"payload": {"decision": "continue", "summary": "keep developing",
                "ideas": [{"idea_id": "idea5", "title": "A mechanism", "content": "draft"}],
                "hypotheses": [{"hypothesis_id": "h5", "idea_ref": "idea5", "statement": "A testable claim",
                    "mechanism": "A mechanism", "assumptions": ["assumption"], "predictions": ["prediction"],
                    "experimental_design": {"control": "baseline"}, "prior_art_status": {"status": "unverified"},
                    "status": "testable"}], "candidates": [], "tasks": []}}

        assert coordinator.tick(agent_runner=fake_researcher, require_model=False)["status"] == "completed"
        with RuntimeStore(coordinator.store_path) as store:
            assert store.hypothesis("h5")["status"] == "testable"
            assert store.db.execute("select count(*) from candidates").fetchone()[0] == 0
            assert store.db.execute("select count(*) from agent_tasks where role='hypothesis'").fetchone()[0] == 0
            event = next(item for item in store.events("m5") if item["kind"] == "research_decision")
            assert event["payload"]["suggested_tasks"][0]["role"] == "hypothesis"
        assert coordinator.tick(require_model=False)["role"] == "coordinator"
