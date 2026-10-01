# VLA Harness 研究续接审查（进行中）

## 最新状态：2026-09-30 恢复阻塞修复完成，按用户要求暂停

用户最新指示为“修完之后就先不要运行了”。本 Mission 保持 paused（核验时 version 322），没有 queued/running 任务；四个执行服务和定时器均 inactive，服务 MainPID 均为 0。每小时恢复任务 `rsi-2` 和另一个会补预算并恢复本 Mission 的 `automation-2` 均已暂停。本次没有恢复研究或再次补预算；等待用户另行要求启动。

暂停根因复核：上一轮只调整了 Curator 的失败统计，主调度仍累计整个 Mission 的历史失败，因此两个 Curator 成功归档后，又被旧 503 失败触发 `bounded collaborator retry budget exhausted`。这不是新的模型故障。两处现已统一按最近一次明确恢复后的任务执行版本统计失败；历史失败、消耗及单任务自动重试上限都保留。使用版本而非秒级时间戳，避免恢复和失败发生在同一秒时漏计；恢复前排队、恢复后执行的任务失败仍计入新窗口。

新增回归覆盖主编排和 Curator：旧失败触发暂停 → 明确恢复后可派单 → 同秒内出现两次新失败仍暂停，同时验证旧记录及预算不被删除或退款。相关测试本地 39 passed / 1 skipped，服务器 40 passed。修复已同步服务器，部署备份为 `/home/kang/embodied-rsi/.rsi/backups/resume-window-20260929T214427Z/`。只验证了恢复逻辑和测试，不代表生产研究已经重新执行。

核查时间：2026-09-29 15:32 UTC（北京时间 23:32）。Mission `body-memory-open-20260929-001` 仍 active。本报告是阶段记录，不能作为最终科研结论。

## 当前结论

主编排已经并行派出不同研究责任，研究员有真实模型响应和工具执行；但本次续接尚无登记的正式 Run，更没有已验证的 pi0.5/LIBERO 闭环收益。首轮三个研究任务都因 900 秒时间上限失败，自动接续保留了原始失败和已有文件。当前主要风险是重复阅读和环境检查耗掉模型任务预算，尚未形成可独立审查的实验结果。

本 Mission 上限维持 48 个模型任务、12 次实验、每次 180 秒，3 个协作者和 1 个 Runner 并发。核查时剩余 19 个模型任务名额、11 次实验名额；历史失败/取消不退款。数据库有 24 条模型调用预留记录，该数量并不等于供应商实际 HTTP 请求数或计费次数，CLI 内部重试也不能据此计价。未获取完整供应商费用账单。

## 可追溯的工作

主编排任务：`task-d42f87bca30ae2df6f1c`，Astra/high。研究员 Sol/xhigh，其他角色配置保持 Sol/high。

| 节点及派发版本 | 首轮任务 | 自动接续任务 | 核查到的工作 |
| --- | --- | --- | --- |
| `body-state-transfer` revision 2 | `task-b3461996ed1af6d6178a` | `task-a9233143fbb0ccc94e02` | 编写并运行配对历史数据的因果诊断脚本，接续任务在读取其已发布产物 |
| `harness-runtime-execution-path` revision 1 | `task-133eb99ccf3efd079a5d` | `task-3e41cae95ae814b7a106` | 检查真实模型/模拟器接口和执行条件，有 bash 操作；尚无正式 Run |
| `phase-feedback-control` revision 1 | `task-5ece366648e05c410d52` | `task-d015cc0113b369fe4734` | 探索不依赖持久身体记忆的反馈控制解释，阅读代码、查文献和尝试环境检查 |

并行任务于 15:03:57 UTC 派发，15:18:59 因 `TimeoutError: agent time budget exhausted` 失败，15:19:29 开始接续。后续两个任务出现过供应商 `server_error: Our servers are currently overloaded`，随后继续产生工具操作；本次没有核查到 403 余额不足。

服务器证据根目录：`/home/kang/embodied-rsi/`。

- 数据库：`.rsi/runtime/state.sqlite3`，对应 `agent_tasks`、`task_scopes`、`research_task_bindings`、`runtime_agent_calls`、`blackboard_events`、`runs`。
- 每个任务的 `.rsi/runtime/tasks/<task_id>/events.jsonl` 记录真实响应和工具调用；`input.json` 保存冻结输入。
- 第一条研究的脚本：`.rsi/runtime/tasks/task-b3461996ed1af6d6178a/communications/outbox/notes/analyze_paired_v2_causal.py`（13,004 字节）。
- 对应诊断结果：同目录 `paired_v2_causal_diagnostic.json`（70,337 字节）。workspace 原件也仍保留。它们是离线诊断产物，并非正式 Runner 回执。
- `runs` 查询当前 Mission 为 0 条；历史有一条失败的 Runner 任务 `task-a9b60fe76f737616bcd1`，因此仍占用一个实验名额。不得把它当作完成的 Run。

