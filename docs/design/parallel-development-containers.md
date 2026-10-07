# 并行研究与开发容器

Researcher 可以同时委托多个 Coder，分别实现不同假设、架构或消融。
每个任务拥有独立可写目录、开发容器和缓存；注册 Candidate 的 Coder 还拥有
独立 Git worktree。依赖使用已配置并允许的 Docker 镜像，不复制模型权重。

## 开发循环

1. 在自己的 `communications.workspace` 写实现、配置和自测脚本。
2. 调用 `rsi_dev_exec`，使用实际容器内的依赖测试，直接获得退出码、日志与回执。
3. 根据错误修改自己的文件，再用新调用测试；不必每次先结束 Coder、派 Runner。
4. 完成后发布代码和实现说明。Researcher 按 `file_id` 读取与选择结果，再派受控 Run。

例子：

```json
{
  "command": "\"$RSI_DEV_PYTHON\" -m pytest tests -q",
  "environment": "pi05-self-managed",
  "gpu": false
}
```

容器的 `/work` 对应本任务的可写工作区；注册 Candidate 时 `/code` 对应本任务的
可写源码 checkout。`$RSI_DEV_PYTHON` 为所选镜像的 Python。原有 `/app`、
checkpoint、数据及绑定文件继续按环境配置只读挂载。需要改参考源码时复制源码到
自己的工作区，记录来源与变更；不要复制权重。

容器有自己的进程、临时目录、网络命名空间，多个任务都可使用本地端口 8012。
不暴露宿主机端口，不自动启动共享 policy 服务；任务可在自己的命令中启动、调试
和清理本地子进程。容器没有外网，联网文献检索仍使用 Agent 的现有工具。

## 并行与 GPU

CPU 开发测试可并行，沿用 Mission 的协作者并发数，不按 Coder 角色限制为一个。
不同实验同时修改同名 `probe.py` 不会互相覆盖。

`gpu=true` 为显式选择。容器开发测试与 Docker 正式 Run 共享服务器仓库级 GPU 锁，
跨 worker、跨该仓库的 Mission 协调；锁在正式 Run 清理完容器后才释放。
GPU 忙时开发工具返回 `resource_busy`，没有启动命令，也不消耗一次正式 Run。
等在途任务结束再重试，CPU 检查可继续。正式 Run 等锁时继续维护任务租约。
没有新增命令总时限或模型/上下文限制。

这把锁协调 RSI 管理的 Docker 任务；既有宿主机 GPU shell 和其他程序不会自动参与。
需要 GPU 开发时优先使用该工具，不停止其他任务释放显存。GPU 容器带资源所有者
标签；异常退出遗留的容器会阻止新的 GPU 分配，须核验原任务、保留日志后清理。

## 回执与清理

开发请求与控制面回执保存在 `.rsi/runtime/tasks/<task_id>/development/<call_hash>/`。
完整 stdout、stderr 及可读回执位于本任务工作区的 `dev-tests/<call_hash>/`。
工具仅返回日志尾部，完整日志仍保留；自己的测试也可直接写持久进度和结果。

相同调用 ID 重放已保存的回执，不重复执行。结果不确定的旧请求会保留为中断，
清理它自己的容器，并要求新调用 ID，不自动再次执行可能有副作用的命令。
正常结束、命令失败和租约/任务取消时都清理本次容器。
无 Docker socket、宿主机凭据或其他任务的可写目录挂载。

开发回执明确标为诊断。它可以帮助排错和决定研究，但不会自动计为已完成的
受控实验或科学增益；正式结论仍需固定代码、输入与环境身份并审查真实结果。

## 同一 Candidate 的并行实现

每名 Coder 在启动时固定 Candidate 的当时提交，建立以任务 ID 命名的独立分支。
提交前执行原有代码边界检查与测试；发布时在同一数据库事务中检查 Candidate
提交是否仍为自己的起点。

若另一名 Coder 已更新 Candidate，新分支保留、任务正常回传
`candidate_publication.status=requires_reconciliation`，并记录 `candidate_branch_ready`。
Researcher 可以选择一个版本，或派新的整合 Coder 从当前版本取所需改动。
不会自动合并科学方案，也不会用最后完成者覆盖先前实现。

## 部署

不需要新建常驻策略服务。使用既有 `runner_environments` 和 Mission 的
`allowed_runner_environments`，运行时为每次开发调用创建并销毁独立容器。
镜像由管理员配置并固定到本地 SHA-256 image ID；Agent 无法指定任意镜像或挂载。
新启动的 Researcher/Coder 会收到工具和精确路径提示。已有 Pi 会话需自然完成后
在新 invocation 加载工具，不为启用功能打断正常研究。

## 开源实现参考

2026-10-07 直接检查源码，下面的链接固定到当时提交，避免仅凭项目介绍推断实现。

| 项目 | 已检查的做法 | 本系统采用方式 |
| --- | --- | --- |
| [OpenCode worktree](https://github.com/anomalyco/opencode/blob/ecc4916b5a9608c30e6dd58a67f2137b594407ca/packages/opencode/src/worktree/index.ts) | 创建前检查目录及分支名，独立分支和 checkout，创建失败显式返回错误 | 用任务身份固定私有分支与起点；重试复用任务清单；发布检查 Candidate 起点，保留冲突分支 |
| [OpenHands SDK DockerWorkspace](https://github.com/OpenHands/software-agent-sdk/blob/608a102c637d8d8a999f49d7b04846524bd8bd1c/openhands-workspace/openhands/workspace/docker/workspace.py) | 每个工作区创建唯一容器，显式挂载，管理启动、日志和销毁，按需 GPU | 复用允许的固定镜像；每次测试独立容器与命名空间，自己的文件跨调用保留，日志与回执持久化 |
| [SWE-agent environment](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/environment/swe_env.py) / [SWE-ReX DockerDeployment](https://github.com/SWE-agent/SWE-ReX/blob/5c995c365dfb1fd5bc56fda688be5d8538f9931f/src/swerex/deployment/docker.py) | 代码库起始提交、执行环境和会话分层；唯一容器名，启动失败及关闭时清理 | 代码身份、开发测试、正式 Run 分开记录；相同调用回放回执；不确定请求保留中断证据，不默默再执行 |

参考的是工作区隔离与生命周期管理，不是把这些项目接入 RSI。
RSI 的开发容器不运行模型 Agent 服务，不需要转发模型密钥或公开服务端口；
联网检索仍留在 Agent 层。注册 Candidate 使用 Git worktree，探索型代码使用已存在的
任务独立目录及发布快照，两者都不会共同修改一份可写实验源码。
GPU 开发与正式 Run 使用额外的共享资源锁；上述源码参考不构成本系统 GPU 并发验收。
