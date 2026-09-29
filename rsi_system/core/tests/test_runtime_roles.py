"""Verify role identity, configured models, and writing tools for every Agent."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rsi_system.core.pi_agent import run_agent
from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from rsi_system.core.runtime_roles import model_policy
from rsi_system.core.sandbox import agent_command
from .test_orchestrator import project
from .test_runtime_acceptance import candidate, decision, request
from .test_runtime_hardening import hypothesis


@pytest.mark.parametrize("role", ["hypothesis", "analyzer", "critic"])
def test_semantic_role_and_explicit_model_reach_invocation_and_ledger(tmp_path, role):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["models"] = {role: {"model": f"{role}-fixture", "provider": "fixture"},
                        "reviewer": {"model": "wrong-fallback", "provider": "fixture"}}
    path.write_text(json.dumps(config))
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "test independent responsibilities"))
        if role != "hypothesis":
            store.create_candidate("m", candidate())
        task = store.create_task("m", role, "resolve the assigned question", "test",
                                 candidate_id="c0" if role != "hypothesis" else None)
    calls = []
    def agent(**kwargs):
        calls.append(kwargs)
        return {"hypotheses": [hypothesis()]} if role == "hypothesis" else {"summary": "fixture evidence review"}
    result = coordinator.tick(agent_runner=agent)
    assert result["status"] == "completed", result
    assert calls[0]["role"] == role
    assert calls[0]["model"] == f"{role}-fixture"
    assert json.loads(calls[0]["prompt"].split("\n", 1)[1])["role_responsibility"]
    with RuntimeStore(coordinator.store_path) as store:
        policy = json.loads(store.db.execute("SELECT policy_json FROM runtime_agent_calls WHERE task_id=?",
                                             (task["task_id"],)).fetchone()[0])
        assert policy["role"] == role
        assert policy["model"]["model"] == f"{role}-fixture"


def test_model_fallback_uses_research_model_for_hypothesis():
    config = {"models": {"researcher": {"model": "research"}, "reviewer": {"model": "review"}}}
    assert model_policy(config, "hypothesis")["models"]["hypothesis"]["model"] == "research"
    assert model_policy(config, "critic")["models"]["critic"]["model"] == "review"
    assert "hypothesis" not in config["models"]


def test_task_model_routing_selects_explicit_routes():
    config = {"model_routing": {
        "coordinator": {"provider": "fixture", "model": "coordinator-model", "thinking": "high"},
        "problem_reframe": {"provider": "fixture", "model": "reframe-model", "thinking": "high"},
        "researcher": {"provider": "fixture", "model": "research-model", "thinking": "xhigh"},
        "default": {"provider": "fixture", "model": "fallback-model", "thinking": "high"},
    }}
    coordinator = model_policy(config, "coordinator")
    reframe = model_policy(config, "researcher", task_kind="problem_reframe")
    researcher = model_policy(config, "researcher")
    critic = model_policy(config, "critic")
    assert (coordinator["models"]["coordinator"]["model"], coordinator["thinking"]) == ("coordinator-model", "high")
    assert (reframe["models"]["researcher"]["model"], reframe["thinking"]) == ("reframe-model", "high")
    assert (researcher["models"]["researcher"]["model"], researcher["thinking"]) == ("research-model", "xhigh")
    assert (critic["models"]["critic"]["model"], critic["thinking"]) == ("fallback-model", "high")


def test_task_model_routing_keeps_legacy_role_models_authoritative():
    config = {"model_routing": {"default": {"provider": "fixture", "model": "fallback-model", "thinking": "high"}},
              "models": {"researcher": {"provider": "fixture", "model": "legacy", "thinking": "low"}}}
    policy = model_policy(config, "researcher")
    assert policy["models"]["researcher"] == config["models"]["researcher"]
    assert policy["thinking"] == "low"


def test_runtime_ledger_records_selected_task_route(tmp_path):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["model_routing"] = {
        "coordinator": {"provider": "fixture", "model": "astra", "thinking": "high"},
        "problem_reframe": {"provider": "fixture", "model": "astra", "thinking": "high"},
        "researcher": {"provider": "fixture", "model": "sol", "thinking": "xhigh"},
        "default": {"provider": "fixture", "model": "sol", "thinking": "high"},
    }
    path.write_text(json.dumps(config))
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "reframe", resources={"max_agent_tasks": 4}))
    with RuntimeStore(coordinator.store_path) as store:
        root_node = store.research_node("m", "m-root")
        parent = store.create_task("m", "coordinator", "delegate", "uncertain")
        parent = store.claim_task("m")
        task = store.create_task("m", "researcher", "reframe", "test", parent_task_id=parent["task_id"],
                                 research_node_id="m-root", research_revision=root_node["revision"], task_kind="problem_reframe")
    calls = []
    result = coordinator.tick(agent_runner=lambda **kwargs: (calls.append(kwargs) or {
        "reframe_review": {
            "problem_statement": "reframe the question",
            "alternative_explanations": ["alternative"],
            "discriminating_checks": ["check"],
            "recommendation": "continue",
        }
    }))
    assert result["status"] == "completed", result
    assert (calls[0]["model"], calls[0]["thinking"]) == ("astra", "high")
    with RuntimeStore(coordinator.store_path) as store:
        policy = json.loads(store.db.execute("SELECT policy_json FROM runtime_agent_calls WHERE task_id=?",
                                             (task["task_id"],)).fetchone()[0])
    assert policy["task_kind"] == "problem_reframe"
    assert policy["model"] == {"provider": "fixture", "model": "astra", "thinking": "high"}
    assert policy["thinking"] == "high"


def test_runtime_task_route_preserves_researcher_xhigh_over_mission_fallback(tmp_path):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["model_routing"] = {
        "coordinator": {"provider": "fixture", "model": "astra", "thinking": "high"},
        "problem_reframe": {"provider": "fixture", "model": "astra", "thinking": "high"},
        "researcher": {"provider": "fixture", "model": "sol", "thinking": "xhigh"},
        "default": {"provider": "fixture", "model": "sol", "thinking": "high"},
    }
    path.write_text(json.dumps(config))
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "research", resources={"max_agent_tasks": 3, "agent_thinking": "high"}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "researcher", "explore", "test")
    calls = []
    result = coordinator.tick(agent_runner=lambda **kwargs: (calls.append(kwargs) or {"summary": "done"}))
    assert result["status"] == "completed", result
    assert (calls[0]["model"], calls[0]["thinking"]) == ("sol", "xhigh")
    with RuntimeStore(coordinator.store_path) as store:
        policy = json.loads(store.db.execute("SELECT policy_json FROM runtime_agent_calls WHERE task_id=?",
                                             (store.db.execute("SELECT task_id FROM agent_tasks WHERE mission_id=?",
                                                               ("m",)).fetchone()[0],)).fetchone()[0])
    assert policy["task_kind"] == "standard"
    assert policy["model"] == {"provider": "fixture", "model": "sol", "thinking": "xhigh"}
    assert policy["thinking"] == "xhigh"


def test_configured_python_reaches_agent_shell_without_resolving_venv_symlink(tmp_path):
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    interpreter = venv / "python"
    interpreter.symlink_to(sys.executable)
    fake = tmp_path / "fake-pi"
    fake.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
sys.stdin.read()
payload={"python":os.environ.get("RSI_RESEARCH_PYTHON"),"path":os.environ["PATH"].split(os.pathsep)[0]}
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps(payload)}]}}))
''')
    fake.chmod(0o755)
    result = run_agent(executable=str(fake), role="coder", prompt="{}", cwd=tmp_path,
                       output_dir=tmp_path / "receipt", timeout_seconds=5,
                       python_executable=str(interpreter), exploratory=True)
    assert result["payload"] == {"python": str(interpreter), "path": str(venv)}


def test_coordinator_prompt_assigns_independent_research_before_implementation(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "explore competing sparse-reward mechanisms"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", "coordinator", "choose research directions", "open question")
        claimed = store.claim_task("m")
        prompt = coordinator._prompt("coordinator", store.mission("m"), claimed, store)
    responsibility = json.loads(prompt.split("\n", 1)[1])["role_responsibility"]
    assert "without a fixed researcher count" in responsibility
    assert "autonomy to search, prototype, probe, and revise" in responsibility
    assert "without approval or other branches finishing" in responsibility
    assert "measurable outcomes in both directions" in responsibility
    assert "representations and algorithms" in responsibility
    assert "retain questions left open" in responsibility
    assert "including failures" in responsibility


def test_research_prompt_requires_literature_before_local_mechanism_work(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "investigate an open mechanism"))
    with RuntimeStore(coordinator.store_path) as store:
        task = store.create_task("m", "researcher", "explore", "open question")
        claimed = store.claim_task("m")
        prompt = coordinator._prompt("researcher", store.mission("m"), claimed, store)
    body = prompt.split("\n", 1)[1]
    payload = json.loads(body)
    text = prompt.lower()
    assert "literature_review" in payload["response_contract"]
    responsibility = payload["role_responsibility"]
    assert "Search literature and the Wiki before committing" in responsibility
    assert "reframe the problem" in responsibility
    assert "any relevant field" in responsibility
    assert "without coordinator approval" in responsibility
    assert "read results with rsi_wait" in responsibility
    assert "Skip uninformative probes with a reason" in responsibility
    assert "failure_reason" in payload["response_contract"]["literature_review"]
    assert "actual reading depth" in responsibility
    assert "access depth" in payload["response_contract"]["literature_review"]["sources"]
    assert "substantive content" in payload["response_contract"]["ideas"]


@pytest.mark.parametrize("role,required", [
    ("coordinator", "retain alternatives"),
    ("researcher", "Save literature_review.json"),
    ("hypothesis", "Trace claims to attributed Ideas"),
    ("coder", "source versions"),
    ("analyzer", "source-linked finding draft"),
    ("critic", "citation reading depth"),
])
def test_role_prompts_preserve_source_and_idea_provenance(tmp_path, role, required):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "research an open question"))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_task("m", role, "review the assigned research question", "prompt check",
                          idea_id="idea-a" if role in {"coder", "analyzer", "critic"} else None)
        claimed = store.claim_task("m")
        prompt = coordinator._prompt(role, store.mission("m"), claimed, store)
    assert required in prompt
    assert "a handoff alone does not complete knowledge curation" in prompt
    assert "dedicated curator publishes source-linked Wiki" in prompt
    assert "unfinished Ideas as substantive drafts" in prompt


def test_coder_prompt_requires_reuse_audit_before_new_code(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec("m", "implement a bounded mechanism",
                                  authorization={"allow_code_candidates": True}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_candidate("m", candidate("c0", kind="baseline"))
        task = store.create_task("m", "coder", "implement the candidate", "test", candidate_id="c0")
        claimed = store.claim_task("m")
        prompt = coordinator._prompt("coder", store.mission("m"), claimed, store)
    body = prompt.split("\n", 1)[1]
    payload = json.loads(body)
    text = prompt.lower()
    responsibility = payload["role_responsibility"]
    assert "Prefer reuse" in responsibility
    assert "git clone" in responsibility
    assert "without per-file approval" in responsibility
    assert "return results directly to the delegating Agent" in responsibility
    assert "completed source snapshot" in responsibility
    assert "implementation_note.md" in responsibility
    assert "license" in responsibility and "compatibility" in responsibility
    assert "reuse_audit" in payload["response_contract"]


@pytest.mark.parametrize("role", ["coordinator", "researcher", "hypothesis", "analyzer", "critic", "reviewer", "coder"])
def test_pi_subprocess_keeps_semantic_role_and_gives_every_role_write_tools(tmp_path, role):
    fake = tmp_path / "fake-pi"
    fake.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
sys.stdin.read()
payload={"role":os.environ["RSI_AGENT_ROLE"],"tools":sys.argv[sys.argv.index("--tools")+1],
"workspace":os.environ["RSI_AGENT_WORKSPACE"],"communications":os.environ.get("RSI_COMMUNICATION_MANIFEST"),
"argv":sys.argv}
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps(payload)}]}}))
''')
    fake.chmod(0o755)
    guard = tmp_path / "pi-guard.ts"
    github = tmp_path / "pi-github.ts"
    web = tmp_path / "pi-web.ts"
    guard.write_text("// fixture")
    github.write_text("// fixture")
    web.write_text("// fixture")
    manifest = tmp_path / "inbox.json"
    manifest.write_text("{}")
    result = run_agent(executable=str(fake), role=role, prompt="{}", cwd=tmp_path,
                       output_dir=tmp_path / "receipt", timeout_seconds=5,
                       guard=guard if role not in {"analyzer", "critic"} else None,
                       communication_manifest=manifest)
    assert result["role"] == result["payload"]["role"] == role
    tools = set(result["payload"]["tools"].split(","))
    assert {"write", "edit"} <= tools
    assert Path(result["payload"]["workspace"]).is_dir()
    assert result["payload"]["communications"] == str(manifest.resolve())
    assert ("bash" in tools) == (role in {"researcher", "coder", "analyzer", "critic"})
    assert ({"github_search", "github_read_file", "mcp"} <= tools) == (role in {"coordinator", "researcher", "coder"})
    assert ({"web_search", "fetch_content", "get_search_content"} <= tools) == (role in {"coordinator", "researcher", "coder"})
    assert (str(github) in result["payload"]["argv"]) == (role in {"coordinator", "researcher", "coder"})
    assert (str(web) in result["payload"]["argv"]) == (role in {"coordinator", "researcher", "coder"})


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")
def test_agent_shell_writes_only_in_assigned_workspaces(tmp_path):
    workspace = tmp_path / "task-workspace"
    candidate_workspace = tmp_path / "candidate-worktree"
    workspace.mkdir()
    candidate_workspace.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("original")
    command = ["/bin/sh", "-c", 'printf allowed > "$1"; printf denied > "$2"',
               "sh", str(workspace / "note.txt"), str(protected)]
    result = subprocess.run(agent_command(command, workspace=workspace), capture_output=True, text=True)
    assert result.returncode != 0
    assert (workspace / "note.txt").read_text() == "allowed"
    assert protected.read_text() == "original"
    command = ["/bin/sh", "-c", 'printf implementation > "$1"',
               "sh", str(candidate_workspace / "prototype.py")]
    subprocess.run(agent_command(command, workspace=workspace, candidate_workspace=candidate_workspace),
                   check=True, capture_output=True, text=True)
    assert (candidate_workspace / "prototype.py").read_text() == "implementation"


def test_coder_github_extension_is_bounded_and_read_only(tmp_path):
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    package = Path.home() / ".local/share/rsi/pi/node_modules/@earendil-works/pi-coding-agent/package.json"
    if not node or not package.is_file():
        pytest.skip("Pi extension runtime is not installed")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout
    if int(version.strip().lstrip("v").split(".")[0]) < 22:
        pytest.skip("Pi extension requires Node 22")
    extension = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-github.ts"
    guard = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(f"#!{sys.executable}\n" + '''import base64,json,os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ["RSI_CURL_TRACE"]).open("a") as output:
    output.write(json.dumps(args)+"\\n")
if "/search/" in args[-1]:
    payload={"total_count":1,"items":[{"full_name":"example/project",
        "html_url":"https://github.com/example/project","description":"fixture",
        "default_branch":"main","updated_at":"2026-01-01","stargazers_count":3}]}
else:
    payload={"type":"file","encoding":"base64","content":base64.b64encode(b"fixture source").decode(),
        "html_url":"https://github.com/example/project/blob/main/README.md","sha":"abc","size":14}
print(json.dumps(payload))
''')
    fake_curl.chmod(0o755)
    trace = tmp_path / "curl-trace.jsonl"
    script = '''import extension from EXTENSION;
import guard from GUARD;
const tools=[]; extension({registerTool:(tool)=>tools.push(tool)});
if(tools.map(tool=>tool.name).join(",")!=="github_search,github_read_file") throw Error("wrong tools");
const found=await tools[0].execute("id",{query:"robotics"});
const file=await tools[1].execute("id",{repository:"example/project",path:"README.md"});
if(!found.content[0].text.includes("example/project")||!file.content[0].text.includes("fixture source")) throw Error("missing results");
try {await tools[1].execute("id",{repository:"example/project",path:"../secret"}); throw Error("traversal allowed");}
catch(error) {if(error.message==="traversal allowed") throw error;}
let hook; guard({on:(_,handler)=>{hook=handler;}});
process.env.RSI_AGENT_ROLE="coder";
if(await hook({toolName:"github_search",input:{query:"robotics"}})!==undefined) throw Error("coder search blocked");
if(await hook({toolName:"bash",input:{command:"git clone https://github.com/example/project"}})!==undefined) throw Error("coder shell blocked");
if(await hook({toolName:"mcp",input:{tool:"alphaxiv_discover_papers",args:{}}})!==undefined) throw Error("coder papers blocked");
process.env.RSI_AGENT_ROLE="researcher";
if(await hook({toolName:"github_search",input:{query:"robotics"}})!==undefined) throw Error("researcher GitHub blocked");
if(await hook({toolName:"bash",input:{command:"python prototype.py"}})!==undefined) throw Error("researcher shell blocked");
if(await hook({toolName:"web_search",input:{query:"robotics",workflow:"none"}})!==undefined) throw Error("researcher web search blocked");
if(!(await hook({toolName:"web_search",input:{query:"robotics",provider:"all"}}))?.block) throw Error("provider override allowed");
if(await hook({toolName:"fetch_content",input:{url:"https://example.org/paper"}})!==undefined) throw Error("public page blocked");
if(!(await hook({toolName:"fetch_content",input:{url:"/tmp/secret"}}))?.block) throw Error("local fetch allowed");
if(!(await hook({toolName:"fetch_content",input:{url:"http://localhost:7897"}}))?.block) throw Error("local URL allowed");
if(!(await hook({toolName:"fetch_content",input:{url:"https://127.0.0.1/secret"}}))?.block) throw Error("private IP allowed");
process.env.RSI_AGENT_ROLE="critic";
if(!(await hook({toolName:"web_search",input:{query:"robotics"}}))?.block) throw Error("critic web search allowed");
if(!(await hook({toolName:"bash",input:{command:"pwd"}}))?.block) throw Error("critic shell without bridge allowed");
process.env.RSI_TOOL_SOCKET="/tmp/test-rsi-bridge";
if(await hook({toolName:"bash",input:{command:"pwd"}})!==undefined) throw Error("critic audit shell blocked");
delete process.env.RSI_TOOL_SOCKET;
if(!(await hook({toolName:"github_write",input:{}}))?.block) throw Error("write tool allowed");
'''.replace("EXTENSION", json.dumps(extension.as_uri())).replace("GUARD", json.dumps(guard.as_uri()))
    subprocess.run([node, "--experimental-strip-types", "--input-type=module", "-e", script],
                   check=True, capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                        "RSI_CURL_TRACE": str(trace)})
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(calls) == 2
    assert all(call[0] == "-q" for call in calls)
    assert all(call[-1].startswith("https://api.github.com/") for call in calls)
    assert all(call[call.index("--request") + 1] == "GET" for call in calls)
    assert all("--max-time" in call and "--max-filesize" in call for call in calls)


def test_role_guidance_does_not_override_mission_code_authorization(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    with RuntimeStore(coordinator.store_path) as store:
        store.create_mission(MissionSpec("m", "configuration demo", authorization={"allow_code_candidates": False}))
        store.create_candidate("m", candidate())
        store.create_task("m", "coordinator", "delegate", "test")
        task = store.claim_task("m")
        with pytest.raises(ValueError, match="does not authorize"):
            coordinator._handle_coordinator(store, store.mission("m"), task, decision([request("coder", "c0")]))
        assert len(store.list_tasks("m")) == 1


def test_model_policy_prefers_provider_model_thinking_then_role_default():
    config = {"models": {"researcher": {"provider": "acme", "model": "deep"}},
              "providers": {"acme": {"models": [{"id": "deep", "thinking": "low"}]}},
              "thinking": "xhigh"}
    assert model_policy(config, "researcher")["thinking"] == "low"
    config["providers"]["acme"]["models"][0]["thinking"] = None
    assert model_policy(config, "researcher")["thinking"] == "xhigh"
