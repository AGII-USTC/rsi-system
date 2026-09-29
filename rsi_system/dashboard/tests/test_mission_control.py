"""Generic Mission actions use temporary state and never invoke a real model."""
from __future__ import annotations

import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from rsi_system.core.config import load_research_config
from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.dashboard.configuration import Configuration
from rsi_system.dashboard.mission_control import MissionControl, RuntimeUnavailable, availability
from rsi_system.dashboard.server import make_handler, snapshot


@pytest.fixture
def root(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment" / "rsi"
    deployment.mkdir(parents=True)
    (deployment / "research.json").write_text(json.dumps({
        "pi_executable": "pi", "python": "python", "models": {},
        "thinking": "high", "agent_timeout_seconds": 120,
    }))
    monkeypatch.setenv("RSI_CONFIG_DIR", str(tmp_path / "private"))
    return tmp_path


def ready(_root):
    return {"available": True, "reasons": []}


def test_create_is_paused_and_never_invokes_other_mission(root):
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("existing", "unrelated live work"))
    control = MissionControl(root, readiness=ready)
    result = control.create({"objective": "Compare team knowledge sharing", "mission_id": "new"})
    assert result == {"mission_id": "new", "status": "paused"}
    with RuntimeStore(coordinator.store_path) as store:
        assert store.mission("existing")["status"] == "active"
        assert store.list_tasks("new") == []
        payload = store.mission("new")["payload"]
        assert payload["resources"]["max_agent_tasks"] == 12
        assert payload["authorization"] == {"allow_code_candidates": False,
                                             "allow_server_simulation": False}
        assert store.events("new")[0]["kind"] == "mission_created"
    assert coordinator.tick(mission_id="new", require_model=False) == {"status": "idle"}
    assert coordinator.tick(mission_id="existing", require_model=False)["mission_id"] == "existing"


def test_explicit_run_scopes_bounded_background_worker(root):
    invoked = []
    class FakeCoordinator(RuntimeCoordinator):
        def tick(self, *, mission_id=None):
            invoked.append(mission_id)
            return {"status": "completed", "mission_id": mission_id}
    control = MissionControl(root, coordinator_factory=FakeCoordinator, readiness=ready)
    control.create({"objective": "Compare alternatives", "mission_id": "requested"})
    RuntimeCoordinator(root).start(MissionSpec("other", "Other active task"))
    with RuntimeStore(root / ".rsi/runtime/state.sqlite3") as store:
        other_before = store.mission("other")
    job = control.action("requested", "run", {"steps": 3})
    assert job["status"] == "running"
    deadline = time.monotonic() + 2
    while control.public()["jobs"][0]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert invoked == ["requested"] * 3
    assert control.public()["jobs"][0]["completed_steps"] == 3
    assert control.action("requested", "pause", {})["status"] == "paused"
    assert control.action("requested", "resume", {})["status"] == "active"
    assert invoked == ["requested"] * 3
    with RuntimeStore(root / ".rsi/runtime/state.sqlite3") as store:
        assert store.mission("other") == other_before


def test_unavailable_run_leaves_mission_paused(root):
    control = MissionControl(root, readiness=lambda _: {"available": False, "reasons": ["Pi missing"]})
    control.create({"objective": "Review sources", "mission_id": "new"})
    with pytest.raises(RuntimeUnavailable, match="Pi missing"):
        control.action("new", "run", {})
    assert snapshot(root, "new")["mission"]["status"] == "paused"


@pytest.mark.parametrize("body", [
    {"objective": "", "mission_id": "m"},
    {"objective": "x", "resources": {"max_agent_tasks": None}},
    {"objective": "x", "authorization": {"allow_everything": True}},
    {"objective": "x", "known_evidence": "not a list"},
])
def test_mission_input_is_validated(root, body):
    with pytest.raises(ValueError):
        MissionControl(root, readiness=ready).create(body)


def test_prompt_has_generic_knowledge_and_real_read_events(root):
    knowledge = root / "knowledge"
    (knowledge / "concepts").mkdir(parents=True)
    (knowledge / "SCHEMA.md").write_text("knowledge schema")
    (knowledge / "concepts" / "sharing.md").write_text("# Sharing\nTeam sharing example")
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "Team sharing"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "Compare methods", "Find evidence")
        task = store.claim_task("m")
        prompt = coordinator._prompt("researcher", store.mission("m"), task, store)
        assert "nominal VLA" not in prompt
        assert "Memory adaptation" not in prompt
        event = next(event for event in store.events("m") if event["kind"] == "knowledge_read")
        assert event["payload"]["files"][0]["path"] == "knowledge/concepts/sharing.md"
    event = next(event for event in snapshot(root)["events"] if event["kind"] == "knowledge_read")
    assert event["knowledge_action"] == "read"
    assert event["from_task_id"] == task["task_id"]
    assert event["knowledge_files"][0]["sha256"]


def test_no_match_does_not_invent_knowledge_read(root):
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "Unrelated topic"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "Review", "Start")
        task = store.claim_task("m")
        coordinator._prompt("researcher", store.mission("m"), task, store)
        assert not any(event["kind"] == "knowledge_read" for event in store.events("m"))


def test_portable_interpreter_defaults(root):
    assert load_research_config(root)["python"] == sys.executable
    assert load_research_config(root)["pi_executable"]


def test_availability_reports_missing_components_without_model_call(root, monkeypatch):
    import rsi_system.dashboard.mission_control as module
    monkeypatch.setattr(module, "_executable", lambda _: False)
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    monkeypatch.setattr(module, "settings_status", lambda _: {
        "default_provider": None, "default_model": None})
    status = availability(root)
    assert not status["available"]
    assert not status["pi_available"] and not status["models_available"]
    assert not status["python_available"] and not status["sandbox_available"]
    assert len(status["reasons"]) == 4


def test_mission_http_post_origin_and_unavailable_result(root):
    control = MissionControl(root, readiness=lambda _: {"available": False, "reasons": ["Pi missing"]})
    config = Configuration(root, config_dir=root / "private", pi_dir=root / "pi")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, config, control=control))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def post(path, body, origin=None):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
            conn.request("POST", path, json.dumps(body), headers={
                "Content-Type": "application/json",
                "Origin": origin or f"http://127.0.0.1:{httpd.server_port}"})
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result
        assert post("/api/missions", {"objective": "x"}, "http://evil.test")[0] == 403
        status, created = post("/api/missions", {"objective": "Research sharing"})
        assert status == 201 and created["status"] == "paused"
        status, error = post(f"/api/missions/{created['mission_id']}/run", {})
        assert status == 409 and "Pi missing" in error["error"]
        assert post(f"/api/missions/{created['mission_id']}/resume", {})[0] == 200
        assert post(f"/api/missions/{created['mission_id']}/pause", {})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
