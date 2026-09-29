import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowRight, Check, ChevronDown, CircleHelp, GitBranch, KeyRound, MessageCircle, Plus, RefreshCw, WifiOff, X } from "lucide-react";
import { discoverProvider, fetchConfig, fetchRuntime, fetchState, saveProvider, saveRoles } from "./api";
import { demoSnapshot, demoStageAt } from "./demo";
import { Scene } from "./Scene";
import { MissionControls } from "./MissionControls";
import { WorldScene } from "./WorldScene";
import { loadWorldSettings, WorldControls } from "./WorldControls";
import { discoveryDraftReady, discoveryRequestKey } from "./providerDiscovery";
import { eventCaption } from "./world/engine";
import type { WorldSettings } from "./worldTypes";
import type { Config, Event, Provider, ProviderDiscovery, RuntimeState, Snapshot, Task } from "./types";
import { ROLES, roleLabel, statusLabel } from "./types";

type View = "overview" | "records" | "settings" | "graph";
type RecordTab = "tasks" | "events";
type Feedback = { type: "success" | "error"; text: string } | null;
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const;
type ModelSetting = {
  context_window: number;
  max_tokens: number;
  thinking: string | null;
  // Undefined means leave Pi's existing metadata untouched. An array is an
  // explicit dashboard declaration, including an empty non-reasoning set.
  thinking_levels?: string[] | null;
};

const thinkingLabel: Record<string, string> = {
  off: "Off",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Xhigh",
  max: "Max",
};

