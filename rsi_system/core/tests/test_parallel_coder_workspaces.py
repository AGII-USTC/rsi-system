from pathlib import Path
import json

from rsi_system.core.runtime import RuntimeCoordinator, MissionSpec
from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.workspace import git
from .test_orchestrator import project
from .test_runtime_hardening import hypothesis, candidate


def test_two_coders_same_candidate_preserve_both_implementations(tmp_path):
    root = project(tmp_path)
    coordinator = RuntimeCoordinator(root)
    coordinator.start(MissionSpec('parallel', 'compare alternatives',
                                 authorization={'allow_code_candidates': True}))
    with RuntimeStore(coordinator.store_path) as store:
        store.create_hypothesis('parallel', hypothesis(status='candidate_ready'))
        initial = git(root, 'rev-parse', 'HEAD')
        c = store.create_candidate('parallel', candidate(kind='hypothesis', hypothesis_id='h',
            hypothesis_version=1), code_commit=initial)
        a = store.create_task('parallel', 'coder', 'implementation A', 'test', candidate_id='c0')
        b = store.create_task('parallel', 'coder', 'implementation B', 'test', candidate_id='c0')
        wa, _ = coordinator._coder_workspace(a, c)
        wb, _ = coordinator._coder_workspace(b, c)
        assert wa.path != wb.path and wa.branch != wb.branch
        (wa.path / 'experiments/residual_memory/memory.py').write_text('CANDIDATE = 2\n')
        (wb.path / 'experiments/residual_memory/memory.py').write_text('CANDIDATE = 3\n')
        assert '1' in (root / 'experiments/residual_memory/memory.py').read_text()
        pa = coordinator._prepare_coder(store, a)
        pb = coordinator._prepare_coder(store, b)
        with store.atomic():
            assert coordinator._handle_coder(store, store.mission('parallel'), a, pa)['status'] == 'published'
        with store.atomic():
            result = coordinator._handle_coder(store, store.mission('parallel'), b, pb)
            assert result['status'] == 'requires_reconciliation'
        assert store.candidate('c0')['code_commit'] == pa['source_commit']
        assert git(wb.path, 'rev-parse', 'HEAD') == pb['source_commit']
        rebound, expected = coordinator._coder_workspace(b, store.candidate('c0'))
        assert rebound.path == wb.path and expected == initial
        assert any(e['kind'] == 'candidate_branch_ready' for e in store.events('parallel'))
