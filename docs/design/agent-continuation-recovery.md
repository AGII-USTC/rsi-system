# Agent continuation and failure recovery

RSI keeps the Mission blackboard and every task receipt as immutable evidence. A
failed Agent turn is therefore not replaced by a new, unrelated prompt. The
runtime creates a linked continuation with the same logical `session_id`, a
source task ID, an event cursor, and a structured failure description. The
continuation receives addresses to the source receipt and handoff files and can
selectively read them through the RSI tools.

The recovery boundary is explicit:

```text
model turn -> tool events -> final payload validation
    valid                         -> complete
    provider/transport failure   -> bounded model_retry
    partial model output         -> bounded model_retry
    tool failure or interruption -> tool_recovery with an error result
    JSON/schema/reference error  -> protocol_correction in the same logical session
    user stop or scientific fail -> terminal evidence; no automatic retry
```

This follows the durable-session ideas used by Codex, Claude Code, and
OpenCode, while keeping the RSI evidence boundary. A task directory retains
the full `events.jsonl`, `agent_result.json` when available, and communication
snapshot. `.rsi/runtime/sessions/<session_id>.jsonl` is only a compact index of
turns, tool-call summaries, hashes, and failure classes; it is not a substitute
for the original receipt. Pi is invoked with the same `--session-id` and a
separate `.rsi/runtime/pi-sessions/` directory for every task in the logical
session. Pi therefore owns the append-only JSONL transcript, parent/leaf message
tree, tool results, and native context compaction; the RSI ledger remains the
cross-task audit index. A task with no logical session keeps `--no-session` and
does not silently create a transcript.

Tool calls have a state machine of `pending`, `completed`, and `failed`. If a
worker exits while a call is pending, RSI writes an explicit
`interrupted_tool_call` error result before scheduling recovery. This keeps the
tool-use/tool-result pairing replayable and prevents a continuation from
repeating a completed child or inheriting an open call.

Final model text may contain a short preamble or one fenced JSON object, but it
must contain exactly one top-level JSON object. Multiple objects are rejected
with a local parse error so a fragment such as `rsi_wiki_check{}` cannot be
mistaken for the Agent's final decision.

Recovery is bounded by `runtime_recovery_max_attempts` and uses capped
exponential backoff for provider/model retries. A generic researcher exception
is not automatically classified as a protocol error; only validated
Coordinator contract failures enter protocol correction. Retries and local
repairs never count as scientific success, and the original failures remain in
the Mission ledger.

References:

- Codex CLI: <https://developers.openai.com/codex/cli/>
- Claude Code subagents: <https://code.claude.com/docs/en/sub-agents>
- OpenCode session processor: <https://github.com/anomalyco/opencode>