function timeLabel(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function shortId(value: string): string { return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value; }

function Empty({ text, action }: { text: string; action?: React.ReactNode }) {
  return <div className="empty"><CircleHelp size={20} strokeWidth={1.8} /><span>{text}</span>{action}</div>;
}

function Status({ value }: { value: string }) {
  return <span className={`status status-${value}`}><i />{statusLabel[value] || value}</span>;
}

function EventRow({ event, onClick, selected }: { event: Event; onClick: () => void; selected?: boolean }) {
  return <button type="button" className={`event-row ${selected ? "selected" : ""}`} onClick={onClick}>
    <span className={`event-symbol ${event.kind === "direct_message" ? "message" : ""}`}>{event.kind === "direct_message" ? <MessageCircle size={15} /> : event.kind === "task_created" ? <GitBranch size={15} /> : <Activity size={15} />}</span>
    <span className="event-body"><span className="event-head"><strong>{eventCaption(event)}</strong><time>{timeLabel(event.created_at)}</time></span>
      {(event.from_role || event.to_role) && <small className="event-route">{event.from_role ? roleLabel[event.from_role] || event.from_role : "系统"}{event.to_role ? ` → ${roleLabel[event.to_role] || event.to_role}` : ""}</small>}
      <span className="event-text">{event.text || "查看事件详情"}</span>
    </span>
  </button>;
}

function TaskRow({ task, selected, onClick }: { task: Task; selected?: boolean; onClick: () => void }) {
  return <button type="button" className={`task-row ${selected ? "selected" : ""}`} onClick={onClick}>
    <span className={`role-mark role-${task.role}`}>{(roleLabel[task.role] || task.role).slice(0, 1)}</span>
    <span className="task-row-main"><strong>{task.objective}</strong><small>{roleLabel[task.role] || task.role} · {shortId(task.task_id)}{task.parent_task_id ? " · 委派任务" : ""}</small></span>
    <Status value={task.status} />
  </button>;
}

function Detail({ task, event, tasks, onTask }: { task: Task | null; event: Event | null; tasks: Task[]; onTask: (id: string) => void }) {
  if (!task && !event) return <aside className="detail-panel"><Empty text="选择左侧记录查看详情" /></aside>;
  if (event) return <aside className="detail-panel"><div className="panel-eyebrow">EVENT / {event.event_id}</div><h2>{eventCaption(event)}</h2><p className="detail-time">{timeLabel(event.created_at)}</p>
    <div className="detail-block"><span>信息内容</span><p>{event.text || "无附加文本"}</p></div>
    {(event.from_role || event.to_role) && <div className="detail-block"><span>传递路径</span><p>{event.from_role ? roleLabel[event.from_role] || event.from_role : "系统"} <ArrowRight size={14} /> {event.to_role ? roleLabel[event.to_role] || event.to_role : "任务黑板"}</p></div>}
    {event.from_task_id && <div className="detail-block"><span>发送任务</span><button className="id-link" type="button" onClick={() => onTask(event.from_task_id!)}>{shortId(event.from_task_id)}</button></div>}
    {event.to_task_id && <div className="detail-block"><span>接收任务</span><button className="id-link" type="button" onClick={() => onTask(event.to_task_id!)}>{shortId(event.to_task_id)}</button></div>}
    <div className="detail-block"><span>事件类型</span><code>{event.kind}</code></div>
    {event.knowledge_action && <div className="detail-block"><span>知识动作</span><p>{event.knowledge_action === "read" ? "读取" : event.knowledge_action === "archive" ? "证据归档" : "写入知识页"}</p></div>}
    {event.evidence_status && <div className="detail-block"><span>记录说明</span><p>{event.evidence_status}</p></div>}
  </aside>;
  const children = tasks.filter((item) => item.parent_task_id === task!.task_id);
  return <aside className="detail-panel"><div className="panel-eyebrow">AGENT / {roleLabel[task!.role] || task!.role}</div><h2>{task!.objective}</h2><Status value={task!.status} />
    <div className="detail-block"><span>任务 ID</span><code>{task!.task_id}</code></div>
    {task!.parent_task_id && <div className="detail-block"><span>来自任务</span><button className="id-link" type="button" onClick={() => onTask(task!.parent_task_id!)}>{shortId(task!.parent_task_id!)}</button></div>}
    {task!.reason && <div className="detail-block"><span>任务缘由</span><p>{task!.reason}</p></div>}
    {task!.summary && <div className="detail-block"><span>回执摘要</span><p>{task!.summary}</p></div>}
    {task!.error && <div className="detail-block detail-error"><span>错误</span><p>{task!.error}</p></div>}
    <div className="detail-block"><span>开始 / 更新</span><p>{timeLabel(task!.created_at)} / {timeLabel(task!.updated_at)}</p></div>
    {children.length > 0 && <div className="detail-block"><span>派发给</span><div className="detail-children">{children.map((child) => <button key={child.task_id} className="id-link" type="button" onClick={() => onTask(child.task_id)}>{roleLabel[child.role] || child.role} · {shortId(child.task_id)}</button>)}</div></div>}
  </aside>;
}

function Records({ snapshot, tab, setTab, taskId, eventId, selectTask, selectEvent }: { snapshot: Snapshot; tab: RecordTab; setTab: (tab: RecordTab) => void; taskId: string | null; eventId: number | null; selectTask: (id: string) => void; selectEvent: (id: number) => void }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const tasks = snapshot.tasks.filter((task) => (filter === "all" || task.status === filter) && `${task.objective} ${task.role} ${task.task_id}`.toLowerCase().includes(query.toLowerCase()));
  const events = snapshot.events.filter((event) => `${event.title} ${event.text} ${event.from_role || ""} ${event.to_role || ""}`.toLowerCase().includes(query.toLowerCase()));
  return <>
    <div className="page-head"><div><span className="eyebrow">ACTIVITY / HISTORY</span><h1>任务与消息</h1><p>查看每次委派、传递和回执。</p></div><div className="segmented" role="tablist" aria-label="记录类型"><button type="button" role="tab" aria-selected={tab === "tasks"} className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}>任务 {snapshot.counts?.tasks ?? snapshot.tasks.length}</button><button type="button" role="tab" aria-selected={tab === "events"} className={tab === "events" ? "active" : ""} onClick={() => setTab("events")}>消息与事件 {snapshot.counts?.events ?? snapshot.events.length}</button></div></div>
    <div className="records-toolbar"><input aria-label="搜索记录" placeholder="搜索任务、角色或消息" value={query} onChange={(event) => setQuery(event.target.value)} />{tab === "tasks" && <select aria-label="按状态筛选" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部状态</option><option value="running">进行中</option><option value="queued">待执行</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select>}</div>
    {snapshot.truncated?.[tab] && <p className="truncation-note">当前显示最近 {tab === "tasks" ? snapshot.tasks.length : snapshot.events.length} 条记录；总计 {tab === "tasks" ? snapshot.counts?.tasks : snapshot.counts?.events} 条。搜索和筛选仅作用于已加载记录。</p>}
    <div className="records-layout"><div className="records-list">{tab === "tasks" ? tasks.length ? tasks.map((task) => <TaskRow key={task.task_id} task={task} selected={taskId === task.task_id} onClick={() => selectTask(task.task_id)} />) : <Empty text="没有符合条件的任务" /> : events.length ? events.map((event) => <EventRow key={event.event_id} event={event} selected={eventId === event.event_id} onClick={() => selectEvent(event.event_id)} />) : <Empty text="没有符合条件的消息或事件" />}</div><Detail task={tab === "tasks" ? snapshot.tasks.find((item) => item.task_id === taskId) || null : null} event={tab === "events" ? snapshot.events.find((item) => item.event_id === eventId) || null : null} tasks={snapshot.tasks} onTask={selectTask} /></div>
  </>;
}

