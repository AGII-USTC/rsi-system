"""Wire-level regression tests for the Pi extension and durable socket bridge."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import tempfile
from threading import Thread
import time

import pytest

from rsi_system.core.runtime import RuntimeStore
from rsi_system.core.runtime_tools import ToolBridge, serve_tools
from .test_orchestrator import project
from .test_runtime_tools import setup_parent


EXTENSION = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-runtime-tools.ts"
NODE_BOOTSTRAP = """
import Module from 'node:module';
import net from 'node:net';
const load = Module._load;
Module._load = function(id, ...args) {
  if (id === 'typebox') return {Type: new Proxy({}, {get: () => () => ({})})};
  return load.call(this, id, ...args);
};
let connections = 0;
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  connections++;
  return connect.apply(this, args);
};
const socketTimeout = net.Socket.prototype.setTimeout;
net.Socket.prototype.setTimeout = function(_ms, callback) {
  return socketTimeout.call(this, Number(process.env.RSI_TEST_IDLE_MS), callback);
};
const {default: extension} = await import(process.env.RSI_EXTENSION_URL);
const tools = {};
extension({registerTool(tool) { tools[tool.name] = tool; }, on() {}});
"""


def run_node(tmp_path, script, socket_path, *, idle_ms=100, child_task=None, role=None):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Pi transport test")
    source = tmp_path / "transport.mjs"
    source.write_text(NODE_BOOTSTRAP + script)
    result = subprocess.run([node, "--experimental-strip-types", str(source)],
                            env={**os.environ, "RSI_TOOL_SOCKET": str(socket_path),
                                 "RSI_EXTENSION_URL": EXTENSION.as_uri(),
                                 "RSI_TEST_IDLE_MS": str(idle_ms),
                                 "RSI_CHILD_TASK": child_task or "", "RSI_AGENT_ROLE": role or ""},
                            capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def test_critic_bash_is_registered_as_bridge_tool(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="rsi-audit-wire-", dir="/tmp") as directory:
        root = project(Path(directory))
        runtime, parent, _ = setup_parent(root)
        with RuntimeStore(runtime.store_path) as store:
            task = store.create_task("tool-mission", "critic", "audit the evidence", "test",
                                     parent_task_id=parent["task_id"], idea_id="idea-a")
            store.claim_task("tool-mission")
        seen = []

        def fake_audit(**kwargs):
            seen.append(kwargs["command"])
            return {"kind": "diagnostic_audit", "exit_code": 0, "timed_out": False}

        monkeypatch.setattr("rsi_system.core.audit_shell.run_audit_shell", fake_audit)
        with serve_tools(root, task["task_id"]) as (socket_path, _bridge):
            result = run_node(tmp_path, """
if (!tools.bash) throw Error('critic bash was not registered');
const answer = await tools.bash.execute('audit-call', {command: 'python3 audit.py'});
console.log(JSON.stringify({answer: answer.details, name: tools.bash.name}));
""", socket_path, role="critic")
        assert result["name"] == "bash"
        assert result["answer"]["ok"] and seen == ["python3 audit.py"]


@contextmanager
def fake_server(handler):
    with tempfile.TemporaryDirectory(prefix="rsi-wire-") as directory:
        path = Path(directory) / "bridge.sock"

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        server = Server(str(path), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_wait_and_queued_status_survive_transport_idle_timeout(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="rsi-wait-", dir="/tmp") as directory:
        root = project(Path(directory))
        runtime, parent, _ = setup_parent(root)
        delegated = ToolBridge(root, parent["task_id"]).call({
            "tool": "rsi_delegate", "call_id": "slow-child", "args": {
                "role": "coder", "objective": "produce a slow result", "idea_id": "idea-a"}})
        assert delegated["ok"]

        def slow_agent(**kwargs):
            time.sleep(0.4)
            return {"payload": {"summary": "slow result completed"}, "tool_calls": []}

        monkeypatch.setattr("rsi_system.core.pi_agent.run_agent", slow_agent)
        monkeypatch.setattr("rsi_system.core.runtime_tools.BRIDGE_HEARTBEAT_SECONDS", 0.025)
        with serve_tools(root, parent["task_id"]) as (socket_path, bridge):
            result = run_node(tmp_path, """
