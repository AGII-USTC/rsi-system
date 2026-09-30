export type TaskStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | string;

export interface Mission {
  mission_id: string;
  status: string;
  objective: string;
  created_at: string;
  updated_at: string;
  version: number;
  brief?: { background?: string; known_evidence?: string[]; stop_conditions?: string[] };
  resources?: { max_agent_tasks?: number; max_runs?: number; max_seconds_per_run?: number };
}

export interface Task {
  task_id: string;
  role: string;
  status: TaskStatus;
  objective: string;
  reason: string | null;
  candidate_id: string | null;
  error: string | null;
  summary: string;
  parent_task_id: string | null;
  reply_to_task_id: string | null;
  idea_id: string | null;
  waiting: boolean;
  created_at: string;
  updated_at: string;
}

export interface Event {
  event_id: number;
  kind: string;
  title: string;
  text: string;
  task_id: string | null;
  from_task_id: string | null;
  to_task_id: string | null;
  from_role: string | null;
  to_role: string | null;
  created_at: string;
  knowledge_action?: "read" | "write" | "archive";
  knowledge_files?: { path?: string }[];
  evidence_status?: string;
}

export interface Run {
  run_id: string;
  status: string;
  kind: string;
  candidate_id: string | null;
  idea_ref: string | null;
  created_at: string;
}

export interface Snapshot {
  missions: Mission[];
  mission: Mission | null;
  tasks: Task[];
  events: Event[];
  runs: Run[];
  counts?: { missions: number; tasks: number; events: number; runs: number };
  truncated?: { missions: boolean; tasks: boolean; events: boolean; runs: boolean };
  updated_at: string;
}

export interface ResearchNode {
  node_id: string;
  revision: number;
  question: string;
  scope: string;
  summary: string;
  owner: string;
  execution_status: string;
  finding_status: string;
  finding_status_basis?: string;
  run_refs: string[];
  created_version: number;
}

export interface ResearchGraph {
  mission_id: string;
  cursor: number;
  nodes: ResearchNode[];
  relations: { kind: string; source: { node_id: string; revision: number }; target: { node_id: string; revision: number } }[];
  task_bindings: { task_id: string; node_id: string; revision: number }[];
}

export interface Provider {
  name: string;
  base_url: string;
  api: string;
  has_key: boolean;
  models: {
    id: string;
    context_window: number | null;
    max_tokens: number | null;
    thinking?: string | null;
    thinking_levels: string[] | null;
    thinking_capability: "known" | "unknown" | string;
  }[];
}

export interface Config {
  providers: Provider[];
  roles: Record<string, { provider?: string; model?: string }>;
  thinking: string;
}

export interface BillingSummary {
  balance: number | null;
  currency: string | null;
  remaining: number | null;
  credit_limit: number | null;
  debt: number | null;
  outstanding: number | null;
  status: string;
  fetched_at: string;
  source: string | null;
  availability: "available" | "unavailable" | "unknown" | string;
}

export interface ProviderDiscovery {
  models: { id: string; name?: string; context_window?: number; max_tokens?: number }[];
  model_source: "endpoint" | "known-list" | string;
  billing: BillingSummary;
}

export interface RuntimeJob {
  job_id?: string;
  mission_id: string;
  status: string;
  steps: number;
  completed_steps: number;
  result: { status?: string; reason?: string } | null;
  error?: string | null;
}

export interface RuntimeState {
  runtime: { available: boolean; reasons: string[]; pi_available?: boolean; models_available?: boolean; python_available?: boolean; sandbox_available?: boolean };
  jobs: RuntimeJob[];
}

export const ROLES = ["coordinator", "researcher", "hypothesis", "coder", "analyzer", "critic", "reviewer"] as const;

export const roleLabel: Record<string, string> = {
  coordinator: "主 Agent",
  researcher: "研究员",
  hypothesis: "假设研究",
  coder: "代码实现",
  analyzer: "结果分析",
  critic: "质疑审查",
  reviewer: "评审",
  runner: "实验执行",
  student: "研究学生",
};

export const statusLabel: Record<string, string> = {
  queued: "待执行",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  active: "运行中",
  paused: "已暂停",
  stopped: "已停止",
};
