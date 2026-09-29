# 任务恢复与 Runner 接续验收

2026-09-29。本次先修改本地独立仓库，再同步到 kangrenlab 的 `/home/kang/embodied-rsi`。没有恢复旧 Mission、提高预算、更换模型或启动新的科学实验。

## 已修改

- 模型临时失败：最多默认追加 2 次关联重试，延迟 30、60 秒；次数和等待时间来自配置。重试继续消耗 Mission 与每日调用预算。普通回复不会被当作恢复任务继承模型；明确恢复任务保留原模型、思维强度、任务类型、研究节点、父任务和评审范围。
- 租约：执行期间通过独立连接续期，不覆盖暂停、停止或已替换的租约。仍在执行的过期任务不会挡住其他有并发额度的分支。恢复次数耗尽且没有其他待办时，保留失败并暂停。
- Runner：执行前登记 provisional Run；探索 Runner 在启动子进程前写入持久启动标记。未启动的同一任务可以继续；已完成的回执经输入和产物哈希校验后直接导入；活进程等待；启动状态不确定时记录失败，不自动重放。Candidate Runner 继续使用原有独立实验账本和 worker 接续机制。
- Run 终态不能被另一种状态或不同证据覆盖；provisional Run 不进入评审证据包，也不算实验成功。任务自己的 Run 不会被去重规则误挡成另一次实验。
- 主编排回复修正：无效的 challenge acknowledgement 允许一次受限纠正；只有读取工具，不开放派单、写文件、检索或 shell。纠正后由下一次正常主编排决策继续研究。运输错误重试仍受统一上限约束。
- 研究续接：省略 continuation 或明确 `{requested:false}` 不创建空任务；明确依赖必须结束后才领取，失败和取消结果也可供研究员继续分析。不会根据 objective 的自然语言猜测是否要派单。
- resume 保留失败状态和原始错误，不再把所有失败改为取消。

## 验证

1. 本地完整回归：414 passed、13 skipped（平台或可选依赖条件）。之后追加普通回复与受限纠正链隔离测试，相关 5 项通过。
2. Linux 独立测试目录：427 passed。首次误用系统旧 Node，16 项失败；切换到正式 Pi 服务使用的 Node 22 后全量通过，保留首次失败日志。未使用生产模型服务或生产 Mission 预算。
3. 最终已部署文件针对性验收：49 passed，涵盖真实 Python 子进程、启动前中断、回执写完但数据库未提交、禁止重复启动、预算/退避、模型策略保留、受限回复修正、依赖等待及只读证据边界。
4. 数据库迁移先在生产副本执行，21 张历史表的原有列内容逐项比较未变化；561 个任务、68 个 Run 均保留，SQLite integrity_check 为 ok。部署后再次与备份比较，历史表内容未变化。
5. 文件同步前后校验 SHA256；只替换本轮修改的源码和测试，公共配置仅补齐恢复参数。保留服务器既有模型与私有配置。四个定时服务恢复启用，Qwen 的 18082 端口仍关闭。

## 可核查位置

- 本地：`rsi_system/core/runtime.py`、`runtime_recovery.py`、`runtime_lease.py`、`runtime_decision_repair.py`、`runtime_runner_durability.py`、`runtime_exploration.py`、`runtime_store.py`。
- 使用与配置说明：`USER_GUIDE_CN.md`、`config/research.example.json`。
- 本地回归日志：`/tmp/rsi-recovery-final-local.log`。
- 服务器全量验收：`/tmp/rsi-recovery-linux-final.log`。
- 部署后验收：`/tmp/rsi-recovery-installed-final.log`。
- 首次完整备份：`/home/kang/embodied-rsi/.rsi/backups/recovery-20260929T135003Z/`。
- 最后两文件修正备份：`/home/kang/embodied-rsi/.rsi/backups/recovery-20260929T135300Z/`。

## 当前边界

原 Mission `body-memory-open-20260929-001` 仍暂停，原预算 48 个模型任务、12 个 Run 未增加。已结束的历史失败不会被本次部署自动改写为成功。本次证明的是恢复与执行记录的工程行为，没有证明 memory 方法有效，也没有运行新的 pi0.5 GPU 实验。

本轮编码子 Agent 出现 HTTP 403 / insufficient balance，剩余修改由主 Agent 接手完成；没有据此断言服务器科研接口已恢复可用。
