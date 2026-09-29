import type { Task } from "../types";

export interface Point { x: number; y: number }
export interface GroupLayout {
  id: string;
  label: string;
  taskIds: string[];
  visibleCount: number;
  total: number;
  pageCount: number;
  zone?: { x: number; y: number; rx: number; ry: number; tone: number };
}
export interface Reservation { groupId: string; page: number; slot: number; home: Point; overview: boolean }
export interface LayoutState {
  anchors: Map<string, number>;
  reservations: Map<string, Reservation>;
}
export interface SceneLayout {
  tasks: Map<string, Task>;
  parents: Map<string, string | null>;
  coordinator: string | null;
  selected: string[];
  groups: GroupLayout[];
  reservations: Map<string, Reservation>;
}

const DESKTOP_CENTER = { x: 432, y: 250 };
const MOBILE_CENTER = { x: 252, y: 300 };
const DESKTOP_ZONES = [194, 432, 670];
const MOBILE_ZONES = [100, 252, 404];
const PAGE_CAPACITY = 5;

function overviewSeats(mobile: boolean, zone: number): Point[] {
  const x = (mobile ? MOBILE_ZONES : DESKTOP_ZONES)[zone % 3];
  return mobile ? [{ x, y: 450 }, { x: x - 36, y: 630 }] : [{ x, y: 375 }, { x: x - 52, y: 475 }, { x: x + 52, y: 475 }];
}

function focusSeats(mobile: boolean): Point[] {
  return mobile
    ? [{ x: 252, y: 450 }, { x: 70, y: 630 }, { x: 160, y: 630 }, { x: 344, y: 630 }, { x: 434, y: 630 }]
    : [{ x: 432, y: 375 }, { x: 180, y: 475 }, { x: 284, y: 475 }, { x: 580, y: 475 }, { x: 684, y: 475 }];
}

export function sceneSlots(mobile: boolean): readonly Point[] {
  return [mobile ? MOBILE_CENTER : DESKTOP_CENTER, ...[0, 1, 2].flatMap((zone) => overviewSeats(mobile, zone))];
}

function taskOrder(a: Task, b: Task): number {
  return a.created_at.localeCompare(b.created_at) || a.task_id.localeCompare(b.task_id);
}

export function deduplicateTasks(tasks: Task[]): Map<string, Task> {
  const unique = new Map<string, Task>();
  for (const task of tasks) {
    const previous = unique.get(task.task_id);
    if (!previous || task.updated_at > previous.updated_at || (task.updated_at === previous.updated_at && JSON.stringify(task) > JSON.stringify(previous))) unique.set(task.task_id, task);
  }
  return new Map([...unique.values()].sort(taskOrder).map((task) => [task.task_id, task]));
}

function normalizedParents(tasks: Map<string, Task>, coordinator: string | null): Map<string, string | null> {
  const parents = new Map<string, string | null>();
  for (const task of tasks.values()) parents.set(task.task_id, task.parent_task_id && tasks.has(task.parent_task_id) && task.parent_task_id !== task.task_id ? task.parent_task_id : null);
  if (coordinator) parents.set(coordinator, null);
  for (const id of tasks.keys()) {
    const chain: string[] = [];
    const seen = new Map<string, number>();
    let current: string | null = id;
    while (current && !seen.has(current)) {
      seen.set(current, chain.length);
      chain.push(current);
      current = parents.get(current) ?? null;
    }
    if (current) parents.set(chain.slice(seen.get(current)).sort()[0], null);
  }
  return parents;
}

