"""The real Runner pipeline pins and reports its configured environment."""
import json
from contextlib import contextmanager
from pathlib import Path
import pytest

from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore
from rsi_system.core import runtime_exploration as exploration
from .test_runner_restart_acceptance import setup, execute, expire


def test_runner_uses_authorized_profile_and_recovery_keeps_pinned_environment(tmp_path, monkeypatch):
    runtime, task = setup(tmp_path)
    config = runtime.root / 'deployment/rsi/research.json'
    data = json.loads(config.read_text())
    data['runner_environments'] = {'sim': {'image': 'test-image', 'python': '/python', 'mounts': [], 'env': {}, 'gpu': False}}
    config.write_text(json.dumps(data))
    with RuntimeStore(runtime.store_path) as store:
        payload = store.mission('m')['payload']
        payload['resources'].update(runner_environment='sim', allowed_runner_environments=['sim'])
        store.db.execute('UPDATE missions SET payload_json=? WHERE mission_id=?', (json.dumps(payload), 'm'))
        store.db.commit()
    prepared = {'kind': 'docker', 'name': 'sim', 'image_id': 'sha256:fixed'}
    monkeypatch.setattr(exploration, 'prepare_environment', lambda profile: prepared)
    calls = []
    @contextmanager
    def launch(pinned, directory, argv, env, *, check_cancelled=None):
        check_cancelled()
        calls.append(pinned)
        yield argv, env, {'kind': 'docker', 'image_id': pinned['image_id'], 'test_double': True}
    monkeypatch.setattr(exploration, 'launch_environment', launch)
    real = exploration.mark_receipt_ready
    monkeypatch.setattr(exploration, 'mark_receipt_ready', lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        execute(runtime, task)
    directory = runtime.runtime_root / 'runs' / exploration.exploration_run_id(task['task_id'])
    pinned = json.loads((directory / 'input.json').read_text())
    assert pinned['runner_environment'] == prepared
    assert json.loads((directory / 'run_record.json').read_text())['execution_environment']['image_id'] == 'sha256:fixed'
    data['runner_environments']['sim']['image'] = 'changed-image'
    config.write_text(json.dumps(data))
    expire(runtime)
    monkeypatch.setattr(exploration, 'mark_receipt_ready', real)
    monkeypatch.setattr(exploration, 'prepare_environment', lambda *_: pytest.fail('must not repin completed input'))
    result = runtime.tick()
    assert result['status'] == 'completed', result
    assert calls == [prepared]
    assert json.loads((directory / 'input.json').read_text()) == pinned


def test_dedup_distinguishes_environments_and_preserves_host_fingerprints():
    original = dict(source_task_id='source', script_file_id='script', script_sha256='code', inputs_sha256='data')
    assert RuntimeCoordinator._same_exploration_fingerprint(original, {**original, 'environment': None})
    assert not RuntimeCoordinator._same_exploration_fingerprint(original, {**original, 'environment': 'libero-sim'})
    assert not RuntimeCoordinator._same_exploration_fingerprint(
        {**original, 'environment': 'libero-sim'}, {**original, 'environment': 'pi05-libero'})


def test_failed_container_cleanup_blocks_mission(tmp_path, monkeypatch):
    runtime, task = setup(tmp_path)
    @contextmanager
    def boundary(value):
        argv, env, metadata = value
        metadata['cleanup_errors'] = ['container could not be removed']
        yield argv, env, metadata
    monkeypatch.setattr(exploration, 'nullcontext', boundary)
    result = execute(runtime, task)
    with RuntimeStore(runtime.store_path) as store:
        assert store.mission('m')['status'] == 'paused'
        run = store.list_runs('m')[0]
        assert run['status'] == 'failed'
        assert 'cleanup failed' in run['evidence']['error']
