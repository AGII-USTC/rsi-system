"""Validated middle-layer records between open Ideas and executable Candidates."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from .schema import safe_id


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must contain nonempty strings")
    return tuple(item.strip() for item in value)


def _structured(value: object, label: str) -> dict[str, Any] | str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        try:
            result = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be JSON serializable") from exc
        if isinstance(result, dict) and result:
            return result
    raise ValueError(f"{label} must be a nonempty string or object")


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    hypothesis_id: str
    mission_id: str
    statement: str
    mechanism: str
    assumptions: tuple[str, ...]
    predictions: tuple[str, ...]
    experimental_design: dict[str, Any] | str
    prior_art_status: dict[str, Any] | str
    idea_ref: str | None = None
    status: str = "developing"

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_id", safe_id(self.hypothesis_id, "hypothesis_id"))
        object.__setattr__(self, "mission_id", safe_id(self.mission_id, "mission_id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "mechanism", _text(self.mechanism, "mechanism"))
        object.__setattr__(self, "assumptions", _sequence(self.assumptions, "assumptions"))
        object.__setattr__(self, "predictions", _sequence(self.predictions, "predictions"))
        object.__setattr__(self, "experimental_design", _structured(self.experimental_design, "experimental_design"))
        object.__setattr__(self, "prior_art_status", _structured(self.prior_art_status, "prior_art_status"))
        if self.idea_ref is not None:
            object.__setattr__(self, "idea_ref", safe_id(self.idea_ref, "idea_ref"))
        if self.status not in {"developing", "testable", "candidate_ready", "paused"}:
            raise ValueError("invalid hypothesis status")

    @classmethod
    def from_payload(cls, mission_id: str, payload: Mapping[str, Any]) -> "HypothesisSpec":
        return cls(
            hypothesis_id=payload.get("hypothesis_id") or payload.get("id") or "",
            mission_id=mission_id,
            statement=payload.get("statement", ""),
            mechanism=payload.get("mechanism", ""),
            assumptions=tuple(payload.get("assumptions", ())),
            predictions=tuple(payload.get("predictions", ())),
            experimental_design=payload.get("experimental_design", {}),
            prior_art_status=payload.get("prior_art_status", {}),
            idea_ref=payload.get("idea_ref"),
            status=payload.get("status", "developing"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {"hypothesis_id": self.hypothesis_id, "mission_id": self.mission_id,
                "statement": self.statement, "mechanism": self.mechanism,
                "assumptions": list(self.assumptions), "predictions": list(self.predictions),
                "experimental_design": self.experimental_design, "prior_art_status": self.prior_art_status,
                "idea_ref": self.idea_ref, "status": self.status}

    @property
    def candidate_ready(self) -> bool:
        return self.status == "candidate_ready"
