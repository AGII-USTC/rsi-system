# 服务器 RSI 框架

当前服务器运行的是 Mission 驱动的 RSI runtime；旧的固定阶段编排仅保留给历史批次：

```text
Idea Exploration → Hypothesis Development → Candidate Evolution
       Wiki draft       structured claim         Run evidence
```

三层之间不是强制每轮直通。Idea 可以长期保持 draft；结构化 Hypothesis 校验
statement、mechanism、assumptions、predictions、experimental_design 和
prior_art_status。同一假设使用 `expected_version` 和 `revision_reason` 修订，历史版本保留。
新 Candidate 必须绑定 `candidate_ready` 的具体 `hypothesis_version` 并给出预期观测；
子代记录父代选择理由。明确声明 `kind=baseline` 的根基线可不关联假设，但不得改动源码。
历史未关联记录标记为 `legacy`，不会被迁移程序伪造为成熟假设。

当前主编排的数据流是：

```text
持久 coordinator Agent → 结构化决定 → 确定性 runtime
          ↑                             ↓
     黑板 / Wiki ← 产物与证据 ← 按需协作者 / Runner
```

代码可部署到任意独立目录，由部署者管理。后台服务可按需检查队列；常用操作见 [部署说明](../../deployment/rsi/README.md)。

## 模块

| 模块 | 职责 |
|---|---|
| `knowledge.py` | 读取 Markdown 知识、按关键词检索、校验证据后写入候选知识 |
| `proposals.py` | 输出研究上下文、接受结构化提案，固定评测定义 |
| `schema.py` / `store.py` | SQLite 状态、父子实验关系和审核记录 |
| `protocols.py` | 将已登记实验协议接到现有 Harness / Memory 入口 |
| `provenance.py` / `worker.py` | 执行已提交的代码快照，复制输入、记录哈希、控制超时和取消 |
| `cycle.py` | 结果写回知识库；重试时避免重复页面 |
| `orchestrator.py` | 三个角色的持久阶段、预算、返工、暂停与下一轮 |
| `runtime.py` | 调用持久主编排 Agent，校验决定，执行已登记协作者任务 |
| `runtime_types.py` / `hypothesis.py` | Mission 与结构化假设的输入校验 |
| `runtime_store.py` | SQLite 黑板、任务、候选和 Run；原子调度、任务预算与版本检查 |
| `runtime_hypotheses.py` | 假设修订历史、成熟度准入和基线例外 |
| `runtime_evidence.py` | 按任务生成带哈希的只读证据清单，限定候选、父代、已分配 Idea 的探索及固定联合来源 |
| `runtime_communications.py` | 全角色可写任务工作区、带作者的交接快照、同 Mission 收件目录 |
| `runtime_roles.py` | 研究、假设、实现、分析、批评和主编排的职责与模型路由 |
| `runtime_runs.py` / `runtime_recovery.py` | 稳定实验身份、回执恢复、实验结果原子登记 |
| `runtime_limits.py` | 每日调用登记、模型策略记录、Mission 实验额度和合同冻结 |
| `runtime_knowledge.py` | 校验 Run 证据并幂等写回未确认 Wiki 页面 |
| `runtime_cli.py` | Mission 的 start/status/pause/resume/tick 入口 |
| `pi_agent.py` | 独立 Pi 会话、结构化输出、超时和调用记录 |
| `workspace.py` / `sandbox.py` | 候选分支、固定测试和 Linux 执行隔离 |
| `cli.py` | 人或智能体都可调用的统一入口 |

研究知识库记录跨实验的经验；Runtime Memory 仍负责单次任务内的因果适应。

## 统一的算法与系统演化面

Runtime 不把“算法”限制为策略网络或 Memory。调度器、候选准入、状态机、协同策略和 Runner 策略，只要改变系统如何选择、执行或更新，也作为算法组件登记在同一个 Candidate Manifest 中。Manifest 使用 `optimization_scope=algorithm|system|algorithm_and_system` 区分变化范围，并显式列出 `components`、`mutation` 和可选的 `ablation`。评测合同仍属于 Mission 的固定边界，不能由 Manifest 改写。

