import type { Event, Task } from "../types";
import type { WorldSettings } from "../worldTypes";
import { findPath } from "./vendor/tileMap";
import { buildSceneLayout, deduplicateTasks, type GroupLayout, type LayoutState, type Point } from "./layout";

export type Action = "idle" | "work" | "read" | "write" | "archive" | "talk" | "wave" | "nap" | "failed";
export interface Actor {
  task: Task;
  x: number;
  y: number;
  home: { x: number; y: number };
  palette: number;
  slot: number;
  direction: number;
  path: { col: number; row: number }[];
  action: Action;
  source: Event | null;
  timer: number;
  returning: boolean;
  arrivalAction: Action;
  snapHomeOnArrival: boolean;
  awaitingCreation: boolean;
  routeRetry: number;
}
export interface World {
  width: number;
  height: number;
  time: number;
  mobile: boolean;
  actors: Map<string, Actor>;
  tiles: number[][];
  blocked: Set<string>;
  board: { x: number; y: number };
  shelf: { x: number; y: number };
  queue: Event[];
  seen: Set<number>;
  settings: WorldSettings;
  highlighted: Event | null;
  highlightTimer: number;
  focusGroup: string | null;
  focusPage: number;
  groups: GroupLayout[];
  tasks: Map<string, Task>;
  actorCache: Map<string, Actor>;
  layoutState: LayoutState;
  orchestration?: { phase: "summon" | "approach" | "brief" | "depart" | "work"; fromId: string; toId: string; progress: number; eventId: number };
  orchestrationClock: number;
  orchestrationDuration: number;
}

const TILE = 24;
export const actionLabels: Record<Action, string> = { idle: "等待调度", work: "电脑工作", read: "检索知识", write: "整理知识", archive: "证据归档", talk: "协作讨论", wave: "派发任务", nap: "等待回复", failed: "任务遇阻" };
export function eventCaption(event: Event): string { return knowledgeAction(event) === "archive" ? "证据归档" : event.title; }
export function knowledgeAction(event: Event): "read" | "write" | "archive" | null {
  if (event.knowledge_action) return event.knowledge_action;
  if (event.kind === "knowledge_read") return "read";
  if (event.kind === "idea_written_to_wiki") return "write";
  return null;
}

function hash(value: string): number {
  let number = 2166136261;
  for (let i = 0; i < value.length; i++) number = Math.imul(number ^ value.charCodeAt(i), 16777619);
  return number >>> 0;
}

export function makeWorld(settings: WorldSettings, mobile: boolean): World {
  const width = mobile ? 504 : 864;
  const height = mobile ? 672 : 504;
  const meadow = settings.style === "meadow";
  return { width, height, mobile, time: 0, actors: new Map(), tiles: Array.from({ length: height / TILE }, () => Array(width / TILE).fill(1)), blocked: new Set(), board: meadow ? { x: mobile ? 403 : 700, y: mobile ? 282 : 226 } : { x: width - 132, y: 102 }, shelf: meadow ? { x: mobile ? 85 : 115, y: mobile ? 280 : 269 } : { x: 80, y: 95 }, queue: [], seen: new Set(), settings, highlighted: null, highlightTimer: 0, focusGroup: null, focusPage: 0, groups: [], tasks: new Map(), actorCache: new Map(), layoutState: { anchors: new Map(), reservations: new Map() }, orchestrationClock: 0, orchestrationDuration: 0 };
}

export function chosenTasks(tasks: Task[], mobile: boolean, focusGroup: string | null = null, focusPage = 0): Task[] {
  const layout = buildSceneLayout(tasks, mobile, { anchors: new Map(), reservations: new Map() }, focusGroup, focusPage);
  return layout.selected.map((id) => layout.tasks.get(id)!);
}

/**
 * Pick the settled action shown at a task's station. A running task defaults
 * to computer work, while the roles with a clear office activity get a
 * matching pose. Travel is handled separately by `petFrame`/the renderer and
 * never changes this settled action.
 */
