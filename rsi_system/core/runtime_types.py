"""Mission input and shared runtime value validation."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping
from .schema import safe_id
from .reasoning_capabilities import THINKING_LEVELS


RUNTIME_SCHEMA_VERSION = 4
COLLABORATOR_ROLES = ("researcher", "hypothesis", "coder", "runner", "analyzer", "critic", "curator")
ROLES = ("coordinator", *COLLABORATOR_ROLES)
TASK_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
CANDIDATE_STATUSES = ("proposed", "coding", "ready", "running", "analyzed", "paused", "promoted")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        result = json.loads(_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be an object")
    return result


def _text(value: object, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class MissionSpec:
    """User-owned research objective and execution boundary."""

    mission_id: str
    objective: str
    background: str = ""
    known_evidence: tuple[str, ...] = ()
    resources: dict[str, Any] | None = None
    stop_conditions: tuple[str, ...] = ()
    authorization: dict[str, Any] | None = None
    evaluator: dict[str, Any] | None = None
    mode: str = "open"

    def __post_init__(self) -> None:
        object.__setattr__(self, "mission_id", safe_id(self.mission_id, "mission_id"))
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        object.__setattr__(self, "background", _text(self.background, "background", required=False))
        for name in ("known_evidence", "stop_conditions"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"{name} must be a sequence of nonempty strings")
            object.__setattr__(self, name, tuple(item.strip() for item in values))
        for name in ("resources", "authorization", "evaluator"):
            value = getattr(self, name)
            object.__setattr__(self, name, {} if value is None else _object(value, name))
        for name in ("max_agent_tasks", "max_seconds_per_run", "max_runs"):
            value = self.resources.get(name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.resources.get("agent_thinking", "xhigh") not in set(THINKING_LEVELS):
            raise ValueError("invalid agent_thinking")
        if "exploration_review" in self.resources and type(self.resources["exploration_review"]) is not bool:
            raise ValueError("exploration_review must be a boolean")
        if self.mode not in {"open", "delivery", "confirmation"}:
            raise ValueError("mode must be open, delivery, or confirmation")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MissionSpec":
        return cls(
            mission_id=payload.get("mission_id") or payload.get("id") or "",
            objective=payload.get("objective", ""),
            background=payload.get("background", ""),
            known_evidence=tuple(payload.get("known_evidence", ())),
            resources=payload.get("resources", {}),
            stop_conditions=tuple(payload.get("stop_conditions", ())),
            authorization=payload.get("authorization", {}),
            evaluator=payload.get("evaluator", {}),
            mode=payload.get("mode", "open"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "objective": self.objective,
                "background": self.background, "known_evidence": list(self.known_evidence),
                "resources": self.resources, "stop_conditions": list(self.stop_conditions),
                "authorization": self.authorization, "evaluator": self.evaluator,
                "mode": self.mode}
