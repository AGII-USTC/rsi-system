"""Read-only research history and Mission event API tests."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_research import research_root_id
from rsi_system.core.runtime_types import MissionSpec
from rsi_system.dashboard.configuration import Configuration
from rsi_system.dashboard import research_api
from rsi_system.dashboard.server import make_handler


@contextmanager
def api(root: Path):
    config = Configuration(root, config_dir=root / "private", pi_dir=root / "pi")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def get(path: str) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        def post(path: str, body: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
            try:
                conn.request("POST", path, json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        yield get, post
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _store(root: Path) -> RuntimeStore:
    store = RuntimeStore(root / ".rsi" / "runtime" / "state.sqlite3")
    store.create_mission(MissionSpec("m", "compare approaches"))
    return store


def test_missing_and_legacy_databases_are_explicit_and_unchanged(tmp_path):
    path = tmp_path / ".rsi" / "runtime" / "state.sqlite3"
    with api(tmp_path) as (get, _):
        for endpoint in ("research", "events"):
            status, body = get(f"/api/{endpoint}?mission_id=m")
            assert (status, body["code"]) == (404, "runtime_db_missing")
    assert not path.exists()

    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE missions(mission_id TEXT, version INTEGER);
            CREATE TABLE blackboard_events(event_id INTEGER PRIMARY KEY, mission_id TEXT,
              kind TEXT, payload_json TEXT, created_at TEXT);
            INSERT INTO missions VALUES('m',1);""")
    with api(tmp_path) as (get, _):
        for endpoint in ("research", "events"):
            status, body = get(f"/api/{endpoint}?mission_id=m")
            assert (status, body["code"]) == (409, "research_schema_unavailable")
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "missions", "blackboard_events"}


def test_graph_snapshot_history_and_state_bindings(tmp_path):
    with _store(tmp_path) as store:
        task = store.create_task("m", "coordinator", "coordinate", "start")
        first = store.event("m", "research_create", {"node_id": "branch"})
        payload = {"question": "why", "scope": "branch", "summary": "first",
                   "owner": "team", "execution_status": "proposed", "finding_status": "unknown",
                   "artifact_refs": [], "run_refs": []}
        store.db.execute("INSERT INTO research_nodes VALUES(?,?,?,?)", ("branch", "m", first, "now"))
        store.db.execute("INSERT INTO research_node_revisions VALUES(?,?,?,?,?,?)",
                         ("branch", 1, "m", json.dumps(payload), first, "now"))
        store.db.commit()
        second = store.event("m", "research_revise", {"node_id": "branch"})
        store.db.execute("INSERT INTO research_node_revisions VALUES(?,?,?,?,?,?)",
                         ("branch", 2, "m", json.dumps({**payload, "summary": "revised"}), second, "later"))
        store.db.commit()
        bound = store.create_task("m", "researcher", "investigate", "assigned",
                                  research_node_id="branch", research_revision=1)
        store.create_mission(MissionSpec("other", "separate"))
        store.event("other", "probe", {"value": 1})
        high = store.mission("m")["version"]
    with api(tmp_path) as (get, _):
        status, past = get(f"/api/research?mission_id=m&at_version={first}")
        assert status == 200
        assert past["cursor"] == first
        assert [(node["node_id"], node["revision"]) for node in past["nodes"]] == [("branch", 1)]
        assert len(past["revisions"]) == 1
        assert past["task_bindings"] == []
        status, current = get("/api/research?mission_id=m")
        assert status == 200
        assert current["cursor"] == high
        assert current["nodes"][0]["summary"] == "revised"
        assert [revision["revision"] for revision in current["revisions"]] == [1, 2]
        assert current["task_bindings"] == [{"task_id": bound["task_id"], "mission_id": "m",
                                             "node_id": "branch", "revision": 1}]
        status, state = get("/api/state?mission=m")
        assert status == 200
        visible = next(item for item in state["tasks"] if item["task_id"] == bound["task_id"])
        assert (visible["research_node_id"], visible["research_revision"]) == ("branch", 1)
        legacy = next(item for item in state["tasks"] if item["task_id"] == task["task_id"])
        assert (legacy["research_node_id"], legacy["research_revision"]) == (None, None)


def test_event_paging_uses_mission_versions_and_raw_payloads(tmp_path):
    with _store(tmp_path) as store:
        for index in range(165):
            store.event("m", "research_note", {"index": index, "nested": {"ok": True}})
            if index % 9 == 0:
                store.create_mission(MissionSpec(f"other-{index}", "separate"))
        high = store.mission("m")["version"]
    with api(tmp_path) as (get, _):
        status, first = get("/api/events?mission_id=m")
        assert status == 200
        assert len(first["events"]) == 150
        assert first["next_version"] == 150
        assert first["has_more"] is True
        assert first["high_water_mark"] == high
        assert [item["version"] for item in first["events"]] == list(range(1, 151))
        assert any(item["event_id"] != item["version"] for item in first["events"])
        assert first["events"][1]["payload"] == {"index": 0, "nested": {"ok": True}}
        status, second = get(f"/api/events?mission_id=m&after_version={first['next_version']}")
        assert status == 200
        assert [item["version"] for item in second["events"]] == list(range(151, high + 1))
        assert second["has_more"] is False
        assert second["next_version"] == high
        status, empty = get(f"/api/events?mission_id=m&after_version={high}")
        assert status == 200
        assert (empty["events"], empty["next_version"], empty["has_more"]) == ([], high, False)