export function actionForTask(task: Pick<Task, "role" | "status" | "waiting">): Action {
  if (task.status === "failed") return "failed";
  if (task.waiting) return "nap";
  if (task.status !== "running") return "idle";
  switch (task.role) {
    case "researcher":
    case "analyzer":
    case "critic":
      return "read";
    case "hypothesis":
      return "write";
    case "reviewer":
      return "archive";
    default:
      return "work";
  }
}

function baseAction(task: Task): Action {
  return actionForTask(task);
}

function refreshActors(world: World): void {
  const requestedGroup = world.focusGroup;
  const requestedPage = world.focusPage;
  let layout = buildSceneLayout([...world.tasks.values()], world.mobile, world.layoutState, world.focusGroup, world.focusPage);
  world.tasks = layout.tasks;
  if (world.focusGroup && !layout.groups.some((group) => group.id === world.focusGroup)) { world.focusGroup = null; world.focusPage = 0; }
  else if (world.focusGroup) world.focusPage = Math.min(world.focusPage, layout.groups.find((group) => group.id === world.focusGroup)!.pageCount - 1);
  if (world.focusGroup !== requestedGroup || world.focusPage !== requestedPage) {
    layout = buildSceneLayout([...world.tasks.values()], world.mobile, world.layoutState, world.focusGroup, world.focusPage);
  }
  world.groups = layout.groups;
  const previous = world.actors;
  if ([...previous.values()].some((actor) => actor.source && !layout.selected.includes(actor.task.task_id)) ||
    requestedGroup !== world.focusGroup || requestedPage !== world.focusPage) {
    clearTransient(world);
    for (const actor of world.actorCache.values()) { actor.x = actor.home.x; actor.y = actor.home.y; }
  }
  world.actors = new Map();
  for (const id of layout.selected) {
    const task = layout.tasks.get(id)!;
    const reservation = layout.reservations.get(id)!;
    const existing = world.actorCache.get(id);
    if (existing) {
      const homeChanged = existing.home.x !== reservation.home.x || existing.home.y !== reservation.home.y;
      existing.task = task;
      existing.home = reservation.home;
      existing.slot = reservation.slot;
      if (!previous.has(id)) {
        existing.x = existing.home.x; existing.y = existing.home.y;
        existing.path = []; existing.source = null; existing.returning = false; existing.snapHomeOnArrival = false;
        existing.awaitingCreation = false;
        existing.routeRetry = 0;
      }
      if (!existing.source && !existing.path.length && !existing.awaitingCreation && !existing.snapHomeOnArrival) {
        if (homeChanged) { existing.x = existing.home.x; existing.y = existing.home.y; }
        existing.action = baseAction(task);
      }
      world.actors.set(id, existing);
    } else {
      const actor: Actor = { task, x: reservation.home.x, y: reservation.home.y, home: reservation.home, slot: reservation.slot, palette: hash(id) % 6, direction: 0, path: [], action: baseAction(task), source: null, timer: 0, returning: false, arrivalAction: baseAction(task), snapHomeOnArrival: false, awaitingCreation: false, routeRetry: 0 };
      world.actorCache.set(id, actor);
      world.actors.set(id, actor);
    }
  }
  for (const id of world.actorCache.keys()) if (!layout.tasks.has(id)) world.actorCache.delete(id);
  const meadow = world.settings.style === "meadow";
  world.blocked.clear();
  if (meadow) {
    for (let row = 0; row < Math.floor((world.mobile ? 184 : 145) / TILE); row++)
      for (let col = 0; col < world.width / TILE; col++) world.blocked.add(`${col},${row}`);
    const props = [
      { x: world.shelf.x, y: world.shelf.y - 19, rx: 35, ry: 27 },
      { x: world.board.x, y: world.board.y - 22, rx: 77, ry: 25 },
    ];
    for (const prop of props)
      for (let row = Math.floor((prop.y - prop.ry) / TILE); row <= Math.floor((prop.y + prop.ry) / TILE); row++)
        for (let col = Math.floor((prop.x - prop.rx) / TILE); col <= Math.floor((prop.x + prop.rx) / TILE); col++) world.blocked.add(`${col},${row}`);
  } else {
    for (let row = 0; row < 6; row++) for (let col = 0; col < world.width / TILE; col++) world.blocked.add(`${col},${row}`);
    for (const actor of world.actors.values()) {
      for (let row = Math.floor((actor.home.y - 65) / TILE); row <= Math.floor((actor.home.y - 24) / TILE); row++)
        for (let col = Math.floor((actor.home.x - 28) / TILE); col <= Math.floor((actor.home.x + 28) / TILE); col++) world.blocked.add(`${col},${row}`);
    }
  }
}

