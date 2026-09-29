"""Durable local Runner intent and recovery classification.

The intent is written before a subprocess is launched.  It is deliberately
separate from the scientific receipt: a receipt is only terminal after its
input and artifact hashes validate.  A started intent without a valid receipt
is therefore *uncertain* and must never be silently replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping

from .pi_agent import atomic_json

SCHEMA_VERSION = 1
STAGES = {"prepared", "started", "receipt_ready", "completed", "uncertain"}


def stable_runner_id(task_id: str) -> str:
    return "run-" + hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:28]


def input_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def intent_path(directory: Path | str) -> Path:
    return Path(directory) / "execution_intent.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def write_intent(directory: Path | str, *, run_id: str, task_id: str,
                 input_sha256: str, executor: str = "local",
                 attempt: int = 1, stage: str = "prepared", **fields: Any) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"invalid Runner intent stage: {stage}")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = intent_path(directory)
    old = _read(path) if path.exists() else None
    if path.exists() and old is None:
        raise ValueError("Runner intent is unreadable or malformed")
    if old:
        immutable = (old.get("run_id"), old.get("task_id"), old.get("input_sha256"))
        if immutable != (run_id, task_id, input_sha256):
            raise ValueError("Runner intent identity or frozen input changed")
        # Never move a terminal/uncertain intent backwards.
        if old.get("stage") in {"completed", "uncertain"} and stage != old.get("stage"):
            return old
        if old.get("stage") == "started" and stage == "prepared":
            return old
        attempt = max(int(old.get("attempt", 1)), int(attempt))
    record = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "task_id": task_id,
              "input_sha256": input_sha256, "executor": executor, "attempt": attempt,
              "stage": stage, "updated_at_unix": time.time(), **fields}
    atomic_json(path, record)
    return record


def mark_started(directory: Path | str, *, pid: int | None = None,
                 process_start_token: str | None = None, **fields: Any) -> dict[str, Any]:
    path = intent_path(directory)
    old = _read(path)
    if not old:
        raise ValueError("Runner intent missing before start")
    if old.get("stage") in {"completed", "uncertain"}:
        return old
    old.update({"stage": "started", "started_at_unix": old.get("started_at_unix", time.time()),
                "updated_at_unix": time.time()})
    if pid is not None:
        old["process_pid"] = pid
    if process_start_token is not None:
        old["process_start_token"] = process_start_token
    old.update(fields)
    atomic_json(path, old)
    return old


def mark_receipt_ready(directory: Path | str, *, receipt_sha256: str,
                       **fields: Any) -> dict[str, Any]:
    path = intent_path(directory)
    old = _read(path)
    if not old:
        raise ValueError("Runner intent missing before receipt")
    old.update({"stage": "receipt_ready", "receipt_sha256": receipt_sha256,
                "receipt_ready_at_unix": time.time(), "updated_at_unix": time.time(), **fields})
    atomic_json(path, old)
    return old


def _process_matches(record: Mapping[str, Any]) -> bool:
    pid, token = record.get("process_pid"), record.get("process_start_token")
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    if not token:
        return True
    try:
        current = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return True  # non-Linux: pid liveness is the available check
    return current == token


def _valid_receipt(receipt: Path, record: Mapping[str, Any]) -> bool:
    if not receipt.is_file():
        return False
    saved = _read(receipt)
    if not saved or saved.get("status") not in {"completed", "failed"} or saved.get("run_id") not in {None, record.get("run_id")}:
        return False
    expected = record.get("receipt_sha256")
    return expected is None or hashlib.sha256(receipt.read_bytes()).hexdigest() == expected


def classify_runner_recovery(directory: Path | str, *, receipt: Path | str | None = None) -> dict[str, Any]:
    """Classify a local Runner directory without launching anything.

    ``active`` means the recorded executor is still alive.  ``uncertain`` is
    an interrupted started attempt and is terminal for automatic recovery.
    ``receipt_ready``/``completed`` are safe to reingest only when receipt hash
    validation succeeds.  The caller decides how to persist these outcomes.
    """
    directory = Path(directory)
    path = intent_path(directory)
    if not path.exists():
        # A pre-existing directory without an intent is legacy/ambiguous.  An
        # actually empty directory is the only safe launch case.
        if directory.exists() and any(directory.iterdir()):
            return {"state": "missing_intent", "safe_to_launch": False, "record": None}
        return {"state": "missing_intent", "safe_to_launch": True, "record": None}
    record = _read(path)
    if record is None:
        return {"state": "invalid_intent", "safe_to_launch": False, "record": None}
    receipt_path = Path(receipt) if receipt is not None else directory / "run_record.json"
    if record.get("stage") in {"receipt_ready", "completed"}:
        valid = _valid_receipt(receipt_path, record)
        return {"state": "receipt_ready" if valid else "invalid_receipt",
                "safe_to_launch": False, "reingest": valid, "record": record,
                "receipt": str(receipt_path)}
    if record.get("stage") == "started":
        # The subprocess may have exited cleanly just before the wrapper died.
        # Adopt a verified receipt before declaring uncertainty.
        if _valid_receipt(receipt_path, record):
            return {"state": "receipt_ready", "safe_to_launch": False,
                    "reingest": True, "record": record, "receipt": str(receipt_path)}
        active = _process_matches(record)
        return {"state": "active" if active else "uncertain", "safe_to_launch": False,
                "active_executor": active, "record": record}
    if record.get("stage") == "prepared":
        return {"state": "prepared", "safe_to_launch": True, "record": record}
    return {"state": "uncertain", "safe_to_launch": False, "record": record}


def mark_uncertain(directory: Path | str, *, reason: str = "executor interrupted") -> dict[str, Any]:
    path = intent_path(directory)
    old = _read(path)
    if not old:
        raise ValueError("Runner intent missing")
    old.update({"stage": "uncertain", "uncertainty_reason": reason,
                "updated_at_unix": time.time()})
    atomic_json(path, old)
    return old
