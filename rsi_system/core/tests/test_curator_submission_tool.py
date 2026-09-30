"""Curator submissions emit accepted receipts before stopping tools and providers."""
import json
import os
from pathlib import Path
import socketserver
import subprocess

import pytest

from .test_runtime_context import GUARD, node_command
from .test_runtime_tool_transport import EXTENSION, fake_server


SDK = Path.home() / ".local/lib/node_modules/@earendil-works/pi-coding-agent"
BOOTSTRAP = '''
import fs from 'node:fs';
import Module from 'node:module';
const load = Module._load;
Module._load = function(id, ...args) {
  if (id === 'typebox') return process.env.RSI_TEST_TYPEBOX
    ? load.call(this, process.env.RSI_TEST_TYPEBOX)
    : {Type:new Proxy({}, {get:() => () => ({})})};
  return load.call(this,id,...args);
};
const {default:extension} = await import(process.env.RSI_EXTENSION_URL);
const {default:guard} = await import(process.env.RSI_GUARD_URL);
const tools = {};
const hooks = {};
const pi = {registerTool(tool) {tools[tool.name]=tool;},on(name,fn) {(hooks[name] ??= []).push(fn);}};
guard(pi); extension(pi);
const runHooks = async (name,event,ctx={}) => {
  let result;
  for (const handler of hooks[name] ?? []) {
    const value = await handler(event,ctx);
    if (value) {
      result = {...result,...value};
      if(name === 'tool_result') Object.assign(event,value);
      if(value.block) break;
    }
  }
  return result;
};
'''