export function syncWorld(world: World, tasks: Task[], events: Event[], initialize = false): void {
  const known = new Set(world.actorCache.keys());
  world.tasks = deduplicateTasks(tasks);
  refreshActors(world);
  if (initialize) { events.forEach((event) => world.seen.add(event.event_id)); return; }
  for (const event of [...events].reverse()) {
    if (world.seen.has(event.event_id)) continue;
    world.seen.add(event.event_id);
    const age = Date.now() - Date.parse(event.created_at);
    if (age < 90_000 && ["task_created", "direct_message", "communication_published", "agent_tool_audit", "knowledge_read", "idea_written_to_wiki", "run_written_to_wiki"].includes(event.kind)) world.queue.push(event);
  }
  world.queue = world.queue.slice(-30);
  if (!initialize) {
    for (const [index, actor] of [...world.actors.values()].entries()) {
      const id = actor.task.task_id;
      if (known.has(id)) continue;
      if (world.queue.some((event) => event.kind === "task_created" && (event.to_task_id || event.task_id) === id) || !clearOfTraffic(world, actor, actor.home)) {
        // Keep queued arrivals outside the canvas until their parent can summon them.
        actor.x = world.width + 120 + index * 90;
        actor.y = actor.home.y;
        actor.awaitingCreation = true;
        actor.action = "idle";
      }
    }
  }
}

export function setWorldFocus(world: World, id: string | null, page = 0): boolean {
  if (id !== null && !world.groups.some((group) => group.id === id)) return false;
  const group = id ? world.groups.find((item) => item.id === id)! : null;
  const nextPage = group && Number.isFinite(page) ? Math.max(0, Math.min(Math.floor(page), group.pageCount - 1)) : 0;
  if (world.focusGroup === id && world.focusPage === nextPage) return true;
  clearTransient(world);
  for (const actor of world.actorCache.values()) {
    actor.x = actor.home.x; actor.y = actor.home.y;
  }
  world.focusGroup = id;
  world.focusPage = nextPage;
  refreshActors(world);
  return true;
}

function clearOfTraffic(world: World, actor: Actor, point: Point): boolean {
  for (const other of world.actors.values()) {
    if (other === actor || other.awaitingCreation) continue;
    let previous: Point = other;
    for (const next of [other, ...other.path.map((tile) => ({ x: tile.col * TILE + TILE / 2, y: tile.row * TILE + TILE / 2 }))]) {
      if (point.x > Math.min(previous.x, next.x) - 65 && point.x < Math.max(previous.x, next.x) + 65 &&
        point.y > Math.min(previous.y, next.y) - 90 && point.y < Math.max(previous.y, next.y) + 90) return false;
      previous = next;
    }
  }
  return true;
}

function movementBlocks(world: World, actor: Actor): Set<string> {
  const blocked = new Set(world.blocked);
  if (world.settings.style === "meadow") {
    const props = [{ ...world.shelf, rx: 45, top: -49, bottom: 96 }, { ...world.board, rx: 83, top: -54, bottom: 72 }];
    for (let row = 0; row < world.tiles.length; row++) for (let col = 0; col < world.tiles[0].length; col++) {
      const x = col * TILE + TILE / 2; const y = row * TILE + TILE / 2;
      if (x < 33 || x > world.width - 33 || y > world.height - 25 || props.some((prop) => x + 33 > prop.x - prop.rx && x - 33 < prop.x + prop.rx && y + 15 > prop.y + prop.top && y - 70 < prop.y + prop.bottom)) blocked.add(`${col},${row}`);
    }
  }
  for (const other of world.actors.values()) {
    if (other === actor || other.awaitingCreation && other.x > world.width) continue;
    for (const position of [other, other.home, ...other.path.map((tile) => ({ x: tile.col * TILE + TILE / 2, y: tile.row * TILE + TILE / 2 }))]) {
      for (let row = Math.max(0, Math.floor((position.y - 90) / TILE)); row <= Math.min(world.tiles.length - 1, Math.floor((position.y + 90) / TILE)); row++)
        for (let col = Math.max(0, Math.floor((position.x - 65) / TILE)); col <= Math.min(world.tiles[0].length - 1, Math.floor((position.x + 65) / TILE)); col++)
          if (Math.abs(col * TILE + TILE / 2 - position.x) < 65 && Math.abs(row * TILE + TILE / 2 - position.y) < 90) blocked.add(`${col},${row}`);
    }
  }
  return blocked;
}

