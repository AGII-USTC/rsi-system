"""Small, domain-neutral manifests for evolving algorithms and the RSI system.

The runtime already treats the evaluator configuration and the candidate
source commit as separate values.  This module adds the missing third value:
the declarative description of the change itself.  A runtime scheduler,
admission rule, memory policy, or coordinator strategy is an algorithmic
component of the system and uses this same surface.  The manifest is metadata
and provenance; execution remains owned by the existing Runner.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = 1
OPTIMIZATION_SCOPES = frozenset({"algorithm", "system", "algorithm_and_system"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*\Z")

# These belong to Mission/Evaluator and must never be smuggled in as an
# algorithm mutation.  Keeping this list here also gives the coordinator a
# single place to enforce the boundary.
_FIXED_FIELDS = {
    "evaluator", "evaluator_id", "protocol", "protocol_id", "dataset",
    "dataset_version", "test", "test_split", "success_definition",
    "success_metric", "environment", "checkpoint", "checkpoint_revision",
    "safety_limits", "hardware_mode", "command", "resource_budget",
}


def _canonical(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        result = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be an object")
    return result


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or not _NAME.fullmatch(value.strip()):
        raise ValueError(f"{label} must be a safe algorithm name")
    return value.strip()


def validate_manifest(value: object) -> dict[str, Any]:
    """Return a canonical manifest suitable for persistence and hashing.

    An empty manifest is allowed for legacy candidates and unchanged
    baselines.  Once a manifest is supplied, its shape is deliberately small:
    module semantics remain domain-owned while the runtime checks identity,
    mutation provenance, and the evaluator boundary.
    """
    manifest = _canonical(value, "algorithm")
    if not manifest:
        return {}
    version = manifest.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError(f"algorithm schema_version must be {SCHEMA_VERSION}")
    family = _name(manifest.get("family", "opaque"), "algorithm family")
    optimization_scope = manifest.get("optimization_scope", "algorithm")
    if not isinstance(optimization_scope, str) or optimization_scope not in OPTIMIZATION_SCOPES:
        raise ValueError("algorithm optimization_scope must be algorithm, system, or algorithm_and_system")
    components = manifest.get("components", {})
    if not isinstance(components, Mapping):
        raise ValueError("algorithm components must be an object")
    for component, implementation in components.items():
        _name(component, "algorithm component")
        if isinstance(implementation, str):
            _name(implementation, f"implementation for {component}")
        elif not isinstance(implementation, Mapping):
            raise ValueError(f"implementation for {component} must be a name or object")

    for field in _FIXED_FIELDS:
        if field in manifest:
            raise ValueError(f"algorithm cannot change fixed evaluator field: {field}")

    mutation = manifest.get("mutation")
    if mutation is not None:
        mutation = _canonical(mutation, "algorithm mutation")
        operation = _name(mutation.get("operation"), "algorithm mutation operation")
        if operation not in {"tune", "replace", "insert", "remove", "compose"}:
            raise ValueError("algorithm mutation operation is unsupported")
        if operation != "compose":
            _name(mutation.get("component"), "algorithm mutation component")
        manifest["mutation"] = mutation

    ablation = manifest.get("ablation")
    if ablation is not None:
        ablation = _canonical(ablation, "algorithm ablation")
        if "group" in ablation:
            _name(ablation["group"], "algorithm ablation group")
        if "condition" in ablation:
            _name(ablation["condition"], "algorithm ablation condition")
        active = ablation.get("active_components")
        if active is not None:
            if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
                raise ValueError("algorithm active_components must be a list of names")
            ablation["active_components"] = [_name(item, "active component") for item in active]
        manifest["ablation"] = ablation

    manifest["schema_version"] = SCHEMA_VERSION
    manifest["family"] = family
    manifest["optimization_scope"] = optimization_scope
    manifest["components"] = dict(components)
    return manifest


def manifest_json(value: object) -> str:
    return json.dumps(validate_manifest(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def manifest_sha256(value: object) -> str:
    return hashlib.sha256(manifest_json(value).encode("utf-8")).hexdigest()
