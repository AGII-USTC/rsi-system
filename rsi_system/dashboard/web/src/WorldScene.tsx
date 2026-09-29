import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, X } from "lucide-react";
import type { Event } from "./types";
import type { WorldSceneProps } from "./worldTypes";
import { roleLabel, statusLabel } from "./types";
import { clearTransient, eventCaption, makeWorld, setWorldFocus, syncWorld, updateWorld } from "./world/engine";
import { renderWorld, type HitBox } from "./world/render";
import { sceneAssetsIncomplete } from "./world/assets";
import { loadPetSheets, PET_PRESETS, type PetSheets } from "./world/pets";
import { startGameLoop } from "./world/vendor/gameLoop";
import "./world/world.css";

const MESSAGE_KINDS = new Set(["direct_message", "communication_published"]);
const KNOWLEDGE_KINDS = new Set(["knowledge_read", "idea_written_to_wiki", "run_written_to_wiki"]);

function latestEvents(events: Event[], matches: (event: Event) => boolean): Event[] {
  return events.filter(matches).sort((a, b) => b.event_id - a.event_id);
}

function shortId(id: string): string { return id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id; }

export function WorldScene(props: WorldSceneProps) {
  const stage = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const popover = useRef<HTMLElement>(null);
  const targets = useRef(new Map<string, HTMLButtonElement>());
  const latest = useRef(props);
  latest.current = props;
  const [mobile, setMobile] = useState(() => window.matchMedia("(max-width: 620px)").matches);
  const [loadedAssets, setLoadedAssets] = useState({ pets: 0, legacy: 0 });
  const [ready, setReady] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  const [selection, setSelection] = useState<string | null>(null);
  const [, setSceneRevision] = useState(0);
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const [expanded, setExpanded] = useState(false);
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const world = useRef(makeWorld(props.settings, mobile));
  const images = useRef<HTMLImageElement[]>([]);
  const petSheets = useRef<PetSheets>({});
  const hits = useRef<HitBox[]>([]);
  const wasLive = useRef(props.live);
  const initialized = useRef(false);
  const waves = useRef(new Map<string, number>());

  const positionOverlays = () => {
    const stageElement = stage.current;
    const canvasElement = canvas.current;
    if (!stageElement || !canvasElement) return;
    const stageRect = stageElement.getBoundingClientRect();
    const canvasRect = canvasElement.getBoundingClientRect();
    const scaleX = canvasRect.width / world.current.width;
    const scaleY = canvasRect.height / world.current.height;
    const project = (x: number, y: number) => ({
      x: canvasRect.left - stageRect.left + x * scaleX,
      y: canvasRect.top - stageRect.top + y * scaleY,
    });
    const updateTargets = () => {
      for (const [id, target] of targets.current) {
        const actor = world.current.actors.get(id);
        const box = actor ? world.current.settings.style === "meadow"
          ? { x: actor.x - 48, y: actor.y - 101, width: 96, height: 106 }
          : { x: actor.x - 30, y: actor.y - 70, width: 60, height: 110 }
          : id === "board" ? { x: world.current.board.x - 95, y: world.current.board.y - 65, width: 190, height: 130 }
            : id === "shelf" ? { x: world.current.shelf.x - 55, y: world.current.shelf.y - 65, width: 110, height: 130 } : null;
        if (!box) continue;
        const at = project(box.x, box.y);
        target.style.left = `${at.x}px`;
        target.style.top = `${at.y}px`;
        target.style.width = `${box.width * scaleX}px`;
        target.style.height = `${box.height * scaleY}px`;
      }
    };
    const selected = selectionRef.current;
    const element = popover.current;
    if (!selected || !element) { updateTargets(); return; }
    const actor = world.current.actors.get(selected);
    if (!actor && selected !== "board" && selected !== "shelf") {
      setSelection(null);
      updateTargets();
      return;
    }
    const anchor = actor ? project(actor.x, actor.y - 68)
      : selected === "board" ? project(world.current.board.x, world.current.board.y - 46)
        : project(world.current.shelf.x, world.current.shelf.y - 47);
    const inset = 10;
    const gap = 13;
    const width = element.offsetWidth;
    const preferredHeight = element.offsetHeight;
    const x = Math.max(inset, Math.min(anchor.x - width / 2, stageRect.width - width - inset));
    const roomAbove = anchor.y - gap - inset;
    const roomBelow = stageRect.height - anchor.y - gap - inset;
    const above = preferredHeight <= roomAbove || roomAbove >= roomBelow;
    const limit = `${Math.max(85, Math.min(expandedRef.current ? 340 : 220, above ? roomAbove : roomBelow))}px`;
    const limitChanged = element.style.maxHeight !== limit;
    if (limitChanged) element.style.maxHeight = limit;
    const height = limitChanged ? element.offsetHeight : preferredHeight;
    const desiredY = above ? anchor.y - height - gap : anchor.y + gap;
    const y = Math.max(inset, Math.min(desiredY, stageRect.height - height - inset));
    element.style.left = `${x}px`;
    element.style.top = `${y}px`;
    element.style.setProperty("--world-arrow-x", `${Math.max(15, Math.min(anchor.x - x, width - 15))}px`);
    element.dataset.placement = above ? "above" : "below";
    updateTargets();
  };

  useEffect(() => {
    const media = window.matchMedia("(max-width: 620px)"); const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const resize = () => setMobile(media.matches); const reduce = () => setReduced(motion.matches);
    media.addEventListener("change", resize); motion.addEventListener("change", reduce);
    return () => { media.removeEventListener("change", resize); motion.removeEventListener("change", reduce); };
  }, []);
  useEffect(() => {
    world.current = makeWorld(props.settings, mobile);
    syncWorld(world.current, props.tasks, props.events, true);
    initialized.current = true; wasLive.current = props.live; setHover(null); setSelection(null); setExpanded(false);
    setSceneRevision((revision) => revision + 1);
  }, [props.missionId, props.settings, mobile]);
  useEffect(() => {
    const previousIds = [...world.current.actors.keys()];
    const selectedId = selectionRef.current;
    const previousTask = selectedId ? JSON.stringify(world.current.actors.get(selectedId)?.task) : undefined;
    const previousGroups = JSON.stringify({ groups: world.current.groups, focusGroup: world.current.focusGroup, focusPage: world.current.focusPage });
    syncWorld(world.current, props.tasks, props.events, !initialized.current || !props.live || !wasLive.current);
    initialized.current = true;
    if (selectedId && selectedId !== "board" && selectedId !== "shelf" && !world.current.actors.has(selectedId)) setSelection(null);
    const nextIds = [...world.current.actors.keys()];
    const selectedTask = selectedId ? JSON.stringify(world.current.actors.get(selectedId)?.task) : undefined;
    const nextGroups = JSON.stringify({ groups: world.current.groups, focusGroup: world.current.focusGroup, focusPage: world.current.focusPage });
    if (previousIds.length !== nextIds.length || previousIds.some((id, index) => id !== nextIds[index]) ||
      previousTask !== selectedTask || previousGroups !== nextGroups) setSceneRevision((revision) => revision + 1);
  }, [props.tasks, props.events, props.live]);
  useEffect(() => {
    if (props.live !== wasLive.current) {
      clearTransient(world.current);
      syncWorld(world.current, props.tasks, props.events, true);
    }
    wasLive.current = props.live;
  }, [props.live]);
  useEffect(() => {
    let active = true;
    const legacySheets = Promise.allSettled(Array.from({ length: 6 }, (_, index) => new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = `/assets/characters/char_${index}.png`;
    })));
    Promise.all([loadPetSheets().catch(() => ({} as PetSheets)), legacySheets]).then(([pets, results]) => {
      if (!active) return;
      petSheets.current = pets;
      images.current = results.map((result) => result.status === "fulfilled" ? result.value : undefined) as HTMLImageElement[];
      setLoadedAssets({ pets: Object.keys(pets).length, legacy: results.filter((result) => result.status === "fulfilled").length });
      setReady(true);
    });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const resize = () => {
      const size = Math.max(1, element.clientWidth);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      element.width = Math.round(size * dpr); element.height = Math.round(size * world.current.height / world.current.width * dpr);
      positionOverlays();
    };
    resize(); const observer = new ResizeObserver(resize); observer.observe(element);
    const stop = startGameLoop(element, {
      update: (dt) => {
        const current = latest.current;
        const moving = current.settings.motion && !reduced && current.live && !document.hidden;
        updateWorld(world.current, dt, moving);
      },
      render: (ctx) => { hits.current = renderWorld(ctx, world.current, images.current, latest.current.events, undefined, petSheets.current, world.current.settings.style !== "meadow"); positionOverlays(); },
    });
    return () => { stop(); observer.disconnect(); };
  }, [mobile, props.settings, reduced]);
  useEffect(() => () => { for (const timer of waves.current.values()) window.clearTimeout(timer); }, []);
  useLayoutEffect(() => { positionOverlays(); }, [selection, expanded, mobile, props.tasks, props.events]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape" && selectionRef.current) { setSelection(null); setExpanded(false); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);

  const locate = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * world.current.width; const y = (event.clientY - rect.top) / rect.height * world.current.height;
    return [...hits.current].reverse().find((hit) => x >= hit.x && x <= hit.x + hit.width && y >= hit.y && y <= hit.y + hit.height)?.id || null;
  };
  const locateProp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * world.current.width;
    const y = (event.clientY - rect.top) / rect.height * world.current.height;
    const { board, shelf } = world.current;
    if (Math.abs(x - board.x) < 95 && Math.abs(y - board.y) < 65) return "board";
    if (Math.abs(x - shelf.x) < 55 && Math.abs(y - shelf.y) < 65) return "shelf";
    return null;
  };
  const select = (id: string | null) => {
    setSelection(id);
    setExpanded(false);
    if (!id) return;
    const actor = world.current.actors.get(id);
    if (!actor || actor.source || actor.path.length || waves.current.has(id)) return;
    const previous = actor.action;
    actor.action = "wave";
    const timer = window.setTimeout(() => {
      waves.current.delete(id);
      if (world.current.actors.get(id) === actor && actor.action === "wave" && !actor.source) actor.action = previous;
    }, 1100);
    waves.current.set(id, timer);
  };
  const focusGroup = (id: string | null, page = 0) => {
    if (!setWorldFocus(world.current, id, page)) return;
    setSelection(id);
    setExpanded(false);
    setSceneRevision((revision) => revision + 1);
  };

  const actor = selection && selection !== "board" && selection !== "shelf" ? world.current.actors.get(selection) : null;
  const associated = actor ? latestEvents(props.events, (event) =>
    (MESSAGE_KINDS.has(event.kind) || KNOWLEDGE_KINDS.has(event.kind) || !!event.knowledge_action) &&
    [event.task_id, event.from_task_id, event.to_task_id].includes(actor.task.task_id))
    : latestEvents(props.events, (event) => selection === "board" ? MESSAGE_KINDS.has(event.kind) : KNOWLEDGE_KINDS.has(event.kind) || !!event.knowledge_action);
  const latestEvent = associated[0];
  const shown = selection === "board" ? "讨论黑板" : selection === "shelf" ? "知识书架" : actor ? roleLabel[actor.task.role] || actor.task.role : "";
  const subtitle = actor ? `${statusLabel[actor.task.status] || actor.task.status}${actor.task.waiting ? " · 等待回复" : ""}` : selection === "board" ? "最近讨论" : "最近知识";
  const selectedGroup = actor ? world.current.groups.find((group) => group.id === actor.task.task_id) : undefined;
  const isCoordinator = actor?.task.role === "coordinator";
  const focusedGroup = world.current.focusGroup ? world.current.groups.find((group) => group.id === world.current.focusGroup) : undefined;
  const actorIds = [...world.current.actors.keys()];
  const assetError = ready && sceneAssetsIncomplete(props.settings.style, loadedAssets.pets, PET_PRESETS.length, loadedAssets.legacy, 6);

  return <div className="living-world"><div className={`world-stage${props.settings.style === "office" ? " is-office" : ""}`} ref={stage} onPointerDown={(event) => { if (event.target === event.currentTarget) select(null); }}>
    <canvas ref={canvas} role="img" aria-label={`${props.settings.style === "meadow" ? "草地" : "工作室"}协作场景，${props.tasks.length} 个 Agent 任务；点击人物或场景物件查看详情`} className={hover ? "is-interactive" : ""} style={{ aspectRatio: `${world.current.width} / ${world.current.height}` }} onPointerMove={(event) => setHover(locate(event) || locateProp(event))} onPointerLeave={() => setHover(null)} onPointerDown={(event) => select(locate(event) || locateProp(event))} />
    {actorIds.map((id) => { const item = world.current.actors.get(id)!; return <button key={id} ref={(node) => { if (node) targets.current.set(id, node); else targets.current.delete(id); }} className="world-focus-target" type="button" aria-label={`${roleLabel[item.task.role] || item.task.role}：${item.task.objective}`} onClick={() => select(id)} />; })}
    {(["board", "shelf"] as const).map((id) => <button key={id} ref={(node) => { if (node) targets.current.set(id, node); else targets.current.delete(id); }} className="world-focus-target" type="button" aria-label={id === "board" ? "讨论黑板详情" : "知识书架详情"} onClick={() => select(id)} />)}
    {!ready && <span className="world-state">人物正在进入场景…</span>}
    {assetError && <span className="world-state error">{loadedAssets.pets + loadedAssets.legacy ? "部分人物素材未加载，已使用可用素材替代" : "人物素材未加载，请刷新重试"}</span>}
    {focusedGroup && <div className="world-group-controls"><button type="button" onClick={() => focusGroup(null)}><ArrowLeft size={14} />返回全景</button>{focusedGroup.pageCount > 1 && <div className="world-group-pages"><button type="button" aria-label="上一页小组成员" title="上一页" disabled={world.current.focusPage === 0} onClick={() => focusGroup(focusedGroup.id, world.current.focusPage - 1)}><ChevronLeft size={15} /></button><span>{world.current.focusPage + 1}/{focusedGroup.pageCount}</span><button type="button" aria-label="下一页小组成员" title="下一页" disabled={world.current.focusPage >= focusedGroup.pageCount - 1} onClick={() => focusGroup(focusedGroup.id, world.current.focusPage + 1)}><ChevronRight size={15} /></button></div>}</div>}
    {selection && (actor || selection === "board" || selection === "shelf") && <section ref={popover} className={`world-context${expanded ? " is-expanded" : ""}`} role="dialog" aria-label={`${shown}详情`} data-placement="above">
      <button className="world-context-close" type="button" aria-label="关闭场景详情" title="关闭" onClick={() => select(null)}><X size={16} /></button>
      <div className="world-context-scroll"><span className="world-context-label">{shown}</span><span className="world-context-status">{subtitle}</span>
        {actor ? <><h2>{actor.task.objective}</h2><p className="world-context-summary">{actor.task.summary || actor.task.reason || "暂无任务摘要"}</p><div className="world-context-meta"><span title={actor.task.task_id}>任务 {shortId(actor.task.task_id)}</span>{actor.task.parent_task_id && <span title={actor.task.parent_task_id}>上级 {shortId(actor.task.parent_task_id)}</span>}</div>
          {expanded && <div className="world-context-details"><div><strong>任务 ID</strong><span>{actor.task.task_id}</span></div>{actor.task.parent_task_id && <div><strong>上级 ID</strong><span>{actor.task.parent_task_id}</span></div>}<div><strong>任务状态</strong><span>{statusLabel[actor.task.status] || actor.task.status}{actor.task.waiting ? " · 等待回复" : ""}</span></div>{actor.task.error && <div><strong>问题</strong><span>{actor.task.error}</span></div>}{actor.task.reason && actor.task.reason !== actor.task.summary && <div><strong>缘由</strong><span>{actor.task.reason}</span></div>}</div>}</>
          : <><h2>{latestEvent ? eventCaption(latestEvent) : selection === "board" ? "暂无讨论记录" : "暂无知识记录"}</h2><p className="world-context-summary">{latestEvent?.text || "等待新的协作记录"}</p></>}
        {isCoordinator && world.current.groups.length > 0 && <div className="world-context-groups"><strong>研究小组 · {world.current.groups.length}</strong><div className="world-context-group-list">{world.current.groups.map((group) => <button key={group.id} type="button" title={group.label} onClick={() => focusGroup(group.id)}><span>{group.label}</span><small>{group.total} 人</small><ChevronRight size={13} /></button>)}</div></div>}
        {expanded && <div className="world-context-records"><strong>{actor ? "关联消息与知识" : "最近记录"}</strong>{associated.length ? associated.slice(0, 4).map((event) => <article key={event.event_id}><span>{eventCaption(event)} · #{event.event_id}</span><p>{event.text || event.title}</p></article>) : <p>暂无关联记录</p>}</div>}
      </div>{selectedGroup && world.current.focusGroup !== selectedGroup.id && <button className="world-context-group" type="button" onClick={() => focusGroup(selectedGroup.id)}>查看小组 · {selectedGroup.total} 人 <ChevronRight size={14} /></button>}<button className="world-context-expand" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? "收起详情" : "展开详情"}{expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</button>
    </section>}
  </div></div>;
}