def test_event_page_high_water_and_rows_share_one_snapshot(tmp_path, monkeypatch):
    with _store(tmp_path):
        pass
    original_mission = research_api._mission
    appended = False

    def append_after_high_water(db, mission_id):
        nonlocal appended
        row = original_mission(db, mission_id)
        if not appended:
            appended = True
            with RuntimeStore(tmp_path / ".rsi" / "runtime" / "state.sqlite3") as writer:
                writer.event("m", "later", {"value": 2})
        return row

    monkeypatch.setattr(research_api, "_mission", append_after_high_water)
    with api(tmp_path) as (get, _):
        status, first = get("/api/events?mission_id=m")
        assert status == 200
        assert first["high_water_mark"] == 1
        assert [item["version"] for item in first["events"]] == [1]
        status, second = get("/api/events?mission_id=m&after_version=1")
        assert status == 200
        assert second["high_water_mark"] == 2
        assert second["events"][0]["payload"] == {"value": 2}


@pytest.mark.parametrize("query", [
    "", "mission_id=", "mission_id=m&mission_id=other", "mission_id=m&bad=x",
    "mission_id=m&at_version=0", "mission_id=m&at_version=-1",
    "mission_id=m&at_version=1.5", "mission_id=m&at_version=2",
    "mission_id=m&at_version=01",
])
def test_research_rejects_malformed_or_out_of_range_queries(tmp_path, query):
    with _store(tmp_path):
        pass
    with api(tmp_path) as (get, _):
        assert get("/api/research?" + query)[0] == 400


@pytest.mark.parametrize("query", [
    "", "mission_id=m&after_version=-1", "mission_id=m&after_version=1.1",
    "mission_id=m&after_version=2", "mission_id=m&limit=0",
    "mission_id=m&limit=501", "mission_id=m&limit=bad", "mission_id=m&limit=2&limit=3",
])
def test_events_rejects_malformed_or_out_of_range_queries(tmp_path, query):
    with _store(tmp_path):
        pass
    with api(tmp_path) as (get, _):
        assert get("/api/events?" + query)[0] == 400


def test_unknown_mission_is_not_an_empty_graph(tmp_path):
    with _store(tmp_path):
        pass
    with api(tmp_path) as (get, _):
        for endpoint in ("research", "events"):
            status, body = get(f"/api/{endpoint}?mission_id=unknown")
            assert (status, body["code"]) == (404, "mission_not_found")


def test_human_proposal_is_versioned_and_visible_to_coordinator(tmp_path):
    with _store(tmp_path) as store:
        root_id = research_root_id("m")
        store.apply_research_operation("m", "create", {
            "node_id": root_id, "question": "Main question", "scope": "whole mission",
            "reason": "mission start"})
        before = store.mission("m")["version"]
    body = {"mission_id": "m", "parent": {"node_id": root_id, "revision": 1},
            "question": "Can a memory reader help?", "scope": "Explore retrieval at inference",
            "summary": "Keep the base model frozen."}
    with api(tmp_path) as (get, post):
        status, result = post("/api/research/nodes", body)
        assert status == 201
        node_id = result["node"]["node_id"]
        assert result["node"]["execution_status"] == "proposed"
        assert result["node"]["finding_status"] == "unknown"
        assert result["node"]["finding_status_basis"] == "human_proposal"
        assert result["relation"]["source"] == body["parent"]
        assert result["relation"]["target"] == {"node_id": node_id, "revision": 1}
        assert result["mission_version"] == before + 2
        assert node_id not in {node["node_id"] for node in
                               get(f"/api/research?mission_id=m&at_version={before}")[1]["nodes"]}
        assert node_id in {node["node_id"] for node in get("/api/research?mission_id=m")[1]["nodes"]}
    with RuntimeStore(tmp_path / ".rsi" / "runtime" / "state.sqlite3") as store:
        events = store.events("m")[-2:]
        assert [event["kind"] for event in events] == ["research_create", "direct_message"]
        assert events[-1]["payload"]["from_role"] == "human"
        assert events[-1]["payload"]["to_role"] == "coordinator"
        assert events[-1]["payload"]["proposal_id"] == node_id


def test_human_proposal_rejects_stale_or_foreign_parent_atomically(tmp_path):
    with _store(tmp_path) as store:
        root_id = research_root_id("m")
        store.apply_research_operation("m", "create", {
            "node_id": root_id, "question": "Main question", "scope": "whole mission",
            "reason": "mission start"})
        store.create_mission(MissionSpec("other", "Other work"))
        other_id = research_root_id("other")
        store.apply_research_operation("other", "create", {
            "node_id": other_id, "question": "Other question", "scope": "other mission",
            "reason": "mission start"})
        store.create_task("m", "coordinator", "Review", "Mission work")
        actor = store.claim_task("m")
        store.apply_research_operation("m", "revise", {
            "node_id": root_id, "expected_revision": 1, "reason": "New evidence",
            "summary": "Revised"}, actor_task_id=actor["task_id"])
        before = store.mission("m")["version"]
    body = {"mission_id": "m", "parent": {"node_id": root_id, "revision": 1},
            "question": "New branch?", "scope": "Check", "summary": "Proposed"}
    with api(tmp_path) as (_, post):
        assert post("/api/research/nodes", body)[0] == 400
        assert post("/api/research/nodes", {**body,
            "parent": {"node_id": other_id, "revision": 1}})[0] == 400
        assert post("/api/research/nodes", {**body, "question": " "})[0] == 400
    with RuntimeStore(tmp_path / ".rsi" / "runtime" / "state.sqlite3") as store:
        assert store.mission("m")["version"] == before
        assert not [n for n in store.research_snapshot("m")["nodes"]
                    if n["node_id"].startswith("human-")]