因此系统级优化继续复用现有 Candidate 父子链、代码提交、Run 和 coordinator 反馈，不引入第二套系统实验协议。`algorithm_json`、Run 输入和冻结的 `runner_input.json` 保存同一份规范化 Manifest 及其 SHA-256；旧 Candidate 没有该字段时按空 Manifest 读取。当前这层提供可组合、可追溯和可消融的登记与冻结语义，自动生成消融组合和 Pareto 搜索仍由后续 Candidate 提案策略决定。

## 已接入协议

- `runtime-harness-smoke-v1`：CPU 仿真，固定周期调度，关闭外部事件监控和动力学切换。可设 `seed`、`steps`（最多 160）。这是框架连通性检查。
- `runtime-harness-period-v1`：固定 seed=0、64 步、延迟 4、动作块 16，只搜索请求间隔 `{4,6,8,12,16,24}`。先满足最终 waypoint 距离 ≤5 厘米，再最小化请求数；这是单种子的调度参数研究演示。
- `residual-memory-dev-v1`：复放已有的四个开发集场景，记录 nominal / online 预测误差。数据目录和模型由 `deployment/rsi/contracts.json` 固定。

配置候选需要 Mission 显式开启 `allow_config_candidates`、关闭 `allow_code_candidates`，
并将 `evaluator.source_commit` 固定为完整提交号；协议合同也必须声明 `candidate_mode=configuration_only`。
内核校验参数后可直接交给 Runner，不需要没有代码改动的 coder 阶段。子代仍需绑定成熟假设、父代和选择理由。
小任务可在 Mission 的 `resources.agent_thinking` 指定推理档位；实际值写入调用策略记录。

每次运行最多 1800 秒，顺序执行。数据输入、源代码提交、参数、输出证据均记录 SHA-256 或 Git 提交号；失败和中断也保留。源码快照是版本隔离，执行器仍使用同一个 Python 环境，因此不是完整容器环境复现。

范围内实验由持久主编排 Agent 读取黑板后分配研究问题，调度决定记录 Mission 版本、理由、预期信息、预算影响和停止条件。所有 Agent 可自主派发同一 Idea 的子任务，委派次数与递归深度无限；改变目标、跨分支资源或停止仍由 coordinator 决定。coordinator 使用研究模型；所有模型角色都有 read/edit/write 工具；Runner 是确定性代码。正式实验自动写回为 `candidate`，探索 Run 单独保留，运行成功不等于科研结论成立。

## Agent 写作与协作

所有 Agent（包括主编排、研究、假设、编码、分析和批评）都可以在自己的
`.rsi/runtime/tasks/<task_id>/workspace/` 中创建和编辑文件、嵌套目录。
材料可以是讨论、推导、研究笔记、Wiki 页面提案，也可以是原型代码；不按文件后缀限制研究表达。
提示中提供绝对路径，并要求写 `handoff.md`，说明发现、分歧、未验证部分及下一步问题。

任务结束时，内核把材料和最终 JSON 复制为 `communications/outbox/` 快照，
随任务结果事务登记 `communication_published` 事件。作者角色、任务、Mission、文件哈希和任务成功/失败状态
均可追溯。失败任务的已有笔记也保留，明确标记失败，不视为正式研究结果。
后续同 Mission Agent 在 `communications/inbox/manifest.json` 中发现此前的交接文件，
读取副本，再在自己的工作区回复并引用作者任务和文件 ID。收件快照与输入一同冻结，重启恢复不引入新消息。
子任务在当前对话中完成后，`rsi_wait` 返回其已发布文件 ID；`rsi_read_handoff` 按 ID 分页读取，
无需猜测其他任务的私有路径。
每个任务的写作不会进入候选代码 diff，也不会覆盖其他 Agent 的原稿、调用回执或测量记录。

写作是所有 Agent 的能力；研究、实现、分析是职责。研究角色自主提出机制与备选解释，
主编排负责任务选择与资源分配。正式 Candidate 的实现交给 coder 的独立 worktree；
草稿中的原型不会自动执行或成为已测 Candidate。可经探索 Runner 执行，留下独立的探索证据。

