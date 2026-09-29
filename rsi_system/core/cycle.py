"""Bridge durable run evidence to candidate knowledge, with retryable writeback."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sys

from .knowledge import write_candidate
from .provenance import sha256_file
from .store import ExperimentStore
from .worker import run_once


def reconcile_knowledge(store: ExperimentStore, root: Path) -> tuple[list[dict], list[dict]]:
    written, errors = [], []
    for record in store.list():
        run = record.get("run")
        if record["status"] not in {"completed", "failed", "interrupted", "analyzed"} or not run:
            continue
        try:
            folder = root / ".rsi" / "knowledge-evidence"
            folder.mkdir(parents=True, exist_ok=True)
            evidence_path = folder / f"{record['experiment_id']}.json"
            # Copy database evidence to a stable, independently inspectable file.
            payload = json.dumps(run, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            if evidence_path.exists():
                if evidence_path.read_text(encoding="utf-8") != payload:
                    raise ValueError("stored knowledge evidence differs from the run record")
            else:
                temporary = evidence_path.with_suffix(".tmp")
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(evidence_path)
            evidence = [{"path": evidence_path.relative_to(root).as_posix(), "sha256": sha256_file(evidence_path)}]
            if run.get("evidence_path"):
                output = Path(run["evidence_path"]).resolve()
                evidence.append({"path": output.relative_to(root).as_posix(), "sha256": run["evidence_sha256"]})
            status = run["status"]
            summary = f"{record['protocol_id']}: {status}. "
            summary += ("Recorded protocol metrics; scientific interpretation awaits review."
                        if status == "completed" else str(run.get("error", "Execution did not complete."))[:300])
            result = write_candidate(root, {"experiment_id": record["experiment_id"], "status": status,
                                            "summary": summary, "evidence": evidence})
            if result["created"]:
                written.append(result)
        except (OSError, ValueError, KeyError) as exc:
            errors.append({"experiment_id": record["experiment_id"], "error": str(exc)})
    return written, errors


def run_cycle(store: ExperimentStore, root: Path, run_root: Path, *, python: str = sys.executable) -> dict:
    root = root.resolve()
    if not run_root.resolve().is_relative_to(root):
        raise ValueError("run-root must be inside project root so knowledge can verify evidence")
    lock_path = store.path.with_suffix(store.path.suffix + ".cycle.lock")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"run": None, "written": [], "errors": []}
        run = run_once(store, root, run_root, python=python)
        written, errors = reconcile_knowledge(store, root)
        return {"run": run, "written": written, "errors": errors}
