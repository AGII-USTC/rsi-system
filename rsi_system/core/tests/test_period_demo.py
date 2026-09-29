import json
import subprocess

import pytest

from rsi_system.core.protocols import PERIOD_PROTOCOL, build_command, read_metrics
from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from .test_orchestrator import project
from .test_protocols import record


def test_period_protocol_fixes_the_comparison_and_rejects_evaluator_changes(tmp_path):
    argv = build_command(record(PERIOD_PROTOCOL, {"period_ticks": 24}), tmp_path, "python")
    assert argv[argv.index("--steps") + 1] == "64"
    assert argv[argv.index("--period-ticks") + 1] == "24"
    for config in ({"period_ticks": True}, {"period_ticks": 7}, {"period_ticks": 12, "steps": 80}):
        with pytest.raises(ValueError, match="period demo"):
            build_command(record(PERIOD_PROTOCOL, config), tmp_path, "python")


def test_period_metrics_check_actual_config_and_waypoint_predicate(tmp_path):
    config = {"period_ticks": 12, "steps": 64, "seed": 0, "mode": "periodic", "monitor": "none",
              "switch_tick": -1, "latency_ticks": 4, "chunk_ticks": 16}
    summary = {"schema_version": 1, "environment_steps": 64, "mode": "periodic", "monitor_mode": "none",
               "waypoint_distance_m": 0.01, "waypoint_reached": True, "requests": 6,
               "accepted_responses": 5, "metaworld_task_success": False, "gain_switch_tick": None}
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    proposal = record(PERIOD_PROTOCOL, {"period_ticks": 12})
    assert read_metrics(proposal, tmp_path)["feasible"]
    summary["waypoint_reached"] = False
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="predicate"):
        read_metrics(proposal, tmp_path)
    config["steps"] = 80
    (tmp_path / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="frozen"):
        read_metrics(proposal, tmp_path)


def test_config_candidate_pins_source_without_coder_permissions(tmp_path):
    root = project(tmp_path)
    (root / "deployment/rsi/contracts.json").write_text(json.dumps({PERIOD_PROTOCOL: {"candidate_mode": "configuration_only"}}))
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        mission = store.create_mission(MissionSpec("demo", "small diagnostic",
            authorization={"allow_code_candidates": False, "allow_config_candidates": True},
            evaluator={"protocol_id": PERIOD_PROTOCOL, "source_commit": commit}))
        raw = {"candidate_id": "base", "kind": "baseline", "baseline_reason": "unchanged reference",
               "hypothesis": "reference", "expected_observation": "measure requests and waypoint distance",
               "protocol_id": PERIOD_PROTOCOL, "config": {"period_ticks": 12}}
        baseline = coordinator._register_candidate(store, mission, raw)
        assert baseline["status"] == "ready" and baseline["code_commit"] == commit
        assert not store.list_tasks("demo")
        with pytest.raises(ValueError, match="period demo"):
            coordinator._register_candidate(store, mission, {**raw, "candidate_id": "bad", "config": {"period_ticks": 1}})
        assert store.candidate("bad") is None
        with pytest.raises(ValueError, match="immutable source_commit"):
            coordinator._register_candidate(store, {**mission, "payload": {**mission["payload"],
                "evaluator": {"protocol_id": PERIOD_PROTOCOL, "source_commit": "HEAD"}}}, {**raw, "candidate_id": "unpinned"})


def test_long_research_objective_preserves_prompt_without_exceeding_search_limit(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    objective = "Compare fixed simulation configurations and keep competing explanations. " * 8
    with RuntimeStore(coordinator.store_path) as store:
        mission = store.create_mission(MissionSpec("long", objective))
        task = store.create_task("long", "coordinator", objective, "start")
        prompt = coordinator._prompt("coordinator", mission, task, store)
        assert json.loads(prompt.split("\n", 1)[1])["context"]["mission"]["objective"] == objective.strip()
