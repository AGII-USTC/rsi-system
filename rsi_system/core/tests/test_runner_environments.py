"""Docker Runner profile selection, pinning, isolation, and teardown."""
import hashlib
import json
import subprocess

import pytest

from rsi_system.core import runner_environments as backend


IMAGE = "sha256:" + "a" * 64
POLICY_IMAGE = "sha256:" + "b" * 64


def profile(tmp_path, *, policy=False):
    data = tmp_path / "dataset"
    data.mkdir(exist_ok=True)
    config = tmp_path / "libero-config.json"
    config.write_text('{"suite":"spatial"}')
    result = {"name": "libero", "image": "openpi_libero_eval:latest",
              "python": "/opt/venv/bin/python", "pythonpath": ["/app", "/app/src"],
              "mounts": [{"source": str(data), "target": "/app/data"},
                         {"source": str(config), "target": "/libero-config/config.json"}],
              "env": {"MUJOCO_GL": "egl"}, "gpu": True}
    if policy:
        result["policy"] = {"image": "openpi_policy:latest", "python": "/opt/venv/bin/python",
                            "command": ["/opt/venv/bin/python", "/app/serve.py"],
                            "port": 8000, "env": {"MODEL_DIR": "/app/checkpoint"}, "gpu": True}
    return result


def fake_docker(monkeypatch, *, policy=False):
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            image = POLICY_IMAGE if args[-1] == "openpi_policy:latest" else IMAGE
            return subprocess.CompletedProcess(args, 0, image + "\n", "")
        if args[:1] == ("inspect",):
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        return subprocess.CompletedProcess(args, 0, "ready\n", "")

    monkeypatch.setattr(backend, "_docker", call)
    if policy:
        monkeypatch.setattr(backend, "_policy_logs", lambda *args: calls.append(("logs", *args)))
    return calls


def test_selection_requires_mission_allowlist_and_admin_profile(tmp_path):
    selected = profile(tmp_path)
    selected.pop("name")
    config = {"runner_environments": {"libero": selected}}
    mission = {"payload": {"resources": {"allowed_runner_environments": ["libero"],
                                          "runner_environment": "libero"}}}
    assert backend.select_environment(config, mission, {})["name"] == "libero"
    assert backend.select_environment(config, {"payload": {"resources": {}}}, {}) is None
    with pytest.raises(ValueError, match="not allowed"):
        backend.select_environment(config, {"payload": {"resources": {}}},
                                   {"environment": "libero"})
    with pytest.raises(ValueError, match="must be an object"):
        backend.select_environment(config, {"resources": {"allowed_runner_environments": ["unknown"]}},
                                   {"environment": "unknown"})


def test_prepare_pins_images_and_small_file_without_hashing_directory(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch)
    pinned = backend.prepare_environment(profile(tmp_path))
    assert pinned["image_id"] == IMAGE
    assert pinned["mounts"][0]["content"] == "live_directory"
    assert "sha256" not in pinned["mounts"][0]
    config = tmp_path / "libero-config.json"
    assert pinned["mounts"][1]["sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert json.loads(json.dumps(pinned))["sha256"] == pinned["sha256"]
    assert pinned == backend.prepare_environment(profile(tmp_path))
    assert all(call[:2] == ("image", "inspect") for call in calls)


def test_launch_has_readonly_inputs_only_output_writable_and_filtered_env(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch)
    pinned = backend.prepare_environment(profile(tmp_path))
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)
    argv = ["/host/python", "-I", str(run / "runner_bootstrap.py"), str(run / "probe.py")]
    env = {"RSI_EXPLORATION_OUTPUT": str(run / "output"),
           "RSI_EXPLORATION_SOURCE": str(run / "source"),
           "RSI_EXPLORATION_INPUTS": str(run / "inputs"), "OPENAI_API_KEY": "must-not-pass"}
    with backend.launch_environment(pinned, run, argv, env) as (command, host_env, provenance):
        assert command[-4:] == [pinned["python"], "-s", str(run / "runner_bootstrap.py"),
                                str(run / "probe.py")]
        assert provenance["image_id"] == IMAGE and provenance["network"] == "none"
        assert "OPENAI_API_KEY" not in host_env
    container = next(call for call in calls if call[0] == "run")
    assert "--read-only" in container and "--cap-drop=ALL" in container
    assert "--security-opt=no-new-privileges" in container and "--pid=host" not in container
    assert "--ipc=private" in container and "--gpus" in container
    assert "--log-driver=json-file" in container and "max-size=8m" in container
    assert container[container.index("--network") + 1] == "none"
    mounts = [container[i + 1] for i, item in enumerate(container[:-1]) if item == "--mount"]
    assert f"type=bind,src={run},dst={run},readonly" in mounts
    assert f"type=bind,src={run / 'output'},dst={run / 'output'}" in mounts
    assert all("docker.sock" not in item for item in mounts)
    assert "OPENAI_API_KEY=must-not-pass" not in container
    assert calls[-1][:2] == ("network", "rm")
    assert json.loads((run / "docker_environment.json").read_text())["status"] == "cleaned"