## 本轮发现和修复的基础设施缺陷

1. `pi-guard.ts` 读取了 external manifest，却没有把清单本身纳入可验证只读文件集合。位于 `.rsi` 下的清单因而被文件工具拦截。
2. 独立项目迁移移除了服务器专用默认路径，却没有把部署配置接入 manifest 构建。实际任务清单 `roots: []`。这是我们的部署接续遗漏，不能解释为研究本身不可行。

已修复：添加 `external_readonly_roots` 私有配置项及格式/重叠校验，运行时据此创建新任务资源清单；清单本身允许带哈希校验的读取，写入和篡改后读取仍拒绝。配置覆盖已授权的研究代码、openpi 源码子目录、历史 eval 和 checkpoint，共 8 个互不重叠的只读根目录，不复制权重。公开默认值保持空列表，主机路径不进入核心代码。

本地针对性检查通过（两项平台相关检查跳过）；服务器隔离环境对应检查全部通过。另用生产部署代码和实际私有配置执行 18 项清单/目录访问检查，通过，生成清单仅 1,181 字节，模型调用和 checkpoint 复制均为 0。此验收只证明资源配置与文件访问规则，不证明 GPU 实验链路可执行。

服务器备份：`.rsi/backups/external-roots-20260929T152948Z/`，包含部署前代码、配置及数据库快照。通过 `operator_infrastructure_fix` 事件与给主编排的 `direct_message` 记录了修复和限制，没有指定算法或强迫实验/合并。

**生效边界：**未重启或打断正在执行的模型任务。它们保持原始冻结清单，长期运行的 worker 也保持已经导入的 Python 代码。部署后的新工作进程执行新任务时才获得新资源清单；目前不能说所有活动任务都已获得修复。后续应在没有执行中任务的安全边界切换 worker，并检查新任务清单；不要为了生效而杀掉任务或重做旧实验。现有研究员受控 bash 已经能够读取这些授权资源，空清单主要影响资源发现和文件工具，不能把它说成所有研究无法开展的唯一原因。

另有不存在的相对路径、`libero` Python 依赖缺失、`fd` 不存在和 GitHub 403 等检查错误；它们与清单缺陷不是同一问题。不放宽全局权限，不把诊断失败当作方法无效。

## 仍待验证

- 创新探索：三个责任方向比单一路线完整，但还不能证明形成了新的有效机制。已派发不等于研究员完成了自主研究闭环。
- Wiki：输入与工具记录显示已有文档被读取；尚不足以归因出“某条 Wiki 经验导致有效计划调整”。
- 递归分工、对话改变计划、按证据组合：当前证据不足，留待后续节点和任务记录审查。
- 真实验证、独立审计、复现及正式 Wiki 归档：尚未完成。没有正式 Run 时，不给出算法性能结论。
- 主编排是否在研究员回传后调整方向，以及是否避免继续把预算消耗在重复阅读/环境诊断，仍需观察。

自动跟进保持开启；无用户可操作阻塞，暂不暂停 Mission，不提高预算，也不重复启动研究。

## 23:37 北京时间更新

三个接续研究员于 15:34:29 UTC 再次触及 900 秒上限，系统于 15:35:29 自动开始第二次（配置允许的最后一次）恢复：`task-b76461d5bda4081f2be2`、`task-518e28aea1dcb99be868`、`task-9cbc814bfa0f204cdbd6`。均已核验真实模型响应和工具操作。剩余模型任务名额降为 16，实验名额仍为 11，正式 Run 仍为 0。

执行路径研究员的工具输出包含 `19 passed, 1 warning, 5 subtests passed in 0.08s` 的代码检查回执；这是代码检查，不是 pi0.5 策略或 LIBERO 闭环实验。其余接续主要仍在阅读、检查和准备，尚无完整研究报告。

新恢复任务的外部资源清单仍为 0 个根目录，确认长期驻留 worker 尚未加载新代码。上次部署修复不能算对当前任务已经生效。不要通过修改冻结清单冒充生效；应在安全执行边界更新 worker。总体评价：有真实工作，但连续两轮超时接续、正式实验尚未开始，研究效率不理想。

## 用户取消模型任务时限（23:40 北京时间）

用户明确要求不设置模型 Agent 总执行时限。已将服务器私有 `agent_timeout_seconds` 从 900 改为 `null`，并同步本地公开配置/使用示例；模型任务和实验总数、180 秒单次实验上限、并发、模型及思维强度均未改变。原配置备份于服务器 `.rsi/backups/no-agent-deadline-20260929T153958Z/`。现有实现支持空时限，且持续续期租约；针对无时限/有时限两种模式的测试均通过。

