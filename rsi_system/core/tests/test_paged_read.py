"""Real Node source reads remain complete, bounded, hash pinned, and authorized."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from .test_runtime_context import GUARD, node_command, run_guard, source_record


def decode_result(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("json_pointer", [None, "/content/0/text", "/messages/0/content/0/text", "/a~1b/~0text"])
def test_pages_reconstruct_unicode_and_long_single_line_sources(tmp_path, json_pointer):
    text = "\ufeff\u7814\u7a76\U0001f9ea\U0001f680e\u0301" * 6000
    source = tmp_path / "long-source.json"
    value = {"content": [{"text": text}], "messages": [{"content": [{"text": text}]}],
             "a/b": {"~text": text}}
    source.write_text(text if json_pointer is None else json.dumps(value, ensure_ascii=False), encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const pages = [];
let offset = 0;
let sha256;
for (;;) {
  const args = {path: request.path, offset, max_bytes: 8192,
    ...(sha256 ? {sha256} : {}), ...(request.pointer === null ? {} : {json_pointer:request.pointer})};
  const allowed = await hooks.tool_call({toolName:'rsi_read_source',input:args});
  if (allowed?.block) throw Error(allowed.reason);
  const result = await tools.rsi_read_source.execute('read-'+pages.length, args);
  const page = JSON.parse(result.content[0].text);
  if (!page.ok) throw Error(page.error.message);
  pages.push({page,response_bytes:Buffer.byteLength(JSON.stringify(result),'utf8')});
  if (page.eof) break;
  if (page.next_offset <= offset) throw Error('Cursor did not advance');
  offset = page.next_offset;
  sha256 = page.sha256;
}
console.log(JSON.stringify({pages, bridge:process.env.RSI_TOOL_SOCKET ?? null}));
''', limit=256000, payload={"path": str(source), "pointer": json_pointer})
    response = decode_result(result)
    pages = [item["page"] for item in response["pages"]]
    assert response["bridge"] is None
    assert len(pages) > 3
    assert "".join(page["text"] for page in pages) == text
    assert pages[0]["offset"] == 0
    assert pages[-1]["next_offset"] == len(text)
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    for index, page in enumerate(pages):
        assert page["sha256"] == expected_hash
        assert page["offset_unit"] == "unicode_codepoints"
        assert page["next_offset"] - page["offset"] == len(page["text"])
        assert page["bytes"] == len(page["text"].encode("utf-8")) <= 8192
        assert page["eof"] is (index == len(pages) - 1)
        assert response["pages"][index]["response_bytes"] <= 20000
        if index:
            assert page["offset"] == pages[index - 1]["next_offset"]


def test_json_escaping_is_counted_in_the_entire_page_response(tmp_path):
    text = ('\x00\n\\"\t' + "\U0001f9ea") * 10000
    source = tmp_path / "escaped.json"
    source.write_text(json.dumps({"text": text}), encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const results = [];
let offset = 0;
let sha256;
for (;;) {
  const result = await tools.rsi_read_source.execute('page', {path:request.path,json_pointer:'/text',
    offset,max_bytes:16384,...(sha256 ? {sha256} : {})});
  const page = JSON.parse(result.content[0].text);
  if (!page.ok) throw Error(page.error.message);
  results.push({page,bytes:Buffer.byteLength(JSON.stringify(result),'utf8')});
  if (page.eof) break;
  offset = page.next_offset; sha256 = page.sha256;
}
console.log(JSON.stringify(results));
''', payload={"path": str(source)})
    pages = decode_result(result)
    assert "".join(item["page"]["text"] for item in pages) == text
    assert all(item["bytes"] <= 20000 for item in pages)
    assert any(item["page"]["bytes"] < 16384 for item in pages[:-1])


def test_continuation_requires_hash_and_rejects_mutated_sources(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("before " * 2000, encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const first = JSON.parse((await tools.rsi_read_source.execute('first',{path:request.path,max_bytes:100})).content[0].text);
const unpinned = await tools.rsi_read_source.execute('unpin',{path:request.path,offset:first.next_offset});
fs.writeFileSync(request.path, 'after '.repeat(2000));
const mutated = await tools.rsi_read_source.execute('next',{path:request.path,offset:first.next_offset,sha256:first.sha256});
const wrong = await tools.rsi_read_source.execute('wrong',{path:request.path,sha256:'0'.repeat(64)});
console.log(JSON.stringify({first, unpinned:JSON.parse(unpinned.content[0].text),
  mutated:JSON.parse(mutated.content[0].text),wrong:JSON.parse(wrong.content[0].text)}));
''', payload={"path": str(source)})
    response = decode_result(result)
    assert response["first"]["ok"] is True
    for name in ("unpinned", "mutated", "wrong"):
        assert response[name]["ok"] is False
        assert "SHA-256" in response[name]["error"]["message"]


@pytest.mark.parametrize("arguments", [{"offset": -1}, {"offset": 0.5}, {"max_bytes": 3},
    {"max_bytes": 16385}, {"json_pointer": "text"}, {"json_pointer": "/~2text"},
    {"json_pointer": "/content/01"}, {"json_pointer": "/missing"}, {"extra": True}])
def test_invalid_cursors_or_pointers_fail_closed(tmp_path, arguments):
    source = tmp_path / "source.json"
    source.write_text('{"content":["known"]}', encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const result = await tools.rsi_read_source.execute('invalid',{path:request.path,...request.arguments});
console.log(result.content[0].text);
''', payload={"path": str(source), "arguments": arguments})
    assert decode_result(result)["ok"] is False


def test_source_permissions_and_symlink_replacement_are_rechecked_at_execution(tmp_path):
    result, _ = run_guard(tmp_path, '''
const source = process.env.RSI_AGENT_ROOT + '/source.txt';
const secret = process.env.RSI_AGENT_ROOT + '/.rsi/private.txt';
fs.writeFileSync(source,'authorized source');
fs.mkdirSync(process.env.RSI_AGENT_ROOT + '/.rsi'); fs.writeFileSync(secret,'private contents');
const paths = ['/etc/passwd', '../outside.txt', secret];
const checks = [];
for (const path of paths) {
  const call = await hooks.tool_call({toolName:'rsi_read_source',input:{path}});
  const page = JSON.parse((await tools.rsi_read_source.execute('denied',{path})).content[0].text);
  checks.push({path,blocked:!!call?.block,page});
}
const before = await hooks.tool_call({toolName:'rsi_read_source',input:{path:source}});
fs.unlinkSync(source); fs.symlinkSync(secret,source);
const after = JSON.parse((await tools.rsi_read_source.execute('swap',{path:source})).content[0].text);
const link = process.env.RSI_AGENT_WORKSPACE + '/link.txt'; fs.symlinkSync(secret,link);
const workspaceLink = JSON.parse((await tools.rsi_read_source.execute('link',{path:link})).content[0].text);
console.log(JSON.stringify({checks,first_allowed:!before?.block,after,workspaceLink}));
''')
    response = decode_result(result)
    assert response["first_allowed"] is True
    assert all(item["blocked"] and item["page"]["ok"] is False for item in response["checks"])
    assert response["after"]["ok"] is False
    assert response["workspaceLink"]["ok"] is False
    assert "private contents" not in result.stdout


def test_pi_native_read_result_is_not_rewrapped_by_rsi(tmp_path):
    content = [{"type": "text", "text": "\u7814\u7a76\U0001f9ea" * 800}]
    result, _ = run_guard(tmp_path, '''
const event = {toolName:'read',toolCallId:'old',content:request.content,details:{raw:'large details'.repeat(10000)}};
const projection = hooks.tool_result(event);
const archivePath = process.env.RSI_AGENT_WORKSPACE + '/.context-sources';
const archiveCount = fs.existsSync(archivePath) ? fs.readdirSync(archivePath).length : 0;
console.log(JSON.stringify({projection:projection ?? null,archiveCount}));
''', limit=256000, payload={"content": content})
    response = decode_result(result)
    assert response["projection"] is None
    assert response["archiveCount"] == 0


def test_archived_custom_text_pages_are_not_archived_or_truncated_again(tmp_path):
    text = "complete source " * 15000
    result, _ = run_guard(tmp_path, '''
const original = {toolName:'web_search',toolCallId:'huge',content:[{type:'text',text:request.text}],details:{origin:'source'}};
const projected = hooks.tool_result(original);
const summary = JSON.parse(projected.content[0].text);
const {tool,...args} = summary.read_source;
const page = await tools.rsi_read_source.execute('page',{...args,max_bytes:16384});
const count = () => fs.readdirSync(process.env.RSI_AGENT_WORKSPACE + '/.context-sources').length;
const before = count();
const repeated = hooks.tool_result({toolName:'rsi_read_source',toolCallId:'page',...page});
const after = count();
const messages = [
  {role:'user',content:[{type:'text',text:'keep source constraints'}]},
  {role:'assistant',content:[{type:'toolCall',id:'huge',name:'web_search',arguments:{path:'source'}}]},
  {role:'toolResult',...original},
  {role:'assistant',content:[{type:'toolCall',id:'page',name:'rsi_read_source',arguments:args}]},
  {role:'toolResult',toolName:'rsi_read_source',toolCallId:'page',...page}
];
const payload = {input:messages};
const provider = hooks.before_provider_request({payload},{model:{contextWindow:256000,maxTokens:128000}});
console.log(JSON.stringify({summary,page,recursive:repeated !== undefined,before,after,messages,payload,provider}));
''', limit=256000, payload={"text": text})
    response = decode_result(result)
    assert response["recursive"] is False
    assert response["before"] == response["after"] == 1
    assert response["summary"]["read_source"]["json_pointer"] == "/content/0/text"
    assert source_record(response["summary"]["source_ref"])["content"][0]["text"] == text
    assert response.get("provider") is None
    assert response["payload"]["input"] == response["messages"]
    page = json.loads(response["page"]["content"][0]["text"])
    assert page["text"] == text[:page["next_offset"]]
    current = next(message for message in response["messages"] if message.get("toolCallId") == "page")
    assert current["content"] == response["page"]["content"]
    assert current["details"] == response["page"]["details"]


def test_registered_schema_is_accepted_by_installed_pi_sdk(tmp_path):
    sdk = Path.home() / ".local/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/utils/validation.js"
    if not sdk.is_file():
        pytest.skip("Installed Pi SDK required for production schema regression")
    source = tmp_path / "source.txt"
    source.write_text("installed SDK source", encoding="utf-8")
    script = '''import guard from GUARD;
import {validateToolArguments} from SDK;
let tool;
guard({on(){},registerTool(value){tool=value;}});
const args = validateToolArguments(tool, {name:tool.name,id:'sdk',arguments:{path:SOURCE,max_bytes:8192}});
const result = await tool.execute('sdk',args);
console.log(result.content[0].text);
'''.replace("GUARD", json.dumps(GUARD.as_uri())).replace("SDK", json.dumps(sdk.as_uri())).replace("SOURCE", json.dumps(str(source)))
    env = {key: value for key, value in os.environ.items() if not key.startswith("RSI_")}
    env.update(RSI_AGENT_ROOT=str(tmp_path), RSI_AGENT_ROLE="curator")
    result = subprocess.run([node_command(), "--experimental-strip-types", "--input-type=module", "-e", script],
        env=env, capture_output=True, text=True, timeout=10)
    page = decode_result(result)
    assert page["ok"] is True
    assert page["text"] == "installed SDK source"


def test_native_provider_keeps_page_when_runtime_appends_large_inbox(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("source text " * 10000, encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const page = await tools.rsi_read_source.execute('page',{path:request.path,max_bytes:16000});
const inbox = {type:'text',text:'runtime inbox '.repeat(1000)};
const messages = [
  {role:'user',content:[{type:'text',text:'source constraints'}]},
  {role:'assistant',content:[{type:'toolCall',id:'page',name:'rsi_read_source',arguments:{path:request.path}}]},
  {role:'toolResult',toolName:'rsi_read_source',toolCallId:'page',content:[...page.content,inbox],
    details:{...page.details,raw:'runtime duplicate'.repeat(10000)}}
];
const projection = hooks.tool_result(messages[2]);
const payload = {input:messages};
const provider = hooks.before_provider_request({payload},{model:{contextWindow:256000,maxTokens:128000}});
console.log(JSON.stringify({page,inbox,messages,projection:projection ?? null,payload,provider}));
''', limit=256000, payload={"path": str(source)})
    response = decode_result(result)
    assert response.get("provider") is None
    assert response["payload"]["input"] == response["messages"]
    assert response["projection"] is None
    current = response["messages"][2]
    assert current["content"] == response["messages"][2]["content"]
    assert current["content"][0] == response["page"]["content"][0]
    assert current["content"][1] == response["inbox"]


def test_large_sparse_source_is_rejected_before_loading_its_contents(tmp_path):
    source = tmp_path / "large-source.txt"
    with source.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    result, _ = run_guard(tmp_path, '''
const call = await hooks.tool_call({toolName:'rsi_read_source',input:{path:request.path}});
const page = JSON.parse((await tools.rsi_read_source.execute('large',{path:request.path})).content[0].text);
console.log(JSON.stringify({call,page}));
''', payload={"path": str(source)})
    response = decode_result(result)
    assert response["call"]["block"] is True
    assert response["page"]["ok"] is False
    assert "16 MiB" in response["call"]["reason"]
    assert "16 MiB" in response["page"]["error"]["message"]


def test_frozen_manifest_sources_are_authorized_and_pinned_after_mutation(tmp_path):
    bundle = tmp_path / ".rsi/evidence"
    bundle.mkdir(parents=True)
    source = bundle / "assigned.txt"
    source.write_text("frozen evidence " * 1000, encoding="utf-8")
    secret = bundle / "unassigned.txt"
    secret.write_text("unassigned private source", encoding="utf-8")
    manifest = bundle / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"path": source.name,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}), encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const allowed = await hooks.tool_call({toolName:'rsi_read_source',input:{path:request.path}});
const first = JSON.parse((await tools.rsi_read_source.execute('first',{path:request.path,max_bytes:100})).content[0].text);
const forbidden = JSON.parse((await tools.rsi_read_source.execute('forbidden',{path:request.secret})).content[0].text);
fs.appendFileSync(request.path,'changed');
const call = await hooks.tool_call({toolName:'rsi_read_source',input:{path:request.path,offset:first.next_offset,sha256:first.sha256}});
const mutated = JSON.parse((await tools.rsi_read_source.execute('next',{path:request.path,offset:first.next_offset,sha256:first.sha256})).content[0].text);
console.log(JSON.stringify({allowed:!allowed?.block,first,forbidden,blocked:!!call?.block,mutated}));
''', payload={"path": str(source), "secret": str(secret)}, env_overrides={"RSI_EVIDENCE_MANIFEST": str(manifest)})
    response = decode_result(result)
    assert response["allowed"] is True
    assert response["first"]["ok"] is True
    assert response["forbidden"]["ok"] is False
    assert response["blocked"] is True
    assert response["mutated"]["ok"] is False
    assert "hash" in response["mutated"]["error"]["message"]


def test_external_source_reads_use_the_same_authorized_roots_as_read(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    source = external / "paper.txt"
    source.write_text("external research source", encoding="utf-8")
    manifest = root / "external.json"
    manifest.write_text(json.dumps({"schema_version": 1, "roots": [{"path": str(external),
        "mode": "read-only"}]}), encoding="utf-8")
    result, _ = run_guard(tmp_path, '''
const read = await hooks.tool_call({toolName:'read',input:{path:request.path}});
const paged = await hooks.tool_call({toolName:'rsi_read_source',input:{path:request.path}});
const page = JSON.parse((await tools.rsi_read_source.execute('external',{path:request.path})).content[0].text);
const outside = JSON.parse((await tools.rsi_read_source.execute('outside',{path:'/etc/passwd'})).content[0].text);
console.log(JSON.stringify({read_allowed:!read?.block,paged_allowed:!paged?.block,page,outside}));
''', payload={"path": str(source)}, env_overrides={"RSI_AGENT_ROOT": str(root),
    "RSI_EXTERNAL_READONLY_MANIFEST": str(manifest)})
    response = decode_result(result)
    assert response["read_allowed"] is response["paged_allowed"] is True
    assert response["page"]["text"] == "external research source"
    assert response["outside"]["ok"] is False
