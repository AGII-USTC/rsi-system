# RSI 自主管理 pi0.5 实验部署

本说明针对 kangrenlab 的 `body-memory-open-20260929-001`。模型权重不改动，
研究 Agent 可以编写、修改、运行自己的策略接入和仿真代码。主编排负责派发研究责任，
Researcher 决定方法与验证，Coder 实现，Runner 执行并返回真实回执。

## 环境与路径

`pi05-self-managed` 是 GPU Runner 环境，没有额外常驻策略进程。
不要同时加载不需要的第二份模型。Python 为 `/.venv/bin/python`。

| 容器路径 | 内容 |
| --- | --- |
| `/app` | OpenPI 源码及 LIBERO，不是 Python 打包安装目录 |
| `/openpi-cache/openpi-assets/checkpoints/pi05_libero` | 现有 pi0.5 checkpoint，params 与 assets 都要核对 |
| `/rsi-memory` | 既有 Memory 实现，包括 `latent_policy.py` |
| `/rsi-artifacts` | 既有 adapter、report、请求样本与结果 |
| `/rsi-bindings.json` | 五项绑定的内容清单 |
| `/rsi-deployment` | 当前接入模板、绑定校验工具和工程验收脚本 |
| `/libero-config` | 已配置的 LIBERO 数据地址 |

以上是原件的只读挂载。Agent 的部署代码、配置与新产物不是只读：

- `communications.workspace`：使用 write/edit/bash 编写和检查 loader、服务、驱动、部署说明。
- `RSI_EXPLORATION_INPUTS`：Runner 收到的已发布代码和支持文件，冻结输入。
- `RSI_EXPLORATION_OUTPUT`：Runner 可写目录，可保存修订的代码、服务配置、日志、轨迹和回执。
- `/tmp`：容器内可写临时目录，不保证跨 Run 保留。

Researcher/Coder 可将所需的参考源码复制到自己的工作区改动。不要复制权重。
如果改动 OpenPI/Memory 实现，需要记录新源码哈希并重新核验，不能继续声称使用原绑定清单。
原始 adapter 是历史 response-contrast 产物，不是最新 pairedV1/V2 训练的权重。

## 自己实现、启动和调试

Coder 的 Linux 工作区 shell 可以直接使用宿主机 NVIDIA GPU 做开发测试，
无需先完成任务或等待 Runner。使用已安装且支持 CUDA 的 Python 环境，
把代码、缓存和测试产物写入自己的工作区；权重原件仍只读。
加载前检查显存与在途实验，结束后清理自己启动的进程，不停止其他任务释放资源。
GPU 开发测试保留日志和来源，但不自动成为受控实验的科学结果。
需要容器内 OpenPI/LIBERO 依赖和冻结输入回执时继续使用 Runner；
宿主机 GPU 权限不代表宿主机拥有容器内的 Python 依赖。

先在任务工作区编写 Python 入口和支持模块，完成发布，再使用返回的 `file_id`
派给 Runner，`experiment.environment` 选择 `pi05-self-managed`。
支持文件通过 `RSI_EXPLORATION_INPUTS` 读取；不要找其他任务的私有工作区。

Runner 中可以直接调用 `load_bound_policy`，也可以从自己发布的服务代码启动本地
子进程、访问容器内 localhost、检查 stderr 并在 finally 中终止自己启动的服务。
用 `sys.executable` 启动同一环境的 Python。进程不需要宿主机 Docker 权限。
环境不联网，依赖已安装；缺失包或镜像应返回确切错误及所需环境变更。

下面是可改写的工程验收调用，不是研究算法要求：

```python
import os
from pathlib import Path
import subprocess
import sys

output = Path(os.environ['RSI_EXPLORATION_OUTPUT'])
subprocess.run([
    sys.executable, '/rsi-deployment/pi05_binding_acceptance.py',
    '--manifest', '/rsi-bindings.json',
    '--checkpoint-root', '/openpi-cache/openpi-assets/checkpoints/pi05_libero',
    '--openpi-root', '/app', '--memory-root', '/rsi-memory',
    '--artifacts-root', '/rsi-artifacts',
    '--output', str(output / 'binding-acceptance'), '--simulator-smoke',
], check=True)
```

入口的 Pythonpath 包含 `/app`、`/app/src`、`/app/packages/openpi-client/src`、
`/app/third_party/libero`、`/rsi-memory`、`/rsi-deployment`。与自己的模块并用时，
在实际进程中记录加载路径，避免误导入其他安装版本。

固定噪声请求需 `harness_flow_noise: float32[10,32]`。
策略返回的是转换后的物理动作 `[10,7]`，不是内部 32 维动作。
Memory 请求还需 `memory_tokens` 和 `memory_mask`；详细契约看实际 loader 与请求样本。
官方、trained-memory、zero-gate 的加载入口位于 `pi05_policy_service.py`。
JAX 编译可能捕获参数；零门控必须在首次推理前设置，不能仅在已编译实例上改 alpha
就声称做了零门控实验。提供的验收入口为各比较臂重新加载模型。

## 故障与证据

遇错误读取本 Run 日志，修改自己的部署代码，重新发布并按 Runner 回执接续。
不要改历史失败、输入、权重、评估器或控制面配置来让检查通过。
主编排应拿到确切错误和可读文件索引，不必等操作员代写所有实验代码。
无总执行时限；不要给模型加载或仿真增加旧的 120/180/900 秒总超时。

此前 `run-b6192a579f3f8dec6476eb346068/policy.log` 的失败是显存耗尽，
不是 Memory 科学负结果。用户已授权停止 Qwen 和 red-object 服务，不能自行重启它们。

内容清单只能证明当前本地文件身份，不能认证上游官方校验和。
五项绑定要靠真实加载、模型响应、策略身份和 Runner 证据解除；模板和单元测试不够。
两步 LIBERO smoke 只验证接口链路。历史信息是否改善适应，需要 Researcher 自行设计
并执行真实冻结 pi0.5/LIBERO 闭环对照，保留初态、随机性、交互预算及原始结果。
