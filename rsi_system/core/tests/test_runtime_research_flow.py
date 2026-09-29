"""Injected model choices exercise the production research task lifecycle."""
from pathlib import Path

from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge
from .test_orchestrator import project


def _research(bridge, action, data, call_id):
    result = bridge.call({"tool": "rsi_research", "call_id": call_id,
                          "args": {"action": action, "data": data}})
    assert result["ok"], result
    return result


def _delegate(bridge, role, objective, node_id, idea_id, call_id):
    result = bridge.call({"tool": "rsi_delegate", "call_id": call_id,
        "args": {"role": role, "objective": objective, "research_node_id": node_id,
                 "research_revision": 1, "idea_id": idea_id}})
    assert result["ok"], result
    return result


def test_unattended_research_composes_and_continues_after_restart(tmp_path):
    root = project(tmp_path)
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("mission-flow", "Improve controller behavior",
        resources={"max_agent_tasks": 12, "max_parallel_agents": 2},
        authorization={"allow_code_candidates": True}))
    coordinator_calls = 0

    def agent(**kwargs):
        nonlocal coordinator_calls
        bridge = ToolBridge(root, kwargs["task_id"])
        workspace = Path(kwargs["output_dir"]) / "workspace"
        if kwargs["role"] == "coordinator":
            coordinator_calls += 1
            if coordinator_calls == 1:
                with RuntimeStore(runtime.store_path) as store:
                    root_node = store.research_node("mission-flow", "mission-flow-root")
                for name in ("timing", "gating"):
                    _research(bridge, "create", {"node_id": name, "question": f"Investigate {name}",
                        "scope": name, "parent": {"node_id": root_node["node_id"], "revision": 1},
                        "reason": "independent question"}, f"create-{name}")
                    _delegate(bridge, "researcher", f"Investigate {name}", name,
                              f"idea-{name}", f"delegate-{name}")
                decision = "schedule"
            else:
                with RuntimeStore(runtime.store_path) as store:
                    graph = store.research_snapshot("mission-flow")
                    current_tasks = store.list_tasks("mission-flow")
                    sources_ready = all(any(t["status"] == "completed" and
                        t["scope"]["research_node_id"] == name for t in current_tasks)
                        for name in ("timing", "gating"))
                if sources_ready and not graph["compositions"]:
                    _research(bridge, "compose", {"node_id": "joint-control",
                        "question": "Can timing and gating improve the controller together?",
                        "scope": "joint timing and gating", "reason": "test their interaction",
                        "inputs": [{"node_id": "timing", "revision": 1},
                                   {"node_id": "gating", "revision": 1}]}, "compose")
                    decision = "schedule"
                elif any(t["status"] in {"queued", "running"} and t["task_id"] != kwargs["task_id"]
                         for t in current_tasks):
                    decision = "wait"
                else:
                    decision = "stop"
            return {"payload": {"decision": decision, "summary": "reviewed the research graph",
                "tasks": [], "expected_information": "branch findings", "budget_impact": "bounded",
                "stop_condition": "joint investigation complete"}, "tool_protocol_version": 1}
        with RuntimeStore(runtime.store_path) as store:
            task = store.task(kwargs["task_id"])
        node_id = task["scope"]["research_node_id"]
        if kwargs["role"] == "researcher" and node_id in {"timing", "gating"}:
            _research(bridge, "revise", {"node_id": node_id, "expected_revision": 1,
                "reason": "preliminary inspection", "summary": f"{node_id} may matter",
                "finding_status": "inconclusive"}, f"revise-{node_id}")
            (workspace / "handoff.md").write_text(f"{node_id} source finding; not measured support.\n")
            return {"payload": {"summary": f"{node_id} needs a joint check", "decision": "continue"},
                    "tool_protocol_version": 1}
        if kwargs["role"] == "researcher" and node_id == "joint-control":
            source = bridge.call({"tool": "rsi_research", "call_id": "joint-snapshot",
                                  "args": {"action": "snapshot", "at_version": None}})
            assert source["ok"] and source["research"]["compositions"]
            _delegate(bridge, "coder", "Prototype the joint controller", "joint-control",
                      "joint-control", "joint-prototype")
            (workspace / "handoff.md").write_text("Joint question registered; code and experiment remain open.\n")
            return {"payload": {"summary": "requested a joint prototype", "decision": "continue"},
                    "tool_protocol_version": 1}
        if kwargs["role"] == "coder":
            (workspace / "handoff.md").write_text("Prototype sketch only; no measured benefit.\n")
            return {"payload": {"summary": "prototype sketch", "code_change": False},
                    "tool_protocol_version": 1}
        raise AssertionError(kwargs["role"])

    assert runtime.tick(agent_runner=agent, require_model=False)["status"] == "scheduled"
    for _ in range(12):
        result = RuntimeCoordinator(root).tick(agent_runner=agent, require_model=False)
        if result["status"] in {"stopped", "paused"}:
            break
    with RuntimeStore(runtime.store_path) as store:
        graph = store.research_snapshot("mission-flow")
        assert len(graph["compositions"]) == 1
        assert graph["compositions"][0]["node_id"] == "joint-control"
        assert {node["node_id"] for node in graph["nodes"]} >= {
            "mission-flow-root", "timing", "gating", "joint-control"}
        assert any(t["role"] == "coder" and t["scope"]["research_node_id"] == "joint-control"
                   and t["status"] == "completed" for t in store.list_tasks("mission-flow"))
