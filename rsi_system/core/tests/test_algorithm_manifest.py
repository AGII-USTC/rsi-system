"""Tests for the thin target-algorithm evolution metadata layer."""

import pytest

from rsi_system.core.algorithm_manifest import manifest_sha256, validate_manifest
from rsi_system.core.runtime import MissionSpec, RuntimeStore


def _baseline(algorithm=None):
    return {
        "candidate_id": "c0",
        "kind": "baseline",
        "baseline_reason": "unchanged reference",
        "hypothesis": "reference algorithm",
        "expected_observation": "reference metric",
        "protocol_id": "runtime-harness-smoke-v1",
        "config": {"steps": 3},
        "algorithm": algorithm or {},
    }


def test_manifest_is_canonical_and_hash_stable():
    left = {"family": "toy", "components": {"reader": "linear@v1"}, "schema_version": 1}
    right = {"schema_version": 1, "components": {"reader": "linear@v1"}, "family": "toy"}
    assert validate_manifest(left) == validate_manifest(right)
    assert manifest_sha256(left) == manifest_sha256(right)


def test_manifest_rejects_evaluator_boundary_changes():
    with pytest.raises(ValueError, match="fixed evaluator field"):
        validate_manifest({"family": "toy", "evaluator": "mutable"})


def test_system_change_uses_the_same_manifest_surface():
    manifest = {
        "family": "rsi-control-plane",
        "optimization_scope": "system",
        "components": {"runtime.scheduler": "lease-aware@v2", "runtime.admission": "lineage@v1"},
        "mutation": {"operation": "replace", "component": "runtime.scheduler"},
        "ablation": {"group": "scheduler-policy", "condition": "without-lease-aware"},
    }
    saved = validate_manifest(manifest)
    assert saved["optimization_scope"] == "system"
    assert saved["components"]["runtime.scheduler"] == "lease-aware@v2"


def test_candidate_persists_algorithm_manifest_separately_from_protocol_config(tmp_path):
    manifest = {
        "schema_version": 1,
        "family": "toy",
        "components": {"reader": "linear@v1"},
        "mutation": {"operation": "replace", "component": "reader"},
        "ablation": {"group": "reader-1", "condition": "full", "active_components": ["reader"]},
    }
    with RuntimeStore(tmp_path / "state.sqlite3") as store:
        store.create_mission(MissionSpec("m", "evolve a target algorithm"))
        candidate = store.create_candidate("m", _baseline(manifest))
        saved = store.candidate(candidate["candidate_id"])
        assert saved["config"] == {"steps": 3}
        assert saved["algorithm"] == validate_manifest(manifest)
        assert store.events("m")[-1]["payload"]["algorithm_sha256"] == manifest_sha256(manifest)
