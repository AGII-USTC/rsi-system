"""Runtime-owned frozen pi0.5 service; weights remain at the read-only checkpoint."""
import argparse
import importlib.util
import json
import logging
from pathlib import Path
import sys


class ExplicitNoisePolicy:
    def __init__(self, policy):
        self.policy = policy

    def infer(self, observation):
        import numpy as np

        request = dict(observation)
        if 'harness_flow_noise' not in request:
            raise ValueError('Provide harness_flow_noise: finite float32[10,32] for reproducible inference')
        noise = np.asarray(request.pop('harness_flow_noise'), dtype=np.float32)
        if noise.shape != (10, 32) or not np.all(np.isfinite(noise)):
            raise ValueError('harness_flow_noise must be finite float32[10,32]')
        return self.policy.infer(request, noise=noise)


def load_bound_policy(manifest_path, roots, *, mode='official'):
    """Load one explicit comparison arm; usable directly by an isolated Runner."""
    from pi05_bindings import sha256_file, verify_manifest

    if mode not in ('official', 'memory', 'zero-gate'):
        raise ValueError('Unknown policy mode')
    manifest = json.loads(Path(manifest_path).read_text())
    artifacts = verify_manifest(manifest, roots)
    pinned_files = {str((Path(roots[group]) / item['path']).resolve()): item['sha256']
                    for group in ('openpi', 'memory') for item in manifest['groups'][group]}

    def verify_loaded_modules():
        origins = {}
        for name, module in tuple(sys.modules.items()):
            if name == 'openpi' or name.startswith(('openpi.', 'openpi_client.')) or name == 'rsi_bound_latent_policy':
                origin = getattr(module, '__file__', None)
                if origin is None:
                    continue
                path = str(Path(origin).resolve())
                if path not in pinned_files or sha256_file(path) != pinned_files[path]:
                    raise ValueError('Loaded module is outside the verified source: ' + name)
                origins[name] = {'path': path, 'sha256': pinned_files[path]}
        return origins

    # Pin import locations as well as file contents; a shadow installation is not equivalent.
    import openpi
    from openpi.training import config
    from openpi.policies import policy_config

    verify_loaded_modules()
    policy = None
    if mode == 'official':
        policy = policy_config.create_trained_policy(config.get_config('pi05_libero'), str(roots['checkpoint']))
        wrapper = ExplicitNoisePolicy(policy)
    else:
        loader_path = Path(roots['memory']) / 'latent_policy.py'
        spec = importlib.util.spec_from_file_location('rsi_bound_latent_policy', loader_path)
        loader = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = loader
        spec.loader.exec_module(loader)
        report = json.loads(artifacts['report'].read_text())
        # The existing loader reads the sibling report; bind that exact file, too.
        if artifacts['adapter'].with_name('report.json').resolve() != artifacts['report'].resolve():
            raise ValueError('Adapter loader report path differs from the bound report')
        position = report.get('position')
        if position not in ('output', 'velocity'):
            raise ValueError('Bound service currently supports output/velocity readers')
        policy = loader.create_policy(
            Path(roots['checkpoint']), position, adapter=artifacts['adapter'],
            metadata_bias=bool(report.get('metadata_bias', False)),
            attention_dim=report.get('attention_dim', 64),
            response_contrast=bool(report.get('response_contrast', False)),
            response_gating=bool(report.get('response_gating', False)),
            learned_time_gate=bool(report.get('learned_time_gate', False)),
        )
        if mode == 'zero-gate':
            import jax.numpy as jnp

            model = policy._model
            reader = model.memory_adapter if position == 'output' else model.velocity_memory_adapter
            reader.alpha.value = jnp.zeros_like(reader.alpha.value)
        wrapper = loader.MemoryPolicy(policy)
    origins = verify_loaded_modules()
    metadata = dict(policy.metadata)
    metadata.update(harness_fixed_noise=True, rsi_binding={
        'schema_version': 1, 'content_id': manifest['content_id'], 'mode': mode,
        'base_config': 'pi05_libero', 'source_status': 'current-source-requalification',
        'artifact_hashes': {name: item['sha256'] for name, item in manifest['artifacts'].items()},
        'loaded_modules': origins,
        'service_sha256': sha256_file(__file__),
        'binding_helper_sha256': sha256_file(Path(__file__).with_name('pi05_bindings.py')),
        'action_contract': 'Policy.infer transformed physical actions; no internal chunk claim',
    })
    return wrapper, metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--port', type=int, default=8012)
    parser.add_argument('--binding-manifest', type=Path)
    parser.add_argument('--openpi-root', type=Path, default=Path('/app'))
    parser.add_argument('--memory-root', type=Path)
    parser.add_argument('--artifact-root', type=Path)
    parser.add_argument('--mode', choices=('official', 'memory', 'zero-gate'), default='official')
    args = parser.parse_args()
    checkpoint = Path(args.checkpoint).resolve(strict=True)
    if not (checkpoint / 'params').is_dir():
        raise ValueError('Local checkpoint params directory is missing')
    logging.basicConfig(level=logging.INFO)
    if args.binding_manifest:
        if args.memory_root is None or args.artifact_root is None:
            parser.error('--binding-manifest needs --memory-root and --artifact-root')
        roots = {'checkpoint': checkpoint, 'openpi': args.openpi_root,
                 'memory': args.memory_root, 'artifacts': args.artifact_root}
        wrapper, metadata = load_bound_policy(args.binding_manifest, roots, mode=args.mode)
    else:
        if args.mode != 'official':
            parser.error('Reader modes require --binding-manifest')
        from openpi.policies import policy_config
        from openpi.training import config

        policy = policy_config.create_trained_policy(config.get_config('pi05_libero'), str(checkpoint))
        metadata = dict(policy.metadata)
        metadata['harness_fixed_noise'] = True
        wrapper = ExplicitNoisePolicy(policy)
    from openpi.serving.websocket_policy_server import WebsocketPolicyServer

    logging.info('RSI policy identity: %s', json.dumps(metadata, sort_keys=True, default=str))
    WebsocketPolicyServer(wrapper, host='0.0.0.0', port=args.port,
                          metadata=metadata).serve_forever()


if __name__ == '__main__':
    main()
