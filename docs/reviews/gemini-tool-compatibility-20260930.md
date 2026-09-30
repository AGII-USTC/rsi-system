# Gemini 工具兼容修复与部署验收

日期：2026-09-30。原研究 Mission：`body-memory-open-20260929-001`。

## 已确认的故障

服务器 `kangracknerd` 的 Sub2API 将 `/antigravity/v1/messages` 转发到
Antigravity OAuth 账号 5。实际进程报告版本 0.2.4、源码 commit
`5de5e2bed035d43591a2e10e51f420ef6a84eb98`。网关保留的上游报错为：

```text
400 INVALID_ARGUMENT
Please enable tool_config.include_server_side_tool_invocations
to use Built-in tools with Function calling.
```

这是工具兼容问题。不能从这条错误推断余额不足、Cloudflare 配置错误或模型服务死亡。
上游源码的 Claude 转换路径已经有混用参数处理，所以仅凭版本标识无法解释运行进程
为何仍报错，本次没有声称修好了中转软件所有转换路径，也没有替换其二进制。

另有一个明确的语义问题：转换器将普通自定义函数名 `web_search` 识别为 Google
内置搜索，移除它的函数声明，并触发搜索模型回退。RSI 原本注册的是客户端 Exa
搜索函数，这种按名字转换会改变原有工具的含义。

## 最小请求和真实工具验收

通过现有私有凭证发送独立验收请求，不复制密钥到仓库、不执行研究实验：

| 请求 | 结果 |
| --- | --- |
| 普通 `lookup` 函数，流式请求 | HTTP 200，返回 `lookup` 工具调用 |
| 普通 `web_search` 加 `lookup` | HTTP 400；网关日志仍为上述混用参数错误 |
| Anthropic 内置搜索声明加 `lookup` | HTTP 400；同类错误 |
| 修复后完整 Pi 调用：读取源码、接收工具结果、返回 JSON | 两台服务器均成功 |

第一次非流式纯函数探针曾网络超时，保留为失败记录；不能把它当成功结果。
第一个 README 读取探针还被路径边界阻止，模型虽然返回了 JSON，也没有通过
文件读取验收。改读存在的 `deployment/rsi/pi-guard.ts` 后，真实 `read` 成功。

成功回执：

- `kangrenlab:/tmp/rsi-read-acceptance-sub2api-reframe-guard/agent_result.json`
- `zhaohuijin:/tmp/rsi-read-acceptance-sub2api-reframe-guard/agent_result.json`
- `zhaohuijin:/tmp/rsi-read-acceptance-aihub-guard/agent_result.json`

## 可选开关

通用代码保留搜索工具。仅在服务器私有 `~/.config/rsi/research.json` 设置：

```json
{"agent_disabled_tools": {"coordinator": ["web_search"]}}
```

它在新模型请求之前缩减工具列表并跳过注册；不会只在工具执行时拦截。
其他角色的搜索不受影响，主编排保留 MCP、GitHub、页面读取等工具。将列表设为
`[]` 即可恢复。没有关闭 Gemini 服务，也没有更换任何模型、思维强度或并发配置。
旧任务输入、失败日志和已完成回执保持原样。

相关实现：`rsi_system/core/config.py`、`pi_agent.py`、`runtime.py`、
`orchestrator.py`、`deployment/rsi/pi-web.ts`。另外修复了新部署中
`pi-web-access` 找不到模块的问题，使用安装目录和现有 jiti 加载方式。

核心回归为 393 passed、15 skipped；随后适配器导入与禁用工具相关测试通过。
目标服务器的新配置测试也通过。这些是工程验收，不是研究收益。

## 部署与续接

新部署入口：`zhaohuijin:/data-nfs/ZhaohuiJin/rsi-system`。完整部署说明见
`docs/deployment/zhaohuijin-20260930.md`；没有在新服务器创建研究 Mission。
AIHub 直连存在 DNS/TLS 问题，服务器已有本地 HTTP 代理 `127.0.0.1:7894`
通过 HTTPS 和真实 GPT-6.1-sol 工具往返验收。代理只在私有环境配置中使用，未改系统 DNS。