任务可通过 `idea_id` 关联研究分支，无 Candidate 时也允许 coder、runner、analyzer、critic。
协作者用 `rsi_delegate` 或 `rsi_run_probe` 自主派发子任务，无需分配 `delegate_budget`；researcher/hypothesis
还可在运行中用 `rsi_register_hypothesis`、`rsi_register_candidate` 持久推进 Idea → Hypothesis → Candidate，
随后直接把 Candidate 交给 coder、analyzer、critic 或 Runner，不需要 coordinator 逐项批准。
委派次数与递归深度无限；旧数据库和恢复任务的有限额度自动迁移为无限。
Mission 明确配置的模型任务与 Run 总额度仍可用于指定有界实验，未配置时总额度也无限。
并发名额只控制同时运行数量，不限制累计派发数量。每次调用在事务中登记；拒绝不会创建任务，但会留下可审计的失败回执，
错误返回修复提示，Agent 可纠正字段后重试。
`rsi_message` 通过 `to_task_id` 或 `to_role` 定向，
必须限定 Idea；任务已有 Idea 或输出只有一个 Idea 时可推断关联，否则须显式指定。
主编排可接收各 Idea 发给 coordinator 的消息；`in_reply_to` 记录回复关系，普通消息不自动触发模型调用。
`rsi_wait` 在当前对话内并行推进独立子任务并恢复其结果；最终 JSON 主要记录研究解释。
coordinator 可用 `rsi_branch_control` 取消目标分支的排队后代，并向正在运行的后代发送持久的安全停止请求；
运行中的 Agent 不会被中途强杀，以免丢失交接或破坏代码状态。
主编排请求 `stop` 时，已入队的子任务仍会完成；停止意图持久保存，队列清空后才将 Mission 标为 stopped。
若待处理任务失败，Mission 暂停供核对；恢复旧版本留下的“已停止但仍有队列”状态也遵循这一顺序。

探索 Runner 的 `experiment` 包含已完成的 `source_task_id`、已发布 Python 文件的 `script_file_id`、`question`、
`baseline`、`expected_difference`、`falsifier`、`source_reason`。作者交接文件、项目 Git 源码与输入哈希
在派发时按清单核对，运行前冻结；旧任务的 `script` 路径仍可解析，歧义会要求改用文件 ID。
脚本通过 `RSI_EXPLORATION_SOURCE`、`RSI_EXPLORATION_INPUTS` 读取快照，
通过 `RSI_EXPLORATION_OUTPUT` 写结果。服务器沿用现有 Python 依赖，沙箱禁止网络，最多 120 秒；
输出至多 32 个文件、单文件 2 MB、合计 8 MB。异常退出和不支持想法的结果同样保留。
Runner 是确定性执行监督器：先做入口、依赖、路径和输出协议预检，再执行冻结脚本并生成
typed Run receipt。发现导入、语法、入口或输出契约问题时，Runner 会把带日志和修复要求的
repair task 退回源 Coder，并通知该分支 Researcher；这属于工程修复，不是科学负结果。
执行完成后，Runner 只把 receipt 交给 Researcher。Analyzer/Critic 由 Researcher 或 Coordinator
读取 receipt 后显式派发，`experiment.review_roles` 仅作为历史兼容的复核意图元数据，不再自动创建任务。
Runner 不解释指标，也不决定 Hypothesis/Candidate。
结束后如 Mission 还有模型任务额度，runtime 不会自动创建研究反馈任务；Researcher 通过
`rsi_wait`、`rsi_status` 或 `rsi_inbox` 读取结果并继续编排。
没有终态回执的中断暂停核对，已完成回执可直接恢复登记，不重复执行脚本。
目前不提供自动下载或安装 benchmark 依赖；外部输入需先实际保存为作者交接文件，并记录来源。

researcher 的科研顺序固定为：先检索相关论文和公开方法，再读 Mission brief、README、
其他 Agent 的交接材料，以及相关源码和测试。它可按研究问题深入检查实现行为、核对前提与固定评测的区分力，
并据此提出 Idea、机制和可证伪预测；源码检查不等于实验测量。
researcher 与 coder 都可使用 `bash`、GitHub、alphaXiv 和网页工具，在各自任务工作区探索。
coder 负责正式 Candidate 实现和复用审计，可以直接克隆有用的公开 GitHub 仓库，
并在自己的工作区写 `implementation_note.md`，说明来源、改动、测试、限制和待验证问题。
编码完成后，researcher 或 analyzer 读取这份说明和 Run 证据，判断 Idea 是否得到支持。
每次 researcher 输出都要附 `literature_review`，记录查询、来源、发现、争议和缺口；无法检索时必须说明原因并保持结论为初步结果。
Pi 回执与 `agent_tool_audit` 同时记录真实工具调用的成功/失败；模型写出的 URL 或 searched 字段不能替代调用证据。

