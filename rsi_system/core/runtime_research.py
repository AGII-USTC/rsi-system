"""Persistent research graph.

Scientific finding statuses are actor judgments with checked provenance, not
automatic conclusions from a successful Run. Canonical evidence references are
``run:<run_id>`` and ``handoff:<task_id>:<manifest_sha256>``. A handoff is
agent-authored and remains unverified scientific evidence.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Mapping

from .runtime_types import _json, _now, _object, _text
from .schema import safe_id


RELATION_TYPES = frozenset({"decomposes", "depends_on", "complements", "contradicts",
                            "composed_from", "supports", "refutes"})
SEMANTIC_TYPES = frozenset({"complements", "contradicts", "supports", "refutes"})
STRUCTURAL_TYPES = frozenset({"decomposes", "depends_on", "composed_from"})
RELATION_STATUSES = frozenset({"proposed", "accepted", "supported", "refuted", "withdrawn"})
EXECUTION_STATUSES = frozenset({"proposed", "active", "running", "blocked", "completed", "failed", "paused", "stopped"})
FINDING_STATUSES = frozenset({"unknown", "inconclusive", "supported", "refuted", "mixed"})


def research_root_id(mission_id: str) -> str:
    """Keep historical short IDs while bounding long Mission IDs to safe_id's limit."""
    mission_id = safe_id(mission_id, "mission_id")
    ordinary = f"{mission_id}-root"
    return (ordinary if len(ordinary) <= 96 else
            f"{mission_id[:74]}-{hashlib.sha256(mission_id.encode()).hexdigest()[:16]}-root")


