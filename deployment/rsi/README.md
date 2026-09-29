# RSI 运行配置

RSI 以用户提供的 Mission 为目标，记录任务委派、协作消息、交接文件和运行证据。任务领域由 Mission 决定。仓库里的实验协议属于特定项目示例，通用研究任务不需要虚构评测器或测量结果。

## 私有配置

`research.json` 只提供通用默认值。每次调用读取 `~/.config/rsi/research.json` 覆盖；也可以用 `RSI_CONFIG_DIR` 为不同项目选择独立目录。Pi 默认从 PATH 查找，Python 默认使用启动 RSI 的解释器。机器专属路径写入私有配置：

```json
{
  "pi_executable": "/absolute/path/to/pi",
  "python": "/absolute/path/to/python",
  "models": {
    "researcher": {"provider": "my-provider", "model": "my-model"}
  },
  "thinking": "high",
  "runtime_max_parallel_agents": 2,
  "runtime_max_parallel_runs": 1,
  "agent_timeout_seconds": 600
}
```

Pi 供应商保存在 `~/.pi/agent/models.json`。API Key 单独放在私有目录，文件权限为 0600，通过引用读取。不要把实际地址、密钥或账户信息写进仓库。可通过 独立包中的本地工作台 配置供应商及角色路由。

## Mission

复制 [mission.example.json](mission.example.json)，修改任务 ID、目标、背景与完成条件，再登记任务。它是普通资料研究示例，未启用正式实验：

```bash
python -m rsi_system.core.runtime_cli --root . start deployment/rsi/mission.example.json
python -m rsi_system.core.runtime_cli --root . status
python -m rsi_system.core.runtime_cli --root . pause mission-id --reason "review progress"
python -m rsi_system.core.runtime_cli --root . resume mission-id --reason "continue"
python -m rsi_system.core.runtime_cli --root . tick
```

CLI 的 `start` 创建活跃 Mission；工作台的创建入口会先暂停，等待显式运行。CLI 的 `tick` 默认选择最近活跃任务。工作台运行按任务 ID 推进，避免误选其他任务。

通用默认授权允许读材料、研究和写任务笔记；正式候选代码修改与服务器实验需要 Mission 明确授权。后台模型调用需要可用 Pi 和账户配置。核心任务编排和普通 Agent 调用不依赖 Linux；Linux 上的 bubblewrap 只为 researcher/coder 和审计命令提供更强的隔离。本机工作台会单独显示隔离能力是否可用。

## 任务与证据

- `.rsi/runtime/state.sqlite3` 保存 Mission、任务、租约、事件与工具操作。
- `.rsi/runtime/tasks/<task-id>/workspace/` 保存任务笔记及原型。
- `.rsi/runtime/tasks/<task-id>/` 保存冻结输入、输出与交接清单。
- `knowledge/` 保存带状态和来源的知识页面。

`rsi_delegate` 登记子任务，`rsi_message` 发送署名消息，`rsi_wait` 等待实际子任务结果，`rsi_read_handoff` 读取冻结交接文件。没有成功回执的委派不会被当作已执行。知识检索只提供匹配片段，草稿和执行证据不等同于确认结论。

Mission 的 `max_agent_tasks`、`max_runs` 和并发数限制执行范围。失败与取消任务也占任务预算。暂停阻止新领取及旧状态输出接纳，但不保证立即杀死已启动进程。恢复前应检查暂停原因；已有回执的任务按运行时规则恢复，缺失回执不补造结果。

## 可选后台服务

`deployment/rsi/` 中的 systemd 单元和 Secret Service 脚本是 Linux 的可选部署方式。安装前核对本机工作目录、解释器和私有配置，不应直接沿用另一台机器的路径。没有这些服务时，可以直接运行 Python CLI 和看板；定时服务只是持续推进活跃任务的便利方式。

固定实验协议是可选扩展，详见 `rsi_system.core` 的控制面说明。更换领域时需要提供自己的数据、执行入口和评测合同，不能把示例合同当作通用评测器。
