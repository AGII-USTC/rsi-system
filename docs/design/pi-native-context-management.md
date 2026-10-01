# Pi 原生上下文管理与 RSI 的边界

2026-10-01。用户要求模型上下文配置为 256000 token，Agent 与 Runner 不设总执行时限。上下文估算、输出预算调整、自动压缩和溢出恢复交给 Pi；RSI 不再用 JSON 字节数模拟 token 数并提前结束任务。

## 配置放在哪里

Pi 的私有 `~/.pi/agent/models.json` 为各模型配置 `contextWindow: 256000`。`maxTokens` 是供应商支持的输出上限，不是每次请求固定预留的空间，也不是思维强度。供应商、模型名、密钥引用和输出上限由管理员的私有配置决定，不嵌入研究核心代码。

Pi 的 `~/.pi/agent/settings.json` 管理原生压缩。已核对 Pi 0.87.1 的原生默认值：

```json
{
  "compaction": {
    "enabled": true,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000
  }
}
```

省略该配置时使用 Pi 默认值。RSI 不叠加第二套压缩阈值，不修改 Pi 生成的请求输出预算。模型窗口是给 Pi 的配置上限；token 估算及供应商返回溢出错误后的处理仍由 Pi 完成。思维强度、任务并发和 Mission 预算分别配置。

## 删除与保留

- 删除将 UTF-8 字节视为 token 的启动拦截、会话删减和压缩请求拦截。
- 删除 RSI 对供应商请求强制填入固定输出 token 上限的处理。
- 保留模型窗口配置核验，以及不改变请求的诊断记录。旧回执中的字节估算不再用于新请求准入。
- 原生 `read`、`bash` 等工具结果及 `rsi_read_source` 分页不再被 RSI 二次截成前缀。Pi 负责原生工具截断和会话管理，RSI 只给非分页的自定义工具大结果保存来源。
- 删除累计事件日志超过 16 MB 就终止 Agent 的隐藏限制。事件解析和哈希计算改为流式读取，Curator 已提交结果的恢复也不必把全部事件读入内存。
- 保留研究角色权限、只读权重和历史证据、Mission 预算和并发、任务提交协议及证据校验。这些是 RSI 的研究业务责任。
- 保留来源文件、哈希和 `rsi_read_source` 分页。任务可以按需要读材料，不必把整个研究历史装入首条提示。来源分页不应取代 Pi 的会话压缩。

## Gemini：先区分所用接口

用户提供的 [Antigravity Agent 官方文档](https://ai.google.dev/gemini-api/docs/antigravity-agent?hl=zh-cn) 描述 Google 托管的 `antigravity-preview-09-2026`，通过 Interactions API 调用，在 Google 的环境中运行 Agent。文档说明它自带工具循环和自动压缩，通过 `agent_config` 控制 Agent 配置；函数调用续接使用 `previous_interaction_id`。文档还列出该托管接口不支持 `temperature`、`top_p`、`top_k`、`stop_sequences`、`max_output_tokens`。

当前研究部署是本机 Pi 经 Sub2API 的 `anthropic-messages` 兼容接口调用 Gemini 模型。Pi 执行工具循环及压缩，模型请求使用该兼容协议。不能因为代理地址中有 `antigravity`，就把它当成 Google 的托管 Agent API，也不能把上述托管接口限制直接套到 Anthropic 的 `max_tokens`。

当前修复继续使用已有兼容接口和角色路由。若以后接入托管 Agent，应单独实现其有状态交接和工具回执，不在现有请求中混入 `agent_config` 或 `previous_interaction_id`。搜索与函数调用的兼容开关保持按角色或任务可选，不全局关闭搜索。

## 验收依据

已用安装的 Pi 0.87.1 SDK 做离线验收：使用 `--no-session` 对应的 `SessionManager.inMemory`、默认压缩设置和 256000 模型窗口，本地假供应商返回的 usage 触发原生 threshold 压缩；一次摘要完成后能继续下一轮，没有 session 文件和外部网络请求。两份原始失败会话也已回放，旧实现拒绝，新实现保持请求不变，源文件哈希不变。

提交校验、只读权限和来源分页仍需回归。服务器部署要更新实际服务入口，并保存旧任务输入、失败记录及回执。离线假供应商验收不代表真实模型服务已经通过。

验收应区分离线 SDK 测试、真实模型响应和研究实验结果。运行状态或工具读取成功都不能当作 pi0.5/LIBERO 的科学收益。实际部署与接续证据记录在 [本次研究审查](../reviews/body-harness-resume-20260929.md)。

原生机制参考：[Pi compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)、[Pi settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)、[Pi models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)。本轮同时检查已安装 Pi 0.87.1 源码；未来升级应重新核对，不把这些默认值当成 RSI 自己的实现。