生效于下一次 Agent 调用。已经启动的任务保留调用参数中的 900 秒，不能仅修改配置就改变进程里的值，未人为杀死它们或篡改其原始调用账本。已通过事件与消息告知主编排，让其依据已保留证据和剩余预算安排后续。

15:38:08 UTC 新建的 Runner `task-4821b0533ad2fc81a91c` 已结束，目标是“不调用策略的模拟器命令效果可行性探针”，不是完整 pi0.5 实验；其具体回执仍需独立核验。主编排旧调用也已到时，恢复任务 `task-d9f141fdbccc24595d41` 排队等待执行。核查时剩余 15 个模型任务和 10 次实验名额。

## 用户取消 Runner 总时限（部署交接中）

用户随后明确要求 Runner 也不设时间限制。实现已支持 Mission `resources.max_seconds_per_run=null`，同时处理普通 Candidate Runner、探索探针固定 120 秒上限和 Runner 租约的第二层截止时间。探针新输入记录实际时间预算；既有冻结输入与回执不改写。资源条目为 `null` 时，只取消总耗时限制，实验次数、并发和日志/产物限制仍在。

验收：本地 34 项通过、4 项 Linux 隔离检查跳过；服务器同组 38 项全部通过，覆盖无时限执行、有限时执行、人工取消、租约与崩溃接续。另验证修改 Mission 时限后，恢复仍使用原冻结输入和回执，不重跑脚本（1 项通过）。

代码已部署至服务器，备份 `.rsi/backups/runner-unlimited-20260929T154615Z/`。为避免把 null 传给已加载的旧执行器，尚未提前修改活动 Mission 的数值。维护脚本 `/tmp/rsi-activate-unlimited-runner.py` 暂停定时器的新派发，等待已执行任务正常交接、旧服务退出，再有记录地将 Mission 时限改为 null 并恢复定时器。不会杀掉当前 Agent。状态见服务器 `/tmp/rsi-unlimited-runner-activation.json`；若等待十分钟仍无安全边界，自动恢复原定时器，保留待切换状态，不能声称已生效。

自动跟进说明已同步用户最新授权：模型 Agent 与 Runner 都取消总执行时限，不应重新应用旧的 15 分钟、180 秒或探针 120 秒上限；任务和实验总数量、并发及模型配置不变。

## 23:53 北京时间：不限时切换完成并恢复研究

旧任务已交接，主编排提交 maintenance handoff 后处于等待。维护脚本于 15:52:15 UTC 在没有执行中或排队任务、旧服务已退出时，把本 Mission `max_seconds_per_run` 从 180 改为 null，保留变更事件和原始数据库快照：`.rsi/backups/runner-unlimited-activate-20260929T155215Z/`。所有原定时器已恢复。随后核验私有 Agent 时限、普通 Runner 与探索 Runner 均接受 null，从 `task-d9f141fdbccc24595d41` 保存的 handoff/checkpoint 恢复同一个 Mission；没有恢复其他 Mission，也没有创建重复实验。

此时 Mission active，剩余 15 个模型任务和 10 次实验，模型/思维强度/并发均未改变。维护等待已经结束；无时限配置确已激活，不再只是已部署未加载。

主编排交接也明确：Run `run-c0a763c3097109ecf31f3796` 虽然程序退出成功，但产物报告源文件资格检查未通过，尚未产生模拟器回放或策略效果测量。这一科学/环境问题不因取消时间限制而自动解决，已明确告知恢复后的主编排，仍由研究员选择后续验证与修复路线。

## 23:56 北京时间跟进

实际新调用确认配置生效：主编排 `task-841cd60881474c82d58c`（Astra/high）和归档员 `task-a0c7c103efb4414afb0b`（Sol/high）的调用账本均为 `timeout_seconds: null`；两者的 external manifest 均包含 8 个只读根目录，已不再是空清单。两者均有真实模型响应及文件/研究工具操作，本次未见供应商错误。Mission active，版本 221，`max_seconds_per_run` 为 null。

独立查看探针 `run-c0a763c3097109ecf31f3796` 的原始 `output/command_effect.json`：其明确记录 `status=blocked`、`stage=source_qualification`，冻结源码中缺少指定的三个研究文件及 `examples/libero/main.py`。因此数据库的 Run completed 只代表脚本正常结束并返回阻塞诊断，不代表模拟器、策略或 Memory 验证完成。主编排已经读取此问题并继续评审，归档员正在整理来源，不追加重复实验或替主编排指定研究路线。

## 00:16 北京时间更新：源码修复后第二个 Runner 探针

