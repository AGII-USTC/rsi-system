import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from rsi_system.core import development_containers as dev
from rsi_system.core import runner_environments as docker
from rsi_system.core.gpu_resources import GPUResourceBusy, gpu_slot
from rsi_system.core.runtime_tools import ToolBridge
from rsi_system.core.runtime_store import RuntimeStore
from .test_runtime_tools import setup_parent
from .test_orchestrator import project

IMAGE = 'sha256:' + 'a' * 64


def context(tmp_path):
    root = tmp_path / 'runtime'
    task = {'task_id': 'task-a', 'mission_id': 'mission-a'}
    (root / 'tasks/task-a/workspace').mkdir(parents=True)
    data = tmp_path / 'dataset'
    data.mkdir()
    config = {'runner_environments': {'dev': {'image': IMAGE, 'python': '/usr/bin/python3',
        'gpu': True, 'mounts': [{'source': str(data), 'target': '/dataset'}]}}}
    mission = {'payload': {'resources': {'runner_environment': 'dev', 'allowed_runner_environments': ['dev']}}}
    return root, task, config, mission


def fake_engine(monkeypatch, exit_code=0):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        output = IMAGE if args[:2] == ('image', 'inspect') else ''
        return subprocess.CompletedProcess(args, 0, output, '')

    def popen(argv, **kwargs):
        calls.append(argv)
        kwargs['stdout'].write(b'diagnostic-output\n')
        class Process:
            returncode = exit_code
            def poll(self):
                return self.returncode
        return Process()

    monkeypatch.setattr(docker, '_docker', run)
    monkeypatch.setattr(dev.subprocess, 'Popen', popen)
    return calls


def test_container_exposes_only_owned_workspace_and_readonly_references(tmp_path, monkeypatch):
    root, task, config, mission = context(tmp_path)
    calls = fake_engine(monkeypatch)
    monkeypatch.setenv('OPENAI_API_KEY', 'never-forward')
    result = dev.run_development_test(runtime_root=root, task=task, config=config,
        mission=mission, call_id='frozen-call', command='python3 test.py')
    assert result['exit_code'] == 0 and not result['scientific_evidence']
    assert Path(result['stdout_path']).is_relative_to(root / 'tasks/task-a/workspace')
    argv = next(x for x in calls if x[0] == 'docker')
    assert '--network=none' in argv and '--gpus' not in argv
    assert '--read-only' in argv and '--ipc=private' in argv
    assert argv[-4:] == ['/bin/sh', IMAGE, '-c', 'python3 test.py']
    assert any('dst=/dataset,readonly' in x for x in argv)
    assert any('dst=/work' in x and 'readonly' not in x for x in argv)
    assert not any('docker.sock' in x or 'never-forward' in x for x in argv)
    assert calls[-1][:2] == ('rm', '--force')
    before = len([x for x in calls if x[0] == 'docker'])
    assert dev.run_development_test(runtime_root=root, task=task, config=config,
        mission=mission, call_id='frozen-call', command='python3 test.py') == result
    assert len([x for x in calls if x[0] == 'docker']) == before


def test_uncertain_call_does_not_execute_twice(tmp_path, monkeypatch):
    root, task, config, mission = context(tmp_path)
    calls = fake_engine(monkeypatch)
    p = root / 'tasks/task-a/development/old'
    p.mkdir(parents=True)
    (p / 'request.json').write_text(json.dumps({'task_id': 'task-a', 'mission_id': 'mission-a',
        'command': 'touch result', 'environment': 'dev', 'gpu': False}))
    result = dev.run_development_test(runtime_root=root, task=task, config=config,
        mission=mission, call_id='old', command='touch result')
    assert result['status'] == 'interrupted'
    assert not any(x[0] == 'docker' for x in calls)


def test_gpu_slot_busy_is_not_a_command_failure(tmp_path, monkeypatch):
    root, task, config, mission = context(tmp_path)
    calls = fake_engine(monkeypatch)
    with gpu_slot(root):
        with pytest.raises(GPUResourceBusy):
            dev.run_development_test(runtime_root=root, task=task, config=config,
                mission=mission, call_id='gpu', command='true', gpu=True)
    assert not (root / 'tasks/task-a/development/gpu/request.json').exists()
    assert not any(x[0] == 'docker' for x in calls)
    result = dev.run_development_test(runtime_root=root, task=task, config=config,
        mission=mission, call_id='gpu', command='true', gpu=True)
    argv = next(x for x in calls if x[0] == 'docker')
    assert '--gpus' in argv and docker.gpu_resource_label(root) in argv
    assert result['exit_code'] == 0
    with gpu_slot(root):
        pass


def test_reference_cannot_shadow_work_and_gpu_orphan_is_preserved(tmp_path, monkeypatch):
    root, task, config, mission = context(tmp_path)
    fake_engine(monkeypatch)
    config['runner_environments']['dev']['mounts'][0]['target'] = '/work/other'
    with pytest.raises(ValueError, match='overlaps'):
        dev.run_development_test(runtime_root=root, task=task, config=config,
            mission=mission, call_id='work', command='true')
    with pytest.raises(GPUResourceBusy):
        with gpu_slot(root, occupied=lambda: True):
            pytest.fail('orphan must block overlapping GPU allocation')


def test_bridge_ownership_command_failure_and_replay(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    calls = []
    def test(**kwargs):
        calls.append(kwargs['task']['task_id'])
        assert kwargs['runtime_root'] == coordinator.runtime_root
        return {'kind': 'development_test', 'status': 'completed', 'exit_code': 2,
                'stderr_tail': 'missing module', 'scientific_evidence': False}
    monkeypatch.setattr(dev, 'run_development_test', test)
    bridge = ToolBridge(root, parent['task_id'])
    request = {'tool': 'rsi_dev_exec', 'call_id': 'once', 'args': {'command': 'python test.py'}}
    first = bridge.call(request)
    assert not first['ok'] and first['exit_code'] == 2 and 'missing module' in first['stderr_tail']
    assert bridge.call(request) == first and len(calls) == 1
    assert not bridge.call({**request, 'args': {'command': 'other'}})['ok']
    with RuntimeStore(coordinator.store_path) as store:
        child = store.create_task(parent['mission_id'], 'critic', 'audit', 'test',
            parent_task_id=parent['task_id'], idea_id='idea-a')
        child = store.claim_task(parent['mission_id'])
    assert not ToolBridge(root, child['task_id']).call(request)['ok']


def test_running_formal_runner_does_not_block_cpu_development(tmp_path, monkeypatch):
    root = project(tmp_path)
    coordinator, parent, _ = setup_parent(root)
    with RuntimeStore(coordinator.store_path) as store:
        t = store.create_task(parent['mission_id'], 'runner', 'run', 'test',
                             parent_task_id=parent['task_id'], idea_id='idea-a')
        store.claim_task(parent['mission_id'])
    called = []
    monkeypatch.setattr(dev, 'run_development_test', lambda **kwargs:
        called.append(kwargs['gpu']) or {'status': 'completed', 'exit_code': 0})
    bridge = ToolBridge(root, parent['task_id'])
    args = {'command': 'test', 'gpu': True}
    request = {'tool': 'rsi_dev_exec', 'call_id': 'same', 'args': args}
    assert bridge.call(request)['status'] == 'resource_busy'
    assert bridge.call({**request, 'call_id': 'cpu', 'args': {'command': 'test'}})['ok']
    assert called == [False]
