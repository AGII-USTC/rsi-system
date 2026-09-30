import { useEffect, useMemo, useState, type FormEvent } from "react";
import { GitBranch, Plus, RefreshCw } from "lucide-react";
import { fetchResearch, proposeResearchNode } from "./api";
import type { Mission, ResearchGraph, ResearchNode } from "./types";
import "./research-nodes.css";

type Draft = { question: string; scope: string; summary: string };
const emptyDraft: Draft = { question: "", scope: "", summary: "" };
const executionLabels: Record<string, string> = {
  proposed: "待评估", active: "进行中", running: "执行中", blocked: "受阻",
  completed: "已完成", failed: "失败", paused: "已暂停", stopped: "已停止",
};
const findingLabels: Record<string, string> = {
  unknown: "尚无结论", inconclusive: "证据不足", supported: "支持", refuted: "不支持", mixed: "结果混合",
};

function orderedNodes(graph: ResearchGraph): { node: ResearchNode; depth: number }[] {
  const nodes = new Map(graph.nodes.map((node) => [node.node_id, node]));
  const children = new Map<string, ResearchNode[]>();
  const parentIds = new Set<string>();
  for (const relation of graph.relations) {
    if (relation.kind !== "decomposes" || !nodes.has(relation.source.node_id)) continue;
    const child = nodes.get(relation.target.node_id);
    if (!child) continue;
    parentIds.add(child.node_id);
    children.set(relation.source.node_id, [...(children.get(relation.source.node_id) || []), child]);
  }
  const result: { node: ResearchNode; depth: number }[] = [];
  const visited = new Set<string>();
  const walk = (node: ResearchNode, depth: number) => {
    if (visited.has(node.node_id)) return;
    visited.add(node.node_id);
    result.push({ node, depth });
    for (const child of (children.get(node.node_id) || []).sort((a, b) => a.created_version - b.created_version)) walk(child, depth + 1);
  };
  for (const node of graph.nodes.filter((item) => !parentIds.has(item.node_id))) walk(node, 0);
  for (const node of graph.nodes) walk(node, 0);
  return result;
}

