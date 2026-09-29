"""Research tool acceptance through the production durable bridge."""
from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project


def call(bridge, action, data, call_id):
    return bridge.call({"tool": "rsi_research", "call_id": call_id,
                        "args": {"action": action, "data": data}})


def setup_coordinator(tmp_path, *, max_agent_tasks=20):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("research-mission", "Find a better control strategy",
                              resources={"max_agent_tasks": max_agent_tasks}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("research-mission", "coordinator", "Plan research", "initial blueprint")
        parent = store.claim_task("research-mission")
    return runtime, parent, ToolBridge(root, parent["task_id"])


def test_research_reason_location_is_repaired_without_losing_audit_reason(tmp_path):
    runtime, _, bridge = setup_coordinator(tmp_path)
    node = {"node_id": "reason-location", "question": "Which mechanism matters?", "scope": "local"}
    missing = bridge.call({"tool": "rsi_research", "call_id": "missing-reason",
                           "args": {"action": "create", "data": node}})
    assert not missing["ok"]
    assert missing["error"]["message"] == "data.reason must be a nonempty string"
    assert "data.reason" in missing["error"]["repair"]

    legacy = {"action": "create", "data": node, "reason": "split the mechanism question"}
    created = bridge.call({"tool": "rsi_research", "call_id": "legacy-reason", "args": legacy})
    assert created["ok"]
    assert created["normalizations"] == ["moved top-level reason to data.reason"]
    canonical = {"action": "create", "data": {**node, "reason": legacy["reason"]}}
    assert bridge.call({"tool": "rsi_research", "call_id": "legacy-reason", "args": canonical}) == created

    conflict = bridge.call({"tool": "rsi_research", "call_id": "conflicting-reasons",
                            "args": {"action": "create", "data": {
                                **node, "node_id": "conflict", "reason": "first rationale"},
                                "reason": "different rationale"}})
    assert not conflict["ok"]
    assert "reason conflicts with data.reason" in conflict["error"]["message"]
    assert "Remove top-level reason" in conflict["error"]["repair"]
    with RuntimeStore(runtime.store_path) as store:
        assert store.research_node("research-mission", "conflict") is None
        assert store.research_node("research-mission", "reason-location") is not None
        events = [event for event in store.events("research-mission")
                  if event["kind"] == "research_create"
                  and event["payload"]["node"]["node_id"] == "reason-location"]
        assert len(events) == 1
        assert events[0]["payload"]["reason"] == legacy["reason"]


def test_long_mission_id_gets_a_safe_root_node(tmp_path):
    mission_id = "m" * 96
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec(mission_id, "Long identifier Mission"))
    with RuntimeStore(runtime.store_path) as store:
        nodes = store.research_snapshot(mission_id)["nodes"]
        assert len(nodes) == 1
        assert len(nodes[0]["node_id"]) <= 96
        assert nodes[0]["question"] == "Long identifier Mission"
    assert runtime.tick(require_model=False)["status"] == "scheduled"


def test_create_without_parent_uses_mission_root_and_its_current_revision(tmp_path):
    runtime, coordinator, bridge = setup_coordinator(tmp_path)
    first = call(bridge, "create", {"node_id": "first", "question": "First question",
        "scope": "first", "reason": "split"}, "first")
    assert first["ok"]
    updated = call(bridge, "revise", {"node_id": "research-mission-root",
        "expected_revision": 1, "summary": "Sharper blueprint", "reason": "refine"}, "root-revise")
    assert updated["ok"] and updated["node"]["revision"] == 2
    second = call(bridge, "create", {"node_id": "second", "question": "Second question",
        "scope": "second", "reason": "another split"}, "second")
    assert second["ok"]
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(coordinator["task_id"], coordinator["lease_token"],
            {"payload": {"summary": "Blueprint complete"}})
        bound = store.create_task("research-mission", "coordinator", "Continue first", "assigned",
            research_node_id="first", research_revision=1)
        bound = store.claim_task("research-mission")
    third = call(ToolBridge(runtime.root, bound["task_id"]), "create", {
        "node_id": "third", "question": "Third question", "scope": "nested",
        "reason": "deeper split"}, "third")
    assert third["ok"]
    with RuntimeStore(runtime.store_path) as store:
        graph = store.research_snapshot("research-mission")
        edges = {edge["target"]["node_id"]: edge for edge in graph["relations"]
                 if edge["kind"] == "decomposes"}
        assert edges["first"]["source"] == {"node_id": "research-mission-root", "revision": 1}
        assert edges["second"]["source"] == {"node_id": "research-mission-root", "revision": 2}
        assert edges["third"]["source"] == {"node_id": "first", "revision": 1}
        historical = store.research_snapshot("research-mission", at_version=first["node"]["event_version"])
        assert [edge["target"]["node_id"] for edge in historical["relations"]] == ["first"]


