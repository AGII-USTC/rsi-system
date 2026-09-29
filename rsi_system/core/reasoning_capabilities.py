"""Pi thinking-level capability helpers shared by configuration and runtime.

Pi describes model capabilities with ``reasoning`` and ``thinkingLevelMap``.
The map is tri-state: an omitted entry uses Pi's standard mapping, a string is
an explicitly supported provider value, and ``null`` marks a level unsupported.
Keeping this interpretation in one small, dependency-free module prevents the
dashboard and runtime from drifting apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# Keep Pi's ordering.  ``off`` is a real Pi value; ``default`` is represented
# by a missing per-model override and is therefore intentionally not listed.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
STANDARD_THINKING_LEVELS = ("minimal", "low", "medium", "high")
EXTENDED_THINKING_LEVELS = ("xhigh", "max")


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """A model's known Pi levels.

    ``levels`` is ``None`` when the metadata does not say whether the model
    supports reasoning.  This is deliberately different from ``("off",)``:
    the former is unknown and the latter is a known non-reasoning model.
    """

    known: bool
    levels: tuple[str, ...] | None
    source: str = "unknown"


def normalize_levels(value: object) -> tuple[str, ...] | None:
    """Validate and normalize a user supplied level collection.

    ``None`` means no declaration.  Invalid values return ``None`` so callers
    can produce an HTTP validation error without accidentally treating a bad
    declaration as an empty, non-reasoning model.
    """

    if not isinstance(value, (list, tuple)):
        return None
    if any(not isinstance(item, str) or item not in THINKING_LEVELS for item in value):
        return None
    unique = set(value)
    return tuple(level for level in THINKING_LEVELS if level in unique)


def _explicit_levels(model: Mapping[str, Any]) -> tuple[str, ...] | None:
    # Accept both the dashboard's snake-case API-shaped field and the native
    # Pi camel-case spelling for integrations that persist the declaration
    # without translating it first.
    for key in ("thinking_levels", "thinkingLevels"):
        if key in model:
            return normalize_levels(model[key])
    return None


def model_capabilities(model: object) -> ModelCapabilities:
    """Derive supported Pi levels from model metadata without name heuristics."""

    if not isinstance(model, Mapping):
        return ModelCapabilities(False, None)

    explicit = _explicit_levels(model)
    if explicit is not None:
        return ModelCapabilities(True, explicit, "explicit")

    reasoning = model.get("reasoning")
    if reasoning is False:
        return ModelCapabilities(True, ("off",), "pi")
    if reasoning is True:
        mapping = model.get("thinkingLevelMap")
        if not isinstance(mapping, Mapping):
            mapping = {}
        levels: list[str] = []
        for level in THINKING_LEVELS:
            # ``null`` explicitly hides a level.  Pi only exposes xhigh/max
            # when a mapping key is present; omitted standard entries use the
            # provider's defaults.
            marker = object()
            mapped = mapping.get(level, marker)
            if mapped is None or (mapped is not marker and not isinstance(mapped, str)):
                continue
            if level in EXTENDED_THINKING_LEVELS and level not in mapping:
                continue
            levels.append(level)
        return ModelCapabilities(True, tuple(levels), "pi")

    # Older dashboard versions persisted only a selected tier.  Treat that
    # explicit choice as a one-level declaration so those configurations keep
    # working, while a model with no selected tier remains unknown and safe.
    selected = model.get("thinking")
    if selected in THINKING_LEVELS:
        return ModelCapabilities(True, (selected,), "legacy")
    return ModelCapabilities(False, None)


def resolve_level(requested: object, capabilities: ModelCapabilities) -> str | None:
    """Return a level Pi can accept, or ``None`` when capability is unknown.

    The fallback mirrors Pi's clamp behavior but never emits an unsupported
    value.  Unknown models intentionally return ``None`` so the caller can omit
    ``--thinking`` and let Pi use its own model default.
    """

    if not capabilities.known or capabilities.levels is None:
        # ``off`` is a Pi-wide disable semantic and does not require guessing
        # a provider-specific effort value. Other levels remain omitted until
        # capability metadata is known.
        return "off" if requested == "off" else None
    levels = capabilities.levels
    if not levels:
        return None
    requested = requested if requested in THINKING_LEVELS else "off"
    if requested in levels:
        return requested
    try:
        index = THINKING_LEVELS.index(requested)
    except ValueError:
        index = 0
    # Prefer the next supported level, then the nearest lower one.  This is
    # the same direction Pi uses when clamping a requested level.
    for candidate in THINKING_LEVELS[index:]:
        if candidate in levels:
            return candidate
    for candidate in reversed(THINKING_LEVELS[:index]):
        if candidate in levels:
            return candidate
    return levels[0]


def capability_record(capabilities: ModelCapabilities) -> dict[str, object]:
    """Return the credential-free representation stored for runtime lookup."""

    return {"known": capabilities.known,
            "levels": list(capabilities.levels) if capabilities.levels is not None else None,
            "source": capabilities.source}