function freePosition(world: World, actor: Actor, target: Point, maxDistance = 240, minHomeDistance = 0, avoidActors = false): Point | null {
  const candidates: Point[] = [];
  const blocked = avoidActors ? movementBlocks(world, actor) : world.blocked;
  for (let row = 0; row < world.tiles.length; row++) {
    for (let col = 0; col < world.tiles[row].length; col++) {
      if (blocked.has(`${col},${row}`)) continue;
      const point = { x: col * TILE + TILE / 2, y: row * TILE + TILE / 2 };
      if (Math.hypot(point.x - target.x, point.y - target.y) > maxDistance) continue;
      if (Math.hypot(point.x - actor.home.x, point.y - actor.home.y) < minHomeDistance) continue;
      if (point.x < 33 || point.x > world.width - 33 || point.y < 75 || point.y > world.height - 25) continue;
      if (world.settings.style === "meadow") {
        const intersects = (x: number, y: number, rx: number, top: number, bottom: number) => point.x + 33 > x - rx && point.x - 33 < x + rx && point.y + 15 > y + top && point.y - 70 < y + bottom;
        if (intersects(world.shelf.x, world.shelf.y, 45, -49, 96) || intersects(world.board.x, world.board.y, 83, -54, 72)) continue;
      }
      if (!clearOfTraffic(world, actor, point)) continue;
      candidates.push(point);
    }
  }
  candidates.sort((a, b) => Math.hypot(a.x - target.x, a.y - target.y) - Math.hypot(b.x - target.x, b.y - target.y) || a.y - b.y || a.x - b.x);
  return candidates.find((point) => findPath(Math.floor(actor.x / TILE), Math.floor(actor.y / TILE), Math.floor(point.x / TILE), Math.floor(point.y / TILE), world.tiles, blocked).length > 0 || Math.floor(actor.x / TILE) === Math.floor(point.x / TILE) && Math.floor(actor.y / TILE) === Math.floor(point.y / TILE)) ?? null;
}

function spawnNearParent(world: World, actor: Actor, parent: Actor): void {
  const direction = actor.home.x >= parent.x ? 1 : -1;
  const target = { x: parent.x + direction * 88, y: parent.y + (actor.home.y >= parent.y ? 92 : -92) };
  const start = freePosition(world, actor, target, 96, 48, true) ?? freePosition(world, actor, target, 160, 48, true);
  if (!start) return;
  actor.x = start.x; actor.y = start.y;
  actor.action = "idle"; actor.path = [];
}

function route(world: World, actor: Actor, target: Point, action: Action): void {
  const goingHome = target === actor.home;
  if (goingHome && actor.x === target.x && actor.y === target.y) {
    actor.path = []; actor.snapHomeOnArrival = false;
    actor.action = action; actor.arrivalAction = action;
    return;
  }
  const destination = freePosition(world, actor, target, goingHome ? TILE * 1.5 : 240, 0, true);
  const blocked = movementBlocks(world, actor);
  actor.path = destination ? findPath(Math.floor(actor.x / TILE), Math.floor(actor.y / TILE), Math.floor(destination.x / TILE), Math.floor(destination.y / TILE), world.tiles, blocked) : [];
  actor.arrivalAction = action;
  actor.snapHomeOnArrival = goingHome;
  actor.routeRetry = 0.4;
  actor.timer = 4.5;
  if (!actor.path.length) {
    if (goingHome && destination && Math.hypot(actor.x - target.x, actor.y - target.y) <= TILE * 1.5 && clearOfTraffic(world, actor, target)) { actor.x = target.x; actor.y = target.y; actor.snapHomeOnArrival = false; }
    actor.action = action;
  }
}