Researcher `task-84c98a4d62c272d71298` 针对首个探针缺少源码的问题，发布了新的 hash-qualified bundle（包括 `examples/libero/main.py`、三个研究文件和配置），并明确这是新的输入指纹，不是重放旧 Run。主编排 `task-ad8b628a762a931481c8` 据此派出 Runner `task-4eb59b69e13859cc4219`，产生新 Run `run-8eb2c6d4bf36e1b858154d0c`。

这次 Runner 的源码快照与四个文件哈希检查通过，但 `output/command_effect.json` 报告：`status=blocked`、`stage=dependency_preflight`，依赖检查显示 `mujoco/numpy/yaml` 存在，`robosuite` 不存在；没有构造 LIBERO 环境，没有 replay、动作效果、策略调用或 Memory 测量。Run envelope 的 `status=completed` 仅说明探针正常返回阻塞诊断；不能称为实验成功。新的 Run 资源回执明确记录 `max_seconds=null`，证明 Runner 不限总执行时限已实际生效。

研究员、主编排和 Curator 均有真实模型响应和工具操作。Curator 已归档新的源链接 Wiki 页面，主编排正在审查并决定依赖边界的下一步。当前 Mission 仍 active；没有因为依赖缺失强行宣称算法无效，也没有重复旧 Run。

## 00:38 北京时间更新：新增组合研究与停止评审

Mission 仍 active，当前主编排 `task-93665f3ed38a167c8117` 正在评审，剩余 8 个模型任务和 9 次实验，没有新增 Run。新的重审任务 `task-f247356073f616bec27f` 已完成：它认为继续被动重算 V2 数据无法解决经验来源成本不匹配或闭环任务效用，建议在 Runner 依赖路径获得修复前暂缓分支，最终 Mission 决定留给主编排。

本轮首次出现明确的组合对象 `composition-23207b452d2fdf024072`：固定输入为根节点 revision 9、body-state-transfer revision 3、simulator-command-effect-feasibility revision 5，输出 body-memory-feasible-next-test。它组合已有问题与证据来评估下一步，不是代码合并，也没有联合方案收益；其协议仍为 to_be_defined。

需准确区分此次“独立再分析”和正式 problem_reframe：主编排两次 rsi_delegate 携带空 candidate_id 被拒（problem reframe needs an unanchored researcher task），随后选择创建组合研究任务。实际调用账本显示该任务 task_kind=standard、Sol/xhigh，不是用户为正式 problem_reframe 配置的 Astra/high。拒绝原因和绕行均被主编排写入 handoff，后续应审查参数契约易用性；不能声称 Astra 重审已完成。

当前执行障碍仍是选定 Runner Python 缺少 robosuite；历史 Docker 镜像存在不等于通用探索 Runner 已有受控镜像选择能力。主编排已读到此区别，仍在决定后续或暂缓，未把技术探针 completed 误述为科学收益。
## 2026-09-30：Runner 环境接口接入与 Mission 恢复

用户要求先暂停研究并把已有 pi0.5/LIBERO 环境接入 Runner。研究 Mission `body-memory-open-20260929-001` 在接口验收期间保持 paused；服务器原有 40 个模型任务和 3 次 Runner 任务的历史没有删除。

接口部署到 `/home/kang/embodied-rsi` 后，管理员配置了 `libero-sim` 与 `pi05-libero` 两个环境。Runner 只能选择 Mission allowlist 中的名称；镜像按 SHA 固定，项目与 checkpoint 只读挂载，策略服务使用本 Run 的内部网络，权重没有复制。Runner 的暂停/租约检查会中止容器，清理失败会保留回执并暂停 Mission。环境配置和使用说明见 `docs/design/runner-environments.md` 与 `deployment/rsi/runner-environments.example.json`。

独立基础设施验收不占生产 Mission 预算。真实 Run `run-7ed6fc10576931f47b7834c2` 的回执位于服务器 `.rsi/acceptance/runner-environments-20260930/pi05-pass/`：LIBERO reset、10 次 warmup、pi0.5 推理、动作形状 `[10,7]`、动作进入仿真、只读源码和无 Docker socket 检查均通过，容器清理通过。该 Run 的 `purpose` 明确为 infrastructure acceptance，不是 Memory、成功率或适应收益证据。此前两次显存不足的失败回执也原样保存在 `pi05-oom-1` 和 `pi05-oom-2`；根因是同机其他进程占满显存，清理后验收通过。

接口回归测试为 57 passed、6 skipped。服务器相关测试为 59 passed、1 failed；失败是服务器原有角色工具契约与本地角色测试基线的差异（coordinator 实际暴露了额外 GitHub 工具），不是 Runner 环境链路失败，未借此改写角色权限。

