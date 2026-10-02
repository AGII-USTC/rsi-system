# Zhaohuijin search permission repair — 2026-10-02

## Scope and outcome

The user authorized repairing Coordinator and independent problem-reframe tool
permissions on zhaohuijin. Model routes, thinking levels, credentials, Mission
budgets, historical failures, and queued tasks were preserved.

- Coordinator (Astra/high): `web_search` is available again.
- Independent problem reframe (Gemini/high): only `web_search` is disabled.
- Standard Researchers retain their existing search permissions.
- Gemini completed a real model/read-tool/JSON round trip after deployment.
- AIHub connectivity remains blocked by the unavailable server proxy; no
  successful Astra recovery or resumed scientific work is claimed.

## Cause and targeted deployment

The remote checkout had not received the task-kind permission mapping from
local commit `4a2039a0`. An independent reframe runs as role `researcher`, but
the old policy did not merge `agent_disabled_tools.problem_reframe` into that
role's invocation policy. The Coordinator also retained a search prohibition
from its previous Gemini route.

Deployed only the existing mapping block in
`rsi_system/core/runtime_roles.py`. The merge preserves restrictions already
applied to Researchers and does not mutate shared configuration. Private
configuration now contains `coordinator: []` and
`problem_reframe: ["web_search"]` under `agent_disabled_tools`.

At the idle boundary, the database, edited source file, and private config were
backed up under
`.rsi/deployment/backups/zhaohuijin-tool-policy-20261002T045920Z/`. The dashboard
was stopped for reload and restarted successfully. No running task was killed.

## Verification

- Local `test_agent_disabled_tools.py` and `test_runtime_roles.py` passed, with
  two environment-dependent skips.
- Remote subprocess acceptance verified the actual tool list for Coordinator,
  standard Researcher, and independent reframe. It also checked additive
  restrictions and preservation of the original configuration.
- Remote acceptance evidence:
  `.rsi/deployment/tool-policy-acceptance-i7tjzhbg/acceptance.json`.
- Real Gemini acceptance evidence:
  `.rsi/deployment/gemini-tool-live-20261002T050143Z/agent_result.json`.
  The model successfully invoked `read` and returned the requested JSON.
- Deployment receipt:
  `.rsi/deployment/zhaohuijin-tool-policy-repair.json`.

The historical Gemini failure `task-637b2cc3d2f5da7e478b` returned a generic
`400 Invalid request` before any successful tool operation. The new request
succeeded after permission and network-route corrections. The earlier response
does not expose enough upstream detail to attribute the 400 conclusively to a
single cause.

## Remaining network dependency

Coordinator `task-e4cb9eeb92bc60c733b1` completed 15 successful assistant turns
and 28 tool operations before connection errors. The configured AIHub proxy
on loopback port 7894 no longer has a listener. The existing proxy on port 7890
times out during TLS; direct AIHub access times out during DNS resolution.
The deployment account cannot read the administrator's proxy configuration and
has no passwordless administrator access.

Gemini's endpoint is reachable directly. Its domain was added to private
`NO_PROXY`, preserving existing proxy values and all credentials. The original
private environment file was backed up before editing.

An administrator must restore the existing AIHub proxy, or the user must supply
another usable route. Pending research was not blindly retried through the
broken endpoint. At the final deployment check the memory-study Mission was
active/version 110 with three queued tasks and no running task; its budget and
queue were unchanged. This is an engineering repair, not a research result.
