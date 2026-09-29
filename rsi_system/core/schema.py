"""Validated records shared by the RSI registry and future runners."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from collections.abc import Mapping, Sequence


STATUSES = (
    "proposed",
    "awaiting_approval",
    "approved",
    "queued",
    "running",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "analyzed",
    "promoted",
    "archived",
)

ALLOWED_TARGETS = ("local", "server")

TRANSITIONS = {
    "proposed": {"awaiting_approval", "cancelled"},
    "awaiting_approval": {"approved", "cancelled"},
    "approved": {"queued", "cancelled"},
    "queued": {"running", "cancelled"},
    "running": {"completed", "failed", "interrupted", "cancelled"},
    "completed": {"analyzed"},
    "failed": {"analyzed"},
    "interrupted": {"analyzed"},
    "analyzed": {"promoted", "archived"},
    "promoted": {"archived"},
    "cancelled": {"archived"},
    "archived": set(),
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def safe_id(value: object, label: str = "experiment_id") -> str:
    value = _text(value, label)
    if len(value) > 96 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None or value in {".", ".."}:
        raise ValueError(f"{label} must be a safe identifier (1-96 ASCII letters, digits, _, ., -)")
    return value


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
        result = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-serializable") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be an object")
    return result


def _command(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("command must be a sequence of strings")
    result = tuple(_text(item, "command item") for item in value)
    return result


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """A bounded proposal that can later be executed by a local or server worker."""

    experiment_id: str
    hypothesis: str
    protocol_id: str
    config: dict[str, object]
    parent_id: str | None = None
    dataset_version: str = "unspecified"
    evaluator_id: str = "unspecified"
    execution_target: str = "server"
    command: tuple[str, ...] = ()
    resource_budget: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", safe_id(self.experiment_id))
        object.__setattr__(self, "hypothesis", _text(self.hypothesis, "hypothesis"))
        object.__setattr__(self, "protocol_id", _text(self.protocol_id, "protocol_id"))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", safe_id(self.parent_id, "parent_id"))
        object.__setattr__(self, "dataset_version", _text(self.dataset_version, "dataset_version"))
        object.__setattr__(self, "evaluator_id", _text(self.evaluator_id, "evaluator_id"))
        if self.execution_target not in ALLOWED_TARGETS:
            raise ValueError(f"execution_target must be one of {ALLOWED_TARGETS}")
        object.__setattr__(self, "config", _mapping(self.config, "config"))
        object.__setattr__(self, "command", _command(self.command))
        budget = {} if self.resource_budget is None else _mapping(self.resource_budget, "resource_budget")
        object.__setattr__(self, "resource_budget", budget)

    def to_json_fields(self) -> dict[str, str]:
        return {
            "config_json": json.dumps(self.config, sort_keys=True, allow_nan=False),
            "command_json": json.dumps(list(self.command), ensure_ascii=False),
            "resource_budget_json": json.dumps(self.resource_budget, sort_keys=True, allow_nan=False),
        }


def validate_transition(current: str, target: str) -> None:
    if current not in STATUSES or target not in STATUSES:
        raise ValueError("unknown experiment status")
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid experiment transition: {current} -> {target}")
