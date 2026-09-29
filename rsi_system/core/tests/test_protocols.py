import json

import pytest

from rsi_system.core import protocols
from rsi_system.core.protocols import build_command, read_metrics
from rsi_system.core.provenance import snapshot_inputs
from rsi_system.core.schema import ExperimentSpec


def record(protocol="runtime-harness-smoke-v1", config=None, command=()):
    spec = ExperimentSpec("run-1", "integration check", protocol, config or {}, command=command,
                          resource_budget={"max_seconds": 20})
    return {**spec.__dict__} if hasattr(spec, "__dict__") else {
        "experiment_id": spec.experiment_id,
        "protocol_id": spec.protocol_id, "config": spec.config, "execution_target": spec.execution_target,
        "command": spec.command, "resource_budget": spec.resource_budget}


@pytest.mark.parametrize("overrides,match", [
    ({"protocol_id": "shell-v1"}, "unregistered"),
    ({"command": ("sh", "-c", "touch /tmp/unsafe")}, "empty proposal command"),
    ({"config": {"monitor": "oracle"}}, "unknown runtime"),
    ({"resource_budget": {"max_seconds": 0}}, "max_seconds"),
])
def test_rejects_unregistered_or_unbounded_proposals(tmp_path, overrides, match):
    proposal = record()
    proposal.update(overrides)
    with pytest.raises(ValueError, match=match):
        build_command(proposal, tmp_path / "out", "/usr/bin/python3")


def test_harness_command_disables_monitor_and_gain_change(tmp_path):
    argv = build_command(record(config={"seed": 3, "steps": 12}), tmp_path / "out", "/usr/bin/python3")
    assert argv[argv.index("--monitor") + 1] == "none"
    assert argv[argv.index("--switch-tick") + 1] == "-1"


def test_residual_requires_development_manifest(tmp_path):
    data = tmp_path / "test"
    data.mkdir()
    (data / "manifest.json").write_text('{"schema_version":1,"split":"test","kind":"residual_memory_public_trajectories_v1"}')
    model = tmp_path / "nominal_model.json"
    model.write_text("{}")
    (tmp_path / "fit.json").write_text("{}")
    with pytest.raises(ValueError, match="development manifest"):
        build_command(record("residual-memory-dev-v1", {"data_dir": str(data), "model_path": str(model)}),
                      tmp_path / "out", "/usr/bin/python3")


def test_residual_command_accepts_only_explicit_dev_inputs(tmp_path):
    data = tmp_path / "dev"
    data.mkdir()
    (data / "manifest.json").write_text('{"schema_version":1,"split":"dev","kind":"residual_memory_public_trajectories_v1"}')
    model = tmp_path / "nominal_model.json"
    model.write_text("{}")
    (tmp_path / "fit.json").write_text("{}")
    argv = build_command(record("residual-memory-dev-v1", {"data_dir": str(data), "model_path": str(model)}),
                         tmp_path / "out", "/usr/bin/python3")
    assert argv[argv.index("--split") + 1] == "dev"
    assert "fit_path" not in argv
    copied, hashes = snapshot_inputs({"data_dir": data, "model_path": model,
                                      "fit_path": tmp_path / "fit.json"}, tmp_path / "frozen")
    assert set(copied) == {"data_dir", "model_path"}
    assert set(hashes) == {"dev_data_sha256", "model_sha256", "fit_sha256"}


def test_pi05_fixed_command_and_scale_snapshot(tmp_path, monkeypatch):
    scale = tmp_path / "response_scale_dev.json"
    scale.write_text('{"schema_version":1,"source_initial_state_index":0,"nominal_scale_m_per_action":0.01}')
    monkeypatch.setattr(protocols, "PI05_SCALE", scale)
    monkeypatch.setattr(protocols, "PI05_SCALE_SHA256", protocols.sha256_file(scale))
    proposal = record(protocols.PI05_PROTOCOL, {"condition": "continuous_memory"})
    argv = build_command(proposal, tmp_path / "output", "/usr/bin/python3")
    assert argv[argv.index("--condition") + 1] == "continuous_memory"
    copied, hashes = snapshot_inputs({"scale_file": scale}, tmp_path / "inputs")
    frozen = build_command(proposal, tmp_path / "output", "/usr/bin/python3", input_overrides=copied)
    assert frozen[frozen.index("--scale-file") + 1] == str(copied["scale_file"])
    assert len(hashes["scale_file_sha256"]) == 64
    with pytest.raises(ValueError, match="registered condition"):
        build_command(record(protocols.PI05_PROTOCOL, {"condition": "oracle"}), tmp_path, "/usr/bin/python3")
    with pytest.raises(ValueError, match="registered condition"):
        build_command(record(protocols.PI05_PROTOCOL, {"condition": "no_memory", "gain": 0.7}), tmp_path, "/usr/bin/python3")


def test_pi05_result_requires_fixed_schedule_and_all_tasks(tmp_path):
    proposal = record(protocols.PI05_PROTOCOL, {"condition": "frozen_memory"})
    scale_hash = protocols.PI05_SCALE_SHA256
    report = {"suite": "libero_spatial", "seed": 7, "initial_state_index": 5,
              "condition": "frozen_memory", "xyz_gain": 1.0, "correction_power": 0.5,
              "selected_task_id": None, "trials_per_task": 1, "replan_steps": 1,
              "warmup_steps": 10, "scale_file_sha256": scale_hash,
              "gain_schedule": {"gain_before": 1.0, "gain_after": 0.6,
                                "switch_controlled_step": 80,
                                "indexing": "zero-based controlled actions; warmup excluded"},
              "successes": 10,
              "tasks": [{"task_id": i, "success": True, "error": None,
                         "controlled_steps": 100} for i in range(10)]}
    (tmp_path / "summary.json").write_text(json.dumps(report))
    assert read_metrics(proposal, tmp_path)["successes"] == 10
    report["gain_schedule"]["switch_controlled_step"] = 79
    (tmp_path / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="gain schedule"):
        read_metrics(proposal, tmp_path)
