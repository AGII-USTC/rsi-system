import { useState } from "react";
import { Check, ChevronDown, ChevronUp, Pause, Play, Plus, X } from "lucide-react";
import { createMission, missionAction } from "./api";
import type { Mission, RuntimeState } from "./types";

export function MissionControls({ mission, runtime, demo, onCreated, onChanged, onDemo }: { mission: Mission | null; runtime: RuntimeState | null; demo: boolean; onCreated: (id: string) => void; onChanged: () => void; onDemo: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [details, setDetails] = useState(false);
  const [objective, setObjective] = useState("");
  const [background, setBackground] = useState("");
  const [evidence, setEvidence] = useState("");
  const [stop, setStop] = useState("");
  const [limit, setLimit] = useState(12);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const jobs = runtime?.jobs.filter((job) => job.mission_id === mission?.mission_id) || [];
  const activeJob = jobs.find((job) => job.status === "running" || job.status === "queued");
  const lastJob = activeJob || jobs[0];
  const runAvailable = !!runtime?.runtime.available;
  const jobError = lastJob?.error || (lastJob?.status === "failed" ? lastJob.result?.reason : null);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setFeedback(null);
    try {
      const list = (value: string) => value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      const created = await createMission({ objective: objective.trim(), background: background.trim(), known_evidence: list(evidence), stop_conditions: list(stop), resources: { max_agent_tasks: limit, max_runs: 1, max_seconds_per_run: 120 } });
      onCreated(created.mission_id); setExpanded(false); setObjective(""); setFeedback({ error: false, text: "研究任务已创建。点击推进运行开始 Agent 协作。" });
    } catch (error) { setFeedback({ error: true, text: error instanceof Error ? error.message : "创建任务失败" }); }
    finally { setSaving(false); }
  };
  const action = async (kind: "run" | "pause" | "resume") => {
    if (!mission) return;
    setPending(kind); setFeedback(null);
    try {
      await missionAction(mission.mission_id, kind, kind === "run" ? { steps: 3 } : {});
      setFeedback({ error: false, text: kind === "run" ? "已开始推进，任务与信息流会持续更新。" : kind === "pause" ? "已请求暂停，当前调用完成后停止推进。" : "任务已恢复，可继续推进运行。" });
      onChanged();
    } catch (error) { setFeedback({ error: true, text: error instanceof Error ? error.message : "操作失败" }); }
    finally { setPending(null); }
  };
  return <section className="mission-controls">
    <div className="mission-control-row"><div className="mission-control-title"><span className="eyebrow">RESEARCH MISSION</span><span>{demo ? "演示协作" : mission ? "当前研究任务" : "开启一次研究"}</span></div><div className="mission-buttons">
      {!demo && mission && <>
        {mission.status === "paused" ? <button className="quiet-button" type="button" disabled={!!pending || !!activeJob} onClick={() => void action("resume")}><Play size={14} /> 恢复任务</button> : <button className="quiet-button" type="button" disabled={!!pending} onClick={() => void action("pause")}><Pause size={14} /> 暂停</button>}
        <button className="primary-button" type="button" disabled={!runAvailable || !!pending || !!activeJob} onClick={() => void action("run")}><Play size={14} /> {activeJob ? "推进中" : mission.status === "paused" ? "开始运行" : "推进运行"}</button>
      </>}
      <button className="quiet-button" type="button" onClick={() => { setExpanded((value) => !value); setFeedback(null); }}><Plus size={14} /> 新建研究</button>
      {!demo && <button className="quiet-button" type="button" onClick={onDemo}>体验演示</button>}
    </div></div>
    {!demo && runtime && !runtime.runtime.available && <p className="runtime-unavailable">{runtime.runtime.reasons.join("；") || "Agent 运行环境尚未就绪，请先完成模型配置。"}</p>}
    {!demo && lastJob && <div className={`runtime-job ${jobError ? "failed" : ""}`}><span>{activeJob ? "Agent 正在推进" : lastJob.status === "failed" ? "推进失败" : "最近一次推进"}</span><span>{lastJob.completed_steps} / {lastJob.steps} 步</span>{jobError && <p>{jobError}</p>}</div>}
    {expanded && <form className="mission-form" onSubmit={submit}><div className="mission-form-heading"><strong>你想研究什么？</strong><button className="icon-button" type="button" aria-label="关闭新建研究" onClick={() => setExpanded(false)}><X size={16} /></button></div><label htmlFor="mission-objective">研究目标</label><textarea id="mission-objective" value={objective} onChange={(event) => setObjective(event.target.value)} required minLength={3} maxLength={8000} rows={3} placeholder="描述一个问题、希望比较的方案，或者需要验证的想法…" />
      <button className="quiet-button mission-more" type="button" onClick={() => setDetails((value) => !value)}>{details ? <ChevronUp size={14} /> : <ChevronDown size={14} />} 背景、证据与任务额度</button>
      {details && <div className="mission-extra"><label>背景<textarea value={background} onChange={(event) => setBackground(event.target.value)} rows={2} maxLength={8000} /></label><label>已有证据 · 每行一条<textarea value={evidence} onChange={(event) => setEvidence(event.target.value)} rows={2} maxLength={8000} /></label><label>完成条件 · 每行一条<textarea value={stop} onChange={(event) => setStop(event.target.value)} rows={2} maxLength={8000} /></label><label>Agent 任务额度<input type="number" min={1} max={100} required value={limit} onChange={(event) => setLimit(Number(event.target.value))} /></label></div>}
      <div className="form-footer"><button className="primary-button" type="submit" disabled={saving}>{saving ? "创建中…" : "创建研究任务"}</button><span className="mission-create-note">创建后暂停等待；运行由你手动启动。</span></div></form>}
    {feedback && <div role="status" className={`mission-feedback ${feedback.error ? "error" : ""}`}>{!feedback.error && <Check size={14} />}{feedback.text}</div>}
  </section>;
}