原 Mission 从暂停版本 749 恢复。预算事件 750 保留历史 93 个模型任务、9 次
实验任务，并补足剩余 48/12；总上限因此为 141/21，没有删除失败消耗。
原排队任务 `task-ef6604d2cc0e7abcdc86` 完成了 8 次工具操作并产出主编排决策
事件 753。它选择等待，未派新任务，运行时随后将空队列的 Mission 置为暂停。
这次等待不是 400 失败，也不能被说成已经开展新实验。

这轮请求还暴露出上下文配置不等于请求上限：单轮网关用量报告约 470k，超过用户
要求的 256k。后续新调用需要紧凑历史引用和发送前检查，旧输入与回执不覆盖。

## 上下文故障与保护

在新的请求保护启用后，Curator 任务 `task-d3488a1efe64d5125ace` 及其恢复链
仍因多轮读取累积失败。最后的 `task-fa13009972a6dfe1b62d` 日志为：

```text
RSI context budget exceeded: 145059 input bytes + 128000 output tokens > 256000.
```

Mission 在事件 769 暂停，队列为空。这是发送前保护触发，不是原来的 Gemini
400，也不是新的科学失败。输入字节数用作保守的 token 上界，输出预留仍为
模型配置的 128000，不能把这段字节计数当作供应商实际 token 用量。

后续修复将紧凑上下文用于所有启用上限的新角色，并保留 Researcher、Curator
等角色的节点绑定、研究责任、来源任务及文档索引。完整上下文以哈希引用存入
任务证据目录。旧失败任务、输入及预算消耗不改，恢复须创建新的续接任务。
每次调用只验收本次新增的预算检查，不能用旧检查冒充新请求验收。

第一次多轮修复后，`task-e5073374d7eeb0a570e8`、
`task-980cab26f179e787de07`、`task-550232c18eb0542b59e2` 仍被保护终止。
真实归档确认 `read` 的正文已缩短，但 `details` 中两条原截断文本各约 54 KB，
最新工具结果仍携带这份重复内容。最终修复将完整详情保存为哈希来源，正文、
工具调用 ID、错误状态和当前职责保持可查，避免详情重复占用请求视图。

真实失败归档重放后，请求为 59,862 字节，加 128,000 输出预留通过保护；
38 项上下文测试通过，核心其余测试为 394 passed、15 skipped。
`git diff --check` 通过。这些都是请求与部署验收，尚不表示长期科研流程无故障。

最终 guard 哈希为
`ccf10f442c7e946b62a75cfb630f880874397043f4adad1eda08b3d0771564a0`。
2026-09-30 16:49 CST 已在两台服务器安全边界部署，原进程文件备份在各服务器
`/tmp/rsi-guard-details-backup-20260930T084922Z.ts`（kangrenlab）和
`/tmp/rsi-guard-details-backup-20260930T084923Z.ts`（zhaohuijin）。

原 Mission 预算事件 786 保留累计 99 个模型任务和 9 个 Runner 任务，将总上限
设为 147/21，以补足剩余 48/12；人工基础设施交接事件为 787，恢复事件为 788。
搜索开关、模型路由、思维强度、3/1 并发和 Agent/Runner 不限总时长配置未改。
后续运行结果必须单独核验，不能用这次恢复事件代替模型或实验成功。

16:52 CST 实查：续接 Curator `task-61999211280e1ac7f0f8` 正在执行，已记录
11 次允许的真实 provider 请求、27 次成功工具操作和 3 次动态上下文投影；
最大请求为 100,251 字节，加 128,000 输出预留仍低于 256,000。此时剩余
47 个模型任务、12 次实验。该任务尚未完成，尚无新的正式 pi0.5/LIBERO Run。
目标新服务器的搜索与上下文专项测试为 57 passed；私有配置隔离问题已在测试
fixture 中修正，未通过改动生产配置来让测试通过。

