import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from rsi_system.core.sandbox import candidate_environment, command
from rsi_system.core.store import ExperimentStore
from rsi_system.core.worker import run_once
from .test_worker import fixture_repo, queued

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Linux bubblewrap required")


def test_candidate_process_can_only_write_granted_directory(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-credential")
    with tempfile.TemporaryDirectory(prefix="rsi-isolation-", dir=Path.home()) as temp:
        root = Path(temp)
        writable = root / "allowed"; writable.mkdir()
        protected = root / "protected"; protected.write_text("original")
        script = '''import json, os, pathlib, socket, sys
root=pathlib.Path(sys.argv[1])
(root/'allowed'/'result').write_text('ok')
try:
 (root/'protected').write_text('modified')
except OSError:
 blocked=True
else:
 blocked=False
print(json.dumps({'blocked':blocked,'credential_inherited':'OPENAI_API_KEY' in os.environ}))
'''
        result = subprocess.run(command([sys.executable, "-c", script, str(root)], cwd=root, writable=[writable]),
                                env=candidate_environment(), check=True, capture_output=True, text=True)
        assert json.loads(result.stdout) == {"blocked":True, "credential_inherited":False}
        assert protected.read_text() == "original"
        assert (writable / "result").read_text() == "ok"


def test_worker_evaluates_inside_linux_sandbox():
    with tempfile.TemporaryDirectory(prefix="rsi-worker-sandbox-", dir=Path.home()) as temp:
        root = Path(temp)
        repo = fixture_repo(root)
        with ExperimentStore(root / "state.sqlite3") as store:
            queued(store)
            result = run_once(store, repo, root / "runs", python=sys.executable, sandbox=True)
            assert result["status"] == "completed", result
            assert result["metrics"]["environment_steps"] == 3
            assert "/runtime/output/" in result["evidence_path"]


def test_sandbox_hides_control_database_and_restores_only_job_inputs():
    with tempfile.TemporaryDirectory(prefix="rsi-private-boundary-", dir=Path.home()) as temp:
        root = Path(temp)
        private = root / ".rsi"
        source = private / "job/source"
        inputs = private / "job/inputs"
        output = private / "job/output"
        for folder in (source, inputs, output):
            folder.mkdir(parents=True, exist_ok=True)
        (private / "state.sqlite3").write_text("private state")
        (source / "code.py").write_text("pinned code")
        (inputs / "data").write_text("pinned input")
        probe = """import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
private=root/'.rsi'
assert not (private/'state.sqlite3').exists()
assert (private/'job/source/code.py').read_text() == 'pinned code'
assert (private/'job/inputs/data').read_text() == 'pinned input'
(private/'job/output/result').write_text('ok')
print('isolated')
"""
        result = subprocess.run(command([sys.executable, "-c", probe, str(root)], cwd=source,
                                protected=[private], readable=[inputs], writable=[output]),
                                env=candidate_environment(), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "isolated"
