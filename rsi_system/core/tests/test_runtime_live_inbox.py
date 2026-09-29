"""Messages after frozen input reach active contexts; budget failures remain diagnosable."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from rsi_system.core.runtime import RuntimeStore
from rsi_system.core.runtime_communications import prepare_communications, publish_direct_messages
from rsi_system.core.runtime_tools import ToolBridge, serve_tools
from .test_orchestrator import project
from .test_runtime_tools import setup_parent


def send_late(runtime, parent, count=1):
    directory = runtime.runtime_root / 'tasks' / parent['task_id']
    with RuntimeStore(runtime.store_path) as store:
        manifest = prepare_communications(store, parent, directory)
        before = manifest.read_bytes()
        sender = store.create_task(parent['mission_id'], 'coder', 'check', 'fixture', idea_id='idea-a')
        for index in range(count):
            publish_direct_messages(store, sender, {'messages': [
                {'to_task_id': parent['task_id'], 'text': f'Counterexample {index}: revise the next probe'}]})
        other = store.create_task(parent['mission_id'], 'researcher', 'private', 'fixture', idea_id='idea-b')
        publish_direct_messages(store, other, {'messages': [{'to_role': 'researcher', 'text': 'Other branch private note'}]})
    return manifest, before


def test_late_inbox_is_paged_replayable_and_branch_scoped(tmp_path):
    runtime, parent, _ = setup_parent(project(tmp_path))
    manifest, frozen = send_late(runtime, parent, count=15)
    bridge = ToolBridge(runtime.root, parent['task_id'])
    request = {'tool': 'rsi_inbox', 'call_id': 'late-1', 'args': {}}
    first = bridge.call(request)
    assert first['ok'] and len(first['messages']) == 12 and first['has_more']
    assert all(m['version'] > parent['input_version'] for m in first['messages'])
    assert ToolBridge(runtime.root, parent['task_id']).call(request) == first
    second = bridge.call({'tool': 'rsi_inbox', 'call_id': 'late-2',
                          'args': {'after_version': first['next_version']}})
    assert len(second['messages']) == 3 and not second['has_more']
    assert all(t['idea_id'] == 'idea-a' for t in second['active_tasks'])
    assert manifest.read_bytes() == frozen
    assert not bridge.call({'tool': 'rsi_inbox', 'call_id': 'invalid', 'args': {'after_version': -1}})['ok']


@pytest.fixture
def short_root():
    with tempfile.TemporaryDirectory(prefix='rsi-inbox-') as directory:
        yield Path(directory)


def test_pi_tool_result_exposes_late_message_once(tmp_path, short_root):
    node = os.environ.get('RSI_TEST_NODE') or shutil.which('node')
    if not node or not (Path.home()/'.local/share/rsi/pi/node_modules/typebox').exists():
        pytest.skip('Pi Node runtime required')
    runtime, parent, _ = setup_parent(project(short_root))
    send_late(runtime, parent)
    extension = Path(__file__).resolve().parents[3] / 'deployment/rsi/pi-runtime-tools.ts'
    script = tmp_path / 'inbox.mjs'
    script.write_text('import extension from ' + json.dumps(extension.as_uri()) + ';\n' + '''
const hooks = {};
extension({registerTool() {}, on(name, callback) {hooks[name] = callback;}});
const event = {toolName: 'read', toolCallId: 'read-1', content: [{type:'text',text:'original tool result'}]};
const first = await hooks.tool_result(event);
const second = await hooks.tool_result({...event,toolCallId:'read-2'});
console.log(JSON.stringify({first,second: second ?? null}));
''')
    with serve_tools(runtime.root, parent['task_id']) as (socket, _):
        result = subprocess.run([node, '--experimental-strip-types', str(script)],
            env={**os.environ, 'RSI_TOOL_SOCKET': str(socket)}, capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value['first']['content'][0]['text'] == 'original tool result'
    assert 'Counterexample 0' in value['first']['content'][1]['text']
    assert value['second'] is None
    with RuntimeStore(runtime.store_path) as store:
        receipts = [x for x in store.tool_operations(parent['task_id']) if x['tool'] == 'rsi_inbox']
        assert len(receipts) == 2


def test_wait_wakes_for_message_without_cancelling_running_child(tmp_path, monkeypatch):
    runtime, parent, _ = setup_parent(project(tmp_path))
    config_path = runtime.root / 'deployment/rsi/research.json'
    config = json.loads(config_path.read_text())
    config['runtime_max_parallel_agents'] = 2
    config_path.write_text(json.dumps(config))
    started, release = Event(), Event()
    def coder(**kwargs):
        started.set()
        assert release.wait(10), 'test failed to release the child'
        return {'payload': {'summary': 'implementation complete'}, 'tool_calls': []}
    monkeypatch.setattr('rsi_system.core.pi_agent.run_agent', coder)
    bridge = ToolBridge(runtime.root, parent['task_id'])
    child = bridge.call({'tool':'rsi_delegate','call_id':'coder','args':{'role':'coder','objective':'probe'}})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(bridge.call, {'tool':'rsi_wait','call_id':'wait',
                                              'args':{'task_id':child['task_id']}})
            assert started.wait(5)
            with RuntimeStore(runtime.store_path) as store:
                publish_direct_messages(store, store.task(child['task_id']), {'messages':[
                    {'to_task_id':parent['task_id'],'text':'Counterexample found; reconsider the plan'}]})
            woke = waiting.result(timeout=5)
            assert woke['ok'] and woke['status']=='running' and woke['wait_reason']=='message_available'
            assert 'Counterexample' in woke['inbox']['messages'][0]['text']
            replay = bridge.call({'tool':'rsi_wait','call_id':'wait','args':{
                'task_id':child['task_id'],'after_message_version':woke['inbox']['next_version']}})
            assert replay == woke
    finally:
        release.set()
        if bridge.worker:
            bridge.worker.join(timeout=10)
    finished = bridge.call({'tool':'rsi_wait','call_id':'wait-again','args':{
        'task_id':child['task_id'],'after_message_version':woke['inbox']['next_version']}})
    assert finished['status']=='completed'


def test_failed_probe_reports_output_budget_and_preserves_log_hashes(tmp_path, monkeypatch):
    runtime, parent, _ = setup_parent(project(tmp_path))
    monkeypatch.setattr('rsi_system.core.runtime_exploration.MAX_TOTAL_BYTES', 800)
    def coder(**kwargs):
        workspace = Path(kwargs['output_dir']) / 'workspace'
        (workspace/'probe.py').write_text("import os\nfrom pathlib import Path\np=Path(os.environ['RSI_EXPLORATION_OUTPUT'])\nfor i in range(9): (p/str(i)).write_bytes(b'x'*100)\n")
        return {'payload': {'summary': 'script ready'}, 'tool_calls': []}
    monkeypatch.setattr('rsi_system.core.pi_agent.run_agent', coder)
    bridge = ToolBridge(runtime.root, parent['task_id'])
    child = bridge.call({'tool':'rsi_delegate','call_id':'coder','args':{'role':'coder','objective':'probe'}})
    result = bridge.call({'tool':'rsi_wait','call_id':'coded','args':{'task_id':child['task_id']}})
    file = next(f for f in result['handoff_files'] if f['workspace_path']=='probe.py')
    run = bridge.call({'tool':'rsi_run_probe','call_id':'run','args':{'objective':'measure','experiment':{
        'source_task_id':child['task_id'],'script_file_id':file['file_id'],'question':'test','baseline':'empty',
        'expected_difference':'output','falsifier':'no output','source_reason':'fixture'}}})
    measured = bridge.call({'tool':'rsi_wait','call_id':'measured','args':{'task_id':run['task_id']}})
    assert measured['run']['status']=='failed'
    evidence=measured['run']['evidence']
    diagnostic=evidence['artifact_diagnostics']
    assert diagnostic['output_bytes']==900 and diagnostic['output_file_count']==9
    assert diagnostic['log_bytes']=={'stdout.log':0,'stderr.log':0}
    assert diagnostic['limits']['max_total_bytes_including_logs']==800
    assert not evidence['artifact_hashes_complete']
    assert {'stdout.log','stderr.log'} <= set(evidence['artifact_hashes'])
    with RuntimeStore(runtime.store_path) as store:
        prompt=runtime._prompt('researcher',store.mission(parent['mission_id']),parent,store)
    assert json.loads(prompt.split('\n',1)[1])['context']['exploration_contract']['limits']['max_total_bytes_including_logs']==800
