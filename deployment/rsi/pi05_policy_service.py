"""Runtime-owned frozen pi0.5 service; weights remain at the read-only checkpoint."""
import argparse
import logging
from pathlib import Path

import numpy as np
from openpi.policies import policy_config
from openpi.serving.websocket_policy_server import WebsocketPolicyServer
from openpi.training import config


class ExplicitNoisePolicy:
    def __init__(self, policy):
        self.policy = policy

    def infer(self, observation):
        request = dict(observation)
        if 'harness_flow_noise' not in request:
            raise ValueError('Provide harness_flow_noise: finite float32[10,32] for reproducible inference')
        noise = np.asarray(request.pop('harness_flow_noise'), dtype=np.float32)
        if noise.shape != (10, 32) or not np.all(np.isfinite(noise)):
            raise ValueError('harness_flow_noise must be finite float32[10,32]')
        return self.policy.infer(request, noise=noise)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--port', type=int, default=8012)
    args = parser.parse_args()
    checkpoint = Path(args.checkpoint).resolve(strict=True)
    if not (checkpoint / 'params').is_dir():
        raise ValueError('Local checkpoint params directory is missing')
    logging.basicConfig(level=logging.INFO)
    policy = policy_config.create_trained_policy(config.get_config('pi05_libero'), str(checkpoint))
    metadata = dict(policy.metadata)
    metadata['harness_fixed_noise'] = True
    WebsocketPolicyServer(ExplicitNoisePolicy(policy), host='0.0.0.0', port=args.port,
                          metadata=metadata).serve_forever()


if __name__ == '__main__':
    main()
