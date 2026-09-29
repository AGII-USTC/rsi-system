"""Dashboard state and private configuration regression tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import sqlite3
import threading

import pytest

from rsi_system.core.config import load_research_config
from rsi_system.core.orchestrator import _call
from rsi_system.core.pi_agent import link_pi_configuration
from rsi_system.core.runtime import RuntimeCoordinator
from rsi_system.dashboard.configuration import Configuration
from rsi_system.dashboard import server


def fixture_root(tmp_path: Path) -> Path:
    public = tmp_path / "deployment" / "rsi"
    public.mkdir(parents=True)
    (public / "research.json").write_text(json.dumps({
        "models": {}, "thinking": "high", "agent_timeout_seconds": 120,
    }))
    return tmp_path


def fixture_db(root: Path, *, modern: bool) -> sqlite3.Connection:
    path = root / ".rsi" / "runtime" / "state.sqlite3"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE missions(mission_id TEXT, status TEXT, version INTEGER,
          created_at TEXT, updated_at TEXT, payload_json TEXT);
        CREATE TABLE agent_tasks(task_id TEXT, mission_id TEXT, role TEXT, status TEXT,
          objective TEXT, reason TEXT, candidate_id TEXT, output_json TEXT, error TEXT,
          created_at TEXT, updated_at TEXT);
        CREATE TABLE blackboard_events(event_id INTEGER PRIMARY KEY, mission_id TEXT,
          kind TEXT, payload_json TEXT, created_at TEXT);
        CREATE TABLE runs(run_id TEXT, mission_id TEXT, status TEXT, candidate_id TEXT,
          created_at TEXT);
    """)
    if modern:
        db.executescript("""
            CREATE TABLE task_scopes(task_id TEXT, parent_task_id TEXT,
              reply_to_task_id TEXT, idea_id TEXT);
            CREATE TABLE rsi_tool_operations(task_id TEXT, tool TEXT, status TEXT);
            ALTER TABLE runs ADD COLUMN kind TEXT;
            ALTER TABLE runs ADD COLUMN idea_ref TEXT;
        """)
    db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?)",
               ("m1", "active", 4, "2026-01-01", "2026-01-02", '{"objective":"research"}'))
    for number in range(165):
        task = f"task-{number}"
        db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (task, "m1", "researcher", "running" if number == 164 else "completed",
                    f"objective {number}", "reason", None,
                    '{"payload":{"summary":"result"}}', None, f"2026-01-01T{number:03}", "2026-01-02"))
        if modern:
            db.execute("INSERT INTO task_scopes VALUES(?,?,?,?)",
                       (task, "task-0" if number else None, None, "idea-1"))
    db.execute("INSERT INTO blackboard_events VALUES(?,?,?,?,?)",
               (1, "m1", "task_created", json.dumps({
                   "task_id": "task-164", "role": "researcher", "parent_task_id": "task-0",
                   "idea_id": "idea-1", "objective": "objective 164"}), "2026-01-02"))
    db.execute("INSERT INTO blackboard_events VALUES(?,?,?,?,?)",
               (2, "m1", "direct_message", json.dumps({
                   "from_task_id": "task-0", "from_role": "coordinator",
                   "to_task_id": "task-164", "to_role": "researcher", "text": "please check"}),
                "2026-01-02"))
    db.execute("INSERT INTO blackboard_events VALUES(?,?,?,?,?)",
               (3, "m1", "communication_published", json.dumps({
                   "task_id": "task-164", "author_role": "researcher", "files": [{"path": "report"}]}),
                "2026-01-02"))
    db.execute("INSERT INTO runs(run_id,mission_id,status,candidate_id,created_at) "
               "VALUES(?,?,?,?,?)", ("run-1", "m1", "completed", None, "2026-01-02"))
    if modern:
        db.execute("INSERT INTO rsi_tool_operations VALUES(?,?,?)",
                   ("task-164", "rsi_wait", "pending"))
    db.commit()
    return db


@pytest.mark.parametrize("modern", [False, True])
def test_snapshot_maps_real_events_and_legacy_schema(tmp_path, modern):
    root = fixture_root(tmp_path)
    db = fixture_db(root, modern=modern)
    db.close()
    state = server.snapshot(root, "m1")
    task = next(item for item in state["tasks"] if item["task_id"] == "task-164")
    assert task["parent_task_id"] == "task-0"
    assert task["idea_id"] == "idea-1"
    assert task["waiting"] is modern
    assert task["summary"] == "result"
    assert state["counts"]["tasks"] == 165
    assert state["truncated"]["tasks"] is True
    assert state["runs"][0]["kind"] is None
    assert state["events"][1]["from_task_id"] == "task-0"
    assert state["events"][1]["to_task_id"] == "task-164"
    assert state["events"][0]["from_task_id"] == "task-164"


