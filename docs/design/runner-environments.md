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

## 官方权重与 Memory 绑定

`deployment/rsi/pi05_bindings.py` 为已有安装生成内容清单。它流式读取并哈希
checkpoint 的全部 `params`、`assets`（含 norm stats），OpenPI Python 源码、
Memory loader，以及指定 adapter、训练报告和真实请求。只读取权重，不复制。
清单保存根目录内的相对路径，因此同一身份可以在宿主机与容器内核验。
该身份固定当前本地文件，不证明这些文件与上游官方发布逐字节相同。

```bash
python deployment/rsi/pi05_bindings.py build \
  --manifest /srv/rsi/bindings/reader-v1.json \
  --checkpoint-root /srv/pi05/cache/openpi-assets/checkpoints/pi05_libero \
  --openpi-root /srv/pi05/openpi \
  --memory-root /srv/pi05/research/experiments/pi05_memory \
  --artifacts-root /srv/pi05/eval \
  --adapter /srv/pi05/eval/reader/adapter.npz \
  --report /srv/pi05/eval/reader/report.json \
  --request /srv/pi05/eval/collection/chunks/request.npz
```

生成位置必须在数据根目录外；同名清单不会被覆盖。验证时使用 `verify` 操作，
保留 `--manifest` 和四个 root 参数，换成实际容器路径即可。

管理员在需要这条路线的 Runner profile 中增加只读映射：

| 宿主文件或目录 | 容器位置 |
| --- | --- |
| `deployment/rsi/pi05_bindings.py` | `/pi05_bindings.py` |
| `deployment/rsi/pi05_policy_service.py` | `/rsi-policy-service.py` |
| 当前内容清单 | `/rsi-bindings.json` |
| Memory 源码目录 | `/rsi-memory` |
| 选定产物所在 eval 目录 | `/rsi-artifacts` |

原 `/app` 和 `/openpi-cache` 映射保留。策略启动命令可追加：

```text
--binding-manifest /rsi-bindings.json --openpi-root /app
--memory-root /rsi-memory --artifact-root /rsi-artifacts --mode memory
```

`--mode official` 加载官方无 reader 路线；`memory` 按绑定训练报告加载已保存
reader；`zero-gate` 加载同一 reader 后只把其 alpha 置零。训练位置目前支持
output 和 velocity。请求仍使用现有 WebSocket/固定噪声格式，没有新增隐式
模型选择字段。握手元数据 `rsi_binding` 包含清单身份、模式、实际加载模块
路径及内容哈希，供 Runner 写入回执。源码不符时应明确做当前源码重新验收，
不能声称复现旧源码。

需要同一进程内比较多个模式的探针可导入挂载的服务模块并调用
`load_bound_policy(manifest_path, roots, mode=...)`，返回 `(wrapper, metadata)`。
管理员提供没有常驻 `policy` 的 profile，可避免探针加载权重时又启动一份
无关策略。输出仍是经过 Policy 输出变换的物理动作；没有采集内部 chunk 时
不能声称已测得内部 32 维 chunk。

单元测试验证内容篡改、挂载重映射、实际导入路径和模式校验。
部署验收还必须实际加载绑定权重与 reader、发送固定噪声请求，并将返回动作
送入 LIBERO。单元测试或元数据握手不能替代这一步。

## 保护、恢复与证据

- 容器使用非 root 身份、只读根目录及数据挂载，只开放本 Run 的输出目录和临时目录；不提供 Docker socket、主机网络或模型凭证。
- 冻结输入保存实际镜像 SHA、环境配置、挂载位置及小文件哈希。大文件和目录标记为实时只读来源，**只读不等于内容已冻结**；外部源码、权重版本仍需在研究协议中固定并核查。
- 回执记录环境、执行结果、输出和策略服务日志哈希。原失败与旧回执保留，不通过改写输入重跑旧任务。
- `max_seconds_per_run: null` 不设总执行时限。模型加载也没有另加总时限；短连接检查仍有超时。取消、暂停或租约失效会中止执行并清理本 Run 的容器与网络。进程崩溃后按已有 Runner 接续规则处理，不盲目重放。
- 清理失败写入回执并暂停 Mission，先处理残留再继续。日志、产物大小和并发限制仍有效。

验收需分别检查：模拟器初始化与动作执行、真实模型加载与推理、返回动作进入仿真、只读限制、容器清理和回执。基础设施验收使用独立测试账本，不记作研究结果，也不占生产 Mission 的科研额度。
