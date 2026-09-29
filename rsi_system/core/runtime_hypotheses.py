"""Immutable hypothesis revisions and explicit Candidate admission rules."""
from __future__ import annotations
import json
from .hypothesis import HypothesisSpec
from .runtime_types import _json, _now, _text
from .schema import safe_id


class HypothesisRecords:
    def migrate_hypotheses(self):
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(hypotheses)")}
        if "version" not in columns:
            self.db.execute("ALTER TABLE hypotheses ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        self.db.execute("""CREATE TABLE IF NOT EXISTS hypothesis_revisions(
            hypothesis_id TEXT NOT NULL REFERENCES hypotheses(hypothesis_id), version INTEGER NOT NULL,
            payload_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(hypothesis_id,version))""")
        for row in self.db.execute("SELECT hypothesis_id FROM hypotheses").fetchall():
            h = self.hypothesis(row[0])
            self.db.execute("INSERT OR IGNORE INTO hypothesis_revisions VALUES(?,?,?,?,?)",
                            (h["hypothesis_id"], h["version"], _json(h), "existing record migration", h["updated_at"]))
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(candidates)")}
        for name, definition in (("hypothesis_version", "INTEGER"), ("kind", "TEXT NOT NULL DEFAULT 'legacy'"),
                                 ("admission_reason", "TEXT NOT NULL DEFAULT 'pre-versioned record'")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE candidates SET hypothesis_version=1 WHERE hypothesis_id IS NOT NULL AND hypothesis_version IS NULL")

    def create_hypothesis(self, mission_id, data):
        """Create v1 or replace the current projection using an explicit expected version."""
        spec = HypothesisSpec.from_payload(mission_id, data)
        with self.atomic():
            if self.mission(mission_id) is None:
                raise ValueError("unknown mission")
            previous = self.hypothesis(spec.hypothesis_id)
            expected = data.get("expected_version", 0)
            if type(expected) is not int or expected != (previous["version"] if previous else 0):
                raise ValueError("hypothesis revision requires the current expected_version")
            if previous and previous["mission_id"] != mission_id:
                raise ValueError("hypothesis belongs to another Mission")
            reason = _text(data.get("revision_reason"), "revision_reason") if previous else "initial structured hypothesis"
            version, now = expected + 1, _now()
            values = (spec.idea_ref, spec.statement, spec.mechanism, _json(list(spec.assumptions)),
                      _json(list(spec.predictions)), _json(spec.experimental_design), _json(spec.prior_art_status),
                      spec.status, now, version)
            if previous:
                self.db.execute("""UPDATE hypotheses SET idea_ref=?,statement=?,mechanism=?,assumptions_json=?,
                    predictions_json=?,experimental_design_json=?,prior_art_status_json=?,status=?,updated_at=?,version=?
                    WHERE hypothesis_id=?""", (*values, spec.hypothesis_id))
            else:
                self.db.execute("""INSERT INTO hypotheses(idea_ref,statement,mechanism,assumptions_json,
                    predictions_json,experimental_design_json,prior_art_status_json,status,updated_at,version,
                    hypothesis_id,mission_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, spec.hypothesis_id, mission_id, now))
            result = self.hypothesis(spec.hypothesis_id)
            self.db.execute("INSERT INTO hypothesis_revisions VALUES(?,?,?,?,?)",
                            (spec.hypothesis_id, version, _json(result), reason, now))
            self.event(mission_id, "hypothesis_revised" if previous else "hypothesis_registered",
                       {"hypothesis_id": spec.hypothesis_id, "version": version, "reason": reason, "status": spec.status})
        return result

    def hypothesis(self, hypothesis_id, version=None):
        safe_id(hypothesis_id, "hypothesis_id")
        if version is not None:
            row = self.db.execute("SELECT payload_json FROM hypothesis_revisions WHERE hypothesis_id=? AND version=?",
                                  (hypothesis_id, version)).fetchone()
            return json.loads(row[0]) if row else None
        row = self.db.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("assumptions_json", "predictions_json", "experimental_design_json", "prior_art_status_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def candidate_admission(self, mission_id, data):
        kind, hypothesis_id = data.get("kind", "hypothesis"), data.get("hypothesis_id")
        if kind == "baseline":
            if hypothesis_id is not None or data.get("parent_id") is not None:
                raise ValueError("baseline must be an unchanged root without a linked hypothesis")
            return None, "baseline", _text(data.get("baseline_reason"), "baseline_reason")
        if kind != "hypothesis" or hypothesis_id is None:
            raise ValueError("candidate requires a candidate_ready hypothesis or explicit baseline")
        version = data.get("hypothesis_version")
        linked = self.hypothesis(hypothesis_id, version) if type(version) is int and version > 0 else None
        if linked is None or linked["mission_id"] != mission_id or linked["status"] != "candidate_ready":
            raise ValueError("candidate requires a candidate_ready hypothesis_version in the same Mission")
        reason = (_text(data.get("parent_selection_reason"), "parent_selection_reason")
                  if data.get("parent_id") else "mature root hypothesis")
        return version, kind, reason
