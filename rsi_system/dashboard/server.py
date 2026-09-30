"""Run a local RSI collaboration and configuration dashboard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from .configuration import Configuration
from .mission_control import MissionControl, RuntimeUnavailable
from .research_api import ResearchReadError, events as research_events, propose_node, research


STATIC = Path(__file__).parent / "static"
LIMITS = {"missions": 30, "tasks": 160, "events": 150, "runs": 30}
TABLES = {"missions": "missions", "tasks": "agent_tasks",
          "events": "blackboard_events", "runs": "runs"}


def _json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _limited(db: sqlite3.Connection, table: str, columns: str, order: str,
             where: str = "", args: tuple = ()) -> tuple[list[dict], int]:
    total = db.execute(f"SELECT count(*) FROM {TABLES[table]} {where}", args).fetchone()[0]
    rows = [dict(row) for row in db.execute(
        f"SELECT {columns} FROM {TABLES[table]} {where} ORDER BY {order} LIMIT ?",
        (*args, LIMITS[table]))]
    return rows, total


def _event(row: dict, objective: str) -> dict:
    event = dict(row)
    payload = _json(event.pop("payload_json"))
    kind = event["kind"]
    event.update(task_id=payload.get("task_id"), from_task_id=None, to_task_id=None,
                 from_role=None, to_role=None, text="", title="")
    if kind == "task_created":
        event.update(title="任务派发", text=payload.get("objective", ""),
                     from_task_id=payload.get("parent_task_id"), to_task_id=payload.get("task_id"),
                     to_role=payload.get("role"), from_role="coordinator" if
                     payload.get("role") != "coordinator" and not payload.get("parent_task_id") else None)
    elif kind == "direct_message":
        event.update(title="协作消息", text=payload.get("text", ""),
                     from_task_id=payload.get("from_task_id"), from_role=payload.get("from_role"),
                     to_task_id=payload.get("to_task_id"), to_role=payload.get("to_role"))
    elif kind == "communication_published":
        files = payload.get("files", [])
        event.update(title="交接发布", text=f"发布 {len(files) if isinstance(files, list) else 0} 个文件到任务黑板",
                     from_task_id=payload.get("task_id"), from_role=payload.get("author_role"))
    elif kind in {"mission_started", "mission_created"}:
        event.update(title="任务启动", text=objective)
        if kind == "mission_created":
            event["title"] = "任务创建"
    elif kind in {"idea_written_to_wiki", "run_written_to_wiki", "knowledge_read"}:
        event.update(title="知识读取" if kind == "knowledge_read" else "知识写入",
                     from_task_id=payload.get("task_id"), from_role=payload.get("role"),
                     text=payload.get("path") or payload.get("query") or payload.get("run_id") or "",
                     knowledge_action="read" if kind == "knowledge_read" else "write",
                     knowledge_files=payload.get("files", payload.get("evidence", [])),
                     evidence_status=payload.get("evidence_status", "recorded runtime operation; not a verified conclusion"))
        if payload.get("path"):
            event["knowledge_files"] = [{"path": payload["path"]}]
        if kind == "run_written_to_wiki" and payload.get("kind") == "exploration" and not payload.get("path"):
            event.update(title="证据归档", knowledge_action="archive")
    elif kind == "mission_status":
        event.update(title="任务状态", text=payload.get("reason", payload.get("status", "")))
    elif kind == "agent_tool_audit":
        calls = payload.get("calls", [])
        event.update(title="工具调用", text=f"记录 {len(calls) if isinstance(calls, list) else 0} 次工具调用",
                     from_task_id=payload.get("task_id"), from_role=payload.get("role"))
    else:
        event.update(title={"candidate_registered": "候选登记", "idea_drafted": "想法记录",
                            "runner_report": "实验结果", "coordinator_decision": "协调决策",
                            "task_delegated": "任务委派"}.get(kind, kind.replace("_", " ")),
                     text=payload.get("summary") or payload.get("reason") or
                     payload.get("decision") or payload.get("candidate_id") or "")
    event["text"] = str(event["text"])[:4000]
    return event


def snapshot(root: Path, mission_id: str | None = None) -> dict:
    path = root / ".rsi" / "runtime" / "state.sqlite3"
    result = {"missions": [], "mission": None, "tasks": [], "events": [], "runs": [],
              "counts": {key: 0 for key in LIMITS}, "truncated": {key: False for key in LIMITS},
              "updated_at": datetime.now(timezone.utc).isoformat()}
    if not path.is_file():
        return result
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        required = ("missions", "agent_tasks", "blackboard_events", "runs")
        missing = [table for table in required if not _columns(db, table)]
        if missing:
            raise ValueError(f"运行时数据库缺少表: {', '.join(missing)}")
        missions, total = _limited(
            db, "missions", "mission_id,status,version,created_at,updated_at,payload_json",
            "updated_at DESC, mission_id DESC")
        result["counts"]["missions"] = total
        result["truncated"]["missions"] = total > len(missions)
        if not missions:
            if mission_id:
                raise ValueError("未找到这个研究任务")
            return result
        for item in missions:
            item["objective"] = _json(item.pop("payload_json")).get("objective", "")
        selected = next((item for item in missions if item["mission_id"] == mission_id), None)
        if mission_id and selected is None:
            row = db.execute("SELECT mission_id,status,version,created_at,updated_at,payload_json "
                             "FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                raise ValueError("未找到这个研究任务")
            selected = dict(row)
            selected["objective"] = _json(selected.pop("payload_json")).get("objective", "")
            missions.append(selected)
        if mission_id is None and not any(item["status"] == "active" for item in missions):
            row = db.execute("SELECT mission_id,status,version,created_at,updated_at,payload_json "
                             "FROM missions WHERE status='active' ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row is not None:
                active = dict(row)
                active["objective"] = _json(active.pop("payload_json")).get("objective", "")
                missions.append(active)
        selected = selected or next((item for item in missions if item["status"] == "active"), missions[0])
        mid = selected["mission_id"]
        result["missions"], result["mission"] = missions, selected

        scopes = _columns(db, "task_scopes")
        research_bindings = _columns(db, "research_task_bindings")
        operations = _columns(db, "rsi_tool_operations")
        scope_join = "LEFT JOIN task_scopes s ON s.task_id=t.task_id" if scopes else ""
        binding_join = (" LEFT JOIN research_task_bindings b ON b.task_id=t.task_id"
                        if {"task_id", "node_id", "revision"} <= research_bindings else "")
        binding_fields = ("b.node_id AS research_node_id,b.revision AS research_revision"
                          if binding_join else "NULL AS research_node_id,NULL AS research_revision")
        scope_fields = ", ".join(f"s.{field}" if field in scopes else f"NULL AS {field}"
                                 for field in ("parent_task_id", "reply_to_task_id", "idea_id"))
        waiting = ("EXISTS(SELECT 1 FROM rsi_tool_operations o WHERE o.task_id=t.task_id "
                   "AND o.tool='rsi_wait' AND o.status='pending')"
                   if {"task_id", "tool", "status"} <= operations else "0")
        tasks, total = _limited(db, "tasks",
            f"t.task_id,t.role,t.status,t.objective,t.reason,t.candidate_id,"
            f"t.output_json,t.error,t.created_at,t.updated_at,{scope_fields},{binding_fields},{waiting} AS waiting",
            "t.created_at DESC,t.task_id DESC", f"AS t {scope_join}{binding_join} WHERE t.mission_id=?", (mid,))
        result["counts"]["tasks"] = total
        result["truncated"]["tasks"] = total > len(tasks)
        for item in tasks:
            output = _json(item.pop("output_json"))
            payload = output.get("payload", {})
            item["summary"] = payload.get("summary", "") if isinstance(payload, dict) else ""
            item["waiting"] = bool(item["waiting"])
        if not scopes:
            legacy_scopes = {}
            for row in db.execute("SELECT payload_json FROM blackboard_events "
                                  "WHERE mission_id=? AND kind='task_created'", (mid,)):
                payload = _json(row["payload_json"])
                if payload.get("task_id"):
                    legacy_scopes[payload["task_id"]] = payload
            for item in tasks:
                payload = legacy_scopes.get(item["task_id"], {})
                for field in ("parent_task_id", "reply_to_task_id", "idea_id"):
                    item[field] = payload.get(field)
        result["tasks"] = tasks

        events, total = _limited(db, "events", "event_id,kind,payload_json,created_at",
                                 "event_id DESC", "WHERE mission_id=?", (mid,))
        result["counts"]["events"] = total
        result["truncated"]["events"] = total > len(events)
        result["events"] = [_event(row, selected["objective"]) for row in events]
        run_columns = _columns(db, "runs")
        run_fields = ",".join(field if field in run_columns else f"NULL AS {field}"
                              for field in ("run_id", "status", "kind", "candidate_id", "idea_ref", "created_at"))
        runs, total = _limited(db, "runs", run_fields, "created_at DESC,run_id DESC",
                               "WHERE mission_id=?", (mid,))
        result["counts"]["runs"] = total
        result["truncated"]["runs"] = total > len(runs)
        result["runs"] = runs
        return result
    finally:
        db.close()


def _static_file(path: str) -> Path | None:
    decoded = unquote(path)
    if decoded in {"/", "/index.html"}:
        decoded = "/index.html"
    if (not decoded.startswith("/") or "\\" in decoded or
            any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
        return None
    parts = Path(decoded.lstrip("/")).parts
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        return None
    target = STATIC.joinpath(*parts)
    if not target.is_file() or not target.resolve().is_relative_to(STATIC.resolve()):
        return None
    return target


def make_handler(root: Path, configuration: Configuration, *, control: MissionControl | None = None):
    control = control or MissionControl(root)
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, status: int, value: dict) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _local(self, modifying: bool = False) -> bool:
            host = self.headers.get("Host", "")
            if host not in {f"{name}:{self.server.server_port}" for name in ("127.0.0.1", "localhost")} and host not in {"127.0.0.1", "localhost"}:
                self._reply(403, {"error": "仅允许通过本机访问"})
                return False
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self._reply(403, {"error": "仅允许通过本机访问"})
                return False
            if modifying:
                origin = self.headers.get("Origin")
                if (origin and origin != f"http://{host}") or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self._reply(403, {"error": "跨站请求已拒绝"})
                    return False
                if self.headers.get_content_type() != "application/json":
                    self._reply(415, {"error": "请求必须是 JSON"})
                    return False
            return True

        def do_GET(self) -> None:
            if not self._local():
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/api/state":
                    mission = parse_qs(url.query).get("mission", [None])[0]
                    self._reply(200, snapshot(root, mission))
                elif url.path == "/api/research":
                    self._reply(200, research(root, url.query))
                elif url.path == "/api/events":
                    self._reply(200, research_events(root, url.query))
                elif url.path == "/api/config":
                    self._reply(200, configuration.public())
                elif url.path == "/api/runtime":
                    self._reply(200, control.public())
                else:
                    if url.path.startswith("/api/"):
                        self._reply(404, {"error": "未找到接口"})
                        return
                    target = _static_file(url.path)
                    if target is None:
                        self._reply(404, {"error": "未找到页面"})
                        return
                    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    self._send(200, target.read_bytes(), content_type)
            except ResearchReadError as exc:
                self._reply(exc.status, {"error": str(exc), "code": exc.code})
            except (ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})
            except (OSError, sqlite3.DatabaseError):
                self._reply(500, {"error": "读取状态失败"})

        def do_PUT(self) -> None:
            if not self._local(modifying=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 16_384:
                    raise ValueError("配置请求过大或为空")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("配置必须是 JSON 对象")
                if self.path == "/api/provider":
                    self._reply(200, configuration.save_provider(body))
                elif self.path == "/api/roles":
                    self._reply(200, configuration.save_roles(body))
                else:
                    self._reply(404, {"error": "未找到配置入口"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})
            except OSError:
                self._reply(500, {"error": "保存配置失败"})

        def do_POST(self) -> None:
            if not self._local(modifying=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32_768:
                    raise ValueError("任务请求过大或为空")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求必须是 JSON 对象")
                if self.path == "/api/provider/discover":
                    self._reply(200, configuration.discover_provider(body))
                    return
                if self.path == "/api/missions":
                    self._reply(201, control.create(body))
                    return
                if self.path == "/api/research/nodes":
                    self._reply(201, propose_node(root, body))
                    return
                match = re.fullmatch(r"/api/missions/([A-Za-z0-9._-]+)/(run|pause|resume)", self.path)
                if match:
                    result = control.action(match[1], match[2], body)
                    self._reply(202 if result["status"] == "running" else 200, result)
                    return
                self._reply(404, {"error": "未找到任务入口"})
            except RuntimeUnavailable as exc:
                self._reply(409, {"error": str(exc), "runtime": control.public()["runtime"]})
            except ResearchReadError as exc:
                self._reply(exc.status, {"error": str(exc), "code": exc.code})
            except (ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})
            except (OSError, sqlite3.DatabaseError):
                self._reply(500, {"error": "任务操作失败"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(root, Configuration(root)))
    print(f"RSI dashboard: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
