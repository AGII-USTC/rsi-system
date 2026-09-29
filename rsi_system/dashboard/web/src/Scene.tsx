import { useEffect, useMemo, useState } from "react";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./scene.css";
import type { Event, Task } from "./types";
import { roleLabel, statusLabel } from "./types";
import { petForTask } from "./world/pets";

type AgentData = { task: Task; verticalEdges: boolean; live: boolean };
type AgentNode = Node<AgentData, "agent">;

function Person({ task, live }: { task: Pick<Task, "task_id" | "role" | "status" | "waiting">; live: boolean }) {
  const pet = petForTask(task);
  return <span className={`graph-person graph-role-${task.role} ${live ? "graph-person-live" : ""} ${task.status === "running" && !task.waiting ? "graph-person-active" : ""} ${task.waiting ? "graph-person-waiting" : ""} ${task.status === "failed" ? "graph-person-failed" : ""}`} aria-hidden="true">
    <span className="graph-person-disc" />
    <span className="graph-person-shadow" />
    <span className="graph-pet" style={{ backgroundImage: `url(${pet.sheet})` }} />
  </span>;
}

function AgentCard({ data }: NodeProps<AgentNode>) {
  const task = data.task;
  return <div className={`agent-node ${task.status === "running" ? "is-running" : ""} ${task.status === "failed" ? "is-failed" : ""}`}>
    <Handle type="target" position={data.verticalEdges ? Position.Top : Position.Left} style={{ opacity: 0 }} />
    <Person task={task} live={data.live} />
    <div className="agent-node-copy">
      <span className="agent-node-type">{roleLabel[task.role] || task.role}</span>
      <strong title={task.objective}>{task.objective}</strong>
      <span className="agent-node-meta"><i className={`status-dot ${task.status}`} />{task.waiting ? "等待回复" : statusLabel[task.status] || task.status}<span className="task-suffix">#{task.task_id.slice(-5)}</span></span>
    </div>
    <Handle type="source" position={data.verticalEdges ? Position.Bottom : Position.Right} style={{ opacity: 0 }} />
  </div>;
}

const nodeTypes = { agent: AgentCard };

function visibleTasks(tasks: Task[], limit: number): Task[] {
  const byId = new Map(tasks.map((task) => [task.task_id, task]));
  const priority = [...tasks].sort((a, b) => {
    const score = (task: Task) => task.status === "running" ? 0 : task.status === "queued" ? 1 : task.role === "coordinator" ? 2 : 3;
    return score(a) - score(b) || b.created_at.localeCompare(a.created_at);
  });
  const selected = new Map<string, Task>();
  for (const task of priority) {
    if (selected.size >= limit) break;
    const chain: Task[] = [];
    let current: Task | undefined = task;
    const seen = new Set<string>();
    while (current && !selected.has(current.task_id) && !seen.has(current.task_id)) {
      seen.add(current.task_id);
      chain.unshift(current);
      current = current.parent_task_id ? byId.get(current.parent_task_id) : undefined;
    }
    if (selected.size + chain.length <= limit) chain.forEach((item) => selected.set(item.task_id, item));
  }
  return [...selected.values()];
}

