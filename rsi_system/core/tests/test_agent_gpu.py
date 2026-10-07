from pathlib import Path

from rsi_system.core import sandbox


def test_gpu_devices_exclude_regular_files_and_unrelated_devices(tmp_path, monkeypatch):
    for name in ('nvidia0', 'nvidia1', 'nvidiactl', 'nvidia-uvm', 'nvidia-not-a-device'):
        (tmp_path / name).touch()
    caps = tmp_path / 'nvidia-caps'
    caps.mkdir()
    (caps / 'nvidia-cap1').touch()
    (caps / 'unrelated').touch()
    real_devices = {'nvidia0', 'nvidiactl', 'nvidia-uvm', 'nvidia-cap1'}
    monkeypatch.setattr(Path, 'is_char_device', lambda path: path.name in real_devices)
    assert {path.name for path in sandbox.gpu_device_paths(tmp_path)} == real_devices


def test_gpu_access_preserves_workspace_boundary_and_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: '/usr/bin/bwrap')
    devices = [Path('/dev/nvidia0'), Path('/dev/nvidia-caps/nvidia-cap1')]
    monkeypatch.setattr(sandbox, 'gpu_device_paths', lambda: devices)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    cpu = sandbox.agent_command(['python', 'test.py'], workspace=workspace)
    gpu = sandbox.agent_command(['python', 'test.py'], workspace=workspace, gpu=True)
    assert '--dev-bind' not in cpu
    assert gpu[:10] == cpu[:10]
    assert gpu[-7:] == cpu[-7:]
    assert gpu[gpu.index('--ro-bind'):gpu.index('--ro-bind') + 3] == ['--ro-bind', '/', '/']
    assert ['--bind', str(workspace), str(workspace)] == gpu[gpu.index('--bind'):gpu.index('--bind') + 3]
    for device in devices:
        index = gpu.index(str(device))
        assert gpu[index - 1:index + 2] == ['--dev-bind', str(device), str(device)]
