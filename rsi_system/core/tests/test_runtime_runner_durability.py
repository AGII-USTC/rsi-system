import hashlib
import json
import os
from pathlib import Path

from rsi_system.core.runtime_runner_durability import (
    classify_runner_recovery, input_digest, mark_receipt_ready, mark_started,
    stable_runner_id, write_intent,
)


def test_intent_stages_and_verified_receipt_are_monotonic(tmp_path):
    frozen = {"task_id": "task-a", "value": 3}
    digest = input_digest(frozen)
    assert stable_runner_id("task-a").startswith("run-")
    write_intent(tmp_path, run_id="run-a", task_id="task-a", input_sha256=digest)
    assert classify_runner_recovery(tmp_path)["state"] == "prepared"
    mark_started(tmp_path, pid=os.getpid(), process_start_token=None)
    assert classify_runner_recovery(tmp_path)["state"] == "active"
    receipt = tmp_path / "run_record.json"
    receipt.write_text(json.dumps({"run_id": "run-a", "status": "completed"}))
    mark_receipt_ready(tmp_path, receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
    result = classify_runner_recovery(tmp_path)
    assert result["state"] == "receipt_ready" and result["reingest"]


def test_started_without_receipt_is_uncertain_and_never_safe_to_launch(tmp_path):
    write_intent(tmp_path, run_id="run-a", task_id="task-a", input_sha256="x")
    mark_started(tmp_path, pid=999999, process_start_token="missing")
    result = classify_runner_recovery(tmp_path)
    assert result["state"] == "uncertain"
    assert result["safe_to_launch"] is False


def test_corrupt_or_legacy_directory_fails_closed(tmp_path):
    (tmp_path / "execution_intent.json").write_text("{broken")
    result = classify_runner_recovery(tmp_path)
    assert result["state"] == "invalid_intent" and not result["safe_to_launch"]
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "stdout.log").write_text("started")
    result = classify_runner_recovery(legacy)
    assert result["state"] == "missing_intent" and not result["safe_to_launch"]


def test_running_receipt_is_not_adopted(tmp_path):
    digest = input_digest({"a": 1})
    write_intent(tmp_path, run_id="run-a", task_id="task-a", input_sha256=digest)
    mark_started(tmp_path, pid=999999, process_start_token="missing")
    receipt = tmp_path / "run_record.json"
    receipt.write_text(json.dumps({"run_id": "run-a", "status": "running"}))
    # No receipt hash was recorded, and running is explicitly nonterminal.
    result = classify_runner_recovery(tmp_path)
    assert result["state"] == "uncertain"