export function buildSceneLayout(tasksInput: Task[], mobile: boolean, state: LayoutState, focusGroup: string | null, focusPage: number): SceneLayout {
  const overviewCapacity = mobile ? 2 : 3;
  const tasks = deduplicateTasks(tasksInput);
  const coordinator = [...tasks.values()].find((task) => task.role === "coordinator")?.task_id ?? null;
  const parents = normalizedParents(tasks, coordinator);
  const children = new Map([...tasks.keys()].map((id) => [id, [] as string[]]));
  for (const [id, parent] of parents) if (parent) children.get(parent)?.push(id);
  const roots = [...tasks.keys()].filter((id) => !parents.get(id) && id !== coordinator);
  const branchRoots = coordinator ? [...(children.get(coordinator) ?? []), ...roots] : roots;
  const groups: GroupLayout[] = [];
  for (const root of branchRoots) {
    const members: string[] = [];
    const queue = [root];
    while (queue.length) {
      const id = queue.shift()!;
      members.push(id);
      queue.push(...(children.get(id) ?? []));
    }
    groups.push({ id: root, label: tasks.get(root)!.objective || tasks.get(root)!.role, taskIds: members, visibleCount: 0, total: members.length, pageCount: 1 });
  }

  const previous = state.reservations;
  const anchors = new Map([...state.anchors].filter(([id, zone]) => groups.some((group) => group.id === id) && Number.isInteger(zone) && zone >= 0));
  const usedZones = new Set(anchors.values());
  for (const group of groups) {
    if (anchors.has(group.id)) continue;
    // A newly arrived parent inherits its former orphan branch's zone.
    const inherited = group.taskIds.map((id) => state.anchors.get(id)).find((zone) => zone !== undefined && !usedZones.has(zone));
    let zone = inherited;
    if (zone === undefined) {
      zone = 0;
      while (usedZones.has(zone)) zone++;
    }
    anchors.set(group.id, zone);
    usedZones.add(zone);
  }

  const reservations = new Map<string, Reservation>();
  if (coordinator) reservations.set(coordinator, { groupId: "", page: 0, slot: 0, home: mobile ? MOBILE_CENTER : DESKTOP_CENTER, overview: true });
  for (const group of groups) {
    const zone = anchors.get(group.id)!;
    const focused = focusGroup === group.id;
    const seats = focused ? focusSeats(mobile) : overviewSeats(mobile, zone);
    const used = new Map<number, Set<number>>();
    const assign = (id: string, page: number, slot: number): void => {
      const occupied = used.get(page) ?? new Set<number>();
      occupied.add(slot); used.set(page, occupied);
      const overview = zone < 3 && page === 0 && slot < overviewCapacity;
      const home = focused ? seats[slot] : zone < 3 ? overviewSeats(mobile, zone)[Math.min(slot, overviewCapacity - 1)] : { x: (mobile ? 504 : 864) + 120 + zone * 90, y: mobile ? 450 : 375 };
      reservations.set(id, { groupId: group.id, page, slot, home, overview });
    };
    for (const id of group.taskIds) {
      const saved = previous.get(id);
      if (saved && saved.groupId !== group.id && state.anchors.get(saved.groupId) === zone && saved.page === 0 && saved.slot < overviewCapacity && !used.get(0)?.has(saved.slot)) assign(id, 0, saved.slot);
    }
    for (const id of group.taskIds) {
      const saved = previous.get(id);
      if (saved?.groupId === group.id && saved.page >= 0 && saved.slot >= 0 && saved.slot < PAGE_CAPACITY && !used.get(saved.page)?.has(saved.slot) && !(saved.page > 0 && saved.slot === reservations.get(group.id)?.slot)) {
        assign(id, saved.page, saved.slot);
      }
    }
    for (const id of group.taskIds) {
      if (reservations.has(id)) continue;
      let page = 0;
      const missingParent = tasks.get(id)!.parent_task_id && !tasks.has(tasks.get(id)!.parent_task_id!);
      let slot = id === group.id && !missingParent ? 0 : 1;
      while (used.get(page)?.has(slot) || slot >= PAGE_CAPACITY || page > 0 && slot === reservations.get(group.id)?.slot) {
        slot++;
        if (slot >= PAGE_CAPACITY) { page++; slot = 1; }
      }
      assign(id, page, slot);
    }
    group.pageCount = Math.max(1, ...group.taskIds.map((id) => reservations.get(id)!.page + 1));
    if (focused) group.zone = mobile ? { x: 252, y: 540, rx: 223, ry: 112, tone: zone % 3 } : { x: 432, y: 425, rx: 291, ry: 66, tone: zone % 3 };
    else if (zone < 3) group.zone = mobile ? { x: MOBILE_ZONES[zone] - 18, y: 540, rx: 73, ry: 106, tone: zone } : { x: DESKTOP_ZONES[zone], y: 425, rx: 104, ry: 66, tone: zone };
  }

  const selected: string[] = coordinator ? [coordinator] : [];
  if (focusGroup && groups.some((group) => group.id === focusGroup)) {
    const group = groups.find((item) => item.id === focusGroup)!;
    selected.push(group.id);
    for (const id of group.taskIds.slice(1)) if (reservations.get(id)!.page === focusPage) selected.push(id);
  } else {
    for (const group of groups) if (reservations.get(group.id)!.overview) selected.push(group.id);
    for (const group of groups) for (const id of group.taskIds.slice(1)) if (reservations.get(id)!.overview) selected.push(id);
  }
  const visible = new Set(selected);
  for (const group of groups) {
    group.visibleCount = group.taskIds.filter((id) => visible.has(id)).length;
    if (!group.visibleCount) group.zone = undefined;
  }
  state.anchors = anchors;
  state.reservations = reservations;
  return { tasks, parents, coordinator, selected, groups, reservations };
}
