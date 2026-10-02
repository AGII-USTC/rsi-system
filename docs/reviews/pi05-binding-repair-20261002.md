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

Subsequent SSH/SCP transfers initially timed out. Connectivity then recovered,
and the user authorized deployment, writable research implementations and
continuation of the same Mission. The manifest identifies current local
checkpoint content; it is not an independently authenticated upstream official
checksum.

## 自主管理部署与验收续接

2026-10-02 已备份数据库、实际 `experiments/rsi_control_plane/runtime.py`
及私有配置。核对远端文件后，只部署本次任务上下文改动，保留服务器历史修改。
Mission version 1305 授权 `pi05-self-managed` GPU 环境；既有环境保留。
新环境无常驻 policy 副本，挂载真实 checkpoint、源码、Memory、adapter、report、
请求样本和绑定清单。Agent 可以修改自己的部署代码、配置和驱动；Runner 可以
加载模型、管理自己的本地策略子进程并写产物。原始权重和历史证据保持只读。
部署指南见 `docs/design/pi05-rsi-self-managed-deployment.md`，不是仅授予只读浏览。

本地相关回归 46 项通过、2 项跳过；远端在隔离测试配置下 29 项通过、1 项跳过。
第一次远端测试被生产私有配置覆盖测试路由，保留该失败输出，隔离配置后通过。

首轮真实 GPU 验收完成两次加载、七次策略调用及两次 LIBERO 动作观测。
另有十步预热。但首轮在已编译实例上修改 alpha，零门控输出仍与 memory 相同，
所以该零门控比较不能作为有效对照。原始回执完整保留；验收脚本已改为每个比较臂
首次推理前独立加载，在新的不可变部署快照中重新验收。

续接证据位于服务器
`.rsi/deployment/pi05-binding-repair-20261002/self-managed-activation.json`。
第二轮快照为 `self-managed-v2-20261002T090947Z/`。
这些工程调用不计作 Mission 的科学收益。用户追加授权在恢复时把剩余预算补足
48 个模型任务、12 次实验；历史消耗与失败不删除、不退款。

恢复后的运行修复：旧归档器把同一任务的 `handoff.md` 与
`handoff.md.txt` 映射到同一目标，已改为在冲突时保留两个稳定路径；原冲突字节
备份在服务器 `.rsi/deployment/backups/archive-conflict-20261002/`。Coordinator 的
repair 响应曾因带有只读 `reviewed_failures` 字段被二次拒绝，已允许该字段在
`decision=wait` 修复中被忽略。修复后 Coordinator 已真实运行并创建
`closed-loop-body-response-identification` 研究节点；当前剩余账本为 46 个模型任务、
12 次实验。Curator 曾出现一次连接错误，保留失败回执并未当作科学结果。
