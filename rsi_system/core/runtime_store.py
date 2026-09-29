"""SQLite Mission blackboard, tasks, hypotheses, candidates and Run records."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Mapping
from contextlib import contextmanager
from .runtime_hypotheses import HypothesisRecords
from .runtime_research import research_root_id
from .runtime_limits import RuntimeLimits
from .runtime_research import ResearchGraph
from .algorithm_manifest import manifest_json, manifest_sha256, validate_manifest
from .schema import safe_id
from .runtime_types import (MissionSpec, RUNTIME_SCHEMA_VERSION, ROLES, CANDIDATE_STATUSES,
                            _now, _json, _object, _text)
from .runtime_research_contract import normalize_research_mandate
from .runtime_research_continuation import continuation_ready


class RuntimeStore(ResearchGraph, HypothesisRecords, RuntimeLimits):
    """SQLite blackboard. Rows are projections; events preserve the history."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._atomic_depth = 0
        self._schema()

    @classmethod
    def open_readonly(cls, path: Path | str) -> "RuntimeStore":
        store = cls.__new__(cls)
        store.path = Path(path)
        store.db = sqlite3.connect(f"{store.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        store.db.row_factory = sqlite3.Row
        store.db.execute("PRAGMA foreign_keys = ON")
        store._atomic_depth = 0
        return store

    def _schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deferred_mission_stops(
                mission_id TEXT PRIMARY KEY REFERENCES missions(mission_id),
                reason TEXT NOT NULL, requested_at TEXT NOT NULL,
                reviewed_failed_task_ids_json TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS missions(
                mission_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blackboard_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                version INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(mission_id, version));
            CREATE TABLE IF NOT EXISTS agent_tasks(
                task_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                role TEXT NOT NULL, objective TEXT NOT NULL, reason TEXT NOT NULL, candidate_id TEXT,
                status TEXT NOT NULL, input_version INTEGER NOT NULL, attempt INTEGER NOT NULL,
                lease_token TEXT, lease_until TEXT, output_json TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS agent_tasks_queue ON agent_tasks(mission_id, status, created_at);
            CREATE TABLE IF NOT EXISTS hypotheses(
                hypothesis_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                idea_ref TEXT, statement TEXT NOT NULL, mechanism TEXT NOT NULL, assumptions_json TEXT NOT NULL,
                predictions_json TEXT NOT NULL, experimental_design_json TEXT NOT NULL,
                prior_art_status_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(
                candidate_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                parent_id TEXT, idea_ref TEXT, hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
                hypothesis TEXT NOT NULL, expected_observation TEXT NOT NULL,
                protocol_id TEXT NOT NULL, config_json TEXT NOT NULL, purpose TEXT NOT NULL,
                code_commit TEXT, worktree TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                algorithm_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS runs(
                run_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                candidate_id TEXT REFERENCES candidates(candidate_id), task_id TEXT,
                purpose TEXT NOT NULL, status TEXT NOT NULL, source_commit TEXT, input_json TEXT NOT NULL,
                evidence_json TEXT, evidence_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_scopes(
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(task_id), parent_task_id TEXT REFERENCES agent_tasks(task_id),
                idea_id TEXT, reply_to_task_id TEXT REFERENCES agent_tasks(task_id),
                experiment_json TEXT, depth INTEGER NOT NULL DEFAULT 0,
                delegate_budget INTEGER NOT NULL DEFAULT -1, review_run_id TEXT,
                task_kind TEXT NOT NULL DEFAULT 'standard',
                research_mandate_json TEXT,
                continuation_json TEXT);
            CREATE TABLE IF NOT EXISTS rsi_tool_operations(
                task_id TEXT NOT NULL REFERENCES agent_tasks(task_id), call_id TEXT NOT NULL,
                tool TEXT NOT NULL, request_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                response_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, call_id));
            CREATE TABLE IF NOT EXISTS research_checkpoints(
                checkpoint_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                task_id TEXT NOT NULL REFERENCES agent_tasks(task_id), event_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(RUNTIME_SCHEMA_VERSION),))
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(candidates)")}
        if "hypothesis_id" not in columns:
            self.db.execute("ALTER TABLE candidates ADD COLUMN hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id)")
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        if version not in {"1", "2", "3", str(RUNTIME_SCHEMA_VERSION)}:
            raise ValueError(f"unsupported runtime schema version: {version}")
        stop_columns = {row[1] for row in self.db.execute("PRAGMA table_info(deferred_mission_stops)")}
        if "reviewed_failed_task_ids_json" not in stop_columns:
            self.db.execute("""ALTER TABLE deferred_mission_stops ADD COLUMN
                reviewed_failed_task_ids_json TEXT NOT NULL DEFAULT '[]'""")
        run_columns = {row[1]: row for row in self.db.execute("PRAGMA table_info(runs)")}
        if run_columns["candidate_id"][3]:
            self.db.execute("""CREATE TABLE runs_v3(
                run_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                candidate_id TEXT REFERENCES candidates(candidate_id), task_id TEXT,
                purpose TEXT NOT NULL, status TEXT NOT NULL, source_commit TEXT, input_json TEXT NOT NULL,
                evidence_json TEXT, evidence_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.db.execute("INSERT INTO runs_v3 SELECT * FROM runs")
            self.db.execute("DROP TABLE runs")
            self.db.execute("ALTER TABLE runs_v3 RENAME TO runs")
        run_columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "idea_ref" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN idea_ref TEXT")
        if "kind" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'candidate'")
        candidate_columns = {row[1] for row in self.db.execute("PRAGMA table_info(candidates)")}
        if "algorithm_json" not in candidate_columns:
            self.db.execute("ALTER TABLE candidates ADD COLUMN algorithm_json TEXT NOT NULL DEFAULT '{}'")
        scope_columns = {row[1] for row in self.db.execute("PRAGMA table_info(task_scopes)")}
        if "delegate_budget" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN delegate_budget INTEGER NOT NULL DEFAULT -1")
        if "review_run_id" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN review_run_id TEXT")
        if "task_kind" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'standard'")
        if "research_mandate_json" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN research_mandate_json TEXT")
        if "continuation_json" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN continuation_json TEXT")
        if "retry_not_before" not in scope_columns:
            self.db.execute("ALTER TABLE task_scopes ADD COLUMN retry_not_before TEXT")
        # Keep the legacy column for old databases; -1 represents unlimited delegation.
        self.db.execute("UPDATE task_scopes SET delegate_budget=-1 WHERE delegate_budget!=-1")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_task_once ON runs(task_id) WHERE task_id IS NOT NULL")
        self.migrate_hypotheses()
        self.migrate_limits()
        self.migrate_research()
        self.db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(RUNTIME_SCHEMA_VERSION),))
        self._commit()

    def _begin(self):
        if not self._atomic_depth:
            self.db.execute("BEGIN IMMEDIATE")

    def _commit(self):
        if not self._atomic_depth:
            self.db.commit()

    def _rollback(self):
        if not self._atomic_depth:
            self.db.rollback()

    @contextmanager
    def atomic(self):
        outer = self._atomic_depth == 0
        if outer:
            self.db.execute("BEGIN IMMEDIATE")
        self._atomic_depth += 1
        try:
            yield
            if outer:
                self.db.commit()
        except BaseException:
            if outer:
                self.db.rollback()
            raise
        finally:
            self._atomic_depth -= 1

    def validate_task(self, task):
        current, mission = self.task(task["task_id"]), self.mission(task["mission_id"])
        if (not current or current["status"] != "running" or current["lease_token"] != task["lease_token"]
                or current["lease_until"] <= _now()):
            raise ValueError("task lease is no longer valid")
        if mission["status"] != "active":
            raise ValueError("task output uses stale Mission state")
        if self.db.execute("""SELECT 1 FROM blackboard_events WHERE mission_id=? AND version>?
                AND kind='mission_status' LIMIT 1""", (task["mission_id"], task["input_version"])).fetchone():
            raise ValueError("task output uses stale Mission status")

    def tool_operations(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM rsi_tool_operations WHERE task_id=? ORDER BY created_at,rowid",
                               (safe_id(task_id, "task_id"),))
        return [dict(row) for row in rows]

    def tool_operation(self, task_id: str, call_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM rsi_tool_operations WHERE task_id=? AND call_id=?",
                              (safe_id(task_id, "task_id"), safe_id(call_id, "call_id"))).fetchone()
        return dict(row) if row else None

    def put_tool_operation(self, task_id: str, call_id: str, tool: str, request_sha256: str,
                           status: str, response: Mapping[str, Any] | None = None) -> None:
        now = _now()
        self.db.execute("""INSERT INTO rsi_tool_operations VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id,call_id) DO UPDATE SET status=excluded.status,
            response_json=excluded.response_json,updated_at=excluded.updated_at""",
            (task_id, call_id, tool, request_sha256, status,
             _json(response) if response is not None else None, now, now))
        self._commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "RuntimeStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_mission(self, mission: MissionSpec, *, status: str = "active") -> dict[str, Any]:
        if status not in {"active", "paused"}:
            raise ValueError("new Mission must be active or paused")
        now = _now()
        payload = _json(mission.to_payload())
        try:
            self.db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?)", (mission.mission_id, payload, status, 1, now, now))
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission.mission_id, 1, "mission_started" if status == "active" else "mission_created", payload, now))
            self._commit()
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise ValueError(f"mission already exists: {mission.mission_id}") from exc
        return self.mission(mission.mission_id)

    def mission(self, mission_id: str) -> dict[str, Any] | None:
        safe_id(mission_id, "mission_id")
        row = self.db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def active_mission(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT mission_id FROM missions WHERE status='active' ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self.mission(row[0]) if row else None

    def set_mission_status(self, mission_id: str, status: str, *, reason: str) -> dict[str, Any]:
        if status not in {"active", "paused", "completed", "stopped", "failed"}:
            raise ValueError("invalid mission status")
        self._begin()
        try:
            row = self.db.execute("SELECT version FROM missions WHERE mission_id=?", (safe_id(mission_id, "mission_id"),)).fetchone()
            if row is None:
                raise ValueError("unknown mission")
            version = row[0] + 1
            now = _now()
            payload = {"status": status, "reason": _text(reason, "reason")}
            self.db.execute("UPDATE missions SET status=?,version=?,updated_at=? WHERE mission_id=?", (status, version, now, mission_id))
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission_id, version, "mission_status", _json(payload), now))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.mission(mission_id)  # type: ignore[return-value]

    def resume_mission(self, mission_id: str, *, reason: str) -> dict[str, Any]:
        """Resume after an operator has reviewed bounded task failures.

        Failed tasks and their original payloads remain failure evidence.
        Resuming neither refunds budget nor rewrites historical outcomes.
        """
        mission_id = safe_id(mission_id, "mission_id")
        self._begin()
        try:
            now = _now()
            mission = self.mission(mission_id)
            if mission is None:
                raise ValueError("unknown mission")
            if mission["status"] == "stopped" and self.queued_or_running(mission_id):
                self.db.execute("""INSERT OR REPLACE INTO deferred_mission_stops
                    (mission_id,reason,requested_at,reviewed_failed_task_ids_json) VALUES(?,?,?,?)""",
                    (mission_id, "Finish previously queued tasks before stopping", now, "[]"))
            elif mission["status"] != "active":
                self.db.execute("DELETE FROM deferred_mission_stops WHERE mission_id=?", (mission_id,))
            # Failed rows are immutable evidence.  Older code relabelled every
            # failure as cancelled here, which erased the distinction between a
            # provider outage and an operator cancellation and reset recovery
            # accounting on every resume.
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.set_mission_status(mission_id, "active", reason=reason)

    def deferred_stop(self, mission_id: str) -> str | None:
        row = self.db.execute("SELECT reason FROM deferred_mission_stops WHERE mission_id=?", (mission_id,)).fetchone()
        return row[0] if row else None

    def unreviewed_deferred_failures(self, mission_id: str) -> list[str]:
        row = self.db.execute("""SELECT reviewed_failed_task_ids_json FROM deferred_mission_stops
            WHERE mission_id=?""", (safe_id(mission_id, "mission_id"),)).fetchone()
        if row is None:
            raise ValueError("Mission has no deferred stop")
        reviewed = set(json.loads(row[0]))
        return [task_id for (task_id,) in self.db.execute("""SELECT task_id FROM agent_tasks
            WHERE mission_id=? AND status='failed' ORDER BY created_at,rowid""", (mission_id,))
            if task_id not in reviewed]

    def finish_deferred_stop(self, mission_id: str) -> dict[str, Any]:
        with self.atomic():
            if self.queued_or_running(mission_id):
                raise ValueError("cannot stop while Mission tasks remain queued or running")
            reason = self.deferred_stop(mission_id)
            if reason is None:
                raise ValueError("Mission has no deferred stop")
            if self.unreviewed_deferred_failures(mission_id):
                raise ValueError("cannot stop with unreviewed failed tasks")
            result = self.set_mission_status(mission_id, "stopped", reason=reason)
            self.db.execute("DELETE FROM deferred_mission_stops WHERE mission_id=?", (mission_id,))
            return result

    def event(self, mission_id: str, kind: str, payload: Mapping[str, Any]) -> int:
        self._begin()
        try:
            row = self.db.execute("SELECT version FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                raise ValueError("unknown mission")
            version, now = row[0] + 1, _now()
            self.db.execute("UPDATE missions SET version=?,updated_at=? WHERE mission_id=?", (version, now, mission_id))
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission_id, version, _text(kind, "event kind"), _json(payload), now))
            self._commit()
            return version
        except Exception:
            self._rollback()
            raise

    def events(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM blackboard_events WHERE mission_id=? ORDER BY version", (mission_id,))
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def latest_run(self, mission_id: str, candidate_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT run_id FROM runs WHERE mission_id=? AND candidate_id=? ORDER BY created_at DESC LIMIT 1",
                              (mission_id, candidate_id)).fetchone()
        return self.run(row[0]) if row else None

    def create_task(self, mission_id: str, role: str, objective: str, reason: str, *, candidate_id: str | None = None,
                    parent_task_id: str | None = None, idea_id: str | None = None,
                    reply_to_task_id: str | None = None, experiment: Mapping[str, Any] | None = None,
                    delegate_budget: int | None = None, research_node_id: str | None = None,
                    research_revision: int | None = None, review_run_id: str | None = None,
                    task_kind: str = "standard", research_mandate: Mapping[str, Any] | None = None,
                    continuation: Mapping[str, Any] | None = None,
                    retry_not_before: str | None = None) -> dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        objective, reason = _text(objective, "objective"), _text(reason, "reason")
        parent = self.task(parent_task_id) if parent_task_id else None
        research_mandate = normalize_research_mandate(research_mandate)
        if continuation is not None and research_mandate is None and parent and parent["scope"].get("research_mandate"):
            research_mandate = parent["scope"]["research_mandate"] if role == "researcher" else None
        if continuation is not None:
            continuation = _object(continuation, "continuation")
            after_task_ids = continuation.get("after_task_ids", [])
            if not isinstance(after_task_ids, list):
                raise ValueError("continuation.after_task_ids must be a list")
            continuation = {"objective": _text(continuation.get("objective"), "continuation.objective"),
                            "after_task_ids": after_task_ids,
                            "source_task_id": parent_task_id}
            if any(not isinstance(item, str) or not item.strip() for item in continuation["after_task_ids"]):
                raise ValueError("continuation.after_task_ids must be a list of nonempty strings")
            if role != "researcher" or not parent_task_id:
                raise ValueError("continuation must be a researcher child of its source task")
        if candidate_id is not None:
            safe_id(candidate_id, "candidate_id")
        if idea_id is not None:
            safe_id(idea_id, "idea_id")
        if task_kind not in {"standard", "problem_reframe"}:
            raise ValueError("unknown task kind")
        if task_kind == "problem_reframe" and (role != "researcher" or idea_id or candidate_id or experiment or
                (not parent and not reply_to_task_id) or
                (parent and parent["role"] != "coordinator" and not reply_to_task_id)):
            raise ValueError("problem reframe requires a coordinator-delegated researcher or linked recovery")
        if research_node_id is None and parent:
            research_node_id = parent["scope"]["research_node_id"]
            research_revision = parent["scope"]["research_revision"]
        if research_node_id is not None:
            research_node_id = safe_id(research_node_id, "research_node_id")
            node = self.research_node(mission_id, research_node_id, research_revision)
            if node is None:
                raise ValueError("research task binding requires a node revision in this Mission")
            research_revision = node["revision"]
        elif research_revision is not None:
            raise ValueError("research_revision requires research_node_id")
        if task_kind == "problem_reframe" and research_node_id != research_root_id(mission_id):
            raise ValueError("problem reframe must address the Mission root question")
        if review_run_id is not None:
            review_run_id = safe_id(review_run_id, "review_run_id")
            run = self.run(review_run_id)
            if run is None or run["mission_id"] != mission_id or run["status"] != "completed":
                raise ValueError("review_run_id must name a completed Run in this Mission")
            if candidate_id is not None and run.get("candidate_id") != candidate_id:
                raise ValueError("review_run_id does not belong to the task Candidate")
        if parent_task_id and (parent is None or parent["mission_id"] != mission_id):
            raise ValueError("parent task belongs to another Mission")
        if continuation is not None:
            if any(dependency_id == parent_task_id for dependency_id in continuation["after_task_ids"]):
                raise ValueError("continuation cannot depend on itself or its source task")
            for dependency_id in continuation["after_task_ids"]:
                dependency = self.task(dependency_id)
                if dependency is None or dependency["mission_id"] != mission_id:
                    raise ValueError("continuation dependency belongs to another Mission")
                if dependency["scope"].get("idea_id") != idea_id:
                    raise ValueError("continuation dependency must stay in the assigned Idea")
                if research_node_id and dependency["scope"].get("research_node_id") != research_node_id:
                    raise ValueError("continuation dependency must stay on the assigned research node")
                ancestor = parent_task_id
                while ancestor:
                    if dependency_id == ancestor:
                        raise ValueError("continuation cannot depend on a source ancestor")
                    linked = self.task(ancestor)
                    ancestor = linked["scope"].get("parent_task_id") if linked else None
        reply = self.task(reply_to_task_id) if reply_to_task_id else None
        if reply_to_task_id and (reply is None or reply["mission_id"] != mission_id):
            raise ValueError("reply target belongs to another Mission")
        if reply and reply["scope"]["idea_id"] and reply["scope"]["idea_id"] != idea_id:
            raise ValueError("reply target belongs to another Idea branch")
        if task_kind == "problem_reframe" and reply_to_task_id:
            if (not reply or reply["role"] != "researcher" or
                    reply["scope"].get("task_kind") != "problem_reframe" or
                    reply["scope"].get("research_node_id") != research_node_id):
                raise ValueError("problem reframe recovery must reference the original independent reframe")
        if experiment is not None and (role != "runner" or candidate_id is not None):
            raise ValueError("exploration experiment requires a candidate-free runner")
        self._begin()
        try:
            mission = self.mission(mission_id)
            if mission is None or mission["status"] != "active":
                raise ValueError("task requires an active Mission")
            if role != "runner" and self.remaining_agent_tasks(mission_id) == 0:
                raise ValueError("Mission agent task budget exhausted")
            if task_kind == "problem_reframe" and self.db.execute("""SELECT 1 FROM task_scopes s
                    JOIN agent_tasks t ON t.task_id=s.task_id
                    WHERE t.mission_id=? AND s.task_kind='problem_reframe'
                    AND t.status IN ('queued','running') LIMIT 1""", (mission_id,)).fetchone():
                raise ValueError("an independent problem reframe is already in progress")
            if role == "runner" and self.remaining_runs(mission_id) == 0:
                raise ValueError("Mission run budget exhausted")
            if parent:
                scope = parent["scope"]
                composed = self.db.execute("""SELECT inputs_json FROM research_compositions
                    WHERE mission_id=? AND node_id=? AND actor_task_id=?""",
                    (mission_id, research_node_id, parent["task_id"])).fetchone()
                joint_within_scope = bool(scope["research_node_id"] and composed and all(
                    self._research_descendant(mission_id, scope["research_node_id"], ref["node_id"])
                    for ref in json.loads(composed["inputs_json"])))
                if parent["status"] not in {"running", "completed"}:
                    raise ValueError("delegation parent must be running or completed")
                if scope["idea_id"] and idea_id != scope["idea_id"] and not joint_within_scope:
                    raise ValueError("delegated task must stay with the assigned Idea")
                if scope["research_node_id"] and parent["role"] != "coordinator":
                    if research_node_id is None or not (self._research_descendant(
                            mission_id, scope["research_node_id"], research_node_id) or joint_within_scope):
                        raise ValueError("delegated research task must stay in the assigned subtree")
            task_id, now, version = f"task-{secrets.token_hex(10)}", _now(), mission["version"] + 1
            self.db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (task_id, mission_id, role, objective, reason, candidate_id, "queued",
                             mission["version"], 0, None, None, None, None, now, now))
            depth = (self.task_scope(parent_task_id)["depth"] + 1) if parent_task_id else 0
            self.db.execute("INSERT INTO task_scopes(task_id,parent_task_id,idea_id,reply_to_task_id,experiment_json,depth,delegate_budget,review_run_id,task_kind,research_mandate_json,continuation_json,retry_not_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (task_id, parent_task_id, idea_id, reply_to_task_id,
                             _json(_object(experiment, "experiment")) if experiment is not None else None,
                             depth, -1, review_run_id, task_kind,
                             _json(research_mandate) if research_mandate else None,
                             _json(continuation) if continuation else None, retry_not_before))
            if research_node_id is not None:
                self.db.execute("INSERT INTO research_task_bindings VALUES(?,?,?,?)",
                                (task_id, mission_id, research_node_id, research_revision))
            self.db.execute("UPDATE missions SET version=?,updated_at=? WHERE mission_id=?", (version, now, mission_id))
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission_id, version, "task_created", _json({"task_id": task_id, "role": role,
                             "candidate_id": candidate_id, "idea_id": idea_id, "parent_task_id": parent_task_id,
                             "research_node_id": research_node_id, "research_revision": research_revision,
                             "objective": objective, "reason": reason, "task_kind": task_kind,
                             "research_mandate": research_mandate,
                             "continuation": continuation}), now))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.task(task_id)

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (safe_id(task_id, "task_id"),)).fetchone()
        if not row:
            return None
        result = self._decode_task(row)
        result["scope"] = self.task_scope(task_id)
        return result

    def task_scope(self, task_id: str | None) -> dict[str, Any]:
        if not task_id:
            return {"parent_task_id": None, "idea_id": None, "reply_to_task_id": None, "experiment": None,
                    "depth": 0, "delegate_budget": None, "research_node_id": None, "research_revision": None,
                    "review_run_id": None, "task_kind": "standard", "research_mandate": None, "continuation": None, "retry_not_before": None}
        row = self.db.execute("SELECT * FROM task_scopes WHERE task_id=?", (safe_id(task_id, "task_id"),)).fetchone()
        if not row:
            return {"parent_task_id": None, "idea_id": None, "reply_to_task_id": None, "experiment": None,
                    "depth": 0, "delegate_budget": None, "research_node_id": None, "research_revision": None,
                    "review_run_id": None, "task_kind": "standard", "research_mandate": None, "continuation": None, "retry_not_before": None}
        result = dict(row)
        result["delegate_budget"] = None
        experiment = result.pop("experiment_json")
        result["experiment"] = json.loads(experiment) if experiment else None
        binding = self.db.execute("SELECT node_id,revision FROM research_task_bindings WHERE task_id=?", (task_id,)).fetchone()
        result["research_node_id"] = binding["node_id"] if binding else None
        result["research_revision"] = binding["revision"] if binding else None
        result["review_run_id"] = result.get("review_run_id")
        mandate = result.pop("research_mandate_json", None)
        result["research_mandate"] = json.loads(mandate) if mandate else None
        continuation = result.pop("continuation_json", None)
        result["continuation"] = json.loads(continuation) if continuation else None
        return result

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["output"] = json.loads(result.pop("output_json")) if result.get("output_json") else None
        return result

    def claim_task(self, mission_id: str, *, lease_seconds: int = 900,
                   max_parallel_agents: int | None = None, max_parallel_runs: int = 1,
                   collaborators_only: bool = False) -> dict[str, Any] | None:
        self._begin()
        try:
            mission = self.mission(mission_id)
            if not mission or mission["status"] != "active":
                self._commit()
                return None
            stopping = bool(self.deferred_stop(mission_id))
            running = self.db.execute("""SELECT t.*, EXISTS(SELECT 1 FROM rsi_tool_operations o
                WHERE o.task_id=t.task_id AND o.tool='rsi_wait' AND o.status='pending') AS waiting
                FROM agent_tasks t WHERE t.mission_id=? AND t.status='running'""", (mission_id,)).fetchall()
            agents = sum(t["role"] not in {"coordinator", "runner"} and not t["waiting"] for t in running)
            runners = sum(t["role"] == "runner" for t in running)
            rows = self.db.execute("SELECT * FROM agent_tasks WHERE mission_id=? AND status='queued' ORDER BY created_at,rowid",
                                   (mission_id,)).fetchall()
            row = None
            for queued in rows:
                queued_task = self.task(queued["task_id"])
                if queued_task and queued_task["scope"].get("retry_not_before") and queued_task["scope"]["retry_not_before"] > _now():
                    continue
                if queued_task and queued_task["scope"].get("continuation"):
                    if stopping:
                        continue
                    ready, _ = continuation_ready(self, queued_task)
                    if not ready:
                        continue
                role = queued["role"]
                if role == "coordinator" and (collaborators_only or any(t["role"] == role for t in running)):
                    continue
                if max_parallel_agents is not None and role not in {"coordinator", "runner"} and agents >= max_parallel_agents:
                    continue
                if role == "runner" and runners >= max_parallel_runs:
                    continue
                # Code writes and execution remain exclusive per Candidate. Analyzer
                # and Critic only read a frozen Run and may therefore claim together.
                if queued["candidate_id"] and role in {"coder", "runner", "analyzer", "critic"} and any(
                        t["candidate_id"] == queued["candidate_id"] and
                        (role in {"coder", "runner"} or t["role"] in {"coder", "runner"})
                        for t in running):
                    continue
                row = queued
                break
            if row is None:
                self._commit()
                return None
            token = secrets.token_urlsafe(18)
            now = datetime.now(timezone.utc)
            lease_until = (now + timedelta(seconds=lease_seconds)).replace(microsecond=0).isoformat()
            version = self.mission(mission_id)["version"]
            self.db.execute("UPDATE agent_tasks SET status='running',attempt=attempt+1,lease_token=?,lease_until=?,updated_at=?,input_version=? WHERE task_id=?",
                            (token, lease_until, now.replace(microsecond=0).isoformat(), version, row["task_id"]))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.task(row["task_id"])

    def finish_task(self, task_id: str, token: str, output: Mapping[str, Any], *, status: str = "completed") -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid task terminal status")
        self._begin()
        try:
            row = self.db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["status"] != "running" or row["lease_token"] != token:
                raise ValueError("task lease is no longer valid")
            now = _now()
            encoded = _json(output)
            error = output.get("error") if status == "failed" else None
            self.db.execute("UPDATE agent_tasks SET status=?,output_json=?,error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE task_id=?",
                            (status, encoded, error, now, task_id))
            if status in {"failed", "cancelled"}:
                # An explicit continuation is a request against successful source
                # completion. Keep its receipt, but cancel queued children when
                # the source cannot satisfy that precondition.
                rows = self.db.execute("SELECT task_id,continuation_json FROM task_scopes WHERE continuation_json IS NOT NULL").fetchall()
                for child in rows:
                    try:
                        continuation = json.loads(child["continuation_json"])
                    except (TypeError, ValueError):
                        continue
                    if continuation.get("source_task_id") == task_id:
                        self.db.execute("UPDATE agent_tasks SET status='cancelled',output_json=?,error=?,updated_at=? WHERE task_id=? AND status='queued'",
                                        (_json({"error": f"continuation source {task_id} ended {status}"}),
                                         f"continuation source {task_id} ended {status}", now, child["task_id"]))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.task(task_id)  # type: ignore[return-value]

    def record_research_checkpoint(self, mission_id: str, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Store a compact, source-addressed change summary and its audit event."""
        checkpoint_id = f"checkpoint-{secrets.token_hex(10)}"
        now = _now()
        with self.atomic():
            mission = self.mission(mission_id)
            if mission is None:
                raise ValueError("unknown Mission")
            task = self.task(task_id)
            if task is None or task["mission_id"] != mission_id:
                raise ValueError("checkpoint task belongs to another Mission")
            version = mission["version"] + 1
            event_payload = {"checkpoint_id": checkpoint_id, "task_id": task_id, **dict(payload)}
            self.db.execute("UPDATE missions SET version=?,updated_at=? WHERE mission_id=?", (version, now, mission_id))
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission_id, version, "research_checkpoint", _json(event_payload), now))
            self.db.execute("INSERT INTO research_checkpoints VALUES(?,?,?,?,?,?)",
                            (checkpoint_id, mission_id, task_id, version, _json(dict(payload)), now))
        return {"checkpoint_id": checkpoint_id, "event_version": version, **dict(payload)}

    def research_checkpoints(self, mission_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM research_checkpoints WHERE mission_id=?"
        args: list[Any] = [mission_id]
        if task_id:
            query += " AND task_id=?"
            args.append(task_id)
        query += " ORDER BY event_version"
        return [{**dict(row), "payload": json.loads(row["payload_json"])}
                for row in self.db.execute(query, args)]

    def list_hypotheses(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT hypothesis_id FROM hypotheses WHERE mission_id=? ORDER BY created_at",
                               (mission_id,))
        return [self.hypothesis(row[0]) for row in rows]  # type: ignore[misc]

    def list_candidates(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT candidate_id FROM candidates WHERE mission_id=? ORDER BY created_at", (mission_id,))
        return [self.candidate(row[0]) for row in rows]  # type: ignore[misc]

    def list_runs(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT run_id FROM runs WHERE mission_id=? ORDER BY created_at", (mission_id,))
        return [self.run(row[0]) for row in rows]  # type: ignore[misc]

    def list_tasks(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM agent_tasks WHERE mission_id=? ORDER BY created_at", (mission_id,))
        return [self.task(row["task_id"]) for row in rows]

    def create_candidate(self, mission_id: str, data: Mapping[str, Any], *, code_commit: str | None = None) -> dict[str, Any]:
        candidate_id = safe_id(data.get("candidate_id") or data.get("id"), "candidate_id")
        mission = self.mission(mission_id)
        if mission is None:
            raise ValueError("unknown mission")
        parent = data.get("parent_id")
        if parent is not None:
            ancestor = self.candidate(parent)
            if ancestor is None or ancestor["mission_id"] != mission_id:
                raise ValueError(f"unknown candidate parent in Mission: {parent}")
        hypothesis_id = data.get("hypothesis_id")
        hypothesis_version, kind, admission_reason = self.candidate_admission(mission_id, data)
        algorithm = validate_manifest(data.get("algorithm", {}))
        now = _now()
        values = (candidate_id, mission_id, parent, data.get("idea_ref"), hypothesis_id, _text(data.get("hypothesis"), "hypothesis"),
                  _text(data.get("expected_observation"), "expected_observation"),
                  _text(data.get("protocol_id"), "protocol_id"), _json(_object(data.get("config", {}), "config")),
                  _text(data.get("purpose", "exploration"), "purpose"), code_commit, None, "proposed", now, now,
                  hypothesis_version, kind, admission_reason, manifest_json(algorithm))
        try:
            self.db.execute("INSERT INTO candidates(candidate_id,mission_id,parent_id,idea_ref,hypothesis_id,hypothesis,expected_observation,protocol_id,config_json,purpose,code_commit,worktree,status,created_at,updated_at,hypothesis_version,kind,admission_reason,algorithm_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            self._commit()
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise ValueError(f"candidate already exists: {candidate_id}") from exc
        self.event(mission_id, "candidate_registered", {"candidate_id": candidate_id, "parent_id": parent,
                                                          "idea_ref": data.get("idea_ref"), "hypothesis": values[5],
                                                          "algorithm_sha256": manifest_sha256(algorithm)})
        return self.candidate(candidate_id)  # type: ignore[return-value]

    def candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM candidates WHERE candidate_id=?", (safe_id(candidate_id, "candidate_id"),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        result["algorithm"] = validate_manifest(json.loads(result.pop("algorithm_json", "{}")))
        return result

    def update_candidate(self, candidate_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"code_commit", "worktree", "status"}
        if set(changes) - allowed:
            raise ValueError("candidate update contains unsupported fields")
        if changes.get("status") not in (None, *CANDIDATE_STATUSES):
            raise ValueError("invalid candidate status")
        if not changes:
            return self.candidate(candidate_id)  # type: ignore[return-value]
        fields = ",".join(f"{name}=?" for name in changes)
        values = [changes[name] for name in changes] + [_now(), safe_id(candidate_id, "candidate_id")]
        self.db.execute(f"UPDATE candidates SET {fields},updated_at=? WHERE candidate_id=?", values)
        if self.db.execute("SELECT changes()").fetchone()[0] == 0:
            raise ValueError("unknown candidate")
        self._commit()
        return self.candidate(candidate_id)  # type: ignore[return-value]

    def create_run(self, mission_id: str, candidate_id: str | None, task_id: str | None, purpose: str,
                   status: str, source_commit: str | None, inputs: Mapping[str, Any], evidence: Mapping[str, Any] | None,
                   *, idea_ref: str | None = None) -> dict[str, Any]:
        if candidate_id is not None:
            candidate = self.candidate(candidate_id)
            if candidate is None or candidate["mission_id"] != mission_id:
                raise ValueError("run candidate does not belong to mission")
        elif not (idea_ref and task_id and purpose == "exploration"):
            raise ValueError("candidate-free Run requires an Idea and exploration task")
        if task_id:
            previous = self.db.execute("SELECT run_id FROM runs WHERE task_id=?", (task_id,)).fetchone()
            if previous:
                return self.run(previous[0])
        run_id = f"run-{secrets.token_hex(12)}"
        evidence_json = _json(evidence) if evidence is not None else None
        digest = hashlib.sha256(evidence_json.encode()).hexdigest() if evidence_json else None
        now = _now()
        self.db.execute("""INSERT INTO runs(run_id,mission_id,candidate_id,task_id,purpose,status,source_commit,
            input_json,evidence_json,evidence_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, mission_id, candidate_id, task_id, _text(purpose, "purpose"), _text(status, "status"),
                         source_commit, _json(inputs), evidence_json, digest, now, now))
        self.db.execute("UPDATE runs SET idea_ref=?,kind=? WHERE run_id=?",
                        (idea_ref, "exploration" if candidate_id is None else "candidate", run_id))
        self._commit()
        self.event(mission_id, "run_registered", {"run_id": run_id, "candidate_id": candidate_id,
                                                    "idea_ref": idea_ref, "kind": "exploration" if candidate_id is None else "candidate", "status": status,
                                                    "purpose": purpose, "evidence_sha256": digest})
        return self.run(run_id)  # type: ignore[return-value]

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (safe_id(run_id, "run_id"),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["inputs"] = json.loads(result.pop("input_json"))
        result["evidence"] = json.loads(result.pop("evidence_json")) if result.get("evidence_json") else None
        return result

    def update_run_evidence(self, run_id: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Attach durable post-run artifacts without changing the Run result."""
        encoded = _json(evidence)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = _now()
        self.db.execute("UPDATE runs SET evidence_json=?,evidence_sha256=?,updated_at=? WHERE run_id=?",
                        (encoded, digest, now, safe_id(run_id, "run_id")))
        if self.db.execute("SELECT changes()").fetchone()[0] == 0:
            raise ValueError("unknown Run")
        self._commit()
        return self.run(run_id)  # type: ignore[return-value]

    def finalize_run(self, run_id: str, status: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Finalize a provisionally registered local Runner Run idempotently."""
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("invalid terminal Run status")
        encoded = _json(evidence)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        current = self.run(run_id)
        if current is None:
            raise ValueError("unknown Run")
        if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            if current["status"] != status or current.get("evidence_sha256") != digest:
                raise ValueError("terminal Run cannot be overwritten")
            return current
        self.db.execute("UPDATE runs SET status=?,evidence_json=?,evidence_sha256=?,updated_at=? WHERE run_id=? AND status NOT IN ('completed','failed','cancelled','interrupted')",
                        (status, encoded, digest, _now(), safe_id(run_id, "run_id")))
        if self.db.execute("SELECT changes()").fetchone()[0] == 0:
            current = self.run(run_id)
            if current and current["status"] == status and current.get("evidence_sha256") == digest:
                return current
            raise ValueError("Run finalized concurrently")
        self.event(current["mission_id"], "run_finalized", {"run_id": run_id, "status": status,
                    "evidence_sha256": digest})
        self._commit()
        return self.run(run_id)  # type: ignore[return-value]

    def queued_or_running(self, mission_id: str) -> int:
        return self.db.execute("SELECT count(*) FROM agent_tasks WHERE mission_id=? AND status IN ('queued','running')",
                               (mission_id,)).fetchone()[0]

    def failed_tasks(self, mission_id: str, *, since_resume: bool = False,
                     role: str | None = None) -> int:
        """Keep historical failures; optionally count the current recovery window.

        Claiming a task records the Mission version it actually read. Using
        that version avoids losing failures in the same second as a resume,
        and counts tasks queued earlier but executed after the resume.
        """
        version = 0
        if since_resume:
            version = max((event["version"] for event in self.events(mission_id)
                           if event["kind"] == "mission_status"
                           and event["payload"].get("status") == "active"), default=0)
        return self.db.execute("""SELECT count(*) FROM agent_tasks
            WHERE mission_id=? AND status='failed' AND input_version>=?
            AND (? IS NULL OR role=?)""", (mission_id, version, role, role)).fetchone()[0]

    def remaining_agent_tasks(self, mission_id: str) -> int | None:
        limit = self.mission(mission_id)["payload"]["resources"].get("max_agent_tasks")
        if limit is None:
            return None
        used = self.db.execute("SELECT count(*) FROM agent_tasks WHERE mission_id=? AND role!='runner'",
                               (mission_id,)).fetchone()[0]
        return max(0, limit - used)

    def apply_coordinator_decision(self, task: Mapping[str, Any], payload: Mapping[str, Any],
                                   requested: list[dict[str, Any]], *, keep_active: bool = False) -> None:
        """Atomically validate the snapshot, dispatch the whole batch and finish its lease."""
        self._begin()
        try:
            now, mission_id = _now(), task["mission_id"]
            current, mission = self.task(task["task_id"]), self.mission(mission_id)
            if (not current or current["status"] != "running" or current["lease_token"] != task["lease_token"]
                    or current["lease_until"] <= now):
                raise ValueError("coordinator task lease is no longer valid")
            self.validate_task(task)
            reviewed_failures = payload.get("reviewed_failures", [])
            if not isinstance(reviewed_failures, list):
                raise ValueError("reviewed_failures must be a list")
            if reviewed_failures and payload["decision"] != "stop":
                raise ValueError("reviewed_failures require a stop decision")
            reviewed_ids: list[str] = []
            for item in reviewed_failures:
                if not isinstance(item, Mapping):
                    raise ValueError("reviewed failure needs task_id and reason")
                failed_id = safe_id(item.get("task_id"), "reviewed failure task_id")
                _text(item.get("reason"), "reviewed failure reason")
                if failed_id in reviewed_ids:
                    raise ValueError("reviewed failure task_id is duplicated")
                failed = self.task(failed_id)
                if failed is None or failed["mission_id"] != mission_id or failed["status"] != "failed":
                    raise ValueError("reviewed failure must be a failed task in this Mission")
                reviewed_ids.append(failed_id)
            remaining = self.remaining_agent_tasks(mission_id)
            if remaining is not None and sum(s["role"] != "runner" for s in requested) > remaining:
                raise ValueError("coordinator decision exceeds Mission agent task budget")
            remaining_runs = self.remaining_runs(mission_id)
            if remaining_runs is not None and sum(s["role"] == "runner" for s in requested) > remaining_runs:
                raise ValueError("coordinator decision exceeds Mission run budget")
            version = mission["version"]
            scheduled = [row[0] for row in self.db.execute(
                "SELECT task_id FROM task_scopes WHERE parent_task_id=? ORDER BY rowid", (task["task_id"],))]
            for spec in requested:
                task_id = f"task-{secrets.token_hex(10)}"
                review_run_id = spec.get("review_run_id")
                if review_run_id is not None:
                    if spec.get("role") not in {"analyzer", "critic"}:
                        raise ValueError("review_run_id is only valid for analyzer or critic tasks")
                    review_run_id = safe_id(review_run_id, "review_run_id")
                    review_run = self.run(review_run_id)
                    if (review_run is None or review_run["mission_id"] != mission_id
                            or review_run["status"] != "completed"
                            or (spec.get("candidate_id") is not None
                                and review_run.get("candidate_id") != spec.get("candidate_id"))):
                        raise ValueError("review_run_id must name a completed Run for this Candidate")
                self.db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (task_id, mission_id, spec["role"], spec["objective"], spec["reason"],
                                 spec.get("candidate_id"), "queued", version, 0, None, None, None, None, now, now))
                self.db.execute("INSERT INTO task_scopes(task_id,parent_task_id,idea_id,reply_to_task_id,experiment_json,depth,delegate_budget,review_run_id,research_mandate_json,continuation_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (task_id, None, spec.get("idea_id"), spec.get("reply_to_task_id"),
                                 _json(spec["experiment"]) if spec.get("experiment") else None,
                                 0, -1, review_run_id,
                                 _json(spec.get("research_mandate")) if spec.get("research_mandate") else None,
                                 None))
                node_id = spec.get("research_node_id") or current["scope"].get("research_node_id")
                revision = spec.get("research_revision")
                if node_id:
                    if revision is None and node_id == current["scope"].get("research_node_id"):
                        revision = current["scope"].get("research_revision")
                    if revision is None or self.research_node(mission_id, node_id, revision) is None:
                        raise ValueError("coordinator dispatch requires an exact research revision in this Mission")
                    self.db.execute("INSERT INTO research_task_bindings VALUES(?,?,?,?)",
                                    (task_id, mission_id, node_id, revision))
                    spec = {**spec, "research_node_id": node_id, "research_revision": revision}
                elif revision is not None:
                    raise ValueError("research_revision requires research_node_id")
                version += 1
                self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                                (mission_id, version, "task_created", _json({"task_id": task_id, **spec}), now))
                scheduled.append(task_id)
            version += 1
            from .runtime_feedback import new_research_feedback
            review = payload.get("exploration_review") or {}
            reviewed_version = review.get("reviewed_event_version", task["input_version"])
            if type(reviewed_version) is not int or not 0 <= reviewed_version <= mission["version"]:
                raise ValueError("invalid reviewed feedback version")
            decision = {**payload, "task_id": task["task_id"], "base_state_version": task["input_version"],
                        "reviewed_feedback_version": reviewed_version,
                        "reviewed_failed_task_ids": reviewed_ids,
                        "scheduled_task_ids": scheduled}
            self.db.execute("INSERT INTO blackboard_events(mission_id,version,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (mission_id, version, "coordinator_decision", _json(decision), now))
            if payload["decision"] == "stop":
                continuation_rows = self.db.execute("""SELECT t.task_id FROM agent_tasks t
                    JOIN task_scopes s ON s.task_id=t.task_id
                    WHERE t.mission_id=? AND t.status='queued' AND s.continuation_json IS NOT NULL""",
                    (mission_id,)).fetchall()
                for continuation_row in continuation_rows:
                    self.db.execute("""UPDATE agent_tasks SET status='cancelled',output_json=?,error=?,updated_at=?
                        WHERE task_id=? AND status='queued'""",
                        (_json({"error": "Mission stop requested; queued research continuation cancelled"}),
                         "Mission stop requested", now, continuation_row["task_id"]))
            pending = self.db.execute("""SELECT count(*) FROM agent_tasks
                WHERE mission_id=? AND task_id!=? AND status IN ('queued','running')""",
                (mission_id, task["task_id"])).fetchone()[0]
            failed_ids = {row[0] for row in self.db.execute(
                "SELECT task_id FROM agent_tasks WHERE mission_id=? AND status='failed'", (mission_id,))}
            unread_feedback = (payload["decision"] == "stop" and bool(new_research_feedback(
                self, mission_id, reviewed_version, final_review=True)))
            deferred = payload["decision"] == "stop" and (
                pending > 0 or bool(failed_ids - set(reviewed_ids)) or unread_feedback)
            if deferred:
                self.db.execute("""INSERT OR REPLACE INTO deferred_mission_stops
                    (mission_id,reason,requested_at,reviewed_failed_task_ids_json) VALUES(?,?,?,?)""",
                    (mission_id, "Coordinator requested stop after queued tasks complete", now, _json(reviewed_ids)))
            else:
                self.db.execute("DELETE FROM deferred_mission_stops WHERE mission_id=?", (mission_id,))
            status = "active" if deferred or (payload["decision"] == "wait" and (pending or keep_active)) else {
                "schedule": "active", "wait": "paused", "stop": "stopped"}[payload["decision"]]
            self.db.execute("UPDATE missions SET status=?,version=?,updated_at=? WHERE mission_id=?",
                            (status, version, now, mission_id))
            self.db.execute("UPDATE agent_tasks SET status='completed',output_json=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE task_id=?",
                            (_json({"payload": payload}), now, task["task_id"]))
            self._commit()
        except Exception:
            self._rollback()
            raise
