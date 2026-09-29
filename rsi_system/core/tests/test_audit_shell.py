import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from rsi_system.core.audit_shell import _snapshot, run_audit_shell


def manifest(tmp_path):
    bundle = tmp_path / "source"
    bundle.mkdir()
    evidence = bundle / "run-1" / "results.jsonl"
    evidence.parent.mkdir()
    evidence.write_text('{"target":3,"predicted":4}\n')
    content = evidence.read_bytes()
    payload = {"task_id": "review-1", "mission_id": "mission-1", "files": [
        {"run_id": "run-1", "path": "run-1/results.jsonl",
         "sha256": hashlib.sha256(content).hexdigest()}]}
    path = bundle / "manifest.json"
    path.write_text(json.dumps(payload))
    return path


def test_audit_snapshot_rejects_changed_inputs_and_path_escape(tmp_path):
    path = manifest(tmp_path)
    _snapshot(path, tmp_path / "copy", "review-1", "mission-1")
    (path.parent / "run-1/results.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        _snapshot(path, tmp_path / "tampered", "review-1", "mission-1")
    data = json.loads(path.read_text())
    data["files"][0]["path"] = "../outside"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="path"):
        _snapshot(path, tmp_path / "escape", "review-1", "mission-1")


def test_audit_snapshot_accepts_identical_duplicate_but_rejects_conflict(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    payload = json.loads(path.read_text())
    payload["files"].append(dict(payload["files"][0]))
    path.write_text(json.dumps(payload))
    monkeypatch.chdir(tmp_path)
    _snapshot(Path("source/manifest.json"), tmp_path / "same", "review-1", "mission-1")
    payload["files"][-1]["sha256"] = "0" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="conflicting duplicate"):
        _snapshot(path, tmp_path / "conflict", "review-1", "mission-1")


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")
def test_audit_bash_recomputes_and_cannot_modify_or_escape_evidence(tmp_path):
    source = manifest(tmp_path)
    workspace = tmp_path / "work"
    command = """python3 - <<'PY'
import json
from pathlib import Path
row=json.loads(Path('/evidence/run-1/results.jsonl').read_text())
Path('/work/audit.py').write_text('print(4 - 3)\\n')
Path('/work/error.txt').write_text(str(row['predicted']-row['target']))
for target in ('/evidence/run-1/results.jsonl', '/home/secret', '/etc/passwd'):
    try: Path(target).write_text('changed')
    except OSError: pass
    else: raise AssertionError('unexpected write to ' + target)
assert not Path('/home').exists()
print('audit=1')
PY"""
    receipt = run_audit_shell(task_id="review-1", mission_id="mission-1", manifest_path=source,
        workspace=workspace, audit_dir=tmp_path / "audit", command=command, timeout_seconds=10)
    assert receipt["exit_code"] == 0, receipt["stderr"]
    assert "audit=1" in receipt["stdout"]
    assert (workspace / "error.txt").read_text() == "1"
    assert (source.parent / "run-1/results.jsonl").read_text() == '{"target":3,"predicted":4}\n'
    assert (tmp_path / "audit/receipt.json").is_file()


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")
def test_audit_bash_timeout_keeps_failure_receipt(tmp_path):
    receipt = run_audit_shell(task_id="review-1", mission_id="mission-1", manifest_path=manifest(tmp_path),
        workspace=tmp_path / "work", audit_dir=tmp_path / "audit",
        command="sleep 5", timeout_seconds=1)
    assert receipt["timed_out"]
    assert receipt["exit_code"] != 0
    assert (tmp_path / "audit/receipt.json").is_file()


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")
def test_audit_bash_linked_output_is_rejected_with_receipt(tmp_path):
    source = manifest(tmp_path)
    receipt = run_audit_shell(task_id="review-1", mission_id="mission-1", manifest_path=source,
        workspace=tmp_path / "work", audit_dir=tmp_path / "audit",
        command="ln -s /evidence/run-1/results.jsonl /work/forged", timeout_seconds=10)
    assert receipt["exit_code"] == 0
    assert "link" in receipt["integrity_error"]
    assert json.loads((tmp_path / "audit/receipt.json").read_text())["integrity_error"] == receipt["integrity_error"]
    repaired = run_audit_shell(task_id="review-1", mission_id="mission-1", manifest_path=source,
        workspace=tmp_path / "work", audit_dir=tmp_path / "audit-repair",
        command="rm /work/forged && python3 -c 'print(1)'", timeout_seconds=10)
    assert repaired["exit_code"] == 0 and repaired["integrity_error"] is None


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")
def test_audit_bash_mounts_configured_python_read_only(tmp_path):
    receipt = run_audit_shell(task_id="review-1", mission_id="mission-1", manifest_path=manifest(tmp_path),
        workspace=tmp_path / "work", audit_dir=tmp_path / "audit", python_executable=Path(sys.executable),
        command="command -v python3 && python3 -c 'import sys; print(sys.version_info.major)'",
        timeout_seconds=10)
    assert receipt["exit_code"] == 0, receipt["stderr"]
    assert "/opt/audit-python/bin/python3" in receipt["stdout"]
    inputs = json.loads((tmp_path / "audit/inputs.json").read_text())
    assert inputs["python_sha256"] == hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