## Wiki 重复阅读诊断与增量写作指示

后续核查 `task-61999211280e1ac7f0f8`：218 条 assistant 响应，298 次工具操作，
其中 read 252、ls 17、grep 24、find 5；没有 write、edit 或 rsi_wiki_check，
workspace 中没有业务草稿或进度笔记。累计日志用量为 4,686,196 totalTokens，
含缓存读取，不能据此推算实际账单，也不能算作有效研究产出。

15 条待整理记录、4 条主编排记录形成 122,892 字节的地址簿。252 次读取中，
97 次读这个地址簿、17 次读 SCHEMA、28 次读同一恢复 handoff；这不是 252 份
不同文献。95 次 read 结果经工具保护只保留 2,048 字节前缀。较早结果又变为
384 字节前缀，最后一次请求视图将 517 条消息投影为 40 条，移出了 478 条消息，
没有提供包含已核实内容与待办事项的进度摘要。

归档保留原文不等于模型能有效续读：11 次读取 `.context-sources`，其中 5 次
再次被截断并归档。JSON 中的正文换行是字符串转义，部分正文在文件中是
10--41 KB 的单行；按 JSON 文件行数分页无法继续切分这行内部的文字。因此
分页读取、二次截断和进度丢失能促成重复取证。无对照重跑，不能把这些工程
问题认定为唯一原因；任务批量和模型自身迟迟不落稿也可能参与。

写入工具在角色配置中存在，workspace 允许写入；本次没有写入尝试或发布校验，
不能归因于权限拒绝或发布校验太严格。发布器本来也允许只覆盖一部分 pending
记录，不要求一次整理全部。最后的 AIHub `403 insufficient balance` 是中断
原因，不能解释此前为何一直未写出草稿；事件 797 记录余额阻塞，798 暂停。

用户随后明确要求边读边写。人工消息事件 799 发给 Curator、800 发给主编排，
后续 Curator 角色提示已同步本地及两台部署：来源组足以支持范围明确的内容时
先写 workspace/wiki 草稿并执行 rsi_wiki_check，再读下一组；未核验内容和
缺失证据标为 unresolved；进度笔记保存已读来源及范围、已核对内容、草稿地址
和下一步。证据与发布校验要求未降低，旧冻结输入未改。此次没有恢复 Mission，
当时上下文续读和进度保留的代码问题尚未修复。

## Curator 续读修复与模型配置

2026-10-01（北京时间）已将 kangrenlab 和 zhaohuijin 私有配置中的 Curator
显式设为 `sub2api-reframe/gemini-3.8-flash-high/high`，解析后的有效路由一致。
`agent_context_limit_tokens=256000` 保留；主编排、独立重述和其他角色路由未改。
路由保存在 `~/.config/rsi/research.json`，凭证未写入源码或本报告。

新的 Curator 来源目录只列来源摘要及 `documents_manifest` 地址、大小和哈希，
每条记录的完整目标与文档地址放在独立的只读清单。体积回归夹具保留 15 条研究
记录、6 条规划记录和 182 份文档，总目录 21,226 字节；这是夹具检查，并非
真实任务的新产出。已有 schema 1 冻结目录和证据收据保留。

`rsi_read_source` 在同一文件授权内按 Unicode 字符游标续读，可通过 JSON pointer
提取归档中被转义的正文；续读必须带原文件 SHA-256。默认一页 8,192 字节，
最多 16,384 字节，完整返回不超过 20,000 字节。分页结果避免再次截断；上下文
压缩时保留当页正文，运行时追加消息独立保存地址。普通小段读取的正文也保留，
超大 details 另归档。源文件上限 16 MiB，每页仍需读取并核对整个源，暂不承诺
任意大日志的流式读取。进度靠已要求的 workspace 笔记续接，不声称模型必定写出
笔记或不再重复阅读。

