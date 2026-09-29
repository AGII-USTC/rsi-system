"""Structured proposal input/output; no model invocation or autonomous launch."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from .knowledge import context as knowledge_context
from .schema import ExperimentSpec


_FIELDS = {
    "experiment_id", "hypothesis", "protocol_id", "config", "parent_id",
    "dataset_version", "evaluator_id", "execution_target", "command", "resource_budget",
}
_REQUIRED = {"experiment_id", "hypothesis", "protocol_id", "config", "dataset_version", "evaluator_id"}
_FIXED_CONFIG_KEYS = {
    "protocol", "protocol_id", "evaluator", "evaluator_id", "evaluation", "eval",
    "test", "test_split", "dataset", "dataset_version", "success_definition",
    "success_metric", "environment", "checkpoint", "checkpoint_revision", "safety_limits",
    "hardware_mode",
}


def _check_config_boundary(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _FIXED_CONFIG_KEYS:
                raise ValueError(f"proposal config cannot set fixed field: {key}")
            _check_config_boundary(nested)
    elif isinstance(value, list):
        for nested in value:
            _check_config_boundary(nested)


def proposal_context(root: Path | str, query: str, limit: int = 5) -> dict[str, object]:
    """Provide source excerpts and standing project priorities for a proposer."""
    return {
        "knowledge": knowledge_context(root, query, limit),
        "priorities": [
            "External event monitoring is paused.",
            "Qualify a reliable nominal VLA base under a fixed evaluation before searching Memory adaptation variants.",
            "Memory prediction gains do not establish closed-loop gains; compare correct Memory with matched controls.",
        ],
        "constraints": [
            "The evaluator, test split, success definition, and fixed protocol are not proposal variables.",
            "A proposal requires approval under the project's delegated acceptance policy before execution.",
            "Source excerpts are lexical matches, not semantic retrieval or verified claims about a new experiment.",
        ],
    }


def import_proposal(
    payload: Mapping[str, object] | str,
    *,
    fixed_protocol_id: str,
    fixed_dataset_version: str,
    fixed_evaluator_id: str,
    fixed_command: tuple[str, ...] = (),
) -> ExperimentSpec:
    """Validate a JSON proposal against the caller's immutable evaluation contract."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("proposal must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("proposal must be a JSON object")
    extra = set(payload) - _FIELDS
    missing = _REQUIRED - set(payload)
    if extra or missing:
        raise ValueError(f"proposal fields: missing={sorted(missing)}, unknown={sorted(extra)}")
    fixed = {
        "protocol_id": fixed_protocol_id,
        "dataset_version": fixed_dataset_version,
        "evaluator_id": fixed_evaluator_id,
    }
    if any(not isinstance(value, str) or not value.strip() or value == "unspecified" for value in fixed.values()):
        raise ValueError("fixed evaluation contract must contain explicit identifiers")
    for key, value in fixed.items():
        if payload[key] != value:
            raise ValueError(f"proposal cannot change fixed {key}")
    _check_config_boundary(payload["config"])
    if "command" in payload:
        command = payload["command"]
        if not isinstance(command, (list, tuple)) or tuple(command) != fixed_command:
            raise ValueError("proposal cannot change fixed command")
    fields = dict(payload)
    fields["command"] = fixed_command
    return ExperimentSpec(**fields)


def export_proposal(spec: ExperimentSpec) -> dict[str, object]:
    """Serialize a validated spec for a future provider or human review UI."""
    return {
        "experiment_id": spec.experiment_id,
        "hypothesis": spec.hypothesis,
        "protocol_id": spec.protocol_id,
        "config": spec.config,
        "parent_id": spec.parent_id,
        "dataset_version": spec.dataset_version,
        "evaluator_id": spec.evaluator_id,
        "execution_target": spec.execution_target,
        "command": list(spec.command),
        "resource_budget": spec.resource_budget,
    }