function ProviderForm({ config, onSaved }: { config: Config; onSaved: (config: Config) => void }) {
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [api, setApi] = useState("openai-responses");
  const [key, setKey] = useState("");
  const [models, setModels] = useState("");
  const [modelSettings, setModelSettings] = useState<Record<string, ModelSetting>>({});
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [discoveredBilling, setDiscoveredBilling] = useState<ProviderDiscovery["billing"] | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const discoveryAttempt = useRef<string | null>(null);
  const discoveryInFlight = useRef<string | null>(null);
  const current = config.providers.find((provider) => provider.name === selected);
  const modelIds = [...new Set(models.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
  const settingsFor = (id: string): ModelSetting => modelSettings[id] || { context_window: 128000, max_tokens: 16384, thinking: null };
  const modelInfo = (id: string) => current?.models.find((model) => model.id === id);
  const supportedLevels = (id: string): string[] => {
    const setting = settingsFor(id);
    if (setting.thinking_levels !== undefined) return setting.thinking_levels || [];
    return modelInfo(id)?.thinking_levels || [];
  };
  const updateModel = (id: string, patch: Partial<ModelSetting>) =>
    setModelSettings((previous) => ({ ...previous, [id]: { ...settingsFor(id), ...patch } }));
  const choose = (providerName: string) => {
    setSelected(providerName);
    const provider = config.providers.find((item) => item.name === providerName);
    setName(provider?.name || ""); setUrl(provider?.base_url || ""); setApi(provider?.api || "openai-responses");
    setModels(provider?.models.map((item) => item.id).join("\n") || "");
    setModelSettings(Object.fromEntries((provider?.models || []).map((model) => [model.id, {
      context_window: model.context_window || 128000,
      max_tokens: model.max_tokens || 16384,
      thinking: model.thinking || null,
      // Keep this undefined for Pi-derived metadata. Sending the derived list
      // back would erase provider-specific thinkingLevelMap values.
      thinking_levels: undefined,
    }])));
    setKey(""); setFeedback(null);
    setDiscoveredBilling(null);
    discoveryAttempt.current = null;
    discoveryInFlight.current = null;
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setFeedback(null);
    const modelIds = models.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    if (new Set(modelIds).size !== modelIds.length) { setFeedback({ type: "error", text: "模型 ID 不能重复" }); return; }
    setSaving(true);
    try {
      const saved = await saveProvider({ name: name.trim(), base_url: url.trim(), api, api_key: key, models: modelIds, model_settings: Object.fromEntries(modelIds.map((id) => [id, settingsFor(id)])) });
      onSaved(saved); setSelected(name.trim()); setKey(""); setFeedback({ type: "success", text: "供应商已保存，将用于之后启动的 Agent。" });
    } catch (error) { setFeedback({ type: "error", text: error instanceof Error ? error.message : "保存失败" }); }
    finally { setSaving(false); }
  };
  const runDiscovery = async (force: boolean) => {
    const baseUrl = url.trim();
    const apiKey = key.trim();
    if (!discoveryDraftReady(baseUrl, apiKey)) {
      if (force) setFeedback({ type: "error", text: "获取模型需要先填写 API 地址和临时 API Key；不会保存或回传" });
      return;
    }
    const requestKey = discoveryRequestKey(api, baseUrl, apiKey);
    if (discoveryInFlight.current || (!force && discoveryAttempt.current === requestKey)) return;
    discoveryAttempt.current = requestKey;
    discoveryInFlight.current = requestKey;
    setFeedback(null);
    setDiscoveredBilling(null);
    setDiscovering(true);
    try {
      const discovered = await discoverProvider({
        name: name.trim(), base_url: baseUrl, api, api_key: apiKey, models: modelIds,
      });
      const found = discovered.models.filter((model) => model.id).map((model) => model.id);
      if (!found.length) throw new Error("供应商未返回可用模型");
      setModels(found.join("\n"));
      setDiscoveredBilling(discovered.billing || null);
      setModelSettings((previous) => Object.fromEntries(found.map((id) => {
        const remote = discovered.models.find((model) => model.id === id);
        const prior = previous[id] || { context_window: 128000, max_tokens: 16384, thinking: null };
        return [id, {
          ...prior,
          context_window: remote?.context_window || prior.context_window,
          max_tokens: remote?.max_tokens || prior.max_tokens,
        }];
      })));
      setFeedback({ type: "success", text: `已获取 ${found.length} 个模型，可继续手写编辑。` });
    } catch (error) {
      setFeedback({ type: "error", text: error instanceof Error ? error.message : "获取模型失败" });
    } finally {
      if (discoveryInFlight.current === requestKey) discoveryInFlight.current = null;
      setDiscovering(false);
    }
  };
  const discover = () => { void runDiscovery(true); };
  const autoDiscover = () => { void runDiscovery(false); };
  return <section className="settings-section"><div className="section-title"><span className="eyebrow">01 / CONNECTION</span><h2>API 供应商</h2></div>
    <div className="provider-tabs"><label htmlFor="provider-choice">选择供应商</label><div className="select-wrap"><select id="provider-choice" value={selected} onChange={(event) => choose(event.target.value)}><option value="">+ 新增供应商</option>{config.providers.map((provider) => <option key={provider.name} value={provider.name}>{provider.name}</option>)}</select><ChevronDown size={15} /></div></div>
    <form onSubmit={submit} className="settings-form"><label>供应商名称<input value={name} onChange={(event) => setName(event.target.value)} required pattern="[A-Za-z][A-Za-z0-9._-]*" maxLength={64} disabled={!!selected} placeholder="my-provider" autoComplete="off" /></label>
      <label>API 地址<input type="url" value={url} onChange={(event) => setUrl(event.target.value)} onBlur={autoDiscover} required placeholder="https://api.example.com/v1" autoComplete="off" /></label>
      <label>接口协议<select value={api} onChange={(event) => setApi(event.target.value)}><option value="openai-responses">OpenAI Responses</option><option value="openai-completions">OpenAI Completions</option><option value="anthropic-messages">Anthropic Messages</option></select></label>
      <label>API Key <span className={`key-state ${current?.has_key ? "configured" : ""}`}><KeyRound size={12} />{current?.has_key ? "已设置" : "未设置"}</span><input type="password" value={key} onChange={(event) => setKey(event.target.value)} onBlur={autoDiscover} placeholder={current?.has_key ? "留空以保留现有密钥" : "填写 API Key"} autoComplete="new-password" /></label>
      <label>模型 ID <small>每行一个</small><div className="model-discovery-actions"><button className="secondary-button" type="button" onClick={discover} disabled={discovering || saving}>{discovering ? "获取中…" : "获取模型"}</button><span>使用上面的临时 API Key 查询，不会保存密钥。</span></div>{discoveredBilling && <div className="discovery-billing" role="status"><span>账单：{discoveredBilling.availability === "available" ? "可用" : discoveredBilling.availability === "unavailable" ? "不可用" : "未知"}</span>{discoveredBilling.balance != null && <span>余额 {discoveredBilling.balance}{discoveredBilling.currency ? ` ${discoveredBilling.currency}` : ""}</span>}{discoveredBilling.outstanding != null && <span>欠费 {discoveredBilling.outstanding}{discoveredBilling.currency ? ` ${discoveredBilling.currency}` : ""}</span>}</div>}<textarea value={models} onChange={(event) => setModels(event.target.value)} rows={4} required placeholder="model-id" /></label>
      {modelIds.length > 0 && <div className="model-settings"><div className="model-settings-head"><span>模型</span><span>上下文长度</span><span>最大输出</span><span>能力 / 选定档位</span></div>{modelIds.map((id) => {
        const info = modelInfo(id);
        const setting = settingsFor(id);
        const levels = supportedLevels(id);
        const selectedIsUnsupported = !!setting.thinking && !levels.includes(setting.thinking);
        return <div className="model-settings-row" key={id}>
          <code title={id}>{id}</code>
          <input aria-label={`${id} 上下文长度`} type="number" min={1024} max={4000000} required value={setting.context_window} onChange={(event) => updateModel(id, { context_window: Number(event.target.value) })} />
          <input aria-label={`${id} 最大输出`} type="number" min={1} max={setting.context_window} required value={setting.max_tokens} onChange={(event) => updateModel(id, { max_tokens: Number(event.target.value) })} />
          <div className="model-thinking-control">
            <select aria-label={`${id} 推理档位`} value={setting.thinking || ""} onChange={(event) => updateModel(id, { thinking: event.target.value || null })}>
              <option value="">使用角色默认</option>
              {selectedIsUnsupported && <option value={setting.thinking || ""}>{thinkingLabel[setting.thinking || ""] || setting.thinking}（当前不受支持）</option>}
              {levels.map((level) => <option key={`${id}-${level}`} value={level}>{thinkingLabel[level] || level}</option>)}
            </select>
            <small>{info?.thinking_capability !== "known" && setting.thinking_levels === undefined
              ? "Pi 未声明能力；请勾选支持档位"
              : `支持：${levels.length ? levels.map((level) => thinkingLabel[level] || level).join("、") : "暂无（仅可关闭）"}`}</small>
            <div className="thinking-levels" aria-label={`${id} 支持的推理档位`}>
              {THINKING_LEVELS.map((level) => <label key={`${id}-cap-${level}`}><input type="checkbox" checked={levels.includes(level)} onChange={(event) => {
                const next = event.target.checked ? [...new Set([...levels, level])] : levels.filter((item) => item !== level);
                updateModel(id, { thinking_levels: next });
              }} /><span>{thinkingLabel[level]}</span></label>)}
            </div>
          </div>
        </div>;
      })}</div>}
      <div className="form-footer"><button className="primary-button" type="submit" disabled={saving}>{saving ? "保存中…" : "保存供应商"}</button>{feedback && <span className={`feedback ${feedback.type}`} role="status">{feedback.type === "success" && <Check size={14} />}{feedback.text}</span>}</div>
    </form>
  </section>;
}

function RolesForm({ config, onSaved }: { config: Config; onSaved: (config: Config) => void }) {
  const [roles, setRoles] = useState(config.roles);
  const [thinking, setThinking] = useState(config.thinking || "high");
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);
  useEffect(() => { setRoles(config.roles); setThinking(config.thinking || "high"); }, [config]);
  const options = config.providers.flatMap((provider: Provider) => provider.models.map((model) => ({ value: `${provider.name}|${model.id}`, label: `${provider.name} / ${model.id}`, provider: provider.name, model: model.id })));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setFeedback(null);
    try { const saved = await saveRoles({ roles, thinking }); onSaved(saved); setFeedback({ type: "success", text: "角色分配已保存，将用于之后启动的 Agent。" }); }
    catch (error) { setFeedback({ type: "error", text: error instanceof Error ? error.message : "保存失败" }); }
    finally { setSaving(false); }
  };
  return <section className="settings-section"><div className="section-title"><span className="eyebrow">02 / ROUTING</span><h2>角色与模型</h2></div>
    <form onSubmit={submit} className="settings-form"><div className="roles-list">{ROLES.map((role) => <label className="role-setting" key={role}><span>{roleLabel[role]}<small>{role}</small></span><select aria-label={`${roleLabel[role]}模型`} value={roles[role]?.provider && roles[role]?.model ? `${roles[role].provider}|${roles[role].model}` : ""} onChange={(event) => { const chosen = options.find((option) => option.value === event.target.value); setRoles((previous) => ({ ...previous, [role]: chosen ? { provider: chosen.provider, model: chosen.model } : {} })); setFeedback(null); }}><option value="">使用默认模型</option>{options.map((option) => <option key={`${role}-${option.value}`} value={option.value}>{option.label}</option>)}</select></label>)}</div>
      {options.length === 0 && <p className="inline-note">先在左侧添加供应商和模型，再为角色分配模型。</p>}
      <label>角色默认推理档位<select value={thinking} onChange={(event) => setThinking(event.target.value)}>{THINKING_LEVELS.map((level) => <option key={level} value={level}>{thinkingLabel[level]}</option>)}</select><small>模型能力已知时会按支持集合校准；未声明能力的模型会使用 Pi 默认值。</small></label>
      <div className="form-footer"><button className="primary-button" type="submit" disabled={saving}>{saving ? "保存中…" : "保存角色分配"}</button>{feedback && <span className={`feedback ${feedback.type}`} role="status">{feedback.type === "success" && <Check size={14} />}{feedback.text}</span>}</div>
    </form><div className="security-note"><KeyRound size={16} /><span>密钥只写入当前用户的私有目录。页面只显示设置状态，已有密钥不会回传。运行中的 Agent 继续使用启动时的配置。</span></div>
  </section>;
}