export function ResearchNodes({ mission, demo, onTask, onCreated }: {
  mission: Mission | null; demo: boolean; onTask: (id: string) => void; onCreated: () => void;
}) {
  const [graph, setGraph] = useState<ResearchGraph | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [composing, setComposing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const missionId = demo ? null : mission?.mission_id;

  useEffect(() => {
    setGraph(null); setSelected(null); setComposing(false); setError(null); setLoadError(null); setNotice(null);
    if (!missionId) return;
    let alive = true;
    const load = async () => {
      try {
        const next = await fetchResearch(missionId);
        if (alive) { setGraph(next); setLoadError(null); setSelected((id) => id && next.nodes.some((n) => n.node_id === id) ? id : next.nodes[0]?.node_id || null); }
      } catch (cause) { if (alive) setLoadError(cause instanceof Error ? cause.message : "研究节点读取失败"); }
    };
    void load();
    const interval = window.setInterval(() => { if (!document.hidden) void load(); }, 5000);
    return () => { alive = false; window.clearInterval(interval); };
  }, [missionId]);

  const ordered = useMemo(() => graph ? orderedNodes(graph) : [], [graph]);
  const node = graph?.nodes.find((item) => item.node_id === selected) || null;
  const parent = graph?.relations.find((relation) => relation.kind === "decomposes" && relation.target.node_id === selected);
  const boundTasks = graph?.task_bindings.filter((binding) => binding.node_id === selected) || [];
  const canPropose = mission?.status === "active" || mission?.status === "paused";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!missionId || !node || !canPropose || saving) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      const result = await proposeResearchNode({ mission_id: missionId,
        parent: { node_id: node.node_id, revision: node.revision },
        question: draft.question.trim(), scope: draft.scope.trim(), summary: draft.summary.trim() });
      setGraph((current) => current ? { ...current, nodes: [...current.nodes, result.node],
        relations: [...current.relations, { kind: "decomposes", source: { node_id: node.node_id, revision: node.revision },
          target: { node_id: result.node.node_id, revision: 1 } }] } : current);
      setSelected(result.node.node_id); setDraft(emptyDraft); setComposing(false);
      setNotice("研究提议已记录，等待主编排评估。"); onCreated();
      try { setGraph(await fetchResearch(missionId)); } catch { setLoadError("节点已保存，列表将在连接恢复后同步。"); }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交失败");
      try { setGraph(await fetchResearch(missionId)); } catch { /* Preserve the actionable error. */ }
    } finally { setSaving(false); }
  };

  return <div className="research-view">
    <header className="page-head research-head"><div><h1>研究节点</h1><p>{mission?.objective || ""}</p></div><span>{graph ? `${graph.nodes.length} 个节点` : ""}</span></header>
    {demo ? <p className="research-empty">演示模式没有真实研究节点。</p> : !mission ? <p className="research-empty">先选择一个研究任务。</p> :
      <div className="research-layout">
        <div className="research-node-list" aria-label="研究节点列表">
          {ordered.map(({ node: item, depth }) => <button key={item.node_id} type="button" className={`research-node-row ${selected === item.node_id ? "selected" : ""}`}
            style={{ paddingLeft: `${14 + Math.min(depth, 6) * 17}px` }} onClick={() => { setSelected(item.node_id); setComposing(false); setNotice(null); setError(null); }}>
            <GitBranch size={15} /><span><strong>{item.question}</strong><small>{item.finding_status_basis === "human_proposal" ? "人工提议" : executionLabels[item.execution_status] || item.execution_status} · v{item.revision}</small></span>
          </button>)}
          {graph && !graph.nodes.length && <p className="research-empty">这个任务还没有研究节点。</p>}
        </div>
        <section className="research-detail" aria-label="节点详情">
          {node ? <><div className="research-detail-head"><span>NODE / {node.node_id} · v{node.revision}</span><strong>{node.finding_status_basis === "human_proposal" ? "人工提议" : executionLabels[node.execution_status] || node.execution_status}</strong></div>
            <h2>{node.question}</h2>
            <dl><dt>研究范围</dt><dd>{node.scope}</dd><dt>当前判断</dt><dd>{node.summary || "暂无结论"}</dd>
              <dt>发现状态</dt><dd>{findingLabels[node.finding_status] || node.finding_status}</dd>
              {parent && <><dt>上级节点</dt><dd><button type="button" className="research-text-button" onClick={() => setSelected(parent.source.node_id)}>{graph?.nodes.find((item) => item.node_id === parent.source.node_id)?.question || parent.source.node_id}</button></dd></>}
              {node.run_refs.length > 0 && <><dt>关联 Run</dt><dd>{node.run_refs.join(", ")}</dd></>}
              {boundTasks.length > 0 && <><dt>关联任务</dt><dd className="research-task-links">{boundTasks.map((binding) => <button key={binding.task_id} type="button" className="research-text-button" onClick={() => onTask(binding.task_id)}>{binding.task_id} · v{binding.revision}</button>)}</dd></>}
            </dl>
            {canPropose && !composing && <button className="primary-button research-add" type="button" onClick={() => { setComposing(true); setError(null); setNotice(null); }}><Plus size={15} /> 新增研究方向</button>}
            {composing && <form className="research-proposal" onSubmit={submit}>
              <h3>在此节点下提出方向</h3>
              <label>研究问题<input value={draft.question} onChange={(event) => setDraft({ ...draft, question: event.target.value })} maxLength={500} required autoFocus /></label>
              <label>研究范围<textarea value={draft.scope} onChange={(event) => setDraft({ ...draft, scope: event.target.value })} maxLength={2000} rows={3} required /></label>
              <label>目前的依据或想法<textarea value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} maxLength={2000} rows={3} required /></label>
              <div className="research-form-actions"><button className="primary-button" type="submit" disabled={saving}>{saving ? "提交中" : "提交提议"}</button><button className="research-text-button" type="button" onClick={() => setComposing(false)} disabled={saving}>取消</button></div>
            </form>}
            {notice && <p className="feedback success" role="status">{notice}</p>}
          </> : <p className="research-empty">选择左侧节点查看详情。</p>}
          {(error || loadError) && <p className="feedback error" role="alert"><RefreshCw size={13} />{error || loadError}</p>}
        </section>
      </div>}
  </div>;
}
