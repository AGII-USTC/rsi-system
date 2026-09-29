"""One deterministic experiment per runner task, including restart reconciliation."""
from __future__ import annotations
import hashlib
import json
from .schema import ExperimentSpec
from .store import ExperimentStore
from .worker import run_once
from .runtime_knowledge import reconcile_runtime_knowledge
from .algorithm_manifest import manifest_json, manifest_sha256
from .runtime_evaluation_summary import persist_evaluation_summary


def execute_runner(coordinator, store, mission, task):
    candidate = store.candidate(task.get("candidate_id")) if task.get("candidate_id") else None
    if candidate is None:
        from .runtime_exploration import execute_exploration
        return execute_exploration(coordinator, store, mission, task)
    coordinator._validate_run(mission, candidate)
    contracts = json.loads((coordinator.root / "deployment/rsi/contracts.json").read_text())
    contract = contracts.get(candidate["protocol_id"])
    if not isinstance(contract, dict):
        raise ValueError("candidate protocol has no evaluator contract")
    contract_hash = store.bind_evaluator(mission["mission_id"], contract)
    for key, value in contract.get("fixed_config", {}).items():
        if candidate["config"].get(key) != value:
            raise ValueError(f"candidate changes fixed evaluator input: {key}")
    run_id = "run-" + hashlib.sha256(task["task_id"].encode()).hexdigest()[:28]
    research_ref = {"node_id": task["scope"].get("research_node_id"),
                    "revision": task["scope"].get("research_revision")}
    compositions = [item for item in store.research_snapshot(mission["mission_id"])["compositions"]
                    if item["node_id"] == research_ref["node_id"]]
    composition = compositions[0] if compositions else None
    algorithm = candidate.get("algorithm", {})
    inputs = {"algorithm": algorithm, "algorithm_sha256": manifest_sha256(algorithm), "protocol_id": candidate["protocol_id"], "config": candidate["config"],
              "dataset_version": contract["dataset_version"], "evaluator_id": contract["evaluator_id"],
              "contract_sha256": contract_hash,
              "resource_budget": {"max_seconds": mission["payload"]["resources"].get("max_seconds_per_run", 120)}}
    if research_ref["node_id"]:
        inputs.update({"research_node": research_ref,
                       "composition_inputs": composition["inputs"] if composition else [],
                       "source_run_ids": composition["plan"].get("frozen_source_run_ids", []) if composition else []})
    spec = ExperimentSpec(run_id, candidate["hypothesis"], candidate["protocol_id"], candidate["config"],
                          parent_id=None, dataset_version=contract["dataset_version"],
                          evaluator_id=contract["evaluator_id"], execution_target="server",
                          resource_budget={"max_seconds": mission["payload"]["resources"].get("max_seconds_per_run", 120)})
    directory = coordinator.runtime_root / "tasks" / task["task_id"]
    directory.mkdir(parents=True, exist_ok=True)
    from .pi_agent import atomic_json
    pinned_path = directory / "runner_input.json"
    pinned = {"source_commit": candidate["code_commit"], "inputs": inputs, "algorithm_manifest": manifest_json(algorithm), "spec": spec.to_json_fields()}
    if pinned_path.exists():
        if json.loads(pinned_path.read_text()) != pinned:
            raise ValueError("runner inputs or evaluator changed since task registration")
    else:
        atomic_json(pinned_path, pinned)
    # Reserve the Run before launch; the private worker registry owns process recovery.
    with store.atomic():
        store.validate_task(task)
        store.create_run(mission["mission_id"], candidate["candidate_id"], task["task_id"],
                         candidate["purpose"], "prepared", candidate["code_commit"], inputs, None)
    # A private task registry prevents the worker from claiming another queued experiment.
    with ExperimentStore(directory / "experiment.sqlite3") as experiments:
        record = experiments.get(run_id) or experiments.create(spec)
        transitions = {"proposed": "awaiting_approval", "awaiting_approval": "approved", "approved": "queued"}
        while record["status"] in transitions:
            record = experiments.transition(run_id, transitions[record["status"]], note="authorized runtime Runner")
        if record.get("run") is None:
            run_once(experiments, coordinator.root, coordinator.runtime_root / "runs", python=coordinator._config()["python"],
                     sandbox=coordinator._config().get("sandbox", True), source_commit=candidate["code_commit"])
        evidence = experiments.get(run_id).get("run")
    if evidence is None:
        raise ValueError("Runner could not reconcile its assigned experiment")
    with store.atomic():
        store.validate_task(task)
        runtime_run = store.create_run(mission["mission_id"], candidate["candidate_id"], task["task_id"],
                                       candidate["purpose"], evidence["status"], evidence.get("source_commit"), inputs, evidence)
        if runtime_run["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
            runtime_run = store.finalize_run(runtime_run["run_id"], evidence["status"], evidence)
        if runtime_run["status"] == "completed" and evidence.get("artifact_dir"):
            summary_path, summary = persist_evaluation_summary(
                runtime_run, artifact_dir=evidence["artifact_dir"], candidate=candidate,
                protocol_metrics=evidence.get("metrics"))
            evidence = {**evidence, "evaluation_summary_path": str(summary_path),
                        "evaluation_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                        "artifact_hashes": {**evidence.get("artifact_hashes", {}),
                                            "evaluation_summary.json": hashlib.sha256(summary_path.read_bytes()).hexdigest()}}
            store.update_run_evidence(runtime_run["run_id"], evidence)
            runtime_run = store.run(runtime_run["run_id"])
        store.update_candidate(candidate["candidate_id"], status="running")
        store.finish_task(task["task_id"], task["lease_token"], {"run": runtime_run})
    reconcile_runtime_knowledge(coordinator.root, store, mission["mission_id"])
    return runtime_run
