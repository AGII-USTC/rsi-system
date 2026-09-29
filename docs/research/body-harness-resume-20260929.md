# 本轮研究续接指令

日期：2026-09-29。用户认可以下方向并要求启动研究。这是对原 Mission 的研究方向澄清，不重置预算，也不修改历史实验结果。

## 总问题

在固定 pi0.5 基座参数的前提下，研究 VLA 的 Harness 侧机制：如何利用交互中积累的经验，让策略在身体或动作响应发生变化后，用更少的交互恢复任务表现。

## 组织方式

主编排先理解系统目标、既有证据与未知，建立层次化研究蓝图。可按整体机制、模块职责与接口、底层算法与表示、实现和验证等层次拆分，但不要求固定结构。派单明确研究责任、来源事实、接口和必要约束；具体算法由 Researcher 形成。Researcher 可以递归派发子 Researcher，形成可检验假设后派 Coder，收到结果后解释并继续迭代。简单问题直接验证，不为了层级增派 Agent。

分支可以长期独立。主编排根据证据决定是否及何时交流、组合或联合研究；不强制合并、不预设赢家。底层发现可以改变上层问题定义。主编排的猜测不应在派单中变成硬约束。审计结论和探索新想法在没有真实依赖时可以并行。

## Wiki 与既有工作

Researcher 主动检索 Wiki，先读摘要，按需沿来源地址查阅代码、数据和交接。核对条件、版本、草稿与证据状态；可以挑战历史判断，旧页面的后续建议不是新的强制流程。新发现保存到自己的来源关联文档，由 Curator 整理。

入口：
- docs/research/body-memory-research-handoff-20260927.md
- docs/design/hierarchical-research-responsibilities-20260929.md
- knowledge/index.md
- knowledge/findings/pi05-memory-first-cycle.md
- knowledge/queries/runtime-body-state-transfer.md
- knowledge/queries/runtime-ordered-history-alternative.md
- knowledge/queries/runtime-phase-feedback-control.md

这三个既有方向仅供接续、挑战或重新组合，不要求逐一完成。请检查先前被基础设施中断的工作，按信息价值决定恢复哪些，不照抄已取消的具体派单。

## 资源与真实验证

服务器项目 /home/kang/pi05-libero；研究代码 /home/kang/pi05-libero/research/experiments/pi05_memory/；模型 /home/kang/pi05-libero/cache/openpi-assets/checkpoints/pi05_libero/；历史结果 /home/kang/pi05-libero/eval/。不复制大模型，不覆盖原数据或权重。RSI 工作区与正式实验由运行时提供，不因这些路径而获得额外写权限。

启动前已只读确认上述路径存在，Docker 可访问，已有 openpi_server:latest 与 openpi_libero_eval:latest 镜像，GPU 有空余。这只证明资源存在，不证明实验链路已经通过。探索 Runner 默认 CPU 隔离；真实 GPU/服务实验必须走已支持的受控协议，并确认协议与当前研究代码、模型和输入匹配。不要将无法访问 GPU 的 CPU 探针包装成 pi0.5 任务效果。

本轮应争取真实模型和仿真中的可复现反馈；先确认可执行路径，再用有依据的原型检验。不要因工具困难而悄悄把总问题替换为更容易的合成预测问题。若现有协议无法满足，应记录具体缺口并向主编排说明，不绕过隔离或自称完成验证。

记录恢复任务表现所需交互、任务成功率、稳定性及额外成本。预测误差只是诊断。没有执行的实验、无 Memory 基线或匹配条件，不得宣称身体适应收益。真实机器人不在授权范围。

## 本轮预算与模型

沿用原 Mission 总上限 48 个模型任务、12 次实验、每次 180 秒，并发 3 个协作者、1 个 Runner；已消费任务不退款。模型从服务器私有配置读取：主编排及 problem_reframe 为 gpt-6-astra/high；普通 Researcher 为 gpt-6-sol/xhigh；其他模型角色为 gpt-6-sol/high。不自动提高预算或更换模型。