def test_snapshot_corrupt_schema_is_not_empty(tmp_path):
    root = fixture_root(tmp_path)
    path = root / ".rsi" / "runtime" / "state.sqlite3"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="缺少表"):
        server.snapshot(root)


def test_active_mission_is_selected_beyond_list_limit(tmp_path):
    root = fixture_root(tmp_path)
    db = fixture_db(root, modern=True)
    for index in range(35):
        db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?)",
                   (f"later-{index}", "stopped", 1, "2026-02-01", f"2026-02-{index + 1:02}",
                    '{"objective":"old"}'))
    db.commit()
    db.close()
    state = server.snapshot(root)
    assert state["mission"]["mission_id"] == "m1"
    assert state["counts"]["missions"] == 36
    assert state["truncated"]["missions"] is True


def test_configuration_private_override_and_round_trip(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    private = tmp_path / "private"
    pi_dir = tmp_path / "pi"
    config = Configuration(root, config_dir=private, pi_dir=pi_dir)
    body = {"name": "acme", "base_url": "https://api.example.test/v1",
            "api": "openai-responses", "models": ["model-a", "model-b"],
            "api_key": "hidden-key", "context_window": 200_000, "max_tokens": 32_000}
    reply = config.save_provider(body)
    assert "hidden-key" not in json.dumps(reply)
    assert "hidden-key" not in config.models_path.read_text()
    assert (private / "keys" / "acme.key").read_text().strip() == "hidden-key"
    assert os.stat(private / "keys" / "acme.key").st_mode & 0o777 == 0o600
    assert os.stat(config.models_path).st_mode & 0o777 == 0o600
    config.save_roles({"roles": {"researcher": {"provider": "acme", "model": "model-a"}},
                       "thinking": "xhigh"})
    monkeypatch.setenv("RSI_CONFIG_DIR", str(private))
    assert load_research_config(root)["models"]["researcher"]["model"] == "model-a"
    assert RuntimeCoordinator(root)._config()["thinking"] == "xhigh"
    stored = json.loads(config.models_path.read_text())
    stored["providers"]["acme"]["models"][0]["reasoning"] = False
    stored["providers"]["acme"]["models"][0]["input"] = ["text", "image"]
    stored["providers"]["acme"]["models"][0]["contextWindow"] = 180_000
    config.models_path.write_text(json.dumps(stored))
    config.save_provider({**body, "api_key": "", "context_window": 50_000, "max_tokens": 2000})
    kept = json.loads(config.models_path.read_text())["providers"]["acme"]["models"][0]
    assert (kept["reasoning"], kept["input"], kept["contextWindow"], kept["maxTokens"]) == (
        False, ["text", "image"], 180_000, 32_000)
    with pytest.raises(ValueError, match="请先调整"):
        config.save_provider({**body, "api_key": "", "models": ["model-b"]})
    assert config.public()["roles"]["researcher"] == {"provider": "acme", "model": "model-a"}


def test_provider_discovery_uses_temporary_key_and_extracts_limits(tmp_path, monkeypatch):
    config = Configuration(fixture_root(tmp_path), config_dir=tmp_path / "private",
                           pi_dir=tmp_path / "pi")
    captured = {}

    def fake_request(url, api, api_key, *, timeout):
        if not url.endswith("/models"):
            raise ValueError("billing probe intentionally unavailable")
        captured.update(url=url, api=api, api_key=api_key, timeout=timeout)
        return 200, {"data": [
            {"id": "gpt-a", "context_window": 128000, "max_tokens": 4096,
             "secret": "must not be copied"},
            {"id": "gpt-a"}, {"id": "bad id"},
        ]}

    monkeypatch.setattr("rsi_system.dashboard.configuration._safe_json_request", fake_request)
    result = config.discover_provider({
        "name": "draft", "base_url": "https://api.example.test/v1/",
        "api": "openai-responses", "api_key": "temporary-key", "models": ["old"],
    })
    assert captured == {
        "url": "https://api.example.test/v1/models", "api": "openai-responses",
        "api_key": "temporary-key", "timeout": 4.0,
    }
    assert result["models"] == [{"id": "gpt-a", "context_window": 128000, "max_tokens": 4096}]
    assert "temporary-key" not in json.dumps(result)
    assert not (tmp_path / "pi" / "models.json").exists()
    assert not (tmp_path / "private").exists()


def test_anthropic_discovery_returns_known_models_without_probe(tmp_path, monkeypatch):
    config = Configuration(fixture_root(tmp_path), config_dir=tmp_path / "private",
                           pi_dir=tmp_path / "pi")
    monkeypatch.setattr("rsi_system.dashboard.configuration._safe_json_request",
                        lambda *args, **kwargs: pytest.fail("Anthropic should not probe /models"))
    result = config.discover_provider({
        "name": "anthropic", "base_url": "https://api.anthropic.com/v1",
        "api": "anthropic-messages", "api_key": "temporary-key",
    })
    assert result["model_source"] == "known-list"
    assert result["models"]
    assert all("id" in item for item in result["models"])


def test_orchestrator_reload_private_routing_at_invocation(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    public = root / "deployment" / "rsi" / "research.json"
    public.write_text(json.dumps({"objective": "research", "pi_executable": "/bin/false",
                                  "models": {}, "thinking": "high", "max_cycles": 1,
                                  "max_repairs": 0, "max_agent_calls_per_day": 2,
                                  "agent_timeout_seconds": 120}))
    private = tmp_path / "private"
    private.mkdir()
    (private / "research.json").write_text(json.dumps({
        "models": {"researcher": {"provider": "acme", "model": "fresh-model"}},
        "thinking": "xhigh"}))
    monkeypatch.setenv("RSI_CONFIG_DIR", str(private))
    output = tmp_path / "out"
    output.mkdir()
    prompt = "test prompt"
    (output / "prompt.txt").write_text(prompt)
    state = {"cycle": {"inflight": {"output_dir": str(output),
                                   "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()},
                       "workspace": {"path": str(tmp_path)}}}
    captured = {}
    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"payload": {"summary": "done"}}
    result = _call(root, state, {"models": {}, "thinking": "low"}, "researcher", {}, fake_runner)
    assert result == {"summary": "done"}
    assert (captured["provider"], captured["model"], captured["thinking"]) == (
        "acme", "fresh-model", "xhigh")


@pytest.mark.parametrize("override", [
    {"base_url": "https://api.example.test/v1?api_key=secret"},
    {"base_url": "https://user:pass@api.example.test/v1"},
    {"base_url": "https://api.example.test/v1\nsecret"},
    {"base_url": "https://api.example.test:bad/v1"},
    {"unexpected": "value"},
    {"models": ["bad model"]},
])
def test_provider_rejects_bad_inputs(tmp_path, override):
    config = Configuration(fixture_root(tmp_path), config_dir=tmp_path / "private",
                           pi_dir=tmp_path / "pi")
    body = {"name": "acme", "base_url": "https://api.example.test/v1",
            "api": "openai-responses", "models": ["model-a"]}
    with pytest.raises(ValueError):
        config.save_provider({**body, **override})


def test_concurrent_provider_updates_preserve_both(tmp_path):
    config = Configuration(fixture_root(tmp_path), config_dir=tmp_path / "private",
                           pi_dir=tmp_path / "pi")
    def save(name):
        config.save_provider({"name": name, "base_url": "https://api.example.test/v1",
                              "api": "openai-responses", "models": ["model-a"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, ("alpha", "beta")))
    assert set(json.loads(config.models_path.read_text())["providers"]) == {"alpha", "beta"}


def test_two_project_configs_serialize_shared_pi_models(tmp_path):
    root = fixture_root(tmp_path)
    configs = [Configuration(root, config_dir=tmp_path / name, pi_dir=tmp_path / "pi")
               for name in ("project-a", "project-b")]
    def save(pair):
        index, config = pair
        config.save_provider({"name": f"provider-{index}", "base_url": "https://api.example.test/v1",
                              "api": "openai-responses", "models": ["model-a"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, enumerate(configs)))
    assert set(json.loads(configs[0].models_path.read_text())["providers"]) == {"provider-0", "provider-1"}


def test_empty_pi_models_configuration_is_readable(tmp_path):
    root = tmp_path / "root"
    deployment = root / "deployment" / "rsi"
    deployment.mkdir(parents=True)
    (deployment / "research.json").write_text('{}')
    pi_dir = tmp_path / "pi"
    pi_dir.mkdir()
    (pi_dir / "models.json").write_text('{}')
    config = Configuration(root, config_dir=tmp_path / "private", pi_dir=pi_dir)
    assert config.public()["providers"] == []


def test_public_url_redacts_old_credentials_and_per_model_edits(tmp_path):
    config = Configuration(fixture_root(tmp_path), config_dir=tmp_path / "private",
                           pi_dir=tmp_path / "pi")
    config.save_provider({"name": "acme", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["model-a", "model-b"]})
    stored = json.loads(config.models_path.read_text())
    stored["providers"]["acme"]["baseUrl"] = "https://user:secret@api.example.test/v1?token=secret#secret"
    config.models_path.write_text(json.dumps(stored))
    assert config.public()["providers"][0]["base_url"] == "https://api.example.test/v1"
    assert "secret" not in json.dumps(config.public())
    config.save_provider({"name": "acme", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["model-a", "model-b"],
                          "model_settings": {"model-a": {"context_window": 80_000, "max_tokens": 4000}}})
    models = config.public()["providers"][0]["models"]
    assert models[0]["context_window"] == 80_000 and models[0]["max_tokens"] == 4000
    assert models[1]["context_window"] == 128_000 and models[1]["max_tokens"] == 16_384


def test_per_model_thinking_round_trip_and_clear_falls_back_to_role(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    private = tmp_path / "private"
    config = Configuration(root, config_dir=private, pi_dir=tmp_path / "pi")
    base = {"name": "acme", "base_url": "https://api.example.test/v1",
            "api": "openai-responses", "models": ["slow", "fast"]}
    config.save_provider({**base, "model_settings": {
        "slow": {"thinking": "low"}, "fast": {"thinking": "xhigh"}}})
    public = config.public()["providers"][0]["models"]
    assert {item["id"]: item["thinking"] for item in public} == {"slow": "low", "fast": "xhigh"}
    private_data = json.loads((private / "research.json").read_text())
    assert private_data["model_thinking"] == {"acme": {"slow": "low", "fast": "xhigh"}}
    config.save_provider({**base, "model_settings": {"slow": {"thinking": None}}})
    public = config.public()["providers"][0]["models"]
    assert {item["id"]: item["thinking"] for item in public} == {"slow": None, "fast": "xhigh"}
    config.save_roles({"roles": {"researcher": {"provider": "acme", "model": "slow"}}, "thinking": "medium"})
    monkeypatch.setenv("RSI_CONFIG_DIR", str(private))
    assert load_research_config(root)["model_thinking"] == {"acme": {"fast": "xhigh"}}


def test_key_command_quotes_private_path(tmp_path):
    root = fixture_root(tmp_path)
    config = Configuration(root, config_dir=tmp_path / "private's space",
                           pi_dir=tmp_path / "pi")
    config.save_provider({"name": "acme", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["model-a"],
                          "api_key": "secret"})
    stored = json.loads(config.models_path.read_text())["providers"]["acme"]["apiKey"]
    assert stored.startswith("!cat ")
    assert "secret" not in stored
    assert shlex.split(stored[1:]) == [
        "cat", str(config.config_dir / "keys" / "acme.key")]


def test_pi_task_configuration_references_private_source(tmp_path):
    source = tmp_path / "pi-source"
    source.mkdir()
    (source / "models.json").write_text('{"apiKey":"secret"}')
    task = tmp_path / "task" / "pi-agent"
    task.mkdir(parents=True)
    (task / "models.json").write_text("stale private copy")
    link_pi_configuration(task, source)
    assert (task / "models.json").is_symlink()
    assert (task / "models.json").resolve() == (source / "models.json")
    assert not (task / "settings.json").exists()


def test_http_origin_and_static_boundaries(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("app")
    (static / "app.js").write_text("code")
    outside = tmp_path / "secret"
    outside.write_text("private")
    (static / "assets").mkdir()
    (static / "assets" / "escape.js").symlink_to(outside)
    monkeypatch.setattr(server, "STATIC", static)
    config = Configuration(root, config_dir=tmp_path / "private", pi_dir=tmp_path / "pi")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(root, config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_port
        def request(method, path, *, headers=None, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result
        status, headers, data = request("GET", "/")
        assert status == 200 and data == b"app"
        assert "'unsafe-inline'" in headers["Content-Security-Policy"]
        assert request("GET", "/assets/escape.js")[0] == 404
        assert request("GET", "/%2e%2e/secret")[0] == 404
        assert request("GET", "/.rsi/runtime/state.sqlite3")[0] == 404
        body = json.dumps({"roles": {}, "thinking": "high"})
        headers = {"Content-Type": "application/json", "Origin": "http://evil.test"}
        assert request("PUT", "/api/roles", headers=headers, body=body)[0] == 403
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        assert request("PUT", "/api/roles", headers=headers, body=body)[0] == 200
        provider = json.dumps({"name": "acme", "base_url": "https://api.example.test/v1",
                               "api": "openai-responses", "models": ["model-a"],
                               "api_key": "http-secret"})
        status, _, reply = request("PUT", "/api/provider", headers=headers, body=provider)
        assert status == 200 and b"http-secret" not in reply
        assert request("GET", "/api/config", headers={"Host": "evil.test"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