const wait = tools.rsi_wait.execute('long-wait', {task_id: process.env.RSI_CHILD_TASK});
await new Promise(resolve => setTimeout(resolve, 50));
const status = tools.rsi_status.execute('queued-status', {task_id: process.env.RSI_CHILD_TASK});
const [waited, checked] = await Promise.all([wait, status]);
console.log(JSON.stringify({waited: waited.details, checked: checked.details, connections}));
""", socket_path, idle_ms=100, child_task=delegated["task_id"])
            if bridge.worker:
                bridge.worker.join(timeout=5)
        assert result["waited"]["ok"] and result["waited"]["status"] == "completed", result
        assert result["checked"]["ok"] and result["checked"]["status"] == "completed", result
        assert result["connections"] == 2
        with RuntimeStore(runtime.store_path) as store:
            operations = store.tool_operations(parent["task_id"])
            assert len([item for item in operations if item["tool"] == "rsi_wait"]) == 1
            assert len([item for item in operations if item["tool"] == "rsi_status"]) == 1


def test_lost_final_response_replays_same_durable_call(tmp_path):
    root = project(tmp_path)
    runtime, parent, _ = setup_parent(root)
    bridge = ToolBridge(root, parent["task_id"])
    seen = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline())
            response = bridge.call(request)
            seen.append(request)
            if len(seen) > 1:
                self.wfile.write((json.dumps(response) + "\n").encode())

    with fake_server(Handler) as socket_path:
        result = run_node(tmp_path, """
const sent = await tools.rsi_message.execute('durable-message', {text: 'One durable note', idea_id: 'idea-a', to_role: 'coordinator'});
console.log(JSON.stringify({sent: sent.details, connections}));
""", socket_path)
    assert result["sent"]["ok"] and result["connections"] == 2, result
    assert len(seen) == 2 and seen[0]["call_id"] == seen[1]["call_id"] == "durable-message"
    assert all(request["bridge_protocol"] == 2 for request in seen)
    with RuntimeStore(runtime.store_path) as store:
        events = [item for item in store.events("tool-mission")
                  if item["kind"] == "direct_message" and item["payload"]["text"] == "One durable note"]
        assert len(events) == 1


def test_old_client_gets_no_heartbeat_frame(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="rsi-old-", dir="/tmp") as directory:
        root = project(Path(directory))
        _, parent, _ = setup_parent(root)
        monkeypatch.setattr("rsi_system.core.runtime_tools.BRIDGE_HEARTBEAT_SECONDS", 0.02)

        def delayed_call(self, request):
            time.sleep(0.15)
            return {"ok": True, "status": "completed"}

        monkeypatch.setattr(ToolBridge, "call", delayed_call)
        with serve_tools(root, parent["task_id"]) as (socket_path, _):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall((json.dumps({"tool": "rsi_status", "call_id": "old-client",
                                            "args": {"task_id": "child"}}) + "\n").encode())
                client.settimeout(0.08)
                with pytest.raises(socket.timeout):
                    client.recv(1)
                client.settimeout(1)
                response = b""
                while not response.endswith(b"\n"):
                    response += client.recv(4096)
        assert json.loads(response) == {"ok": True, "status": "completed"}


def test_silent_bridge_still_times_out_and_retries(tmp_path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.rfile.readline()
            time.sleep(0.25)

    with fake_server(Handler) as socket_path:
        result = run_node(tmp_path, """
const checked = await tools.rsi_status.execute('silent-status', {task_id: 'unreachable'});
console.log(JSON.stringify({checked: checked.details, connections}));
""", socket_path, idle_ms=70)
    assert result["checked"]["error"]["code"] == "bridge_error"
    assert result["checked"]["error"]["retryable"] is True
    assert "timed out" in result["checked"]["error"]["message"]
    assert result["connections"] == 3


def test_development_tool_reaches_bridge_and_guard_accepts_owner(tmp_path, monkeypatch):
    from rsi_system.core import development_containers
    with tempfile.TemporaryDirectory(prefix='rsi-dev-wire-', dir='/tmp') as directory:
        root = project(Path(directory))
        _, parent, _ = setup_parent(root)
        seen = []
        def development(**kwargs):
            seen.append(kwargs['command'])
            return {'kind': 'development_test', 'status': 'completed', 'exit_code': 0,
                    'scientific_evidence': False, 'stdout_tail': 'container-test-passed'}
        monkeypatch.setattr(development_containers, 'run_development_test', development)
        with serve_tools(root, parent['task_id']) as (socket_path, _):
            result = run_node(tmp_path, """
const {default: guard} = await import(process.env.RSI_EXTENSION_URL.replace('pi-runtime-tools.ts','pi-guard.ts'));
const hooks={};guard({registerTool() {},on(name,fn){hooks[name]=fn;}});
if(await hooks.tool_call({toolName:'rsi_dev_exec',input:{command:'test'}})) throw Error('owner blocked');
const result=await tools.rsi_dev_exec.execute('owned-test',{command:'test'});
const replay=await tools.rsi_dev_exec.execute('owned-test',{command:'test'});
process.env.RSI_AGENT_ROLE='critic';
if(!(await hooks.tool_call({toolName:'rsi_dev_exec',input:{command:'test'}}))?.block) throw Error('reviewer allowed');
console.log(JSON.stringify({result:JSON.parse(result.content[0].text),replay:JSON.parse(replay.content[0].text)}));
""", socket_path, idle_ms=500, role='researcher')
        assert result['result']['ok'] and not result['result']['scientific_evidence']
        assert result['replay'] == result['result'] and seen == ['test']
