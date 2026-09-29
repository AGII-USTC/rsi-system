"""Server RSI registry, bounded execution, and research knowledge interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import knowledge, proposals
from .cycle import run_cycle
from .protocols import build_command
from .schema import STATUSES
from .store import ExperimentStore


def _json_object(value: str, label: str) -> dict:
    try:
        raw = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be a JSON object or a JSON file") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _validate(record: dict, contracts: dict, root: Path) -> None:
    contract = contracts.get(record["protocol_id"])
    if not isinstance(contract, dict):
        raise ValueError("protocol has no fixed evaluation contract")
    for key in ("dataset_version", "evaluator_id"):
        if record[key] != contract[key]:
            raise ValueError(f"proposal cannot change fixed {key}")
    for key, value in contract.get("fixed_config", {}).items():
        if record["config"].get(key) != value:
            raise ValueError(f"proposal cannot change fixed config: {key}")
    build_command(record, root / ".rsi" / "validation", sys.executable)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--db", type=Path, default=Path(".rsi/state.sqlite3"))
    p.add_argument("--contracts", type=Path, default=Path("deployment/rsi/contracts.json"))
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="create the registry")
    research_start = sub.add_parser("research-start", help="start or resume autonomous server research")
    objective_source = research_start.add_mutually_exclusive_group()
    objective_source.add_argument("--objective")
    objective_source.add_argument("--objective-file", type=Path)
    research_start.add_argument("--new", action="store_true", help="start a separate campaign and archive a paused one")
    research_start.add_argument("--protocol", help="restrict a new campaign to one fixed protocol")
    sub.add_parser("research-status", help="show server research progress and Pi readiness")
    sub.add_parser("research-pause", help="pause after the current stage")
    research_tick = sub.add_parser("research-tick", help="advance one persistent research stage")
    research_tick.add_argument("--quiet-empty", action="store_true")
    submit = sub.add_parser("submit", help="validate and register a structured proposal")
    submit.add_argument("proposal", help="JSON file or inline JSON")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=STATUSES)
    show = sub.add_parser("show")
    show.add_argument("experiment_id")
    approve = sub.add_parser("approve", help="record a reviewer decision and queue a bounded run")
    approve.add_argument("experiment_id")
    approve.add_argument("--note", required=True, help="reviewer and acceptance rationale")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("experiment_id")
    cancel.add_argument("--note", default="cancelled by operator")
    transition = sub.add_parser("transition", help="record an analysis or archival decision")
    transition.add_argument("experiment_id")
    transition.add_argument("status", choices=("analyzed", "promoted", "archived"))
    transition.add_argument("--note", required=True)
    work = sub.add_parser("work-once", help="execute at most one queued run and reconcile knowledge")
    work.add_argument("--python", default=sys.executable)
    work.add_argument("--run-root", type=Path, default=Path(".rsi/runs"))
    work.add_argument("--quiet-empty", action="store_true")
    for name in ("search", "context"):
        command = sub.add_parser(name)
        command.add_argument("query")
        command.add_argument("--limit", type=int, default=5)
    sub.add_parser("lint", help="validate knowledge links and source references")
    return p


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    resolve = lambda path: path if path.is_absolute() else root / path
    try:
        if args.action.startswith("research-"):
            from . import orchestrator
            if args.action == "research-start":
                objective = args.objective_file.read_text(encoding="utf-8") if args.objective_file else args.objective
                result = orchestrator.start(root, objective=objective,
                                            new_campaign=args.new, protocol=args.protocol)
            elif args.action == "research-pause":
                result = orchestrator.pause(root)
            elif args.action == "research-status":
                result = orchestrator.status(root)
            else:
                result = orchestrator.tick(root)
                if args.quiet_empty and result["status"] in {"idle", "paused", "waiting_model", "waiting_budget", "busy", "completed", "stopped", "failed"}:
                    return 0
            _emit(result)
            return int(result.get("status") == "error")
        if args.action == "search":
            _emit(knowledge.search(root, args.query, args.limit))
            return 0
        if args.action == "context":
            _emit(proposals.proposal_context(root, args.query, args.limit))
            return 0
        if args.action == "lint":
            issues = knowledge.lint(root)
            _emit({"issues": issues})
            return int(bool(issues))
        with ExperimentStore(resolve(args.db)) as store:
            if args.action == "init":
                _emit({"db": str(store.path), "initialized": True})
            elif args.action == "submit":
                payload = _json_object(args.proposal, "proposal")
                contracts = _json_object(str(resolve(args.contracts)), "contracts")
                contract = contracts.get(payload.get("protocol_id"))
                if not isinstance(contract, dict):
                    raise ValueError("protocol has no fixed evaluation contract")
                spec = proposals.import_proposal(payload, fixed_protocol_id=payload["protocol_id"],
                    fixed_dataset_version=contract["dataset_version"], fixed_evaluator_id=contract["evaluator_id"])
                _validate(proposals.export_proposal(spec), contracts, root)
                _emit(store.create(spec))
            elif args.action == "approve":
                if not args.note.strip():
                    raise ValueError("review note cannot be empty")
                record = store.get(args.experiment_id)
                if record is None or record["status"] not in {"proposed", "awaiting_approval", "approved"}:
                    raise ValueError("experiment is not awaiting a queue decision")
                contracts = _json_object(str(resolve(args.contracts)), "contracts")
                _validate(record, contracts, root)
                if record["status"] == "proposed":
                    record = store.transition(args.experiment_id, "awaiting_approval", note=args.note)
                if record["status"] == "awaiting_approval":
                    store.transition(args.experiment_id, "approved", note=args.note)
                _emit(store.transition(args.experiment_id, "queued", note=args.note))
            elif args.action == "list":
                _emit(store.list(status=args.status))
            elif args.action == "show":
                record = store.get(args.experiment_id)
                if record is None:
                    raise ValueError(f"unknown experiment: {args.experiment_id}")
                _emit({"experiment": record, "events": store.events(args.experiment_id)})
            elif args.action in {"transition", "cancel"}:
                target = "cancelled" if args.action == "cancel" else args.status
                _emit(store.transition(args.experiment_id, target, note=args.note))
            elif args.action == "work-once":
                outcome = run_cycle(store, root, resolve(args.run_root), python=args.python)
                if not args.quiet_empty or outcome["run"] or outcome["written"] or outcome["errors"]:
                    _emit(outcome)
                return int(bool(outcome["errors"]))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"rsi: {exc}", file=sys.stderr)
        return 2
