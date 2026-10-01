"""Exclude selected tools before model requests without changing other roles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rsi_system.core.config import load_research_config
from rsi_system.core.pi_agent import TOOLS, run_agent, link_pi_configuration
from rsi_system.core.runtime import MissionSpec, RuntimeCoordinator, RuntimeStore
from rsi_system.core.runtime_roles import model_policy
from .test_orchestrator import project
from .test_runtime_acceptance import decision


@pytest.mark.parametrize("disabled", [
    None, [], "web_search", {"unknown": []}, {"coordinator": "web_search"},
    {"coordinator": None}, {"coordinator": [None]}, {"coordinator": ["unknown"]},
    {"coordinator": ["bash"]}, {"coordinator": ["web_search", "web_search"]},
    {"problem_reframe": "web_search"}, {"problem_reframe": ["unknown"]},
    {"problem_reframe": ["web_search", "web_search"]},
])
def test_disabled_tool_config_rejects_invalid_roles_names_and_types(tmp_path, disabled):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["agent_disabled_tools"] = disabled
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="agent_disabled_tools"):
        load_research_config(root, config_dir=tmp_path / "empty")


def test_disabled_tools_from_private_config_reach_only_selected_role(tmp_path):
    root = project(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    (private / "research.json").write_text(json.dumps({"agent_disabled_tools": {"coordinator": ["web_search"]}}))
    config = load_research_config(root, config_dir=private)
    assert config["agent_disabled_tools"] == {"coordinator": ["web_search"]}


def fake_pi(tmp_path):
    executable = tmp_path / "fake-pi"
    executable.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
sys.stdin.read()
payload={"tools":sys.argv[sys.argv.index("--tools")+1],"disabled":json.loads(os.environ["RSI_AGENT_DISABLED_TOOLS"]),"argv":sys.argv}
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps(payload)}]}}))
''')
    executable.chmod(0o755)
    return executable


def test_reframe_tool_restrictions_are_additive_and_do_not_change_standard_researchers(tmp_path):
    root = project(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    declared = {"researcher": ["fetch_content"], "problem_reframe": ["web_search"]}
    (private / "research.json").write_text(json.dumps({"agent_disabled_tools": declared}))
    config = load_research_config(root, config_dir=private)
    for kind, expected in [("problem_reframe", ["fetch_content", "web_search"]),
                           ("standard", ["fetch_content"])]:
        effective = model_policy(config, "researcher", task_kind=kind)
        result = run_agent(executable=str(fake_pi(tmp_path)), role="researcher", prompt="{}",
                           cwd=tmp_path, output_dir=tmp_path / kind, timeout_seconds=5,
                           disabled_tools=effective["agent_disabled_tools"].get("researcher", []))
        assert result["payload"]["disabled"] == expected
        assert ("web_search" in result["tools"]) == (kind == "standard")
    assert config["agent_disabled_tools"] == declared
    assert model_policy(config, "coordinator")["agent_disabled_tools"].get("coordinator", []) == []


@pytest.mark.parametrize("role", ["coordinator", "researcher"])
def test_subprocess_disables_coordinator_search_but_preserves_researcher(tmp_path, monkeypatch, role):
    monkeypatch.setenv("RSI_AGENT_DISABLED_TOOLS", '["fetch_content"]')
    config = {"coordinator": ["web_search"]}
    result = run_agent(executable=str(fake_pi(tmp_path)), role=role, prompt="{}", cwd=tmp_path,
                       output_dir=tmp_path / "out", timeout_seconds=5,
                       disabled_tools=config.get(role, []))
    tools = set(result["payload"]["tools"].split(","))
    assert ("web_search" in tools) == (role == "researcher")
    assert {"mcp", "github_search", "github_read_file", "fetch_content", "get_search_content"} <= tools
    assert result["payload"]["disabled"] == config.get(role, [])
    assert set(result["tools"]) == tools
    assert TOOLS[role].split(",").count("web_search") == 1


@pytest.mark.parametrize("disabled,want_tools", [(["web_search"], "read"), (["read"], "")])
def test_decision_repair_never_adds_tools(tmp_path, disabled, want_tools):
    guard = tmp_path / "pi-guard.ts"
    guard.write_text("// fixture")
    (tmp_path / "pi-web.ts").write_text("// fixture")
    result = run_agent(executable=str(fake_pi(tmp_path)), role="coordinator", prompt="{}", cwd=tmp_path,
                       output_dir=tmp_path / "out", timeout_seconds=5, guard=guard,
                       decision_repair=True, disabled_tools=disabled)
    assert result["payload"]["tools"] == want_tools
    assert str(tmp_path / "pi-web.ts") not in result["payload"]["argv"]


def test_completed_receipt_is_reused_without_rewriting_tool_evidence(tmp_path):
    options = dict(executable=str(fake_pi(tmp_path)), role="coordinator", prompt="{}", cwd=tmp_path,
                   output_dir=tmp_path / "out", timeout_seconds=5)
    original = run_agent(**options)
    receipt = tmp_path / "out/agent_result.json"
    frozen = receipt.read_bytes()
    options["executable"] = "must-not-run"
    assert run_agent(**options, disabled_tools=["web_search"]) == original
    assert receipt.read_bytes() == frozen


def test_isolated_pi_config_links_private_web_settings_without_copying(tmp_path):
    source = tmp_path / "private/agent"
    source.mkdir(parents=True)
    for name in ("settings.json", "models.json"):
        (source / name).write_text("{}")
    web = source.parent / "web-search.json"
    web.write_text('{"provider":"exa"}')
    target = tmp_path / "output/pi-agent"
    link_pi_configuration(target, source)
    assert (target / "web-search.json").is_symlink()
    assert (target / "web-search.json").resolve() == web
    assert {path.name for path in target.iterdir()} == {"settings.json", "models.json", "web-search.json"}


def test_runtime_passes_disabled_tools_to_selected_agent(tmp_path):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["agent_disabled_tools"] = {"coordinator": ["web_search"]}
    path.write_text(json.dumps(config))
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "tool compatibility", resources={"max_agent_tasks": 3}))
    with RuntimeStore(runtime.store_path) as store:
        store.create_task("m", "coordinator", "wait for evidence", "test invocation policy")
    calls = []
    result = runtime.tick(agent_runner=lambda **kwargs: (calls.append(kwargs) or decision(action="wait")))
    assert result["status"] == "completed", result
    assert calls[0]["disabled_tools"] == ["web_search"]


def test_web_adapter_excludes_disabled_search_before_registration(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for web extension registration")
    version = subprocess.check_output([node, "--version"], text=True)
    if int(version.strip().lstrip("v").split(".")[0]) < 22:
        pytest.skip("Pi extension requires Node 22")
    extension = tmp_path / "pi-web.ts"
    shutil.copyfile(Path(__file__).resolve().parents[3] / "deployment/rsi/pi-web.ts", extension)
    prefix = tmp_path / ".local/share/rsi/pi/node_modules"
    web_package = prefix / "pi-web-access"
    web_package.mkdir(parents=True)
    (web_package / "package.json").write_text('{"type":"module"}')
    (web_package / "index.ts").write_text('''export default function(pi) {
for (const name of ["web_search", "fetch_content", "get_search_content", "unrelated_tool"])
  pi.registerTool({name, execute: async () => ({})});
}''')
    pi_package = prefix / "@earendil-works/pi-coding-agent"
    pi_package.mkdir(parents=True)
    (pi_package / "package.json").write_text("{}")
    typebox = prefix / "typebox"
    typebox.mkdir()
    (typebox / "index.js").write_text('''exports.Type = new Proxy({}, {
get: () => (...args) => ({args})
});''')
    jiti = prefix / "jiti"
    jiti.mkdir()
    (jiti / "index.js").write_text('''const { readFileSync } = require("node:fs");
exports.createJiti = () => ({import: path =>
  import("data:text/javascript;base64," + Buffer.from(readFileSync(path)).toString("base64"))});
''')
    script = '''import extension from EXTENSION;
async function names(disabled) {
  process.env.RSI_AGENT_DISABLED_TOOLS = JSON.stringify(disabled);
  const tools = []; await extension({registerTool: tool => tools.push(tool)});
  return tools.map(tool => tool.name);
}
console.log(JSON.stringify({coordinator:await names(["web_search"]), researcher:await names([])}));
'''.replace("EXTENSION", json.dumps(extension.as_uri()))
    result = subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script],
                            capture_output=True, text=True, env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    names = json.loads(result.stdout)
    assert names["coordinator"] == ["fetch_content", "get_search_content"]
    assert names["researcher"] == ["web_search", "fetch_content", "get_search_content"]