修复已在 kangrenlab 暂停边界部署，回执为
`/home/kang/embodied-rsi/.rsi/deployment/curator-paging-activation.json`。
部署备份位于 `.rsi/deployment/backups/curator-paging-20260930T160553Z/`。
zhaohuijin 此时已有另一个运行中的 Mission `dsh-cve-envbuild-20260930-001`，
所以仅完成之前的私有路由修改，代码部署检查拒绝继续，未干预其研究。
原 `body-memory-open-20260929-001` 与自动跟进保持暂停，旧任务输入、失败记录、
预算与回执未改写。

本地核心测试为 460 passed、15 skipped；kangrenlab 使用生产 Node/Pi 的分页、
上下文和 Curator 专项测试为 89 passed、1 skipped。第一次服务器测试的两处失败
来自测试假定 prompt JSON 必定带缩进，而生产配置使用紧凑 JSON；测试改为解码
包含 `context.curation` 的对象后通过，生产 prompt 未因此改写。跳过项包括
测试所需的 SDK/隔离依赖；不能据此声称所有宿主环境组合均已验收。

随后用真实私有 Gemini Curator 路由做一次隔离工程验收。临时 Fixture 有自己的
SQLite、两个静态任务和合成来源，不读写真实 Mission，不执行 Runner，也不发布
到真实 Wiki。31,757 字节的 JSON 源通过 `/body` 连续读了 4 页，SHA-256 和
Unicode 游标一致，末尾未提前提供给模型的标记核验成功。首屏后确实写入进度
笔记，最后写出 workspace 草稿，并有一次成功 `rsi_wiki_check` 工具与 SQLite
回执。14 次 provider 请求前检查均在 256k 上限内。累计 usage 为 input 132,348、
output 4,532、totalTokens 136,880；这是多次调用累加，不是单次上下文或实际账单。

验收任务为 `task-adeae504f53138d9ba87`，源事件为隔离数据库中的 4。
不可将它当作原 Mission 的科研任务或正式 Run。原始 Fixture 保留在
`/tmp/rsi-curator-acceptance-hzblqrhq`，32 个文件另归档到
`/home/kang/embodied-rsi/.rsi/deployment/curator-paging-acceptance/rsi-curator-acceptance-hzblqrhq/`，
逐文件哈希与原件一致，权限 0600/0700。`acceptance-receipt.json`、任务
`agent_result.json`、`events.jsonl`、进度笔记和草稿可供审查。这证明特定工程
流程跑通；尚未证明原 15 条材料整理的质量、速度改善，或模型不会再次重复读取。

最终实查原 Mission 仍 paused/version 800，无排队或执行任务，剩余 46 模型任务、
12 实验。此次没有重置预算、恢复研究或重新启用自动跟进。

## 用户授权启动后的核验

用户随后明确要求“启动吧”。2026-10-01 北京时间，已在无排队或执行任务的边界
恢复同一个 `body-memory-open-20260929-001`。事件 801 按既有模型故障恢复授权
将剩余从 46/12 补足到 48/12（模型任务总上限从 147 改为 149，实验上限仍 21），
802/803 通知 Curator 和主编排工程修复与增量写作要求，804 恢复 active。
事务内核对全部历史任务记录哈希未变，未创建新 Mission。

后台两项 timer 均 active；collaborators.service 确有执行进程。新 Curator
`task-84756af8768805808756` 已运行，实查 27 个 assistant 响应均来自
`sub2api-reframe/gemini-3.8-flash-high`，其中 7 次 `rsi_read_source` 已完成。
新 schema 2 真实来源目录 44,136 字节，15 条待整理记录、247 份文档；其大小
与前述体积测试夹具不同。检查时尚无草稿或进度文件，不把模型调用视为整理完成。
剩余为 47 模型任务、12 实验，原任务历史状态和失败证据保留。
每小时跟进 `rsi-2` 已重新启用，监测实际产出与模型故障；此次启动没有新增
科研实验结果，也没有干预 zhaohuijin 的独立 Mission。

