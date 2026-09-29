"""Build and validate compact, durable summaries for completed Runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .algorithm_manifest import manifest_sha256, validate_manifest
from .pi_agent import atomic_json
from .protocols import read_metrics


SUMMARY_SCHEMA_VERSION = 1


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_json(value: object) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                              allow_nan=False, separators=(",", ":")).encode())


def _number(value: object, label: str) -> int | float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    return value


def _raw_refs(artifact_dir: Path, evidence: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    hashes = evidence.get("artifact_hashes", {}) if isinstance(evidence, Mapping) else {}
    for relative, digest in sorted(hashes.items()):
        if not isinstance(relative, str) or not isinstance(digest, str):
            continue
        path = artifact_dir / relative
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(artifact_dir):
            refs.append({"path": relative, "sha256": digest})
    evidence_path = evidence.get("evidence_path") if isinstance(evidence, Mapping) else None
    if isinstance(evidence_path, str):
        path = Path(evidence_path).resolve()
        if path.is_file() and path.is_relative_to(artifact_dir):
            refs.append({"path": path.relative_to(artifact_dir).as_posix(),
                         "sha256": evidence.get("evidence_sha256")})
    unique: dict[str, dict[str, Any]] = {item["path"]: item for item in refs}
    return [unique[key] for key in sorted(unique)]


def _resource_cost(evidence: Mapping[str, Any] | None, run: Mapping[str, Any]) -> dict[str, Any]:
    evidence = evidence if isinstance(evidence, Mapping) else {}
    start, finish = evidence.get("started_at_unix"), evidence.get("finished_at_unix")
    cost: dict[str, Any] = {}
    if type(start) in {int, float} and type(finish) in {int, float} and finish >= start:
        cost["wall_seconds"] = round(finish - start, 6)
    diagnostics = evidence.get("artifact_diagnostics")
    if isinstance(diagnostics, Mapping):
        limits = diagnostics.get("limits")
        if isinstance(limits, Mapping) and type(limits.get("max_seconds")) is int:
            cost["max_seconds"] = limits["max_seconds"]
        if type(diagnostics.get("stdout_bytes")) is int:
            cost["stdout_bytes"] = diagnostics["stdout_bytes"]
        if type(diagnostics.get("stderr_bytes")) is int:
            cost["stderr_bytes"] = diagnostics["stderr_bytes"]
    budget = run.get("inputs", {}).get("resource_budget") if isinstance(run.get("inputs"), Mapping) else None
    if isinstance(budget, Mapping):
        cost["budget"] = dict(budget)
    if not cost and evidence.get("resource_budget") is not None:
        cost["budget"] = evidence["resource_budget"]
    return cost


def _condition_metrics(metrics: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize protocol output into condition -> method -> metrics.

    Protocol readers already validate their immutable result contracts. This
    layer only chooses compact, reviewer-friendly dimensions and never invents
    a method or condition when the protocol did not provide one.
    """
    if not isinstance(metrics, Mapping):
        raise ValueError("protocol metrics must be an object")
    declared_conditions = metrics.get("conditions")
    if isinstance(declared_conditions, Mapping):
        normalized: dict[str, dict[str, Any]] = {}
        for name, values in declared_conditions.items():
            if isinstance(values, Mapping):
                normalized[str(name)] = {str(method): dict(result)
                                         for method, result in values.items()
                                         if isinstance(result, Mapping)}
        if normalized:
            return normalized
    condition = config.get("condition", "default") if isinstance(config, Mapping) else "default"
    if not isinstance(condition, str) or not condition:
        condition = "default"
    if "episodes" in metrics and isinstance(metrics["episodes"], list):
        methods: dict[str, Any] = {}
        for episode in metrics["episodes"]:
            if not isinstance(episode, Mapping):
                continue
            episode_metrics = episode.get("metrics")
            if isinstance(episode_metrics, Mapping):
                for method, value in episode_metrics.items():
                    if isinstance(value, Mapping):
                        methods.setdefault(str(method), []).append(dict(value))
        if methods:
            return {condition: {method: {"episodes": values} for method, values in methods.items()}}
    declared_methods = metrics.get("methods")
    if isinstance(declared_methods, Mapping):
        return {condition: {str(method): dict(value) for method, value in declared_methods.items()
                            if isinstance(value, Mapping)}}
    flat = {key: value for key, value in metrics.items()
            if key not in {"tasks", "episodes"} and type(value) in {int, float, str, bool}}
    return {condition: {"default": flat}}


