# Zhaohuijin Deployment, 2026-09-30

## Latest update: native Pi context management, 2026-10-01

At 23:57 Beijing time on October 1, the user-authorized update was installed
under the same physical checkout, using selected files from commit `a37568f`.
This is a compatible file deployment, not a reset of the remote Git worktree.
The historical initialization and context-guard descriptions below describe
earlier deployments; the native Pi behavior in this section supersedes them.

The deployed files are `core/pi_agent.py`, `core/config.py`,
`core/runtime_context.py`, `core/runtime_evidence.py` under `rsi_system/`,
`deployment/rsi/pi-guard.ts`, three corresponding tests, and
`docs/design/pi-native-context-management.md`. RSI no longer estimates tokens
from JSON byte counts to reject requests, edits the conversation for compaction,
forces an output-token limit, truncates native Pi tool results a second time,
or terminates an Agent for a cumulative 16 MiB event log. Model windows remain
256000; Pi manages conversation compaction and overflow recovery.

The server had already received the Curator's schema-2 source catalogs, but
its evidence registrar still registered only the top-level catalog. This
deployment also brings the matching registration and hash checks for each
referenced record catalog. Existing source-index snapshots remain unchanged.

There were no running Agents or Runners at the deployment boundary. The
dashboard was briefly stopped, the coordinator lock acquired, and databases
and each replaced file backed up. The dashboard was then restarted with PID
410160; `/`, `/app.js`, `/style.css`, `/api/runtime`, and `/api/config` returned
HTTP 200. Private configuration hashes and a logical dump hash of the runtime
database matched before and after the update. The independent Missions remain:

- `dsh-cve-envbuild-20260930-001`: active/version39, with the pre-existing queued
  Coordinator `task-e1f69bed1ac0657a390e`.
- `dsh-cve-memory-study-20261001-001`: active/version65, no running task at inspection.

No Mission was created, resumed, ticked, or given more budget by this update.
Role routes remain specific to this server: Gemini/high Coordinator and Curator,
Astra/high independent reframe, and Sol 6.1/xhigh Researcher/default. This does
not copy kangrenlab's role swap into this deployment.

Focused tests passed in isolation and against the deployed production files:
110 passed, one SDK-path-dependent test skipped. The initial isolation run
exposed stale Curator test fixtures and the missing catalog registrar; the
baseline failures and all subsequent logs were retained. Using this server's
installed Pi 0.87.1 SDK with an offline fake provider also verified automatic
compaction and continuation: three user calls, one summary, zero network
requests. These are engineering checks, not a new live model call or a research
result.

Evidence paths relative to the deployment root:

- `.rsi/deployment/pi-native-activation.json`: per-file old/new hashes, service
  health, database-preservation verification, and source commit.
- `.rsi/deployment/backups/pi-native-20261001T155733Z/`: old files and databases.
- `.rsi/deployment/acceptance/pi-native-20261001T155733Z/`: initial failures,
  baseline comparison, passing focused/production tests, and native SDK receipt.

## Location and State

- SSH alias: `zhaohuijin`; login account: `zhanchi.wang`.
- Requested entry: `/data-nfs/ZhaohuiJin/rsi-system`.
- Physical checkout: `/home/zhanchi.wang/.local/share/rsi-deployment/rsi-system`.
- The requested entry is a symlink to the physical checkout. `/data-nfs` is NFS;
  the physical checkout and both SQLite WAL databases are on local ext4.
- Source: local `rsi_system` repository at base commit
  `9504923a442a5afc879e7b71d501a2787b2d8aeb`, with the current tracked edits
  and untracked frontend source overlaid. The deployment is not a clean release
  commit. Git history was transferred by bundle; no push was performed.
- Mission, task, Run, event, and experiment ledgers are empty. No Mission was
  created or started. No old runtime database, task output, weight, or credential
  was copied into the checkout. The Wiki contains only a fresh schema, index,
  and publication log.

## Installed Runtime

The checkout uses `.venv` with managed CPython 3.12.14. Installed packages include
`rsi-system==0.1.0`, `PyYAML==6.0.3`, and `pytest==9.1.1`. Node 22.23.3 is under
`~/.local/share/rsi/node/`; its archive passed the repository's pinned SHA-256.
Pi 0.87.1 and the pinned repository adapter dependencies are installed under
`~/.local/share/rsi/pi/`. The Pi entry point is `~/.local/bin/pi`.

Private configuration is separate from source:

- `~/.config/rsi/research.json` selects machine paths, role routes, and limits.
- `~/.config/rsi/*.key` contains provider credentials.

Later update, 2026-10-01 Beijing time: the private Curator role route is explicitly
`sub2api-reframe/gemini-3.8-flash-high/high`, with the existing 256000 context cap.
The effective configuration was checked without displaying credentials. A later
read-only deployment check found the independently started
`dsh-cve-envbuild-20260930-001` Mission active. The new Curator paging code was
therefore not installed during this turn; that Mission and its workers were left
running. The initial empty deployment state described above is historical.
- `~/.pi/agent/models.json` and `settings.json` configure Pi.
- Private directories are 0700; configuration and key files are 0600.

The existing authorized model routes were configured privately. Pi authentication
checks reported `ready` for `aihub/gpt-6.1-sol` and `aihub/gpt-6-astra`; that is a
local credential readiness check, not proof of a successful remote model call.
An initial live Sol probe failed on the direct route. After configuring the
existing server HTTP proxy at `127.0.0.1:7894` in private `agent.env`, both
`aihub/gpt-6.1-sol/high` and `sub2api-reframe/gemini-3.8-flash-high/high` completed
real read-tool round trips and returned JSON. Receipts are saved separately in
`/tmp/rsi-read-acceptance-aihub-guard/agent_result.json` and
`/tmp/rsi-read-acceptance-sub2api-reframe-guard/agent_result.json`.
These are deployment checks, not research findings or proof of every tool.

The optional private setting `agent_disabled_tools.coordinator=["web_search"]`
avoids the gateway's conversion of that client tool to built-in Google search.
Other roles retain their tools. Set the list to `[]` to re-enable it after
changing the model or verifying gateway compatibility; no search implementation
was deleted. All configured model context windows are at most 256000.

Private `agent_context_limit_tokens=256000` also checks the complete provider
request before sending it. New task prompts use compact shared history with a
hash-checked full-context source; role assignments and node relationships are
preserved. Multi-turn history uses archived sources when it grows, with the
provider check remaining the final limit. The UTF-8 byte estimate is deliberately
conservative and may reject earlier than actual token accounting.

The final Sol adapter acceptance is separate from the earlier core checks:
`/tmp/rsi-read-acceptance-aihub-cap-dynamic/agent_result.json` records a successful
read and JSON response, with two allowed provider checks and an explicit
128000-token output maximum. This verifies a small tool round trip, not a long
research conversation or every runtime path.
Four completed model checks are also archived under
`.rsi/deployment/model-acceptance/`, with a verified `manifest.sha256`. Original
temporary evidence remains in place. These archives contain no private settings
or credential links.

After the context guard update, 57 focused search and context tests passed on
this server. Tests isolate private configuration from their fixtures; the
deployed model routes and limits were not changed to satisfy test assertions.

## Commands

Inspect the empty initialized runtime:

```bash
ssh zhaohuijin
cd /data-nfs/ZhaohuiJin/rsi-system
.venv/bin/rsi-runtime --root . status
.venv/bin/rsi-core --root . list
.venv/bin/rsi-core --root . lint
~/.local/bin/pi --version
```

The dashboard currently runs as a user service, listening only on
`127.0.0.1:8765`. The service is disabled for boot and has `Restart=no`.
No research timer, worker service, or automatic Mission recovery was installed.

```bash
systemctl --user status rsi-zhaohuijin-dashboard.service --no-pager
systemctl --user stop rsi-zhaohuijin-dashboard.service
systemctl --user start rsi-zhaohuijin-dashboard.service
```

From the local computer, use an SSH tunnel and open `http://127.0.0.1:8765/`:

```bash
ssh -N -L 8765:127.0.0.1:8765 zhaohuijin
```

The dashboard checks the requested Host against its server port, so a different
local port alone will receive HTTP 403. If local port 8765 is occupied, leave the
conflicting service alone and use the same alternate port for the remote server
and tunnel. For example, stop the deployed dashboard service, run the foreground
command below with `--port 18765`, then forward
`ssh -N -L 18765:127.0.0.1:18765 zhaohuijin` and open
`http://127.0.0.1:18765/`.

For foreground operation after stopping the service:

```bash
cd /data-nfs/ZhaohuiJin/rsi-system
.venv/bin/python -m rsi_system.dashboard.server --root . --port 8765
```

## Acceptance Evidence

The following checks were run on the target server:

- CLI help and registry/runtime initialization succeeded.
- Runtime status returned `active: null`, `missions: []`, `hypotheses: []`.
- Both SQLite databases passed `quick_check` and `foreign_key_check`; all Mission,
  task, Run, event, and experiment counts were zero.
- 68 focused storage, runtime, Wiki, sandbox, dashboard, and research API tests
  passed. Frontend `npm ci` and `npm run build` succeeded.
- Wiki lint returned no issues and an empty search returned no records.
- Real candidate and Agent bubblewrap commands wrote inside their workspace;
  writes outside that workspace were rejected by the read-only host mount.
- Private permissions passed inspection. Dashboard public configuration did not
  contain the configured API key values.
- `/`, `/app.js`, `/style.css`, `/api/state`, `/api/runtime`, and `/api/config`
  returned HTTP 200. An unknown Mission returned 404; a foreign Host returned 403.
- `ss` confirmed a listener only at `127.0.0.1:8765`.

Machine-readable acceptance is saved in `.rsi/deployment/acceptance.json` under
the physical checkout. Focused test output is in
`.rsi/deployment/focused-tests.txt`. The no-Mission acceptance script is at
`~/.local/share/rsi-deployment/check-install.py`.

## Before Research

A new research question, Mission resources, authorization, and stop conditions
still need to be supplied. The example Mission is a template only.
`rsi-runtime start` registers an active Mission; `tick`, `work`, and dashboard
Run actions can execute work. These commands have deliberately not been run.

`sandbox=true` is configured and Linux bubblewrap is available. The observed
write protection does not establish a complete security boundary for every
tool. No Docker Runner profile, external evidence root, GPU allocation, pi0.5
service, or LIBERO environment is configured or required for this deployment.
Configure and accept an appropriate execution environment before authorizing
formal experiments. Gateway repairs and model tool-call acceptance remain
separate from the core deployment acceptance.

## Network and Retrieval Readiness

As of 2026-09-30 15:45 CST, the target uses systemd-resolved's `127.0.0.53` stub
with upstream `202.38.64.7`. NSS hosts order is
`files mdns4_minimal [NOTFOUND=return] dns`. `getent ahostsv4 aihub.top` and a
direct query to the upstream timed out. `sub2api.angelkang.com` resolved and
completed HTTPS without disabling certificate verification.

Public UDP resolver answers for `aihub.top` were inconsistent. HTTPS DNS from
Tencent returned the following chain, matching an independently observed Google
HTTPS DNS answer:

```text
aihub.top -> cdn.umsny.kdns.fr -> saas.sin.fan
          -> singgcdn.singgnetworkcdn.com
A: 162.159.130.234, 162.159.135.234 (TTL 60 seconds)
```

`curl --resolve` to each A record preserved `aihub.top` as the HTTPS hostname and
SNI, but the TLS handshake failed with `Connection reset by peer` (curl exit 35).
A local DNS override alone does not restore the route. No system DNS setting or
`/etc/hosts` was changed, and no paid model retry was made during these diagnostics.
The server's existing proxy at `127.0.0.1:7890` timed out during TLS; its local
HTTP proxy at `127.0.0.1:7894` completed verified HTTPS to AIHub. Private
`~/.config/rsi/agent.env` now sets HTTP_PROXY and HTTPS_PROXY to that working
proxy, excluding loopback hosts. The Pi wrapper loads this file. The successful
Sol tool round trip above used this route; the direct-route failures remain
historical evidence. No system DNS, hosts file, provider key, or model was
changed to work around the network issue.

`deployment/rsi/pi-web-search.json` supplies a credential-free configuration
that preserves the existing Exa search strategy. The pinned web-access package
supports Exa's public MCP endpoint without a separate API key, but this path has
not been accepted on the target. Fetching tries direct HTTPS and Jina's public
extraction service. Browser-cookie access is disabled and search workflow is
`none`, so this default does not request model-generated search summaries.
Private credentials, if required, are configured separately from source.

Node, using the same HTTP proxy as Pi, completed MCP initialization for both
AlphaXiv and Exa with HTTP 200. A Python urllib probe had returned 403; that
failure is retained separately and is not the Node adapter's acceptance result.
Initialization proves server access, not a successful literature search or
source retrieval. AlphaXiv initially returned HTTP 401 because its private
authorization was absent. Its credential is now installed privately and the
adapter reads it from `~/.config/rsi/alphaxiv.key`. Isolated task configuration
links the private `~/.pi/web-search.json`, without copying its contents to task
output. Neither an initial 401 nor a network failure is research evidence, and
no research Mission has been started on this server.