随后按用户授权把 Mission 总额度补足到“当前剩余 48 个模型任务、12 次实验”：历史计数仍为 42 个 Agent 任务、3 个 Runner 任务，因此数据库上限为 88/15。Mission 已恢复为 active，四个定时器 active；恢复后主编排 Astra/high 与 Curator Sol/high 已产生真实调用，调用账本 `timeout_seconds=null`。此时新调用已消耗 2 个模型任务，当前剩余为 46 个模型任务、12 次实验。模型、思维强度、并发和不限时配置没有改变。

每小时 heartbeat `rsi-2` 已启用。它只在确认模型中断、没有运行或排队任务且可安全接续时，保留失败证据、把剩余额度补足到 48/12 并恢复同一 Mission；正常运行、科学失败、主动暂停和未解决的外部依赖不会触发重置。

这次工作证明了执行接口和恢复边界，尚未证明身体 Memory 带来科学收益；后续研究结果仍需由 Researcher、Analyzer 和 Critic 依据统一协议解释。

## 2026-10-01：Curator 提交修复后的最终核验

原 Mission 当前状态为 `paused`、version `848`，没有 queued/running 任务，剩余 42 个模型任务、12 次实验。暂停事件为 `bounded collaborator retry budget exhausted`，不是余额、网络或 Runner 故障。历史任务、调用账本、输入回执和失败输出均保留；没有再次重置预算或恢复其他 Mission。

### 执行协议修复

事故任务 `task-dcab977ef7d3e219e483` 的六份草稿已经通过 `rsi_wiki_check`，但旧 `final_payload` 从混合解释文本中误取 `rsi_wiki_check{}`。服务器部署了 `rsi_wiki_submit` 路径提交协议，部署记录为 `/home/kang/embodied-rsi/.rsi/deployment/curator-submit-activation.json`，备份为 `/home/kang/embodied-rsi/.rsi/deployment/backups/curator-submit-20260930T180859Z/`。提交回执绑定 task、Mission、input version、prompt hash、文件 hash 和 SQLite tool operation；进程中断时从冻结回执恢复，不重新调用模型。

本地核心回归为 522 passed、15 skipped；服务器 Curator 提交/恢复/工具协议回归为 83 passed、2 skipped。独立真实 Gemini 验收在隔离 fixture 中读取事故草稿及三个 provenance 来源，实际完成 `rsi_wiki_check` 和 `rsi_wiki_submit`，自然语言结尾不需要 JSON，回执重放保持相同 hash，生产 Wiki 未写入。验收证据位于服务器 `/home/kang/embodied-rsi/.rsi/deployment/curator-paging-acceptance/rsi-curator-acceptance-submission-20261001/`。

### 真实研究工作

修复后的 Curator 任务 `task-18ed65364b8a0b63e02f` 使用真实 Gemini/high，47 次 provider request 全部通过 256k 上下文检查。它实际执行了 22 次 `read`、6 次 `rsi_read_source`、3 次写入、4 次 `rsi_wiki_check` 和 1 次 `rsi_wiki_submit`，保存了 `progress-note.md`，提交并发布了：

- `knowledge/experiments/body-memory-closed-loop-probe-boundary-20260930.md`
- `knowledge/queries/runtime-internal-memory-reader.md`

对应 `knowledge_curated` 事件为 version `847`、source event `3597`。这证明了增量阅读、落稿、回执和 Wiki 归档的执行闭环，不证明 Memory 的性能收益。

### 主编排与研究限制

主编排接续任务 `task-ba4bf20aaea28546387c` 和 `task-19cbc4b5f6f4f4cee988` 均有真实 Gemini 响应和工具操作，但分别因 `coordinator decision must be schedule, wait, or stop` 以及 `deferred stop reframe must not name a reviewed task` 失败。Researcher `task-3b762ad5abef654b8255` 在 provider 请求前触发 256k 预算保护。以上是协议/上下文执行问题，不是科学负结果，也没有据此强迫算法路线或伪造实验结论。

后续修复：Coordinator 协议校验错误现在保留原始 `agent_result.json`、`events.jsonl` 和通信清单，并生成一次只读纠正续接。纠正提示会列出具体错误、完整顶层字段要求和可读文件地址，固定为 `decision=wait`，禁止重新派单、实验或修改研究状态；纠正耗尽仍按有限重试规则记录并暂停。普通 Researcher 续接只收到源任务状态、错误摘要、事件号和 handoff 文件索引，可用 `rsi_status`/`rsi_read_handoff` 按需读取，不再重放全量历史。

