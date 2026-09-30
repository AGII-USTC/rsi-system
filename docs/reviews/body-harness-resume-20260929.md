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