def run_extension(tmp_path, script, *, role="curator", socket_path=None, context_limit=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = tmp_path / "submission.mjs"
    source.write_text(BOOTSTRAP + script, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("RSI_")}
    env.update(RSI_AGENT_ROOT=str(tmp_path), RSI_AGENT_WORKSPACE=str(workspace), RSI_AGENT_ROLE=role,
        RSI_EXTENSION_URL=EXTENSION.as_uri(), RSI_GUARD_URL=GUARD.as_uri())
    if socket_path:
        env["RSI_TOOL_SOCKET"] = str(socket_path)
    if context_limit is not None:
        env["RSI_AGENT_CONTEXT_LIMIT_TOKENS"] = str(context_limit)
    typebox = SDK / "node_modules/typebox/build/index.mjs"
    if typebox.is_file():
        env["RSI_TEST_TYPEBOX"] = str(typebox)
    return subprocess.run([node_command(), "--experimental-strip-types", str(source)], env=env,
        capture_output=True, text=True, timeout=15)


def decode_result(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def accepted_receipt():
    return {"ok": True, "status": "accepted", "payload": {"decision": "publish", "summary": "submitted"},
        "paths": ["wiki/findings/test.md"], "files": [{"path": "wiki/findings/test.md", "sha256": "a" * 64, "bytes": 12}],
        "source_event_ids": ["event-a"], "submission_id": "b" * 64,
        "submission_manifest": "/assigned/curator-submission/manifest.json", "submission_sha256": "c" * 64,
        "publication": {"status": "pending"}}


def bridge_handler(responder, seen):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline())
            seen.append(request)
            self.wfile.write((json.dumps(responder(request)) + "\n").encode())
    return Handler


@pytest.mark.parametrize("role", ["curator", "coordinator", "researcher", "coder", "critic", "analyzer"])
def test_submit_tool_is_visible_only_to_curator_and_requires_bridge(tmp_path, role):
    result = run_extension(tmp_path, '''
const call = await runHooks('tool_call',{toolName:'rsi_wiki_submit',input:{paths:[],summary:'No new findings'}});
console.log(JSON.stringify({registered:!!tools.rsi_wiki_submit,call}));
''', role=role)
    response = decode_result(result)
    assert response["registered"] is (role == "curator")
    assert response["call"]["block"] is True
    reason = response["call"]["reason"]
    assert ("bridge" in reason) if role == "curator" else ("Curator" in reason)


def test_accepted_receipt_terminates_after_result_emission_and_skips_inbox(tmp_path):
    receipt = accepted_receipt()
    seen = []
    with fake_server(bridge_handler(lambda request: receipt, seen)) as socket:
        result = run_extension(tmp_path, '''
let aborted = 0;
const ctx = {abort(){aborted++;}};
const before = await runHooks('tool_call',{toolName:'rsi_wiki_submit',input:{paths:['wiki/findings/test.md'],summary:'submitted'}});
const answer = await tools.rsi_wiki_submit.execute('submit',{paths:['wiki/findings/test.md'],summary:'submitted'});
const beforeResult = aborted;
await runHooks('tool_result',{toolName:'rsi_wiki_submit',toolCallId:'submit',...answer},ctx);
const beforeEmission = aborted;
await runHooks('message_end',{message:{role:'toolResult',toolName:'rsi_wiki_submit',toolCallId:'submit',...answer}},ctx);
const read = await runHooks('tool_call',{toolName:'read',input:{path:'.'}});
const write = await runHooks('tool_call',{toolName:'write',input:{path:'note.md'}});
const duplicate = await tools.rsi_wiki_submit.execute('again',{paths:[],summary:'again',decision:'no_change'});
console.log(JSON.stringify({before,answer,beforeResult,beforeEmission,aborted,read,write,duplicate}));
''', socket_path=socket)
    response = decode_result(result)
    assert "before" not in response
    assert response["answer"]["details"] == receipt
    assert json.loads(response["answer"]["content"][0]["text"]) == receipt
    assert response["answer"]["terminate"] is True
    assert response["beforeResult"] == response["beforeEmission"] == 0
    assert response["aborted"] == 1
    assert response["read"]["block"] is response["write"]["block"] is True
    assert response["read"]["terminate"] is response["write"]["terminate"] is True
    assert json.loads(response["duplicate"]["content"][0]["text"])["ok"] is False
    assert len(seen) == 1
    assert seen[0]["args"]["decision"] == "publish"


def test_invalid_submission_remains_correctable_and_no_change_is_explicit(tmp_path):
    seen = []
    def respond(request):
        if request["args"]["decision"] == "publish":
            return {"ok": False, "error": {"code": "validation", "message": "missing source"}}
        return {**accepted_receipt(), "payload": {"decision": "no_change", "summary": request["args"]["summary"]},
                "paths": [], "files": [], "publication": {"status": "no_change"}}
    with fake_server(bridge_handler(respond, seen)) as socket:
        result = run_extension(tmp_path, '''
const first = await tools.rsi_wiki_submit.execute('invalid',{paths:['wiki/findings/missing.md'],summary:'draft'});
const correction = await runHooks('tool_call',{toolName:'write',input:{path:process.env.RSI_AGENT_WORKSPACE+'/wiki/findings/fixed.md'}});
const second = await tools.rsi_wiki_submit.execute('no-change',{paths:[],summary:'Evidence is unchanged',decision:'no_change'});
console.log(JSON.stringify({first,correction,second}));
''', socket_path=socket)
    response = decode_result(result)
    assert response["first"]["details"]["error"]["code"] == "validation"
    assert "terminate" not in response["first"]
    assert "correction" not in response
    assert response["second"]["terminate"] is True
    assert response["second"]["details"]["payload"]["decision"] == "no_change"
    assert seen[1]["args"]["paths"] == []


@pytest.mark.parametrize("context_limit", [None, 256000])
def test_provider_barrier_runs_only_after_accepted_result_was_emitted(tmp_path, context_limit):
    seen = []
    with fake_server(bridge_handler(lambda request: accepted_receipt(), seen)) as socket:
        result = run_extension(tmp_path, '''
const answer = await tools.rsi_wiki_submit.execute('submit',{paths:['wiki/findings/test.md'],summary:'submitted'});
await runHooks('tool_result',{toolName:'rsi_wiki_submit',toolCallId:'submit',...answer},{abort(){}});
await runHooks('before_provider_request',{payload:{input:[]}}, {model:{contextWindow:256000,maxTokens:128000}});
console.log(JSON.stringify({stage:'receipt'}));
await runHooks('message_end',{message:{role:'toolResult',toolName:'rsi_wiki_submit',toolCallId:'submit',...answer}},{abort(){}});
await runHooks('before_provider_request',{payload:{input:[]}}, {model:{contextWindow:256000,maxTokens:128000}});
console.log(JSON.stringify({stage:'provider continued'}));
''', socket_path=socket, context_limit=context_limit)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"stage": "receipt"}
    assert "no further provider request" in result.stderr