function makeGraph(tasks: Task[], events: Event[], live: boolean, mobile: boolean, compact: boolean): { nodes: AgentNode[]; edges: Edge[]; hidden: number } {
  const shown = visibleTasks(tasks, mobile || compact ? 6 : 9);
  const ids = new Set(shown.map((task) => task.task_id));
  const byId = new Map(shown.map((task) => [task.task_id, task]));
  const depth = (task: Task): number => {
    let current = task;
    let count = 0;
    const seen = new Set([task.task_id]);
    while (current.parent_task_id && byId.has(current.parent_task_id) && count < 5) {
      current = byId.get(current.parent_task_id)!;
      if (seen.has(current.task_id)) break;
      seen.add(current.task_id);
      count++;
    }
    return count;
  };
  if (mobile || compact) shown.sort((a, b) => depth(a) - depth(b) || a.created_at.localeCompare(b.created_at));
  const columns = new Map<number, Task[]>();
  shown.forEach((task) => {
    const column = Math.min(depth(task), 2);
    columns.set(column, [...(columns.get(column) || []), task]);
  });
  const nodes = shown.map((task): AgentNode => {
    const column = Math.min(depth(task), 2);
    const group = columns.get(column)!;
    const index = shown.indexOf(task);
    const position = mobile ? { x: 25, y: index * 180 + 20 } : compact ? { x: index % 2 * 280 + 15, y: Math.floor(index / 2) * 183 + 20 } : { x: column * 294, y: group.indexOf(task) * 183 + 20 };
    return { id: task.task_id, type: "agent", data: { task, verticalEdges: mobile || compact, live }, ariaLabel: `${roleLabel[task.role] || task.role}：${task.objective}，${statusLabel[task.status] || task.status}，任务 ${task.task_id}`, position, draggable: false };
  });
  const edges: Edge[] = shown.filter((task) => task.parent_task_id && ids.has(task.parent_task_id)).map((task) => ({
    id: `delegate-${task.task_id}`, source: task.parent_task_id!, target: task.task_id, type: "smoothstep",
    animated: live && task.status === "running", markerEnd: { type: MarkerType.ArrowClosed, color: "#8bad9c", width: 14, height: 14 },
    style: { stroke: task.status === "running" ? "#79ac92" : "#c2d1c7", strokeWidth: 1.8 },
  }));
  const recentMessages = events.filter((event) => event.kind === "direct_message" && event.from_task_id && event.to_task_id && ids.has(event.from_task_id) && ids.has(event.to_task_id)).slice(0, 8);
  recentMessages.forEach((event) => {
    const recent = Date.now() - Date.parse(event.created_at) < 90_000;
    edges.push({ id: `message-${event.event_id}`, source: event.from_task_id!, target: event.to_task_id!, type: "smoothstep",
      animated: live && recent, label: "消息", labelStyle: { fill: "#986c51", fontSize: 10 },
      labelBgStyle: { fill: "#fff8ef" }, labelBgPadding: [5, 3],
      markerEnd: { type: MarkerType.ArrowClosed, color: "#dcad78", width: 14, height: 14 },
      style: { stroke: "#dcad78", strokeDasharray: "4 5", strokeWidth: 1.8 },
    });
  });
  return { nodes, edges, hidden: Math.max(0, tasks.length - shown.length) };
}

export function Scene({ tasks, events, live, missionId, onTask, onEvent }: { tasks: Task[]; events: Event[]; live: boolean; missionId: string; onTask: (id: string) => void; onEvent: (id: number) => void }) {
  const [mobile, setMobile] = useState(() => window.matchMedia("(max-width: 620px)").matches);
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 1360px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 620px)");
    const compactMedia = window.matchMedia("(max-width: 1360px)");
    const change = () => setMobile(media.matches);
    const changeCompact = () => setCompact(compactMedia.matches);
    media.addEventListener("change", change);
    compactMedia.addEventListener("change", changeCompact);
    return () => { media.removeEventListener("change", change); compactMedia.removeEventListener("change", changeCompact); };
  }, []);
  const graph = useMemo(() => makeGraph(tasks, events, live, mobile, compact), [tasks, events, live, mobile, compact]);
  if (!tasks.length) return <div className="scene-empty"><span className="scene-empty-figure"><Person task={{ task_id: "empty", role: "coordinator", status: "queued", waiting: false }} live={false} /></span><strong>协作现场待命</strong><span>任务开始后，这里会显示真实的派发关系与消息路径。</span></div>;
  return <div className="scene-wrap">
    <ReactFlow key={`${missionId}-${mobile}-${compact}-${graph.nodes.map((node) => node.id).join("-")}`} nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: mobile ? 0.06 : compact ? 0.07 : 0.1, minZoom: 0.35, maxZoom: 1.2 }}
      minZoom={0.25} maxZoom={1.5} nodesConnectable={false} nodesDraggable={false} elementsSelectable
      onNodeClick={(_, node) => onTask(node.id)} onEdgeClick={(_, edge) => { if (edge.id.startsWith("message-")) onEvent(Number(edge.id.slice(8))); }}
      proOptions={{ hideAttribution: true }} aria-label="Agent 协作关系图">
      <Background color="#e1e9e2" gap={22} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
    {graph.hidden > 0 && <div className="scene-overflow">图中显示最近与进行中的任务 · 另有 {graph.hidden} 个任务可在记录中查看</div>}
  </div>;
}