def _refs(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return [item.strip() for item in value]


class ResearchGraph:
    def migrate_research(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS research_nodes(
                node_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                created_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_node_revisions(
                node_id TEXT NOT NULL REFERENCES research_nodes(node_id), revision INTEGER NOT NULL,
                mission_id TEXT NOT NULL REFERENCES missions(mission_id), payload_json TEXT NOT NULL,
                event_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(node_id,revision));
            CREATE INDEX IF NOT EXISTS research_nodes_by_mission ON research_nodes(mission_id,created_version);
            CREATE TABLE IF NOT EXISTS research_relations(
                edge_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                relation_type TEXT NOT NULL, source_node_id TEXT NOT NULL REFERENCES research_nodes(node_id),
                source_revision INTEGER NOT NULL, target_node_id TEXT NOT NULL REFERENCES research_nodes(node_id),
                target_revision INTEGER NOT NULL, created_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_relation_revisions(
                edge_id TEXT NOT NULL REFERENCES research_relations(edge_id), revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL, event_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(edge_id,revision));
            CREATE INDEX IF NOT EXISTS research_relations_by_mission ON research_relations(mission_id,created_version);
            CREATE TABLE IF NOT EXISTS research_compositions(
                composition_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                node_id TEXT NOT NULL UNIQUE REFERENCES research_nodes(node_id), rationale TEXT NOT NULL,
                plan_json TEXT NOT NULL, inputs_json TEXT NOT NULL, actor_task_id TEXT NOT NULL,
                event_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_composition_revisions(
                composition_id TEXT NOT NULL REFERENCES research_compositions(composition_id),
                revision INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                actor_task_id TEXT NOT NULL, event_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(composition_id,revision));
            CREATE INDEX IF NOT EXISTS research_composition_revisions_by_event
                ON research_composition_revisions(composition_id,event_version);
            CREATE TABLE IF NOT EXISTS research_task_bindings(
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(task_id),
                mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                node_id TEXT NOT NULL REFERENCES research_nodes(node_id), revision INTEGER NOT NULL);
        """)
        # Backfill an initial lifecycle row for databases created before lifecycle
        # records were introduced; source composition history remains untouched.
        self.db.execute("""INSERT OR IGNORE INTO research_composition_revisions
            (composition_id,revision,status,reason,actor_task_id,event_version,created_at)
            SELECT composition_id,1,'active',rationale,actor_task_id,event_version,created_at
            FROM research_compositions""")

    def _research_actor(self, mission_id: str, actor_task_id: str | None) -> dict[str, Any]:
        if actor_task_id is None:
            raise ValueError("research operation requires an actor task")
        actor = self.task(safe_id(actor_task_id, "actor_task_id"))
        if actor is None or actor["mission_id"] != mission_id:
            raise ValueError("research actor belongs to another Mission")
        self.validate_task(actor)
        return actor

    def _research_ref(self, mission_id: str, value: Any, label: str) -> dict[str, Any]:
        ref = _object(value, label)
        node_id = safe_id(ref.get("node_id"), f"{label}.node_id")
        revision = ref.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError(f"{label}.revision must be a positive integer")
        node = self.research_node(mission_id, node_id, revision)
        if node is None:
            raise ValueError(f"{label} is not an existing revision in this Mission")
        return {"node_id": node_id, "revision": revision}

    def _research_descendant(self, mission_id: str, ancestor_id: str, node_id: str) -> bool:
        if ancestor_id == node_id:
            return True
        rows = self.db.execute("""SELECT source_node_id,target_node_id FROM research_relations
            WHERE mission_id=? AND relation_type='decomposes'""", (mission_id,)).fetchall()
        children: dict[str, set[str]] = {}
        for row in rows:
            children.setdefault(row[0], set()).add(row[1])
        seen, stack = set(), [ancestor_id]
        while stack:
            current = stack.pop()
            if current == node_id:
                return True
            if current not in seen:
                seen.add(current)
                stack.extend(children.get(current, ()))
        return False

    def _research_authorize(self, mission_id: str, actor: Mapping[str, Any], *node_ids: str) -> None:
        if actor["role"] == "coordinator":
            return
        bound = actor["scope"]["research_node_id"]
        if not bound or any(not self._research_descendant(mission_id, bound, node_id) for node_id in node_ids):
            raise ValueError("research operation is outside actor's assigned branch")

    def _research_cycle(self, mission_id: str, source_id: str, target_id: str) -> None:
        if source_id == target_id:
            raise ValueError("research source/dependency cannot refer to itself")
        rows = self.db.execute("""SELECT source_node_id,target_node_id FROM research_relations
            WHERE mission_id=? AND relation_type IN ('decomposes','depends_on','composed_from')""",
                               (mission_id,)).fetchall()
        forward: dict[str, set[str]] = {}
        for row in rows:
            forward.setdefault(row[0], set()).add(row[1])
        stack, seen = [target_id], set()
        while stack:
            current = stack.pop()
            if current == source_id:
                raise ValueError("research source/dependency cycle")
            if current not in seen:
                seen.add(current)
                stack.extend(forward.get(current, ()))

    def _research_node_payload(self, data: Mapping[str, Any], previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
        fields = ("question", "scope", "summary", "owner", "execution_status", "finding_status",
                  "artifact_refs", "run_refs")
        base = {key: previous[key] for key in fields if previous is not None and key in previous}
        for key in fields:
            if key in data:
                base[key] = data[key]
        base["question"] = _text(base.get("question"), "question")
        base["scope"] = _text(base.get("scope"), "scope")
        base["summary"] = _text(base.get("summary", ""), "summary", required=False)
        base["owner"] = _text(base.get("owner", "unassigned"), "owner")
        base["execution_status"] = base.get("execution_status", "proposed")
        base["finding_status"] = base.get("finding_status", "unknown")
        if base["execution_status"] not in EXECUTION_STATUSES or base["finding_status"] not in FINDING_STATUSES:
            raise ValueError("invalid research node status")
        base["artifact_refs"] = _refs(base.get("artifact_refs"), "artifact_refs")
        base["run_refs"] = _refs(base.get("run_refs"), "run_refs")
        base["input_refs"] = list(previous.get("input_refs", [])) if previous else []
        base["finding_status_basis"] = "actor_judgment"
        return base

    def _research_validate_runs(self, mission_id: str, run_refs: list[str]) -> None:
        for run_id in run_refs:
            row = self.db.execute("SELECT mission_id FROM runs WHERE run_id=?", (safe_id(run_id, "run_id"),)).fetchone()
            if row is None or row[0] != mission_id:
                raise ValueError("research Run reference is not in this Mission")

    def _research_validate_evidence(self, mission_id: str, refs: list[str], *, required: bool) -> None:
        if required and not refs:
            raise ValueError("research finding requires evidence")
        for ref in refs:
            kind, separator, value = ref.partition(":")
            if not separator:
                raise ValueError("evidence reference must be run:<id> or handoff:<task_id>:<sha256>")
            if kind == "run":
                run_id = safe_id(value, "evidence run_id")
                row = self.db.execute("SELECT mission_id,evidence_json,evidence_sha256 FROM runs WHERE run_id=?",
                                      (run_id,)).fetchone()
                if (row is None or row["mission_id"] != mission_id or not row["evidence_json"]
                        or hashlib.sha256(row["evidence_json"].encode()).hexdigest() != row["evidence_sha256"]):
                    raise ValueError("evidence Run is absent, outside this Mission, or has no valid evidence")
            elif kind == "handoff":
                task_id, separator, digest = value.partition(":")
                task_id = safe_id(task_id, "evidence task_id")
                if not separator or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError("handoff evidence requires task_id and manifest SHA-256")
                task = self.db.execute("SELECT mission_id FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None or task["mission_id"] != mission_id:
                    raise ValueError("evidence handoff belongs to another Mission")
                published = False
                for row in self.db.execute("""SELECT payload_json FROM blackboard_events
                        WHERE mission_id=? AND kind='communication_published' ORDER BY version DESC""", (mission_id,)):
                    payload = json.loads(row["payload_json"])
                    if payload.get("task_id") == task_id and payload.get("manifest_sha256") == digest:
                        published = True
                        break
                if not published:
                    raise ValueError("evidence handoff is not published in this Mission")
                manifest = self.path.parent / "tasks" / task_id / "communications" / "outbox" / "manifest.json"
                if not manifest.is_file() or hashlib.sha256(manifest.read_bytes()).hexdigest() != digest:
                    raise ValueError("evidence handoff manifest is missing or changed")
                from .runtime_communications import load_manifest
                try:
                    load_manifest(manifest, {"mission_id": mission_id, "task_id": task_id})
                except (KeyError, TypeError, OSError, ValueError) as exc:
                    raise ValueError("evidence handoff snapshot is invalid") from exc
            else:
                raise ValueError("evidence reference must be run:<id> or handoff:<task_id>:<sha256>")

    def _research_validate_finding(self, mission_id: str, payload: Mapping[str, Any]) -> None:
        handoffs = [ref for ref in payload["artifact_refs"] if ref.startswith("handoff:")]
        refs = [f"run:{run_id}" for run_id in payload["run_refs"]] + handoffs
        self._research_validate_evidence(mission_id, refs,
            required=payload["finding_status"] in {"supported", "refuted", "mixed"})

    def _research_composition_contract(self, data: Mapping[str, Any], plan: Mapping[str, Any],
                                       inputs: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        """Normalize the auditable composition contract without changing source revisions.

        Older callers did not send the two contract fields. Their normalized values are
        deliberately explicit and conservative, so a persisted composition still states
        that it is a proposal and carries no experimental support claim.
        """
        supplied_contract = plan.get("contract", {})
        supplied_contract = _object(supplied_contract, "plan.contract") if supplied_contract else {}
        raw_assumptions = data.get("interaction_assumptions",
                                   supplied_contract.get("interaction_assumptions",
                                                        plan.get("interaction_assumptions")))
        if raw_assumptions is None:
            raw_assumptions = ["The selected source revisions may interact; this remains an unverified hypothesis."]
        if not isinstance(raw_assumptions, list) or not raw_assumptions:
            raise ValueError("interaction_assumptions must be a nonempty list")
        assumptions: list[Any] = []
        for index, item in enumerate(raw_assumptions):
            if isinstance(item, str):
                if not item.strip():
                    raise ValueError(f"interaction_assumptions[{index}] must be nonempty")
                assumptions.append(item.strip())
            elif isinstance(item, Mapping):
                item = _object(item, f"interaction_assumptions[{index}]")
                statement = item.get("statement", item.get("assumption"))
                if not isinstance(statement, str) or not statement.strip():
                    raise ValueError(f"interaction_assumptions[{index}] needs a nonempty statement")
                assumptions.append(item)
            else:
                raise ValueError(f"interaction_assumptions[{index}] must be a string or object")

        protocol = data.get("evaluation_protocol",
                            supplied_contract.get("evaluation_protocol", plan.get("evaluation_protocol")))
        if protocol is None:
            protocol = {"protocol_id": "unspecified", "status": "to_be_defined"}
        elif isinstance(protocol, str):
            protocol = {"protocol_id": protocol}
        else:
            protocol = _object(protocol, "evaluation_protocol")
        protocol_id = protocol.get("protocol_id", protocol.get("reference"))
        if not isinstance(protocol_id, str) or not protocol_id.strip():
            raise ValueError("evaluation_protocol must include a nonempty protocol_id")
        protocol["protocol_id"] = protocol_id.strip()

        selected = [{"node_id": ref["node_id"], "revision": ref["revision"],
                     "selection": ref["selection"]} for ref in inputs]
        lineage = data.get("lineage", supplied_contract.get("lineage", plan.get("lineage", {})))
        lineage = _object(lineage, "lineage") if lineage else {}
        lineage.update({
            "source_revisions": [{"node_id": ref["node_id"], "revision": ref["revision"]}
                                  for ref in inputs],
            "selected_content": selected,
            "composition_reason": reason,
            "evidence_status": "composition_proposal",
            "code_integration": "not_claimed",
            "experimental_support": "not_established",
        })
        return {"contract_version": 1, "inputs": inputs, "interaction_assumptions": assumptions,
                "evaluation_protocol": protocol, "output_node_revision": 1,
                "lineage": lineage}

    def _research_insert_node(self, mission_id: str, node_id: str, payload: Mapping[str, Any], version: int) -> dict[str, Any]:
        now = _now()
        if self.db.execute("SELECT 1 FROM research_nodes WHERE node_id=?", (node_id,)).fetchone():
            raise ValueError("research node already exists")
        self.db.execute("INSERT INTO research_nodes VALUES(?,?,?,?)", (node_id, mission_id, version, now))
        self.db.execute("INSERT INTO research_node_revisions VALUES(?,?,?,?,?,?)",
                        (node_id, 1, mission_id, _json(payload), version, now))
        return self.research_node(mission_id, node_id, 1)

    def _research_insert_relation(self, mission_id: str, relation_type: str, source: Mapping[str, Any],
                                  target: Mapping[str, Any], reason: str, status: str, evidence_refs: list[str],
                                  actor_task_id: str, version: int, edge_id: str | None = None) -> dict[str, Any]:
        if relation_type not in RELATION_TYPES or status not in RELATION_STATUSES:
            raise ValueError("invalid research relation type or status")
        if relation_type in STRUCTURAL_TYPES and status != "accepted":
            raise ValueError("structural research relation must be accepted")
        if relation_type in STRUCTURAL_TYPES:
            self._research_cycle(mission_id, source["node_id"], target["node_id"])
        edge_id = safe_id(edge_id or f"edge-{secrets.token_hex(10)}", "edge_id")
        if self.db.execute("SELECT 1 FROM research_relations WHERE edge_id=?", (edge_id,)).fetchone():
            raise ValueError("research relation already exists")
        now = _now()
        payload = {"reason": _text(reason, "reason"), "status": status, "evidence_refs": evidence_refs,
                   "actor_task_id": actor_task_id,
                   "status_basis": "actor_judgment" if relation_type in SEMANTIC_TYPES else "declared_structure"}
        self.db.execute("INSERT INTO research_relations VALUES(?,?,?,?,?,?,?,?,?)",
                        (edge_id, mission_id, relation_type, source["node_id"], source["revision"],
                         target["node_id"], target["revision"], version, now))
        self.db.execute("INSERT INTO research_relation_revisions VALUES(?,?,?,?,?)",
                        (edge_id, 1, _json(payload), version, now))
        return self._research_relation(mission_id, edge_id)

    def _research_relation(self, mission_id: str, edge_id: str, at_version: int | None = None) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT r.*,v.revision,v.payload_json,v.event_version,v.created_at AS revised_at
            FROM research_relations r JOIN research_relation_revisions v ON v.edge_id=r.edge_id
            WHERE r.mission_id=? AND r.edge_id=? AND (? IS NULL OR v.event_version<=?)
            ORDER BY v.revision DESC LIMIT 1""", (mission_id, edge_id, at_version, at_version)).fetchone()
        if row is None:
            return None
        return {**{k: row[k] for k in ("edge_id", "mission_id", "created_version", "created_at",
                                      "revision", "event_version", "revised_at")},
                "relation_id": row["edge_id"], "kind": row["relation_type"],
                "source": {"node_id": row["source_node_id"], "revision": row["source_revision"]},
                "target": {"node_id": row["target_node_id"], "revision": row["target_revision"]},
                **json.loads(row["payload_json"])}

    def _research_composition(self, mission_id: str, composition_id: str,
                              at_version: int | None = None) -> dict[str, Any] | None:
        row = self.db.execute("""SELECT c.*,v.revision AS lifecycle_revision,v.status,
            v.reason AS lifecycle_reason,v.actor_task_id AS lifecycle_actor_task_id,
            v.event_version AS lifecycle_event_version,v.created_at AS lifecycle_created_at
            FROM research_compositions c JOIN research_composition_revisions v
            ON v.composition_id=c.composition_id
            WHERE c.mission_id=? AND c.composition_id=?
              AND (? IS NULL OR v.event_version<=?)
            ORDER BY v.revision DESC LIMIT 1""",
            (mission_id, composition_id, at_version, at_version)).fetchone()
        if row is None:
            return None
        lifecycle_rows = self.db.execute("""SELECT revision,status,reason,actor_task_id,event_version,created_at
            FROM research_composition_revisions WHERE composition_id=?
              AND (? IS NULL OR event_version<=?) ORDER BY revision""",
            (composition_id, at_version, at_version)).fetchall()
        return {"composition_id": row["composition_id"], "mission_id": row["mission_id"],
                "node_id": row["node_id"], "reason": row["rationale"],
                "plan": json.loads(row["plan_json"]),
                "contract": json.loads(row["plan_json"]).get("contract", {}),
                "inputs": json.loads(row["inputs_json"]),
                "actor_task_id": row["actor_task_id"], "event_version": row["event_version"],
                "created_at": row["created_at"], "status": row["status"],
                "lifecycle_revision": row["lifecycle_revision"],
                "lifecycle": [dict(item) for item in lifecycle_rows]}

    def apply_research_operation(self, mission_id: str, action: str, data: Mapping[str, Any],
                                 *, actor_task_id: str | None = None) -> dict[str, Any]:
        mission_id = safe_id(mission_id, "mission_id")
        data = _object(data, "research operation")
        with self.atomic():
            mission = self.mission(mission_id)
            bootstrap = (actor_task_id is None and action == "create" and mission is not None
                         and mission["version"] == 1 and data.get("node_id") == research_root_id(mission_id)
                         and not data.get("parent") and not self.db.execute(
                             "SELECT 1 FROM agent_tasks WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone())
            actor = ({"role": "coordinator", "task_id": "system:mission_start"} if bootstrap
                     else self._research_actor(mission_id, actor_task_id))
            actor_id = actor_task_id or "system:mission_start"
            if action not in {"create", "revise", "relate", "compose", "revoke_composition", "rollback", "revoke"}:
                raise ValueError("unsupported research operation")
            reason = _text(data.get("reason"), "reason")
            version = self.mission(mission_id)["version"] + 1
            if action in {"revoke_composition", "rollback", "revoke"}:
                composition_id = safe_id(data.get("composition_id"), "composition_id")
                composition = self._research_composition(mission_id, composition_id)
                if composition is None:
                    raise ValueError("unknown composition in Mission")
                self._research_authorize(mission_id, actor, composition["node_id"],
                                         *(item["node_id"] for item in composition["inputs"]))
                expected = data.get("expected_revision", composition["lifecycle_revision"])
                if type(expected) is not int or expected != composition["lifecycle_revision"]:
                    raise ValueError("composition lifecycle revision conflict")
                if composition["status"] in {"revoked", "rolled_back"}:
                    raise ValueError("composition is already revoked")
                status = "rolled_back" if action == "rollback" else "revoked"
                now = _now()
                self.db.execute("INSERT INTO research_composition_revisions VALUES(?,?,?,?,?,?,?)",
                                (composition_id, composition["lifecycle_revision"] + 1, status,
                                 reason, actor_id, version, now))
                updated = self._research_composition(mission_id, composition_id)
                result = {"composition": updated, "node": self.research_node(
                    mission_id, composition["node_id"]), "relations": []}
                self.event(mission_id, f"research_{action}", {"schema_version": 1,
                           "actor_task_id": actor_id, "reason": reason, **result})
                return result
            if action == "create":
                node_id = safe_id(data.get("node_id") or f"research-{secrets.token_hex(10)}", "node_id")
                separate_root = data.get("separate_root", False)
                if type(separate_root) is not bool:
                    raise ValueError("separate_root must be a boolean")
                parent_data = data.get("parent")
                if separate_root and parent_data is not None:
                    raise ValueError("separate_root cannot be combined with parent")
                if separate_root and actor["role"] != "coordinator":
                    raise ValueError("only coordinator can create a separate root research node")
                parent = self._research_ref(mission_id, parent_data, "parent") if parent_data is not None else None
                if not parent and not separate_root and not bootstrap:
                    bound_id = actor.get("scope", {}).get("research_node_id")
                    if bound_id:
                        parent = self._research_ref(mission_id, {"node_id": bound_id,
                            "revision": actor["scope"]["research_revision"]}, "parent")
                    elif actor["role"] == "coordinator":
                        root = self.research_node(mission_id, research_root_id(mission_id))
                        if root is None:
                            # Pre-graph Missions may have a root with a historical ID.
                            rows = self.db.execute("""SELECT n.node_id FROM research_nodes n
                                WHERE n.mission_id=? AND NOT EXISTS (
                                    SELECT 1 FROM research_relations r
                                    WHERE r.mission_id=n.mission_id AND r.target_node_id=n.node_id
                                      AND r.relation_type IN ('decomposes','composed_from'))
                                ORDER BY n.created_version LIMIT 2""", (mission_id,)).fetchall()
                            if len(rows) == 1:
                                root = self.research_node(mission_id, rows[0]["node_id"])
                            elif len(rows) > 1:
                                raise ValueError("research parent is ambiguous; supply parent or separate_root")
                        if root:
                            parent = {"node_id": root["node_id"], "revision": root["revision"]}
                if parent:
                    self._research_authorize(mission_id, actor, parent["node_id"])
                elif actor["role"] != "coordinator":
                    raise ValueError("only coordinator can create a root research node")
                payload = self._research_node_payload(data)
                self._research_validate_runs(mission_id, payload["run_refs"])
                self._research_validate_finding(mission_id, payload)
                node = self._research_insert_node(mission_id, node_id, payload, version)
                relations = []
                if parent:
                    relations.append(self._research_insert_relation(mission_id, "decomposes", parent,
                                      {"node_id": node_id, "revision": 1}, reason, "accepted", [],
                                      actor_id, version))
                result = {"node": node}
            elif action == "revise":
                node_id = safe_id(data.get("node_id"), "node_id")
                current = self.research_node(mission_id, node_id)
                if current is None:
                    raise ValueError("unknown research node in Mission")
                self._research_authorize(mission_id, actor, node_id)
                if type(data.get("expected_revision")) is not int or data["expected_revision"] != current["revision"]:
                    raise ValueError("research node revision conflict")
                payload = self._research_node_payload(data, current)
                self._research_validate_runs(mission_id, payload["run_refs"])
                self._research_validate_finding(mission_id, payload)
                now = _now()
                self.db.execute("INSERT INTO research_node_revisions VALUES(?,?,?,?,?,?)",
                                (node_id, current["revision"] + 1, mission_id, _json(payload), version, now))
                result = {"node": self.research_node(mission_id, node_id)}
            elif action == "relate":
                if data.get("relation_id") and "expected_revision" in data:
                    edge_id = safe_id(data["relation_id"], "relation_id")
                    current = self._research_relation(mission_id, edge_id)
                    if current is None:
                        raise ValueError("unknown research relation in Mission")
                    self._research_authorize(mission_id, actor, current["source"]["node_id"],
                                             current["target"]["node_id"])
                    if (type(data["expected_revision"]) is not int
                            or data["expected_revision"] != current["revision"]):
                        raise ValueError("research relation revision conflict")
                    if current["kind"] in STRUCTURAL_TYPES:
                        raise ValueError("structural relation cannot be revised")
                    status = data.get("status", current["status"])
                    evidence = _refs(data.get("evidence_refs", current["evidence_refs"]), "evidence_refs")
                    if status not in RELATION_STATUSES:
                        raise ValueError("invalid research relation status")
                    self._research_validate_evidence(mission_id, evidence,
                        required=status in {"supported", "refuted"})
                    transitions = {"proposed": {"accepted", "withdrawn"},
                                   "accepted": {"supported", "refuted", "withdrawn"},
                                   "supported": {"refuted", "withdrawn"},
                                   "refuted": {"supported", "withdrawn"}, "withdrawn": set()}
                    if status != current["status"] and status not in transitions[current["status"]]:
                        raise ValueError("invalid research relation status transition")
                    payload = {"reason": reason, "status": status,
                               "evidence_refs": evidence,
                               "actor_task_id": actor_id, "status_basis": current["status_basis"]}
                    self.db.execute("INSERT INTO research_relation_revisions VALUES(?,?,?,?,?)",
                                    (edge_id, current["revision"] + 1, _json(payload), version, _now()))
                    result = {"relation": self._research_relation(mission_id, edge_id)}
                else:
                    source = self._research_ref(mission_id, data.get("source"), "source")
                    target = self._research_ref(mission_id, data.get("target"), "target")
                    self._research_authorize(mission_id, actor, source["node_id"], target["node_id"])
                    relation_type = data.get("kind")
                    if relation_type in {"decomposes", "composed_from"}:
                        raise ValueError("research lineage is created by create or compose")
                    status = data.get("status", "proposed" if relation_type in SEMANTIC_TYPES else "accepted")
                    evidence = _refs(data.get("evidence_refs"), "evidence_refs")
                    self._research_validate_evidence(mission_id, evidence,
                        required=status in {"supported", "refuted"})
                    relation = self._research_insert_relation(mission_id, relation_type, source, target,
                                                               reason, status, evidence, actor_id, version,
                                                               data.get("relation_id"))
                    result = {"relation": relation}
            else:
                raw_inputs = data.get("inputs")
                if not isinstance(raw_inputs, list) or len(raw_inputs) < 2:
                    raise ValueError("composition requires at least two inputs")
                inputs = []
                for item in raw_inputs:
                    ref = self._research_ref(mission_id, item, "composition input")
                    ref["selection"] = _text(item.get("selection", "whole revision"), "selection")
                    inputs.append(ref)
                if len({ref["node_id"] for ref in inputs}) != len(inputs):
                    raise ValueError("composition inputs must be distinct nodes")
                self._research_authorize(mission_id, actor, *(ref["node_id"] for ref in inputs))
                node_id = safe_id(data.get("node_id") or f"research-{secrets.token_hex(10)}", "node_id")
                payload = self._research_node_payload(data)
                payload["input_refs"] = inputs
                self._research_validate_runs(mission_id, payload["run_refs"])
                self._research_validate_finding(mission_id, payload)
                node = self._research_insert_node(mission_id, node_id, payload, version)
                composition_id = f"composition-{secrets.token_hex(10)}"
                now = _now()
                plan = _object(data.get("plan", {}), "plan")
                contract = self._research_composition_contract(data, plan, inputs, reason)
                plan = {**plan, "contract": contract}
                payload["composition_contract"] = contract
                # The node was inserted above to keep the existing insertion path. Update
                # its revision payload in place before returning it; revision remains 1.
                self.db.execute("""UPDATE research_node_revisions SET payload_json=?
                                 WHERE node_id=? AND revision=1""",
                                (_json(payload), node_id))
                node = self.research_node(mission_id, node_id, 1)
                self.db.execute("INSERT INTO research_compositions VALUES(?,?,?,?,?,?,?,?,?)",
                                (composition_id, mission_id, node_id, reason, _json(plan), _json(inputs),
                                 actor_id, version, now))
                self.db.execute("INSERT INTO research_composition_revisions VALUES(?,?,?,?,?,?,?)",
                                (composition_id, 1, "active", reason, actor_id, version, now))
                relations = [self._research_insert_relation(mission_id, "composed_from", ref,
                             {"node_id": node_id, "revision": 1}, reason, "accepted", [], actor_id,
                             version) for ref in inputs]
                result = {"node": node, "composition": self._research_composition(mission_id, composition_id),
                          "relations": relations}
            event_result = {**result, "relations": relations} if action == "create" else result
            self.event(mission_id, f"research_{action}", {"schema_version": 1, "actor_task_id": actor_id,
                       "reason": reason, **event_result})
            return result

    def research_node(self, mission_id: str, node_id: str, revision: int | None = None) -> dict[str, Any] | None:
        mission_id, node_id = safe_id(mission_id, "mission_id"), safe_id(node_id, "node_id")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("revision must be a positive integer")
        row = self.db.execute("""SELECT n.created_at,n.created_version,v.revision,v.payload_json,
            v.event_version,v.created_at AS revised_at FROM research_nodes n
            JOIN research_node_revisions v ON v.node_id=n.node_id
            WHERE n.mission_id=? AND n.node_id=? AND (? IS NULL OR v.revision=?)
            ORDER BY v.revision DESC LIMIT 1""", (mission_id, node_id, revision, revision)).fetchone()
        return ({"node_id": node_id, "mission_id": mission_id, "revision": row["revision"],
                 "created_version": row["created_version"], "event_version": row["event_version"],
                 "created_at": row["created_at"], "revised_at": row["revised_at"],
                 **json.loads(row["payload_json"])} if row else None)

    def research_snapshot(self, mission_id: str, *, at_version: int | None = None) -> dict[str, Any]:
        if not self.db.in_transaction:
            self.db.execute("BEGIN")
            try:
                return self.research_snapshot(mission_id, at_version=at_version)
            finally:
                self.db.rollback()
        mission = self.mission(safe_id(mission_id, "mission_id"))
        if mission is None:
            raise ValueError("unknown mission")
        cursor = mission["version"] if at_version is None else at_version
        if type(cursor) is not int or not 1 <= cursor <= mission["version"]:
            raise ValueError("invalid research snapshot version")
        nodes, revisions = [], []
        rows = self.db.execute("""SELECT n.node_id,v.revision,v.payload_json,v.event_version,v.created_at AS revised_at,
            n.created_at,n.created_version FROM research_nodes n JOIN research_node_revisions v ON v.node_id=n.node_id
            WHERE n.mission_id=? AND v.event_version<=? ORDER BY n.created_version,v.revision""",
                               (mission_id, cursor)).fetchall()
        latest = {}
        for row in rows:
            node = {"node_id": row["node_id"], "mission_id": mission_id, "revision": row["revision"],
                    "created_version": row["created_version"], "event_version": row["event_version"],
                    "created_at": row["created_at"], "revised_at": row["revised_at"],
                    **json.loads(row["payload_json"])}
            revisions.append(node)
            latest[row["node_id"]] = node
        nodes = list(latest.values())
        edge_ids = [row[0] for row in self.db.execute("SELECT edge_id FROM research_relations WHERE mission_id=? AND created_version<=? ORDER BY created_version,rowid", (mission_id, cursor))]
        relations = [self._research_relation(mission_id, edge_id, cursor) for edge_id in edge_ids]
        compositions = []
        for row in self.db.execute("SELECT * FROM research_compositions WHERE mission_id=? AND event_version<=? ORDER BY event_version,rowid", (mission_id, cursor)):
            composition = self._research_composition(mission_id, row["composition_id"], cursor)
            if composition is not None:
                compositions.append(composition)
        bindings = [dict(row) for row in self.db.execute("""SELECT b.* FROM research_task_bindings b
            JOIN agent_tasks t ON t.task_id=b.task_id JOIN blackboard_events e
            ON e.mission_id=t.mission_id AND e.kind='task_created'
            AND json_extract(e.payload_json,'$.task_id')=b.task_id
            WHERE b.mission_id=? AND e.version<=? ORDER BY e.version""", (mission_id, cursor))]
        return {"mission_id": mission_id, "cursor": cursor, "nodes": nodes, "revisions": revisions,
                "relations": relations, "compositions": compositions, "task_bindings": bindings}