为控制上下文与存储，一个交接快照最多接收 255 个工作区文件，每文件 1 MB、合计 8 MB；
另附最终 JSON。收件快照按最近发布优先，最多 256 个文件、32 MB，清单明确列出未复制项。
工作区原稿仍保留；所有交接内容标为 Agent 撰写、尚未验证，不混入测量证据。

持久身份由数据库、事件与调用记录恢复，不依赖一个永久在线的模型会话。独立子任务支持有界并行。
Pi 显式加载 `pi-runtime-tools.ts`，提供 `rsi_delegate`、`rsi_run_probe`、`rsi_register_hypothesis`、
`rsi_register_candidate`、`rsi_branch_control`、`rsi_message`、`rsi_status`、`rsi_wait`、`rsi_read_handoff`。
委派、登记、干预与消息在工具调用时入库，成功立即返回任务 ID/版本/Candidate ID/回执；
后台协作者执行器和 `rsi_wait` 按队列顺序领取可运行任务并并行执行，把子 Agent 的最终结果、已冻结交接文本或
Runner 原始日志及哈希回传给等待者。工具调用 ID 以哈希存为幂等键；参数错误单次拒绝，Agent 可以修正后重试。
新 Agent 最终 JSON 主要表达研究解释和主编排的阶段决定；带工具协议版本的回执会忽略最终 JSON
里的旧任务与消息字段，避免重复派发。旧任务的 JSON 入口仍可读取和回放。
输入版本仍有效且有完整 Agent 回执时，过期任务复用回执完成登记，不再调用模型。
Runner 按 task_id 固定实验身份并使用独立实验登记库；已完成实验直接导入，实际中断保留中断结果。
工具调用已持久登记而父 Agent 中断时，完成的子任务不再执行，runtime 生成新的同角色续跑任务，
并通过任务表和带作者归属的交接快照恢复结果；这不是恢复原 Pi 对话的 token 级上下文。
无完整回执的普通任务仍暂停待核对。代码测试在数据库事务外执行，最终更新再校验租约及 Mission 状态；
兄弟任务的追加事件不会使任务结果过期，但操作员暂停/恢复 Mission 后旧输出仍被拒绝。
Idea 先随任务事务入库，再幂等写入 Wiki；同一批输出校验失败不会发布部分 Idea。

服务器默认同时运行 3 个子 Agent、1 个 Runner。等待中的父任务释放执行名额，恢复推理前
重新取得名额；每个执行线程独立持有 SQLite 连接，任务租约领取与容量检查在同一事务完成。
同一 Candidate 的编码、运行和复核互斥。`work --follow` 只执行协作者，不新建主编排；
单独的 systemd 服务使派发后任务立即推进。`rsi_wait` 若自行承接一批任务，会等待该批执行完毕
再回传目标结果；已由后台领取的任务则直接观察其持久结果。
`agent_timeout_seconds=null` 表示 Agent 没有强制运行期限；正常完成或明确错误结束调用。
独立心跳持续更新租约，执行器失联后的回执恢复规则仍保留。

## 当前边界与后续接入

服务器 Pi Agent、主编排模型配置与 runtime 已安装。原 Memory Mission 暂停；
`period-demo-20260926-001` 已完成四次真实参数实验并停止，其中包含两个第一代候选和一个父代反馈后的子候选。
该演示禁止代码修改，实际调用了研究角色但没有 coder 或独立 analyzer/critic，不能作为算法代码进化验收。
受控测试另验证了三候选反馈链。尚不能宣称目标架构全部实现或适合无人值守长期研究。

新 runtime 执行 Mission 的 `max_agent_tasks`、`max_seconds_per_run` 和可选的 `max_runs`；模型任务数包含 coordinator、失败及取消任务，实验额度按 Runner 任务预留。
`runtime_max_agent_calls_per_day` 可单独约束 Mission runtime 的 UTC 每日 Pi 调用次数；
设为 `null` 时不设每日次数上限，调用仍持久登记。旧 orchestrator 继续使用 `max_agent_calls_per_day`。
次数不是工具轮次、token 或费用总额。
每次调用保存模型、角色、thinking 和超时策略。Mission 首次执行时冻结合同 JSON 哈希，后续合同变化拒绝执行。
该哈希还不覆盖全部评测器源码、环境和确认集。`max_cycles` 仅用于旧 orchestrator。
Agent 通过内核复制的清单读取原始证据；控制库仍不可读。历史文件无运行时哈希的，清单明确标为“当前采集”，不能倒推其原始真实性。

