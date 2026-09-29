"""Crash injection through real Runner entrypoints and real Python probes."""
import json
from pathlib import Path

import pytest

from rsi_system.core import runtime_exploration as exploration
from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from .test_orchestrator import project
from .test_runtime_exploration import context


def setup(tmp_path):
    runtime = RuntimeCoordinator(project(tmp_path))
    runtime.start(MissionSpec('m', 'Measure', authorization={'allow_server_simulation': True},
                              resources={'max_runs': 1, 'max_seconds_per_run': 5}))
    with RuntimeStore(runtime.store_path) as store:
        source = store.create_task('m', 'researcher', 'Prototype', 'Test', idea_id='a')
    def author(**kw):
        work = Path(context(kw)['communications']['workspace'])
        (work / 'probe.py').write_text("import os\nfrom pathlib import Path\nPath(os.environ['RSI_EXPLORATION_OUTPUT'],'result.txt').write_text('measured')\n")
        return {'summary': 'Prototype'}
    assert runtime.tick(agent_runner=author)['status'] == 'completed'
    with RuntimeStore(runtime.store_path) as store:
        task = store.create_task('m', 'runner', 'Probe', 'Test', idea_id='a', experiment={
            'source_task_id': source['task_id'], 'script': 'probe.py', 'question': 'Does it run?',
            'baseline': 'None', 'expected_difference': 'Output', 'falsifier': 'No output',
            'source_reason': 'Generated test'})
        task = store.claim_task('m')
    return runtime, task


def execute(runtime, task):
    with RuntimeStore(runtime.store_path) as store:
        return runtime._execute(store, store.mission('m'), task,
                                agent_runner=lambda **kw: pytest.fail('Runner must not invoke a model'),
                                require_model=False)


def expire(runtime):
    with RuntimeStore(runtime.store_path) as store:
        store.db.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE status='running'")
        store.db.commit()


def test_unstarted_expired_runner_executes_same_task_without_extra_budget(tmp_path):
    runtime, task = setup(tmp_path)
    expire(runtime)
    result = runtime.tick()
    assert result['status'] == 'completed' and result['recovered'], result
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.list_runs('m')) == 1
        assert store.list_runs('m')[0]['status'] == 'completed'
        assert store.remaining_runs('m') == 0
        assert store.task(task['task_id'])['attempt'] == 1


def test_crash_after_launch_intent_does_not_launch_on_recovery(tmp_path, monkeypatch):
    runtime, task = setup(tmp_path)
    original = exploration.mark_started
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt('crash between intent and Popen')
    monkeypatch.setattr(exploration, 'mark_started', crash)
    with pytest.raises(KeyboardInterrupt):
        execute(runtime, task)
    directory = runtime.runtime_root / 'runs' / exploration.exploration_run_id(task['task_id'])
    assert not (directory / 'output/result.txt').exists()
    with RuntimeStore(runtime.store_path) as store:
        assert store.list_runs('m')[0]['status'] == 'prepared'
    expire(runtime)
    result = runtime.tick()
    assert result['status'] == 'completed', result
    assert not (directory / 'output/result.txt').exists()
    with RuntimeStore(runtime.store_path) as store:
        run = store.list_runs('m')[0]
        assert run['status'] == 'failed'
        assert run['evidence']['execution_status'] == 'uncertain_interrupted'


def test_receipt_survives_crash_before_intent_and_database_update(tmp_path, monkeypatch):
    runtime, task = setup(tmp_path)
    real = exploration.mark_receipt_ready
    def crash(*args, **kwargs):
        raise KeyboardInterrupt('receipt written, intent not updated')
    monkeypatch.setattr(exploration, 'mark_receipt_ready', crash)
    with pytest.raises(KeyboardInterrupt):
        execute(runtime, task)
    directory = runtime.runtime_root / 'runs' / exploration.exploration_run_id(task['task_id'])
    assert (directory / 'output/result.txt').read_text() == 'measured'
    receipt = (directory / 'run_record.json').read_bytes()
    frozen_input = (directory / 'input.json').read_bytes()
    with RuntimeStore(runtime.store_path) as store:
        payload = store.mission('m')['payload']
        payload['resources']['max_seconds_per_run'] = None
        store.db.execute('UPDATE missions SET payload_json=? WHERE mission_id=?', (json.dumps(payload), 'm'))
        store.db.commit()
    expire(runtime)
    monkeypatch.setattr(exploration, 'mark_receipt_ready', real)
    monkeypatch.setattr(exploration, 'mark_started', lambda *a, **k: pytest.fail('must not relaunch'))
    result = runtime.tick()
    assert result['status'] == 'completed', result
    assert (directory / 'run_record.json').read_bytes() == receipt
    assert (directory / 'input.json').read_bytes() == frozen_input
    with RuntimeStore(runtime.store_path) as store:
        assert len(store.list_runs('m')) == 1
        assert store.list_runs('m')[0]['status'] == 'completed'


def test_terminal_run_cannot_be_relabelled_or_rewritten(tmp_path):
    runtime, task = setup(tmp_path)
    execute(runtime, task)
    with RuntimeStore(runtime.store_path) as store:
        run = store.list_runs('m')[0]
        assert store.finalize_run(run['run_id'], 'completed', run['evidence'])['status'] == 'completed'
        with pytest.raises(ValueError, match='cannot be overwritten'):
            store.finalize_run(run['run_id'], 'failed', run['evidence'])
        with pytest.raises(ValueError, match='cannot be overwritten'):
            store.finalize_run(run['run_id'], 'completed', {'claimed': 'different'})