def test_bound_actor_defaults_to_pinned_revision_and_cannot_create_a_root(tmp_path):
    runtime, coordinator, bridge = setup_coordinator(tmp_path)
    branch = call(bridge, "create", {"node_id": "branch", "question": "Branch question",
        "scope": "branch", "reason": "split"}, "branch")
    assert branch["ok"]
    with RuntimeStore(runtime.store_path) as store:
        worker = store.create_task("research-mission", "researcher", "Study branch", "assigned",
            parent_task_id=coordinator["task_id"], research_node_id="branch", research_revision=1)
        worker = store.claim_task("research-mission")
    revised = call(bridge, "revise", {"node_id": "branch", "expected_revision": 1,
        "summary": "New framing", "reason": "refine"}, "branch-revise")
    assert revised["ok"] and revised["node"]["revision"] == 2
    own = ToolBridge(runtime.root, worker["task_id"])
    created = call(own, "create", {"node_id": "branch-child", "question": "Follow-up",
        "scope": "local", "reason": "subquestion"}, "branch-child")
    assert created["ok"]
    forbidden = call(own, "create", {"node_id": "forbidden", "question": "Other root",
        "scope": "outside", "reason": "separate", "separate_root": True}, "forbidden-root")
    assert not forbidden["ok"] and "coordinator" in forbidden["error"]["message"]
    cross = call(own, "create", {"node_id": "cross", "question": "Cross branch",
        "scope": "outside", "reason": "cross", "parent": {
            "node_id": "research-mission-root", "revision": 1}}, "cross-branch")
    assert not cross["ok"] and "assigned branch" in cross["error"]["message"]
    with RuntimeStore(runtime.store_path) as store:
        edges = [edge for edge in store.research_snapshot("research-mission")["relations"]
                 if edge["kind"] == "decomposes" and edge["target"]["node_id"] == "branch-child"]
        assert len(edges) == 1 and edges[0]["source"] == {"node_id": "branch", "revision": 1}
        assert store.research_node("research-mission", "forbidden") is None
        assert store.research_node("research-mission", "cross") is None