Candidate 评审的空父代编号不匹配任何 Run。评审可读取本候选、实际父代、显式分配 Idea 的探索 Run，以及联合节点固定的来源；探索记录保持 `kind=exploration`，不会因复制给 Candidate 评审而成为该候选的测量结果。

Pi 工具通信协议 v2 在正常长等待和服务端互斥锁排队期间发送心跳；客户端仅收到最终结果才完成工具调用。30 秒无数据超时仍用于发现静默断线，同一调用 ID 的重试回放已提交回执。旧客户端未声明协议版本时只收到最终响应。
知识检索仍为关键词匹配；跨领域协议、确认集封存和精确费用/GPU 配额仍待完成。

新增协议时，需要同时加入固定合同、参数校验、执行入口、结果解析和验收用例。评测器、测试集和成功定义不作为搜索变量。源码修改须通过 Git 提交后才参与下次运行。不会自动修改或发布机器人运行代码。

## 验证

```bash
python -m pytest rsi_system/core/tests -q
python -m rsi_system.core lint
```

守卫测试需要 Node 22，可用 `RSI_TEST_NODE` 指定服务器随 Pi 安装的 Node 路径。
测试覆盖版本迁移、成熟度准入、证据范围/篡改/符号链接、调用/实验预算，以及回执、代码提交和实验落库边界的中断恢复。
协作测试覆盖所有角色新建/编辑笔记与原型、研究→主编排→编码交接、同 Mission 发现、失败笔记、输入冻结及回执恢复。
三候选链使用受控 Agent 和固定测试评测器，属于工程证据；不能替代真实模型的多代科研验收或系统断电测试。

## 持久研究图与延迟联合

ResearchNode 表示长期研究问题，AgentTask 表示一次执行。`rsi_research` 可创建子问题、修订蓝图、记录关系，并通过 `compose` 从至少两个明确的 `node_id + revision` 创建新问题，同时入队 researcher。分支可长期独立推进，来源可以深浅不同、版本不同；没有强制合并阶段。合并保留原节点，不自动停止分支，也不等于 Git 代码合并或科学收益已证实。

图谱变更必须提供非空 `data.reason`；兼容入口可将顶层 `reason` 归一到该字段，但两处理由冲突时拒绝调用。`snapshot` 无需理由。错误回执明确指出嵌套字段路径，拒绝的操作不修改图谱。

`rsi_research` 的 `create` 若省略 `data.parent`，新问题会以执行任务绑定的节点及其固定修订为父节点；未绑定节点的 coordinator 则使用 Mission 根节点的当前修订，并写入 `decomposes` 边。需要指定其他父节点时传 `data.parent: {"node_id": "...", "revision": 1}`。只有 coordinator 可以显式设置 `data.separate_root: true` 创建独立根节点，且不能同时传 `parent`。旧 Mission 若没有标准根节点，唯一的旧根节点仍可作为默认父节点；存在多个旧根节点且归属不明确时必须显式指定父节点或独立根。历史图谱不会被回填或改写。

节点和关系保留不可变修订，联合节点固定输入版本以及当时可用的来源任务与 Run。新 researcher 可读取这些来源的交接和实验材料，再调用 coder、探针或提出下一项研究。正式 Candidate 继续经过准入、worktree 和 Runner；代码集成由 coder 实现，收益需要组合与消融实验验证。`depends_on` 当前表示声明的研究依赖，并不自动阻塞调度；执行等待使用真实子任务和 `rsi_wait`。

`finding_status` 明确是具名 Agent 的判断。supported/refuted/mixed 必须引用本 Mission 的 Run 或带哈希的已发布交接；引用核验只检查来源完整性，不证明判断正确。关系 evidence_refs 使用 `run:<run_id>` 或 `handoff:<task_id>:<manifest_sha256>`。

