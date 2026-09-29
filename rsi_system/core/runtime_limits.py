"""Durable invocation accounting and Mission evaluator binding."""
import hashlib
from .runtime_types import _json, _now


class RuntimeLimits:
    def migrate_limits(self):
        legacy = not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_agent_calls'").fetchone()
        self.db.execute("""CREATE TABLE IF NOT EXISTS runtime_agent_calls(
            task_id TEXT PRIMARY KEY REFERENCES agent_tasks(task_id), day TEXT NOT NULL,
            policy_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS evaluator_bindings(
            mission_id TEXT PRIMARY KEY REFERENCES missions(mission_id),
            contract_json TEXT NOT NULL, contract_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""")
        if legacy:
            self.db.execute("""INSERT OR IGNORE INTO runtime_agent_calls
                SELECT task_id,substr(updated_at,1,10),'{}',updated_at FROM agent_tasks
                WHERE role!='runner' AND attempt>0""")

    def reserve_agent_call(self, task, config, role):
        limit = config.get("runtime_max_agent_calls_per_day", config.get("max_agent_calls_per_day"))
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("runtime_max_agent_calls_per_day must be a positive integer or null")
        with self.atomic():
            self.validate_task(task)
            if self.db.execute("SELECT 1 FROM runtime_agent_calls WHERE task_id=?", (task["task_id"],)).fetchone():
                raise ValueError("agent invocation already reserved; reconcile before retry")
            now = _now()
            used = self.db.execute("SELECT count(*) FROM runtime_agent_calls WHERE day=?", (now[:10],)).fetchone()[0]
            if limit is not None and used >= limit:
                raise ValueError("daily agent invocation budget exhausted")
            selected = config.get("models", {}).get(role, {})
            policy = {"role": role,
                      "task_kind": task.get("scope", {}).get("task_kind", "standard"),
                      "model": dict(selected) if isinstance(selected, dict) else {},
                      "thinking": config.get("thinking", "high"),
                      "timeout_seconds": config["agent_timeout_seconds"]}
            self.db.execute("INSERT INTO runtime_agent_calls VALUES(?,?,?,?)", (task["task_id"], now[:10], _json(policy), now))

    def bind_evaluator(self, mission_id, contract):
        encoded = _json(contract)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.atomic():
            row = self.db.execute("SELECT contract_sha256 FROM evaluator_bindings WHERE mission_id=?", (mission_id,)).fetchone()
            if row and row[0] != digest:
                raise ValueError("Mission evaluator contract changed; comparisons require a new explicit Mission")
            self.db.execute("INSERT OR IGNORE INTO evaluator_bindings VALUES(?,?,?,?)", (mission_id, encoded, digest, _now()))
        return digest

    def remaining_runs(self, mission_id):
        limit = self.mission(mission_id)["payload"]["resources"].get("max_runs")
        used = self.db.execute("SELECT count(*) FROM agent_tasks WHERE mission_id=? AND role='runner'", (mission_id,)).fetchone()[0]
        return None if limit is None else max(0, limit - used)