本次三类原始 Coordinator 回执分别显示：`task-d01be32a91e298f9fc54` 把 `rsi_research` 的 `response_limit` 错误对象当成最终 payload；`task-ba4bf20aaea28546387c` 把 `stop_reframe_decision` 内层对象当成顶层响应；`task-19cbc4b5f6f4f4cee988` 在 `defer` 中填写了已经审过的 `reframe_task_id`。它们是外层协议装配错误，不是模型 HTTP 失败或科学结论。

审查后曾误触发一次恢复辅助脚本，创建了预算事件和任务 `task-7dcf28ec0420bd535c0b`；随后立即暂停 Mission、停止服务并保留全部事件。该任务在确认没有实际执行后按原始租约标记为 `cancelled`，没有删除或重算历史预算。当前 Mission 仍为 paused、无 queued/running 任务。修复通过本地完整回归测试（522 passed, 15 skipped）和 `git diff --check`。

本轮没有新的正式 Runner Run，没有真实冻结 pi0.5/LIBERO 闭环性能测量，也没有 Cross-Attention 或其他 Memory 机制的因果收益证据。已有 Wiki 页面主要记录闭环验证边界、离线诊断和下一步条件；是否形成创新机制、是否应组合、独立审计和复现，仍不能从 Curator 成功推出。Mission 因主编排接续预算耗尽暂停，后续需要用户决定是否处理 Coordinator 协议问题后再恢复，自动跟进不擅自重置预算。

## 2026-10-01：停止审阅的局部反馈修复与再次接续

用户随后授权每小时检查、在确认执行故障后修复本地源码并部署到 kangrenlab、续接同一个 Mission。本节记录新的现场核验，更新上一节的暂停状态；研究尚未完成。

### 本次暂停的具体原因

停止决策来自 `task-540d83ab9bfd7ed72427`，对应 Mission version `878`。运行时在 version `880` 暂停，原因是还有 **50 条历史失败任务没有被该停止决策明确审阅**。此前 `task-9e3bd58baacb8905e509` 把“Runner 任务已完成、其 Run 失败”的记录填入只允许失败任务的 `reviewed_failures`，触发 `reviewed failure must be a failed task in this Mission`。这两项属于停止协议与反馈问题，不能记为新的科学负结果。恢复前剩余 41 个模型任务、12 次实验，无 queued/running 任务。

源码提交 `82116f4`（`Give provisional stops bounded failure-review feedback`）增加一次补充审阅任务，向 Coordinator 提供真正失败的任务 ID、尚未审阅的 ID、近期错误摘要和按需读取方式；明确替换停止决策时需保留有依据的原审阅项。主编排仍可派发研究、等待或停止；一次补充审阅仍不完整时暂停，不循环派发、不自动确认失败已审阅。无剩余模型预算时不派发。非法 `reviewed_failures` 引用进入既有的一次只读协议纠正流程，租约等错误仍按原规则处理。

本地核心测试 **534 passed、15 skipped**；最后定向测试 **23 passed**，kangrenlab 对应测试也为 **23 passed**。原始失败回执和 version880 数据库的隔离副本回放重现了上述错误，并验证能生成局部反馈与只读纠正；生产数据库 version、原始任务和回执 hash 均未变化，回放没有调用模型。证据：服务器 `.rsi/deployment/acceptance/stop-failure-review-20261001T022308Z/acceptance.json`。

部署实际更新的是服务使用的 `experiments/rsi_control_plane/runtime.py`、`runtime_decision_repair.py` 及两份相关测试。部署前核对了源码 hash；备份在 `.rsi/deployment/backups/stop-failure-review-20261001T022136Z/`，部署清单在 `.rsi/deployment/stop-failure-review-activation.json`。GitHub `origin/main` 已确认推送至 `82116f4`，包含此前未推送的 `6050c97`。本次未部署或恢复 zhaohuijin。

### 独立讨论与停止理由

独立问题重述 Researcher 并非从未参与：`task-14d1eb4edcdff225b225` 已完成；主编排在 version `562`（`task-1d9c6f5223bbf361ee09`）明确采纳了同来源 carry/reset/fixed/feedback 对照和完整生命周期成本核算的建议。最新停止决策选择暂缓再次重述，引用这份历史报告，称其建议已在 `run-a2a94a441eb468bbc730a937` 中检验。**这是已有独立报告及采纳记录，不等于当前停止已获得新一轮独立审查。**

用户随后问“那不应该还有一个agent要讨论一下吗”。维护消息 version `882` 已将此问题交给主编排，要求它结合旧报告与后续证据判断是否应再次独立讨论，并明确依据。该消息也指出：Mission 现有环境允许 `libero-sim` 和 `pi05-libero`，一般策略推理进入仿真的接口已有工程验收；请区分现成接口与内部记忆读取新方案真正缺少的能力。消息没有代选算法、强迫重审或把接口验收说成 Memory 收益。