def test_historical_composition_is_atomic_idempotent_and_keeps_sources(tmp_path):
    runtime, parent, bridge = setup_coordinator(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        root = store.research_node("research-mission", "research-mission-root")
    b = call(bridge, "create", {"node_id": "branch-b", "question": "Delay alignment",
        "scope": "timing", "parent": {"node_id": root["node_id"], "revision": 1},
        "reason": "independent timing question"}, "b")["node"]
    c = call(bridge, "create", {"node_id": "branch-c", "question": "Gate decisions",
        "scope": "gating", "parent": {"node_id": root["node_id"], "revision": 1},
        "reason": "independent gating question"}, "c")["node"]
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("research-mission", "researcher", "Study B", "source",
                          parent_task_id=parent["task_id"], idea_id="idea-b",
                          research_node_id=b["node_id"], research_revision=1)
        source = store.claim_task("research-mission")
        store.finish_task(source["task_id"], source["lease_token"], {"payload": {"summary": "early alignment finding"}})
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("research-mission", "researcher", "Study C", "source",
                          parent_task_id=parent["task_id"], idea_id="idea-c",
                          research_node_id=c["node_id"], research_revision=1)
        c_author = store.claim_task("research-mission")
    c2 = call(ToolBridge(runtime.root, c_author["task_id"]), "revise", {
        "node_id": c["node_id"], "expected_revision": 1,
        "reason": "new evidence", "summary": "gating changed"}, "revise-c")["node"]
    with RuntimeStore(runtime.store_path) as store:
        store.finish_task(c_author["task_id"], c_author["lease_token"],
                          {"payload": {"summary": "revised gating finding"}})
    b2 = call(bridge, "revise", {"node_id": b["node_id"], "expected_revision": 1,
        "reason": "independent continuation", "summary": "later alignment branch"}, "revise-b")["node"]
    data = {"node_id": "joint-d", "question": "Can early alignment and revised gating work together?",
            "scope": "joint investigation", "reason": "test a specific interaction",
            "inputs": [{"node_id": b["node_id"], "revision": 1},
                       {"node_id": c2["node_id"], "revision": 2}]}
    request = {"tool": "rsi_research", "call_id": "compose-d",
               "args": {"action": "compose", "data": data}}
    composed = bridge.call(request)
    assert composed["ok"] and composed["task_status"] == "queued"
    assert bridge.call(request) == composed
    assert ToolBridge(runtime.root, parent["task_id"]).call(request) == composed
    with RuntimeStore(runtime.store_path) as store:
        task = store.task(composed["task_id"])
        assert task["scope"]["research_node_id"] == "joint-d"
        assert task["scope"]["research_revision"] == 1
        graph = store.research_snapshot("research-mission")
        assert store.research_node("research-mission", "branch-b")["revision"] == b2["revision"]
        assert graph["compositions"][0]["inputs"] == [
            {"node_id": "branch-b", "revision": 1, "selection": "whole revision"},
            {"node_id": "branch-c", "revision": 2, "selection": "whole revision"}]
        assert set(graph["compositions"][0]["plan"]["frozen_source_task_ids"]) == {
            source["task_id"], c_author["task_id"]}
        assert len([item for item in store.list_tasks("research-mission")
                    if item["scope"]["research_node_id"] == "joint-d"]) == 1
        assert store.research_snapshot("research-mission", at_version=b2["event_version"])["compositions"] == []
        late = store.create_task("research-mission", "researcher", "Late work on B", "later evidence",
                                 parent_task_id=parent["task_id"], idea_id="idea-b",
                                 research_node_id="branch-b", research_revision=1)
        joint = store.claim_task("research-mission")
        assert joint["task_id"] == composed["task_id"]
        late_claimed = store.claim_task("research-mission")
        assert late_claimed["task_id"] == late["task_id"]
        store.finish_task(late["task_id"], late_claimed["lease_token"],
                          {"payload": {"summary": "later source result"}})
    joint_bridge = ToolBridge(runtime.root, composed["task_id"])
    assert joint_bridge.call({"tool": "rsi_status", "call_id": "frozen-source",
                              "args": {"task_id": source["task_id"]}})["ok"]
    assert not joint_bridge.call({"tool": "rsi_status", "call_id": "late-source",
                                  "args": {"task_id": late["task_id"]}})["ok"]


def test_composition_rolls_back_when_followup_budget_is_exhausted(tmp_path):
    runtime, parent, bridge = setup_coordinator(tmp_path, max_agent_tasks=1)
    with RuntimeStore(runtime.store_path) as store:
        root = store.research_node("research-mission", "research-mission-root")
    for name in ("b", "c"):
        assert call(bridge, "create", {"node_id": name, "question": name, "scope": name,
            "parent": {"node_id": root["node_id"], "revision": 1}, "reason": "split"}, name)["ok"]
    failed = call(bridge, "compose", {"node_id": "joint", "question": "Joint", "scope": "test",
        "reason": "combine", "inputs": [{"node_id": "b", "revision": 1},
                                          {"node_id": "c", "revision": 1}]}, "compose")
    assert not failed["ok"] and "budget" in failed["error"]["message"]
    with RuntimeStore(runtime.store_path) as store:
        assert store.research_node("research-mission", "joint") is None
        assert store.research_snapshot("research-mission")["compositions"] == []


def test_domain_owner_can_continue_its_local_composition(tmp_path):
    runtime, coordinator, bridge = setup_coordinator(tmp_path)
    domain = call(bridge, "create", {"node_id": "domain", "question": "Local domain",
        "scope": "local", "parent": {"node_id": "research-mission-root", "revision": 1},
        "reason": "domain work"}, "domain")["node"]
    for name in ("local-b", "local-c"):
        call(bridge, "create", {"node_id": name, "question": name, "scope": name,
            "parent": {"node_id": domain["node_id"], "revision": 1},
            "reason": "independent local work"}, name)
    with RuntimeStore(runtime.store_path) as store:
        owner = store.create_task("research-mission", "researcher", "Own domain", "assigned",
            parent_task_id=coordinator["task_id"], idea_id="domain-idea",
            research_node_id="domain", research_revision=1)
        owner = store.claim_task("research-mission")
    owner_bridge = ToolBridge(runtime.root, owner["task_id"])
    joint = call(owner_bridge, "compose", {"node_id": "local-joint", "question": "Join local work",
        "scope": "local joint", "reason": "evaluate interaction",
        "inputs": [{"node_id": "local-b", "revision": 1},
                   {"node_id": "local-c", "revision": 1}]}, "local-compose")
    assert joint["ok"]
    with RuntimeStore(runtime.store_path) as store:
        task = store.claim_task("research-mission")
        assert task["task_id"] == joint["task_id"]
        assert task["scope"]["idea_id"] == "domain-idea"
    continued = call(ToolBridge(runtime.root, joint["task_id"]), "revise", {
        "node_id": "local-joint", "expected_revision": 1, "reason": "joint investigation started",
        "execution_status": "active"}, "local-revise")
    assert continued["node"]["revision"] == 2
    delegated = ToolBridge(runtime.root, joint["task_id"]).call({"tool": "rsi_delegate",
        "call_id": "pinned-child", "args": {"role": "researcher", "objective": "Continue pinned work"}})
    assert delegated["ok"] and delegated["research_revision"] == 1