@pytest.mark.parametrize("mixed_batch", [False, True])
def test_installed_sdk_loop_persists_submit_result_and_stops_the_current_batch(tmp_path, mixed_batch):
    core = SDK / "node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js"
    wrapper = SDK / "dist/core/extensions/wrapper.js"
    if not core.is_file() or not wrapper.is_file():
        pytest.skip("Installed Pi SDK required for real agent loop submission regression")
    seen = []
    with fake_server(bridge_handler(lambda request: accepted_receipt() if request["tool"] == "rsi_wiki_submit"
                                   else {"ok": True, "messages": [], "next_version": 0}, seen)) as socket:
        script = '''
const {runAgentLoop} = await import(CORE);
const {wrapRegisteredTool} = await import(WRAPPER);
tools.read = {name:'read',label:'read',description:'fixture read',parameters:{type:'object',properties:{path:{type:'string'}}},
  async execute(){return {content:[{type:'text',text:'read source'}],details:{}};}};
tools.write = {name:'write',label:'write',description:'must never execute',parameters:{type:'object',properties:{path:{type:'string'}}},
  async execute(){fs.writeFileSync(process.env.RSI_AGENT_WORKSPACE+'/unexpected-write','bad');return {content:[],details:{}};}};
const controller = new AbortController();
const ctx = {abort(){controller.abort();}};
const runner = {getActiveTools(){return Object.keys(tools);},createContext(){return ctx;}};
const wrapped = Object.values(tools).map(definition => wrapRegisteredTool({definition},runner));
let providerCalls = 0;
const calls = [
  ...(MIXED ? [{type:'toolCall',id:'read',name:'read',arguments:{path:'.'}}] : []),
  {type:'toolCall',id:'submit',name:'rsi_wiki_submit',arguments:{paths:['wiki/findings/test.md'],summary:'submitted'}},
  {type:'toolCall',id:'write',name:'write',arguments:{path:process.env.RSI_AGENT_WORKSPACE+'/late.md'}}
];
const model = {api:'openai-responses',provider:'fixture',id:'fixture',contextWindow:256000,maxTokens:128000};
const assistant = {role:'assistant',content:calls,stopReason:'toolUse',timestamp:Date.now(),usage:{input:0,output:0,totalTokens:0}};
const config = {model,convertToLlm:messages=>messages,toolExecution:'parallel',
  beforeToolCall:async ({toolCall,args})=>runHooks('tool_call',{toolName:toolCall.name,toolCallId:toolCall.id,input:args},ctx),
  afterToolCall:async ({toolCall,result,isError})=>runHooks('tool_result',{toolName:toolCall.name,toolCallId:toolCall.id,isError,...result},ctx)};
const emit = async event => {
  await runHooks(event.type,event,ctx);
  if(event.type === 'message_end' && event.message.role === 'toolResult') {
    fs.appendFileSync(process.env.RSI_AGENT_WORKSPACE+'/persisted.jsonl',JSON.stringify(event.message)+'\\n');
    console.log(JSON.stringify({stage:'persisted',message:event.message}));
  }
};
const stream = () => {
  providerCalls++;
  if(providerCalls > 1) throw Error('Unexpected second provider call');
  return {async *[Symbol.asyncIterator](){yield {type:'done',reason:'toolUse',message:assistant};},async result(){return assistant;}};
};
await runAgentLoop([{role:'user',content:[{type:'text',text:'submit curator proposal'}],timestamp:Date.now()}],
  {systemPrompt:'fixture',messages:[],tools:wrapped},config,emit,controller.signal,stream);
throw Error('SDK loop returned past the submitted turn boundary');
'''.replace("CORE", json.dumps(core.as_uri())).replace("WRAPPER", json.dumps(wrapper.as_uri())).replace("MIXED", str(mixed_batch).lower())
        result = run_extension(tmp_path, script, socket_path=socket)
    assert result.returncode == 0, result.stderr
    persisted = [json.loads(line) for line in (tmp_path / "workspace/persisted.jsonl").read_text().splitlines()]
    assert [message["toolName"] for message in persisted] == (["read", "rsi_wiki_submit"] if mixed_batch else ["rsi_wiki_submit"])
    assert persisted[-1]["details"]["status"] == "accepted"
    assert not (tmp_path / "workspace/unexpected-write").exists()
    assert [request["tool"] for request in seen] == (["rsi_inbox", "rsi_wiki_submit"] if mixed_batch else ["rsi_wiki_submit"])
