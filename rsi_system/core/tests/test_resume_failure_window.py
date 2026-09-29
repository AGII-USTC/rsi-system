"""Explicit recovery preserves failures without permanently blocking scheduling."""
import pytest

from rsi_system.core.runtime import RuntimeCoordinator, RuntimeStore, MissionSpec
from rsi_system.core import runtime as runtime_module
from rsi_system.core import runtime_store
from .test_orchestrator import project


@pytest.mark.parametrize('curation', [False, True])
def test_resume_schedules_then_new_failures_pause_without_erasing_history(tmp_path, monkeypatch, curation):
    # The entire scenario occurs in one second: timestamps cannot order it.
    monkeypatch.setattr(runtime_store, '_now', lambda: '2026-09-30T00:00:00+00:00')
    rt = RuntimeCoordinator(project(tmp_path))
    rt.start(MissionSpec('m', 'continue research', resources={
        'knowledge_curator': curation, 'max_agent_tasks': 20}))
    role = 'curator' if curation else 'coordinator'
    if curation:
        monkeypatch.setattr(runtime_module, 'pending_record_ids', lambda *_: [1])
    with RuntimeStore(rt.store_path) as store:
        old = []
        for _ in range(2):
            store.create_task('m', role, 'work', 'fixture')
            task = store.claim_task('m')
            store.finish_task(task['task_id'], task['lease_token'], {'error': '503'}, status='failed')
            old.append(store.task(task['task_id']))
        budget = store.remaining_agent_tasks('m')
    assert rt.tick(require_model=False)['status'] == 'paused'
    rt.resume('m', reason='operator reviewed provider outage')
    with RuntimeStore(rt.store_path) as store:
        assert store.failed_tasks('m') == 2
        assert store.failed_tasks('m', since_resume=True) == 0
        assert store.remaining_agent_tasks('m') == budget
        assert [store.task(task['task_id']) for task in old] == old
    for _ in range(2):
        outcome = rt.tick(require_model=False)
        assert outcome['status'] == 'scheduled' and outcome['role'] == role
        with RuntimeStore(rt.store_path) as store:
            task = store.claim_task('m')
            store.finish_task(task['task_id'], task['lease_token'], {'error': '503'}, status='failed')
    assert rt.tick(require_model=False)['status'] == 'paused'
    with RuntimeStore(rt.store_path) as store:
        assert store.failed_tasks('m') == 4
        assert store.failed_tasks('m', since_resume=True, role=role) == 2
        assert [store.task(task['task_id']) for task in old] == old


def test_task_queued_before_resume_but_claimed_after_counts(tmp_path):
    rt = RuntimeCoordinator(project(tmp_path))
    rt.start(MissionSpec('m', 'research'))
    with RuntimeStore(rt.store_path) as store:
        store.create_task('m', 'researcher', 'work', 'fixture')
        store.set_mission_status('m', 'paused', reason='operator')
        store.resume_mission('m', reason='operator resumed')
        task = store.claim_task('m')
        store.finish_task(task['task_id'], task['lease_token'], {'error': '503'}, status='failed')
        assert store.failed_tasks('m', since_resume=True) == 1