### 恢复后的实测状态

按用户已确认的故障恢复授权，version `881` 将剩余额度补足为 48/12：历史 117 个模型任务和 9 个 Runner 任务保留，对应上限 165/21。version `883` 重新激活原 Mission，保留主编排的暂定停止意图；随后系统创建补充审阅 `task-cc477b99f18948d89510`，剩余变为 **47 个模型任务、12 次实验**。

首次恢复核验时，Mission 为 active/version886，该 Coordinator 正在执行。调用账本为 `sub2api-reframe/gemini-3.8-flash-high/high`、`timeout_seconds=null`；实际事件流已有 **7 条 assistant 完成响应**，包含 `read`、`rsi_research`、`rsi_inbox`、`find` 和 `rsi_status`，未见模型调用错误。该任务已经得到局部错误反馈，不只是队列或服务启动。

本次维护没有新建 Runner 实验。主编排此前把 `run-15e2b6ef0021a0bcc5258331` 总结为多步历史对离线 teacher action0 修正有预测信号，但这里未重新独立复算其科学数据，也没有把它升级为真实冻结 pi0.5/LIBERO 闭环收益。新的研究选择和后续验证仍待主编排及研究员完成。

每小时跟进 `rsi-2` 已设为 ACTIVE：仅在确认可恢复执行故障时修复、测试、备份部署并续接，正常工作不重复启动；科学失败、有效自主停止、用户主动暂停和真实依赖等待分别处理。状态无实质变化时保持安静，需要用户余额、权限或显存时报告并暂停。

## 2026-10-01：补充停止审阅完成

恢复后的 Coordinator `task-cc477b99f18948d89510` 已完成真实 Gemini/high 调用，Mission version `887` 记录了 `stop`，version `888` 完成通信发布，Mission 最终状态为 `stopped`。本次没有新 Runner Run，剩余 47 个模型任务和 12 次实验；没有 queued/running 任务。

这次补充审阅明确列出并审阅了 **57 条**历史失败任务，修正了此前把“完成但 Run 失败”的 Runner 记录误放进 `reviewed_failures` 的问题。它确认 `task-14d1eb4edcdff225b225` 的独立问题重述已经在 version `562` 被主编排采纳，并认为在已有 C/R/F/N 对照与后续离线时序信号之后，再进行纯概念重述不会改变当前判断。因此 `stop_reframe_decision` 为 `defer`，没有伪造一轮新的独立 Researcher。

停止理由也修正为更具体的边界：一般冻结 pi0.5/LIBERO 闭环接口已由 `pi05-libero` 工程验收；剩余缺口是要测试内部 Memory 读取时，policy service 尚未提供中间 Transformer 激活或 KV 注入/adapter hook。已有 Run `run-a2a94a441eb468bbc730a937` 否定了朴素 scalar carry 的边际收益，`run-15e2b6ef0021a0bcc5258331` 只证明离线 teacher action0 修正的预测信号，仍没有内部读取分支的真实闭环收益证据。

当时没有交换 Coordinator 与 problem-reframe 模型，每小时自动跟进随后暂停。需要纠正的是：停止决策通过协议校验，不等于接口缺口无法由 Researcher/Coder 实现；不能把“用户必须先提供扩展接口”作为已证实的前提。用户随后明确授权交换模型并继续探索，接续如下。

## 2026-10-01：交换角色、按需读取历史并重新探索

用户明确要求交换主编排与独立问题重述模型。服务器私有路由已改为 Coordinator 使用 `aihub（research）/gpt-6-astra/high`，`problem_reframe` 使用 `sub2api-reframe/gemini-3.8-flash-high/high`。其他角色和凭证保持原配置；普通 Researcher 是 GPT-6.1-Sol/high，部分旧角色仍是 GPT-6-Sol/high，不能概括成所有角色都使用 6.1。256000 上下文、Agent/Runner 不限总时、3 协作者/1 Runner 均保持。

源码提交 `4a2039a` 让可选禁用工具支持 `problem_reframe` 任务类型，Gemini 的 `web_search` 兼容开关随独立重述任务移动，不影响普通 Researcher 和 Astra Coordinator 的搜索。路由交换事件为 version `889`，人工交接为 `890`，第一次恢复为 `891`。部署清单位于服务器 `.rsi/deployment/reframe-route-swap-activation.json`。这次用户重开最初没有补预算。

### 本地拦截不等于模型请求失败

接续任务 `task-6c9ec996909ad11251f1` 和自动重试 `task-1b6c5f977d4bcafe8e09` 均在本地 `context_with_system` 阶段被拦截，Mission 自动暂停至 version `898`。两份事件流只有 system/user 消息，没有 assistant 响应和工具执行，也未到 `before_provider_request`。调用账本中的任务预留不能当作真实模型请求，更不能据此宣称上游余额、网络或模型出错。原始 input、prompt、stderr、events 均保留。

