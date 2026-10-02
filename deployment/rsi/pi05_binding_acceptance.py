"""Engineering acceptance of bound real weights; no adaptation or success-rate claim."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--simulator-smoke', action='store_true')
    for name in ('checkpoint', 'openpi', 'memory', 'artifacts'):
        parser.add_argument('--' + name + '-root', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'claim': 'Engineering binding/inference acceptance only',
              'checkpoint_loads': 0, 'policy_calls': 0, 'simulator_steps': 0, 'stages': []}
    started = time.monotonic()

    def save():
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    save()
    try:
        import jax
        import numpy as np
        from pi05_policy_service import load_bound_policy
        from pi05_bindings import verify_manifest, sha256_file

        roots = {name: getattr(args, name + '_root') for name in ('checkpoint', 'openpi', 'memory', 'artifacts')}
        manifest = json.loads(args.manifest.read_text())
        artifacts = verify_manifest(manifest, roots)
        report['manifest_id'] = manifest['content_id']
        report['devices'] = [str(d) for d in jax.devices()]
        report['backend'] = jax.default_backend()
        with np.load(artifacts['request'], allow_pickle=False) as data:
            request = {'observation/image': data['image'], 'observation/wrist_image': data['wrist_image'],
                       'observation/state': data['state'], 'prompt': data['prompt'].item(),
                       'harness_flow_noise': data['harness_flow_noise'],
                       'memory_tokens': data['memory_tokens'], 'memory_mask': data['memory_mask']}
        outputs = {}

        def infer(wrapper, label, observation):
            tick = time.monotonic()
            response = wrapper.infer(observation)
            actions = np.asarray(response['actions'])
            if actions.shape != (10, 7) or not np.isfinite(actions).all():
                raise ValueError('Invalid physical action chunk: ' + label + ' ' + str(actions.shape))
            outputs[label] = actions
            report['policy_calls'] += 1
            report['stages'].append({'stage': label, 'wall_seconds': time.monotonic() - tick,
                                     'action_shape': list(actions.shape)})
            np.savez_compressed(args.output / 'actions.npz', **outputs)
            save()
            return actions

        # A compiled policy may capture its parameters; changing alpha after
        # inference does not reliably change the executable. Load each arm fresh.
        for mode in ('official', 'zero-gate', 'memory'):
            tick = time.monotonic()
            wrapper, metadata = load_bound_policy(args.manifest, roots, mode=mode)
            report['checkpoint_loads'] += 1
            report[mode + '_identity'] = metadata
            report['stages'].append({'stage': 'load_' + mode, 'wall_seconds': time.monotonic() - tick})
            save()
            infer(wrapper, mode.replace('-', '_'), request)
            if mode != 'memory':
                del wrapper
                gc.collect()
                jax.clear_caches()

        empty = dict(request, memory_mask=np.zeros(16, dtype=bool))
        infer(wrapper, 'empty', empty)
        reader = (wrapper.policy._model.memory_adapter if metadata['position'] == 'output'
                  else wrapper.policy._model.velocity_memory_adapter)
        report['trained_alpha'] = np.asarray(reader.alpha.value).tolist()
        infer(wrapper, 'memory_repeat', request)
        report['repeat_exact'] = bool(np.array_equal(outputs['memory'], outputs['memory_repeat']))
        report['physical_deltas_to_official'] = {
            label: {'exact_equal': bool(np.array_equal(action, outputs['official'])),
                    'max_abs': float(np.max(np.abs(action - outputs['official'])))}
            for label, action in outputs.items()}
        if not report['repeat_exact']:
            raise ValueError('Repeated bound fixed-noise inference is not deterministic')
        report['zero_gate_matches_official'] = bool(np.array_equal(outputs['zero_gate'], outputs['official']))
        report['empty_matches_official'] = bool(np.array_equal(outputs['empty'], outputs['official']))
        if not report['zero_gate_matches_official'] or not report['empty_matches_official']:
            raise ValueError('Empty-mask or separately loaded zero-gate policy differs from the official baseline')

        if args.simulator_smoke:
            from libero.libero import benchmark
            from examples.libero.main import LIBERO_DUMMY_ACTION, LIBERO_ENV_RESOLUTION, _get_libero_env
            from libero_gain_eval import _policy_input

            suite = benchmark.get_benchmark_dict()['libero_spatial']()
            env, prompt = _get_libero_env(suite.get_task(6), LIBERO_ENV_RESOLUTION, 7)
            report['simulator'] = {'task': 6, 'initial_state': 6, 'seed': 7, 'warmup_steps': 10, 'transitions': []}
            try:
                env.reset()
                obs = env.set_init_state(suite.get_task_init_states(6)[6])
                for _ in range(10):
                    obs, _, _, _ = env.step(LIBERO_DUMMY_ACTION)
                    report['simulator_steps'] += 1
                for step in range(2):
                    observation = _policy_input(obs, prompt)
                    observation.update(harness_flow_noise=request['harness_flow_noise'],
                                       memory_tokens=np.zeros((16, 32), np.float32),
                                       memory_mask=np.zeros(16, bool))
                    action = np.clip(infer(wrapper, 'sim_step_' + str(step), observation)[0], -1, 1)
                    before = np.asarray(obs['robot0_eef_pos']).copy()
                    obs, reward, done, _ = env.step(action.tolist())
                    report['simulator_steps'] += 1
                    report['simulator']['transitions'].append({
                        'issued_action': action.tolist(), 'eef_before': before.tolist(),
                        'eef_after': np.asarray(obs['robot0_eef_pos']).tolist(),
                        'reward': float(reward), 'done': bool(done)})
                    save()
                    if done:
                        break
            finally:
                env.close()
        report['actions_sha256'] = sha256_file(args.output / 'actions.npz')
        report['status'] = 'passed'
    except Exception:
        report['status'] = 'failed'
        report['error'] = traceback.format_exc()
        raise
    finally:
        report['wall_seconds'] = time.monotonic() - started
        save()


if __name__ == '__main__':
    main()
