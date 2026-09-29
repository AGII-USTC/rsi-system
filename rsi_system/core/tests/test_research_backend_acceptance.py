"""Independent backend acceptance: injected decisions, real bridge/queue/probe.

This verifies mechanics and provenance, not model research quality.
"""
from pathlib import Path

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project


def request(bridge, tool, call_id, **args):
    result = bridge.call({"tool": tool, "call_id": call_id, "args": args})
    assert result["ok"], result
    return result


def test_joint_question_executes_code_probe_and_continues_without_ui(tmp_path, monkeypatch):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("acceptance", "Investigate two complementary mechanisms",
        resources={"max_agent_tasks": 16, "max_runs": 1, "max_seconds_per_run": 5},
        authorization={"allow_code_candidates": True, "allow_server_simulation": True}))
    with RuntimeStore(runtime.store_path) as store:
        seed = store.create_task("acceptance", "coordinator", "Plan the research", "initial blueprint")
        parent = store.claim_task("acceptance")
    bridge = ToolBridge(runtime.root, parent["task_id"])
    for node in ("early", "recent"):
        request(bridge, "rsi_research", "create-" + node, action="create", data={
            "node_id": node, "question": node + " question", "scope": node + " component",
            "reason": "Independent uncertainty"})
    request(bridge, "rsi_research", "advance-early", action="revise", data={
        "node_id": "early", "expected_revision": 1, "summary": "A newer independent direction",
        "reason": "Continue independent exploration"})
    composition = request(bridge, "rsi_research", "compose", action="compose", data={
        "node_id": "joint", "question": "Does the combined prototype run?", "scope": "joint mechanism",
        "reason": "Test interaction of an older idea with a recent one",
        "inputs": [{"node_id": "early", "revision": 1}, {"node_id": "recent", "revision": 1}]})
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(parent["task_id"], parent["lease_token"], {"summary": "Joint study queued"})
    observed = {"roles": [], "run_id": None, "next_task": None}

    def agent(**kwargs):
        task_id, role = kwargs["task_id"], kwargs["role"]
        observed["roles"].append(role)
        with RuntimeStore(runtime.store_path) as store:
            task = store.task(task_id)
        if role == "coordinator":
            return {"payload": {"decision": "stop", "summary": "Bounded acceptance finished",
                "expected_information": "none", "budget_impact": "no further tasks",
                "stop_condition": "After already queued work completes"}, "tool_protocol_version": 1}
        if role == "coder":
            assert task["scope"]["research_node_id"] == "joint"
            workspace = Path(kwargs["output_dir"]) / "workspace"
            (workspace / "probe.py").write_text("print('joint-probe-observed')\n")
            (workspace / "handoff.md").write_text("A diagnostic prototype, no scientific gain claimed.\n")
            return {"payload": {"summary": "Prototype implemented", "code_change": False},
                    "tool_protocol_version": 1}
        if task["scope"].get("research_node_id") == "next-question":
            return {"payload": {"summary": "Follow-up question investigated; more evidence needed"},
                    "tool_protocol_version": 1}
        assert role == "researcher" and task_id == composition["task_id"]
        own = ToolBridge(runtime.root, task_id)
        child = request(own, "rsi_delegate", "code", role="coder", objective="Implement joint probe")
        code = request(own, "rsi_wait", "await-code", task_id=child["task_id"])
        script = next(item for item in code["handoff_files"] if item["workspace_path"] == "probe.py")
        probe = request(own, "rsi_run_probe", "probe", objective="Measure joint prototype", experiment={
            "source_task_id": child["task_id"], "script_file_id": script["file_id"],
            "question": "Does this prototype execute?", "baseline": "No prototype",
            "expected_difference": "Diagnostic output", "falsifier": "No output",
            "source_reason": "Use the completed coder handoff"})
        result = request(own, "rsi_wait", "await-probe", task_id=probe["task_id"])
        assert result["run"]["status"] == "completed"
        assert "joint-probe-observed" in result["run"]["logs"]["stdout.log"]
        observed["run_id"] = result["run"]["run_id"]
        revision = request(own, "rsi_research", "record-evidence", action="revise", data={
            "node_id": "joint", "expected_revision": 1, "execution_status": "completed",
            "finding_status": "inconclusive", "run_refs": [observed["run_id"]],
            "summary": "Prototype executed; mechanism remains unproven", "reason": "Record actual probe"})
        request(own, "rsi_research", "next-question", action="create", data={
            "node_id": "next-question", "question": "What control would distinguish the mechanism?",
            "scope": "follow-up design", "parent": {"node_id": "joint", "revision": revision["node"]["revision"]},
            "reason": "Execution alone does not answer the scientific question"})
        followup = request(own, "rsi_delegate", "continue-research", role="researcher",
            objective="Investigate the next control", research_node_id="next-question", research_revision=1)
        observed["next_task"] = followup["task_id"]
        return {"payload": {"summary": "Probe completed; next research queued"}, "tool_protocol_version": 1}

    monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", agent)
    for _ in range(8):
        result = runtime.tick(agent_runner=agent, require_model=False, mission_id="acceptance")
        assert result["status"] not in {"task_failed", "paused", "writeback_failed"}, result
        with RuntimeStore(runtime.store_path) as store:
            if store.mission("acceptance")["status"] == "stopped":
                break
    with RuntimeStore(runtime.store_path) as store:
        assert observed["run_id"] is not None and observed["next_task"] is not None
        assert store.task(observed["next_task"])["status"] == "completed"
        assert store.research_node("acceptance", "joint")["finding_status"] == "inconclusive"
        assert store.research_node("acceptance", "early")["revision"] == 2
        assert store.research_node("acceptance", "early", 1)["question"] == "early question"
        assert len(store.list_runs("acceptance")) == 1
        for task in store.list_tasks("acceptance"):
            if task["role"] in {"coder", "runner"}:
                assert task["scope"]["research_node_id"] == "joint"
        assert store.mission("acceptance")["status"] == "stopped"
        assert not store.queued_or_running("acceptance")