后续状态核验：原 Mission 自动 paused/version 817，无排队或执行任务，剩余
45 模型任务、12 实验。恢复后的 Curator 共三次失败，均不是 provider 400、余额
403 或上下文超限。首任务 `task-84756af8768805808756` 把一次意图调用
`rsi_read_source` 的文本当作最终回复，没有可接受 JSON。接续任务
`task-dcab977ef7d3e219e483` 确实写出六份 Wiki 草稿、进度与 handoff，最终
`rsi_wiki_check` 的工具及 SQLite 回执成功，但最后文字包含解释、合同示例和
真实 publish 对象，`final_payload` 在 fallback 中取首个可解码对象，误收前面的
`rsi_wiki_check{}` 为 `{}`；随后运行时拒绝缺失的 decision。第三任务
`task-601a259b1e1ed4935142` 也返回解释文本并被解析为 `{}`，自己的 workspace
没有 Wiki 草稿，任务仍失败。事件 816 表示两次自动接续用尽，817 自动暂停。

成功预检的六份草稿还在第二个失败任务的 workspace 和已发布失败通信快照，
并未正式进入项目 Wiki。此问题需修复结果提取与接续逻辑后用原始输出回放
验收，不通过反复补预算来解决；本次状态询问没有恢复 Mission 或改预算。

## 文件提交回执修复

事故中 Curator 已经写出草稿并成功执行 `rsi_wiki_check`，但最后回复同时包含
`rsi_wiki_check{}`、合同示例和最终 JSON。旧解析器取到第一个 `{}`，把有效工作判成
缺少 `decision`。修复后 Curator 使用 `rsi_wiki_submit` 显式提交
`workspace/wiki/<section>/<slug>.md` 路径和摘要。运行时会在工具事务中冻结所选文件、
记录文件哈希、来源事件、任务/Mission/input 版本和 prompt 哈希，并以 SQLite 工具回执作为
完成依据；普通最终文字不再决定成功。`publish` 可以覆盖部分来源，未覆盖记录保留
pending；有 pending 时空 `no_change` 会被拒绝，避免无效循环。已接受提交的进程即使随后
中断，也能从冻结回执恢复，不重新调用模型或扣预算。发布器只读取提交快照，不读取未提交的
workspace 草稿。

隔离真实 Gemini 验收使用事故任务 `task-dcab977ef7d3e219e483` 的原始 finding 草稿
（SHA-256 `09b7ff2d72c0c33ce813f23def5bc080c996fd03d8f7c03a87679fd816e28019`）和三个
归档来源，实际完成读取、`rsi_wiki_check`、`rsi_wiki_submit`；自然语言结尾不是 JSON
仍成功，提交回执重放保持相同 hash，生产 Wiki 未写入。证据归档于服务器
`.rsi/deployment/curator-paging-acceptance/rsi-curator-acceptance-submission-20261001/`。

2026-10-01 随后在原 Mission 的暂停边界部署上述修复，未改旧任务或其他 Mission；部署记录
为 `curator-submit-activation.json`，备份目录为 `.rsi/deployment/backups/curator-submit-20260930T180859Z/`。
原 Mission `body-memory-open-20260929-001` 已恢复到 `active`，剩余预算补足为 48 个模型任务、
12 次实验，历史计数保留。新 Curator `task-dc5434f9b56ec37ce0de` 真实完成并以
`completion_source=curator_submission` 结束，覆盖 15 个来源事件，未重复模型调用；随后主编排
继续运行。之后出现的 Coordinator 合同错误（缺少合法 `schedule/wait/stop`，以及停止重述引用未审查
任务）和一次 Researcher 256k 请求前保护失败均保留为普通执行证据，不能说成科学负结果；新的
Curator 任务仍在处理事件 3597。
