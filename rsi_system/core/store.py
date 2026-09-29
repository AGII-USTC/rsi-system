"""SQLite-backed experiment registry with explicit state transitions."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .schema import ExperimentSpec, STATUSES, safe_id, validate_transition


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ExperimentStore:
    """Durable metadata only; run outputs stay in the configured artifact directory."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                parent_id TEXT REFERENCES experiments(experiment_id),
                hypothesis TEXT NOT NULL,
                protocol_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                evaluator_id TEXT NOT NULL,
                execution_target TEXT NOT NULL,
                command_json TEXT NOT NULL,
                resource_budget_json TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                from_status TEXT,
                to_status TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experiments_status_idx ON experiments(status);
            CREATE INDEX IF NOT EXISTS experiments_parent_idx ON experiments(parent_id);
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        version = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        if version != str(SCHEMA_VERSION):
            raise ValueError(f"unsupported registry schema version: {version}")
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(experiments)")}
        if "run_json" not in columns:
            self._connection.execute("ALTER TABLE experiments ADD COLUMN run_json TEXT")
        self._connection.commit()

    def create(self, spec: ExperimentSpec, *, status: str = "proposed") -> dict[str, Any]:
        if status != "proposed":
            raise ValueError("new experiments must start as proposed")
        if spec.parent_id is not None and self.get(spec.parent_id) is None:
            raise ValueError(f"parent experiment does not exist: {spec.parent_id}")
        now = _now()
        fields = spec.to_json_fields()
        try:
            self._connection.execute(
                """INSERT INTO experiments(
                    experiment_id, parent_id, hypothesis, protocol_id, config_json,
                    dataset_version, evaluator_id, execution_target, command_json,
                    resource_budget_json, status, decision_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (spec.experiment_id, spec.parent_id, spec.hypothesis, spec.protocol_id,
                 fields["config_json"], spec.dataset_version, spec.evaluator_id,
                 spec.execution_target, fields["command_json"], fields["resource_budget_json"],
                 status, now, now),
            )
            self._connection.execute(
                "INSERT INTO experiment_events(experiment_id, from_status, to_status, note, created_at) VALUES (?, NULL, ?, ?, ?)",
                (spec.experiment_id, status, "created", now),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise ValueError(f"could not create experiment {spec.experiment_id}") from exc
        return self.get(spec.experiment_id)  # type: ignore[return-value]

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        safe_id(experiment_id)
        row = self._connection.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return self._decode(row) if row is not None else None

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError("unknown experiment status")
        query = "SELECT * FROM experiments"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at, experiment_id"
        return [self._decode(row) for row in self._connection.execute(query, params)]

    def transition(self, experiment_id: str, target: str, *, note: str | None = None) -> dict[str, Any]:
        safe_id(experiment_id)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute("SELECT status FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown experiment: {experiment_id}")
            validate_transition(row["status"], target)
            self._set_status(experiment_id, row["status"], target, note)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.get(experiment_id)  # type: ignore[return-value]

    def _set_status(self, experiment_id: str, current: str, target: str, note: str | None) -> None:
        now = _now()
        self._connection.execute(
            "UPDATE experiments SET status = ?, decision_note = ?, updated_at = ? WHERE experiment_id = ? AND status = ?",
            (target, note, now, experiment_id, current),
        )
        self._connection.execute(
            "INSERT INTO experiment_events(experiment_id, from_status, to_status, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (experiment_id, current, target, note, now),
        )

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the oldest approved, queued server experiment."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT experiment_id FROM experiments WHERE status = 'queued' AND execution_target = 'server' "
                "AND EXISTS (SELECT 1 FROM experiment_events e WHERE e.experiment_id = experiments.experiment_id "
                "AND e.to_status = 'approved') "
                "ORDER BY created_at, experiment_id LIMIT 1"
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            experiment_id = row["experiment_id"]
            self._set_status(experiment_id, "queued", "running", "worker claimed")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.get(experiment_id)

    def recover_running(self, *, note: str = "worker restarted before completion") -> list[str]:
        """Call while holding the exclusive worker lock."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            ids = [row[0] for row in self._connection.execute(
                "SELECT experiment_id FROM experiments WHERE status = 'running' ORDER BY experiment_id"
            )]
            for experiment_id in ids:
                self._set_status(experiment_id, "running", "interrupted", note)
            self._connection.commit()
            return ids
        except Exception:
            self._connection.rollback()
            raise

    def finish_run(self, experiment_id: str, status: str, run: dict[str, Any], *, note: str | None = None) -> dict[str, Any]:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid run terminal status")
        safe_id(experiment_id)
        encoded = json.dumps(run, sort_keys=True, allow_nan=False)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute("SELECT status FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("experiment is not running")
            self._connection.execute("UPDATE experiments SET run_json = ? WHERE experiment_id = ?", (encoded, experiment_id))
            self._set_status(experiment_id, "running", status, note)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.get(experiment_id)  # type: ignore[return-value]

    def attach_run(self, experiment_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Attach evidence after cancellation or restart without changing the decision."""
        encoded = json.dumps(run, sort_keys=True, allow_nan=False)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute("SELECT status FROM experiments WHERE experiment_id = ?", (safe_id(experiment_id),)).fetchone()
            if row is None or row["status"] not in {"cancelled", "interrupted"}:
                raise ValueError("run evidence can only attach to cancelled or interrupted experiments")
            self._connection.execute("UPDATE experiments SET run_json = ? WHERE experiment_id = ?", (encoded, experiment_id))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.get(experiment_id)  # type: ignore[return-value]

    def events(self, experiment_id: str) -> list[dict[str, Any]]:
        if self.get(experiment_id) is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        rows = self._connection.execute(
            "SELECT event_id, experiment_id, from_status, to_status, note, created_at "
            "FROM experiment_events WHERE experiment_id = ? ORDER BY event_id",
            (experiment_id,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ExperimentStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        record["command"] = tuple(json.loads(record.pop("command_json")))
        record["resource_budget"] = json.loads(record.pop("resource_budget_json"))
        run_json = record.pop("run_json")
        record["run"] = json.loads(run_json) if run_json else None
        return record
