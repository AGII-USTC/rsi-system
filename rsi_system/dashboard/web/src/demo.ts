import type { Event, Snapshot, Task } from "./types";

// The first eight stages are the original introduction. Profession-specific
// roles join only after the initial delegation has settled so the opening
// choreography and its event IDs remain unchanged.
const STAGE_SECONDS = [0, 4, 10, 16, 23, 29, 34, 39, 46, 53, 60] as const;

export function demoStageAt(elapsedSeconds: number): number {
  const elapsed = Number.isFinite(elapsedSeconds) ? Math.max(0, elapsedSeconds) : 0;
  for (let stage = STAGE_SECONDS.length - 1; stage > 0; stage--) if (elapsed >= STAGE_SECONDS[stage]) return stage;
  return 0;
}

export function demoSnapshot(objective = "探索一个值得研究的问题", elapsedSeconds = 0, startedAt = Date.now()): Snapshot {
  const stage = demoStageAt(elapsedSeconds);
  const time = (seconds: number) => new Date(startedAt + seconds * 1000).toISOString();
  const makeTask = (id: string, role: string, parent: string | null, goal: string, summary: string, created: number): Task => ({
    task_id: id, role, status: "running", objective: goal, summary, parent_task_id: parent, waiting: role === "coordinator",
    reason: null, candidate_id: null, error: null, reply_to_task_id: null, idea_id: null,
    created_at: time(created), updated_at: time(created),
  });
  const plan: { second: number; task: Task; title: string }[] = [
    { second: 0, task: makeTask("demo-main", "coordinator", null, "协调研究与验证", "正在安排研究方向", 0), title: "研究开始" },
    { second: 4, task: makeTask("demo-research-a", "researcher", "demo-main", `收集与“${objective}”有关的公开资料`, "核对来源与事实", 4), title: "资料方向委派" },
    { second: 10, task: makeTask("demo-research-b", "researcher", "demo-main", "比较可行路线与约束", "整理候选路径", 10), title: "方案方向委派" },
    { second: 16, task: makeTask("demo-research-c", "researcher", "demo-main", "审视假设与证据缺口", "检查潜在风险", 16), title: "审查方向委派" },
    { second: 23, task: makeTask("demo-runner", "runner", "demo-research-a", "交叉核对公开来源", "比对资料记录", 23), title: "资料核对委派" },
    { second: 29, task: makeTask("demo-hypothesis", "hypothesis", "demo-research-b", "形成可检验的假设", "列出验证步骤", 29), title: "假设研究委派" },
    { second: 34, task: makeTask("demo-critic", "critic", "demo-research-c", "寻找关键前提的反例", "独立检查薄弱环节", 34), title: "独立审查委派" },
    { second: 39, task: makeTask("demo-coder", "coder", "demo-hypothesis", "编写最小验证程序", "实现验证脚本", 39), title: "验证程序委派" },
    { second: 46, task: makeTask("demo-reviewer", "reviewer", "demo-research-a", "审阅交叉核对结果", "整理审查意见与证据", 46), title: "结果评审委派" },
    { second: 53, task: makeTask("demo-analyzer", "analyzer", "demo-research-c", "分析反例与证据缺口", "汇总风险与结论", 53), title: "证据分析委派" },
    { second: 60, task: makeTask("demo-student", "student", "demo-reviewer", "记录可复用的研究笔记", "整理学习笔记", 60), title: "研究笔记委派" },
  ];
  const tasks = plan.filter((entry) => entry.second <= STAGE_SECONDS[stage]).map((entry) => entry.task);
  const roles = new Map(plan.map((entry) => [entry.task.task_id, entry.task.role]));
  const events: Event[] = plan.slice(1).filter((entry) => entry.second <= STAGE_SECONDS[stage]).map((entry, index) => ({
    event_id: index + 2, kind: "task_created", title: entry.title, text: entry.task.objective,
    from_task_id: entry.task.parent_task_id, to_task_id: entry.task.task_id, task_id: entry.task.task_id,
    from_role: roles.get(entry.task.parent_task_id!) ?? null, to_role: entry.task.role, created_at: time(entry.second),
  })).reverse();
  events.push({ event_id: 1, kind: "mission_started", title: "研究开始", text: objective, task_id: null, from_task_id: null, to_task_id: null, from_role: null, to_role: null, created_at: time(0) });
  const mission = { mission_id: "demo-collaboration", status: "active", objective, version: stage + 1, created_at: time(0), updated_at: time(STAGE_SECONDS[stage]) };
  return { missions: [mission], mission, tasks, events, runs: [], updated_at: time(STAGE_SECONDS[stage]) };
}