def test_legacy_coordinator_dispatch_preserves_exact_binding(tmp_path):
    from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
    from .test_orchestrator import project
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec("legacy-bound", "inspect"))
    with RuntimeStore(runtime.store_path) as store:
        parent = store.create_task("legacy-bound", "coordinator", "plan", "inspect",
            research_node_id="legacy-bound-root", research_revision=1)
        parent = store.claim_task("legacy-bound")
        store.apply_research_operation("legacy-bound", "revise", {
            "node_id": "legacy-bound-root", "expected_revision": 1,
            "summary": "newer blueprint", "reason": "changed"}, actor_task_id=parent["task_id"])
        store.apply_coordinator_decision(parent, {"decision": "schedule", "summary": "dispatch"}, [{
            "role": "researcher", "objective": "study", "reason": "legacy payload"}])
        child = store.claim_task("legacy-bound")
        assert child["scope"]["research_node_id"] == "legacy-bound-root"
        assert child["scope"]["research_revision"] == 1
        bindings = store.research_snapshot("legacy-bound")["task_bindings"]
        assert any(item["task_id"] == child["task_id"] and item["revision"] == 1 for item in bindings)


def test_live_candidate_registration_keeps_research_provenance(tmp_path):
    from .test_runtime_research_tools import setup_coordinator
    from .test_runtime_acceptance import candidate
    from rsi_system.core.runtime import RuntimeStore
    runtime, parent, bridge = setup_coordinator(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        store.db.execute("INSERT INTO research_task_bindings VALUES(?,?,?,?)", (
            parent["task_id"], parent["mission_id"], "research-mission-root", 1))
        store.db.commit()
    result = bridge.call({"tool": "rsi_register_candidate", "call_id": "bound-candidate", "args": candidate()})
    assert result["ok"], result
    with RuntimeStore(runtime.store_path) as store:
        links = [event["payload"] for event in store.events(parent["mission_id"])
                 if event["kind"] == "research_candidate_link"]
        assert len(links) == 1
        assert links[0]["candidate_id"] == result["candidate_id"]
        assert links[0]["node_id"] == "research-mission-root" and links[0]["revision"] == 1


def test_composition_accepts_bootstrap_root_and_rejects_malformed_input(tmp_path):
    from .test_runtime_research_tools import setup_coordinator, call
    runtime, parent, bridge = setup_coordinator(tmp_path)
    node = call(bridge, "create", {"node_id": "independent", "question": "Another angle",
        "scope": "mechanism", "reason": "independent branch"}, "other")["node"]
    data = {"node_id": "root-joint", "question": "Revise overarching approach",
        "scope": "system", "reason": "system and component interaction",
        "inputs": [{"node_id": "research-mission-root", "revision": 1},
                   {"node_id": node["node_id"], "revision": 1}]}
    malformed = call(bridge, "compose", {**data, "inputs": ["bad", None]}, "malformed")
    assert not malformed["ok"]
    assert call(bridge, "compose", data, "with-root")["ok"]


def test_joint_evidence_does_not_expand_via_shared_idea(tmp_path):
    import json
    from .test_runtime_research_tools import setup_coordinator, call
    from rsi_system.core.runtime_evidence import build_evidence_bundle
    runtime, parent, bridge = setup_coordinator(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        source_task = store.create_task(parent["mission_id"], "researcher", "Source exploration",
                                        "Create the initial shared Idea evidence",
                                        parent_task_id=parent["task_id"], idea_id="joint-shared")
        source_task = store.claim_task(parent["mission_id"])
        source = store.create_run(parent["mission_id"], None, source_task["task_id"], "exploration",
                                  "completed", None, {}, {"observation": 1}, idea_ref="joint-shared")
        store.finish_task(source_task["task_id"], source_task["lease_token"],
                          {"payload": {"summary": "Initial shared Idea evidence"}})
    for name in ("a", "b"):
        result = call(bridge, "create", {"node_id": name, "question": name, "scope": name,
            "reason": "split", "run_refs": [source["run_id"]] if name == "a" else []}, name)
        assert result["ok"], result
    composed = call(bridge, "compose", {"node_id": "joint-shared", "question": "combine",
        "scope": "interaction", "reason": "fixed evidence", "inputs": [
            {"node_id": "a", "revision": 1}, {"node_id": "b", "revision": 1}]}, "joint")
    with RuntimeStore(runtime.store_path) as store:
        joint = store.task(composed["task_id"])
        late_task = store.create_task(parent["mission_id"], "researcher", "Late exploration",
                                      "Create later shared Idea evidence",
                                      parent_task_id=parent["task_id"], idea_id="joint-shared")
        while late_task["task_id"] != (claimed := store.claim_task(parent["mission_id"]))["task_id"]:
            store.finish_task(claimed["task_id"], claimed["lease_token"],
                              {"payload": {"summary": "Composition task remains bounded"}})
        late_task = claimed
        late = store.create_run(parent["mission_id"], None, late_task["task_id"], "exploration",
                                "completed", None, {}, {"observation": 2}, idea_ref="joint-shared")
        store.finish_task(late_task["task_id"], late_task["lease_token"],
                          {"payload": {"summary": "Later shared Idea evidence"}})
        manifest = json.loads(build_evidence_bundle(runtime.root, store, joint, tmp_path / "bundle").read_text())
        assert {item["run_id"] for item in manifest["files"]} == {source["run_id"]}
        context = json.loads(runtime._prompt("researcher", store.mission(parent["mission_id"]), joint, store).split("\n", 1)[1])
        assert {run["run_id"] for run in context["context"]["runs"]} == {source["run_id"]}
        assert late["run_id"] not in {run["run_id"] for run in context["context"]["idea_runs"]}
