# pi0.5 execution binding repair

## Verified State

The operator paused `body-memory-open-20260929-001` at version 1300 before
repairing the official-checkpoint gate. Hourly automation `rsi-2` is paused.
Existing inputs, failures, adapter files, checkpoint files and Run receipts
were preserved. No research restart or budget refill was performed.

The failed retained-state probe's preserved `policy.log` ends in
`jaxlib.xla_extension.XlaRuntimeError: RESOURCE_EXHAUSTED`. Checkpoint restore
could not allocate another 71,663,616 bytes on the GPU. This is an environment
failure, not a measured scientific negative.

The user subsequently authorized stopping both occupied GPU services.
`qwen38.service` (kang) and `piperx-pi05.service` (renlab) were stopped through
their respective user systemd managers. Both returned `inactive`; a subsequent
GPU observation showed 47,895 MiB free. They were not disabled or restarted.

## Implemented Locally

- `deployment/rsi/pi05_bindings.py` streams content hashes for every checkpoint
  parameter and asset file, source modules, the adapter, its training report,
  and a recorded real request. Relative paths survive host/container remapping.
- `deployment/rsi/pi05_policy_service.py` optionally verifies that manifest,
  checks actual imported module origins, loads an official, trained-memory or
  zero-gate policy, and exposes the actual identity in handshake metadata.
  The existing unbound official mode remains available.
- Reader construction reuses the installation's strict `latent_policy.py`
  loader. The adapter's saved gate is retained unless zero-gate is selected.
  Current source is explicitly labeled as requalification; it is not called
  reproduction of a historical loader with a different hash.
- `deployment/rsi/pi05_binding_acceptance.py` is an engineering acceptance
  entrypoint for real loads, fixed-noise physical-action calls and an optional
  two-transition LIBERO smoke check. It records failures and partial progress.

Eighteen local tests passed, including mutation of weights/norm stats/source/
adapter, remapped roots, source import origins, trained gate retention and
zero-gate isolation, plus the existing Docker Runner tests. Compilation and
`git diff --check` passed. These checks do not establish real model utility.

## Remote Evidence and Pending Work

The full content manifest was generated on kangrenlab, without copying
weights, at
`.rsi/deployment/pi05-binding-repair-20261002/bindings.json`.
Its content identity is
`4e48f8e5180ca2f94a3c8237ce5b172224e9d7c4704caa2b4da90cbecb4140ef`.
It binds the historical response-contrast adapter, report and task6/chunk1
request identified by `task-09d97dc0ed332b5cbe0b`.

Subsequent SSH/SCP transfers intermittently timed out. The staged service
and acceptance files must be hash-checked before deployment. Production Runner
profiles have not yet been changed by this repair, and the new real-weight
acceptance has not completed. In particular, actual service metadata,
Runner-readable artifact mounts, loader compatibility and inference/simulator
receipts remain pending runtime verification. The manifest identifies current
local checkpoint content; it is not an independently authenticated upstream
official checksum.

Resume repair by verifying the staged files, backing up the private config,
providing read-only bindings/artifacts/loader mounts and a nonduplicating
in-process profile, then executing acceptance against the real checkpoint.
Keep the Mission and automation paused until the user's next instruction.
Report engineering acceptance separately from closed-loop adaptation gains.