def test_formal_gpu_run_waits_for_development_and_checks_cancellation(tmp_path, monkeypatch):
    from threading import Thread, Event
    from rsi_system.core.gpu_resources import gpu_slot
    calls = fake_docker(monkeypatch)
    monkeypatch.setattr(backend, 'gpu_containers_active', lambda root: False)
    pinned = backend.prepare_environment(profile(tmp_path))
    run = tmp_path / 'run'
    (run / 'output').mkdir(parents=True)
    entered, checked, done = Event(), Event(), Event()
    errors = []
    def execute():
        try:
            with backend.launch_environment(pinned, run, ['python', 'probe.py'], {},
                    resource_root=tmp_path / 'runtime', check_cancelled=lambda: checked.set()):
                entered.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    with gpu_slot(tmp_path / 'runtime'):
        worker = Thread(target=execute)
        worker.start()
        assert checked.wait(2)
        assert not entered.is_set() and not any(call[0] == 'run' for call in calls)
    assert done.wait(3)
    worker.join()
    assert entered.is_set() and not errors
    container = next(c for c in calls if c[0] == 'run')
    assert backend.gpu_resource_label(tmp_path / 'runtime') in container


def test_policy_internal_network_readiness_and_cleanup_after_body_error(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch, policy=True)
    pinned = backend.prepare_environment(profile(tmp_path, policy=True))
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="probe failed"):
        with backend.launch_environment(pinned, run, ["/host/python", "-I", "probe.py"], {}) as (_, _, provenance):
            assert provenance["policy_image_id"] == POLICY_IMAGE
            raise RuntimeError("probe failed")
    assert any(call[:3] == ("network", "create", "--internal") for call in calls)
    policy_run = next(call for call in calls if call[0] == "run" and "--network-alias" in call)
    assert policy_run[policy_run.index("--network-alias") + 1] == "policy"
    assert "--publish" not in policy_run and "-p" not in policy_run
    assert any(call[:1] == ("exec",) for call in calls)
    assert any(call[:1] == ("logs",) for call in calls)
    assert len([call for call in calls if call[:2] == ("rm", "--force")]) == 2
    assert calls[-1][:2] == ("network", "rm")


def test_rejects_unsafe_mounts_and_changed_pinned_file(tmp_path, monkeypatch):
    fake_docker(monkeypatch)
    unsafe = profile(tmp_path)
    unsafe["mounts"][0]["target"] = "/var/run/docker.sock"
    with pytest.raises(ValueError, match="unsafe"):
        backend.prepare_environment(unsafe)
    original = {"name": "libero", "image": "openpi_libero_eval:latest",
                "mounts": [{"source": str(tmp_path / "libero-config.json"),
                            "target": "/libero-config/config.json"}]}
    pinned = backend.prepare_environment(original)
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)
    (tmp_path / "libero-config.json").write_text('{"suite":"changed"}')
    with pytest.raises(ValueError, match="pinned Docker input file changed"):
        with backend.launch_environment(pinned, run, ["python", "probe.py"], {}):
            pass
    assert pinned["image_id"] == IMAGE


def test_rejects_mutated_pinned_spec_and_reports_cleanup_failure(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch)
    pinned = backend.prepare_environment(profile(tmp_path))
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)
    modified = dict(pinned)
    modified["python"] = "/other/python"
    with pytest.raises(ValueError, match="hash mismatch"):
        with backend.launch_environment(modified, run, ["python", "probe.py"], {}):
            pass
    assert not any(call[0] == "run" for call in calls)

    def failed_cleanup(*args, **kwargs):
        if args[:2] == ("rm", "--force"):
            return subprocess.CompletedProcess(args, 1, "", "permission denied")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend, "_docker", failed_cleanup)
    errors = backend.cleanup_environment(pinned, run)
    assert len(errors) == 2
    assert len(json.loads((run / "docker_cleanup_errors.json").read_text())["errors"]) == 2


def test_policy_readiness_failure_removes_resources(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch, policy=True)
    pinned = backend.prepare_environment(profile(tmp_path, policy=True))
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)

    def failed_ready(*args, **kwargs):
        calls.append(args)
        if args[:1] == ("exec",):
            return subprocess.CompletedProcess(args, 127, "", "python not found")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend, "_docker", failed_ready)
    with pytest.raises(RuntimeError, match="policy readiness check failed"):
        with backend.launch_environment(pinned, run, ["python", "probe.py"], {}):
            pass
    assert len([call for call in calls if call[:2] == ("rm", "--force")]) == 2
    assert calls[-1][:2] == ("network", "rm")
    state = json.loads((run / "docker_environment.json").read_text())
    assert state["status"] == "setup_failed" and state["policy_image_id"] == POLICY_IMAGE


def test_policy_readiness_cancellation_cleans_up_without_total_deadline(tmp_path, monkeypatch):
    calls = fake_docker(monkeypatch, policy=True)
    pinned = backend.prepare_environment(profile(tmp_path, policy=True))
    run = tmp_path / "run"
    (run / "output").mkdir(parents=True)

    def not_ready(*args, **kwargs):
        calls.append(args)
        if args[:1] == ("exec",):
            return subprocess.CompletedProcess(args, 1, "", "ConnectionRefusedError")
        if args[:1] == ("inspect",):
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend, "_docker", not_ready)
    monkeypatch.setattr(backend.time, "sleep", lambda _: None)
    checks = []

    def cancel():
        checks.append(True)
        if len(checks) == 2:
            raise RuntimeError("Runner cancelled")

    with pytest.raises(RuntimeError, match="Runner cancelled"):
        with backend.launch_environment(pinned, run, ["python", "probe.py"], {},
                                        check_cancelled=cancel):
            pass
    assert len(checks) == 2
    assert len([call for call in calls if call[:1] == ("exec",)]) == 1
    assert len([call for call in calls if call[:2] == ("rm", "--force")]) == 2
    assert calls[-1][:2] == ("network", "rm")
    assert json.loads((run / "docker_environment.json").read_text())["status"] == "setup_failed"