function Settings({ config, error, reload, onSaved }: { config: Config | null; error: string | null; reload: () => void; onSaved: (config: Config) => void }) {
  return <><div className="page-head"><div><span className="eyebrow">SYSTEM / CONFIGURATION</span><h1>模型配置</h1><p>连接供应商，并指定各 Agent 使用的模型。</p></div><span className="local-badge"><KeyRound size={14} /> 本机配置</span></div>
    {error && <div className="error-banner"><WifiOff size={17} />{error}<button type="button" onClick={reload}>重试</button></div>}
    {config ? <div className="settings-grid"><ProviderForm config={config} onSaved={onSaved} /><RolesForm config={config} onSaved={onSaved} /></div> : !error && <Empty text="正在读取配置…" />}
  </>;
}

export default function App() {
  const [view, setView] = useState<View>("overview");
  const [recordTab, setRecordTab] = useState<RecordTab>("tasks");
  const [realState, setRealState] = useState<Snapshot | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedMission, setSelectedMission] = useState<string | undefined>();
  const [refreshSerial, setRefreshSerial] = useState(0);
  const [demo, setDemo] = useState(false);
  const [demoStartedAt, setDemoStartedAt] = useState(Date.now);
  const [demoObjective, setDemoObjective] = useState(() => window.localStorage.getItem("rsi-demo-objective") || "探索一个值得研究的问题");
  const [objectiveDraft, setObjectiveDraft] = useState(demoObjective);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [eventId, setEventId] = useState<number | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configSerial, setConfigSerial] = useState(0);
  const [runtime, setRuntime] = useState<RuntimeState | null>(null);
  const [lastSync, setLastSync] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [worldSettings, setWorldSettings] = useState(loadWorldSettings);
  const [sceneMenuOpen, setSceneMenuOpen] = useState(false);
  const [missionOpen, setMissionOpen] = useState(false);
  const saveWorldSettings = (value: WorldSettings) => { setWorldSettings(value); try { window.localStorage.setItem("rsi-world-settings", JSON.stringify(value)); } catch { /* Scene remains usable without browser storage. */ } };
  useEffect(() => {
    let active = true; let busy = false; let runtimeBusy = false;
    const load = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try { const state = await fetchState(selectedMission); if (active) { setRealState(state); setLastSync(Date.now()); setStateError(null); setLoading(false); } }
      catch (error) { if (active) { setStateError(error instanceof Error ? error.message : "同步失败"); setLoading(false); } }
      finally { busy = false; }
    };
    const loadRuntime = async () => {
      if (runtimeBusy || document.hidden) return;
      runtimeBusy = true;
      try { const status = await fetchRuntime(); if (active) setRuntime(status); }
      catch { if (active) setRuntime(null); }
      finally { runtimeBusy = false; }
    };
    void load(); void loadRuntime(); const interval = window.setInterval(() => { void load(); void loadRuntime(); setNow(Date.now()); }, 2000);
    const visibility = () => { if (!document.hidden) void load(); };
    document.addEventListener("visibilitychange", visibility);
    return () => { active = false; window.clearInterval(interval); document.removeEventListener("visibilitychange", visibility); };
  }, [selectedMission, refreshSerial]);
  useEffect(() => {
    if (view !== "settings") return;
    let active = true;
    fetchConfig().then((value) => { if (active) { setConfig(value); setConfigError(null); } }).catch((error) => { if (active) setConfigError(error instanceof Error ? error.message : "配置读取失败"); });
    return () => { active = false; };
  }, [view, configSerial]);
  const demoStage = demoStageAt((now - demoStartedAt) / 1000);
  const demoState = useMemo(() => demoSnapshot(demoObjective, (now - demoStartedAt) / 1000, demoStartedAt), [demoObjective, demoStartedAt, demoStage]);
  const snapshot = demo ? demoState : realState || { missions: [], mission: null, tasks: [], events: [], runs: [], updated_at: "" };
  const snapshotTime = Date.parse(snapshot.updated_at);
  const feedFresh = Number.isFinite(snapshotTime) && now - snapshotTime < 9000;
  const connected = demo || (!stateError && now - lastSync < 9000 && feedFresh);
  const openTask = (id: string) => { setTaskId(id); setEventId(null); setRecordTab("tasks"); setView("records"); };
  const openEvent = (id: number) => { setEventId(id); setTaskId(null); setRecordTab("events"); setView("records"); };
  const changeMission = (id: string) => { setSelectedMission(id || undefined); setRealState(null); setLoading(true); setTaskId(null); setEventId(null); };
  const enterDemo = () => { setDemoStartedAt(Date.now()); setDemo(true); };
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { setSceneMenuOpen(false); setMissionOpen(false); setView("overview"); } };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  const openView = (next: View) => { setSceneMenuOpen(false); setMissionOpen(false); setView(next); };
  return <div className="immersive-app">
    <WorldScene tasks={snapshot.tasks} events={snapshot.events} objective={snapshot.mission?.objective || ""} settings={worldSettings} demo={demo} live={snapshot.mission?.status === "active" && connected} missionId={demo ? `demo-collaboration-${demoStartedAt}` : snapshot.mission?.mission_id || "empty"} onTask={openTask} onEvent={openEvent} />
    <nav className="immersive-nav" aria-label="场景操作">
      <div className="scene-menu-anchor"><button type="button" aria-expanded={sceneMenuOpen} onClick={() => setSceneMenuOpen((value) => !value)}>场景</button>
        {sceneMenuOpen && <div className="scene-menu"><div className="scene-menu-title">选择场景</div><div className="segmented" role="group" aria-label="场景风格"><button type="button" className={worldSettings.style === "meadow" ? "active" : ""} onClick={() => { saveWorldSettings({ ...worldSettings, style: "meadow" }); setSceneMenuOpen(false); }}>草地</button><button type="button" className={worldSettings.style === "office" ? "active" : ""} onClick={() => { saveWorldSettings({ ...worldSettings, style: "office" }); setSceneMenuOpen(false); }}>工作室</button></div><button type="button" className="scene-menu-link" onClick={() => openView("graph")}>关系图</button><button type="button" className="scene-menu-link" onClick={() => { setRecordTab("events"); openView("records"); }}>任务与消息</button><div className="scene-menu-advanced"><span>更多场景设置</span><WorldControls value={worldSettings} onChange={saveWorldSettings} /></div></div>}
      </div>
      <button type="button" onClick={() => { setSceneMenuOpen(false); setMissionOpen(true); }}>任务</button>
      <button type="button" aria-pressed={demo} onClick={() => { setSceneMenuOpen(false); if (demo) setDemo(false); else enterDemo(); }}> {demo ? "退出演示" : "演示"}</button>
      <button type="button" onClick={() => openView("settings")}>设置</button>
    </nav>
    {stateError && !demo && <div className="immersive-sync" role="alert"><WifiOff size={14} /><span>连接中断</span><button type="button" onClick={() => setRefreshSerial((value) => value + 1)} aria-label="重新连接" title="重新连接"><RefreshCw size={14} /></button></div>}
    {missionOpen && <div className="immersive-shade" onPointerDown={(event) => { if (event.target === event.currentTarget) setMissionOpen(false); }}><section className="immersive-drawer" role="dialog" aria-modal="true" aria-label="研究任务"><div className="immersive-panel-head"><h1>研究任务</h1><button type="button" aria-label="关闭研究任务" title="关闭" onClick={() => setMissionOpen(false)}><X size={18} /></button></div>
      {!demo && <label className="immersive-mission-select">当前任务<select aria-label="选择研究任务" value={selectedMission || snapshot.mission?.mission_id || ""} disabled={!realState?.missions.length} onChange={(event) => changeMission(event.target.value)}>{!realState?.missions.length && <option value="">暂无任务</option>}{realState?.missions.map((mission) => <option key={mission.mission_id} value={mission.mission_id}>{mission.objective || mission.mission_id}</option>)}</select></label>}
      {!demo && <MissionControls mission={snapshot.mission} runtime={runtime} demo={false} onCreated={(id) => { setDemo(false); changeMission(id); setRefreshSerial((value) => value + 1); }} onChanged={() => setRefreshSerial((value) => value + 1)} onDemo={enterDemo} />}
      {demo && <form className="demo-objective" onSubmit={(event) => { event.preventDefault(); const value = objectiveDraft.trim(); if (value) { setDemoObjective(value); setDemoStartedAt(Date.now()); window.localStorage.setItem("rsi-demo-objective", value); } }}><label htmlFor="demo-objective">演示研究目标</label><input id="demo-objective" value={objectiveDraft} onChange={(event) => setObjectiveDraft(event.target.value)} maxLength={300} required /><button className="quiet-button" type="submit"><Plus size={14} /> 更新场景任务</button></form>}
      <button className="scene-menu-link" type="button" onClick={() => { setRecordTab("tasks"); openView("records"); }}>查看全部任务与消息 <ArrowRight size={14} /></button>
      <div className="immersive-drawer-status">{demo ? "演示数据" : loading ? "同步中" : connected ? "已连接本机服务" : "数据同步延迟"}{!demo && realState ? ` · 更新于 ${timeLabel(realState.updated_at)}` : ""}</div>
    </section></div>}
    {view !== "overview" && <div className="immersive-shade immersive-view-shade" onPointerDown={(event) => { if (event.target === event.currentTarget) setView("overview"); }}><section className={`immersive-view immersive-view-${view}`} role="dialog" aria-modal="true" aria-label={view === "settings" ? "模型配置" : view === "graph" ? "关系图" : "任务与消息"}><button type="button" className="immersive-view-close" aria-label="返回场景" title="返回场景" onClick={() => setView("overview")}><X size={19} /></button>
      {view === "records" && <Records snapshot={snapshot} tab={recordTab} setTab={setRecordTab} taskId={taskId} eventId={eventId} selectTask={openTask} selectEvent={openEvent} />}
      {view === "settings" && <Settings config={config} error={configError} reload={() => setConfigSerial((value) => value + 1)} onSaved={setConfig} />}
      {view === "graph" && <><div className="page-head"><div><h1>协作关系</h1></div></div><Scene tasks={snapshot.tasks} events={snapshot.events} live={snapshot.mission?.status === "active" && connected} missionId={snapshot.mission?.mission_id || "empty"} onTask={openTask} onEvent={openEvent} /></>}
    </section></div>}
  </div>;
}
