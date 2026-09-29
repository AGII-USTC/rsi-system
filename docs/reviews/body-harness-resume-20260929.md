# VLA Harness 研究续接审查（进行中）

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
