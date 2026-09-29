"""Read-only research graph and Mission event queries for the dashboard."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs

from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.schema import safe_id


DEFAULT_EVENT_LIMIT = 150
MAX_EVENT_LIMIT = 500
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class ResearchReadError(ValueError):
    def __init__(self, message: str, *, status: int, code: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _query(query: str, allowed: set[str]) -> dict[str, str]:
    values = parse_qs(query, keep_blank_values=True)
    if set(values) - allowed or any(len(items) != 1 for items in values.values()):
        raise ValueError("invalid research query parameters")
    return {key: items[0] for key, items in values.items()}


def _mission_id(values: dict[str, str]) -> str:
    return safe_id(values.get("mission_id", ""), "mission_id")


def _integer(values: dict[str, str], key: str, default: int | None, *,
             minimum: int, maximum: int | None = None) -> int | None:
    raw = values.get(key)
    if raw is None:
        return default
    if not _DECIMAL.fullmatch(raw):
        raise ValueError(f"{key} must be a decimal integer")
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{key} is out of range")
    return value


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _open(root: Path, required: dict[str, set[str]]) -> RuntimeStore:
    path = root / ".rsi" / "runtime" / "state.sqlite3"
    if not path.is_file():
        raise ResearchReadError("runtime database is not available", status=404, code="runtime_db_missing")
    store = RuntimeStore.open_readonly(path)
    try:
        db = store.db
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        absent = [table for table, columns in required.items() if not columns <= _columns(db, table)]
        if absent:
            raise ResearchReadError("research schema is unavailable: " + ", ".join(absent),
                                    status=409, code="research_schema_unavailable")
        return store
    except BaseException:
        store.close()
        raise


def _mission(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    row = db.execute("SELECT mission_id,version FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
    if row is None:
        raise ResearchReadError("unknown mission", status=404, code="mission_not_found")
    return row


def research(root: Path, query: str) -> dict:
    values = _query(query, {"mission_id", "at_version"})
    mission_id = _mission_id(values)
    at_version = _integer(values, "at_version", None, minimum=1)
    store = _open(root, {
        "missions": {"mission_id", "version", "payload_json", "status", "created_at", "updated_at"},
        "research_nodes": {"node_id", "mission_id", "created_version", "created_at"},
        "research_node_revisions": {"node_id", "revision", "mission_id", "payload_json", "event_version", "created_at"},
        "research_relations": {"edge_id", "mission_id", "relation_type", "source_node_id",
                               "source_revision", "target_node_id", "target_revision",
                               "created_version", "created_at"},
        "research_relation_revisions": {"edge_id", "revision", "payload_json", "event_version", "created_at"},
        "research_compositions": {"composition_id", "mission_id", "node_id", "rationale",
                                  "plan_json", "inputs_json", "actor_task_id", "event_version", "created_at"},
        "research_task_bindings": {"task_id", "mission_id", "node_id", "revision"},
        "agent_tasks": {"task_id", "mission_id"},
        "blackboard_events": {"mission_id", "version", "kind", "payload_json"},
    })
    try:
        db = store.db
        mission = _mission(db, mission_id)
        if at_version is not None and at_version > mission["version"]:
            raise ValueError("at_version is out of range")
        return store.research_snapshot(mission_id, at_version=at_version)
    finally:
        store.close()


def events(root: Path, query: str) -> dict:
    values = _query(query, {"mission_id", "after_version", "limit"})
    mission_id = _mission_id(values)
    after_version = _integer(values, "after_version", 0, minimum=0)
    limit = _integer(values, "limit", DEFAULT_EVENT_LIMIT, minimum=1, maximum=MAX_EVENT_LIMIT)
    assert after_version is not None and limit is not None
    store = _open(root, {
        "missions": {"mission_id", "version"},
        "blackboard_events": {"event_id", "mission_id", "version", "kind", "payload_json", "created_at"},
    })
    try:
        db = store.db
        high_water = _mission(db, mission_id)["version"]
        if after_version > high_water:
            raise ValueError("after_version is out of range")
        rows = db.execute("""SELECT event_id,mission_id,version,kind,payload_json,created_at
            FROM blackboard_events WHERE mission_id=? AND version>? AND version<=?
            ORDER BY version ASC LIMIT ?""",
            (mission_id, after_version, high_water, limit + 1)).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        result = [{"event_id": row["event_id"], "mission_id": row["mission_id"],
                   "version": row["version"], "kind": row["kind"],
                   "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
                  for row in page]
        return {"mission_id": mission_id, "events": result,
                "next_version": page[-1]["version"] if page else after_version,
                "has_more": has_more, "high_water_mark": high_water}
    finally:
        store.close()