原因是初始提示约 97.6/98.1 KB，叠加 Pi 系统信息和工具定义后，首份消息视图为 128930 字节。Astra 配置预留 128000 输出 token，视图再预留 16384 的请求封装空间；按现有保守“UTF-8 字节视作输入 token 上界”的算法，只余 111616 字节。这个检查可能早于真实 tokenizer 的上限，不能说实际已发送了超过 256k 的请求。

### 默认给索引，正文按需读

用户进一步指出，Agent 应自行按路径选择文件，不应开场灌入全部历史。提交 `dc1e1a0` 改为默认提供当前责任、必要约束、状态与少量历史索引：任务预览保留近期六项及活跃工作，事件/Run 等近期四项，全局节点近期六项；每类同时保留总数和完整只读来源。Wiki 只给标题、状态、路径和哈希，不内联页面正文。交接文件保留目录地址；Researcher 的组合来源给版本和引用，不重复塞入来源任务正文。预览不是科学排名，未显示的节点没有删除。

角色可用 `read`、`rsi_read_source`、`rsi_read_handoff` 选读下一步真正需要的材料。内联历史目标上限由 96 KB 收紧为 48 KB，为系统提示、工具、最近一页材料和输出预留空间；最终请求保护不放宽。旧输入及完整来源快照不改写。

本地核心回归为 **540 passed、15 skipped**。在服务器隔离目录回放两份原始失败，旧视图均稳定返回 78；新视图均返回 0，随后加读一页 16384 字节资料也通过。首份提示缩至 56443 字节，第二份 56515 字节。回放没有调用模型，原失败文件的哈希保持一致。证据：服务器 `.rsi/deployment/acceptance/source-index-context-20261001T121855Z/acceptance.json`。

已兼容部署到实际生产入口 `experiments/rsi_control_plane/runtime_context.py`；备份为 `.rsi/deployment/backups/source-index-context-20261001T122053Z/`，清单为 `.rsi/deployment/source-index-context-activation.json`。服务器相关回归 **91 passed、1 deselected**。另一次扩展测试有 1 项失败：旧生产 `pi-web.ts` 的宿主绝对导入与可移植版测试夹具不一致，Node 原生类型剥离拒绝 node_modules 下的 TypeScript；这是额外搜索适配器隔离测试的限制，日志保留，未把它计为通过，也未为此覆盖现有生产搜索部署。

确认原 Mission 无排队或运行任务后，按已授权的执行故障恢复规则，在 version `899` 补足剩余 48 个模型任务/12 次实验，原消耗不退款；version `900` 传达按需读取与继续自主研究的意见，version `901` 恢复同一个 Mission，并派出 Astra Coordinator `task-8f598b556e7e9761d54e`。这次工程恢复尚不构成新的 Memory 方法或真实 pi0.5/LIBERO 实验收益。

### 后续核验：上下文通过，网络仍阻塞

新任务 `task-8f598b556e7e9761d54e` 已进入 provider 请求阶段，四次检查都通过；最后一次实际请求 JSON 为 93400 字节，加 128000 输出预留仍在 256000 以内。但是调用只返回 `Connection error`，没有有效的模型研究响应或工具执行。后续自动重试 `task-3b4b351f6fd34f068994`、`task-3a0ca4b41d35e3add451` 也因连接问题失败。**这三次连接阶段失败与此前两次网络请求前的本地拦截是不同问题。** 通过请求检查仍不能证明上游已经处理或成功返回。

已核验 `agent.env` 使用的共享代理 `127.0.0.1:7897`：本地 TCP 可以连接，但对 AIHub 和独立 HTTPS 站点均在 TLS 阶段超时；直接访问 AIHub 也超时。该共享代理由 renlab 账户运行，其配置对 kang 不可读，未擅自重启或修改。另对 kang 自己已有的 mihomo 服务做了可逆检查：确认 17890 端口已开始监听后，两个 HTTPS 目标仍返回 TLS EOF；随后恢复为原来的 inactive/disabled，未修改节点、凭证或 `agent.env`。证据位于上述备份目录的 `proxy-recovery.json`。

为避免继续重复调用，原 Mission 在 version `914` 暂停，核验无 queued/running 任务；剩余 **45 个模型任务、12 次实验**。每小时跟进 `rsi-2` 也已暂停。需要先恢复 kangrenlab 可用的代理出口，再在同一 Mission 上按既有恢复规则继续；当前模型交换和源码修复均已保留，未产生新的正式 Run、有效研究响应或科学收益。
