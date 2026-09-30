"""Rejected acknowledgements get one read-only correction, not repeated research."""
import json
import sys

import pytest

from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from rsi_system.core.runtime_decision_repair import repair_source
from rsi_system.core.pi_agent import run_agent
from .test_orchestrator import project
from .test_runtime_reframe import decision, report


def setup_repair(tmp_path, rejected_payload=None):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec('m', 'Explore mechanisms', resources={'max_agent_tasks': 8}))
    with RuntimeStore(runtime.store_path) as store:
        parent = store.create_task('m', 'coordinator', 'Plan', 'Initial', research_node_id='m-root', research_revision=1)
        parent = store.claim_task('m')
        child = store.create_task('m', 'researcher', 'Independent question', 'Challenge',
                                  parent_task_id=parent['task_id'], task_kind='problem_reframe')
        child = store.claim_task('m')
        runtime._handle_research(store, store.mission('m'), child, report())
        store.finish_task(child['task_id'], child['lease_token'], {'payload': report()})
        store.finish_task(parent['task_id'], parent['lease_token'], {'summary': 'Plan complete'})
        actor = store.create_task('m', 'coordinator', 'Review', 'New report')
        actor = store.claim_task('m')
        result = runtime._execute(store, store.mission('m'), actor,
                                  agent_runner=lambda **kw: (rejected_payload or decision('wait')),
                                  require_model=False)
        assert result['status'] == 'task_failed'
        assert store.task(actor['task_id'])['status'] == 'failed'
        queued = [t for t in store.list_tasks('m') if t['status'] == 'queued']
        assert len(queued) == 1
        return runtime, actor, child, queued[0]


def claim_repair(store):
    # Delayed retries remain gated in production; advance only this test fixture.
    columns = {r[1] for r in store.db.execute('PRAGMA table_info(task_scopes)')}
    if 'retry_not_before' in columns:
        store.db.execute("UPDATE task_scopes SET retry_not_before=NULL")
        store.db.commit()
    return store.claim_task('m')


