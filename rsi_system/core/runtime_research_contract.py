"""Validation and normalization for researcher mandates and checkpoints.

The contract deliberately stores source references as opaque addresses.  The
runtime records what an Agent claimed to read; it does not turn a checkpoint
into a scientific finding or verify the referenced material here.
"""
from __future__ import annotations

from typing import Any, Mapping

from .runtime_types import _json, _object, _text

_MAX_TEXT = 8_000
_MAX_CHECKPOINT_BYTES = 24_000
_MAX_MANDATE_BYTES = 24_000


def _bounded_text(value: Any, label: str) -> str:
    result = _text(value, label)
    if len(result.encode("utf-8")) > _MAX_TEXT:
        raise ValueError(f"{label} exceeds {_MAX_TEXT} bytes")
    return result


def _string_list(value: Any, label: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    result = [_bounded_text(item, label) for item in value]
    if required and not result:
        raise ValueError(f"{label} must contain at least one source")
    return result


def normalize_research_mandate(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    data = _object(value, "research_mandate")
    mode = data.get("mode")
    if mode not in {"direction", "check"}:
        raise ValueError("research_mandate.mode must be direction or check")
    result: dict[str, Any] = {
        "mode": mode,
        "responsibility": _bounded_text(data.get("responsibility"), "research_mandate.responsibility"),
        "constraints": [],
        "observations": [],
        "suggestions": _string_list(data.get("suggestions", []), "research_mandate.suggestions"),
    }
    constraints = data.get("constraints", [])
    if not isinstance(constraints, list):
        raise ValueError("research_mandate.constraints must be a list")
    for item in constraints:
        if not isinstance(item, Mapping):
            raise ValueError("research_mandate constraint must be an object")
        unknown = set(item) - {"statement", "source_ref"}
        if unknown:
            raise ValueError(f"research_mandate constraint has unknown fields: {sorted(unknown)}")
        result["constraints"].append({
            "statement": _bounded_text(item.get("statement"), "research_mandate constraint statement"),
            "source_ref": _bounded_text(item.get("source_ref"), "research_mandate constraint source_ref"),
        })
    observations = data.get("observations", [])
    if not isinstance(observations, list):
        raise ValueError("research_mandate.observations must be a list")
    for item in observations:
        if not isinstance(item, Mapping):
            raise ValueError("research_mandate observation must be an object")
        unknown = set(item) - {"statement", "source_refs"}
        if unknown:
            raise ValueError(f"research_mandate observation has unknown fields: {sorted(unknown)}")
        result["observations"].append({
            "statement": _bounded_text(item.get("statement"), "research_mandate observation statement"),
            "source_refs": _string_list(item.get("source_refs"), "research_mandate observation source_refs", required=True),
        })
    unknown = set(data) - {"mode", "responsibility", "constraints", "observations", "suggestions"}
    if unknown:
        raise ValueError(f"research_mandate has unknown fields: {sorted(unknown)}")
    if len(_json(result).encode("utf-8")) > _MAX_MANDATE_BYTES:
        raise ValueError(f"research_mandate exceeds {_MAX_MANDATE_BYTES} bytes")
    return result


def normalize_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _object(value, "checkpoint")
    changes = data.get("changes", [])
    if not isinstance(changes, list):
        raise ValueError("checkpoint.changes must be a list")
    normalized_changes = []
    for change in changes:
        if not isinstance(change, Mapping):
            raise ValueError("checkpoint change must be an object")
        unknown = set(change) - {"source_refs", "previous_view", "current_view", "reason", "next_test"}
        if unknown:
            raise ValueError(f"checkpoint change has unknown fields: {sorted(unknown)}")
        refs = _string_list(change.get("source_refs"), "checkpoint change source_refs", required=True)
        item = {
            "source_refs": refs,
            "previous_view": _bounded_text(change.get("previous_view"), "checkpoint change previous_view"),
            "current_view": _bounded_text(change.get("current_view"), "checkpoint change current_view"),
            "reason": _bounded_text(change.get("reason"), "checkpoint change reason"),
        }
        if change.get("next_test") is not None:
            item["next_test"] = _bounded_text(change.get("next_test"), "checkpoint change next_test")
        normalized_changes.append(item)
    result = {
        "problem_statement": _bounded_text(data.get("problem_statement"), "checkpoint.problem_statement"),
        "summary": _bounded_text(data.get("summary"), "checkpoint.summary"),
        "changes": normalized_changes,
    }
    if data.get("next_question") is not None:
        result["next_question"] = _bounded_text(data.get("next_question"), "checkpoint.next_question")
    if data.get("unresolved") is not None:
        result["unresolved"] = _bounded_text(data.get("unresolved"), "checkpoint.unresolved")
    unknown = set(data) - {"problem_statement", "summary", "changes", "next_question", "unresolved", "continuation"}
    if unknown:
        raise ValueError(f"checkpoint has unknown fields: {sorted(unknown)}")
    if len(_json(result).encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"checkpoint exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    return result


def normalize_continuation(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    data = _object(value, "checkpoint.continuation")
    # A researcher may explicitly record that a checkpoint needs no follow-up.
    # This is deliberately structured: continuation objectives are opaque user
    # text and must never be interpreted as an opt-out by matching prose.
    requested = data.get("requested", True)
    if type(requested) is not bool:
        raise ValueError("continuation.requested must be a boolean")
    if not requested:
        unknown = set(data) - {"requested"}
        if unknown:
            raise ValueError(f"continuation opt-out has unknown fields: {sorted(unknown)}")
        return {"requested": False}
    result = {"objective": _bounded_text(data.get("objective"), "continuation.objective")}
    after = data.get("after_task_ids", [])
    if not isinstance(after, list) or any(not isinstance(item, str) or not item.strip() for item in after):
        raise ValueError("continuation.after_task_ids must be a list of nonempty strings")
    result["after_task_ids"] = [item.strip() for item in after]
    unknown = set(data) - {"objective", "after_task_ids", "requested"}
    if unknown:
        raise ValueError(f"continuation has unknown fields: {sorted(unknown)}")
    if len(_json(result).encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"continuation exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    return result
