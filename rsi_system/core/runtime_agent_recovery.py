"""Durable classification and bounded recovery metadata for model turns.

The runtime keeps the task and tool receipts as the source of truth.  This
module only derives a small, serialisable description used for retry policy
and for the next Agent turn; it never treats a retry as scientific evidence.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


RETRYABLE_KINDS = {
    "transport_retryable",
    "provider_rate_limit",
    "provider_server_error",
    "partial_model_output",
    "tool_error",
    "output_parse_error",
    "schema_error",
    "semantic_reference_error",
}


def classify_agent_failure(exc: BaseException, *, protocol: bool = False,
                           tool_pending: bool = False) -> dict[str, Any]:
    """Return stable failure metadata without relying on one exact message.

    Provider and parser messages vary across Pi providers.  The runtime uses
    this classification for bounded continuation only; the original string
    remains in the task failure receipt.
    """
    text = str(exc)
    lowered = text.lower()
    if protocol:
        if "json" in lowered or "response must be one json" in lowered:
            kind = "output_parse_error"
        elif "reference" in lowered or "challenge" in lowered or "reframe" in lowered:
            kind = "semantic_reference_error"
        else:
            kind = "schema_error"
    elif tool_pending:
        kind = "tool_error"
    elif any(marker in lowered for marker in ("rate limit", "429", "too many requests")):
        kind = "provider_rate_limit"
    elif any(marker in lowered for marker in (" 5", "503", "502", "504", "service unavailable",
                                               "upstream error", "overloaded")):
        kind = "provider_server_error"
    elif isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
            marker in lowered for marker in ("transport", "timed out", "timeout", "connection reset",
                                              "connection refused", "provider unavailable", "api unavailable")):
        kind = "transport_retryable"
    elif "did not finish" in lowered or "stopreason" in lowered or "tooluse" in lowered:
        kind = "partial_model_output"
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        # A generic ValueError from a researcher is a normal failed attempt;
        # only a validated model-facing protocol path is eligible for repair.
        kind = "schema_error" if protocol else "execution_error"
    else:
        kind = "execution_error"
    return {
        "kind": kind,
        "message": text[:4000],
        "retryable": kind in RETRYABLE_KINDS,
        "next_action": "repair_same_session" if kind in {
            "output_parse_error", "schema_error", "semantic_reference_error"
        } else ("retry_same_session" if kind in RETRYABLE_KINDS else "bounded_failure_review"),
        "evidence_status": "execution failure only; no scientific claim",
    }


def session_id_for(task: Mapping[str, Any]) -> str:
    """Resolve the logical Agent session carried across continuation tasks."""
    continuation = (task.get("scope") or {}).get("continuation") or {}
    session_id = continuation.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id
    return f"agent-session-{task['task_id']}"


def session_dir_for(task: Mapping[str, Any], runtime_root: Path) -> Path:
    """Return a writable, durable transcript directory for this logical session."""
    continuation = (task.get("scope") or {}).get("continuation") or {}
    configured = continuation.get("session_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).resolve()
    return (Path(runtime_root).resolve() / "tasks" / task["task_id"] /
            "workspace" / ".pi-session").resolve()


def continuation_metadata(task: Mapping[str, Any], *, mode: str,
                          failure: Mapping[str, Any] | None = None,
                          event_cursor: int | None = None,
                          session_dir: str | None = None) -> dict[str, Any]:
    """Build the small handoff object; full history stays in immutable files."""
    return {
        "objective": str(task.get("objective") or "Continue the previous Agent turn"),
        "mode": mode,
        "session_id": session_id_for(task),
        "session_dir": session_dir,
        "source_task_id": task["task_id"],
        "after_task_ids": [],
        "event_cursor": event_cursor,
        "failure": dict(failure or {}),
    }
