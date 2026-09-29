"""Prepare configuration candidates against a Mission's unchanged source commit."""
import json
from .schema import ExperimentSpec
from .protocols import build_command
from .workspace import git


def validate_config_candidate(coordinator, mission, data, parent):
    payload = mission["payload"]
    if payload["authorization"].get("allow_code_candidates") is True:
        raise ValueError("configuration-only Mission must disable candidate code changes")
    protocol = payload["evaluator"]["protocol_id"]
    if data["protocol_id"] != protocol:
        raise ValueError("configuration candidate protocol differs from Mission")
    contract = json.loads((coordinator.root / "deployment/rsi/contracts.json").read_text())[protocol]
    if contract.get("candidate_mode") != "configuration_only":
        raise ValueError("protocol does not support configuration-only candidates")
    revision = payload["evaluator"].get("source_commit")
    if not isinstance(revision, str) or len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise ValueError("configuration-only Mission requires an immutable source_commit")
    source = git(coordinator.root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if parent and parent["code_commit"] != source:
        raise ValueError("configuration candidate cannot change parent source")
    spec = ExperimentSpec(data["candidate_id"], data["hypothesis"], protocol, data["config"],
                          resource_budget={"max_seconds": payload["resources"].get("max_seconds_per_run", 120)})
    record = {"protocol_id": protocol, "execution_target": "server", "command": (),
              "config": spec.config, "resource_budget": spec.resource_budget}
    build_command(record, coordinator.runtime_root / "validation-output", coordinator._config()["python"])
    return source
