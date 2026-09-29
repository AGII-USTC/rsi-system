# Runner 如何使用已有实验环境

探索 Runner 可以使用管理员配置的 Docker 环境。Researcher 只选环境名称，不需要安装依赖、加载模型服务或操作 Docker。未配置环境的 Mission 继续使用原有主机执行路径；这个接口目前用于探索探针，不自动改变 Candidate 评测器。

## 配置与调用

把 `config/runner-environments.example.json` 中的 `runner_environments` 合并到私有 `research.json`，替换镜像、解释器和只读目录。不要覆盖已有模型、思维强度和凭证配置。镜像须事先安装，运行时不会拉取。宿主需有 Docker 及对应 GPU 支持；RSI 编排本身不要求 Docker。

Mission 的 `resources` 中授权环境：

```json
{
  "runner_environment": "pi05-libero",
  "allowed_runner_environments": ["pi05-libero"],
  "max_seconds_per_run": null
}
```

Researcher 发布 Python 文件后，在原有 Runner 派单的 `experiment` 内加 `"environment": "pi05-libero"`。省略时使用 Mission 默认环境；其他来源文件、问题和对照字段照常提供。可用名称会进入 Agent 上下文。同脚本与输入选择不同环境时，去重会区分它们；同一环境有意重测仍需 `recheck_reason`。

探针从 `RSI_EXPLORATION_INPUTS` 读取已发布输入，将产物写入 `RSI_EXPLORATION_OUTPUT`。只挂载项目源码不会自动把文件加入已发布、冻结的研究输入；正式依据仍需有版本和内容哈希。

## pi0.5 服务

管理员可在环境配置内添加 `policy`。运行时先启动独立模型容器，再启动探针；探针通过 `RSI_POLICY_HOST` 与 `RSI_POLICY_PORT` 调用服务。两个容器使用本次 Run 专属内部网络，不对宿主发布端口。无 `policy` 的模拟器环境完全禁用网络。

提供的 `deployment/rsi/pi05_policy_service.py` 直接读取只读 checkpoint，不复制权重、不下载模型、不修改参数。请求遵循 OpenPI LIBERO 的图像、state、prompt 格式，另须显式提供有限的 `float32[10,32]` 数组 `harness_flow_noise`，以便记录和重现推理噪声。它返回 pi0.5 动作；成功推理不代表任务完成或适应有效。

服务器应分别配置 `libero-sim`（只需模拟器）和 `pi05-libero`（需要真实模型）。研究员根据自己的问题选择。GPU 被其他任务占满时，应先协调资源；不要反复派研究任务来探测同一个环境故障。

## 保护、恢复与证据

- 容器使用非 root 身份、只读根目录及数据挂载，只开放本 Run 的输出目录和临时目录；不提供 Docker socket、主机网络或模型凭证。
- 冻结输入保存实际镜像 SHA、环境配置、挂载位置及小文件哈希。大文件和目录标记为实时只读来源，**只读不等于内容已冻结**；外部源码、权重版本仍需在研究协议中固定并核查。
- 回执记录环境、执行结果、输出和策略服务日志哈希。原失败与旧回执保留，不通过改写输入重跑旧任务。
- `max_seconds_per_run: null` 不设总执行时限。模型加载也没有另加总时限；短连接检查仍有超时。取消、暂停或租约失效会中止执行并清理本 Run 的容器与网络。进程崩溃后按已有 Runner 接续规则处理，不盲目重放。
- 清理失败写入回执并暂停 Mission，先处理残留再继续。日志、产物大小和并发限制仍有效。

验收需分别检查：模拟器初始化与动作执行、真实模型加载与推理、返回动作进入仿真、只读限制、容器清理和回执。基础设施验收使用独立测试账本，不记作研究结果，也不占生产 Mission 的科研额度。
