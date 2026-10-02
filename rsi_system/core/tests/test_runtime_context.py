"""Compact prompts keep verifiable sources and enforce the provider input budget."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rsi_system.core.pi_agent import run_agent
from rsi_system.core.config import load_research_config
from rsi_system.core.runtime import RuntimeCoordinator
from rsi_system.core.runtime_context import compile_prompt_context
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_types import MissionSpec
from .test_orchestrator import project
from .test_runtime_acceptance import decision


GUARD = Path(__file__).resolve().parents[3] / "deployment/rsi/pi-guard.ts"


@pytest.fixture(autouse=True)
def isolated_private_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_CONFIG_DIR", str(tmp_path / "private-config"))


def manifest_for(tmp_path, task):
    path = tmp_path / "evidence/manifest.json"
    path.parent.mkdir(parents=True)
    original = path.parent / "run-a/summary.json"
    original.parent.mkdir()
    original.write_text('{"metric":3}', encoding="utf-8")
    path.write_text(json.dumps({"schema_version": 1, "task_id": task["task_id"],
        "mission_id": task["mission_id"], "files": [{"run_id": "run-a",
            "path": "run-a/summary.json", "source": "recorded Run summary",
            "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "verification": "recorded at execution"}], "omitted": []}), encoding="utf-8")
    return path


def source_record(reference):
    path = Path(reference["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    record = json.loads(path.read_text(encoding="utf-8"))
    for part in reference.get("json_pointer", "").split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        record = record[int(part)] if isinstance(record, list) else record[part]
    return record


def test_large_outputs_have_small_projections_and_complete_hash_verified_sources(tmp_path):
    task = {"task_id": "coordinator-a", "mission_id": "m", "role": "coordinator",
            "status": "running", "input_version": 12,
            "objective": "Choose the next research question", "scope": {}}
    large = "raw-result-" * 50_000
    tasks = [{"task_id": f"research-{index}", "mission_id": "m", "role": "researcher",
              "status": "completed", "parent_task_id": task["task_id"],
              "objective": "Test the assigned mechanism", "scope": {"idea_id": "idea-a",
                  "research_node_id": "node-a", "research_revision": 3},
              "output": {"payload": {"summary": large, "observations": [large],
                                     "source_refs": ["run:run-a"]}}} for index in range(5)]
    node = {"node_id": "node-a", "revision": 3, "question": large, "summary": large,
            "execution_status": "completed", "finding_status": "open"}
    context = {"mission": {"mission_id": "m", "objective": "Investigate a mechanism"},
               "task": task, "blackboard_version": 12, "tasks": tasks, "events": [],
               "runs": [], "hypotheses": [], "candidates": [],
               "research_graph": {"cursor": 12, "nodes": [node], "revisions": [node],
                                  "relations": [], "compositions": []}}
    before = deepcopy(context)
    manifest_path = manifest_for(tmp_path, task)
    old_entry = json.loads(manifest_path.read_text())["files"][0]

    compact = compile_prompt_context(context, manifest_path, task)

    assert context == before
    assert len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) < 96_000
    full_ref = compact["context_sources"]["full_context_ref"]
    assert source_record(full_ref) == before
    assert Path(full_ref["path"]) == manifest_path.parent / "context/full.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert old_entry in manifest["files"]
    entry = next(item for item in manifest["files"] if item["path"] == "context/full.json")
    assert entry["source"] == "Task prompt context snapshot"
    assert entry["sha256"] == full_ref["sha256"]
    for item, original in zip(compact["tasks"], tasks):
        assert item["task_id"] == original["task_id"]
        assert (item["role"], item["status"]) == ("researcher", "completed")
        assert item["scope"]["research_node_id"] == "node-a"
        assert item["scope"]["research_revision"] == 3
        assert source_record(item["source_ref"]) == original
    projected_node = compact["research_graph"]["nodes"][0]
    assert (projected_node["node_id"], projected_node["revision"]) == ("node-a", 3)
    assert source_record(projected_node["source_ref"]) == node
    frozen_manifest = manifest_path.read_bytes()
    assert compile_prompt_context(context, manifest_path, task) == compact
    assert manifest_path.read_bytes() == frozen_manifest
    context["tasks"][0]["output"]["payload"]["summary"] = "later result"
    with pytest.raises(ValueError, match="frozen"):
        compile_prompt_context(context, manifest_path, task)
    assert source_record(full_ref) == before


def test_context_compilation_does_not_change_recorded_task_results(tmp_path):
    with RuntimeStore(tmp_path / "state.sqlite3") as store:
        store.create_mission(MissionSpec("m", "Retain original research evidence"))
        store.create_task("m", "researcher", "Investigate", "test")
        child = store.claim_task("m")
        result = {"payload": {"summary": "original " * 30_000,
                              "observations": [{"value": 4, "source": "run:run-a"}]}}
        store.finish_task(child["task_id"], child["lease_token"], result)
        task = store.create_task("m", "coordinator", "Inspect the result", "test")
        before = store.list_tasks("m")
        before_changes = store.db.total_changes
        context = {"mission": store.mission("m")["payload"], "task": task,
                   "tasks": before, "research_graph": store.research_snapshot("m")}
        manifest = manifest_for(tmp_path / "bundle", task)

        compact = compile_prompt_context(context, manifest, task)

        assert store.db.total_changes == before_changes
        assert store.list_tasks("m") == before
        assert store.task(child["task_id"])["output"] == result
    assert source_record(compact["context_sources"]["full_context_ref"])["tasks"] == before


def test_pi_native_provider_keeps_payload_and_records_model_configuration(tmp_path):
    task = {"task_id": "coordinator-resumed", "mission_id": "m", "role": "coordinator",
            "status": "running", "input_version": 891, "scope": {}}
    context = {"task": task, "mission": {"mission_id": "m", "objective": "Continue research"},
        "knowledge": {"index": "k" * 12000}, "communications": {"index": "c" * 7500},
        "tasks": [{"task_id": f"research-{i}", "role": "researcher", "status": "completed",
                   "output": {"payload": {"summary": "s" * 512}}} for i in range(24)],
        "events": [{"event_id": i, "version": i, "kind": "result",
                    "payload": {"summary": "e" * 384}} for i in range(24)],
        "research_graph": {"mission_id": "m", "root_node_id": "node-0",
            "nodes": [{"node_id": f"node-{i}", "revision": 1,
                       "question": "q" * 384, "summary": "s" * 512} for i in range(20)],
            "relations": [], "compositions": []}}
    manifest = manifest_for(tmp_path / "bundle", task)
    compact = compile_prompt_context(context, manifest, task)
    source = compact["context_sources"]["full_context_ref"]
    assert source_record(source) == context
    assert compact["task"] == task
    assert compact["mission"] == context["mission"]
    assert compact["research_graph"]["root_node_id"] == "node-0"
    # The guard must leave Pi's system/history and provider payload untouched.
    messages = [{"role": "system", "content": "system/tool schemas " + "t" * 28000},
        {"role": "user", "content": [{"type": "text", "text":
            "role instructions " + "i" * 18500 + json.dumps(compact, ensure_ascii=False)}]}]
    result, receipt = run_guard(tmp_path, '''
const model = {contextWindow:256000, maxTokens:128000, api:'openai-responses'};
const payload = {input:request.messages, max_output_tokens:48000};
const before = JSON.stringify(payload);
const returned = hooks.before_provider_request({payload}, {model});
console.log(JSON.stringify({payload, before, returned}));
''', limit=256000, payload={"messages": messages, "source": source})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["payload"] == {"input": messages, "max_output_tokens": 48000}
    assert response["before"] == json.dumps(response["payload"], ensure_ascii=False, separators=(",", ":"))
    assert response.get("returned") is None
    checks = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert len(checks) == 1
    assert checks[0] == {
        "stage": "provider_request", "management": "pi-native",
        "limit_tokens": 256000, "model_context_window": 256000,
        "model_max_tokens": 128000, "model_configuration_valid": True,
        "checked_at": checks[0]["checked_at"]}


@pytest.mark.parametrize("role", ["researcher", "analyzer", "critic", "curator"])
def test_role_context_preserves_assigned_graph_and_responsibility_fields(tmp_path, role):
    mandate = {"question": "Which observation distinguishes these mechanisms?",
               "scope": "Use the assigned evidence", "success_condition": "Identify the next check"}
    task = {"task_id": f"{role}-a", "mission_id": "m", "input_version": 7,
            "role": role, "status": "running", "objective": "Investigate the assigned question",
            "scope": {"research_node_id": "node-a", "research_revision": 3,
                      "research_mandate": mandate}}
    graph = {"cursor": 7, "assigned_node": {"node_id": "node-a", "revision": 3,
        "question": "Why did recovery change?", "scope": "Investigate the body response",
        "summary": "Current evidence does not resolve the mechanism"},
        "relations": [{"relation_id": "edge-a", "source": {"node_id": "source-a", "revision": 2},
                       "target": {"node_id": "node-a", "revision": 3}, "kind": "composed_from"}],
        "composition_sources": [{"input": {"node_id": "source-a", "revision": 2},
                                  "tasks": [{"task_id": "source-task", "role": "researcher",
                                             "status": "completed", "response": "a recorded claim"}]}],
        "frozen_source_tasks": [{"task_id": "source-task", "role": "researcher", "status": "completed",
                                 "research_node_id": "source-a", "research_revision": 2}],
        "composition_lineage": [{"node_id": "node-a", "inputs": [{"node_id": "source-a", "revision": 2}],
                                 "frozen_source_run_ids": ["run-a"]}]}
    manifest = manifest_for(tmp_path, task)
    context = {"mission": {"mission_id": "m", "objective": "Understand recovery",
                           "known_evidence": ["reference/body-observations.json"]},
               "task": task, "research_mandate": mandate, "research_graph": graph,
               "evidence_manifest": str(manifest), "external_readonly_manifest": "/research/manifest.json",
               "curation": {"document_index": "/evidence/wiki/curation_sources.json"},
               "tasks": [{"task_id": "completed-child", "role": "researcher", "status": "completed",
                          "output": {"payload": {"summary": "full result " * 50_000}}}],
               "events": [{"event_id": "event-a", "version": 6, "kind": "result",
                           "payload": {"observations": "full event " * 30_000}}],
               "runs": [{"run_id": "run-a", "status": "completed", "evidence": {"log": "raw " * 30_000}}]}
    before = deepcopy(context)

    compact = compile_prompt_context(context, manifest, task)

    for key in ("assigned_node", "relations", "frozen_source_tasks", "composition_lineage"):
        assert compact["research_graph"][key] == graph[key]
    projected = compact["research_graph"]["composition_sources"][0]
    assert projected["input"] == graph["composition_sources"][0]["input"]
    assert source_record(projected["source_ref"]) == graph["composition_sources"][0]
    for key in ("task", "research_mandate", "curation", "evidence_manifest", "external_readonly_manifest", "mission"):
        assert compact[key] == context[key]
    assert context == before
    assert source_record(compact["context_sources"]["full_context_ref"]) == before
    assert source_record(compact["tasks"][0]["source_ref"]) == context["tasks"][0]
    assert source_record(compact["events"][0]["source_ref"]) == context["events"][0]
    assert source_record(compact["runs"][0]["source_ref"]) == context["runs"][0]


def test_source_indexes_are_bounded_before_context_is_full(tmp_path):
    task = {"task_id": "task-a", "mission_id": "m", "input_version": 9,
            "role": "researcher", "status": "running", "scope": {}}
    tasks = [{"task_id": f"task-{i}", "role": "researcher",
              "status": "running" if i == 0 else "completed",
              "output": {"payload": {"summary": f"result-{i}"}}} for i in range(20)]
    files = [{"path": f"/published/{i}.md", "author_role": "human" if i == 0 else "researcher"}
             for i in range(20)]
    wiki = {"path": "knowledge/entry.md", "title": "Existing idea", "status": "draft",
            "sha256": "a" * 64, "excerpt": "Do not inject the whole page",
            "sources": ["Long historical literature and claims"]}
    context = {"task": task, "mission": {"mission_id": "m"}, "tasks": tasks,
        "communications": {"workspace": "/own/workspace", "inbox_manifest": "/published/manifest.json",
                           "available_files": files},
        "knowledge": {"results": [wiki], "status": "ok", "entry_points": {"index": "knowledge/index.md"}},
        "events": [{"event_id": i, "version": i, "kind": "record", "payload": {}} for i in range(10)],
        "research_graph": {"root_node_id": "node-0", "nodes": [
            {"node_id": f"node-{i}", "revision": i, "question": "An unresolved question"} for i in range(12)]}}
    before = deepcopy(context)
    compact = compile_prompt_context(context, manifest_for(tmp_path, task), task, max_inline_bytes=1_000_000)
    assert context == before
    assert len(compact["tasks"]) == 7
    assert compact["tasks"][0]["task_id"] == "task-0"  # older active work is still visible
    assert len(compact["events"]) == 4
    assert source_record(compact["events"][0]["source_ref"]) == context["events"][6]
    assert compact["context_sources"]["task_count"] == 20
    assert source_record(compact["context_sources"]["tasks_ref"]) == tasks
    assert len(compact["research_graph"]["nodes"]) == 6
    assert compact["research_graph"]["nodes_count"] == 12
    assert source_record(compact["research_graph"]["nodes_ref"]) == context["research_graph"]["nodes"]
    assert "excerpt" not in compact["knowledge"]["results"][0]
    assert "sources" not in compact["knowledge"]["results"][0]
    assert source_record(compact["knowledge"]["source_ref"])["results"][0] == wiki
    assert len(compact["communications"]["available_files"]) == 5
    assert compact["communications"]["available_files"][0] == files[0]
    assert compact["communications"]["workspace"] == "/own/workspace"
    assert source_record(compact["communications"]["available_files_ref"]) == files


@pytest.mark.parametrize("role", ["researcher", "analyzer", "critic", "curator"])
def test_configured_cap_compiles_each_collaborator_new_prompt(tmp_path, role):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["agent_context_limit_tokens"] = 64_000
    path.write_text(json.dumps(config))
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "Investigate recovery", known_evidence=("reference/observations.json",)))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", role, "Inspect the assigned evidence", "test")
        task = store.claim_task("m")
        manifest = manifest_for(tmp_path / "bundle", task)
        prompt = runtime._prompt(role, store.mission("m"), task, store, evidence_manifest=manifest)
    envelope = json.loads(prompt.split("\n", 1)[1])
    context = envelope["context"]
    source = source_record(context["context_sources"]["full_context_ref"])
    assert context["task"] == task
    assert context["mission"]["known_evidence"] == ["reference/observations.json"]
    assert context["evidence_manifest"] == str(manifest)
    assert envelope["role_responsibility"]
    assert envelope["response_contract"]
    for key in ("task", "mission", "research_mandate", "evidence_manifest"):
        assert context[key] == source[key]
    if role == "curator":
        assert context["curation"]["document_index"] == source["curation"]["document_index"]


def node_command():
    node = os.environ.get("RSI_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for the provider context budget test")
    version = subprocess.check_output([node, "--version"], text=True)
    if int(version.strip().lstrip("v").split(".")[0]) < 22:
        pytest.skip("Pi extension requires Node 22")
    return node


def test_provider_hook_does_not_estimate_serialized_bytes_or_mutate_output_budget(tmp_path):
    text = "\u7814\u7a76\U0001f9ea"
    small = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
    large = deepcopy(small)
    large["messages"].append({"role": "assistant", "content": [{"type": "toolCall",
        "id": "tool-a", "name": "rsi_message", "arguments": {"text": text * 5000}}]})
    for payload in (small, large):
        result, receipt = run_guard(tmp_path / ("small" if payload is small else "large"), '''
const body = {input:request.messages, max_output_tokens:48000};
const before = JSON.stringify(body);
const returned = hooks.before_provider_request({payload:body},
  {model:{contextWindow:64000,maxTokens:32768,api:'openai-responses'}});
console.log(JSON.stringify({body,before,returned}));
''', limit=64_000, payload=payload)
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response["body"] == {"input": payload["messages"], "max_output_tokens": 48000}
        assert response.get("returned") is None
        assert "json_bytes" not in json.loads(receipt.read_text())


@pytest.mark.parametrize("role", ["curator", "researcher"])
@pytest.mark.parametrize("large_arguments", [False, True])
def test_pi_native_provider_preserves_large_history_and_role_constraints(tmp_path, role, large_arguments):
    source_file = tmp_path / "evidence/context/full.json"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("{\"recorded\":true}", encoding="utf-8")
    messages = [
        {"role": "system", "content": "Fixed tools and system constraints"},
        {"role": "user", "content": [{"type": "text", "text": f"{role}: preserve Mission and source constraints"}]},
    ]
    for index in range(18):
        messages.extend([
            {"role": "assistant", "content": [{"type": "text", "text": "reading recorded sources"},
                {"type": "toolCall", "id": f"read-{index}", "name": "read", "arguments": {
                    "path": "evidence/context/full.json", "note": "large prior arguments " * (3000 if large_arguments else 1)}}]},
            {"role": "toolResult", "toolCallId": f"read-{index}", "toolName": "read", "content": [
                {"type": "text", "text": "\u539f\u59cb\u8bc1\u636e\U0001f9ea" * 1500}]}])
    messages.extend([
        {"role": "assistant", "content": [{"type": "toolCall", "id": "recent-call", "name": "read",
            "arguments": {"path": "evidence/current.json", "limit": 20}}]},
        {"role": "toolResult", "toolCallId": "recent-call", "toolName": "read",
         "content": [{"type": "text", "text": "The current recorded result"}]}])
    result, receipt = run_guard(tmp_path, '''
const model = {contextWindow:256000, maxTokens:128000, api:'openai-responses'};
const payload = {model:'gpt-6.1-sol',input:request.messages};
const before = JSON.stringify(payload);
const body = hooks.before_provider_request({payload}, {model});
const readResult = await hooks.tool_call({toolName:'read',input:{path:'evidence/context/full.json'}});
const editResult = await hooks.tool_call({toolName:'edit',input:{path:'evidence/context/full.json'}});
console.log(JSON.stringify({messages:request.messages, before, body,
  read_allowed:readResult === undefined, edit_blocked:editResult?.block === true,
  payload}));
''', limit=256000, payload={"messages": messages})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["messages"] == messages
    assert response["payload"] == {"model": "gpt-6.1-sol", "input": messages}
    assert response["before"] == json.dumps(response["payload"], ensure_ascii=False, separators=(",", ":"))
    assert response.get("body") is None
    assert response["read_allowed"] is True
    assert response["edit_blocked"] is True
    checks = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert checks == [{
        "stage": "provider_request", "management": "pi-native",
        "limit_tokens": 256000, "model_context_window": 256000,
        "model_max_tokens": 128000, "model_configuration_valid": True,
        "checked_at": checks[0]["checked_at"]}]


def test_dynamic_context_does_not_change_small_conversation(tmp_path):
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": [{"type": "text", "text": "current constraints"}]}]
    result, _receipt = run_guard(tmp_path, '''
const payload = {input:request.messages, max_output_tokens:32000};
const before = JSON.stringify(payload);
const returned = hooks.before_provider_request({payload},
  {model:{contextWindow:256000,maxTokens:128000}});
console.log(JSON.stringify({payload,before,returned}));
''', limit=256000, payload={"messages": messages})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["payload"] == {"input": messages, "max_output_tokens": 32000}
    assert response["before"] == json.dumps(response["payload"], separators=(",", ":"))
    assert response.get("returned") is None


@pytest.mark.parametrize("large_content", [False, True])
def test_tool_result_archives_duplicate_details_without_changing_pi_payload(tmp_path, large_content):
    messages = [{"role": "system", "content": "System constraints " * 1200},
        {"role": "user", "content": [{"type": "text", "text": "Current task constraints " * 900}]},
        {"role": "assistant", "content": [{"type": "toolCall", "id": "read-a", "name": "read",
            "arguments": {"path": "evidence/a.json"}}, {"type": "toolCall", "id": "read-b", "name": "read",
            "arguments": {"path": "evidence/b.json"}}]}]
    for call in ("read-a", "read-b"):
        messages.append({"role": "toolResult", "toolCallId": call, "toolName": "web_search", "isError": False,
            "content": [{"type": "text", "text": "current result " * (4000 if large_content else 150)}],
            "details": {"truncated_content": "original duplicate body " * 2500}})
    result, _receipt = run_guard(tmp_path, '''
const projections = request.messages.slice(-2).map(message => hooks.tool_result(message));
const payload = {input:request.messages};
const before = JSON.stringify(payload);
const body = hooks.before_provider_request({payload},
  {model:{contextWindow:256000,maxTokens:128000,api:'openai-responses'}});
console.log(JSON.stringify({messages:request.messages,projections,payload,before,body}));
''', limit=256000, payload={"messages": messages})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["messages"] == messages
    assert response["payload"] == {"input": messages}
    assert response["before"] == json.dumps(response["payload"], separators=(",", ":"))
    assert response.get("body") is None
    for index, projection in enumerate(response["projections"]):
        original = messages[index + 3]
        assert projection["details"]["source_ref"]
        assert source_record(projection["details"]["source_ref"]) == original["details"]
        if large_content:
            archived = source_record(json.loads(projection["content"][0]["text"])["source_ref"])
            assert archived["content"] == original["content"]
            assert archived["details"] == original["details"]
        else:
            assert projection["content"] == original["content"]


def test_custom_tool_result_archives_large_details_even_when_content_is_short(tmp_path):
    event = {"toolName": "web_search", "toolCallId": "search-a", "content": [{"type": "text", "text": "short excerpt"}],
             "details": {"truncation": {"raw": "whole read body " * 10000}}}
    result, _receipt = run_guard(tmp_path, '''
const result = hooks.tool_result(request);
console.log(JSON.stringify(result));
''', limit=256000, payload=event)
    assert result.returncode == 0, result.stderr
    projected = json.loads(result.stdout)
    assert projected["content"] == event["content"]
    assert source_record(projected["details"]["source_ref"]) == event["details"]
    assert len(json.dumps(projected, ensure_ascii=False).encode()) < 6000


def test_responses_provider_keeps_explicit_output_budget(tmp_path):
    result, receipt = run_guard(tmp_path, '''
const payload = {input:[],max_output_tokens:48000};
const body = hooks.before_provider_request({payload},
  {model:{contextWindow:256000,maxTokens:128000,api:'openai-responses'}});
console.log(JSON.stringify({payload,body}));
''', limit=256000)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["payload"] == {"input": [], "max_output_tokens": 48000}
    assert response.get("body") is None
    check = json.loads(receipt.read_text().splitlines()[-1])
    assert check["management"] == "pi-native"
    assert "json_bytes" not in check and "effective_max_output_tokens" not in check


def test_provider_hook_requires_model_context_within_cap(tmp_path):
    result, receipt = run_guard(tmp_path, '''
const payload = {input:[], max_output_tokens:32000};
const body = hooks.before_provider_request({payload},
  {model:{contextWindow:64000,maxTokens:32000,api:'openai-responses'}});
console.log(JSON.stringify({payload,body}));
''', limit=64_000)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout).get("body") is None
    check = json.loads(receipt.read_text())
    assert check["model_context_window"] == 64_000
    assert check["limit_tokens"] == 64_000

    result, _ = run_guard(tmp_path / "too-large", '''
hooks.before_provider_request({payload:{input:[]}},
  {model:{contextWindow:64001,maxTokens:32000,api:'openai-responses'}});
console.log('provider request continued');
''', limit=64_000)
    assert result.returncode == 78
    assert "contextWindow=64001" in result.stderr
    assert "configured cap 64000" in result.stderr
    assert "provider request continued" not in result.stdout


def run_guard(tmp_path, script, *, limit=32_768, payload=None, blocked_receipt=False, env_overrides=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    receipt = workspace / "context-budget.jsonl"
    if blocked_receipt:
        receipt.mkdir()
    source = tmp_path / "guard-check.mjs"
    source.write_text('''import guard from GUARD;
import fs from 'node:fs';
const hooks = {};
const tools = {};
guard({on(name, fn) { hooks[name] = fn; }, registerTool(tool) { tools[tool.name] = tool; }});
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
'''.replace("GUARD", json.dumps(GUARD.as_uri())) + script, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("RSI_")}
    env.update(RSI_AGENT_ROOT=str(tmp_path), RSI_AGENT_WORKSPACE=str(workspace),
               RSI_AGENT_ROLE="coordinator", RSI_AGENT_CONTEXT_LIMIT_TOKENS=str(limit),
               RSI_CONTEXT_BUDGET_RECEIPT=str(receipt))
    env.update(env_overrides or {})
    result = subprocess.run([node_command(), "--experimental-strip-types", str(source)],
        input=json.dumps(payload or {}, ensure_ascii=False), env=env, capture_output=True,
        text=True, timeout=10)
    return result, receipt


def test_provider_hook_allows_large_payload_and_leaves_native_budget_to_pi(tmp_path):
    payload = {"messages": [{"role": "user", "content": "x" * 20_000}]}
    result, receipt = run_guard(tmp_path, '''await hooks.before_provider_request(
    {payload: request}, {model: {maxTokens: 16384, contextWindow: 32768}});
console.log('provider request continued');
''', payload=payload)
    assert result.returncode == 0, result.stderr
    assert "provider request continued" in result.stdout
    check = json.loads(receipt.read_text())
    assert check["management"] == "pi-native"
    assert check["limit_tokens"] == 32_768
    assert check["model_context_window"] == 32_768


def test_provider_hook_records_model_context_window_for_allowed_requests(tmp_path):
    result, receipt = run_guard(tmp_path, '''await hooks.before_provider_request(
    {payload: request}, {model: {maxTokens: 16384, contextWindow: 32768}});
console.log('provider request continued');
''', limit=64_000, payload={"messages": [{"role": "user", "content": "brief prompt"}]})
    assert result.returncode == 0, result.stderr
    assert "provider request continued" in result.stdout
    check = json.loads(receipt.read_text())
    assert check["management"] == "pi-native"
    assert check["model_context_window"] == 32_768
    assert check["limit_tokens"] == 64_000


def test_provider_hook_exits_when_budget_evidence_cannot_be_written(tmp_path):
    result, _ = run_guard(tmp_path, '''await hooks.before_provider_request(
    {payload: request}, {model: {contextWindow:32768,maxTokens:16384}});
console.log('provider request continued');
''', payload={"messages": []}, blocked_receipt=True)
    assert result.returncode == 78
    assert "provider request continued" not in result.stdout
    assert "could not record Pi-native context evidence" in result.stderr


def test_large_tool_results_keep_attribution_and_hash_verified_read_only_source(tmp_path):
    content = [{"type": "text", "text": "\u7814\u7a76\U0001f9ea" * 5000}]
    payload = {"toolName": "web_search", "toolCallId": "search-a", "content": content,
               "details": {"source": "https://example.org/paper", "status": "unverified"}}
    result, _ = run_guard(tmp_path, '''const projected = await hooks.tool_result(request);
const summary = JSON.parse(projected.content[0].text);
const repeated = await hooks.tool_result(request);
const read = await hooks.tool_call({toolName:'read', input:{path:summary.source_ref.path}});
const edit = await hooks.tool_call({toolName:'edit', input:{path:summary.source_ref.path}});
console.log(JSON.stringify({summary, repeated:JSON.parse(repeated.content[0].text),
  read_allowed: !read?.block, edit_blocked: !!edit?.block,
  archive_count:fs.readdirSync(process.env.RSI_AGENT_WORKSPACE + '/.context-sources').length}));
''', payload=payload)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    summary = response["summary"]
    full = source_record(summary["source_ref"])
    assert full["tool_name"] == "web_search"
    assert full["tool_call_id"] == "search-a"
    assert full["content"] == content
    assert full["details"] == payload["details"]
    assert summary["result_bytes"] == Path(summary["source_ref"]["path"]).stat().st_size
    assert response["read_allowed"] is True
    assert response["edit_blocked"] is True
    assert response["repeated"]["source_ref"] == summary["source_ref"]
    assert response["archive_count"] == 1


def fake_pi(tmp_path, *, record_budget=True, budget_stage="provider_request"):
    executable = tmp_path / "fake-pi"
    executable.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
sys.stdin.read()
if RECORD_BUDGET and os.environ.get("RSI_CONTEXT_BUDGET_RECEIPT"):
    pathlib.Path(os.environ["RSI_CONTEXT_BUDGET_RECEIPT"]).write_text(json.dumps({
        "limit_tokens":int(os.environ["RSI_AGENT_CONTEXT_LIMIT_TOKENS"]),
        "model_context_window":int(os.environ["RSI_AGENT_CONTEXT_LIMIT_TOKENS"]),
        "model_max_tokens":16384,"model_configuration_valid":True,
        "management":"pi-native","stage":BUDGET_STAGE}) + "\\n")
payload={"summary":"completed", "limit":os.environ.get("RSI_AGENT_CONTEXT_LIMIT_TOKENS")}
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps(payload)}]}}))
'''.replace("RECORD_BUDGET", str(record_budget)).replace("BUDGET_STAGE", repr(budget_stage)), encoding="utf-8")
    executable.chmod(0o755)
    return executable


def large_output_pi(tmp_path):
    executable = tmp_path / "large-output-pi"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
sys.stdin.read()
if os.environ.get("RSI_CONTEXT_BUDGET_RECEIPT"):
    pathlib.Path(os.environ["RSI_CONTEXT_BUDGET_RECEIPT"]).write_text(json.dumps({
        "stage":"provider_request", "management":"pi-native",
        "limit_tokens":int(os.environ["RSI_AGENT_CONTEXT_LIMIT_TOKENS"]),
        "model_context_window":int(os.environ["RSI_AGENT_CONTEXT_LIMIT_TOKENS"]),
        "model_configuration_valid":True}) + "\\n")
sys.stderr.write("diagnostic\\n" + "x" * 17_000_000)
sys.stderr.flush()
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
    "content":[{"type":"text","text":"{\\"summary\\":\\"completed\\"}"}]}}), flush=True)
''', encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_long_pi_event_and_stderr_stream_is_not_killed_at_16mb(tmp_path):
    options = dict(executable=str(large_output_pi(tmp_path)), role="coordinator", prompt="brief prompt",
                   cwd=tmp_path, output_dir=tmp_path / "out", timeout_seconds=5, guard=GUARD)
    result = run_agent(**options, context_limit_tokens=32_768)
    assert result["payload"] == {"summary": "completed"}
    assert (tmp_path / "out/stderr.log").stat().st_size > 16_000_000


def test_agent_does_not_reject_prompt_by_utf8_bytes_before_pi_launch(tmp_path):
    result = run_agent(executable=str(fake_pi(tmp_path)), role="coordinator", prompt="x" * 30_000,
                       cwd=tmp_path, output_dir=tmp_path / "out", timeout_seconds=5,
                       context_limit_tokens=32_768, guard=GUARD)
    assert result["payload"]["summary"] == "completed"
    assert result["context_budget"]["management"] == "pi-native"


def test_completed_receipt_and_frozen_prompt_reuse_preserves_previous_budget(tmp_path):
    options = dict(executable=str(fake_pi(tmp_path)), role="coordinator", prompt="frozen prompt",
                   cwd=tmp_path, output_dir=tmp_path / "out", timeout_seconds=5, guard=GUARD)
    result = run_agent(**options, context_limit_tokens=32_768)
    assert result["payload"]["limit"] == "32768"
    assert result["context_budget"]["limit_tokens"] == 32_768
    receipt, prompt = tmp_path / "out/agent_result.json", tmp_path / "out/prompt.txt"
    frozen_receipt, frozen_prompt = receipt.read_bytes(), prompt.read_bytes()

    reused = run_agent(**{**options, "executable": "must-not-launch"}, context_limit_tokens=1)

    assert reused == result
    assert receipt.read_bytes() == frozen_receipt
    assert prompt.read_bytes() == frozen_prompt


@pytest.mark.parametrize("cap", [None, 32_768])
def test_successful_agent_without_guard_evidence_is_accepted_only_without_cap(tmp_path, cap):
    options = dict(executable=str(fake_pi(tmp_path, record_budget=False)), role="coordinator",
                   prompt="brief prompt", cwd=tmp_path, output_dir=tmp_path / "out",
                   timeout_seconds=5, guard=GUARD, context_limit_tokens=cap)
    if cap is None:
        assert run_agent(**options)["payload"]["summary"] == "completed"
    else:
        with pytest.raises(ValueError, match="context guard.*provider-check evidence"):
            run_agent(**options)
        assert not (tmp_path / "out/agent_result.json").exists()


@pytest.mark.parametrize("stage", ["context_view", "compaction", None])
def test_context_projection_or_old_checks_cannot_replace_provider_check(tmp_path, stage):
    with pytest.raises(ValueError, match="context guard.*provider-check evidence"):
        run_agent(executable=str(fake_pi(tmp_path, budget_stage=stage)), role="coordinator",
            prompt="brief prompt", cwd=tmp_path, output_dir=tmp_path / "out",
            timeout_seconds=5, guard=GUARD, context_limit_tokens=32768)
    assert not (tmp_path / "out/agent_result.json").exists()


def test_runtime_passes_configured_cap_and_archives_only_new_prompt(tmp_path, monkeypatch):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["agent_context_limit_tokens"] = 64_000
    path.write_text(json.dumps(config))
    assert load_research_config(root, config_dir=tmp_path / "empty")["agent_context_limit_tokens"] == 64_000
    runtime = RuntimeCoordinator(root)
    runtime.start(MissionSpec("m", "Make the next research decision"))
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task("m", "coordinator", "wait for evidence", "test")
    calls = []
    original = RuntimeCoordinator._handle_coordinator

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("after the agent receipt, before task completion")

    monkeypatch.setattr(RuntimeCoordinator, "_handle_coordinator", crash)
    with pytest.raises(KeyboardInterrupt):
        runtime.tick(agent_runner=lambda **kwargs: (calls.append(kwargs) or decision(action="wait")))
    assert calls[0]["session_id"].startswith("agent-session-")
    assert calls[0]["session_dir"] == runtime.runtime_root / "pi-sessions"
    monkeypatch.setattr(RuntimeCoordinator, "_handle_coordinator", original)
    assert calls[0]["context_limit_tokens"] == 64_000
    frozen = runtime.runtime_root / "tasks" / task["task_id"] / "input.json"
    saved_input = frozen.read_bytes()
    context = json.loads(json.loads(saved_input)["prompt"].split("\n", 1)[1])["context"]
    source = Path(context["context_sources"]["full_context_ref"]["path"])
    saved_source = source.read_bytes()
    with RuntimeStore(runtime.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE status='running'")
        store.db.commit()
    monkeypatch.setattr("rsi_system.core.runtime.compile_prompt_context",
                        lambda *_args, **_kwargs: pytest.fail("frozen input must not be recompiled"))
    result = RuntimeCoordinator(root).tick(agent_runner=lambda **_: pytest.fail("receipt must be reused"))
    assert result["recovered"] and result["status"] == "completed"
    assert frozen.read_bytes() == saved_input
    assert source.read_bytes() == saved_source


@pytest.mark.parametrize("limit", [0, True, -1, "64000"])
def test_context_cap_config_rejects_values_that_cannot_enforce_the_budget(tmp_path, limit):
    root = project(tmp_path)
    path = root / "deployment/rsi/research.json"
    config = json.loads(path.read_text())
    config["agent_context_limit_tokens"] = limit
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="agent_context_limit_tokens"):
        load_research_config(root, config_dir=tmp_path / "empty")