def build_evaluation_summary(run: Mapping[str, Any], *, artifact_dir: Path | str,
                             candidate: Mapping[str, Any] | None = None,
                             protocol_metrics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Create a compact summary from a completed Run and its validated output."""
    if run.get("status") != "completed":
        raise ValueError("evaluation summary requires a completed Run")
    artifact = Path(artifact_dir).resolve(strict=True)
    evidence = run.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    config = candidate.get("config", {}) if isinstance(candidate, Mapping) else run.get("inputs", {}).get("config", {})
    protocol_id = candidate.get("protocol_id") if isinstance(candidate, Mapping) else run.get("inputs", {}).get("protocol_id")
    source_commit = run.get("source_commit") or evidence.get("source_commit")
    algorithm = candidate.get("algorithm", {}) if isinstance(candidate, Mapping) else run.get("inputs", {}).get("algorithm", {})
    metrics = dict(protocol_metrics or {})
    if not metrics:
        output_dir = artifact / "output"
        if not output_dir.is_dir():
            output_dir = artifact / "runtime" / "output"
        if protocol_id:
            record = {"protocol_id": protocol_id, "config": config}
            metrics = read_metrics(record, output_dir)
    warnings: list[str] = []
    if evidence.get("artifact_hashes_complete") is False:
        warnings.append("artifact hashes are incomplete")
    if evidence.get("interpretation"):
        warnings.append(str(evidence["interpretation"]))
    protocol_hash = _hash_json({"id": protocol_id, "config": config})
    contract_hash = run.get("inputs", {}).get("contract_sha256") if isinstance(run.get("inputs"), Mapping) else None
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": run.get("run_id"),
        "mission_id": run.get("mission_id"),
        "protocol": {"id": protocol_id, "config": config,
                     "sha256": protocol_hash,
                     "config_sha256": evidence.get("config_sha256"),
                     "contract_sha256": contract_hash},
        "source": {"commit": source_commit, "sha256": source_commit,
                   "file_hashes": evidence.get("source_file_hashes", {}),
                   "algorithm_sha256": manifest_sha256(validate_manifest(algorithm))},
        "metrics": {"condition_by_method": _condition_metrics(metrics, config),
                    "raw": metrics},
        "resource_cost": _resource_cost(evidence, run),
        "warnings": warnings,
        "raw_artifacts": _raw_refs(artifact, evidence),
    }
    return summary


def persist_evaluation_summary(run: Mapping[str, Any], *, artifact_dir: Path | str,
                               candidate: Mapping[str, Any] | None = None,
                               protocol_metrics: Mapping[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    artifact = Path(artifact_dir).resolve(strict=True)
    summary = build_evaluation_summary(run, artifact_dir=artifact, candidate=candidate,
                                       protocol_metrics=protocol_metrics)
    path = artifact / "evaluation_summary.json"
    atomic_json(path, summary)
    return path, summary


def validate_evaluation_summary(path: Path | str) -> dict[str, Any]:
    path = Path(path).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or data.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        raise ValueError("evaluation summary schema mismatch")
    for key in ("run_id", "mission_id", "protocol", "source", "metrics", "resource_cost", "warnings", "raw_artifacts"):
        if key not in data:
            raise ValueError(f"evaluation summary missing {key}")
    for item in data["raw_artifacts"]:
        raw = path.parent / item["path"]
        if raw.is_symlink() or not raw.is_file() or not raw.resolve().is_relative_to(path.parent):
            raise ValueError("evaluation summary raw artifact is unavailable")
        if item.get("sha256") and _sha256(raw.read_bytes()) != item["sha256"]:
            raise ValueError("evaluation summary raw artifact hash mismatch")
    return dict(data)
