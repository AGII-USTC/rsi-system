"""Bounded Pi invocations; each role gets a fresh context and an auditable receipt."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from .reasoning_capabilities import THINKING_LEVELS


TOOLS = {role: "read,rsi_read_source,grep,find,ls,edit,write" for role in
         ("coordinator", "researcher", "hypothesis", "analyzer", "critic", "curator", "reviewer", "coder")}
for role in ("coordinator", "researcher", "coder"):
    TOOLS[role] += ",mcp,github_search,github_read_file,web_search,fetch_content,get_search_content"
for role in ("researcher", "coder"):
    TOOLS[role] += ",bash"
for role in ("analyzer", "critic"):
    TOOLS[role] += ",bash"
for role in TOOLS:
    TOOLS[role] += ",rsi_delegate,rsi_run_probe,rsi_research,rsi_register_hypothesis,rsi_register_candidate,rsi_branch_control,rsi_message,rsi_inbox,rsi_status,rsi_wait,rsi_read_handoff"
TOOLS["researcher"] += ",rsi_research_checkpoint"
TOOLS["curator"] += ",rsi_wiki_check,rsi_wiki_submit"


def validate_disabled_tools(role: str, disabled_tools: list[str]) -> list[str]:
    if role not in TOOLS or not isinstance(disabled_tools, list):
        raise ValueError("agent_disabled_tools requires known roles and tool-name lists")
    available = set(TOOLS[role].split(","))
    if any(not isinstance(name, str) or name not in available for name in disabled_tools):
        raise ValueError(f"agent_disabled_tools contains an unknown tool for {role}")
    if len(disabled_tools) != len(set(disabled_tools)):
        raise ValueError(f"agent_disabled_tools contains duplicate tools for {role}")
    return list(disabled_tools)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def settings_status(executable: str, settings_path: Path | None = None) -> dict:
    settings_path = settings_path or Path.home() / ".pi/agent/settings.json"
    ready = Path(executable).is_file() and os.access(executable, os.X_OK)
    try:
        settings = json.loads(settings_path.read_text())
        model, provider = settings.get("defaultModel"), settings.get("defaultProvider")
    except (OSError, ValueError):
        model = provider = None
    return {"installed": ready, "default_model": model, "default_provider": provider,
            "ready": bool(ready and model and provider)}


def auth_ready(executable: str, *, provider: str, model: str) -> bool:
    """Ask Pi for readiness only; never request or print credential material."""
    try:
        result = subprocess.run([executable, "auth", "check", "--provider", provider, "--model", model,
                                 "--json"], capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        return result.returncode == 0 and data.get("status") == "ready"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def final_payload(events: Path) -> tuple[dict, dict]:
    last, usage, last_error = None, {}, None
    for line in events.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ValueError("invalid Pi JSON event stream") from exc
        if event.get("type") == "message_end" and event.get("message", {}).get("role") == "assistant":
            last = event["message"]
            if last.get("stopReason") == "error":
                last_error = last.get("errorMessage") or last_error
            current = last.get("usage", {})
            for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                if isinstance(current.get(key), (int, float)):
                    usage[key] = usage.get(key, 0) + current[key]
    if not last or last.get("stopReason") in {"error", "aborted", "toolUse"}:
        if last_error:
            raise ValueError(f"Pi provider invocation failed: {last_error}")
        raise ValueError("Pi did not finish with a successful assistant answer")
    text = "\n".join(item.get("text", "") for item in last.get("content", []) if item.get("type") == "text").strip()
    # Models occasionally add a JSON fence, a short preamble, or trailing prose
    # even after being asked for one object. Parse those harmless wrappers while
    # still requiring exactly one object as the durable protocol payload.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        payload = None
        for offset, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if payload is None:
            raise ValueError("agent response must be one JSON object; return JSON or a single ```json``` object without multiple objects")
    if not isinstance(payload, dict):
        raise ValueError("agent response must be one JSON object")
    return payload, usage


def tool_receipts(events: Path) -> list[dict]:
    return _tool_receipts(json.loads(line) for line in events.read_text().splitlines())


def _tool_receipts(events) -> list[dict]:
    calls, starts = [], {}
    for event in events:
        if event.get("type") == "tool_execution_start":
            starts[event.get("toolCallId")] = event.get("args") or {}
            continue
        if event.get("type") != "tool_execution_end":
            continue
        result = event.get("result") or {}
        details = result.get("details") if isinstance(result, dict) else None
        call = {"tool": event.get("toolName"),
                "succeeded": event.get("isError") is False and not (
                    isinstance(details, dict) and (details.get("error") or details.get("successful") == 0))}
        if event.get("toolName") == "mcp":
            call["operation"] = starts.get(event.get("toolCallId"), {}).get("tool")
        calls.append(call)
    return calls


def recover_curator_result(root: Path, task: dict, prompt: str, *,
                           context_limit_tokens: int | None = None,
                           disabled_tools: list[str] | None = None) -> dict | None:
    """Recover an accepted submission without another provider call or reservation."""
    if task["role"] != "curator":
        return None
    from .runtime_curator import load_curator_submission
    submission = load_curator_submission(root, task)
    if submission is None:
        return None
    directory = root / ".rsi/runtime/tasks" / task["task_id"]
    saved = json.loads((directory / "input.json").read_text())
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()
    if saved.get("prompt") != prompt:
        raise ValueError("curator submission belongs to a different prompt")
    receipt = directory / "agent_result.json"
    if receipt.exists():
        result = json.loads(receipt.read_text())
        if (result.get("prompt_sha256") != fingerprint or
                result.get("curator_submission") != submission or
                result.get("payload") != submission["payload"]):
            raise ValueError("agent receipt does not match the curator submission")
        return result
    events_path = directory / "events.jsonl"
    raw = events_path.read_bytes() if events_path.exists() else b""
    events, usage = [], {}
    # A process interruption may leave only its final JSON line unfinished.
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                break
            raise ValueError("invalid Pi JSON event stream")
        events.append(event)
        message = event.get("message", {})
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                value = message.get("usage", {}).get(key)
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
    calls = _tool_receipts(events)
    disabled = validate_disabled_tools("curator", disabled_tools or [])
    result = {"role": "curator", "payload": submission["payload"], "usage": usage,
              "curator_submission": submission, "completion_source": "curator_submission",
              "tool_calls": calls, "tools": [name for name in TOOLS["curator"].split(",") if name not in disabled],
              "disabled_tools": disabled, "tool_protocol_version": 1, "prompt_sha256": fingerprint,
              "started_at_unix": None, "finished_at_unix": time.time(),
              "events_sha256": hashlib.sha256(raw).hexdigest()}
    if context_limit_tokens is not None:
        budget_path = directory / "workspace/.rsi-context-budget.jsonl"
        checks = [json.loads(line) for line in budget_path.read_text().splitlines()]
        if not any(check.get("stage") == "provider_request" and check.get("allowed") is True for check in checks):
            raise ValueError("accepted curator submission has no provider context-check evidence")
        result["context_budget"] = {"limit_tokens": context_limit_tokens,
                                    "prompt_bytes": len(prompt.encode("utf-8")),
                                    "estimation": "UTF-8 JSON bytes as a conservative input-token upper bound",
                                    "provider_checks": checks}
    atomic_json(receipt, result)
    return result


def link_pi_configuration(agent_config: Path, source_config: Path) -> None:
    """Keep task output free of copied Pi settings and credential references."""
    agent_config.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "models.json", "web-search.json"):
        source = source_config.parent / name if name == "web-search.json" else source_config / name
        target = agent_config / name
        if target.is_symlink() and source.is_file() and target.resolve() == source.resolve():
            continue
        if target.exists() or target.is_symlink():
            target.unlink()
        if source.is_file():
            target.symlink_to(source.resolve())


def run_agent(*, executable: str, role: str, prompt: str, cwd: Path, output_dir: Path,
              timeout_seconds: int | None, model: str | None = None, provider: str | None = None,
              thinking: str | None = "high", guard: Path | None = None, evidence_manifest: Path | None = None,
              communication_manifest: Path | None = None, exploratory: bool = False,
              runtime_root: Path | None = None, task_id: str | None = None,
              python_executable: str | None = None,
              external_manifest: Path | None = None, decision_repair: bool = False,
              disabled_tools: list[str] | None = None,
              context_limit_tokens: int | None = None) -> dict:
    if role not in TOOLS or (timeout_seconds is not None and (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1800)):
        raise ValueError("invalid agent role or timeout")
    if thinking is not None and thinking not in THINKING_LEVELS:
        raise ValueError("invalid thinking level")
    cwd, output_dir = cwd.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt = output_dir / "agent_result.json"
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()
    submission_task = None
    if role == "curator" and runtime_root is not None and task_id is not None:
        from .runtime_store import RuntimeStore
        with RuntimeStore(Path(runtime_root) / "state.sqlite3") as store:
            submission_task = store.task(task_id)
        if submission_task is None or submission_task["role"] != role:
            raise ValueError("curator submission requires its own runtime task")
        if output_dir != Path(runtime_root).resolve() / "tasks" / task_id:
            raise ValueError("curator output directory belongs to a different runtime task")
        recovered = recover_curator_result(Path(runtime_root).resolve().parents[1], submission_task, prompt,
                                           context_limit_tokens=context_limit_tokens, disabled_tools=disabled_tools)
        if recovered is not None:
            return recovered
    if receipt.exists():
        result = json.loads(receipt.read_text())
        if result.get("prompt_sha256") != fingerprint:
            raise ValueError("agent receipt belongs to a different prompt")
        return result
    prompt_bytes = len(prompt.encode("utf-8"))
    if context_limit_tokens is not None:
        if type(context_limit_tokens) is not int or context_limit_tokens <= 16384:
            raise ValueError("invalid agent context limit")
        if guard is None or not guard.is_file():
            raise ValueError("agent context limit requires the Pi request guard")
        if prompt_bytes + 16384 > context_limit_tokens:
            atomic_json(output_dir / "context_budget.json", {
                "limit_tokens": context_limit_tokens, "reserve_tokens": 16384,
                "prompt_bytes": prompt_bytes, "allowed": False, "stage": "initial_prompt"})
            raise ValueError("agent context budget exceeded before Pi invocation; inspect context_budget.json")
    disabled = validate_disabled_tools(role, [] if disabled_tools is None else disabled_tools)
    active_tools = [name for name in ("read" if decision_repair else TOOLS[role]).split(",")
                    if name not in disabled]
    (output_dir / "prompt.txt").write_text(prompt)
    argv = [executable, "--mode", "json", "--print", "--no-session", "--no-approve",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-themes",
            "--no-extensions", "--tools", ",".join(active_tools)]
    if thinking is not None:
        argv += ["--thinking", thinking]
    if guard is not None:
        argv += ["--extension", str(guard.resolve())]
        runtime_tools = guard.parent / "pi-runtime-tools.ts"
        if role in {"analyzer", "critic"} and (
                runtime_root is None or task_id is None or not runtime_tools.is_file()):
            raise ValueError("review bash requires the active RSI tool bridge extension")
        if not decision_repair and runtime_root is not None and task_id is not None and runtime_tools.is_file():
            argv += ["--extension", str(runtime_tools.resolve())]
        mcp = guard.parent / "pi-mcp.ts"
        if not decision_repair and role in {"coordinator", "researcher", "coder"} and mcp.is_file():
            argv += ["--extension", str(mcp.resolve())]
        github = guard.parent / "pi-github.ts"
        if not decision_repair and role in {"coordinator", "researcher", "coder"} and github.is_file():
            argv += ["--extension", str(github.resolve())]
        web = guard.parent / "pi-web.ts"
        if not decision_repair and role in {"coordinator", "researcher", "coder"} and web.is_file():
            argv += ["--extension", str(web.resolve())]
    if model:
        argv += ["--model", model]
    if provider:
        argv += ["--provider", provider]
    env = os.environ.copy()
    if python_executable and role in {"researcher", "coder"}:
        # Keep the venv's entry-point path: resolving a Python symlink loses its
        # adjacent pyvenv.cfg and selects the underlying base environment.
        interpreter = Path(python_executable).absolute()
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise ValueError("configured research Python is not executable")
        env["PATH"] = str(interpreter.parent) + os.pathsep + env.get("PATH", "")
        env["RSI_RESEARCH_PYTHON"] = str(interpreter)
    env.update(RSI_AGENT_ROOT=str(cwd), RSI_AGENT_ROLE=role, PI_TELEMETRY="0")
    env["RSI_AGENT_DISABLED_TOOLS"] = json.dumps(disabled)
    env["NODE_DISABLE_COMPILE_CACHE"] = "1"
    env["RSI_AGENT_EXPLORATORY"] = "1" if exploratory else "0"
    workspace = output_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env["RSI_AGENT_WORKSPACE"] = str(workspace)
    budget_receipt = workspace / ".rsi-context-budget.jsonl"
    env.pop("RSI_AGENT_CONTEXT_LIMIT_TOKENS", None)
    env.pop("RSI_CONTEXT_BUDGET_RECEIPT", None)
    if context_limit_tokens is not None:
        env["RSI_AGENT_CONTEXT_LIMIT_TOKENS"] = str(context_limit_tokens)
        env["RSI_CONTEXT_BUDGET_RECEIPT"] = str(budget_receipt)
    if role in {"researcher", "coder"} and sys.platform == "linux":
        from .sandbox import agent_command
        agent_config = output_dir / "pi-agent"
        source_config = Path.home() / ".pi/agent"
        link_pi_configuration(agent_config, source_config)
        env["PI_CODING_AGENT_DIR"] = str(agent_config)
        candidate_workspace = cwd if role == "coder" and not exploratory else None
        argv = agent_command(argv, workspace=workspace, candidate_workspace=candidate_workspace,
                             agent_config=agent_config, external_manifest=external_manifest)
        env["TMPDIR"] = str(workspace)
    env.pop("RSI_EVIDENCE_MANIFEST", None)
    env.pop("RSI_COMMUNICATION_MANIFEST", None)
    env.pop("RSI_EXTERNAL_READONLY_MANIFEST", None)
    if evidence_manifest is not None:
        env["RSI_EVIDENCE_MANIFEST"] = str(evidence_manifest.resolve())
    if communication_manifest is not None:
        env["RSI_COMMUNICATION_MANIFEST"] = str(communication_manifest.resolve())
    if external_manifest is not None:
        env["RSI_EXTERNAL_READONLY_MANIFEST"] = str(external_manifest.resolve())
    stdout_path, stderr_path = output_dir / "events.jsonl", output_dir / "stderr.log"
    budget_prefix = budget_receipt.read_bytes() if context_limit_tokens is not None and budget_receipt.exists() else b""
    started, started_mono = time.time(), time.monotonic()
    invocation_error = None
    submission_accepted_at = None
    with ExitStack() as stack:
        bridge = None
        if not decision_repair and runtime_root is not None and task_id is not None:
            from .runtime_tools import serve_tools
            socket_path, bridge = stack.enter_context(serve_tools(Path(runtime_root).resolve().parents[1], task_id))
            env["RSI_TOOL_SOCKET"] = str(socket_path)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                child.stdin.write(prompt.encode())
                child.stdin.close()
                while child.poll() is None:
                    if submission_task is not None and (output_dir / "curator-submission/manifest.json").is_file():
                        if submission_accepted_at is None:
                            from .runtime_curator import load_curator_submission
                            accepted = load_curator_submission(Path(runtime_root).resolve().parents[1], submission_task)
                            if accepted is not None:
                                submission_accepted_at = time.monotonic()
                        # Give Pi time to emit its successful tool result, then
                        # stop an already committed task if the extension stalls.
                        if submission_accepted_at is not None and time.monotonic() - submission_accepted_at >= 2:
                            break
                    waited = bridge.wait_elapsed() if bridge else 0.0
                    if timeout_seconds is not None and time.monotonic() - started_mono - waited >= timeout_seconds:
                        raise TimeoutError("agent time budget exhausted")
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > 16_000_000:
                        raise ValueError("agent output budget exhausted")
                    time.sleep(0.1)
                if child.returncode != 0 and submission_accepted_at is None:
                    if child.returncode == 78 and context_limit_tokens is not None:
                        raise ValueError("agent context budget exceeded before provider request; inspect workspace/.rsi-context-budget.jsonl")
                    raise ValueError(f"Pi exited with status {child.returncode}; inspect private stage logs")
            except (ValueError, TimeoutError, OSError) as exc:
                invocation_error = exc
            finally:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=2)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
    if submission_task is not None:
        recovered = recover_curator_result(Path(runtime_root).resolve().parents[1], submission_task, prompt,
                                           context_limit_tokens=context_limit_tokens, disabled_tools=disabled_tools)
        if recovered is not None:
            recovered["started_at_unix"] = started
            if invocation_error is not None:
                recovered["post_submission_exit"] = {"error": str(invocation_error)}
            atomic_json(receipt, recovered)
            return recovered
    if invocation_error is not None:
        raise invocation_error
    if stdout_path.stat().st_size + stderr_path.stat().st_size > 16_000_000:
        raise ValueError("agent output budget exhausted")
    if submission_task is not None:
        raise ValueError("curator did not submit Wiki paths with rsi_wiki_submit")
    payload, usage = final_payload(stdout_path)
    result = {"role": role, "payload": payload, "usage": usage, "tool_calls": tool_receipts(stdout_path),
              "tools": active_tools, "disabled_tools": disabled,
              "tool_protocol_version": 1 if runtime_root is not None and task_id is not None else 0,
              "prompt_sha256": fingerprint,
              "started_at_unix": started, "finished_at_unix": time.time(),
              "events_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest()}
    if context_limit_tokens is not None:
        budget_raw = budget_receipt.read_bytes() if budget_receipt.exists() else b""
        if not budget_raw.startswith(budget_prefix):
            raise ValueError("Pi context guard evidence was overwritten")
        checks = [json.loads(line) for line in budget_raw[len(budget_prefix):].decode("utf-8").splitlines()]
        if not any(check.get("stage") == "provider_request" and check.get("allowed") is True for check in checks):
            raise ValueError("Pi context guard produced no provider-check evidence; invocation cannot be accepted")
        result["context_budget"] = {"limit_tokens": context_limit_tokens, "prompt_bytes": prompt_bytes,
                                    "estimation": "UTF-8 JSON bytes as a conservative input-token upper bound",
                                    "provider_checks": checks}
    atomic_json(receipt, result)
    return result