function startEvent(world: World, event: Event): void {
  const from = event.from_task_id ? world.actors.get(event.from_task_id) : undefined;
  const to = event.to_task_id ? world.actors.get(event.to_task_id) : undefined;
  const taskActor = event.task_id ? world.actors.get(event.task_id) : undefined;
  const actor = from || taskActor;
  world.highlighted = event; world.highlightTimer = 7;
  if (event.kind === "task_created") {
    const targetId = event.to_task_id || event.task_id;
    const child = targetId ? world.actors.get(targetId) : undefined;
    if (from && child && from !== child) {
      // A capped event queue may omit the parent's own creation record.
      if (from.awaitingCreation && clearOfTraffic(world, from, from.home)) {
        from.x = from.home.x; from.y = from.home.y; from.awaitingCreation = false;
      }
      if (child.awaitingCreation) {
        child.x = child.home.x; child.y = child.home.y;
        spawnNearParent(world, child, from);
      }
      child.awaitingCreation = false;
      from.source = event; from.path = []; from.action = "wave";
      child.source = event; child.path = []; child.action = "wave";
      world.orchestration = { phase: "summon", fromId: from.task.task_id, toId: child.task.task_id, progress: 0, eventId: event.event_id };
      world.orchestrationClock = 0;
      world.orchestrationDuration = 0.6;
    } else if (child) {
      child.awaitingCreation = false; child.x = child.home.x; child.y = child.home.y;
      child.source = event; child.action = "work"; child.timer = 1.5;
    }
  } else if (actor) {
    actor.source = event; actor.returning = false;
    if (event.kind === "direct_message" && to) {
      route(world, actor, { x: to.x + 38, y: to.y + 22 }, "talk");
      to.source = event; to.action = "talk"; to.timer = Math.max(4, actor.path.length * TILE / 88 + 3);
    }
    else if (event.kind === "direct_message") route(world, actor, { x: world.board.x, y: world.board.y + 56 }, "talk");
    else if (event.kind === "communication_published") route(world, actor, { x: world.board.x, y: world.board.y + 56 }, "talk");
    else if (knowledgeAction(event) === "read") route(world, actor, { x: world.shelf.x + 25, y: world.shelf.y + 90 }, "read");
    else if (knowledgeAction(event) === "write") route(world, actor, { x: world.shelf.x + 25, y: world.shelf.y + 90 }, "write");
    else if (knowledgeAction(event) === "archive") route(world, actor, { x: world.shelf.x + 50, y: world.shelf.y + 135 }, "archive");
    else { actor.action = "work"; actor.arrivalAction = "work"; actor.timer = 4; }
  }
}

export function replayEvent(world: World, event: Event): void {
  if (world.queue.length === 0) world.queue.push(event);
}

export function clearTransient(world: World): void {
  world.queue = [];
  world.highlighted = null;
  world.highlightTimer = 0;
  world.orchestration = undefined;
  world.orchestrationClock = 0;
  world.orchestrationDuration = 0;
  for (const actor of world.actorCache.values()) {
    actor.path = [];
    actor.snapHomeOnArrival = false;
    actor.source = null;
    actor.returning = false;
    actor.timer = 0;
    actor.routeRetry = 0;
    if (actor.awaitingCreation) { actor.x = actor.home.x; actor.y = actor.home.y; }
    actor.awaitingCreation = false;
    actor.action = baseAction(actor.task);
  }
}

