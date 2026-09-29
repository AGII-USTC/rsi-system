"""Recovery keeps budgets, roles and failures without blocking independent tasks."""
import json
import fcntl

import pytest

from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from rsi_system.core.config import load_research_config
from .test_orchestrator import project


def fixture(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec('m', 'research', resources={'max_agent_tasks': 8, 'max_parallel_agents': 3}))
    return runtime


def fail_model(**kw):
    raise TimeoutError('provider timeout')


def test_backoff_budget_and_policy_survive_config_change(tmp_path):
    runtime = fixture(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task('m', 'researcher', 'Study', 'Study', idea_id='a')
        task = store.claim_task('m')
        runtime._execute(store, store.mission('m'), task, agent_runner=fail_model, require_model=False)
        retry = [t for t in store.list_tasks('m') if t['status'] == 'queued'][0]
        assert store.claim_task('m') is None
        assert retry['scope']['retry_not_before']
        independent = store.create_task('m', 'researcher', 'Other mechanism', 'Independent', idea_id='b')
        other = store.claim_task('m')
        assert other['task_id'] == independent['task_id']
        store.finish_task(other['task_id'], other['lease_token'], {'summary': 'done'})
        policy = {'role': 'researcher', 'task_kind': 'standard',
                  'model': {'model': 'pinned-model', 'provider': 'pinned-provider'},
                  'thinking': 'xhigh', 'timeout_seconds': 10}
        store.db.execute('UPDATE runtime_agent_calls SET policy_json=? WHERE task_id=?', (json.dumps(policy), task['task_id']))
        store.db.execute('UPDATE task_scopes SET retry_not_before=NULL')
        store.db.commit()
        current = store.claim_task('m')
        def recovered(**kw):
            assert (kw['model'], kw['provider'], kw['thinking']) == ('pinned-model','pinned-provider','xhigh')
            return {'summary': 'continued'}
        result = runtime._execute(store, store.mission('m'), current, agent_runner=recovered, require_model=False)
        assert result['status'] == 'completed', result
        saved = json.loads(store.db.execute('SELECT policy_json FROM runtime_agent_calls WHERE task_id=?', (current['task_id'],)).fetchone()[0])
        assert saved['model'] == policy['model'] and saved['thinking'] == 'xhigh'
        assert store.task(task['task_id'])['status'] == 'failed'
        store.set_mission_status('m', 'paused', reason='operator pause')
        store.resume_mission('m', reason='operator resumed')
        assert store.task(task['task_id'])['status'] == 'failed'


def test_repeated_failures_stop_after_two_linked_retries(tmp_path):
    runtime = fixture(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        store.create_task('m', 'researcher', 'Study', 'Study', idea_id='a')
        for attempt in range(3):
            store.db.execute('UPDATE task_scopes SET retry_not_before=NULL')
            store.db.commit()
            task = store.claim_task('m')
            assert task
            runtime._execute(store, store.mission('m'), task, agent_runner=fail_model, require_model=False)
        assert len(store.list_tasks('m')) == 3
        assert all(t['status'] == 'failed' for t in store.list_tasks('m'))
        assert store.db.execute('SELECT count(*) FROM runtime_agent_calls').fetchone()[0] == 3


def test_active_expired_executor_does_not_starve_another_branch(tmp_path):
    runtime = fixture(tmp_path)
    with RuntimeStore(runtime.store_path) as store:
        slow = store.create_task('m', 'researcher', 'Slow', 'Still executing')
        slow = store.claim_task('m', lease_seconds=-1)
        other = store.create_task('m', 'researcher', 'Other', 'Independent')
    directory = runtime.runtime_root / 'tasks' / slow['task_id']
    directory.mkdir(parents=True)
    with (directory / 'execution.lock').open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = runtime.tick(agent_runner=lambda **kw: {'summary': 'done'}, require_model=False)
    with RuntimeStore(runtime.store_path) as store:
        assert store.task(other['task_id'])['status'] == 'completed', outcome
        assert store.task(slow['task_id'])['status'] == 'running'


@pytest.mark.parametrize('value', [True, -1, '2', 1.5])
def test_invalid_retry_config_rejected(tmp_path, value):
    root = project(tmp_path)
    p = root / 'deployment/rsi/research.json'
    data = json.loads(p.read_text()); data['runtime_recovery_max_attempts'] = value
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='runtime_recovery_max_attempts'):
        load_research_config(root, config_dir=tmp_path / 'empty')