前端读取 `GET /api/research?mission_id=<id>&at_version=<version>` 重建历史图，读取 `GET /api/events?mission_id=<id>&after_version=<version>&limit=150` 补齐事件。游标是 Mission 内版本；分页有 `next_version`、`has_more` 和 `high_water_mark`，不要求跨 Mission 的全局 event_id 连续。动画后续可直接映射这些真实记录。本轮没有改变动画。

## 探索覆盖与停止复核

Coordinator 上下文及其 `rsi_research snapshot` 当前版本查询附带 `exploration_portfolio`：每个节点绑定的任务、Run、未决状态、评审事件游标、剩余预算。它只汇总事实，不把节点数当成机制多样性分数；历史版本查询不附带当前执行状态，以免混入未来信息。

主编排在收窄、联合和停止前，应比较不同问题定义、表示、机制或简单基线，保留未探索路线及区分它们的下一项实验。允许在任何协调决策中记录 `exploration_review`。Mission 设置 `resources.exploration_review=true` 后，停止时必须附带此结构（示例 Mission 已开启）：

研究中途出现会改变下一步选择的重要权衡或未解释结果时，主编排可通过 `rsi_delegate` 派发 `role=researcher, task_kind=problem_reframe`，绑定 Mission 根节点的确切版本。该任务不绑定 Idea/Candidate；初始提示和收件材料隐藏主编排的历史结论，保留原始 Mission 目标、研究节点问题和证据入口。研究员在 `reframe_review` 中提交重新表述的问题、替代解释、便宜的区分检查及建议；若没有有价值的检查，需说明原因。报告成为研究反馈。主编排下一次处理该反馈时必须在 `challenge_responses` 中按任务 ID 逐项记录 `adopt|defer|reject`、证据理由和下一项检查。运行时防止同时重复派发、遗漏回应或重复回应，但不会仅凭无总赢家就自动启动重审，也不强制合并或耗尽预算。独立性针对初始主编排结论，不等于盲评：研究员仍可主动读取授权来源，报告仍需证据核查。

当开启 `exploration_review` 的 Mission 准备停止、已有未决问题或研究反馈且仍有模型任务额度时，停止决定还必须包含 `stop_reframe_decision`。主编排可引用已完成且已回应的独立重审，也可明确暂缓；暂缓需说明为何新视角不会改变当前取舍，引用本 Mission 已发布的任务或 Run，列出区分检查、预算影响和重开条件。旧重审之后若又出现其他研究反馈，不能把它算作对新反馈的独立审查。缺少字段或引用伪造会拒绝停止决定。要实际派重审时，先创建根节点 `problem_reframe` 任务并继续运行，不能在 `stop` 中声称已经派发。预算用尽不阻止原有收尾路径。后端核验决定与来源，不能判断研究理由是否科学充分。

```json
{
  "alternatives": [{"node_id": "branch-b", "revision": 2, "mechanism": "alternative explanation",
    "disposition": "defer", "reason": "insufficient remaining resources", "next_test": "a discriminating controlled comparison"}],
  "narrowing_reason": "Why this scope is appropriate for the Mission",
  "remaining_unknowns": ["Which mechanism explains the observation?"],
  "next_test_decision": "Why to continue or defer the feasible next test",
  "reviewed_event_version": 42
}
```

`alternatives` 可以为空，但需解释收窄理由。引用必须是本 Mission 真实节点版本，游标必须覆盖最新评审；没有固定分支数、强制合并或全局最优保证。这些字段是 Agent 判断，后端只核验结构与引用，不能证明不同机制确实独立或审查内容正确。旧 Mission 未开启此设置时继续兼容。

真实 analyzer/critic 事件会触发主编排复核。仍有任务时的 stop 是暂定决策；后续出现新的研究交接或评审，收尾前必须再安排一次协调决策。预算耗尽会暂停并注明待复核，不能按旧决定自动结束。目前综合预算仍需主编排主动预留，没有自动保留或追加额度。

模型中断等历史失败仍保留为 failed。主编排可在 stop 时明确填写 `reviewed_failures: [{task_id, reason}]`；只有这些已审查失败不再阻塞延期收尾，之后出现的新失败仍会阻塞。角色名和一份成功回执不能替代研究有效性检查；审查者提出的代码缺陷也需核验实际接口与最小复现。
