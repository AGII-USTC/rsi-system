"""Binding identities survive mount remapping and reject altered execution inputs."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types

import pytest


DEPLOYMENT = Path(__file__).resolve().parents[3] / 'deployment/rsi'


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOYMENT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bindings = load_script('pi05_bindings')
service = load_script('pi05_policy_service')


def installation(tmp_path):
    roots = {name: tmp_path / name for name in ('checkpoint', 'openpi', 'memory', 'artifacts')}
    files = {'checkpoint': ['params/weights', 'assets/libero/norm_stats.json'],
             'openpi': ['src/openpi/__init__.py', 'src/openpi/models/pi0.py',
                        'src/openpi/training/config.py', 'src/openpi/policies/policy_config.py',
                        'packages/openpi-client/src/openpi_client/__init__.py'],
             'memory': ['latent_policy.py'],
             'artifacts': ['adapter.npz', 'report.json', 'request.npz']}
    for group, names in files.items():
        for name in names:
            file = roots[group] / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(name.encode())
    artifacts = {name: roots['artifacts'] / file for name, file in
                 [('adapter', 'adapter.npz'), ('report', 'report.json'), ('request', 'request.npz')]}
    manifest = bindings.build_manifest(roots, artifacts)
    return roots, manifest


def test_manifest_is_portable_across_host_container_mounts(tmp_path):
    roots, manifest = installation(tmp_path / 'host')
    relocated = {}
    for group, root in roots.items():
        relocated[group] = tmp_path / 'container' / group
        shutil.copytree(root, relocated[group])
    result = bindings.verify_manifest(manifest, relocated)
    assert result['adapter'] == relocated['artifacts'] / 'adapter.npz'
    assert str(tmp_path) not in json.dumps(manifest)


@pytest.mark.parametrize('group,relative', [('checkpoint', 'params/weights'),
                                          ('checkpoint', 'assets/libero/norm_stats.json'),
                                          ('memory', 'latent_policy.py'),
                                          ('artifacts', 'adapter.npz')])
def test_changed_weights_stats_source_or_adapter_are_rejected(tmp_path, group, relative):
    roots, manifest = installation(tmp_path)
    (roots[group] / relative).write_bytes(b'changed')
    with pytest.raises(ValueError, match='Content changed'):
        bindings.verify_manifest(manifest, roots)


def test_new_shadow_source_file_and_escape_are_rejected(tmp_path):
    roots, manifest = installation(tmp_path)
    extra = roots['openpi'] / 'src/openpi/extra.py'
    extra.write_text('')
    with pytest.raises(ValueError, match='inventory changed'):
        bindings.verify_manifest(manifest, roots)
    with pytest.raises(ValueError, match='relative'):
        bindings._inside(roots['checkpoint'], '../artifacts/adapter.npz')


def test_loaded_official_identity_and_shadow_import_check(tmp_path, monkeypatch):
    roots, manifest = installation(tmp_path)
    file = tmp_path / 'manifest.json'
    file.write_text(json.dumps(manifest))
    monkeypatch.setitem(sys.modules, 'pi05_bindings', bindings)
    modules = {}
    for name, rel in [('openpi', 'src/openpi/__init__.py'),
                      ('openpi.training', None), ('openpi.policies', None),
                      ('openpi.training.config', 'src/openpi/training/config.py'),
                      ('openpi.policies.policy_config', 'src/openpi/policies/policy_config.py')]:
        module = types.ModuleType(name)
        if rel:
            module.__file__ = str(roots['openpi'] / rel)
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
    modules['openpi.training'].config = modules['openpi.training.config']
    modules['openpi.policies'].policy_config = modules['openpi.policies.policy_config']
    modules['openpi.training.config'].get_config = lambda name: name
    policy = types.SimpleNamespace(metadata={'base': 'pi05_libero'})
    modules['openpi.policies.policy_config'].create_trained_policy = lambda *args: policy
    wrapper, metadata = service.load_bound_policy(file, roots)
    assert wrapper.policy is policy
    assert metadata['rsi_binding']['content_id'] == manifest['content_id']
    assert metadata['rsi_binding']['mode'] == 'official'
    modules['openpi.training.config'].__file__ = str(tmp_path / 'shadow.py')
    with pytest.raises(ValueError, match='outside the verified source'):
        service.load_bound_policy(file, roots)


def test_unknown_mode_cannot_load_model(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pi05_bindings', bindings)
    with pytest.raises(ValueError, match='Unknown policy mode'):
        service.load_bound_policy(tmp_path / 'absent.json', {}, mode='random-reader')


@pytest.mark.parametrize('mode,expected_alpha', [('memory', 0.047), ('zero-gate', 0)])
def test_reader_modes_use_bound_report_and_only_zero_reader_gate(tmp_path, monkeypatch, mode, expected_alpha):
    roots, _ = installation(tmp_path)
    (roots['artifacts'] / 'report.json').write_text(json.dumps({'position': 'output', 'response_contrast': True}))
    (roots['memory'] / 'latent_policy.py').write_text(
        'from types import SimpleNamespace as S\n'
        'def create_policy(checkpoint, position, **kwargs):\n'
        '    assert position == "output" and kwargs["response_contrast"] is True\n'
        '    return S(metadata={"position": position}, _model=S(base_weight=17, '
        'memory_adapter=S(alpha=S(value=0.047))))\n'
        'class MemoryPolicy:\n'
        '    def __init__(self, policy): self.policy = policy\n')
    manifest = bindings.build_manifest(roots, {name: roots['artifacts'] / file for name, file in
                                               [('adapter', 'adapter.npz'), ('report', 'report.json'),
                                                ('request', 'request.npz')]})
    file = tmp_path / 'manifest.json'
    file.write_text(json.dumps(manifest))
    monkeypatch.setitem(sys.modules, 'pi05_bindings', bindings)
    for name in ('openpi', 'openpi.training', 'openpi.policies', 'openpi.training.config',
                 'openpi.policies.policy_config', 'jax', 'jax.numpy'):
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules['openpi.training'].config = sys.modules['openpi.training.config']
    sys.modules['openpi.policies'].policy_config = sys.modules['openpi.policies.policy_config']
    sys.modules['jax.numpy'].zeros_like = lambda value: 0
    # Keep the dynamic loader isolated from other tests' module registry.
    monkeypatch.setitem(sys.modules, 'rsi_bound_latent_policy', None)
    wrapper, metadata = service.load_bound_policy(file, roots, mode=mode)
    assert wrapper.policy._model.memory_adapter.alpha.value == expected_alpha
    assert wrapper.policy._model.base_weight == 17
    assert metadata['rsi_binding']['loaded_modules']['rsi_bound_latent_policy']['sha256'] == bindings.sha256_file(
        roots['memory'] / 'latent_policy.py')