def test_rejected_reply_corrected_without_research_dispatch(tmp_path):
    runtime, original, child, queued = setup_repair(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        task = claim_repair(store)
        assert repair_source(store, task)['source_task_id'] == original['task_id']
        def agent(**kw):
            assert kw['decision_repair'] is True
            prompt = json.loads(kw['prompt'].split('\n', 1)[1])
            assert 'tasks' not in prompt['context']
            assert prompt['context']['reframes'][0]['task_id'] == child['task_id']
            return {'summary': 'Acknowledge independent challenge', 'challenge_responses': [{
                'task_id': child['task_id'], 'disposition': 'defer',
                'reason': 'Needs matched evidence', 'next_test': 'Matched contrast'}]}
        result = runtime._execute(store, store.mission('m'), task, agent_runner=agent, require_model=False)
        assert result['status'] == 'completed', result
        assert len(store.list_tasks('m')) == 4
        assert store.task(original['task_id'])['status'] == 'failed'
        assert store.mission('m')['status'] == 'active'
        recorded = [e for e in store.events('m') if e['kind'] == 'coordinator_decision'][-1]
        assert recorded['payload']['decision'] == 'wait'
        assert recorded['payload']['challenge_responses'][0]['task_id'] == child['task_id']


@pytest.mark.parametrize('rejected', [
    {"summary": "path/event references were submitted", "decision": "handoff:task-x"},
    {"disposition": "defer", "reason": "already reviewed", "evidence_refs": ["task-x"]},
])
def test_protocol_rejection_gets_local_error_and_bounded_repair(tmp_path, rejected):
    runtime, original, child, queued = setup_repair(tmp_path, rejected_payload=rejected)
    with RuntimeStore(runtime.store_path) as store:
        repair = claim_repair(store)
        prompt = runtime._prompt('coordinator', store.mission('m'), repair, store)
        assert 'Correct one rejected coordinator response' in prompt
        assert str(rejected) not in prompt  # raw response remains in its receipt, not copied into the prompt
        assert 'coordinator decision must be schedule, wait, or stop' in prompt
        assert 'communications_outbox' in prompt
        def corrected(**kwargs):
            return {"decision": "wait", "summary": "Acknowledged the local protocol error",
                    "expected_information": "completed branch reports",
                    "budget_impact": "one correction only", "stop_condition": "continue",
                    "challenge_responses": [{"task_id": child['task_id'], "disposition": "defer",
                                               "reason": "Needs matched evidence", "next_test": "Matched contrast"}]}
        result = runtime._execute(store, store.mission('m'), repair,
                                  agent_runner=corrected, require_model=False)
        assert result['status'] == 'completed', result
        assert store.mission('m')['status'] == 'active'
        assert store.task(original['task_id'])['status'] == 'failed'
        assert len([t for t in store.list_tasks('m') if t['status'] == 'queued']) == 0


def test_recovery_context_is_source_addressed_for_followup_research(tmp_path):
    runtime, original, child, queued = setup_repair(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        repair = claim_repair(store)
        context = runtime._recovery_context(
            store, repair,
            runtime.runtime_root / 'tasks' / repair['task_id'] / 'communications/inbox/manifest.json')
        assert context['source_tasks'][0]['task_id'] == original['task_id']
        assert context['source_tasks'][0]['status'] == 'failed'
        assert 'communications_index' in context
        assert context['event_refs']
        assert 'full event history' in context['read_policy']


@pytest.mark.parametrize('payload', [
    {'summary': 'Invalid', 'challenge_responses': [{'task_id': 'missing'}]},
    {'summary': 'Dispatch forbidden', 'tasks': [{'role': 'researcher'}]},
])
def test_correction_cannot_dispatch_or_loop(tmp_path, payload):
    runtime, _, _, _ = setup_repair(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        task = claim_repair(store)
        result = runtime._execute(store, store.mission('m'), task,
                                  agent_runner=lambda **kw: payload, require_model=False)
        assert result['status'] == 'task_failed'
        assert len(store.list_tasks('m')) == 4
        assert not any(t['status'] == 'queued' for t in store.list_tasks('m'))


def test_correction_cli_exposes_only_read(tmp_path):
    fake = tmp_path / 'pi'
    fake.write_text(f'#!{sys.executable}\n' + '''import json,sys
sys.stdin.read()
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":json.dumps({"argv":sys.argv})}]}}))
''')
    fake.chmod(0o755)
    for name in ['pi-guard.ts', 'pi-runtime-tools.ts', 'pi-mcp.ts', 'pi-github.ts', 'pi-web.ts']:
        (tmp_path / name).write_text('// fixture')
    result = run_agent(executable=str(fake), role='coordinator', prompt='Correct only', cwd=tmp_path,
                       output_dir=tmp_path / 'out', timeout_seconds=5, guard=tmp_path / 'pi-guard.ts',
                       runtime_root=tmp_path / 'runtime', task_id='task-test', decision_repair=True)
    argv = result['payload']['argv']
    assert argv[argv.index('--tools') + 1] == 'read'
    assert str(tmp_path / 'pi-guard.ts') in argv
    assert all(str(tmp_path / name) not in argv for name in
               ['pi-runtime-tools.ts', 'pi-mcp.ts', 'pi-github.ts', 'pi-web.ts'])


def test_transport_retry_retains_correction_but_normal_reply_does_not(tmp_path):
    runtime, original, _, _ = setup_repair(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        task = claim_repair(store)
        def unavailable(**kw):
            assert kw['decision_repair'] is True
            raise TimeoutError('transport interrupted')
        runtime._execute(store, store.mission('m'), task, agent_runner=unavailable, require_model=False)
        retries = [t for t in store.list_tasks('m') if t['status'] == 'queued']
        assert len(retries) == 1
        assert repair_source(store, retries[0])['source_task_id'] == original['task_id']
        normal = store.create_task('m', 'coordinator', 'New research decision', 'Reply to corrected turn',
                                   reply_to_task_id=task['task_id'])
        assert repair_source(store, normal) is None