export function updateWorld(world: World, dt: number, moving: boolean): void {
  if (!moving) return;
  world.time += dt;
  world.highlightTimer = Math.max(0, world.highlightTimer - dt);
  if (world.highlightTimer <= 0) world.highlighted = null;
  if (!world.orchestration && ![...world.actors.values()].some((actor) => actor.source || actor.path.length || actor.snapHomeOnArrival)) {
    if (world.queue.length) startEvent(world, world.queue.shift()!);
    else for (const actor of world.actors.values()) {
      if (!actor.awaitingCreation || !clearOfTraffic(world, actor, actor.home)) continue;
      actor.x = actor.home.x; actor.y = actor.home.y;
      actor.awaitingCreation = false;
      actor.action = baseAction(actor.task);
    }
  }
  for (const actor of world.actors.values()) {
    if (actor.snapHomeOnArrival && !actor.path.length) {
      actor.routeRetry -= dt;
      if (actor.routeRetry <= 0) route(world, actor, actor.home, actor.arrivalAction);
      continue;
    }
    if (actor.path.length) {
      let distanceLeft = dt * (actor.source?.kind === "task_created" ? 145 : 88);
      while (actor.path.length && distanceLeft > 0) {
        const next = actor.path[0];
        const tx = next.col * TILE + TILE / 2;
        const ty = next.row * TILE + TILE / 2;
        const dx = tx - actor.x; const dy = ty - actor.y;
        const distance = Math.hypot(dx, dy);
        actor.direction = Math.abs(dx) > Math.abs(dy) ? dx > 0 ? 2 : 1 : dy > 0 ? 0 : 3;
        if (distance <= distanceLeft) { actor.x = tx; actor.y = ty; actor.path.shift(); distanceLeft -= distance; }
        else { actor.x += dx / distance * distanceLeft; actor.y += dy / distance * distanceLeft; distanceLeft = 0; }
      }
      if (!actor.path.length) {
        if (actor.snapHomeOnArrival) { actor.x = actor.home.x; actor.y = actor.home.y; actor.snapHomeOnArrival = false; }
        actor.action = actor.arrivalAction; actor.direction = 0;
      }
      continue;
    }
    if (actor.source && actor.task.task_id !== world.orchestration?.fromId && actor.task.task_id !== world.orchestration?.toId) {
      actor.timer -= dt;
      if (actor.timer <= 0) {
        if (actor.returning) { actor.source = null; actor.returning = false; actor.action = baseAction(actor.task); }
        else { actor.returning = true; route(world, actor, actor.home, baseAction(actor.task)); actor.timer = 0.5; }
      }
    }
  }
  const sequence = world.orchestration;
  if (!sequence) return;
  const from = world.actors.get(sequence.fromId);
  const to = world.actors.get(sequence.toId);
  if (!from || !to) { clearTransient(world); return; }
  world.orchestrationClock += dt;
  sequence.progress = Math.min(1, world.orchestrationClock / world.orchestrationDuration);
  if (world.orchestrationClock < world.orchestrationDuration || (sequence.phase === "approach" || sequence.phase === "depart") && (to.path.length || to.snapHomeOnArrival)) return;
  world.orchestrationClock = 0;
  if (sequence.phase === "summon") {
    sequence.phase = "approach";
    const side = to.home.x >= from.x ? 1 : -1;
    route(world, to, { x: from.x + side * 94, y: from.y + 12 }, "talk");
    to.action = to.path.length ? "wave" : "talk";
    world.orchestrationDuration = Math.max(0.55, to.path.length * TILE / 145 + 0.15);
  } else if (sequence.phase === "approach") {
    sequence.phase = "brief";
    from.action = "talk"; to.action = "talk";
    world.orchestrationDuration = 0.9;
  } else if (sequence.phase === "brief") {
    sequence.phase = "depart";
    from.action = baseAction(from.task);
    route(world, to, to.home, baseAction(to.task));
    to.action = to.path.length ? "wave" : baseAction(to.task);
    world.orchestrationDuration = Math.max(0.5, to.path.length * TILE / 145);
  } else if (sequence.phase === "depart") {
    sequence.phase = "work";
    to.action = baseAction(to.task);
    world.orchestrationDuration = 0.3;
  } else {
    from.source = null; to.source = null;
    from.action = baseAction(from.task); to.action = baseAction(to.task);
    world.orchestration = undefined;
  }
  if (world.orchestration) sequence.progress = 0;
}
